"""Detect dynamic-fee parameter changes on Hydrex Algebra Integral plugins.

For each tracked pool, queries the plugin contract for historical fee-config
events, decodes the new parameters, and saves to data/hydrex_param_changes.csv.

Idempotent — re-running only appends new events.

Uses Tenderly Gateway as RPC because public Base RPCs limit eth_getLogs to
10K-block ranges (and we need to scan ~6M blocks for full history).
"""

import csv
import datetime as dt
import sys
import time
from pathlib import Path

import requests
from eth_utils import keccak

SCRIPT_DIR = Path(__file__).resolve().parent
OUT_CSV = SCRIPT_DIR / "data" / "hydrex_param_changes.csv"

# Tenderly Gateway supports unlimited-range eth_getLogs
RPC = "https://base.gateway.tenderly.co"
# Fallback RPCs (10K-block limit each, only used for current state and tx receipts)
FALLBACK_RPCS = ["https://base-rpc.publicnode.com", "https://mainnet.base.org"]

POOLS = [
    ("WETH/cbBTC", "0x3f9b863ef4b295d6ba370215bcca3785fcc44f44"),
    ("WETH/USDC",  "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29"),
    ("USDC/cbBTC", "0x0ba69825c4c033e72309f6ac0bde0023b15cc97c"),
    ("WETH/EURC",  "0xb20f018dde5a6fe7f93c31da05a5da9efbc52772"),
    ("WETH/cbXRP", "0xee58348059c9ad6ac345be79c399da0c200627ed"),
]

# Backfill back to ~Jan 1 (about 6.2M blocks). Subsequent runs only scan recent.
START_BLOCK_FULL = 40_000_000
LOOKBACK_BLOCKS_INCREMENTAL = 200_000  # ~5 days, used after first run

FIELDS = [
    "timestamp", "date", "block", "tx_hash",
    "pair", "pool_address", "plugin_address",
    "baseFee", "alpha1", "alpha2", "beta1", "beta2", "gamma1", "gamma2",
    "max_fee_pips",
]


def sel(sig): return "0x" + keccak(text=sig).hex()[:8]


def rpc_call(method, params, url=RPC, retries=4):
    for _ in range(retries):
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "method": method,
                                          "params": params, "id": 1}, timeout=30).json()
            if "result" in r:
                return r["result"]
        except Exception:
            pass
        time.sleep(0.5)
    # Try fallbacks
    for fb in FALLBACK_RPCS:
        try:
            r = requests.post(fb, json={"jsonrpc": "2.0", "method": method,
                                          "params": params, "id": 1}, timeout=20).json()
            if "result" in r:
                return r["result"]
        except Exception:
            pass
    return None


def get_plugin(pool_addr: str) -> str | None:
    r = rpc_call("eth_call", [{"to": pool_addr, "data": sel("plugin()")}, "latest"])
    if r and r != "0x":
        return "0x" + r[-40:]
    return None


def chunked_logs(plugin_addr: str, from_block: int, to_block: int) -> list[dict]:
    """Pull logs in 500K-block chunks via Tenderly (supports large ranges)."""
    chunk = 500_000
    all_logs = []
    cur = from_block
    while cur <= to_block:
        end = min(cur + chunk, to_block)
        logs = rpc_call("eth_getLogs", [{"address": plugin_addr,
                                          "fromBlock": hex(cur), "toBlock": hex(end)}])
        if logs is not None:
            all_logs.extend(logs)
        cur = end + 1
        time.sleep(0.2)
    return all_logs


def block_timestamp(block_number: int) -> int | None:
    r = rpc_call("eth_getBlockByNumber", [hex(block_number), False])
    if r and r.get("timestamp"):
        return int(r["timestamp"], 16)
    return None


def decode_fee_config(data_hex: str) -> dict:
    """Decode 224-byte ABI-encoded fee config from event data field.
    Layout: 7 × 32-byte words for (uint16, uint16, uint32, uint32, uint16, uint16, uint16)
    """
    h = data_hex[2:] if data_hex.startswith("0x") else data_hex
    if len(h) < 7 * 64:
        return {}
    words = [int(h[i * 64:(i + 1) * 64], 16) for i in range(7)]
    return {
        "alpha1": words[0],
        "alpha2": words[1],
        "beta1": words[2],
        "beta2": words[3],
        "gamma1": words[4],
        "gamma2": words[5],
        "baseFee": words[6],
    }


def existing_keys() -> set:
    if not OUT_CSV.exists():
        return set()
    keys = set()
    with open(OUT_CSV, newline="") as f:
        for row in csv.DictReader(f):
            keys.add(row.get("tx_hash"))
    return keys


def main():
    latest = rpc_call("eth_blockNumber", [])
    if not latest:
        print("Could not fetch latest block")
        sys.exit(1)
    latest = int(latest, 16)
    print(f"Current block: {latest:,}")

    seen = existing_keys()
    print(f"{len(seen)} param changes already recorded")

    # First run: scan back to START_BLOCK_FULL. Incremental: just last N blocks.
    from_block = START_BLOCK_FULL if not seen else max(latest - LOOKBACK_BLOCKS_INCREMENTAL, START_BLOCK_FULL)
    print(f"Scanning block range {from_block:,} to {latest:,}\n")

    new_rows = []
    for pair, pool in POOLS:
        plugin = get_plugin(pool)
        if not plugin:
            print(f"[{pair}] could not resolve plugin")
            continue
        print(f"[{pair}] plugin {plugin}")

        all_logs = chunked_logs(plugin, from_block, latest)
        candidates = [l for l in all_logs if l.get("data") and (len(l["data"]) - 2) // 2 == 224]
        print(f"  {len(all_logs)} logs / {len(candidates)} fee-config events")

        for log in candidates:
            tx = log.get("transactionHash")
            if tx in seen:
                continue
            blk = int(log["blockNumber"], 16)
            ts = block_timestamp(blk)
            if ts is None:
                continue
            date_str = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d")
            cfg = decode_fee_config(log["data"])
            if not cfg:
                continue
            new_rows.append({
                "timestamp": ts,
                "date": date_str,
                "block": blk,
                "tx_hash": tx,
                "pair": pair,
                "pool_address": pool,
                "plugin_address": plugin,
                **cfg,
                "max_fee_pips": cfg["baseFee"] + cfg["alpha1"] + cfg["alpha2"],
            })
            time.sleep(0.05)

    # Merge with existing
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if OUT_CSV.exists():
        with open(OUT_CSV, newline="") as f:
            existing = list(csv.DictReader(f))

    all_rows = existing + new_rows
    # Dedupe by tx_hash, then sort by block
    seen_tx = set()
    unique = []
    for r in sorted(all_rows, key=lambda r: int(r["block"])):
        if r["tx_hash"] in seen_tx:
            continue
        seen_tx.add(r["tx_hash"])
        unique.append(r)

    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in unique:
            w.writerow({k: r.get(k, "") for k in FIELDS})

    print(f"\n{len(new_rows)} new param changes, {len(unique)} total in {OUT_CSV}\n")

    if unique:
        print("All param changes (sorted by date):")
        for r in unique:
            print(f"  {r['date']}  {r['pair']:<12}  "
                  f"base={r['baseFee']:>4}  a1={r['alpha1']:>5}  a2={r['alpha2']:>6}  "
                  f"b1={r['beta1']:>5}  b2={r['beta2']:>6}  g1={r['gamma1']:>3}  g2={r['gamma2']:>5}  "
                  f"max={r['max_fee_pips']:>6} pips")


if __name__ == "__main__":
    main()
