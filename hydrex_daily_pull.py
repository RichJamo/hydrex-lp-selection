"""Daily Hydrex pool tracker — pulls TVL, Volume, Fees from analytics subgraph
for the 5 pools that are in the Aero vs Hydrex comparison. Computes V/T, F/T, F/V.

First run: backfills from Jan 1 to today.
Subsequent runs: idempotently appends only new days.

Output: data/hydrex_pools_daily.csv

Designed to be cron-able daily:
    0 9 * * *  cd /path/to/hydrex-lp-dashboard && python3 hydrex_daily_pull.py
"""

import csv
import datetime as dt
import sys
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
OUT_CSV = SCRIPT_DIR / "data" / "hydrex_pools_daily.csv"

SUBGRAPH = "https://analytics-subgraph.hydrex.fi/subgraphs/name/hydrex/v3-base/graphql"

# The 5 pools we're tracking against Aero
POOLS = [
    ("WETH/cbBTC", "0x3f9b863ef4b295d6ba370215bcca3785fcc44f44"),
    ("WETH/USDC",  "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29"),
    ("USDC/cbBTC", "0x0ba69825c4c033e72309f6ac0bde0023b15cc97c"),
    ("WETH/EURC",  "0xb20f018dde5a6fe7f93c31da05a5da9efbc52772"),
    ("WETH/cbXRP", "0xee58348059c9ad6ac345be79c399da0c200627ed"),
]

START_DATE = dt.date(2026, 1, 1)  # backfill starts here on first run

FIELDS = [
    "date", "pair", "pool_address",
    "tvl_usd", "volume_usd", "fees_usd",
    "vol_per_tvl_pct", "fees_per_tvl_pct", "fees_per_vol_pct",
]


def query_pool_days(pool_addr: str, start_ts: int, end_ts: int) -> list[dict]:
    """Pull poolDayDatas for a pool over a date range (in seconds since epoch)."""
    all_days = []
    # Subgraph returns max 1000 rows per request; paginate via date_gt cursor
    cursor = start_ts
    while True:
        q = """
        {
          poolDayDatas(
            where: { pool: "%s", date_gte: %d, date_lt: %d }
            orderBy: date, orderDirection: asc,
            first: 1000
          ) { date volumeUSD feesUSD tvlUSD }
        }
        """ % (pool_addr, cursor, end_ts)
        r = requests.post(SUBGRAPH, json={"query": q}, timeout=20)
        r.raise_for_status()
        days = r.json().get("data", {}).get("poolDayDatas", []) or []
        if not days:
            break
        all_days.extend(days)
        if len(days) < 1000:
            break
        cursor = int(days[-1]["date"]) + 1
        time.sleep(0.3)
    return all_days


def existing_keys(csv_path: Path) -> set:
    """Read CSV and return set of (date, pool_address) already recorded."""
    if not csv_path.exists():
        return set()
    seen = set()
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            seen.add((row["date"], (row.get("pool_address") or "").lower()))
    return seen


def main():
    today = dt.date.today()
    seen = existing_keys(OUT_CSV)
    print(f"Existing CSV has {len(seen)} (date, pool) keys")

    # Determine starting date — earliest missing date
    if seen:
        # Resume from latest date - 1 day to refresh today's data
        latest = max(seen, key=lambda x: x[0])[0]
        resume_date = dt.date.fromisoformat(latest)  # re-fetch from latest
        start_ts = int(dt.datetime.combine(resume_date, dt.time(), tzinfo=dt.timezone.utc).timestamp())
        print(f"Resuming from latest recorded date: {resume_date}")
    else:
        start_ts = int(dt.datetime.combine(START_DATE, dt.time(), tzinfo=dt.timezone.utc).timestamp())
        print(f"First run — backfilling from {START_DATE}")

    end_ts = int(dt.datetime.combine(today + dt.timedelta(days=1), dt.time(), tzinfo=dt.timezone.utc).timestamp())

    # Pull each pool
    new_rows = []
    for pair, pool in POOLS:
        print(f"\n[{pair}] pool={pool}")
        days = query_pool_days(pool, start_ts, end_ts)
        print(f"  fetched {len(days)} days from subgraph")
        for d in days:
            date_str = dt.datetime.fromtimestamp(int(d["date"]), tz=dt.timezone.utc).strftime("%Y-%m-%d")
            key = (date_str, pool.lower())
            if key in seen:
                continue
            tvl = float(d["tvlUSD"] or 0)
            vol = float(d["volumeUSD"] or 0)
            fees = float(d["feesUSD"] or 0)
            row = {
                "date": date_str,
                "pair": pair,
                "pool_address": pool,
                "tvl_usd": round(tvl, 2),
                "volume_usd": round(vol, 2),
                "fees_usd": round(fees, 4),
                "vol_per_tvl_pct": round((vol / tvl) * 100, 4) if tvl > 0 else 0,
                "fees_per_tvl_pct": round((fees / tvl) * 100, 6) if tvl > 0 else 0,
                "fees_per_vol_pct": round((fees / vol) * 100, 6) if vol > 0 else 0,
            }
            new_rows.append(row)
        time.sleep(0.3)

    # Write — sort by date then pair so the CSV stays clean
    print(f"\n{len(new_rows)} new rows to write")
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    # Merge with existing
    existing_rows = []
    if OUT_CSV.exists():
        with open(OUT_CSV, newline="") as f:
            for row in csv.DictReader(f):
                key = (row["date"], (row.get("pool_address") or "").lower())
                # Drop any existing rows that we're re-writing (the latest day)
                if key not in {(r["date"], r["pool_address"].lower()) for r in new_rows}:
                    existing_rows.append(row)

    all_rows = existing_rows + new_rows
    all_rows.sort(key=lambda r: (r["date"], r["pair"]))

    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in all_rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})

    print(f"\nWrote {len(all_rows)} total rows to {OUT_CSV}")

    # Quick summary
    if all_rows:
        by_pair = {}
        for r in all_rows:
            by_pair.setdefault(r["pair"], 0)
            by_pair[r["pair"]] += 1
        print("\nRow counts by pair:")
        for pair, n in by_pair.items():
            print(f"  {pair}: {n}")


if __name__ == "__main__":
    main()
