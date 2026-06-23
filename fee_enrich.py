"""
fee_enrich.py — add fee tier + fee estimate to filtered candidates.

DexScreener does not expose fees or fee tier, so we read them on-chain. Three
pool families are handled (verified against live Base pools Jun 2026):

  * Algebra Integral (Hydrex CLAMM, and Aerodrome-style dynamic-fee pools):
    globalState() returns (price, tick, lastFee, pluginConfig, communityFee,
    unlocked). lastFee is the CURRENT dynamic fee in millionths (e.g. 500 = 0.05%).
  * Uniswap v3 / static-fee pools: fee() returns a uint24 in millionths.
  * Uniswap v2: detected via getReserves(); fixed 0.3% fee assumed.
  * Uniswap v4: DexScreener returns the pool ID as a 66-char bytes32 hex string
    rather than a 42-char address. Fee is read from StateView.getSlot0(bytes32)
    which returns (sqrtPriceX96, tick, protocolFee, lpFee); lpFee is uint24 in
    millionths, same encoding as V3 fee().

We try globalState() first, then fee(). Because Algebra fees are DYNAMIC
(lastFee is a snapshot, and pools can quote a lower fee on the first swap of a
block to game aggregators — Austin's point), the derived fee figure is a
SNAPSHOT estimate, flagged with dynamic_fee=True. For realized fees prefer the
DEX subgraph (Hydrex's own subgraph returns feesUSD directly; see
hydrex_daily_pull.py). Treat est_fees here as a ranking signal, not ground truth.

Reads candidates_daily.csv, enriches today's rows that passed the filter,
writes data/candidates_enriched.csv.
"""

import csv
import datetime as dt
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from web3 import Web3

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG = json.loads((SCRIPT_DIR / "selection_config.json").read_text())
IN_CSV = SCRIPT_DIR / "data" / "candidates_daily.csv"
OUT_CSV = SCRIPT_DIR / "data" / "candidates_enriched.csv"

# `or` (not get's default) so an env var that is present-but-empty — e.g. the
# GitHub Action injecting an unset `${{ secrets.BASE_RPC_URL }}` — still falls
# back to the public RPC instead of passing "" to Web3 (which raises MissingSchema).
RPC = os.environ.get(CONFIG["fee_enrich"]["base_rpc_url_env"]) or CONFIG["fee_enrich"]["default_base_rpc_url"]

GLOBALSTATE_ABI = {
    "inputs": [], "name": "globalState",
    "outputs": [
        {"name": "price", "type": "uint160"}, {"name": "tick", "type": "int24"},
        {"name": "lastFee", "type": "uint16"}, {"name": "pluginConfig", "type": "uint8"},
        {"name": "communityFee", "type": "uint16"}, {"name": "unlocked", "type": "bool"},
    ],
    "stateMutability": "view", "type": "function",
}
FEE_ABI = {
    "inputs": [], "name": "fee",
    "outputs": [{"name": "", "type": "uint24"}],
    "stateMutability": "view", "type": "function",
}
GETRESERVES_ABI = {
    "inputs": [], "name": "getReserves",
    "outputs": [
        {"name": "_reserve0", "type": "uint112"},
        {"name": "_reserve1", "type": "uint112"},
        {"name": "_blockTimestampLast", "type": "uint32"},
    ],
    "stateMutability": "view", "type": "function",
}
# Uniswap V4 StateView — getSlot0(bytes32 poolId) returns lpFee in millionths.
# The PoolManager itself does not expose getSlot0 externally; use the StateView
# peripheral (verified Base mainnet 0xa3c0c9b6…, Jun 2026).
GETSLOT0_V4_ABI = {
    "inputs": [{"name": "poolId", "type": "bytes32"}],
    "name": "getSlot0",
    "outputs": [
        {"name": "sqrtPriceX96", "type": "uint160"},
        {"name": "tick", "type": "int24"},
        {"name": "protocolFee", "type": "uint24"},
        {"name": "lpFee", "type": "uint24"},
    ],
    "stateMutability": "view", "type": "function",
}

V4_STATE_VIEW = Web3.to_checksum_address(
    CONFIG["fee_enrich"].get("uniswap_v4_state_view",
                             "0xa3c0c9b65bad0b08107aa264b0f3db444b867a71")
)


def read_fee_tier(w3: Web3, pool_address: str) -> tuple:
    """Return (fee_rate_decimal, dynamic_flag, method) or (None, None, 'unreadable').

    Dispatch rules:
      - 66-char hex (bytes32): Uniswap V4 pool ID → StateView.getSlot0(poolId)
      - 42-char hex: standard EVM address → try Algebra / V3 / V2 in order
      - other length: truly non-EVM address → return non_evm_address immediately
    """
    if len(pool_address) == 66:
        # Uniswap V4 — pool ID is bytes32, not an EVM contract address.
        # Read fee from the singleton StateView contract.
        try:
            state_view = w3.eth.contract(address=V4_STATE_VIEW, abi=[GETSLOT0_V4_ABI])
            pool_id_bytes = bytes.fromhex(pool_address[2:])
            lp_fee = state_view.functions.getSlot0(pool_id_bytes).call()[3]
            return lp_fee / 1_000_000, False, "v4.getSlot0"
        except Exception:
            return None, None, "unreadable"
    if len(pool_address) != 42:
        return None, None, "non_evm_address"
    addr = Web3.to_checksum_address(pool_address)
    # Algebra Integral first (Hydrex CLAMM pools)
    try:
        c = w3.eth.contract(address=addr, abi=[GLOBALSTATE_ABI])
        last_fee = c.functions.globalState().call()[2]
        return last_fee / 1_000_000, True, "globalState.lastFee"
    except Exception:
        pass
    # Uniswap v3 / static fee
    try:
        c = w3.eth.contract(address=addr, abi=[FEE_ABI])
        fee = c.functions.fee().call()
        return fee / 1_000_000, False, "fee()"
    except Exception:
        pass
    # Uniswap v2 — fixed 0.3% fee, detected via getReserves()
    try:
        c = w3.eth.contract(address=addr, abi=[GETRESERVES_ABI])
        c.functions.getReserves().call()
        return 0.003, False, "getReserves.v2"
    except Exception:
        return None, None, "unreadable"


def fetch_7d_data(pool_addrs: list, api_key: str, subgraph_id: str) -> dict:
    """
    Query a The Graph subgraph for the last 7 days of poolDayData for each
    candidate pool address. Pools not indexed by this subgraph simply won't
    appear in the result.
    Returns {pool_addr_lower: {vol_7d, fees_7d, tvl_avg, n_days}}.
    """
    if not api_key or not pool_addrs or not subgraph_id:
        return {}

    url = (f"https://gateway.thegraph.com/api/{api_key}"
           f"/subgraphs/id/{subgraph_id}")
    since_day = (int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
                 // 86400) * 86400

    query = """
    query($pools: [String!]!, $since: Int!) {
      poolDayDatas(
        first: 1000, orderBy: date, orderDirection: desc,
        where: { pool_in: $pools, date_gte: $since }
      ) {
        pool { id }
        volumeUSD feesUSD tvlUSD
      }
    }
    """

    results = {}
    batch_size = 50
    for i in range(0, len(pool_addrs), batch_size):
        batch = pool_addrs[i:i + batch_size]
        try:
            r = requests.post(url,
                json={"query": query, "variables": {"pools": batch, "since": since_day}},
                timeout=30)
            r.raise_for_status()
            days_data = r.json().get("data", {}).get("poolDayDatas", [])
            by_pool = defaultdict(list)
            for d in days_data:
                by_pool[d["pool"]["id"].lower()].append(d)
            for addr, days in by_pool.items():
                results[addr] = {
                    "vol_7d":  sum(float(d["volumeUSD"]) for d in days),
                    "fees_7d": sum(float(d["feesUSD"])   for d in days),
                    "tvl_avg": sum(float(d["tvlUSD"])    for d in days) / len(days),
                    "n_days":  len(days),
                }
        except Exception as e:
            print(f"  7d subgraph batch error: {e}")
        time.sleep(0.05)

    print(f"  7d subgraph data: {len(results)}/{len(pool_addrs)} pools found")
    return results


def fetch_v2_7d_data(pool_addrs: list, api_key: str, subgraph_id: str) -> dict:
    """
    Query a Uniswap V2 subgraph for the last 7 days of pairDayData.
    V2 schema differs from V3: pairAddress_in filter, reserveUSD for TVL,
    no feesUSD (computed as dailyVolumeUSD * 0.003).
    Returns {pool_addr_lower: {vol_7d, fees_7d, tvl_avg, n_days}}.
    """
    if not api_key or not pool_addrs or not subgraph_id:
        return {}

    url = (f"https://gateway.thegraph.com/api/{api_key}"
           f"/subgraphs/id/{subgraph_id}")
    since_day = (int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
                 // 86400) * 86400

    query = """
    query($pairs: [String!]!, $since: Int!) {
      pairDayDatas(
        first: 1000, orderBy: date, orderDirection: desc,
        where: { pairAddress_in: $pairs, date_gte: $since }
      ) {
        pairAddress
        dailyVolumeUSD
        reserveUSD
      }
    }
    """

    V2_FEE = 0.003
    results = {}
    batch_size = 50
    for i in range(0, len(pool_addrs), batch_size):
        batch = pool_addrs[i:i + batch_size]
        try:
            r = requests.post(url,
                json={"query": query, "variables": {"pairs": batch, "since": since_day}},
                timeout=30)
            r.raise_for_status()
            days_data = r.json().get("data", {}).get("pairDayDatas", [])
            by_pair = defaultdict(list)
            for d in days_data:
                by_pair[d["pairAddress"].lower()].append(d)
            for addr, days in by_pair.items():
                vol_7d = sum(float(d["dailyVolumeUSD"]) for d in days)
                results[addr] = {
                    "vol_7d":  vol_7d,
                    "fees_7d": vol_7d * V2_FEE,
                    "tvl_avg": sum(float(d["reserveUSD"]) for d in days) / len(days),
                    "n_days":  len(days),
                }
        except Exception as e:
            print(f"  7d V2 subgraph batch error: {e}")
        time.sleep(0.05)

    print(f"  7d V2 subgraph data: {len(results)}/{len(pool_addrs)} pools found")
    return results


def main():
    if not IN_CSV.exists():
        raise SystemExit(f"{IN_CSV} not found — run candidate_pull.py first")

    w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 20}))
    print(f"Connected to Base RPC: {RPC} (chain_id={w3.eth.chain_id})")

    with open(IN_CSV, newline="") as f:
        rows = list(csv.DictReader(f))

    today = dt.date.today().isoformat()
    cutoff = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    FILTER_CFG = CONFIG["candidate_filters"]
    MIN_LIQ = float(FILTER_CFG.get("min_liquidity_usd", 0))
    MIN_MC  = float(FILTER_CFG.get("min_market_cap_usd", 0))

    # Use rolling 7-day averages from the CSV for filtering and as a scoring fallback.
    # This prevents a pool from being silently dropped because DexScreener had a noisy
    # snapshot on one particular day.
    recent = [r for r in rows if r.get("date", "") >= cutoff]
    by_addr = defaultdict(list)
    for r in recent:
        addr = r.get("pair_address", "").lower()
        if addr:
            by_addr[addr].append(r)

    unique = []
    for addr, addr_rows in by_addr.items():
        def _fv(r, k):
            try: return float(r[k])
            except (KeyError, TypeError, ValueError): return 0.0

        liq_vals = [_fv(r, "liquidity_usd") for r in addr_rows if _fv(r, "liquidity_usd") > 0]
        vol_vals  = [_fv(r, "vol_24h")       for r in addr_rows if _fv(r, "vol_24h")       > 0]
        mc_vals   = [_fv(r, "market_cap")    for r in addr_rows if _fv(r, "market_cap")    > 0]

        avg_liq = sum(liq_vals) / len(liq_vals) if liq_vals else 0
        avg_vol = sum(vol_vals)  / len(vol_vals)  if vol_vals  else 0
        avg_mc  = sum(mc_vals)   / len(mc_vals)   if mc_vals   else 0
        days_seen = len(set(r["date"] for r in addr_rows))

        # Filter on rolling averages, not today's single snapshot
        if avg_liq < MIN_LIQ or avg_mc < MIN_MC:
            continue

        # Representative row: today's if available, else most recent
        today_rows = [r for r in addr_rows if r["date"] == today]
        rep = dict(today_rows[0] if today_rows
                   else sorted(addr_rows, key=lambda x: x["date"])[-1])

        rep["csv_avg_liquidity_7d"] = round(avg_liq, 2)
        rep["csv_avg_vol_7d"]       = round(avg_vol, 2)
        rep["csv_days_seen"]        = days_seen

        # LP exit signal: current liquidity has dropped significantly below rolling avg.
        # Flags genuine LP withdrawal vs normal daily noise.
        exit_threshold = float(FILTER_CFG.get("lp_exit_liquidity_drop", 0.5))
        current_liq = float(rep.get("liquidity_usd") or 0)
        rep["lp_exit_signal"] = (
            avg_liq > 0 and current_liq > 0
            and current_liq < avg_liq * exit_threshold
        )
        unique.append(rep)

    print(f"Enriching {len(unique)} unique pools (7d rolling filter) for {today}")

    out = []
    for r in unique:
        rate, dynamic, method = read_fee_tier(w3, r["pair_address"])
        vol_24h = float(r["vol_24h"] or 0)
        liq = float(r["liquidity_usd"] or 0)
        lp_type = {
            "globalState.lastFee": "CLAMM",
            "fee()": "CL/V3",
            "getReserves.v2": "V2",
            "v4.getSlot0": "V4",
        }.get(method, "")
        if rate is not None:
            fees_24h = vol_24h * rate
            r["fee_tier_bps"] = round(rate * 10000, 4)   # e.g. 0.0005 -> 5 bps
            r["fee_read_method"] = method
            r["lp_type"] = lp_type
            r["dynamic_fee"] = dynamic
            r["est_fees_24h_usd"] = round(fees_24h, 2)
            r["est_fees_per_tvl_24h"] = round(fees_24h / liq, 6) if liq > 0 else 0
        else:
            r["fee_tier_bps"] = ""
            r["fee_read_method"] = method
            r["lp_type"] = lp_type
            r["dynamic_fee"] = ""
            r["est_fees_24h_usd"] = ""
            r["est_fees_per_tvl_24h"] = ""
        out.append(r)
        print(f"  {r['pair']:<20} tier={r['fee_tier_bps']} bps  "
              f"est_fees_24h=${r['est_fees_24h_usd']}  ({method})")

    if not out:
        print("Nothing to write.")
        return

    # Enrich with 7-day rolling data — Aerodrome first, then Uniswap v3/v2/v4
    graph_key = os.environ.get("THEGRAPH_API_KEY", "")
    subgraph_cfg = CONFIG.get("subgraphs", {})
    aero_id = subgraph_cfg.get("aerodrome_base", "")
    uni_id  = subgraph_cfg.get("uniswap_v3_base", "")

    addrs = [r["pair_address"].lower() for r in out if r.get("pair_address")]

    print("\nFetching 7-day rolling data from Aerodrome subgraph...")
    aero_7d = fetch_7d_data(addrs, graph_key, aero_id)

    remaining = [a for a in addrs if a not in aero_7d]
    uni_7d = {}
    if uni_id and remaining:
        print(f"Fetching 7-day rolling data from Uniswap v3 subgraph ({len(remaining)} remaining pools)...")
        uni_7d = fetch_7d_data(remaining, graph_key, uni_id)

    remaining2 = [a for a in remaining if a not in uni_7d]
    v2_id = subgraph_cfg.get("uniswap_v2_base", "")
    uni_v2_7d = {}
    if v2_id and remaining2:
        print(f"Fetching 7-day rolling data from Uniswap v2 subgraph ({len(remaining2)} remaining pools)...")
        uni_v2_7d = fetch_v2_7d_data(remaining2, graph_key, v2_id)

    # V4 pool IDs are bytes32 (66-char), so they are never found in v2/v3 subgraphs.
    # Query the V4 subgraph for any remaining 66-char pool IDs.
    remaining3 = [a for a in remaining2 if a not in uni_v2_7d]
    v4_id = subgraph_cfg.get("uniswap_v4_base", "")
    uni_v4_7d = {}
    v4_pool_ids = [a for a in remaining3 if len(a) == 66 or len(a) == 64]
    if v4_id and v4_pool_ids:
        print(f"Fetching 7-day rolling data from Uniswap v4 subgraph ({len(v4_pool_ids)} V4 pools)...")
        uni_v4_7d = fetch_7d_data(v4_pool_ids, graph_key, v4_id)

    for r in out:
        addr = r.get("pair_address", "").lower()
        if addr in aero_7d:
            d = aero_7d[addr]
            r["vol_7d_usd"]       = round(d["vol_7d"],  2)
            r["fees_7d_usd"]      = round(d["fees_7d"], 2)
            r["tvl_avg_7d_usd"]   = round(d["tvl_avg"], 2)
            r["data_days"]        = d["n_days"]
            r["seven_day_source"] = "aerodrome"
        elif addr in uni_7d:
            d = uni_7d[addr]
            r["vol_7d_usd"]       = round(d["vol_7d"],  2)
            r["fees_7d_usd"]      = round(d["fees_7d"], 2)
            r["tvl_avg_7d_usd"]   = round(d["tvl_avg"], 2)
            r["data_days"]        = d["n_days"]
            r["seven_day_source"] = "uniswap"
        elif addr in uni_v2_7d:
            d = uni_v2_7d[addr]
            r["vol_7d_usd"]       = round(d["vol_7d"],  2)
            r["fees_7d_usd"]      = round(d["fees_7d"], 2)
            r["tvl_avg_7d_usd"]   = round(d["tvl_avg"], 2)
            r["data_days"]        = d["n_days"]
            r["seven_day_source"] = "uniswap-v2"
        elif addr in uni_v4_7d:
            d = uni_v4_7d[addr]
            r["vol_7d_usd"]       = round(d["vol_7d"],  2)
            r["fees_7d_usd"]      = round(d["fees_7d"], 2)
            r["tvl_avg_7d_usd"]   = round(d["tvl_avg"], 2)
            r["data_days"]        = d["n_days"]
            r["seven_day_source"] = "uniswap-v4"
        else:
            r["vol_7d_usd"] = r["fees_7d_usd"] = r["tvl_avg_7d_usd"] = r["data_days"] = ""
            r["seven_day_source"] = ""

    fieldnames = list(out[0].keys())
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out)
    print(f"\nWrote {len(out)} enriched rows to {OUT_CSV}")


if __name__ == "__main__":
    main()
