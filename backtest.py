"""
backtest.py — Does our phase-1 score predict next-epoch profitability?

For each consecutive epoch pair (N → N+1) in data/aerodrome_proxy.csv:
  1. Score every pool using epoch N features (vol, TVL, fees → daily estimates).
  2. Look up whether that same pool is profitable in epoch N+1
     (only where emissions_usd > 0 in N+1 — otherwise 'profitable' is trivially
     true and tells us nothing about incentive efficiency).
  3. Record: score, next_profitable, TVL.

Aggregate:
  - Spearman correlation between score and next-epoch profitability.
  - Precision by score quartile — how often does the top quartile beat the base rate?
  - Both metrics reported for ALL pools and for LOW-TVL pools only
    (TVL ≤ low_tvl_threshold_usd from config, matching Hydrex bootstrap conditions).

The low_tvl_perf feature is built only from epochs strictly before N to prevent
look-ahead bias.

Run:
    python backtest.py
"""

import csv
import datetime as dt
import json
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from scipy.stats import spearmanr

SCRIPT_DIR  = Path(__file__).resolve().parent
PROXY_CSV   = SCRIPT_DIR / "data" / "aerodrome_proxy.csv"
RESULTS_CSV = SCRIPT_DIR / "data" / "backtest_results.csv"
CONFIG      = json.loads((SCRIPT_DIR / "selection_config.json").read_text())
S           = CONFIG["scoring"]

# Epochs flagged as data-quality issues (emissions not yet recorded → trivially profitable)
BAD_EPOCHS = {"2025-W01", "2026-W24"}
MIN_PAIRS  = 20   # skip epoch pairs with fewer scoreable pools


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _f(x, d=0.0):
    try: return float(x)
    except: return d


def _week_key(epoch_str):
    year, w = epoch_str.split("-W")
    return int(year), int(w)


def _next_week(epoch_str):
    year, week = epoch_str.split("-W")
    monday = dt.datetime.strptime(f"{year}-W{int(week):02d}-1", "%G-W%V-%u")
    return (monday + dt.timedelta(days=7)).strftime("%G-W%V")


def _normalize(values):
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


# ---------------------------------------------------------------------------
# low_tvl_perf lookup — built from past epochs only (no leakage)
# ---------------------------------------------------------------------------

def _build_low_tvl_lookup(past_rows):
    threshold = S.get("low_tvl_threshold_usd", 25_000)
    pool_ratios, pair_ratios = defaultdict(list), defaultdict(list)
    for r in past_rows:
        tvl  = _f(r.get("tvl_usd"))
        emis = _f(r.get("emissions_usd"))
        fees = _f(r.get("fees_usd"))
        if 0 < tvl <= threshold and emis > 0:
            ratio = min(fees / emis, 5.0)
            pool_ratios[r["pool"].lower()].append(ratio)
            tokens = frozenset(t.upper().strip()
                               for t in r.get("pair_symbols", "/").split("/"))
            pair_ratios[tokens].append(ratio)
    by_pool = {k: sum(v) / len(v) for k, v in pool_ratios.items()}
    by_pair = {k: sum(v) / len(v) for k, v in pair_ratios.items()}
    all_scores = sorted(by_pool.values())
    default = all_scores[len(all_scores) // 2] if all_scores else 2.5
    return {"by_pool": by_pool, "by_pair": by_pair, "default": default}


# ---------------------------------------------------------------------------
# scorer — mirrors score.py logic but accepts Aerodrome proxy columns
# ---------------------------------------------------------------------------

def _score_epoch(rows, ltv_lookup, age_lookup):
    """
    Score a list of proxy rows using epoch N features.
    Modifies rows in place (adds _score). Returns rows.
    """
    major = {t.upper() for t in S.get("major_tokens", [])}
    cap   = CONFIG["candidate_filters"]["max_pool_age_days_for_new_bucket"]

    for r in rows:
        vol_daily  = _f(r.get("volume_usd")) / 7
        fees_daily = _f(r.get("fees_usd"))   / 7
        tvl        = _f(r.get("tvl_usd"))

        r["_est_fees"] = fees_daily
        r["_fees_tvl"] = fees_daily / tvl if tvl > 0 else 0
        r["_vol_tvl"]  = vol_daily  / tvl if tvl > 0 else 0

        tokens  = [t.strip().upper() for t in r.get("pair_symbols", "/").split("/")][:2]
        n_major = sum(1 for t in tokens if t in major)
        r["_pair_type"] = [0.3, 0.7, 1.0][n_major]

        addr      = r["pool"].lower()
        pair_key  = frozenset(tokens)
        raw       = (ltv_lookup["by_pool"].get(addr)
                     or ltv_lookup["by_pair"].get(pair_key)
                     or ltv_lookup["default"])
        r["_low_tvl_perf"] = min(raw / 5.0, 1.0)

        age = age_lookup.get(addr, 9999)
        r["_newness"] = max(0.0, 1 - age / cap) if cap > 0 else 0
        r["_bs"]      = 0   # not in proxy data

    feat_keys = {
        "est_fees_per_day_usd": "_est_fees",
        "fees_per_tvl_ratio":   "_fees_tvl",
        "vol_tvl_24h":          "_vol_tvl",
        "pair_type_score":      "_pair_type",
        "low_tvl_perf":         "_low_tvl_perf",
        "newness_bonus":        "_newness",
        "buy_sell_balance":     "_bs",
    }
    normed = {
        name: _normalize([_f(r[key]) for r in rows])
        for name, key in feat_keys.items()
    }
    for i, r in enumerate(rows):
        r["_score"] = round(
            sum(S["weights"].get(name, 0) * normed[name][i] for name in feat_keys), 4
        )
    return rows


# ---------------------------------------------------------------------------
# quartile report
# ---------------------------------------------------------------------------

def _quartile_report(pool_results, label):
    if not pool_results:
        print(f"\n{label}: no data")
        return
    n      = len(pool_results)
    base   = sum(r["profitable_n1"] for r in pool_results) / n
    sorted_by_score = sorted(pool_results, key=lambda r: r["score"])
    q      = n // 4
    slices = [
        ("Q1 — bottom 25% (lowest score)",  sorted_by_score[:q]),
        ("Q2",                               sorted_by_score[q:2*q]),
        ("Q3",                               sorted_by_score[2*q:3*q]),
        ("Q4 — top 25% (highest score)",    sorted_by_score[3*q:]),
    ]
    scores = [r["score"] for r in pool_results]
    labels = [int(r["profitable_n1"]) for r in pool_results]
    rho, pval = spearmanr(scores, labels)

    print(f"\n{label}  (n={n}, base rate={base:.1%}, Spearman ρ={rho:.3f} p={pval:.4f})")
    print(f"  {'Quartile':<40} {'N':>5}  {'Profitable next epoch':>22}")
    for name, bucket in slices:
        if not bucket:
            continue
        prec = sum(r["profitable_n1"] for r in bucket) / len(bucket)
        bar  = "█" * int(prec * 20)
        print(f"  {name:<40} {len(bucket):>5}  {prec:>6.1%}  {bar}")
    print(f"  {'Base rate':.<40} {'':>5}  {base:>6.1%}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not PROXY_CSV.exists():
        raise SystemExit(f"{PROXY_CSV} not found — run proxy_dataset.py first.")

    all_rows = list(csv.DictReader(open(PROXY_CSV)))

    # Remove bad epochs
    all_rows = [r for r in all_rows if r["epoch_week"] not in BAD_EPOCHS]

    by_epoch = defaultdict(list)
    for r in all_rows:
        by_epoch[r["epoch_week"]].append(r)

    epochs = sorted(by_epoch.keys(), key=_week_key)

    # Pool age: days since first appearance in the dataset (conservative proxy)
    pool_first = {}
    for ep in epochs:
        ek = _week_key(ep)
        for r in by_epoch[ep]:
            addr = r["pool"].lower()
            if addr not in pool_first:
                pool_first[addr] = ek

    def _age_days(addr, current_ek):
        first = pool_first.get(addr)
        if not first:
            return 9999
        yr0, w0 = first
        yr1, w1 = current_ek
        return max(((yr1 - yr0) * 52 + (w1 - w0)) * 7, 0)

    threshold = S.get("low_tvl_threshold_usd", 25_000)

    all_results = []
    epoch_pairs_used = 0

    for i, epoch_n in enumerate(epochs[:-1]):
        epoch_n1 = _next_week(epoch_n)
        if epoch_n1 not in by_epoch or epoch_n1 in BAD_EPOCHS:
            continue

        # N+1 outcome: only pools with emissions > 0 (otherwise trivially profitable)
        n1_outcome = {
            r["pool"].lower(): (r["profitable"] == "True")
            for r in by_epoch[epoch_n1]
            if _f(r.get("emissions_usd")) > 0
        }
        if not n1_outcome:
            continue

        # Only score epoch-N pools that have a meaningful N+1 outcome
        to_score = [
            deepcopy(r) for r in by_epoch[epoch_n]
            if r["pool"].lower() in n1_outcome
        ]
        if len(to_score) < MIN_PAIRS:
            continue

        # low_tvl_perf built only from epochs strictly before epoch_n
        past_rows = [r for ep in epochs[:i] for r in by_epoch[ep]]
        ltv_lookup = _build_low_tvl_lookup(past_rows)

        ek = _week_key(epoch_n)
        age_lookup = {r["pool"].lower(): _age_days(r["pool"], ek) for r in to_score}

        _score_epoch(to_score, ltv_lookup, age_lookup)
        epoch_pairs_used += 1

        for r in to_score:
            addr = r["pool"].lower()
            tvl  = _f(r.get("tvl_usd"))
            all_results.append({
                "epoch_n":       epoch_n,
                "epoch_n1":      epoch_n1,
                "pool":          addr,
                "pair_symbols":  r.get("pair_symbols", ""),
                "fee_tier_bps":  r.get("fee_tier_bps", ""),
                "tvl_n":         round(tvl, 2),
                "score":         r["_score"],
                "profitable_n1": n1_outcome[addr],
                "low_tvl":       tvl <= threshold,
            })

    if not all_results:
        raise SystemExit("No valid epoch pairs found.")

    print(f"Epoch pairs used: {epoch_pairs_used}")
    print(f"Total pool-epoch observations: {len(all_results)}")

    low_tvl_results = [r for r in all_results if r["low_tvl"]]
    print(f"Low-TVL observations (≤${threshold:,}): {len(low_tvl_results)}")

    _quartile_report(all_results,      "All pools")
    _quartile_report(low_tvl_results,  f"Low-TVL pools only (TVL ≤ ${threshold:,})")

    # Write results CSV
    RESULTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_results[0].keys()))
        w.writeheader()
        w.writerows(all_results)
    print(f"\nWrote {len(all_results)} rows → {RESULTS_CSV}")


if __name__ == "__main__":
    main()
