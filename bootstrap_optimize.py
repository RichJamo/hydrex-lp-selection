#!/usr/bin/env python3
"""Bootstrap pool optimizer — LP-yield model.

In Hydrex ve(3,3): trading fees go to voters; LPs earn oHYDX only.

Break-even is therefore a structural test that is INDEPENDENT of TVL scale
(TVL cancels in the ratio):

    break_even_ratio = (vol/TVL per day × avg_fee_rate × 364) / LP_APR_target

If ratio ≥ 1, break-even is achievable at any TVL. If < 1, no amount of
oHYDX can fix it — the fee rate must be raised.

For each pool outputs:
  1. Structural viability across fee scenarios × LP APR targets
  2. Minimum avg fee needed to break even, and suggested plugin param adjustment
  3. oHYDX budget and expected fees at several target TVL levels
  4. Cut rules

Usage:
  python bootstrap_optimize.py                          # top 2 from weekly_picks.csv
  python bootstrap_optimize.py --top-n 4
  python bootstrap_optimize.py --pairs "VVV/cbBTC" "cbADA/cbBTC"
  python bootstrap_optimize.py --hydx-price 0.03249
"""

import argparse
import csv
import json
import math
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "selection_config.json"
PICKS_CSV   = SCRIPT_DIR / "data" / "weekly_picks.csv"

# Fee plugin starting params derived from hydrex_param_changes.csv reference pools.
# All values in millionths (100 = 1 bp).
FEE_PROFILES = {
    "stable_ratio": {
        "baseFee": 50, "alpha1": 200, "alpha2": 250,
        "beta1": 180, "beta2": 60000, "gamma1": 50, "gamma2": 8500,
        "ref": "WETH/cbBTC (May 2026)",
    },
    "moderate": {
        "baseFee": 200, "alpha1": 300, "alpha2": 2500,
        "beta1": 360, "beta2": 60000, "gamma1": 59, "gamma2": 8500,
        "ref": "WETH/EURC (Mar 2026)",
    },
    "volatile": {
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
    b, q = base.upper(), quote.upper()
    if b in majors and q in majors:
        return "stable_ratio"
    non_major = b if b not in majors else q
    if non_major.startswith("CB"):
        return "moderate"
    return "volatile"


def _avg_fee_bps(profile: dict) -> float:
    """Estimated avg fee in bps: floor + half of first volatility step."""
    return (profile["baseFee"] + profile["alpha1"] // 2) / 100


def _be_ratio(vol_tvl_daily: float, fee_bps: float, lp_apr: float) -> float:
    """Break-even ratio for a given fee rate and LP APR target."""
    return vol_tvl_daily * (fee_bps / 10_000) * 364 / lp_apr


def _min_avg_fee_bps(vol_tvl_daily: float, lp_apr: float) -> float:
    """Minimum avg fee (bps) for break-even at this LP APR target."""
    return lp_apr / (vol_tvl_daily * 364) * 10_000


def analyze(row: dict, cfg: dict) -> None:
    bc              = cfg["bootstrap"]
    hydx            = bc["hydx_price_usd"]
    ohydx_unit_cost = hydx * bc["ohydx_discount"]
    epoch_days      = bc["epoch_days"]
    lp_apr_targets  = bc["lp_apr_targets"]
    tvl_targets     = bc["tvl_targets_usd"]
    cut_thresholds  = {int(k): v for k, v in bc["cut_thresholds"].items()}
    router_tvl      = bc["router_tvl_threshold_usd"]
    majors          = {t.upper() for t in cfg["scoring"]["major_tokens"]}

    pair     = row["pair"]
    dex      = row.get("dex", "?")
    lp_type  = row.get("lp_type", "?")
    ext_bps  = float(row.get("fee_tier_bps") or 0)
    vol_24h  = float(row.get("vol_24h") or 0)
    liq      = float(row.get("liquidity_usd") or 0)

    vol_tvl_daily = vol_24h / liq if liq else 0

    base_sym, _, quote_sym = pair.partition("/")
    tier    = _vol_tier(base_sym, quote_sym, majors)
    profile = FEE_PROFILES[tier]

    floor_bps = profile["baseFee"] / 100
    avg_bps   = _avg_fee_bps(profile)
    peak_bps  = (profile["baseFee"] + profile["alpha1"] + profile["alpha2"]) / 100

    fee_scenarios = [
        (f"Floor ({floor_bps:.1f}bps)", floor_bps),
        (f"Avg (~{avg_bps:.1f}bps)",    avg_bps),
        (f"Peak ({peak_bps:.0f}bps)",   peak_bps),
        (f"Ext ({ext_bps:.0f}bps)",     ext_bps),
    ]

    w = 72
    print(f"\n{'═' * w}")
    print(f"  {pair}  [{dex} · {lp_type}]  external fee={ext_bps:.0f}bps")
    print(f"  External pool: ${vol_24h:,.0f}/day vol  ·  ${liq:,.0f} TVL  "
          f"·  vol/TVL = {vol_tvl_daily:.2f}×/day")
    print(f"{'═' * w}")

    # ── 1. Structural viability ───────────────────────────────────────────────
    print(f"\n  1. Structural viability  "
          f"(break-even ratio = vol/TVL × fee × 364 ÷ LP APR target)")
    print(f"     > 1.0 = fees cover oHYDX cost  |  < 1.0 = structural subsidy\n")

    col = 16
    hdr = "".join(f"{s[0]:>{col}}" for s in fee_scenarios)
    print(f"  {'LP APR target':>15}  {hdr}")
    print(f"  {'-' * (w - 2)}")

    for apr in lp_apr_targets:
        cells = ""
        for _, bps in fee_scenarios:
            ratio = _be_ratio(vol_tvl_daily, bps, apr)
            mark  = " ✓" if ratio >= 1 else "  "
            cells += f"  {ratio:>8.2f}×{mark:2}  "
        print(f"  {apr:>14.0%}  {cells}")

    # Annualised fee yield per scenario
    print(f"\n  Pool fee yield annualised (vol/TVL × fee × 364):")
    for label, bps in fee_scenarios:
        yield_pct = vol_tvl_daily * (bps / 10_000) * 364 * 100
        print(f"    {label:<20}  {yield_pct:>6.1f}%")

    # ── 2. Minimum avg fee + suggested param adjustment ───────────────────────
    print(f"\n  2. Minimum avg fee to break even  (and suggested plugin adjustment)")

    any_adjustment_needed = False
    suggested_alpha1 = None
    suggested_apr    = None

    for apr in lp_apr_targets:
        min_fee = _min_avg_fee_bps(vol_tvl_daily, apr)
        gap     = min_fee - avg_bps
        ok      = avg_bps >= min_fee
        status  = "✓ current avg sufficient" if ok else f"✗ gap: +{gap:.1f}bps needed"
        print(f"    LP APR {apr:.0%}  →  min avg fee = {min_fee:.1f}bps  "
              f"(current: {avg_bps:.1f}bps)  {status}")
        if not ok and not any_adjustment_needed:
            any_adjustment_needed = True
            suggested_apr    = apr
            # Raise alpha1, keep baseFee floor unchanged
            target_millionths = min_fee * 100
            new_alpha1 = max(0, math.ceil((target_millionths - profile["baseFee"]) * 2))
            suggested_alpha1 = new_alpha1

    if any_adjustment_needed:
        new_avg = (profile["baseFee"] + suggested_alpha1 // 2) / 100
        print(f"\n     Suggested fix (raise alpha1, keep floor at {floor_bps:.1f}bps):")
        print(f"       baseFee = {profile['baseFee']}  (unchanged)")
        print(f"       alpha1  = {profile['alpha1']} → {suggested_alpha1}  "
              f"(avg fee: {avg_bps:.1f}bps → {new_avg:.1f}bps)")
        print(f"       alpha2, beta, gamma unchanged")
    else:
        print(f"\n     No adjustment needed at current fee profile.")

    # Choose the working avg fee for sections 3 & 4
    # Use the adjusted avg if needed, otherwise current avg
    if any_adjustment_needed and suggested_alpha1 is not None:
        working_avg_bps = (profile["baseFee"] + suggested_alpha1 // 2) / 100
        working_label   = f"adjusted avg ~{working_avg_bps:.1f}bps"
        working_apr     = suggested_apr
    else:
        working_avg_bps = avg_bps
        working_label   = f"avg ~{avg_bps:.1f}bps"
        working_apr     = lp_apr_targets[1]  # middle scenario

    working_ratio = _be_ratio(vol_tvl_daily, working_avg_bps, working_apr)

    # ── 3. oHYDX budget at target TVL ────────────────────────────────────────
    ratio_label = "✓ profitable" if working_ratio > 1.01 else (
                  "≈ break-even" if working_ratio >= 0.999 else "✗ subsidised")
    print(f"\n  3. oHYDX budget at target TVL  "
          f"(LP APR target: {working_apr:.0%}, fee: {working_label})")
    print(f"     Break-even ratio at this config: {working_ratio:.2f}×  "
          f"({ratio_label})\n")
    print(f"  {'Target TVL':>12}  {'oHYDX/epoch':>13}  {'Cost/epoch':>11}  "
          f"{'Fees/epoch':>11}  {'Ratio':>7}")
    print(f"  {'-' * (w - 2)}")

    for tvl in tvl_targets:
        # oHYDX needed: TVL × LP_APR / (52 epochs × oHYDX unit cost)
        ohydx_needed = tvl * working_apr / (52 * ohydx_unit_cost) if ohydx_unit_cost else 0
        cost_usd     = ohydx_needed * ohydx_unit_cost
        fees_usd     = tvl * vol_tvl_daily * epoch_days * (working_avg_bps / 10_000)
        ratio        = fees_usd / cost_usd if cost_usd else 0
        mark = " ✓" if ratio >= 0.999 else "  "
        print(f"  ${tvl:>11,.0f}  {ohydx_needed:>13,.0f}  ${cost_usd:>10,.2f}  "
              f"${fees_usd:>10,.2f}  {ratio:>6.2f}×{mark}")

    print(f"\n     Note: ratio is constant across TVL levels — scale affects oHYDX")
    print(f"     quantity and absolute fees but not the fees/cost relationship.")

    # ── 4. Cut rules ─────────────────────────────────────────────────────────
    ref_tvl   = tvl_targets[1]
    ref_ohydx = ref_tvl * working_apr / (52 * ohydx_unit_cost) if ohydx_unit_cost else 0
    ref_cost  = ref_ohydx * ohydx_unit_cost

    print(f"\n  4. Cut rules  (reference: ${ref_tvl:,} TVL target, "
          f"{ref_ohydx:,.0f} oHYDX/epoch)")
    print(f"     Hard cut: Hydrex TVL < ${router_tvl:,} after epoch 1  "
          f"(no LP uptake)")
    print(f"     Hard cut: TVL not growing toward ${ref_tvl:,} target by epoch 2")
    for epoch, thresh in sorted(cut_thresholds.items()):
        min_fees = ref_cost * thresh
        print(f"     Epoch {epoch}: observed fees < ${min_fees:,.2f}  "
              f"(< {thresh:.0%} of oHYDX cost)  →  cut")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-n", type=int, default=2)
    parser.add_argument("--pairs", nargs="+", metavar="PAIR")
    parser.add_argument("--hydx-price", type=float)
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
    print(f"Model: fees to voters, oHYDX to LPs. "
          f"Break-even = vol/TVL × avg_fee × 364 ÷ LP APR target.")
    print(f"Analysing {len(rows)} pool(s)\n")

    for row in rows:
        analyze(row, cfg)

    print(f"\n{'═' * 72}")
    print("  vol/TVL ratio taken from external pool — Hydrex pool may differ")
    print("  early on. Calibrate after epoch 1 with actual on-chain data.")
    print(f"{'═' * 72}\n")


if __name__ == "__main__":
    main()
