"""
backtest.py — Validate and optimise the phase-1 weighted scorer against
next-epoch profitability on Aerodrome proxy data.

Default mode
  For each consecutive epoch pair (N → N+1):
    Score every pool using epoch N features, look up whether it is profitable
    in epoch N+1 (only where emissions_usd > 0), report Spearman ρ and
    precision-by-quartile for ALL pools and for LOW-TVL pools separately.

--optimize-weights [--lookback-weeks K]
  70/30 walk-forward split by epoch order.
  Trains logistic regression on ALL pools with emissions > 0 (no TVL filter —
  avoids the circularity of training on the same low-TVL subset that the
  low_tvl_perf feature is derived from).
  When --lookback-weeks K > 1 (default 1 = single-epoch, current behaviour):
    - Replaces the three quantitative features with K-week rolling averages.
    - Adds three new features: fees_trend, vol_trend, consistency.
  Prints a comparison table of current vs suggested weights and evaluates
  both on the held-out test set.

Run:
    python backtest.py
    python backtest.py --optimize-weights
    python backtest.py --optimize-weights --lookback-weeks 4
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

BAD_EPOCHS = {"2025-W01", "2026-W24"}
MIN_PAIRS  = 20

# Feature names as they appear in selection_config.json (order matters — aligns with FEAT_KEYS)
FEAT_CFG_NAMES = [
    "est_fees_per_day_usd",
    "fees_per_tvl_ratio",
    "vol_tvl_24h",
    "pair_type_score",
    "low_tvl_perf",
    "newness_bonus",
    "buy_sell_balance",
]
# Internal row keys populated by _compute_features / _compute_features_lookback
FEAT_KEYS = [
    "_f_est_fees",
    "_f_fees_tvl",
    "_f_vol_tvl",
    "_f_pair_type",
    "_f_low_tvl_perf",
    "_f_newness",
    "_f_bs",
]

# Extra features added when lookback > 1
EXTRA_CFG_NAMES = ["fees_trend", "vol_trend", "consistency"]
EXTRA_KEYS      = ["_f_fees_trend", "_f_vol_trend", "_f_consistency"]


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
# low_tvl_perf lookup — past epochs only
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
    default    = all_scores[len(all_scores) // 2] if all_scores else 2.5
    return {"by_pool": by_pool, "by_pair": by_pair, "default": default}


# ---------------------------------------------------------------------------
# feature computation — single-epoch (current behaviour, k=1)
# ---------------------------------------------------------------------------

def _compute_features(rows, ltv_lookup, age_lookup):
    """Populate FEAT_KEYS from epoch-N data only."""
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
        r["_f_bs"]      = 0
    return rows


# ---------------------------------------------------------------------------
# feature computation — K-week rolling window (k > 1)
# ---------------------------------------------------------------------------

def _compute_features_lookback(rows, by_epoch, epochs, epoch_idx, k, ltv_lookup, age_lookup):
    """
    Populate FEAT_KEYS + EXTRA_KEYS using a K-week rolling window.

    For the three quantitative features (fees, vol, TVL) we use the mean
    across the window (current epoch included) instead of the single-epoch
    snapshot.  Three new features are also computed:
      fees_trend   — current-epoch fees / mean of prior K-1 epochs (momentum)
      vol_trend    — same for volume
      consistency  — fraction of the K window epochs the pool appeared in
    """
    major = {t.upper() for t in S.get("major_tokens", [])}
    cap   = CONFIG["candidate_filters"]["max_pool_age_days_for_new_bucket"]

    # Collect per-pool stats from the prior K-1 epochs in the window
    window_start   = max(0, epoch_idx - k + 1)
    prior_epochs   = epochs[window_start:epoch_idx]   # excludes current epoch

    prior_fees = defaultdict(list)
    prior_vol  = defaultdict(list)
    prior_tvl  = defaultdict(list)
    prior_seen = defaultdict(int)   # appearances in prior window

    for ep in prior_epochs:
        for r in by_epoch.get(ep, []):
            addr = r["pool"].lower()
            prior_fees[addr].append(_f(r.get("fees_usd")))
            prior_vol[addr].append(_f(r.get("volume_usd")))
            prior_tvl[addr].append(_f(r.get("tvl_usd")))
            prior_seen[addr] += 1

    for r in rows:
        addr = r["pool"].lower()

        cur_fees = _f(r.get("fees_usd"))
        cur_vol  = _f(r.get("volume_usd"))
        cur_tvl  = _f(r.get("tvl_usd"))

        pf = prior_fees[addr]
        pv = prior_vol[addr]
        pt = prior_tvl[addr]

        # Rolling means (include current epoch)
        avg_fees = (sum(pf) + cur_fees) / (len(pf) + 1)
        avg_vol  = (sum(pv) + cur_vol)  / (len(pv) + 1)
        avg_tvl  = (sum(pt) + cur_tvl)  / (len(pt) + 1)

        avg_fees_daily = avg_fees / 7
        avg_vol_daily  = avg_vol  / 7

        r["_f_est_fees"] = avg_fees_daily
        r["_f_fees_tvl"] = avg_fees_daily / avg_tvl if avg_tvl > 0 else 0
        r["_f_vol_tvl"]  = avg_vol_daily  / avg_tvl if avg_tvl > 0 else 0

        # Trend: current epoch vs prior-window average (1.0 if no prior data)
        pa_fees = sum(pf) / len(pf) if pf else None
        pa_vol  = sum(pv) / len(pv) if pv else None
        r["_f_fees_trend"] = min(cur_fees / pa_fees, 3.0) if pa_fees else 1.0
        r["_f_vol_trend"]  = min(cur_vol  / pa_vol,  3.0) if pa_vol  else 1.0

        # Consistency: fraction of K epochs this pool appeared in
        total_seen = prior_seen[addr] + 1   # +1 for current epoch
        r["_f_consistency"] = total_seen / k

        # Static features (unchanged)
        tokens  = [t.strip().upper() for t in r.get("pair_symbols", "/").split("/")][:2]
        n_major = sum(1 for t in tokens if t in major)
        r["_f_pair_type"] = [0.3, 0.7, 1.0][n_major]

        raw = (ltv_lookup["by_pool"].get(addr)
               or ltv_lookup["by_pair"].get(frozenset(tokens))
               or ltv_lookup["default"])
        r["_f_low_tvl_perf"] = min(raw / 5.0, 1.0)

        age = age_lookup.get(addr, 9999)
        r["_f_newness"] = max(0.0, 1 - age / cap) if cap > 0 else 0
        r["_f_bs"]      = 0

    return rows


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def _score_with_weights(rows, weights, feat_keys=None):
    if feat_keys is None:
        feat_keys = FEAT_KEYS
    normed = {k: _normalize([_f(r[k]) for r in rows]) for k in feat_keys}
    for i, r in enumerate(rows):
        r["_score"] = round(
            sum(weights[j] * normed[k][i] for j, k in enumerate(feat_keys)), 4
        )
    return rows


# ---------------------------------------------------------------------------
# shared data loader
# ---------------------------------------------------------------------------

def _load_epoch_pairs():
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
# default mode — correlation / quartile analysis
# ---------------------------------------------------------------------------

def main():
    by_epoch, epochs, _age_days, valid_pairs = _load_epoch_pairs()
    threshold       = S.get("low_tvl_threshold_usd", 25_000)
    current_weights = [S["weights"].get(n, 0) for n in FEAT_CFG_NAMES]

    all_results = []
    for i, epoch_n, epoch_n1, n1_outcome in valid_pairs:
        past_rows  = [r for ep in epochs[:i] for r in by_epoch[ep]]
        ltv_lookup = _build_low_tvl_lookup(past_rows)
        ek         = _week_key(epoch_n)
        to_score   = [deepcopy(r) for r in by_epoch[epoch_n]
                      if r["pool"].lower() in n1_outcome]
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

def optimize_weights(lookback_weeks=1):
    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import MinMaxScaler
    except ImportError:
        raise SystemExit("pip install scikit-learn  (needed for --optimize-weights)")

    by_epoch, epochs, _age_days, valid_pairs = _load_epoch_pairs()
    threshold = S.get("low_tvl_threshold_usd", 25_000)
    use_lookback = lookback_weeks > 1

    feat_cfg = FEAT_CFG_NAMES + (EXTRA_CFG_NAMES if use_lookback else [])
    feat_keys = FEAT_KEYS      + (EXTRA_KEYS      if use_lookback else [])

    n_train     = max(int(len(valid_pairs) * 0.70), 1)
    train_pairs = valid_pairs[:n_train]
    test_pairs  = valid_pairs[n_train:]
    print(f"Epoch pairs: {len(valid_pairs)} total — "
          f"{n_train} train ({valid_pairs[0][1]} → {valid_pairs[n_train-1][1]}), "
          f"{len(test_pairs)} test ({valid_pairs[n_train][1]} → {valid_pairs[-1][1]})")
    if use_lookback:
        print(f"Lookback window: {lookback_weeks} weeks "
              f"(rolling avg + trend + consistency features)")
    print(f"Training on: all pools with emissions > 0  "
          f"(no TVL filter — avoids low_tvl_perf circularity)")

    def _featurise(pairs, for_training=False):
        """Score/featurise all pools for a list of epoch pairs."""
        obs = []
        for i, epoch_n, epoch_n1, n1_outcome in pairs:
            past_rows  = [r for ep in epochs[:i] for r in by_epoch[ep]]
            ltv_lookup = _build_low_tvl_lookup(past_rows)
            ek         = _week_key(epoch_n)
            to_score   = [deepcopy(r) for r in by_epoch[epoch_n]
                          if r["pool"].lower() in n1_outcome]
            if not to_score:
                continue
            age_lookup = {r["pool"].lower(): _age_days(r["pool"], ek)
                          for r in to_score}
            if use_lookback:
                _compute_features_lookback(to_score, by_epoch, epochs, i,
                                           lookback_weeks, ltv_lookup, age_lookup)
            else:
                _compute_features(to_score, ltv_lookup, age_lookup)
            for r in to_score:
                addr = r["pool"].lower()
                tvl  = _f(r.get("tvl_usd"))
                obs.append({
                    "features":      [_f(r[k]) for k in feat_keys],
                    "label":         int(n1_outcome[addr]),
                    "score":         None,   # filled in after weight fitting
                    "profitable_n1": n1_outcome[addr],
                    "tvl":           tvl,
                    "_row":          r,      # kept for re-scoring with weights
                })
        return obs

    # --- collect training observations ---
    print("\nCollecting training observations…")
    train_obs = _featurise(train_pairs, for_training=True)
    X_train   = [o["features"] for o in train_obs]
    y_train   = [o["label"]    for o in train_obs]
    print(f"  {len(X_train)} observations  "
          f"({sum(y_train)} profitable / {len(y_train)-sum(y_train)} not)")

    tvl_buckets = [
        (f"  Low-TVL  (≤${threshold:,})",     lambda t: t <= threshold),
        (f"  Mid-TVL  (${threshold:,}–$500k)", lambda t: threshold < t <= 500_000),
        (f"  High-TVL (>$500k)",               lambda t: t > 500_000),
    ]
    for label, fn in tvl_buckets:
        n = sum(1 for o in train_obs if fn(o["tvl"]))
        p = sum(1 for o in train_obs if fn(o["tvl"]) and o["label"]) / n if n else 0
        print(f"  {label}: n={n}, base={p:.1%}")

    if len(X_train) < 50:
        raise SystemExit(f"Only {len(X_train)} training observations — not enough.")

    # --- fit logistic regression ---
    scaler   = MinMaxScaler()
    X_scaled = scaler.fit_transform(X_train)
    lr       = LogisticRegression(class_weight="balanced", max_iter=2000,
                                  random_state=0, C=1.0)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        lr.fit(X_scaled, y_train)
    coefs = lr.coef_[0]

    # Clip negatives to 0 and renormalise to sum=1
    suggested_raw = [max(c, 0.0) for c in coefs]
    total         = sum(suggested_raw) or 1.0
    suggested     = [w / total for w in suggested_raw]

    # Current weights (normalised to sum=1 for fair comparison)
    current     = [S["weights"].get(n, 0) for n in FEAT_CFG_NAMES]
    cur_total   = sum(current) or 1.0
    current_norm = [w / cur_total for w in current]
    # Pad current weights with 0 for extra lookback features
    current_padded = current_norm + [0.0] * len(EXTRA_CFG_NAMES if use_lookback else [])

    # --- comparison table ---
    print(f"\n{'Feature':<26} {'Current':>9} {'Suggested':>10} {'LR coef':>9}  Note")
    print("─" * 72)
    for j, (name, cur, sug, coef) in enumerate(
            zip(feat_cfg, current_padded, suggested, coefs)):
        is_new  = use_lookback and j >= len(FEAT_CFG_NAMES)
        note    = "(new)" if is_new else ""
        if coef < 0:
            note = "⚠ negative → clipped to 0"
        elif not is_new and abs(sug - cur) >= 0.08:
            note = "△ large shift"
        cur_str = f"{cur:9.3f}" if not is_new else "         —"
        print(f"  {name:<24} {cur_str} {sug:>10.3f} {coef:>+9.3f}  {note}")

    # --- evaluate both weight sets on held-out test pairs ---
    def _score_obs(pairs, weights):
        """Re-featurise test pairs and score with the given weights."""
        obs = []
        for i, epoch_n, epoch_n1, n1_outcome in pairs:
            past_rows  = [r for ep in epochs[:i] for r in by_epoch[ep]]
            ltv_lookup = _build_low_tvl_lookup(past_rows)
            ek         = _week_key(epoch_n)
            to_score   = [deepcopy(r) for r in by_epoch[epoch_n]
                          if r["pool"].lower() in n1_outcome]
            if not to_score:
                continue
            age_lookup = {r["pool"].lower(): _age_days(r["pool"], ek)
                          for r in to_score}
            if use_lookback:
                _compute_features_lookback(to_score, by_epoch, epochs, i,
                                           lookback_weeks, ltv_lookup, age_lookup)
            else:
                _compute_features(to_score, ltv_lookup, age_lookup)
            _score_with_weights(to_score, weights, feat_keys=feat_keys)
            for r in to_score:
                addr = r["pool"].lower()
                obs.append({
                    "score":         r["_score"],
                    "profitable_n1": n1_outcome[addr],
                    "tvl":           _f(r.get("tvl_usd")),
                })
        return obs

    def _metrics(obs, tvl_filter=None):
        subset = [o for o in obs if tvl_filter is None or tvl_filter(o["tvl"])]
        if not subset:
            return 0.0, 0.0, 0.0, 0
        scores = [o["score"]             for o in subset]
        labels = [int(o["profitable_n1"]) for o in subset]
        rho, _ = spearmanr(scores, labels)
        base   = sum(labels) / len(labels)
        q      = len(subset) // 4
        top_q  = sorted(subset, key=lambda o: o["score"], reverse=True)[:q]
        q4     = sum(o["profitable_n1"] for o in top_q) / len(top_q) if top_q else 0.0
        return rho, q4, base, len(subset)

    print(f"\nCollecting test observations…")
    cur_obs = _score_obs(test_pairs, current_padded)
    sug_obs = _score_obs(test_pairs, suggested)

    cur_rho, cur_q4, base, n_obs = _metrics(cur_obs)
    sug_rho, sug_q4, _,   _     = _metrics(sug_obs)

    print(f"\nHeld-out test ({len(test_pairs)} epoch pairs, all pools):")
    print(f"  {'Metric':<34} {'Current':>10} {'Suggested':>10}")
    print("  " + "─" * 56)
    print(f"  {'Observations':<34} {n_obs:>10}")
    print(f"  {'Base rate (profitable next epoch)':<34} {base:>10.1%}")
    print(f"  {'Spearman ρ':<34} {cur_rho:>10.3f} {sug_rho:>10.3f}")
    print(f"  {'Q4 precision (top-25% scorers)':<34} {cur_q4:>10.1%} {sug_q4:>10.1%}")

    print(f"\n  By TVL stratum (suggested weights, test set):")
    for label, fn in tvl_buckets:
        rho, q4, base_s, n = _metrics(sug_obs, tvl_filter=fn)
        if n == 0:
            continue
        print(f"  {label}: n={n:>5}, base={base_s:.1%}, "
              f"Q4={q4:.1%}, ρ={rho:.3f}")

    # --- ready-to-paste config block ---
    print("\nSuggested weights for selection_config.json → scoring.weights:")
    print('    "weights": {')
    for name, w in zip(FEAT_CFG_NAMES, suggested[:len(FEAT_CFG_NAMES)]):
        print(f'      "{name}": {w:.3f},')
    print("    }")

    if use_lookback:
        print(f"\n  New features (require multi-week subgraph data in the live pipeline):")
        for name, w in zip(EXTRA_CFG_NAMES, suggested[len(FEAT_CFG_NAMES):]):
            print(f"    {name}: {w:.3f}")

    neg_feats = [n for n, c in zip(feat_cfg, coefs) if c < 0]
    if neg_feats:
        print(f"\n  ⚠ Clipped to 0: {', '.join(neg_feats)}")
        print("    Negative LR coefficients mean these features hurt prediction")
        print("    when controlling for others — worth investigating.")


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--optimize-weights", action="store_true")
    ap.add_argument("--lookback-weeks", type=int, default=1,
                    help="Rolling window size for --optimize-weights (default 1 = single epoch)")
    args = ap.parse_args()
    if args.optimize_weights:
        optimize_weights(lookback_weeks=args.lookback_weeks)
    else:
        main()
