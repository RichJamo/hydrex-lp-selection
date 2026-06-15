#!/usr/bin/env python3
"""Bootstrap pool optimizer.

For each selected pool outputs:
  1. Volume-capture sensitivity: fees at 5/10/20/30% of external volume
  2. Max safe oHYDX budget per epoch at each capture rate
  3. Recommended starting budget (10% capture = break-even)
  4. Cut rules with concrete $ thresholds for epochs 1-3
  5. Dynamic fee plugin starting params (volatility-tier matched to reference pools)

HYDX price is read from selection_config.json → bootstrap.hydx_price_usd.
Override with --hydx-price.

Usage:
  python bootstrap_optimize.py                          # top 2 from weekly_picks.csv
  python bootstrap_optimize.py --top-n 4               # all 4 ranked picks
  python bootstrap_optimize.py --pairs "VVV/cbBTC" "cbADA/cbBTC"
  python bootstrap_optimize.py --hydx-price 0.03249
"""

import argparse
import csv
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "selection_config.json"
PICKS_CSV   = SCRIPT_DIR / "data" / "weekly_picks.csv"

# Fee plugin starting params derived from hydrex_param_changes.csv reference pools.
# All values in millionths (same encoding as Algebra lastFee: 100 = 1 bp).
FEE_PROFILES = {
    "stable_ratio": {
        # Both tokens are majors (e.g. ETH/BTC price ratio).
        # Reference: WETH/cbBTC final tuned config (May 28 2026).
        "baseFee": 50, "alpha1": 200, "alpha2": 250,
        "beta1": 180, "beta2": 60000, "gamma1": 50, "gamma2": 8500,
        "ref": "WETH/cbBTC (May 2026)",
    },
    "moderate": {
        # One major + one wrapped institutional asset (cb-prefixed).
        # Reference: WETH/EURC (Mar 2026).
        "baseFee": 200, "alpha1": 300, "alpha2": 2500,
        "beta1": 360, "beta2": 60000, "gamma1": 59, "gamma2": 8500,
        "ref": "WETH/EURC (Mar 2026)",
    },
    "volatile": {
        # One long-tail / DeFi / new token.
        # Reference: WETH/cbXRP (Mar 2026), scaled down slightly.
        "baseFee": 500, "alpha1": 2000, "alpha2": 7500,
        "beta1": 360, "beta2": 60000, "gamma1": 59, "gamma2": 8500,
        "ref": "WETH/cbXRP (Mar 2026), scaled",
    },
}


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_picks(top_n, pair_filter):
    rows = []
    with open(PICKS_CSV, newline="") as f:
        for row in csv.DictReader(f):
            if pair_filter and row["pair"] not in pair_filter:
                continue
            rows.append(row)
    if not pair_filter:
        rows = rows[:top_n]
    return rows


def _vol_tier(base: str, quote: str, majors: set) -> str:
    """Classify pair into fee volatility tier by token composition."""
    b, q = base.upper(), quote.upper()
    if b in majors and q in majors:
        return "stable_ratio"
    non_major = b if b not in majors else q
    # cb-prefixed = Coinbase wrapped institutional asset → less volatile than pure DeFi
    if non_major.startswith("CB"):
        return "moderate"
    return "volatile"


def analyze(row: dict, cfg: dict) -> None:
    bc   = cfg["bootstrap"]
    hydx = bc["hydx_price_usd"]
    ohydx_unit_cost = hydx * bc["ohydx_discount"]   # USD cost of 1 oHYDX to Hydrex
    epoch_days      = bc["epoch_days"]
    capture_rates   = bc["capture_rates"]
    cut_thresholds  = {int(k): v for k, v in bc["cut_thresholds"].items()}
    router_tvl      = bc["router_tvl_threshold_usd"]
    majors          = {t.upper() for t in cfg["scoring"]["major_tokens"]}

    pair      = row["pair"]
    dex       = row.get("dex", "?")
    lp_type   = row.get("lp_type", "?")
    fee_bps   = float(row.get("fee_tier_bps") or 0)
    vol_24h   = float(row.get("vol_24h") or 0)
    liq       = float(row.get("liquidity_usd") or 0)
    est_fees  = float(row.get("est_fees_24h_usd") or 0)

    base_sym, _, quote_sym = pair.partition("/")
    tier    = _vol_tier(base_sym, quote_sym, majors)
    profile = FEE_PROFILES[tier]
    max_fee = profile["baseFee"] + profile["alpha1"] + profile["alpha2"]

    w = 65
    print(f"\n{'═' * w}")
    print(f"  {pair}  [{dex} · {lp_type}]  tier={fee_bps:.0f}bps  liq=${liq:,.0f}")
    print(f"  External vol: ${vol_24h:,.0f}/day  |  est fees/day (external): ${est_fees:,.2f}")
    print(f"{'═' * w}")

    # ── 1. Volume capture sensitivity ────────────────────────────────────────
    print(f"\n  1. Volume capture sensitivity  (epoch = {epoch_days} days)")
    print(f"  {'Capture':>8}  {'Vol/epoch':>12}  {'Fees/epoch':>12}  "
          f"{'Max oHYDX':>11}  {'Budget (USD)':>13}")
    print(f"  {'-' * (w - 2)}")

    rec_budget_usd = None
    rec_ohydx      = None

    for rate in capture_rates:
        vol_epoch  = vol_24h * epoch_days * rate
        fees_epoch = vol_epoch * (fee_bps / 10_000)
        max_ohydx  = fees_epoch / ohydx_unit_cost if ohydx_unit_cost else 0
        tag = "  ← recommended" if rate == 0.10 else ""
        if rate == 0.10:
            rec_budget_usd = fees_epoch
            rec_ohydx      = max_ohydx
        print(f"  {rate:>7.0%}  ${vol_epoch:>11,.0f}  ${fees_epoch:>11,.2f}  "
              f"{max_ohydx:>11,.0f}  ${fees_epoch:>12,.2f}{tag}")

    # ── 2. Recommended starting budget ───────────────────────────────────────
    print(f"\n  2. Recommended starting budget  (10% capture, break-even)")
    print(f"     {rec_ohydx:,.0f} oHYDX / epoch")
    print(f"     Cost to Hydrex: ${rec_budget_usd:,.2f}  "
          f"(at HYDX ${hydx:.5f} × {bc['ohydx_discount']:.0%} discount)")
    print(f"     Implies ~${vol_24h * epoch_days * 0.10:,.0f} of volume "
          f"routed through Hydrex per epoch from ${vol_24h:,.0f}/day external pool")

    # ── 3. Cut rules ─────────────────────────────────────────────────────────
    print(f"\n  3. Cut rules")
    for epoch in sorted(cut_thresholds):
        thresh    = cut_thresholds[epoch]
        min_fees  = rec_budget_usd * thresh
        min_vol   = min_fees / (fee_bps / 10_000) if fee_bps else 0
        if thresh < 1.0:
            action = f"cut → not on trajectory  (need ${min_fees:,.2f} fees, "
            action += f"~${min_vol:,.0f} vol)"
        else:
            action = f"cut unless improving  (need ${min_fees:,.2f} fees = break-even)"
        print(f"     Epoch {epoch}: fees/incentive < {thresh:.0%}  →  {action}")
    print(f"     Hard cut: TVL < ${router_tvl:,} after epoch 1  (no routing materialised)")
    print(f"     Hard cut: ratio falls epoch-over-epoch  (no growth trajectory)")

    # ── 4. Dynamic fee plugin params ─────────────────────────────────────────
    print(f"\n  4. Dynamic fee plugin params  [volatility tier: {tier}]")
    print(f"     Reference: {profile['ref']}")
    print(f"     {'Param':<10}  {'Value':>7}   note")
    print(f"     {'-' * 42}")
    notes = {
        "baseFee": f"floor  =  {profile['baseFee']/100:.1f} bps",
        "alpha1":  f"+{profile['alpha1']/100:.1f} bps at first vol threshold",
        "alpha2":  f"+{profile['alpha2']/100:.1f} bps at second vol threshold",
        "beta1":   "volatility threshold 1 (tune down to make fee more reactive)",
        "beta2":   "volatility threshold 2 (rarely needs changing from default)",
        "gamma1":  "smoothing window 1  (lower = faster fee response)",
        "gamma2":  "smoothing window 2",
    }
    for k in ["baseFee", "alpha1", "alpha2", "beta1", "beta2", "gamma1", "gamma2"]:
        print(f"     {k:<10}  {profile[k]:>7}   {notes[k]}")
    print(f"     max_fee_pips = {max_fee}  ({max_fee / 100:.1f} bps at peak volatility)")
    print(f"     → Floor undercuts incumbent ({fee_bps:.0f} bps) by "
          f"{fee_bps - profile['baseFee']/100:.1f} bps; "
          f"spikes to {max_fee/100:.1f} bps during high vol to protect LPs")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-n", type=int, default=2,
                        help="How many top picks to analyse (default: 2)")
    parser.add_argument("--pairs", nargs="+", metavar="PAIR",
                        help='Explicit pairs to analyse, e.g. "VVV/cbBTC" "cbADA/cbBTC"')
    parser.add_argument("--hydx-price", type=float,
                        help="Override HYDX price (USD) from config")
    args = parser.parse_args()

    cfg = load_config()
    if args.hydx_price:
        cfg["bootstrap"]["hydx_price_usd"] = args.hydx_price

    pair_filter = set(args.pairs) if args.pairs else None
    rows = load_picks(args.top_n, pair_filter)

    if not rows:
        print("No picks found. Run score.py first or check --pairs spelling.")
        return

    hydx = cfg["bootstrap"]["hydx_price_usd"]
    print(f"\nHydrex Bootstrap Optimizer  |  HYDX ${hydx:.5f}  |  "
          f"oHYDX cost ${hydx * cfg['bootstrap']['ohydx_discount']:.5f}/token")
    print(f"Analysing {len(rows)} pool(s) from weekly_picks.csv\n")

    for row in rows:
        analyze(row, cfg)

    print(f"\n{'═' * 65}")
    print("  Note: capture rates are model assumptions, not observed data.")
    print("  Calibrate against actual Hydrex routing share after epoch 1.")
    print(f"{'═' * 65}\n")


if __name__ == "__main__":
    main()
