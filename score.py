"""
score.py — rank candidates and emit the weekly picks.

Phase 1 (now): a TRANSPARENT weighted score. Each feature is min-max normalized
across today's filtered+enriched candidates, multiplied by the weights in
selection_config.json, and summed. Fully explainable — you can see exactly why a
pool ranked where it did. Outputs the top N as the suggested picks for the epoch.

Phase 2 (once data/aerodrome_proxy.csv has enough labeled rows): run
`python score.py --feature-importance` to train a model on the Aerodrome proxy
labels and print which features actually predict profitability. Use that to
re-weight phase 1 (or swap the weighted score for the model's probability).

Outputs data/weekly_picks.csv.
"""

import argparse
import csv
import datetime as dt
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG = json.loads((SCRIPT_DIR / "selection_config.json").read_text())
ENRICHED_CSV  = SCRIPT_DIR / "data" / "candidates_enriched.csv"
PROXY_CSV     = SCRIPT_DIR / "data" / "aerodrome_proxy.csv"
PICKS_CSV     = SCRIPT_DIR / "data" / "weekly_picks.csv"
PICKS_HTML    = SCRIPT_DIR / "picks.html"
RETENTION_CSV = SCRIPT_DIR / "data" / "retention_scorecard.csv"
BOOTSTRAP_JSON = SCRIPT_DIR / "bootstrap_picks.json"

S = CONFIG["scoring"]
SEED_TVL = S["planned_seed_tvl_usd"]


def _f(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def normalize(values: list) -> list:
    """Min-max to 0-1. Flat columns -> all 0.5 (no signal)."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.5] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _build_low_tvl_lookup() -> dict:
    """
    From aerodrome_proxy.csv, compute the average fees/emissions ratio for each
    pool address and token-pair when TVL was below the configured threshold.
    Returns {by_pool: {addr: score}, by_pair: {frozenset: score}, default: float}.
    Score is capped at 1.0 (= a 5× fees/emissions ratio maps to 1.0).
    """
    threshold = CONFIG["scoring"].get("low_tvl_threshold_usd", 25_000)
    if not PROXY_CSV.exists():
        return {"by_pool": {}, "by_pair": {}, "default": 0.5}

    from collections import defaultdict as _dd
    pool_ratios, pair_ratios = _dd(list), _dd(list)
    with open(PROXY_CSV) as f:
        for r in csv.DictReader(f):
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
    default = all_scores[len(all_scores) // 2] if all_scores else 2.5  # median
    return {"by_pool": by_pool, "by_pair": by_pair, "default": default}


def compute_features(rows: list) -> list:
    """Derive the raw feature values used by the weighted score."""
    major      = {t.upper() for t in S.get("major_tokens", [])}
    ltv_lookup = _build_low_tvl_lookup()

    for r in rows:
        # Priority: subgraph 7d > candidates_csv rolling avg > DexScreener 24h snapshot
        vol_7d  = _f(r.get("vol_7d_usd"))
        fees_7d = _f(r.get("fees_7d_usd"))
        tvl_7d  = _f(r.get("tvl_avg_7d_usd"))
        n_days  = max(_f(r.get("data_days"), 1.0), 1.0)

        csv_liq  = _f(r.get("csv_avg_liquidity_7d"))
        csv_vol  = _f(r.get("csv_avg_vol_7d"))

        if tvl_7d > 0:
            liq       = tvl_7d
            vol_daily = vol_7d / n_days
        elif csv_liq > 0:
            liq       = csv_liq
            vol_daily = csv_vol
        else:
            liq       = _f(r.get("liquidity_usd"))
            vol_daily = _f(r.get("vol_24h"))

        fees_daily = (fees_7d / n_days) if fees_7d > 0 else _f(r.get("est_fees_24h_usd"))

        r["_est_fees_per_day_usd"] = fees_daily
        r["_fees_per_tvl_ratio"]   = fees_daily / liq if liq > 0 else 0
        r["_vol_tvl_24h"]          = vol_daily  / liq if liq > 0 else 0

        # pair_type: blue-chip quality. 2 major tokens → 1.0, 1 → 0.7, 0 → 0.3.
        tokens  = [t.strip().upper() for t in r.get("pair", "/").split("/")][:2]
        n_major = sum(1 for t in tokens if t in major)
        r["_pair_type_score"] = [0.3, 0.7, 1.0][n_major]

        # low_tvl_perf: from Aerodrome proxy — how profitable was this pair/pool
        # historically when TVL was small (i.e. bootstrap-stage conditions).
        addr       = r.get("pair_address", "").lower()
        pair_key   = frozenset(tokens)
        raw_score  = (ltv_lookup["by_pool"].get(addr)
                      or ltv_lookup["by_pair"].get(pair_key)
                      or ltv_lookup["default"])
        r["_low_tvl_perf"] = min(raw_score / 5.0, 1.0)  # 5× ratio → 1.0

        # newness bonus: newer pools score higher, capped by config bucket.
        age = _f(r.get("pool_age_days"), 9999)
        cap = CONFIG["candidate_filters"]["max_pool_age_days_for_new_bucket"]
        r["_newness_bonus"] = max(0.0, 1 - age / cap) if cap > 0 else 0

        # buy/sell balance: closer to 1.0 (balanced two-way flow) is healthier.
        bs = _f(r.get("buy_sell_ratio_24h"), 0)
        r["_buy_sell_balance"] = 1 - min(abs(bs - 1), 1) if bs > 0 else 0
    return rows


def _apply_filters(rows: list) -> list:
    """Drop rows that fail runtime filters (distinct from candidate_pull.py pre-filters)."""
    max_vt = CONFIG["candidate_filters"].get("max_vol_tvl_24h")
    if not max_vt:
        return rows
    kept = []
    for r in rows:
        tvl_7d  = _f(r.get("tvl_avg_7d_usd"))
        csv_liq = _f(r.get("csv_avg_liquidity_7d"))
        liq = tvl_7d if tvl_7d > 0 else (csv_liq if csv_liq > 0 else _f(r.get("liquidity_usd")))
        n       = max(_f(r.get("data_days"), 1.0), 1.0)
        vol_7d  = _f(r.get("vol_7d_usd"))
        csv_vol = _f(r.get("csv_avg_vol_7d"))
        vol = (vol_7d / n) if vol_7d > 0 else (csv_vol if csv_vol > 0 else _f(r.get("vol_24h")))
        if liq > 0 and vol / liq > max_vt:
            continue
        kept.append(r)
    if len(kept) < len(rows):
        dropped = [r["pair"] for r in rows if r not in kept]
        print(f"  Filtered {len(rows) - len(kept)} pools with vol/TVL > {max_vt}×: {', '.join(dropped)}")
    return kept


def score(rows: list) -> list:
    rows = _apply_filters(rows)
    rows = compute_features(rows)
    feats = {
        "est_fees_per_day_usd": "_est_fees_per_day_usd",
        "fees_per_tvl_ratio":   "_fees_per_tvl_ratio",
        "vol_tvl_24h":          "_vol_tvl_24h",
        "pair_type_score":      "_pair_type_score",
        "low_tvl_perf":         "_low_tvl_perf",
        "newness_bonus":        "_newness_bonus",
        "buy_sell_balance":     "_buy_sell_balance",
    }
    normed = {name: normalize([_f(r[key]) for r in rows]) for name, key in feats.items()}

    for i, r in enumerate(rows):
        total, breakdown = 0.0, {}
        for name in feats:
            contrib = S["weights"].get(name, 0) * normed[name][i]
            breakdown[name] = round(contrib, 4)
            total += contrib
        r["score"] = round(total, 4)
        r["score_breakdown"] = json.dumps(breakdown)
    return sorted(rows, key=lambda r: r["score"], reverse=True)


def _existing_hydrex_pairs() -> set:
    """
    Query the Hydrex Goldsky subgraph for every pool that exists on-chain.
    Returns a set of frozensets of uppercase token symbols, e.g. {frozenset({'WETH','USDC'})}.
    Falls back to bootstrap_picks.json if the subgraph is unreachable.
    """
    endpoint = CONFIG.get("hydrex_subgraph", {}).get("endpoint")
    if endpoint:
        query = """{ pools(first: 1000, orderBy: totalValueLockedUSD, orderDirection: desc) {
            token0 { symbol } token1 { symbol }
        } }"""
        try:
            import requests as _req
            r = _req.post(endpoint, json={"query": query}, timeout=15)
            r.raise_for_status()
            body = r.json()
            if "data" in body:
                pairs = set()
                for p in body["data"]["pools"]:
                    tokens = frozenset({
                        p["token0"]["symbol"].upper().strip(),
                        p["token1"]["symbol"].upper().strip(),
                    })
                    pairs.add(tokens)
                print(f"  Hydrex subgraph: {len(pairs)} existing pools loaded")
                return pairs
        except Exception as e:
            print(f"  Warning: Hydrex subgraph unreachable ({e}), falling back to bootstrap_picks.json")

    # Fallback: bootstrap_picks.json
    if not BOOTSTRAP_JSON.exists():
        return set()
    data = json.loads(BOOTSTRAP_JSON.read_text())
    pairs = set()
    for week in data.get("weeks", []):
        for pool in week.get("pools", []):
            tokens = frozenset(t.upper().strip() for t in pool["pair"].split("/"))
            pairs.add(tokens)
    return pairs


def _failed_tokens() -> set:
    """Non-major tokens whose Hydrex pool went CUT/DEAD in the retention scorecard.

    These have burned us before (e.g. cbMEGA: ran 5 epochs, f/i never cleared 0.24).
    Mercenary liquidity follows the token, not the quote asset, so we exclude the
    token from picks regardless of which base it's now paired with. Still shown in
    the table, flagged — just never recommended.
    """
    if not RETENTION_CSV.exists():
        return set()
    majors = {t.upper() for t in S.get("major_tokens", [])}
    failed = set()
    with open(RETENTION_CSV, newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("recommendation") or "").upper() in ("CUT", "DEAD"):
                for t in (r.get("pair") or "").split("/"):
                    tu = t.strip().upper()
                    if tu and tu not in majors:
                        failed.add(tu)
    return failed


def emit_picks(ranked: list):
    existing = _existing_hydrex_pairs()
    n = S["top_n_picks"]
    min_pick = S.get("min_pick_score", 0.0)

    # Deduplicate by token pair. When the same pair exists on multiple DEXes,
    # prefer Aerodrome 7-day data (epoch-aligned, our ground-truth proxy) over
    # Uniswap 7d, then DexScreener 24h fallback; break ties by score.
    def _src_pri(r):
        src = r.get("seven_day_source", "")
        return 2 if src == "aerodrome" else (1 if src else 0)

    pair_best: dict = {}
    for r in ranked:
        tokens = frozenset(t.upper().strip() for t in r.get("pair", "/").split("/"))
        if tokens not in pair_best:
            pair_best[tokens] = r
        else:
            prev = pair_best[tokens]
            if (_src_pri(r), r["score"]) > (_src_pri(prev), prev["score"]):
                pair_best[tokens] = r
    ranked = sorted(pair_best.values(), key=lambda r: r["score"], reverse=True)

    excluded_tokens = {t.upper() for t in CONFIG["candidate_filters"].get("exclude_tokens", [])}
    failed_tokens = _failed_tokens()

    # Separate candidates into new (eligible) and already-on-Hydrex or excluded
    new_candidates, already_exists = [], []
    for r in ranked:
        tokens = frozenset(t.upper().strip() for t in r.get("pair", "/").split("/"))
        r["_prior_fail"] = bool(tokens & failed_tokens)
        if tokens & excluded_tokens:
            continue
        if tokens in existing:
            already_exists.append(r)
        else:
            new_candidates.append(r)

    # A PICK must be top-n by score, clear the quality floor, have a readable fee
    # tier, not be flagged for LP exit, and not be a token that failed before.
    picks, lp_exit_flagged, no_fee_tier = [], [], []
    for r in new_candidates:
        if not r.get("fee_tier_bps"):
            no_fee_tier.append(r)
        elif r.get("lp_exit_signal") in (True, "True"):
            lp_exit_flagged.append(r)
        elif r.get("_prior_fail"):
            continue  # burned us before — shown flagged, never recommended
        elif len(picks) < n and _f(r.get("score")) >= min_pick:
            picks.append(r)

    def _is_dynamic_snapshot(r) -> bool:
        """True when fee is a dynamic-fee snapshot with no subgraph to correct it."""
        return r.get("dynamic_fee") == "True" and not r.get("seven_day_source", "")

    def _note(r) -> str:
        parts = []
        if _is_dynamic_snapshot(r):
            parts.append("Dynamic fee — est. fees are a snapshot only")
        return "; ".join(parts)

    for r in picks:
        r["note"] = _note(r)

    cols = ["date", "pair", "pair_address", "dex", "lp_type", "score", "fee_tier_bps",
            "est_fees_24h_usd", "liquidity_usd", "market_cap", "pool_age_days",
            "vol_24h", "lp_exit_signal", "note", "score_breakdown"]
    PICKS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(PICKS_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(picks)

    def _fmt_row(r):
        src = r.get("seven_day_source", "")
        csv_days = int(_f(r.get("csv_days_seen"), 0))
        if src:
            data_window = f"7d/{src}"
        elif csv_days >= 2:
            data_window = f"{csv_days}d/candidates_csv"
        else:
            data_window = "24h/dexscreener"
        lp_type = r.get("lp_type") or {"globalState.lastFee": "CLAMM", "fee()": "CL/V3", "getReserves.v2": "V2"}.get(r.get("fee_read_method",""), "?")
        fee_tier = r.get("fee_tier_bps", "?")
        if fee_tier and fee_tier != "?" and _is_dynamic_snapshot(r):
            fee_tier = f"~{fee_tier}"
        return (f"  {r['score']:.3f}  {r['pair']:<20} [{r.get('dex','?')}  {lp_type}]  "
                f"tier={fee_tier}bps  est_fees_24h=${r.get('est_fees_24h_usd','?')}  "
                f"liq=${_f(r['liquidity_usd']):,.0f}  data={data_window}  age={r.get('pool_age_days','?')}d")

    print(f"\n=== Suggested {len(picks)} NEW pool(s) to create — {dt.date.today().isoformat()} "
          f"(top {n}, score ≥ {min_pick}, excl. prior fails) ===")
    for r in picks:
        print(_fmt_row(r))
        if _is_dynamic_snapshot(r):
            print(f"    ^ dynamic fee (Algebra) — est. fees are a snapshot only")

    if no_fee_tier:
        print(f"\n  ⚠ No fee tier (unreadable or non-EVM address — cannot assess fees):")
        for r in no_fee_tier:
            print(f"  {_fmt_row(r)}")
            print(f"    ^ fee_read_method={r.get('fee_read_method','')} — likely V4 pool or non-EVM chain")
            if _is_dynamic_snapshot(r):
                print(f"    ^ dynamic fee (Algebra) — est. fees are a snapshot only")

    if lp_exit_flagged:
        print(f"\n  ⚠ LP exit signal (ranked but excluded from picks):")
        for r in lp_exit_flagged:
            avg_liq = _f(r.get("csv_avg_liquidity_7d"))
            cur_liq = _f(r.get("liquidity_usd"))
            drop_pct = int((1 - cur_liq / avg_liq) * 100) if avg_liq > 0 else 0
            print(f"  {_fmt_row(r)}")
            print(f"    ^ liquidity down {drop_pct}% vs 7d avg (${avg_liq:,.0f} → ${cur_liq:,.0f})")
            if _is_dynamic_snapshot(r):
                print(f"    ^ dynamic fee (Algebra) — est. fees are a snapshot only")

    if already_exists:
        print(f"\n  (skipped {len(already_exists)} pairs already on Hydrex: "
              f"{', '.join(r['pair'] for r in already_exists[:5])}"
              f"{'...' if len(already_exists) > 5 else ''})")
    print(f"\nWrote {PICKS_CSV}")

    render_picks_html(picks, new_candidates, len(already_exists))
    print(f"Wrote {PICKS_HTML}")


def render_picks_html(picks: list, ranked_new: list, already_count: int):
    """Write picks.html — the selection dashboard ranking new-pool candidates."""
    pick_addrs = {r.get("pair_address") for r in picks}
    scan_date = next((r.get("date") for r in (picks + ranked_new) if r.get("date")),
                     dt.date.today().isoformat())
    min_pick = S.get("min_pick_score", 0.0)

    def status(r):
        if r.get("_prior_fail"):
            return "#f85149", "prior-fail"
        if r.get("pair_address") in pick_addrs:
            return "#3fb950", "PICK"
        if not r.get("fee_tier_bps"):
            return "#bc8cff", "no tier"
        if r.get("lp_exit_signal") in (True, "True"):
            return "#f85149", "LP exit"
        return "#8b949e", "candidate"

    trs = []
    for i, r in enumerate(ranked_new, 1):
        col, label = status(r)
        hl = ' style="border-left:3px solid #3fb950"' if r.get("pair_address") in pick_addrs else ""
        fee_tvl = _f(r.get("_fees_per_tvl_ratio")) * 100  # daily fees / liquidity, as %
        tier = r.get("fee_tier_bps") or "–"
        age = r.get("pool_age_days") or "–"
        trs.append(
            f"<tr{hl}><td class=num>{i}</td>"
            f"<td><b>{r.get('pair','?')}</b></td>"
            f"<td>{r.get('dex','?')}</td>"
            f"<td class=num><b>{_f(r.get('score')):.3f}</b></td>"
            f"<td class=num>{fee_tvl:.2f}%</td>"
            f"<td class=num>${_f(r.get('est_fees_24h_usd')):,.0f}</td>"
            f"<td class=num>${_f(r.get('liquidity_usd')):,.0f}</td>"
            f"<td class=num>{tier}{'bps' if tier != '–' else ''}</td>"
            f"<td class=num>{age}</td>"
            f"<td><span class=badge style=\"background:{col}\">{label}</span></td></tr>"
        )

    html = f"""<!DOCTYPE html><html lang=en><head><meta charset=UTF-8>
<title>Hydrex — Selection (New Pool Candidates)</title><style>
:root{{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}}
h1{{margin:0 0 4px;font-size:20px}} .sub{{color:var(--muted);font-size:13px;margin-bottom:18px}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden}}
th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid var(--border);font-size:13px}}
th{{background:rgba(255,255,255,.03);color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.5px}}
td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}} tr:last-child td{{border-bottom:none}}
.badge{{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700;color:#0d1117}}
a.nav{{color:var(--accent);text-decoration:none;padding:6px 12px;border:1px solid var(--border);border-radius:6px;margin-right:8px}}
a.nav.active{{background:var(--accent);color:var(--bg);border-color:var(--accent)}}
.foot{{margin-top:18px;color:var(--muted);font-size:11px}}
</style></head><body>
<h1>Hydrex — Selection · New Pool Candidates</h1>
<div class=sub>Scanned {scan_date} · <b style="color:#3fb950">{len(picks)} PICK(s)</b> = top {S['top_n_picks']} above score {min_pick}, excluding tokens that went CUT/DEAD on Hydrex (<b style="color:#f85149">prior-fail</b>) · {already_count} pairs already on Hydrex (hidden).</div>
<div style="margin-bottom:18px">
  <a class=nav href="index.html">Aero vs Hydrex</a>
  <a class=nav href="live.html">Live</a>
  <a class=nav active href="picks.html">Selection</a>
  <a class=nav href="retention.html">Retention</a>
  <a class=nav href="bootstrap.html">Bootstrap</a>
</div>
<table><thead><tr>
  <th class=num>#</th><th>Pair</th><th>DEX</th><th class=num>Score</th><th class=num>Fee/TVL/day</th>
  <th class=num>Est fees 24h</th><th class=num>Liquidity</th><th class=num>Fee tier</th><th class=num>Age (d)</th><th>Status</th>
</tr></thead><tbody>{''.join(trs)}</tbody></table>
<div class=foot>Score = transparent weighted blend (selection_config.json → scoring.weights). Fee/TVL/day = est. daily fees ÷ liquidity. Pre-Hydrex pools are external (Aerodrome/Uniswap) — bootstrap target is the Hydrex pool.</div>
</body></html>"""
    PICKS_HTML.write_text(html)


def feature_importance():
    """Phase 2: learn which features predict profitability from the Aero proxy labels."""
    if not PROXY_CSV.exists():
        raise SystemExit(f"{PROXY_CSV} not found — run proxy_dataset.py first.")
    try:
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import cross_val_score
    except ImportError:
        raise SystemExit("pip install scikit-learn numpy  (only needed for --feature-importance)")

    with open(PROXY_CSV, newline="") as f:
        rows = list(csv.DictReader(f))
    feat_cols = ["volume_usd", "fees_usd", "fee_tier_bps", "tvl_usd"]
    feat_cols = [c for c in feat_cols if c in rows[0]]
    X, y = [], []
    for r in rows:
        try:
            X.append([float(r[c]) for c in feat_cols])
            y.append(1 if str(r["profitable"]).lower() in ("true", "1") else 0)
        except (ValueError, KeyError):
            continue
    if len(set(y)) < 2:
        raise SystemExit("Need both profitable and unprofitable examples to learn from.")

    X, y = np.array(X), np.array(y)
    clf = RandomForestClassifier(n_estimators=300, random_state=0, class_weight="balanced")
    cv = cross_val_score(clf, X, y, cv=min(5, sum(y), len(y) - sum(y)), scoring="roc_auc")
    clf.fit(X, y)
    print(f"Trained on {len(y)} pool-epochs ({sum(y)} profitable). CV ROC-AUC: {cv.mean():.3f}")
    print("\nFeature importance (higher = more predictive of profitability):")
    for name, imp in sorted(zip(feat_cols, clf.feature_importances_), key=lambda x: -x[1]):
        print(f"  {name:<16} {imp:.3f}")
    print("\nUse this to re-weight selection_config.json -> scoring.weights.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--feature-importance", action="store_true",
                    help="Phase 2: train on Aerodrome proxy labels and print importances")
    args = ap.parse_args()

    if args.feature_importance:
        feature_importance()
        return

    if not ENRICHED_CSV.exists():
        raise SystemExit(f"{ENRICHED_CSV} not found — run candidate_pull.py then fee_enrich.py.")
    today = dt.date.today().isoformat()
    with open(ENRICHED_CSV, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("date") == today]
    if not rows:
        raise SystemExit(f"No enriched candidates for {today}.")
    emit_picks(score(rows))


if __name__ == "__main__":
    main()
