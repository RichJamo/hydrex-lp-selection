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

Market-relative read (beta vs alpha): in a down market a pool's absolute f/i
falls with the tide. `market_share` = the pool's fees as a share of total Hydrex
CLAMM fees that epoch; `share_trend` tells you whether the pool is gaining or
losing ground *independent of* market conditions. The decay guard uses it so a
pool holding/gaining share is not flagged as decaying merely because the whole
market dropped. Market totals are cached offline in data/market_fees.csv and
refreshed from the Hydrex epoch API via `--refresh-market`.

Outputs:
  - data/retention_scorecard.csv  (one row per pool, sorted by score desc)
  - retention.html                (dashboard styled like bootstrap.html)
  - a colour-coded ranked table to the console

All thresholds and weights live in selection_config.json -> retention_scorecard.

Usage:
  python retention_scorecard.py [--no-color] [--no-html] [--refresh-market]
                                [--image] [--highlight "PAIR,PAIR"]

  --image renders retention_scorecard.png via headless Chrome (set CHROME_BIN if
  Chrome isn't auto-found); --highlight marks the current vote-list pools.
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TRACKER_CSV = SCRIPT_DIR / "data" / "bootstrap_tracker.csv"
SCORECARD_CSV = SCRIPT_DIR / "data" / "retention_scorecard.csv"
MARKET_FEES_CSV = SCRIPT_DIR / "data" / "market_fees.csv"
DASHBOARD_HTML = SCRIPT_DIR / "retention.html"
IMAGE_PNG = SCRIPT_DIR / "retention_scorecard.png"
CONFIG_FILE = SCRIPT_DIR / "selection_config.json"

# Total Hydrex CLAMM fees per epoch = sum of every pool's fees from this endpoint.
HYDREX_EPOCH_API = "https://staging.api.hydrex.fi/stats/clamm-pool-epoch-data"

# Headless-Chrome candidates for --image (override with env CHROME_BIN).
CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
]

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
    "share_trend_epsilon": 0.10,
    "_share_trend_epsilon_note": "Change in fee-share-of-market (latest minus first, in pct points) beyond this is 'up'/'down', else 'flat'. Strips market beta from the decay call.",
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


def load_market_fees() -> dict:
    """Read cached total Hydrex CLAMM fees per epoch. {epoch:int -> total_fees:float}.

    Empty if the cache is missing — the scorecard then degrades gracefully
    (share columns blank). Refresh it with `--refresh-market`.
    """
    if not MARKET_FEES_CSV.exists():
        return {}
    out = {}
    with open(MARKET_FEES_CSV, newline="") as f:
        for r in csv.DictReader(f):
            try:
                out[int(r["hydrex_epoch"])] = float(r["total_fees_usd"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def refresh_market_fees(epochs: list) -> dict:
    """Fetch total Hydrex CLAMM fees for each epoch from the API and cache them.

    Network call (one per epoch). Returns {epoch -> total_fees}. Epochs that
    return no pools are recorded as 0.0 (share undefined for that epoch).
    """
    import requests  # local import keeps the default offline path dependency-free

    totals = {}
    for ep in sorted(set(epochs)):
        try:
            r = requests.get(f"{HYDREX_EPOCH_API}/{ep}", timeout=30)
            r.raise_for_status()
            pools = r.json().get("pools", [])
            totals[ep] = round(sum(float(p.get("fees") or 0) for p in pools), 2)
            print(f"  market ep{ep}: ${totals[ep]:,.0f} ({len(pools)} pools)")
        except Exception as e:  # noqa: BLE001 — best-effort cache refresh
            print(f"  market ep{ep}: fetch failed ({e}) — left out", file=sys.stderr)
    if totals:
        with open(MARKET_FEES_CSV, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["hydrex_epoch", "total_fees_usd"])
            for ep in sorted(totals):
                w.writerow([ep, totals[ep]])
    return totals


def aggregate_pool(addr: str, epochs: list, cfg: dict, global_max_epoch: int,
                   market_fees: dict) -> dict:
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

    # --- market share (beta vs alpha) ---
    # Fee share of total Hydrex CLAMM fees, measured over the SAME funded epochs
    # as f/i so the two are comparable. Using funded epochs (not all present
    # epochs) avoids anchoring the trend on a pre-funding launch epoch whose
    # near-zero share would make a declining pool falsely read as "gaining".
    share_vals = []
    for r in funded:
        en = int(fnum(r, "hydrex_epoch"))
        mt = market_fees.get(en, 0)
        if mt > 0:
            share_vals.append(fnum(r, "fees_usd") / mt * 100)
    if share_vals:
        market_share_pct_latest = share_vals[-1]
        market_share_pct_mean = sum(share_vals) / len(share_vals)
        if len(share_vals) >= 2:
            share_delta = share_vals[-1] - share_vals[0]
            seps = cfg["share_trend_epsilon"]
            share_trend = "up" if share_delta > seps else "down" if share_delta < -seps else "flat"
        else:
            share_delta = 0.0
            share_trend = "n/a"
    else:
        market_share_pct_latest = market_share_pct_mean = share_delta = None
        share_trend = "n/a"

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
        latest_epoch, global_max_epoch, share_trend,
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
        "market_share_pct_latest": round(market_share_pct_latest, 4) if market_share_pct_latest is not None else "",
        "market_share_pct_mean": round(market_share_pct_mean, 4) if market_share_pct_mean is not None else "",
        "share_delta_pct": round(share_delta, 4) if share_delta is not None else "",
        "share_trend": share_trend,
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
              profitable_ratio, trend, latest_epoch, global_max_epoch, share_trend):
    """Map metrics to a KEEP/WATCH/CUT/NEW/DEAD/TREASURY call + one-line reason."""
    th = cfg["thresholds"]
    profit = cfg["profit_threshold"]
    excl = {str(x).lower() for x in cfg.get("treasury_exclude", [])}
    dropped = latest_epoch < global_max_epoch  # not run in the most recent epoch
    share_note = {"up": " · gaining share", "down": " · losing share"}.get(share_trend, "")

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
    # Market-beta filter: a pool GAINING fee share has an f/i drop driven by the
    # market falling, not the pool failing — so don't call it decaying.
    decaying = trend == "down" and fi_wmean < profit and share_trend != "up"

    # Strong, current signal -> keep funding it.
    if fi_latest >= profit:
        tag = " (last run)" if dropped else ""
        return "KEEP", f"Latest f/i {fi_latest:.2f} ≥ break-even{tag}{share_note}"
    if score >= th["keep"] and not decaying:
        why = "f/i fell with the market but holding share" if trend == "down" and share_trend == "up" \
            else "efficient & sticky"
        return "KEEP", f"Score {score:.2f} ≥ keep threshold — {why}{share_note}"

    # Clear failure -> stop funding it.
    if profitable_ratio == 0 and tvl_ret < cfg["cut_tvl_retention"]:
        return "CUT", f"Never broke even and TVL down to {tvl_ret:.0%} of peak{share_note}"
    if score <= th["cut"]:
        return "CUT", f"Score {score:.2f} ≤ cut threshold — poor ROI/retention{share_note}"

    # Everything else needs a human eye.
    direction = {"up": "improving", "down": "decaying", "flat": "flat"}.get(trend, "mixed")
    return "WATCH", f"Score {score:.2f}, trend {direction}{share_note} — re-up only if budget allows"


def build_scorecard(rows: list, cfg: dict, market_fees: dict) -> list:
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
        cards.append(aggregate_pool(addr, epochs, cfg, global_max_epoch, market_fees))

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

    sh_abbr = {"up": "up", "down": "dn", "flat": "flat", "n/a": "-"}
    hdr = (f"{'PAIR':16} {'EPS':>3} {'FEE/TVL':>8} {'F/I lt':>7} {'F/I wm':>7} "
           f"{'MKT%':>6} {'SHTRD':>5} {'TVLret':>7} {'TREND':>6} {'SCORE':>6}  REC")
    print()
    print(c(hdr, "bold") if use_color else hdr)
    print("-" * len(hdr))
    for r in cards:
        ms = r["market_share_pct_latest"]
        ms_str = f"{ms:>5.2f}%" if isinstance(ms, (int, float)) else f"{'-':>6}"
        line = (
            f"{r['pair'][:16]:16} "
            f"{r['epochs_incentivized']:>3} "
            f"{r['fees_tvl_pct_mean']:>7.2f}% "
            f"{r['fees_per_incentive_latest']:>7.2f} "
            f"{r['fees_per_incentive_wmean']:>7.2f} "
            f"{ms_str} "
            f"{sh_abbr.get(r['share_trend'], r['share_trend']):>5} "
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
      <th class="num" data-k="market_share_pct_latest">Mkt share</th>
      <th class="num" data-k="share_trend">Share trend</th>
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
function shareTrend(t) {{
  if (t === 'up') return '<span style="color:var(--green)">▲ up</span>';
  if (t === 'down') return '<span style="color:var(--red)">▼ down</span>';
  if (t === 'flat') return 'flat';
  return '–';
}}
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
      <td class="num">${{c.market_share_pct_latest === '' || c.market_share_pct_latest == null ? '–' : pctFmt(c.market_share_pct_latest)}}</td>
      <td class="num">${{shareTrend(c.share_trend)}}</td>
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


def find_chrome() -> str:
    """Locate a Chrome/Chromium binary for headless screenshots, or '' if none."""
    env = os.environ.get("CHROME_BIN")
    if env and (Path(env).exists() or shutil.which(env)):
        return env
    for c in CHROME_CANDIDATES:
        if Path(c).exists() or shutil.which(c):
            return c
    return ""


def build_image_html(cards: list, cfg: dict, highlight: set) -> str:
    """Static, self-contained HTML (no JS/CDN) tuned for a headless screenshot."""
    keep_th, cut_th = cfg["thresholds"]["keep"], cfg["thresholds"]["cut"]

    def fnum_(v, d=2):
        try:
            return f"{float(v):.{d}f}"
        except (TypeError, ValueError):
            return "–"

    def share(v):
        try:
            return f"{float(v):.2f}%"
        except (TypeError, ValueError):
            return "–"

    def strend(t):
        return {"up": '<span style="color:#3fb950">▲</span>',
                "down": '<span style="color:#f85149">▼</span>',
                "flat": "·"}.get(t, "–")

    trs = []
    for r in cards:
        hl = ' style="border-left:3px solid #58a6ff"' if r["pair"] in highlight else ""
        col = REC_COLOR.get(r["recommendation"], "#8b949e")
        net = float(r["net_usd"])
        netc = "#3fb950" if net >= 0 else "#f85149"
        trs.append(
            f"<tr{hl}>"
            f"<td><b>{r['pair']}</b> <span class=ep>ep {r['epoch_range']}</span></td>"
            f"<td><span class=badge style=\"background:{col}\">{r['recommendation']}</span></td>"
            f"<td class=num><b>{fnum_(r['retention_score'])}</b></td>"
            f"<td class=num>{fnum_(r['fees_tvl_pct_mean'])}%</td>"
            f"<td class=num>{fnum_(r['fees_per_incentive_wmean'])}</td>"
            f"<td class=num>{share(r['market_share_pct_latest'])}</td>"
            f"<td class=num>{strend(r['share_trend'])}</td>"
            f"<td class=num>{round(float(r['tvl_retention'])*100)}%</td>"
            f"<td class=num style=\"color:{netc}\">${net:,.0f}</td>"
            f"<td class=why>{r['reason']}</td></tr>"
        )

    hl_note = " · <b style=\"color:#58a6ff\">blue bar</b> = current vote list" if highlight else ""
    return f"""<html><head><meta charset=utf-8><style>
body{{margin:0;background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:22px;width:1180px}}
h1{{margin:0 0 4px;font-size:20px}} .sub{{color:#8b949e;font-size:12px;margin-bottom:14px}}
table{{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:10px;overflow:hidden}}
th,td{{padding:7px 10px;text-align:left;border-bottom:1px solid #30363d;font-size:12px;white-space:nowrap}}
th{{background:rgba(255,255,255,.03);color:#8b949e;text-transform:uppercase;font-size:10px;letter-spacing:.5px}}
td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums}}
.badge{{padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;color:#0d1117}}
.ep{{color:#8b949e;font-size:10px}} .why{{color:#8b949e;font-size:11px;white-space:normal;max-width:360px}}
.legend{{color:#8b949e;font-size:11px;margin-top:10px}}
</style></head><body>
<h1>Hydrex Bootstrap — Retention Scorecard</h1>
<div class=sub>KEEP ≥ {keep_th} · CUT ≤ {cut_th}{hl_note} · Mkt share &amp; ▲▼ strip market-wide fee moves (beta vs alpha)</div>
<table>
<thead><tr><th>Pair</th><th>Call</th><th class=num>Score</th><th class=num>Fee/TVL</th><th class=num>f/i (wmean)</th><th class=num>Mkt share</th><th class=num>Shr</th><th class=num>TVL kept</th><th class=num>Net P&amp;L</th><th>Why</th></tr></thead>
<tbody>{''.join(trs)}</tbody></table>
<div class=legend>f/i = $fees per $1 incentive · break-even = 1.0</div>
</body></html>"""


def render_image(cards: list, cfg: dict, highlight: set):
    """Screenshot the scorecard to a PNG via headless Chrome."""
    chrome = find_chrome()
    if not chrome:
        print("Chrome/Chromium not found — set CHROME_BIN to use --image.", file=sys.stderr)
        return
    html = build_image_html(cards, cfg, highlight)
    height = 260 + len(cards) * 34
    tmp = Path(tempfile.gettempdir()) / "retention_scorecard_img.html"
    tmp.write_text(html)
    try:
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--force-device-scale-factor=2", f"--screenshot={IMAGE_PNG}",
             f"--window-size=1224,{height}", f"file://{tmp}"],
            check=True, capture_output=True, timeout=60,
        )
        print(f"Image written: {IMAGE_PNG}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"Image render failed: {e}", file=sys.stderr)
    finally:
        tmp.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser(description="Build the Hydrex retention scorecard.")
    ap.add_argument("--no-color", action="store_true", help="plain console output")
    ap.add_argument("--no-html", action="store_true", help="skip retention.html")
    ap.add_argument("--image", action="store_true",
                    help="also render retention_scorecard.png via headless Chrome (set CHROME_BIN if needed).")
    ap.add_argument("--highlight", default="",
                    help="comma-separated pairs to mark with a blue bar in the image, e.g. 'WETH/NOCK,LFI/USDC'.")
    ap.add_argument("--refresh-market", action="store_true",
                    help="Fetch total Hydrex fees per epoch from the API and update "
                         "data/market_fees.csv before scoring (needed for share metrics).")
    args = ap.parse_args()

    cfg = load_config()
    rows = load_rows()

    if args.refresh_market:
        epochs = sorted({int(fnum(r, "hydrex_epoch")) for r in rows})
        print("Refreshing market fees from the Hydrex epoch API...")
        market_fees = refresh_market_fees(epochs)
    else:
        market_fees = load_market_fees()
        if not market_fees:
            print("Note: no data/market_fees.csv — share columns blank. "
                  "Run with --refresh-market to populate.", file=sys.stderr)

    cards = build_scorecard(rows, cfg, market_fees)
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

    if args.image:
        highlight = {p.strip() for p in args.highlight.split(",") if p.strip()}
        render_image(cards, cfg, highlight)


if __name__ == "__main__":
    main()
