"""
backtest.py — Walk-forward backtest of the phase-1 weighted scorer on Aerodrome proxy data.

Steps:
  1. Explore data/aerodrome_proxy.csv — shape, epoch range, base rate.
  2. Walk-forward backtest: train window = oldest 80% of epochs; test window = newest 20%.
     For each test epoch, score all pools (with look-ahead-safe low_tvl_perf), pick top 4,
     measure precision vs ground-truth profitable label.
  3. Feature importance on training window only — compare to hand-tuned weights.
  4. Write results to data/backtest_results.csv and print a summary report.

Run:
    python backtest.py

Preconditions:
  - data/aerodrome_proxy.csv must exist and be non-empty.
  - score.py must be importable (same directory).
  - scikit-learn and numpy must be installed for feature importance.
"""

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROXY_CSV   = SCRIPT_DIR / "data" / "aerodrome_proxy.csv"
RESULTS_CSV = SCRIPT_DIR / "data" / "backtest_results.csv"
CONFIG_PATH = SCRIPT_DIR / "selection_config.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _load_proxy() -> list:
    if not PROXY_CSV.exists():
        raise SystemExit(
            f"ERROR: {PROXY_CSV} not found.\n"
            "To build it, run:  python proxy_dataset.py\n"
            "That requires DUNE_API_KEY in your environment and a valid\n"
            "proxy_dataset.aerodrome_query_id in selection_config.json."
        )
    with open(PROXY_CSV, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit(f"ERROR: {PROXY_CSV} exists but is empty — nothing to backtest.")
    return rows


def _week_to_int(week_str: str) -> int:
    """Convert '2025-W07' to an integer for ordering. Returns 0 on failure."""
    try:
        year, wnum = week_str.split("-W")
        return int(year) * 100 + int(wnum)
    except (ValueError, AttributeError):
        return 0


# ---------------------------------------------------------------------------
# Step 1 — Explore
# ---------------------------------------------------------------------------

def explore(rows: list) -> dict:
    """Print and return summary statistics about the proxy dataset."""
    print("=" * 70)
    print("STEP 1 — DATASET EXPLORATION")
    print("=" * 70)
    print(f"Total rows        : {len(rows):,}")
    print(f"Columns           : {', '.join(rows[0].keys())}")

    epochs = sorted(set(r["epoch_week"] for r in rows), key=_week_to_int)
    print(f"Epoch range       : {epochs[0]} -> {epochs[-1]}  ({len(epochs)} epochs)")

    profitable = sum(1 for r in rows if r.get("profitable") == "True")
    base_rate  = profitable / len(rows)
    print(f"Base rate         : {profitable:,}/{len(rows):,} profitable = {base_rate:.1%}")

    # Only rows where Aerodrome actually incentivised the pool
    with_emis = [r for r in rows if _f(r.get("emissions_usd")) > 0]
    if with_emis:
        base_incent = sum(1 for r in with_emis if r.get("profitable") == "True") / len(with_emis)
        print(f"  (incentivised pools only: {len(with_emis):,} rows, "
              f"{base_incent:.1%} profitable)")

    print("\nPer-epoch pool count (sample):")
    from collections import Counter
    epoch_counts = Counter(r["epoch_week"] for r in rows)
    for ep in epochs[:3]:
        ep_rows = [r for r in rows if r["epoch_week"] == ep]
        prof = sum(1 for r in ep_rows if r["profitable"] == "True")
        print(f"  {ep}: {epoch_counts[ep]:3d} pools  {prof/epoch_counts[ep]:.0%} profitable")
    print("  ...")
    for ep in epochs[-3:]:
        ep_rows = [r for r in rows if r["epoch_week"] == ep]
        prof = sum(1 for r in ep_rows if r["profitable"] == "True")
        print(f"  {ep}: {epoch_counts[ep]:3d} pools  {prof/epoch_counts[ep]:.0%} profitable")

    # Data quality notes
    if epochs[0] == "2025-W01":
        w01_rows = [r for r in rows if r["epoch_week"] == "2025-W01"]
        w01_prof = sum(1 for r in w01_rows if r["profitable"] == "True")
        print(f"\n  NOTE: 2025-W01 has {w01_prof}/{len(w01_rows)} profitable (100% -- likely no "
              "emissions recorded in Dune for that week; all rows trivially profitable).")
    last_ep = epochs[-1]
    last_rows = [r for r in rows if r["epoch_week"] == last_ep]
    last_prof = sum(1 for r in last_rows if r["profitable"] == "True")
    if last_prof == len(last_rows):
        print(f"  NOTE: {last_ep} has {last_prof}/{len(last_rows)} profitable (100% -- likely "
              "an incomplete/in-progress epoch with no emissions captured yet).")

    return {
        "epochs":            epochs,
        "base_rate":         base_rate,
        "profitable_count":  profitable,
        "total_rows":        len(rows),
    }


# ---------------------------------------------------------------------------
# Column mapping: proxy row -> compute_features() expected fields
# ---------------------------------------------------------------------------

def _proxy_row_to_candidate(row: dict, pool_first_epoch: dict) -> dict:
    """
    Map a proxy dataset row to the field names that score.compute_features() reads.

    The proxy has weekly aggregates; compute_features() prefers vol_7d_usd / fees_7d_usd
    when present, so we set those directly. Daily fallback fields are also populated
    by dividing by 7.
    """
    pool_addr = row.get("pool", "").lower()

    # pair: score.py reads r["pair"] and splits on "/" to get token symbols.
    pair = row.get("pair_symbols", "/")

    # pool_age_days: weeks between pool's first appearance in the dataset and current epoch.
    first_int   = _week_to_int(pool_first_epoch.get(pool_addr, row["epoch_week"]))
    current_int = _week_to_int(row["epoch_week"])
    age_weeks = max(current_int - first_int, 0)
    pool_age_days = age_weeks * 7

    fees_usd   = _f(row.get("fees_usd"))
    volume_usd = _f(row.get("volume_usd"))
    tvl_usd    = _f(row.get("tvl_usd"))

    return {
        # Identity
        "pair_address":    pool_addr,
        "pair":            pair,
        # 7-day aggregates — compute_features() prefers these
        "vol_7d_usd":      volume_usd,
        "fees_7d_usd":     fees_usd,
        "tvl_avg_7d_usd":  tvl_usd,
        "data_days":       7.0,
        # 24h fallbacks (weekly / 7)
        "liquidity_usd":      tvl_usd,
        "vol_24h":            volume_usd / 7.0,
        "est_fees_24h_usd":   fees_usd   / 7.0,
        # fee tier
        "fee_tier_bps":    row.get("fee_tier_bps", ""),
        # Age (for newness_bonus)
        "pool_age_days":   pool_age_days,
        # buy/sell balance: not in Aero data; 0 means "unknown / neutral"
        "buy_sell_ratio_24h": 0,
        # Unused rolling-CSV fields
        "csv_avg_liquidity_7d": 0,
        "csv_avg_vol_7d":       0,
        "csv_days_seen":        0,
    }


# ---------------------------------------------------------------------------
# Leak-free low_tvl_perf lookup
# ---------------------------------------------------------------------------

def _build_low_tvl_lookup_masked(proxy_rows: list, cutoff_epoch: str) -> dict:
    """
    Build the low_tvl_perf lookup using ONLY proxy rows from epochs before cutoff_epoch.
    This mirrors score._build_low_tvl_lookup() but masks out future data so the
    backtest has no look-ahead into the test window.
    """
    config    = json.loads(CONFIG_PATH.read_text())
    threshold = config["scoring"].get("low_tvl_threshold_usd", 25_000)
    cutoff_int = _week_to_int(cutoff_epoch)

    pool_ratios: dict = defaultdict(list)
    pair_ratios: dict = defaultdict(list)

    for r in proxy_rows:
        if _week_to_int(r["epoch_week"]) >= cutoff_int:
            continue
        tvl  = _f(r.get("tvl_usd"))
        emis = _f(r.get("emissions_usd"))
        fees = _f(r.get("fees_usd"))
        if 0 < tvl <= threshold and emis > 0:
            ratio = min(fees / emis, 5.0)
            pool_ratios[r.get("pool", "").lower()].append(ratio)
            tokens = frozenset(t.upper().strip()
                               for t in r.get("pair_symbols", "/").split("/"))
            pair_ratios[tokens].append(ratio)

    by_pool = {k: sum(v) / len(v) for k, v in pool_ratios.items()}
    by_pair = {k: sum(v) / len(v) for k, v in pair_ratios.items()}
    all_scores = sorted(by_pool.values())
    default = all_scores[len(all_scores) // 2] if all_scores else 2.5
    return {"by_pool": by_pool, "by_pair": by_pair, "default": default}


# ---------------------------------------------------------------------------
# Per-epoch scoring
# ---------------------------------------------------------------------------

def _score_epoch(
    epoch_rows: list,
    low_tvl_lookup: dict,
    pool_first_epoch: dict,
    config: dict,
) -> list:
    """
    Score all proxy rows for one epoch using the phase-1 weighted scorer.

    Temporarily replaces score._build_low_tvl_lookup with a closure over the
    pre-masked lookup to avoid data leakage without restructuring score.py.

    Returns rows sorted by score descending (each row is the candidate dict with
    added 'score' key and proxy identity fields for label lookup).
    """
    import score as _score_mod

    _orig_fn = _score_mod._build_low_tvl_lookup
    _score_mod._build_low_tvl_lookup = lambda: low_tvl_lookup
    try:
        candidates = [
            _proxy_row_to_candidate(r, pool_first_epoch)
            for r in epoch_rows
        ]
        # compute_features() adds the _feat fields we need.
        candidates = _score_mod.compute_features(candidates)

        S = config["scoring"]
        feats = {
            "est_fees_per_day_usd": "_est_fees_per_day_usd",
            "fees_per_tvl_ratio":   "_fees_per_tvl_ratio",
            "vol_tvl_24h":          "_vol_tvl_24h",
            "pair_type_score":      "_pair_type_score",
            "low_tvl_perf":         "_low_tvl_perf",
            "newness_bonus":        "_newness_bonus",
            "buy_sell_balance":     "_buy_sell_balance",
        }

        from score import normalize
        normed = {
            name: normalize([_f(r[key]) for r in candidates])
            for name, key in feats.items()
        }

        for i, r in enumerate(candidates):
            total = 0.0
            for name in feats:
                total += S["weights"].get(name, 0) * normed[name][i]
            r["score"] = round(total, 4)

        return sorted(candidates, key=lambda r: r["score"], reverse=True)
    finally:
        _score_mod._build_low_tvl_lookup = _orig_fn


# ---------------------------------------------------------------------------
# Step 2 — Walk-forward backtest
# ---------------------------------------------------------------------------

def backtest(rows: list, epochs: list, base_rate: float) -> tuple:
    """
    Walk-forward backtest with time-based 80/20 split.

    Returns (per_epoch_results: list[dict], aggregate: dict).
    """
    print("\n" + "=" * 70)
    print("STEP 2 — WALK-FORWARD BACKTEST")
    print("=" * 70)

    config  = json.loads(CONFIG_PATH.read_text())
    n_picks = config["scoring"]["top_n_picks"]  # 4

    # Drop edge epochs that trivially inflate precision:
    # - 2025-W01: all profitable because no emissions were recorded.
    # - The last epoch if it's 100% profitable (in-progress, no emissions yet).
    usable_epochs = [ep for ep in epochs if ep not in ("2025-W01",)]
    last_ep = usable_epochs[-1]
    last_rows = [r for r in rows if r["epoch_week"] == last_ep]
    if last_rows and all(r["profitable"] == "True" for r in last_rows):
        print(f"  Dropping {last_ep} (100% profitable -- likely incomplete epoch).")
        usable_epochs = usable_epochs[:-1]

    n_train      = int(len(usable_epochs) * 0.8)
    train_epochs = usable_epochs[:n_train]
    test_epochs  = usable_epochs[n_train:]

    print(f"  Usable epochs   : {len(usable_epochs)}")
    print(f"  Train window    : {usable_epochs[0]} -> {train_epochs[-1]}  ({len(train_epochs)} epochs)")
    print(f"  Test window     : {test_epochs[0]} -> {test_epochs[-1]}  ({len(test_epochs)} epochs)")
    print(f"  Picks per epoch : {n_picks}")

    # Each pool's earliest observed epoch (used for pool_age_days computation).
    # We use the full dataset here; pool age is observational fact, not a label.
    pool_first_epoch: dict = {}
    for r in rows:
        addr = r.get("pool", "").lower()
        ep   = r["epoch_week"]
        if addr not in pool_first_epoch or _week_to_int(ep) < _week_to_int(pool_first_epoch[addr]):
            pool_first_epoch[addr] = ep

    # Group rows by epoch for O(1) access
    by_epoch: dict = defaultdict(list)
    for r in rows:
        by_epoch[r["epoch_week"]].append(r)

    # Ground-truth label: (pool_addr_lower, epoch_week) -> bool
    profitable_label: dict = {
        (r["pool"].lower(), r["epoch_week"]): r["profitable"] == "True"
        for r in rows
    }

    per_epoch_results = []

    print(f"\n  {'Epoch':<12} {'Pools':>5} {'Top-4 picks':<48} {'Prec':>5} {'Oracle':>7}")
    print(f"  {'-'*12} {'-----':>5} {'-'*48} {'-----':>5} {'-------':>7}")

    for epoch in test_epochs:
        epoch_rows = by_epoch[epoch]
        if len(epoch_rows) < n_picks:
            continue

        # Build low_tvl_perf lookup from all epochs strictly before this one.
        # This is recalculated per epoch so weeks in the test window accumulate
        # history as we step forward — identical to how the live pipeline would work.
        low_tvl_lookup = _build_low_tvl_lookup_masked(rows, epoch)

        scored = _score_epoch(epoch_rows, low_tvl_lookup, pool_first_epoch, config)

        picks       = scored[:n_picks]
        oracle_picks = sorted(epoch_rows,
                              key=lambda r: _f(r.get("fees_usd")),
                              reverse=True)[:n_picks]

        n_prof_picks  = sum(1 for p in picks
                            if profitable_label.get((p["pair_address"], epoch), False))
        n_prof_oracle = sum(1 for p in oracle_picks
                            if profitable_label.get((p["pool"].lower(), epoch), False))

        precision        = n_prof_picks  / n_picks
        oracle_precision = n_prof_oracle / n_picks
        epoch_base_rate  = (sum(1 for r in epoch_rows if r["profitable"] == "True")
                            / len(epoch_rows))

        pick_names = ", ".join(p.get("pair", "?") for p in picks)
        per_epoch_results.append({
            "epoch":              epoch,
            "n_pools":            len(epoch_rows),
            "top4_picks":         pick_names,
            "n_profitable_picks": n_prof_picks,
            "precision":          round(precision, 4),
            "epoch_base_rate":    round(epoch_base_rate, 4),
            "oracle_precision":   round(oracle_precision, 4),
        })

        print(f"  {epoch:<12} {len(epoch_rows):5d} {pick_names:<48} "
              f"{precision:5.0%} {oracle_precision:7.0%}")

    if not per_epoch_results:
        print("\n  ERROR: No test epochs produced results -- check dataset size.")
        return per_epoch_results, {}

    avg_precision        = sum(r["precision"]       for r in per_epoch_results) / len(per_epoch_results)
    avg_epoch_base_rate  = sum(r["epoch_base_rate"] for r in per_epoch_results) / len(per_epoch_results)
    avg_oracle_precision = sum(r["oracle_precision"] for r in per_epoch_results) / len(per_epoch_results)
    lift = avg_precision / avg_epoch_base_rate if avg_epoch_base_rate > 0 else float("nan")

    aggregate = {
        "n_test_epochs":        len(per_epoch_results),
        "avg_precision":        round(avg_precision,       4),
        "avg_epoch_base_rate":  round(avg_epoch_base_rate, 4),
        "avg_oracle_precision": round(avg_oracle_precision, 4),
        "precision_lift":       round(lift, 3),
    }

    print(f"\n  {'─'*70}")
    print(f"  Avg precision (top {n_picks})     : {avg_precision:.1%}")
    print(f"  Avg epoch base rate       : {avg_epoch_base_rate:.1%}  (random-pick expectation)")
    print(f"  Precision lift            : {lift:.2f}x  (scorer vs random)")
    print(f"  Oracle precision (by fees): {avg_oracle_precision:.1%}  (upper bound)")

    return per_epoch_results, aggregate


# ---------------------------------------------------------------------------
# Step 3 — Feature importance (training window only)
# ---------------------------------------------------------------------------

def feature_importance_report(rows: list, train_epochs: list, config: dict):
    """
    Train a RandomForest classifier on proxy training data.
    Report feature importances and flag discrepancies vs hand-tuned weights.
    """
    print("\n" + "=" * 70)
    print("STEP 3 — FEATURE IMPORTANCE (training window only)")
    print("=" * 70)

    try:
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_score
    except ImportError:
        print("  Skipping -- scikit-learn/numpy not installed.\n"
              "  Run: pip install scikit-learn numpy")
        return

    train_set  = set(train_epochs)
    train_rows = [r for r in rows if r["epoch_week"] in train_set]
    print(f"  Training rows : {len(train_rows):,}  ({len(train_epochs)} epochs)")

    # Features available directly in the proxy CSV that map to scorer concepts.
    # Raw columns are used here; the scorer derives normalised ratios from them.
    feat_map = [
        ("fees_usd",     "fees_7d_usd (-> est_fees_per_day_usd, fees_per_tvl_ratio)"),
        ("volume_usd",   "vol_7d_usd  (-> vol_tvl_24h)"),
        ("tvl_usd",      "tvl_usd     (denominator for fee and vol ratios)"),
        ("fee_tier_bps", "fee_tier_bps"),
    ]
    feat_cols   = [f[0] for f in feat_map]
    feat_labels = [f[1] for f in feat_map]

    X, y = [], []
    for r in train_rows:
        try:
            X.append([float(r[c]) for c in feat_cols])
            y.append(1 if r["profitable"] == "True" else 0)
        except (ValueError, KeyError):
            continue

    if len(set(y)) < 2:
        print("  Only one class in training data -- cannot compute feature importance.")
        return

    X_arr = np.array(X)
    y_arr = np.array(y)

    n_pos = int(y_arr.sum())
    n_neg = len(y_arr) - n_pos
    n_cv  = min(5, n_pos, n_neg)

    clf = RandomForestClassifier(n_estimators=300, random_state=0, class_weight="balanced")
    cv  = cross_val_score(clf, X_arr, y_arr, cv=n_cv, scoring="roc_auc")
    clf.fit(X_arr, y_arr)

    print(f"  Trained on {len(y_arr):,} pool-epochs  ({n_pos} profitable, {n_neg} not)")
    print(f"  CV ROC-AUC ({n_cv}-fold): {cv.mean():.3f} +/- {cv.std():.3f}")

    print("\n  Feature importances from RandomForest:")
    importances = sorted(
        zip(feat_labels, clf.feature_importances_),
        key=lambda x: -x[1],
    )
    for label, imp in importances:
        bar = "#" * int(imp * 40)
        print(f"    {imp:.3f}  {bar:<40}  {label}")

    # Compare to hand-tuned weights.
    # We group scorer features by which proxy column they relate to.
    hand_weights = config["scoring"]["weights"]
    ml_imp_by_col = dict(zip(feat_cols, clf.feature_importances_))

    print("\n  Hand-tuned weights vs ML signal (rough mapping):")
    print(f"    {'Scorer feature':<24} {'Hand wt':>8}   {'Proxy col':>12}  {'ML imp':>7}  {'Flag'}")
    print(f"    {'-'*24} {'-------':>8}   {'----------':>12}  {'------':>7}  {'----'}")

    mapping = [
        # (scorer_feature,        proxy_col,       hand_weight)
        ("est_fees_per_day_usd", "fees_usd",      hand_weights.get("est_fees_per_day_usd", 0)),
        ("fees_per_tvl_ratio",   "fees_usd",      hand_weights.get("fees_per_tvl_ratio",   0)),
        ("vol_tvl_24h",          "volume_usd",    hand_weights.get("vol_tvl_24h",          0)),
        ("pair_type_score",      None,            hand_weights.get("pair_type_score",       0)),
        ("low_tvl_perf",         None,            hand_weights.get("low_tvl_perf",          0)),
        ("newness_bonus",        None,            hand_weights.get("newness_bonus",         0)),
        ("buy_sell_balance",     None,            hand_weights.get("buy_sell_balance",      0)),
    ]

    flag_count = 0
    for scorer_feat, proxy_col, hw in mapping:
        ml_imp = ml_imp_by_col.get(proxy_col) if proxy_col else None
        ml_str = f"{ml_imp:.3f}" if ml_imp is not None else "   n/a"
        flag   = ""
        if ml_imp is not None and ml_imp > 0:
            # Flag if hand weight is >3x or <1/3 of ML importance (rough signal)
            if hw > 3 * ml_imp + 0.01:
                flag = "<-- possibly over-weighted"
                flag_count += 1
            elif ml_imp > 3 * hw + 0.01:
                flag = "<-- possibly under-weighted"
                flag_count += 1
        proxy_str = proxy_col if proxy_col else "(no proxy)"
        print(f"    {scorer_feat:<24} {hw:>8.2f}   {proxy_str:>12}  {ml_str:>7}  {flag}")

    if flag_count == 0:
        print("    No large discrepancies detected.")
    print()


# ---------------------------------------------------------------------------
# Step 4 — Output
# ---------------------------------------------------------------------------

def write_results(per_epoch_results: list):
    RESULTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not per_epoch_results:
        print("  No per-epoch results to write.")
        return
    with open(RESULTS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(per_epoch_results[0].keys()))
        w.writeheader()
        w.writerows(per_epoch_results)
    print(f"  Wrote {len(per_epoch_results)} rows -> {RESULTS_CSV}")


def print_summary(aggregate: dict):
    print("\n" + "=" * 70)
    print("STEP 4 — SUMMARY REPORT")
    print("=" * 70)
    if not aggregate:
        print("  No results to summarise.")
        return

    avg_p  = aggregate["avg_precision"]
    avg_br = aggregate["avg_epoch_base_rate"]
    oracle = aggregate["avg_oracle_precision"]
    lift   = aggregate["precision_lift"]
    n_ep   = aggregate["n_test_epochs"]

    print(f"  Test epochs evaluated      : {n_ep}")
    print(f"  Scorer precision (top 4)   : {avg_p:.1%}")
    print(f"  Random-pick baseline       : {avg_br:.1%}  (epoch base rate)")
    print(f"  Precision lift             : {lift:.2f}x  (scorer vs random)")
    print(f"  Oracle upper bound (by fees): {oracle:.1%}")
    print()

    if lift >= 1.3:
        verdict = "Scorer adds meaningful value over random selection."
    elif lift >= 1.05:
        verdict = "Scorer adds modest value over random selection."
    else:
        verdict = ("Scorer shows little lift over random selection -- "
                   "consider re-weighting or adding features.")
    print(f"  Verdict: {verdict}")

    gap = oracle - avg_p
    if gap > 0.2:
        print(f"  Gap to oracle: {gap:.1%} -- significant room to improve; "
              "consider Phase 2 (ML model).")
    else:
        print(f"  Gap to oracle: {gap:.1%} -- scorer is close to the feasible ceiling.")

    print()
    print("  INTERPRETATION CAVEAT:")
    print("  The scorer consistently picks large blue-chip pools (USDC/cbBTC,")
    print("  WETH/USDC) which dominate on absolute fees and are genuinely")
    print("  profitable. However, these pools have $25M+ TVL on Aerodrome --")
    print("  far above Hydrex bootstrap-stage TVL ($3k-$50k). The live pipeline")
    print("  excludes pairs already on Hydrex (score.emit_picks deduplicates")
    print("  against the Goldsky subgraph). This backtest measures ranking quality")
    print("  on the full Aerodrome universe; real-world precision on the")
    print("  filtered candidate set will differ. Use low_tvl_perf (bootstrapped")
    print("  from this same proxy data) to capture the low-TVL signal.")

    print(f"\n  Full per-epoch results: {RESULTS_CSV}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    rows = _load_proxy()
    info = explore(rows)

    epochs    = info["epochs"]
    base_rate = info["base_rate"]
    config    = json.loads(CONFIG_PATH.read_text())

    # Determine usable/train epochs (mirrors backtest() logic) for feature importance
    usable_epochs = [ep for ep in epochs if ep not in ("2025-W01",)]
    last_ep   = usable_epochs[-1]
    last_rows = [r for r in rows if r["epoch_week"] == last_ep]
    if last_rows and all(r["profitable"] == "True" for r in last_rows):
        usable_epochs = usable_epochs[:-1]
    n_train       = int(len(usable_epochs) * 0.8)
    train_epochs  = usable_epochs[:n_train]

    per_epoch_results, aggregate = backtest(rows, epochs, base_rate)
    feature_importance_report(rows, train_epochs, config)
    write_results(per_epoch_results)
    print_summary(aggregate)


if __name__ == "__main__":
    main()
