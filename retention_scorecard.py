"""
retention_scorecard.py — which incentivized pools to KEEP, WATCH, or CUT.

Reads data/bootstrap_tracker.csv (one row per pool-epoch), aggregates every
epoch a pool has been incentivized into a single per-pool row, and emits a
data-driven re-incentivize recommendation.

Motivation (Austin, Jun 2026): "the pools that did well are ones we already
run, but they've dropped off in TVL since we started." Eyeballing this week's
fee/TVL misses two things — whether a pool *retains* the TVL it sourced, and
whether it does so *consistently* across epochs (not a one-week wonder). This
scorecard scores both.

Composite `retention_score` (0-1) blends five tunable components:
  - efficiency   : recency-weighted $Fees/$Incentive — does it pay for itself
  - productivity : $Fees/$TVL  — is the sourced TVL actually earning (Austin's
                   new ranking metric as of Jun 2026)
  - stickiness   : how much of peak TVL remains + within-epoch TVL hold
  - consistency  : share of incentivized epochs that cleared break-even (f/i>=1)
  - trend        : is f/i improving or decaying across epochs

Recommendation = KEEP / WATCH / CUT, plus the special states NEW (too little
history), DEAD (TVL gone), TREASURY (config-excluded, e.g. VVV/USDC which has a
treasury allocation and shouldn't compete for incentive budget).

Outputs:
  - data/retention_scorecard.csv  (one row per pool, sorted by score desc)
  - retention.html                (dashboard styled like bootstrap.html)
  - a colour-coded ranked table to the console

All thresholds and weights live in selection_config.json -> retention_scorecard.

Usage:
  python retention_scorecard.py [--no-color] [--no-html]
"""

import argparse
import csv
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TRACKER_CSV = SCRIPT_DIR / "data" / "bootstrap_tracker.csv"
SCORECARD_CSV = SCRIPT_DIR / "data" / "retention_scorecard.csv"
DASHBOARD_HTML = SCRIPT_DIR / "retention.html"
CONFIG_FILE = SCRIPT_DIR / "selection_config.json"

# Defaults used when selection_config.json has no retention_scorecard block.
DEFAULTS = {
    "min_incentive_usd": 1.0,
    "_min_incentive_note": "Epochs with incentive below this are excluded from f/i stats (e.g. unfunded/stub rows).",
    "profit_threshold": 1.0,
    "_profit_threshold_note": "f/i at or above this counts as a 'profitable' (break-even) epoch.",
    "dead_tvl_usd": 50.0,
    "_dead_tvl_note": "If the latest epoch's avg TVL is below this, the pool is DEAD.",
    "min_epochs_for_history": 2,
    "_min_epochs_note": "Pools with fewer incentivized epochs than this are flagged NEW (insufficient track record).",
    "trend_epsilon": 0.10,
    "_trend_epsilon_note": "Change in f/i (latest minus first) larger than this is 'up'/'down', else 'flat'.",
    "normalization_caps": {
        "fees_per_incentive": 1.5,
        "_fees_per_incentive_note": "f/i at/above this maps to a perfect efficiency sub-score.",
        "fees_tvl_pct": 5.0,
        "_fees_tvl_pct_note": "Weekly $Fees/$TVL (in %) at/above this maps to a perfect productivity sub-score."
    },
    "weights": {
        "efficiency": 0.35,
        "productivity": 0.25,
        "stickiness": 0.20,
        "consistency": 0.15,
        "trend": 0.05
    },
    "thresholds": {
        "keep": 0.55,
        "_keep_note": "retention_score at/above this -> KEEP (also KEEP if latest f/i >= profit_threshold).",
        "cut": 0.25,
        "_cut_note": "retention_score at/below this -> CUT (or zero profitable epochs with collapsed TVL)."
    },
    "cut_tvl_retention": 0.30,
    "_cut_tvl_retention_note": "Below this share of peak TVL, a never-profitable pool is a CUT regardless of score.",
    "treasury_exclude": ["USDC/VVV", "VVV/USDC"],
    "_treasury_exclude_note": "Pairs (or pool addresses) flagged TREASURY: scored, but excluded from the incentive-budget call. VVV has a treasury allocation (Austin, Jun 2026)."
}


def deep_merge(base: dict, override: dict) -> dict:
    """Return base with override applied recursively (override wins)."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = {}
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text()).get("retention_scorecard", {})
    return deep_merge(DEFAULTS, cfg)


def fnum(row: dict, key: str, default=0.0) -> float:
    """Parse a CSV cell as float, tolerating '', None, and stray whitespace."""
    v = row.get(key)
    if v is None or str(v).strip() == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def load_rows() -> list:
    if not TRACKER_CSV.exists():
        raise SystemExit(f"No tracker at {TRACKER_CSV}. Run weekly_bootstrap_update.py first.")
    with open(TRACKER_CSV, newline="") as f:
        return list(csv.DictReader(f))


def aggregate_pool(addr: str, epochs: list, cfg: dict, global_max_epoch: int) -> dict:
    """Collapse a pool's epoch rows (sorted ascending) into one scorecard row."""
    min_inc = cfg["min_incentive_usd"]
    profit = cfg["profit_threshold"]
    caps = cfg["normalization_caps"]

    pair = epochs[-1].get("pair") or addr
    epoch_nums = [int(fnum(r, "hydrex_epoch")) for r in epochs]
    latest_epoch = epoch_nums[-1]

    # --- TVL trajectory across all present epochs ---
    tvl_avgs = [fnum(r, "tvl_avg_usd") for r in epochs]
    tvl_peak = max(tvl_avgs) if tvl_avgs else 0.0
    tvl_avg_latest = tvl_avgs[-1]
    tvl_retention = (tvl_avg_latest / tvl_peak) if tvl_peak > 0 else 0.0

    end_start = [
        fnum(r, "tvl_end_usd") / fnum(r, "tvl_start_usd")
        for r in epochs if fnum(r, "tvl_start_usd") > 0
    ]
    tvl_end_start_mean = sum(end_start) / len(end_start) if end_start else 0.0

    # --- fee productivity ($Fees/$TVL, stored as % in the tracker) ---
    # Recency-weighted: a pool's *current* earning power should drive the re-up
    # call, not a lifetime mean that one historically great epoch can inflate.
    fee_tvl_vals = [fnum(r, "fees_tvl_pct") for r in epochs if fnum(r, "tvl_avg_usd") > 0]
    fees_tvl_pct_mean = sum(fee_tvl_vals) / len(fee_tvl_vals) if fee_tvl_vals else 0.0
    fees_tvl_pct_latest = fnum(epochs[-1], "fees_tvl_pct")
    if fee_tvl_vals:
        ft_w = list(range(1, len(fee_tvl_vals) + 1))
        fees_tvl_pct_wmean = sum(w * v for w, v in zip(ft_w, fee_tvl_vals)) / sum(ft_w)
    else:
        fees_tvl_pct_wmean = 0.0

    # --- efficiency ($Fees/$Incentive), only epochs we actually funded ---
    funded = [r for r in epochs if fnum(r, "incentives_usd") >= min_inc]
    fi_series = [fnum(r, "fees_usd") / fnum(r, "incentives_usd") for r in funded]
    epochs_incentivized = len(funded)

    if epochs_incentivized:
        # recency-weighted mean: most recent funded epoch carries the most weight
        weights = list(range(1, epochs_incentivized + 1))
        fi_wmean = sum(w * fi for w, fi in zip(weights, fi_series)) / sum(weights)
        fi_latest = fi_series[-1]
        profitable_epochs = sum(1 for fi in fi_series if fi >= profit)
        profitable_ratio = profitable_epochs / epochs_incentivized
        trend_delta = fi_series[-1] - fi_series[0] if epochs_incentivized >= 2 else 0.0
    else:
        fi_wmean = fi_latest = trend_delta = 0.0
        profitable_epochs = 0
        profitable_ratio = 0.0

    eps = cfg["trend_epsilon"]
    if epochs_incentivized < 2:
        trend = "n/a"
    elif trend_delta > eps:
        trend = "up"
    elif trend_delta < -eps:
        trend = "down"
    else:
        trend = "flat"

    # --- lifetime P&L ---
    total_fees = sum(fnum(r, "fees_usd") for r in epochs)
    total_incentive = sum(fnum(r, "incentives_usd") for r in epochs)
    net_usd = total_fees - total_incentive
    lifetime_roi = (total_fees / total_incentive) if total_incentive > 0 else 0.0

    # --- normalized sub-scores (0-1) ---
    efficiency_n = clamp(fi_wmean / caps["fees_per_incentive"]) if caps["fees_per_incentive"] else 0.0
    productivity_n = clamp(fees_tvl_pct_wmean / caps["fees_tvl_pct"]) if caps["fees_tvl_pct"] else 0.0
    stickiness_n = clamp(0.5 * tvl_retention + 0.5 * min(tvl_end_start_mean, 1.0))
    consistency_n = clamp(profitable_ratio)
    # trend centered at 0.5: improving > 0.5, decaying < 0.5
    trend_n = clamp(0.5 + trend_delta / (2 * caps["fees_per_incentive"])) if caps["fees_per_incentive"] else 0.5

    w = cfg["weights"]
    wsum = sum(w.values()) or 1.0
    retention_score = (
        w["efficiency"] * efficiency_n
        + w["productivity"] * productivity_n
        + w["stickiness"] * stickiness_n
        + w["consistency"] * consistency_n
        + w["trend"] * trend_n
    ) / wsum

    rec, reason = recommend(
        cfg, pair, addr, retention_score, fi_latest, fi_wmean, epochs_incentivized,
        tvl_avg_latest, tvl_retention, profitable_ratio, trend,
        latest_epoch, global_max_epoch,
    )

    return {
        "pair": pair,
        "pool_address": addr,
        "epochs_incentivized": epochs_incentivized,
        "epoch_range": f"{epoch_nums[0]}-{epoch_nums[-1]}" if epoch_nums else "",
        "latest_epoch": latest_epoch,
        "tvl_avg_latest": round(tvl_avg_latest, 2),
        "tvl_peak": round(tvl_peak, 2),
        "tvl_retention": round(tvl_retention, 4),
        "tvl_end_start_mean": round(tvl_end_start_mean, 4),
        "fees_tvl_pct_latest": round(fees_tvl_pct_latest, 4),
        "fees_tvl_pct_mean": round(fees_tvl_pct_mean, 4),
        "fees_tvl_pct_wmean": round(fees_tvl_pct_wmean, 4),
        "fees_per_incentive_latest": round(fi_latest, 4),
        "fees_per_incentive_wmean": round(fi_wmean, 4),
        "profitable_epochs": profitable_epochs,
        "profitable_ratio": round(profitable_ratio, 4),
        "trend": trend,
        "trend_delta": round(trend_delta, 4),
        "total_fees_usd": round(total_fees, 2),
        "total_incentive_usd": round(total_incentive, 2),
        "net_usd": round(net_usd, 2),
        "lifetime_roi": round(lifetime_roi, 4),
        "retention_score": round(retention_score, 4),
        "recommendation": rec,
        "reason": reason,
    }


def recommend(cfg, pair, addr, score, fi_latest, fi_wmean, n_inc, tvl_latest, tvl_ret,
              profitable_ratio, trend, latest_epoch, global_max_epoch):
    """Map metrics to a KEEP/WATCH/CUT/NEW/DEAD/TREASURY call + one-line reason."""
    th = cfg["thresholds"]
    profit = cfg["profit_threshold"]
    excl = {str(x).lower() for x in cfg.get("treasury_exclude", [])}
    dropped = latest_epoch < global_max_epoch  # not run in the most recent epoch

    # Treasury pools are scored but don't compete for the incentive budget.
    if pair.lower() in excl or addr.lower() in excl:
        return "TREASURY", "Treasury allocation — excluded from incentive-budget ranking"

    if tvl_latest < cfg["dead_tvl_usd"]:
        return "DEAD", f"TVL collapsed to ${tvl_latest:,.0f} — liquidity gone"

    if n_inc < cfg["min_epochs_for_history"]:
        return "NEW", f"Only {n_inc} funded epoch — insufficient history, watch next epoch"

    # A pool in clear decline (declining trend AND recency-weighted f/i below
    # break-even) must not be rescued to KEEP by a blended score that an old
    # great epoch still inflates. AORA (1.88 -> 0.04) is the case this catches;
    # using fi_wmean (not fi_latest) spares a strong pool like LFI a soft week.
    decaying = trend == "down" and fi_wmean < profit

    # Strong, current signal -> keep funding it.
    if fi_latest >= profit:
        tag = " (last run)" if dropped else ""
        return "KEEP", f"Latest f/i {fi_latest:.2f} ≥ break-even{tag}"
    if score >= th["keep"] and not decaying:
        return "KEEP", f"Score {score:.2f} ≥ keep threshold — efficient & sticky"

    # Clear failure -> stop funding it.
    if profitable_ratio == 0 and tvl_ret < cfg["cut_tvl_retention"]:
        return "CUT", f"Never broke even and TVL down to {tvl_ret:.0%} of peak"
    if score <= th["cut"]:
        return "CUT", f"Score {score:.2f} ≤ cut threshold — poor ROI/retention"

    # Everything else needs a human eye.
    direction = {"up": "improving", "down": "decaying", "flat": "flat"}.get(trend, "mixed")
    return "WATCH", f"Score {score:.2f}, trend {direction} — re-up only if budget allows"


def build_scorecard(rows: list, cfg: dict) -> list:
    by_pool = {}
    for r in rows:
        addr = (r.get("pool_address") or "").lower()
        if not addr:
            continue
        by_pool.setdefault(addr, []).append(r)

    global_max_epoch = max((int(fnum(r, "hydrex_epoch")) for r in rows), default=0)

    cards = []
    for addr, epochs in by_pool.items():
        epochs.sort(key=lambda r: int(fnum(r, "hydrex_epoch")))
        cards.append(aggregate_pool(addr, epochs, cfg, global_max_epoch))

    cards.sort(key=lambda c: c["retention_score"], reverse=True)
    return cards


def write_csv(cards: list):
    if not cards:
        return
    with open(SCORECARD_CSV, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(cards[0].keys()))
        writer.writeheader()
        writer.writerows(cards)


# --- console rendering -------------------------------------------------------

ANSI = {
    "KEEP": "\033[32m", "WATCH": "\033[33m", "CUT": "\033[31m",
    "NEW": "\033[36m", "DEAD": "\033[35m", "TREASURY": "\033[34m",
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
}


def print_table(cards: list, use_color: bool):
    def c(text, key):
        return f"{ANSI.get(key, '')}{text}{ANSI['reset']}" if use_color else text

    hdr = f"{'PAIR':16} {'EPS':>3} {'FEE/TVL':>8} {'F/I lt':>7} {'F/I wm':>7} {'TVLret':>7} {'TREND':>6} {'SCORE':>6}  REC"
    print()
    print(c(hdr, "bold") if use_color else hdr)
    print("-" * len(hdr))
    for r in cards:
        line = (
            f"{r['pair'][:16]:16} "
            f"{r['epochs_incentivized']:>3} "
            f"{r['fees_tvl_pct_mean']:>7.2f}% "
            f"{r['fees_per_incentive_latest']:>7.2f} "
            f"{r['fees_per_incentive_wmean']:>7.2f} "
            f"{r['tvl_retention']*100:>6.0f}% "
            f"{r['trend']:>6} "
            f"{r['retention_score']:>6.2f}  "
            f"{c(r['recommendation'], r['recommendation'])}"
        )
        print(line)

    # P&L summary
    counts = {}
    for r in cards:
        counts[r["recommendation"]] = counts.get(r["recommendation"], 0) + 1
    net = sum(r["net_usd"] for r in cards)
    summary = "  ".join(f"{c(k, k)}:{v}" for k, v in sorted(counts.items()))
    print("-" * len(hdr))
    print(f"{summary}    lifetime net (fees − incentive): ${net:,.0f}")
    print()


# --- HTML dashboard ----------------------------------------------------------

REC_COLOR = {
    "KEEP": "#3fb950", "WATCH": "#d29922", "CUT": "#f85149",
    "NEW": "#39d4cf", "DEAD": "#bc8cff", "TREASURY": "#58a6ff",
}


def render_dashboard(cards: list, cfg: dict):
    data_json = json.dumps(cards)
    rec_color_json = json.dumps(REC_COLOR)
    keep_th = cfg["thresholds"]["keep"]
    cut_th = cfg["thresholds"]["cut"]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Hydrex Retention Scorecard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{ --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --green:#3fb950; --red:#f85149; --orange:#d29922; }}
  body {{ margin:0; padding:24px; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  h1 {{ margin:0 0 6px; font-size:20px; }}
  .subtitle {{ color:var(--muted); margin-bottom:18px; font-size:13px; }}
  .summary {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:24px; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; }}
  .card-label {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px; }}
  .card-value {{ font-size:22px; font-weight:600; }}
  .card-sub {{ color:var(--muted); font-size:12px; margin-top:4px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--border); border-radius:10px; overflow:hidden; }}
  th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid var(--border); font-size:13px; }}
  th {{ background:rgba(255,255,255,0.03); color:var(--muted); font-weight:600; text-transform:uppercase; font-size:11px; letter-spacing:0.5px; cursor:pointer; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  tr:last-child td {{ border-bottom:none; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px; font-weight:700; color:#0d1117; }}
  .scorebar-wrap {{ background:var(--border); border-radius:4px; height:8px; width:80px; display:inline-block; vertical-align:middle; overflow:hidden; }}
  .scorebar {{ height:100%; border-radius:4px; }}
  .reason {{ color:var(--muted); font-size:11px; }}
  .pos {{ color:var(--green); }} .neg {{ color:var(--red); }}
  .footer {{ margin-top:24px; color:var(--muted); font-size:11px; text-align:center; }}
  .chart-panel {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:24px; }}
  .chart-panel h3 {{ margin:0 0 8px; font-size:13px; font-weight:600; }}
  .chart-wrap {{ position:relative; height:360px; }}
  a.nav {{ color:var(--accent); text-decoration:none; padding:6px 12px; border:1px solid var(--border); border-radius:6px; margin-right:8px; }}
  a.nav.active {{ background:var(--accent); color:var(--bg); border-color:var(--accent); }}
</style>
</head>
<body>
<h1>Hydrex Retention Scorecard</h1>
<div class="subtitle">Which incentivized pools to keep funding. Aggregated across every epoch each pool ran. Score blends efficiency, fee productivity, TVL stickiness, consistency, and trend. KEEP ≥ {keep_th} · CUT ≤ {cut_th}.</div>

<div style="margin-bottom:18px">
  <a class="nav" href="index.html">Aero vs Hydrex</a>
  <a class="nav" href="bootstrap.html">Bootstrap Tracker</a>
  <a class="nav active" href="retention.html">Retention Scorecard</a>
</div>

<div id="summary" class="summary"></div>

<div class="chart-panel">
  <h3>Retention score by pool</h3>
  <div class="chart-wrap"><canvas id="chart-score"></canvas></div>
</div>

<table id="scorecard">
  <thead>
    <tr>
      <th data-k="pair">Pair</th>
      <th data-k="recommendation">Call</th>
      <th class="num" data-k="retention_score">Score</th>
      <th class="num" data-k="epochs_incentivized">Eps</th>
      <th class="num" data-k="fees_tvl_pct_mean">$Fee/$TVL</th>
      <th class="num" data-k="fees_per_incentive_latest">f/i (latest)</th>
      <th class="num" data-k="fees_per_incentive_wmean">f/i (wmean)</th>
      <th class="num" data-k="tvl_retention">TVL kept</th>
      <th class="num" data-k="trend">Trend</th>
      <th class="num" data-k="net_usd">Net P&amp;L</th>
      <th data-k="reason">Why</th>
    </tr>
  </thead>
  <tbody id="scorecard-body"></tbody>
</table>

<div class="footer">Generated by retention_scorecard.py · <a href="data/retention_scorecard.csv" download style="color:var(--accent)">↓ Download CSV</a></div>

<script>
const CARDS = {data_json};
const REC_COLOR = {rec_color_json};

function pctFmt(n) {{ return (Number(n)||0).toFixed(2) + '%'; }}
function num(n, d=2) {{ return (Number(n)||0).toFixed(d); }}
function usd(n) {{
  n = Number(n)||0;
  const s = Math.abs(n) >= 1000 ? '$' + (n/1000).toFixed(1) + 'K' : '$' + n.toFixed(0);
  return n < 0 ? '-' + s.replace('-','') : s;
}}

function renderSummary() {{
  const counts = {{}};
  CARDS.forEach(c => counts[c.recommendation] = (counts[c.recommendation]||0)+1);
  const net = CARDS.reduce((s,c) => s + (Number(c.net_usd)||0), 0);
  const keep = counts['KEEP']||0, cut = counts['CUT']||0;
  const profitable = CARDS.filter(c => Number(c.fees_per_incentive_latest) >= 1).length;
  document.getElementById('summary').innerHTML = `
    <div class="card"><div class="card-label">Pools scored</div><div class="card-value">${{CARDS.length}}</div><div class="card-sub">across all tracked epochs</div></div>
    <div class="card"><div class="card-label">Keep / Cut</div><div class="card-value"><span style="color:var(--green)">${{keep}}</span> / <span style="color:var(--red)">${{cut}}</span></div><div class="card-sub">recommended actions</div></div>
    <div class="card"><div class="card-label">Profitable last run</div><div class="card-value">${{profitable}}</div><div class="card-sub">latest f/i ≥ 1.0</div></div>
    <div class="card"><div class="card-label">Lifetime net</div><div class="card-value" style="color:${{net>=0?'var(--green)':'var(--red)'}}">${{usd(net)}}</div><div class="card-sub">total fees − incentive</div></div>
  `;
}}

let sortKey = 'retention_score', sortDir = -1;
function renderTable() {{
  const rows = [...CARDS].sort((a,b) => {{
    let x = a[sortKey], y = b[sortKey];
    if (typeof x === 'number' || !isNaN(Number(x)) && x !== '') {{ x = Number(x); y = Number(y); }}
    return x < y ? sortDir : x > y ? -sortDir : 0;
  }});
  document.getElementById('scorecard-body').innerHTML = rows.map(c => {{
    const col = REC_COLOR[c.recommendation] || '#8b949e';
    const score = Number(c.retention_score);
    const net = Number(c.net_usd);
    return `<tr>
      <td><strong>${{c.pair}}</strong> <span class="reason">ep ${{c.epoch_range}}</span></td>
      <td><span class="badge" style="background:${{col}}">${{c.recommendation}}</span></td>
      <td class="num"><span class="scorebar-wrap"><span class="scorebar" style="width:${{Math.round(score*100)}}%;background:${{col}}"></span></span> ${{num(score)}}</td>
      <td class="num">${{c.epochs_incentivized}}</td>
      <td class="num">${{pctFmt(c.fees_tvl_pct_mean)}}</td>
      <td class="num">${{num(c.fees_per_incentive_latest)}}</td>
      <td class="num">${{num(c.fees_per_incentive_wmean)}}</td>
      <td class="num">${{Math.round(Number(c.tvl_retention)*100)}}%</td>
      <td class="num">${{c.trend}}</td>
      <td class="num ${{net>=0?'pos':'neg'}}">${{usd(net)}}</td>
      <td class="reason">${{c.reason}}</td>
    </tr>`;
  }}).join('');
}}

document.querySelectorAll('th[data-k]').forEach(th => th.addEventListener('click', () => {{
  const k = th.dataset.k;
  if (k === sortKey) sortDir = -sortDir; else {{ sortKey = k; sortDir = -1; }}
  renderTable();
}}));

function renderChart() {{
  const rows = [...CARDS].sort((a,b) => Number(b.retention_score) - Number(a.retention_score));
  new Chart(document.getElementById('chart-score'), {{
    type: 'bar',
    data: {{
      labels: rows.map(c => c.pair),
      datasets: [{{
        data: rows.map(c => Number(c.retention_score)),
        backgroundColor: rows.map(c => (REC_COLOR[c.recommendation]||'#8b949e') + 'cc'),
        borderColor: rows.map(c => REC_COLOR[c.recommendation]||'#8b949e'),
        borderWidth: 1,
      }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ display:false }},
        tooltip: {{ callbacks: {{ label: ctx => {{
          const c = rows[ctx.dataIndex];
          return [`Score: ${{num(c.retention_score)}}  (${{c.recommendation}})`, c.reason];
        }} }} }}
      }},
      scales: {{
        x: {{ min:0, max:1, ticks:{{ color:'#8b949e' }}, grid:{{ color:'#30363d' }} }},
        y: {{ ticks:{{ color:'#e6edf3', font:{{ size:11 }} }}, grid:{{ display:false }} }}
      }}
    }}
  }});
}}

renderSummary();
renderTable();
renderChart();
</script>
</body>
</html>
"""
    DASHBOARD_HTML.write_text(html)


def main():
    ap = argparse.ArgumentParser(description="Build the Hydrex retention scorecard.")
    ap.add_argument("--no-color", action="store_true", help="plain console output")
    ap.add_argument("--no-html", action="store_true", help="skip retention.html")
    args = ap.parse_args()

    cfg = load_config()
    rows = load_rows()
    cards = build_scorecard(rows, cfg)
    if not cards:
        print("No pools to score — tracker is empty.")
        return

    write_csv(cards)
    use_color = (not args.no_color) and sys.stdout.isatty()
    print_table(cards, use_color)
    print(f"Scorecard written: {SCORECARD_CSV}")

    if not args.no_html:
        render_dashboard(cards, cfg)
        print(f"Dashboard written: {DASHBOARD_HTML}")


if __name__ == "__main__":
    main()
