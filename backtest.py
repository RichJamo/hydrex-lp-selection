"""
backtest.py — Validate and optimise the phase-1 weighted scorer against
next-epoch profitability on Aerodrome proxy data.

Default mode (no flags)
  For each consecutive epoch pair (N → N+1):
    1. Score every pool using epoch N features.
    2. Look up whether that pool is profitable in epoch N+1
       (only where emissions_usd > 0 — otherwise 'profitable' is trivially true).
    3. Report Spearman ρ and precision-by-quartile for ALL pools and for
       LOW-TVL pools (≤ low_tvl_threshold_usd), which match Hydrex bootstrap scale.

--optimize-weights
  70/30 walk-forward split (by epoch order, not random).
  On the training half: fit logistic regression on low-TVL observations
    (raw features, globally normalized) → next-epoch profitable label.
  Derive suggested weights from the LR coefficients.
  Evaluate current vs suggested weights on the held-out 30%.
  Print a comparison table and a ready-to-paste config block.

Run:
    python backtest.py
    python backtest.py --optimize-weights
"""

import argparse
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

BAD_EPOCHS = {"2025-W01", "2026-W24"}  # 100% profitable — incomplete emission data
MIN_PAIRS  = 20                          # skip epoch pairs with too few scoreable pools

# Feature names in config order (must stay in sync with FEAT_KEYS below)
FEAT_CFG_NAMES = [
    "est_fees_per_day_usd",
    "fees_per_tvl_ratio",
    "vol_tvl_24h",
    "pair_type_score",
    "low_tvl_perf",
    "newness_bonus",
    "buy_sell_balance",
]
# Corresponding row keys set by _compute_features()
FEAT_KEYS = [
    "_f_est_fees",
    "_f_fees_tvl",
    "_f_vol_tvl",
    "_f_pair_type",
    "_f_low_tvl_perf",
    "_f_newness",
    "_f_bs",
]


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
# low_tvl_perf — built from past epochs only (no look-ahead)
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
# feature computation (separated from scoring so LR can use raw values)
# ---------------------------------------------------------------------------

def _compute_features(rows, ltv_lookup, age_lookup):
    """Populate FEAT_KEYS on each row (raw, pre-normalisation values)."""
    major = {t.upper() for t in S.get("major_tokens", [])}
    cap   = CONFIG["candidate_filters"]["max_pool_age_days_for_new_bucket"]

    for r in rows:
        vol_daily  = _f(r.get("volume_usd")) / 7
        fees_daily = _f(r.get("fees_usd"))   / 7
        tvl        = _f(r.get("tvl_usd"))

        r["_f_est_fees"] = fees_daily
        r["_f_fees_tvl"] = fees_daily / tvl if tvl > 0 else 0
        r["_f_vol_tvl"]  = vol_daily  / tvl if tvl > 0 else 0

        tokens  = [t.strip().upper() for t in r.get("pair_symbols", "/").split("/")][:2]
        n_major = sum(1 for t in tokens if t in major)
        r["_f_pair_type"] = [0.3, 0.7, 1.0][n_major]

        addr     = r["pool"].lower()
        pair_key = frozenset(tokens)
        raw      = (ltv_lookup["by_pool"].get(addr)
                    or ltv_lookup["by_pair"].get(pair_key)
                    or ltv_lookup["default"])
        r["_f_low_tvl_perf"] = min(raw / 5.0, 1.0)

        age = age_lookup.get(addr, 9999)
        r["_f_newness"] = max(0.0, 1 - age / cap) if cap > 0 else 0
        r["_f_bs"]      = 0   # buy/sell ratio not in proxy data

    return rows


def _score_with_weights(rows, weights):
    """
    Apply per-epoch min-max normalisation and dot with weights.
    Writes _score onto each row. weights is a list aligned to FEAT_KEYS.
    """
    normed = {k: _normalize([_f(r[k]) for r in rows]) for k in FEAT_KEYS}
    for i, r in enumerate(rows):
        r["_score"] = round(
            sum(weights[j] * normed[k][i] for j, k in enumerate(FEAT_KEYS)), 4
        )
    return rows


# ---------------------------------------------------------------------------
# shared data-loading logic
# ---------------------------------------------------------------------------

def _load_epoch_pairs():
    """
    Load proxy CSV, build by_epoch dict, sorted epoch list, pool-age lookup,
    and the list of valid (i, epoch_n, epoch_n1, n1_outcome) tuples.
    """
    if not PROXY_CSV.exists():
        raise SystemExit(f"{PROXY_CSV} not found — run proxy_dataset.py first.")

    all_rows = [r for r in csv.DictReader(open(PROXY_CSV))
                if r["epoch_week"] not in BAD_EPOCHS]

    by_epoch = defaultdict(list)
    for r in all_rows:
        by_epoch[r["epoch_week"]].append(r)

    epochs = sorted(by_epoch.keys(), key=_week_key)

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

    valid_pairs = []
    for i, epoch_n in enumerate(epochs[:-1]):
        epoch_n1 = _next_week(epoch_n)
        if epoch_n1 not in by_epoch or epoch_n1 in BAD_EPOCHS:
            continue
        n1_outcome = {
            r["pool"].lower(): (r["profitable"] == "True")
            for r in by_epoch[epoch_n1]
            if _f(r.get("emissions_usd")) > 0
        }
        if not n1_outcome:
            continue
        scoreable = [r for r in by_epoch[epoch_n] if r["pool"].lower() in n1_outcome]
        if len(scoreable) < MIN_PAIRS:
            continue
        valid_pairs.append((i, epoch_n, epoch_n1, n1_outcome))

    return by_epoch, epochs, _age_days, valid_pairs


# ---------------------------------------------------------------------------
# quartile report
# ---------------------------------------------------------------------------

def _quartile_report(obs, label):
    if not obs:
        print(f"\n{label}: no data")
        return
    n    = len(obs)
    base = sum(r["profitable_n1"] for r in obs) / n
    srt  = sorted(obs, key=lambda r: r["score"])
    q    = n // 4
    slices = [
        ("Q1 — bottom 25% (lowest score)", srt[:q]),
        ("Q2",                              srt[q:2*q]),
        ("Q3",                              srt[2*q:3*q]),
        ("Q4 — top 25% (highest score)",   srt[3*q:]),
    ]
    rho, pval = spearmanr([r["score"] for r in obs],
                          [int(r["profitable_n1"]) for r in obs])
    print(f"\n{label}  (n={n}, base={base:.1%}, Spearman ρ={rho:.3f} p={pval:.4f})")
    print(f"  {'Quartile':<40} {'N':>5}  {'Profitable next epoch':>22}")
    for name, bucket in slices:
        if not bucket:
            continue
        prec = sum(r["profitable_n1"] for r in bucket) / len(bucket)
        bar  = "█" * int(prec * 20)
        print(f"  {name:<40} {len(bucket):>5}  {prec:>6.1%}  {bar}")
    print(f"  {'Base rate':.<40} {'':>5}  {base:>6.1%}")


# ---------------------------------------------------------------------------
# main — correlation / quartile analysis
# ---------------------------------------------------------------------------

def main():
    by_epoch, epochs, _age_days, valid_pairs = _load_epoch_pairs()
    threshold = S.get("low_tvl_threshold_usd", 25_000)
    current_weights = [S["weights"].get(n, 0) for n in FEAT_CFG_NAMES]

    all_results = []
    for i, epoch_n, epoch_n1, n1_outcome in valid_pairs:
        past_rows  = [r for ep in epochs[:i] for r in by_epoch[ep]]
        ltv_lookup = _build_low_tvl_lookup(past_rows)
        ek         = _week_key(epoch_n)

        to_score = [
            deepcopy(r) for r in by_epoch[epoch_n]
            if r["pool"].lower() in n1_outcome
        ]
        age_lookup = {r["pool"].lower(): _age_days(r["pool"], ek) for r in to_score}
        _compute_features(to_score, ltv_lookup, age_lookup)
        _score_with_weights(to_score, current_weights)

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

    print(f"Epoch pairs used: {len(valid_pairs)}")
    print(f"Total pool-epoch observations: {len(all_results)}")

    low_tvl = [r for r in all_results if r["low_tvl"]]
    print(f"Low-TVL observations (≤${threshold:,}): {len(low_tvl)}")

    _quartile_report(all_results, "All pools")
    _quartile_report(low_tvl,     f"Low-TVL pools (TVL ≤ ${threshold:,})")

    RESULTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_results[0].keys()))
        w.writeheader()
        w.writerows(all_results)
    print(f"\nWrote {len(all_results)} rows → {RESULTS_CSV}")


# ---------------------------------------------------------------------------
# --optimize-weights
# ---------------------------------------------------------------------------

def optimize_weights():
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import MinMaxScaler
    except ImportError:
        raise SystemExit("pip install scikit-learn  (needed for --optimize-weights)")

    by_epoch, epochs, _age_days, valid_pairs = _load_epoch_pairs()
    threshold = S.get("low_tvl_threshold_usd", 25_000)

    # 70 / 30 walk-forward split by epoch order
    n_train     = max(int(len(valid_pairs) * 0.70), 1)
    train_pairs = valid_pairs[:n_train]
    test_pairs  = valid_pairs[n_train:]
    print(f"Epoch pairs: {len(valid_pairs)} total — {n_train} train "
          f"({valid_pairs[0][1]} → {valid_pairs[n_train-1][1]}), "
          f"{len(test_pairs)} test "
          f"({valid_pairs[n_train][1]} → {valid_pairs[-1][1]})")

    # --- collect raw features for low-TVL training observations ---
    X_raw, y = [], []
    for i, epoch_n, epoch_n1, n1_outcome in train_pairs:
        past_rows  = [r for ep in epochs[:i] for r in by_epoch[ep]]
        ltv_lookup = _build_low_tvl_lookup(past_rows)
        ek         = _week_key(epoch_n)

        to_score = [
            deepcopy(r) for r in by_epoch[epoch_n]
            if r["pool"].lower() in n1_outcome
            and 0 < _f(r.get("tvl_usd")) <= threshold
        ]
        if not to_score:
            continue
        age_lookup = {r["pool"].lower(): _age_days(r["pool"], ek) for r in to_score}
        _compute_features(to_score, ltv_lookup, age_lookup)
        for r in to_score:
            X_raw.append([_f(r[k]) for k in FEAT_KEYS])
            y.append(int(n1_outcome[r["pool"].lower()]))

    print(f"Training observations (low-TVL): {len(X_raw)} "
          f"({sum(y)} profitable / {len(y)-sum(y)} not)")

    if len(X_raw) < 50:
        raise SystemExit(f"Only {len(X_raw)} training obs — not enough to fit.")

    # --- fit logistic regression on globally-normalised features ---
    # Global min-max ensures coefficients are in the same 0-1 space as our
    # per-run normalisation, making them directly comparable to current weights.
    scaler       = MinMaxScaler()
    X_scaled     = scaler.fit_transform(X_raw)
    lr           = LogisticRegression(class_weight="balanced", max_iter=1000,
                                      random_state=0)
    lr.fit(X_scaled, y)
    coefs        = lr.coef_[0]

    # Clip negatives to 0 (a negative weight would invert the feature direction,
    # which isn't supported by the current scoring framework)
    suggested_raw = [max(c, 0.0) for c in coefs]
    total         = sum(suggested_raw) or 1.0
    suggested     = [w / total for w in suggested_raw]

    current       = [S["weights"].get(n, 0) for n in FEAT_CFG_NAMES]
    cur_total     = sum(current) or 1.0
    current_norm  = [w / cur_total for w in current]

    # --- comparison table ---
    print(f"\n{'Feature':<26} {'Current':>9} {'Suggested':>10} {'LR coef':>9}  Note")
    print("─" * 72)
    for name, cur, sug, coef in zip(FEAT_CFG_NAMES, current_norm, suggested, coefs):
        note = ""
        if coef < 0:
            note = "⚠ negative → clipped to 0"
        elif abs(sug - cur) >= 0.08:
            note = "△ large shift"
        print(f"  {name:<24} {cur:>9.3f} {sug:>10.3f} {coef:>+9.3f}  {note}")

    # --- evaluate both weight sets on held-out test pairs ---
    def _collect_obs(pairs, weights):
        obs = []
        for i, epoch_n, epoch_n1, n1_outcome in pairs:
            past_rows  = [r for ep in epochs[:i] for r in by_epoch[ep]]
            ltv_lookup = _build_low_tvl_lookup(past_rows)
            ek         = _week_key(epoch_n)
            to_score   = [
                deepcopy(r) for r in by_epoch[epoch_n]
                if r["pool"].lower() in n1_outcome
                and 0 < _f(r.get("tvl_usd")) <= threshold
            ]
            if not to_score:
                continue
            age_lookup = {r["pool"].lower(): _age_days(r["pool"], ek) for r in to_score}
            _compute_features(to_score, ltv_lookup, age_lookup)
            _score_with_weights(to_score, weights)
            for r in to_score:
                obs.append({
                    "score":         r["_score"],
                    "profitable_n1": n1_outcome[r["pool"].lower()],
                })
        return obs

    def _metrics(obs):
        if not obs:
            return 0.0, 0.0, 0.0
        scores = [o["score"]           for o in obs]
        labels = [int(o["profitable_n1"]) for o in obs]
        rho, _ = spearmanr(scores, labels)
        base   = sum(labels) / len(labels)
        q      = len(obs) // 4
        top_q  = sorted(obs, key=lambda o: o["score"], reverse=True)[:q]
        q4     = sum(o["profitable_n1"] for o in top_q) / len(top_q) if top_q else 0.0
        return rho, q4, base

    print(f"\nHeld-out test set ({len(test_pairs)} epoch pairs, low-TVL pools):")
    cur_obs = _collect_obs(test_pairs, current_norm)
    sug_obs = _collect_obs(test_pairs, suggested)
    cur_rho, cur_q4, base = _metrics(cur_obs)
    sug_rho, sug_q4, _   = _metrics(sug_obs)

    print(f"  {'Metric':<34} {'Current':>10} {'Suggested':>10}")
    print("  " + "─" * 56)
    print(f"  {'Observations':<34} {len(cur_obs):>10}")
    print(f"  {'Base rate':<34} {base:>10.1%}")
    print(f"  {'Spearman ρ':<34} {cur_rho:>10.3f} {sug_rho:>10.3f}")
    print(f"  {'Q4 precision (top-25% scorers)':<34} {cur_q4:>10.1%} {sug_q4:>10.1%}")

    # --- ready-to-paste config block ---
    print("\nSuggested weights (paste into selection_config.json → scoring.weights):")
    print('    "weights": {')
    for name, w in zip(FEAT_CFG_NAMES, suggested):
        print(f'      "{name}": {w:.3f},')
    print("    }")

    neg_feats = [n for n, c in zip(FEAT_CFG_NAMES, coefs) if c < 0]
    if neg_feats:
        print(f"\n  ⚠ Clipped-to-zero features: {', '.join(neg_feats)}")
        print("    These had negative LR coefficients — controlling for all other")
        print("    features they do not help predict next-epoch profitability.")
        print("    Worth examining whether the feature definition matches the proxy data.")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--optimize-weights", action="store_true",
                    help="Fit logistic regression on training half and compare "
                         "to current weights on held-out test half.")
    args = ap.parse_args()
    if args.optimize_weights:
        optimize_weights()
    else:
        main()
