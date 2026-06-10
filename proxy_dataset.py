"""
proxy_dataset.py — Aerodrome CL proxy dataset for phase-2 scoring.

Why: ~20 Hydrex bootstrap outcomes is too small to learn feature weights from.
Fix: use Aerodrome CL pool history as a proxy — same ve(3,3) incentive structure
on the same chain — and label each pool-epoch profitable = (fees > emissions).

Data sources (no Dune required):
  1. Gauge emissions per epoch — on-chain eth_getLogs (NotifyReward events)
     Voter: 0x16613524e02ad97eDfeF371bC883F2F5d6C480A5  (BASE_RPC_URL)
  2. Pool weekly fees/volume/TVL — Aerodrome Base Full subgraph via The Graph
     Subgraph ID: GENunSHWLBXm59mBSgPzQ8metBEp9YDfdqwFr91Av1UM
     Requires THEGRAPH_API_KEY.
     Queried only for pools that received emissions (not all 3,400+ pools).
  3. AERO/USD price — CoinGecko public API (no key required).

Pipeline order: RPC emissions first → extract incentivised pool list →
targeted subgraph fetch → merge → label → write.

Alternatively: export any Dune query as CSV and save to
  data/aerodrome_proxy_raw.csv — that file is read as a primary source if present.

Writes data/aerodrome_proxy.csv — input to `score.py --feature-importance`.
"""

import csv
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from web3 import Web3

# ── paths & config ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG     = json.loads((SCRIPT_DIR / "selection_config.json").read_text())
OUT_CSV    = SCRIPT_DIR / "data" / "aerodrome_proxy.csv"
RAW_CSV    = SCRIPT_DIR / "data" / "aerodrome_proxy_raw.csv"  # manual-export fallback

PCFG = CONFIG["proxy_dataset"]

# ── Aerodrome contract addresses (Base mainnet, verified via BaseScan) ────────
VOTER_ADDR           = "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"
NOTIFY_REWARD_TOPIC  = "0x095667752957714306e1a6ad83495404412df6fdb932fca6dc849a7ee910d4c1"
SUBGRAPH_ID          = "GENunSHWLBXm59mBSgPzQ8metBEp9YDfdqwFr91Av1UM"
NULL_ADDR            = "0x0000000000000000000000000000000000000000"

# CLGauge2 (Vyper): public storage variable `pool` — getter takes no args
GAUGE_POOL_ABI = [{
    "name":            "pool",
    "inputs":          [],
    "outputs":         [{"name": "", "type": "address"}],
    "stateMutability": "view",
    "type":            "function",
}]

START_DT    = datetime(2025, 1, 1, tzinfo=timezone.utc)
START_TS    = int(START_DT.timestamp())   # 1_735_689_600
LOG_CHUNK   = 2000    # Alchemy eth_getLogs max block range per request
GRAPH_PAGE  = 500     # rows per subgraph page (keeps response < 200 KB)
POOL_BATCH  = 50      # pool addresses per pool_in filter clause
GRAPH_TIMEOUT = 60    # seconds per subgraph request


# ── helpers ───────────────────────────────────────────────────────────────────

def _f(x, d=0.0):
    try:    return float(x)
    except: return d


def _week_key(ts: int) -> str:
    """Return ISO-week string (e.g. '2025-W03') for a Unix timestamp."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-W%V")


# ── 1. On-chain: NotifyReward events → gauge addresses ───────────────────────

def estimate_start_block(w3: Web3) -> int:
    """Estimate the Base block closest to START_TS (Jan 1 2025)."""
    latest  = w3.eth.get_block("latest")
    elapsed = latest.timestamp - START_TS
    start   = max(1, latest.number - int(elapsed * 0.5))  # ~2 s block time
    print(f"  Estimated start block for {START_DT.date()}: {start:,}")
    return start


def fetch_notify_reward_logs(w3: Web3, start_block: int) -> list:
    """
    Fetch all NotifyReward events from all CLGauge contracts since start_block.
    Uses topic-only filter (no address filter) so we catch every gauge at once.
    Paginates in LOG_CHUNK-block windows.
    """
    end_block = w3.eth.block_number
    all_logs  = []
    chunks    = list(range(start_block, end_block + 1, LOG_CHUNK))

    for i, from_b in enumerate(chunks):
        to_b = min(from_b + LOG_CHUNK - 1, end_block)
        try:
            logs = w3.eth.get_logs({
                "topics":    [NOTIFY_REWARD_TOPIC],
                "fromBlock": from_b,
                "toBlock":   to_b,
            })
            all_logs.extend(logs)
        except Exception as e:
            print(f"    eth_getLogs {from_b}-{to_b}: {e}")
        if i % 500 == 0 and i > 0:
            print(f"    {i}/{len(chunks)} chunks, {len(all_logs)} events...")

    print(f"  Fetched {len(all_logs)} NotifyReward events")
    return all_logs


def decode_notify_reward_logs(w3: Web3, logs: list) -> list:
    """
    Decode NotifyReward logs → {gauge, amount_raw, week_key}.
    Timestamps fetched in bulk for unique block numbers.
    """
    if not logs:
        return []

    unique_blocks = {log.blockNumber for log in logs}
    print(f"  Fetching timestamps for {len(unique_blocks)} unique blocks...")
    ts_map = {}
    for bn in unique_blocks:
        try:
            ts_map[bn] = w3.eth.get_block(bn).timestamp
        except Exception:
            ts_map[bn] = 0

    decoded = []
    for log in logs:
        amount_raw = int(log["data"].hex(), 16) if log.get("data") else 0
        decoded.append({
            "gauge":      log["address"].lower(),
            "amount_raw": amount_raw,
            "week_key":   _week_key(ts_map.get(log["blockNumber"], 0)),
        })
    return decoded


# ── 2. On-chain: gauge → pool address ────────────────────────────────────────

def get_pool_addresses(w3: Web3, gauge_addrs: list) -> dict:
    """
    Call gauge.pool() for each gauge. Returns {gauge_addr: pool_addr}.
    Skips gauges where the call fails (non-CL gauges, etc.).
    """
    mapping = {}
    for gauge in gauge_addrs:
        try:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(gauge),
                abi=GAUGE_POOL_ABI,
            )
            pool = contract.functions.pool().call()
            if pool.lower() != NULL_ADDR:
                mapping[gauge.lower()] = pool.lower()
        except Exception as e:
            pass  # v1 gauges, non-CL gauges — silently skip
    print(f"  Resolved {len(mapping)} gauge→pool mappings")
    return mapping


# ── 3. The Graph: pool day data for incentivised pools only ──────────────────

def _graph_post(url: str, query: str, variables: dict) -> dict:
    r = requests.post(
        url, json={"query": query, "variables": variables},
        timeout=GRAPH_TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    if "errors" in body:
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]


def fetch_pool_day_data(api_key: str, pool_addrs: list) -> list:
    """
    Fetch poolDayDatas for the given pool addresses only (batched by POOL_BATCH).
    Skips dead days (volumeUSD = 0). Uses skip-based pagination within each batch
    — safe because each batch covers at most POOL_BATCH × 500 days ≈ 25 000 rows.
    """
    url = (f"https://gateway.thegraph.com/api/{api_key}"
           f"/subgraphs/id/{SUBGRAPH_ID}")
    query = """
    query($pools: [String!]!, $startTs: Int!, $skip: Int!) {
      poolDayDatas(
        first: $first
        skip: $skip
        orderBy: date
        orderDirection: asc
        where: { pool_in: $pools, date_gte: $startTs, volumeUSD_gt: "0.01" }
      ) {
        id date volumeUSD feesUSD tvlUSD
        pool { id feeTier token0 { symbol } token1 { symbol } }
      }
    }
    """.replace("$first", str(GRAPH_PAGE))

    all_rows = []
    batches  = [pool_addrs[i:i+POOL_BATCH] for i in range(0, len(pool_addrs), POOL_BATCH)]
    print(f"  Querying {len(pool_addrs)} pools in {len(batches)} batches of {POOL_BATCH}...")

    for bi, batch in enumerate(batches):
        skip = 0
        while True:
            data = _graph_post(url, query, {
                "pools": batch, "startTs": START_TS, "skip": skip
            })
            page = data.get("poolDayDatas", [])
            all_rows.extend(page)
            if len(page) < GRAPH_PAGE:
                break
            skip += GRAPH_PAGE
            time.sleep(0.05)
        if (bi + 1) % 10 == 0:
            print(f"    batch {bi+1}/{len(batches)}, {len(all_rows)} rows so far...")
        time.sleep(0.05)

    print(f"  Subgraph: fetched {len(all_rows)} pool-day rows")
    return all_rows


def aggregate_to_weeks(day_rows: list) -> dict:
    """
    Aggregate day-level rows to (pool_address, iso_week) buckets.
    Returns: {(pool_addr, week_key): {fees_usd, volume_usd, tvl_usd, fee_tier, pair_symbols}}
    """
    buckets = defaultdict(lambda: dict(fees_usd=0.0, volume_usd=0.0,
                                       tvl_sum=0.0, n_days=0,
                                       fee_tier=0, pair_symbols=""))
    for d in day_rows:
        pool = d["pool"]["id"].lower()
        week = _week_key(int(d["date"]))
        b    = buckets[(pool, week)]
        vol  = _f(d.get("volumeUSD"))
        fees = _f(d.get("feesUSD"))

        # feesUSD absent in some subgraph builds — derive from volume
        if fees == 0 and vol > 0:
            tier = int(d["pool"].get("feeTier") or 0)
            fees = vol * tier / 1_000_000

        b["fees_usd"]    += fees
        b["volume_usd"]  += vol
        b["tvl_sum"]     += _f(d.get("tvlUSD"))
        b["n_days"]      += 1
        b["fee_tier"]     = int(d["pool"].get("feeTier") or 0)
        sym0 = (d["pool"].get("token0") or {}).get("symbol", "?")
        sym1 = (d["pool"].get("token1") or {}).get("symbol", "?")
        b["pair_symbols"] = f"{sym0}/{sym1}"
    return buckets


def aggregate_emissions(decoded_logs: list, gauge_to_pool: dict) -> dict:
    """Sum AERO emissions per (pool, week). Returns {(pool_addr, week_key): aero_float}."""
    sums = defaultdict(float)
    for ev in decoded_logs:
        pool = gauge_to_pool.get(ev["gauge"])
        if pool:
            sums[(pool, ev["week_key"])] += ev["amount_raw"] / 1e18
    return sums


# ── 4. AERO price (CoinGecko public API) ─────────────────────────────────────

def fetch_aero_prices() -> dict:
    """Fetch daily AERO/USD prices from CoinGecko. Returns {date_str: price}."""
    url = ("https://api.coingecko.com/api/v3/coins/aerodrome-finance"
           "/market_chart?vs_currency=usd&days=365&interval=daily")
    r = requests.get(url, timeout=30, headers={"Accept": "application/json"})
    r.raise_for_status()
    prices = {}
    for ts_ms, price in r.json().get("prices", []):
        day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        prices[day] = price
    print(f"  CoinGecko: fetched {len(prices)} daily AERO prices")
    return prices


def aero_price_for_week(week_key: str, daily_prices: dict) -> float:
    """Average AERO price over the 7 days of the given ISO week."""
    year, wk = week_key.split("-W")
    monday = datetime.strptime(f"{year} {wk} 1", "%G %V %u").replace(tzinfo=timezone.utc)
    prices = []
    for i in range(7):
        day = (monday + timedelta(days=i)).strftime("%Y-%m-%d")
        if day in daily_prices:
            prices.append(daily_prices[day])
    return sum(prices) / len(prices) if prices else 0.0


# ── 5. Merge, label, write ────────────────────────────────────────────────────

def build_and_write(pool_weeks: dict, emissions: dict,
                    daily_prices: dict, gauge_to_pool: dict):
    rows = []
    for (pool, week), pw in pool_weeks.items():
        fees       = round(pw["fees_usd"], 4)
        volume     = round(pw["volume_usd"], 4)
        tvl        = round(pw["tvl_sum"] / max(pw["n_days"], 1), 2)
        fee_tier   = pw["fee_tier"]
        fee_tier_bps = fee_tier / 100.0  # millionths → bps

        emis_aero  = emissions.get((pool, week), 0.0)
        aero_price = aero_price_for_week(week, daily_prices)
        emis_usd   = round(emis_aero * aero_price, 4)

        if fees == 0 and emis_usd == 0:
            continue
        # Drop rows where we have AERO emissions but no price — label would be wrong
        if emis_aero > 0 and aero_price == 0.0:
            continue

        rows.append({
            "pool":             pool,
            "pair_symbols":     pw["pair_symbols"],
            "epoch_week":       week,
            "fee_tier_bps":     round(fee_tier_bps, 4),
            "fees_usd":         fees,
            "volume_usd":       volume,
            "tvl_usd":          tvl,
            "emissions_aero":   round(emis_aero, 6),
            "aero_price":       round(aero_price, 6),
            "emissions_usd":    emis_usd,
            "fees_per_emission": round(fees / emis_usd, 4) if emis_usd > 0 else "",
            "profitable":       fees > emis_usd,
        })

    if not rows:
        raise SystemExit("No rows produced — check subgraph connectivity and RPC logs.")

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    n_prof = sum(1 for r in rows if r["profitable"])
    rule   = PCFG["label_rule"]
    print(f"\nWrote {len(rows)} pool-epoch rows to {OUT_CSV}")
    print(f"Label balance: {n_prof} profitable / {len(rows)-n_prof} not "
          f"({n_prof/len(rows)*100:.1f}%) — rule: {rule}")


# ── manual-export fallback ────────────────────────────────────────────────────

def _load_raw_csv(path: Path) -> None:
    """Read a manually-exported CSV and re-normalise it into OUT_CSV."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        raw = list(csv.DictReader(f))
    print(f"Loaded {len(raw)} rows from local {path.name}")
    out = []
    for r in raw:
        fees = _f(r.get("fees_usd"))
        emis = _f(r.get("emissions_usd"))
        if fees == 0 and emis == 0:
            continue
        fee_rate_raw = _f(r.get("fee_rate_raw"))
        fee_tier_bps = fee_rate_raw / 100.0 if fee_rate_raw else 0
        out.append({
            "pool":             r.get("pool", ""),
            "pair_symbols":     r.get("pair_symbols", ""),
            "epoch_week":       r.get("epoch_week", ""),
            "fee_tier_bps":     round(fee_tier_bps, 4),
            "fees_usd":         round(fees, 4),
            "volume_usd":       round(_f(r.get("volume_usd")), 4),
            "tvl_usd":          "",
            "emissions_aero":   round(_f(r.get("emissions_aero")), 6),
            "aero_price":       "",
            "emissions_usd":    round(emis, 4),
            "fees_per_emission": round(fees / emis, 4) if emis > 0 else "",
            "profitable":       fees > emis,
        })
    if not out:
        raise SystemExit("No usable rows in raw CSV.")
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    n_prof = sum(1 for r in out if r["profitable"])
    print(f"Wrote {len(out)} rows. Balance: {n_prof}/{len(out)} profitable.")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    if RAW_CSV.exists():
        _load_raw_csv(RAW_CSV)
        return

    graph_key = os.environ.get("THEGRAPH_API_KEY")
    rpc_url   = os.environ.get("BASE_RPC_URL")

    if not graph_key:
        raise SystemExit("Set THEGRAPH_API_KEY in .env (free at thegraph.com/studio/apikeys).")
    if not rpc_url:
        raise SystemExit("Set BASE_RPC_URL in .env.")

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise SystemExit(f"Cannot connect to Base RPC: {rpc_url}")

    # Step 1: fetch all NotifyReward events → unique gauge addresses
    print("── Step 1: fetch NotifyReward events (all gauges) ──")
    start_block = estimate_start_block(w3)
    raw_logs    = fetch_notify_reward_logs(w3, start_block)
    decoded     = decode_notify_reward_logs(w3, raw_logs)
    unique_gauges = list({ev["gauge"] for ev in decoded})
    print(f"  {len(unique_gauges)} unique gauges emitted rewards since {START_DT.date()}")

    # Step 2: resolve gauge → pool address (CLGauge.pool() call)
    print("\n── Step 2: resolve gauge → pool addresses ──")
    gauge_to_pool = get_pool_addresses(w3, unique_gauges)
    pool_to_gauge = {v: k for k, v in gauge_to_pool.items()}
    pool_addrs    = list(gauge_to_pool.values())

    # Step 3: fetch pool day data from subgraph (only incentivised pools)
    print(f"\n── Step 3: fetch pool day data from subgraph ──")
    day_rows   = fetch_pool_day_data(graph_key, pool_addrs)
    pool_weeks = aggregate_to_weeks(day_rows)
    emissions  = aggregate_emissions(decoded, gauge_to_pool)
    print(f"  {len(pool_weeks)} pool-week buckets, {len(emissions)} emission events")

    # Step 4: AERO prices
    print("\n── Step 4: fetch AERO prices ──")
    aero_prices = fetch_aero_prices()

    # Step 5: merge, label, write
    print("\n── Step 5: merge, label, write ──")
    build_and_write(pool_weeks, emissions, aero_prices, gauge_to_pool)


if __name__ == "__main__":
    main()
