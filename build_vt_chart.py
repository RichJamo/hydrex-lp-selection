"""Generate V/T charts (daily + epoch) from extended_ichi_history.csv.

4 subplots per view (one per pair). Toggle between daily and epoch (weekly Thu-Wed).

Daily V/T  = daily_volume / daily_TVL (snapshot at 00:00 UTC) × 100
Epoch V/T  = sum(daily_volume over 7d) / avg(daily_TVL over 7d) × 100

For Aero on epoch view, use the actual weekly numbers from aero_vs_hydrex_combined.csv
(no ÷7 approximation needed).

Output: hydrex-lp-dashboard/vt_chart.html
"""

import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CSV_PATH = Path("/Users/kingofcrystals/hydrexbd/extended_ichi_history.csv")
AERO_CSV = Path("/Users/kingofcrystals/hydrexbd/hydrex-lp-dashboard/data/aero_vs_hydrex_combined.csv")
OUT_HTML = Path("/Users/kingofcrystals/hydrexbd/hydrex-lp-dashboard/vt_chart.html")

PAIRS = ["WETH/cbBTC", "WETH/USDC", "USDC/cbBTC", "WETH/EURC"]


def f(x):
    try: return float(x)
    except: return None


def load_aero_weekly():
    """{pair: {epoch: {tvl, vol, fees, start_date}}}"""
    out = defaultdict(dict)
    with open(AERO_CSV) as fp:
        for r in csv.DictReader(fp):
            out[r["pair"]][int(r["epoch"])] = {
                "tvl":  float(r["aero_tvl_usd"] or 0),
                "vol":  float(r["aero_volume_usd"] or 0),
                "fees": float(r["aero_fees_usd"] or 0),
                "start_date": r["epoch_start"],
            }
    return out


def epoch_of(date_str):
    """Map a YYYY-MM-DD date to its Hydrex epoch.
    Hydrex epochs run Thu 00:00 UTC -> Thu 00:00 UTC.
    Hydrex epoch 127 starts 2026-01-29 (Thursday)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    base = datetime(2026, 1, 29, tzinfo=timezone.utc)  # epoch 127 start
    days = (d - base).days
    epoch = 127 + (days // 7)
    epoch_start = base + timedelta(days=(days // 7) * 7)
    return epoch, epoch_start.strftime("%Y-%m-%d")


def aggregate_to_epochs(daily_rows, aero_weekly_pair):
    """Roll up daily rows for one pair into per-epoch rows."""
    by_epoch = defaultdict(list)
    for r in daily_rows:
        ep, ep_start = epoch_of(r["date"])
        by_epoch[ep].append((r, ep_start))

    out = []
    for ep in sorted(by_epoch.keys()):
        days = by_epoch[ep]
        ep_start = days[0][1]
        n = len(days)
        # Sum flows
        pool_vol  = sum(f(r["volume"]) or 0 for r, _ in days)
        pool_fees = sum(f(r["fees"])   or 0 for r, _ in days)
        manual_vol = sum(f(r["manual_est_volume"]) or 0 for r, _ in days)
        s1_vol     = sum(f(r["strat1_est_volume"]) or 0 for r, _ in days)
        s2_vol     = sum(f(r["strat2_est_volume"]) or 0 for r, _ in days)
        manual_fees = sum(f(r["manual_est_fees"]) or 0 for r, _ in days)
        s1_fees     = sum(f(r["strat1_est_fees"]) or 0 for r, _ in days)
        s2_fees     = sum(f(r["strat2_est_fees"]) or 0 for r, _ in days)
        # Avg snapshots
        pool_tvl   = sum(f(r["tvl"])         or 0 for r, _ in days) / n
        manual_tvl = sum(f(r["manual_tvl"])  or 0 for r, _ in days) / n
        s1_tvl     = sum(f(r["strat1_tvl"])  or 0 for r, _ in days) / n
        s2_tvl     = sum(f(r["strat2_tvl"])  or 0 for r, _ in days) / n

        # Epoch V/T = epoch_volume / avg_tvl (weekly turnover, %)
        def vt(vol, tvl): return (vol / tvl * 100) if tvl > 0 else None
        pool_vt   = vt(pool_vol, pool_tvl)
        manual_vt = vt(manual_vol, manual_tvl)
        s1_vt     = vt(s1_vol, s1_tvl)
        s2_vt     = vt(s2_vol, s2_tvl)

        # Aero V/T from weekly CSV (real, not approximated).
        # Note: `ep` here is already in Aerodrome epoch numbering
        # (my epoch_of() starts at 127 for Jan 29, which is Aero ep 127),
        # so look up directly.
        aero = aero_weekly_pair.get(ep)
        aero_vt = None
        aero_tvl = aero_vol = aero_fees = None
        if aero and aero["tvl"] > 0:
            aero_vt  = aero["vol"] / aero["tvl"] * 100
            aero_tvl = aero["tvl"]
            aero_vol = aero["vol"]
            aero_fees = aero["fees"]

        # Partial epoch flag (less than 7 days of data)
        partial = n < 7

        out.append({
            "epoch": ep,
            "aero_epoch": ep + 107,
            "epoch_start": ep_start,
            "days_in_epoch": n,
            "partial": partial,
            "pool_tvl": round(pool_tvl, 2),
            "pool_vol": round(pool_vol, 2),
            "pool_fees": round(pool_fees, 4),
            "pool_vt": round(pool_vt, 2) if pool_vt is not None else None,
            "aero_tvl": aero_tvl, "aero_vol": aero_vol, "aero_fees": aero_fees,
            "aero_vt": round(aero_vt, 2) if aero_vt is not None else None,
            "manual_tvl": round(manual_tvl, 2), "manual_vol": round(manual_vol, 2),
            "manual_fees": round(manual_fees, 4), "manual_vt": round(manual_vt, 2) if manual_vt is not None else None,
            "s1_tvl": round(s1_tvl, 2), "s1_vol": round(s1_vol, 2),
            "s1_fees": round(s1_fees, 4), "s1_vt": round(s1_vt, 2) if s1_vt is not None else None,
            "s2_tvl": round(s2_tvl, 2), "s2_vol": round(s2_vol, 2),
            "s2_fees": round(s2_fees, 4), "s2_vt": round(s2_vt, 2) if s2_vt is not None else None,
        })
    return out


def main():
    rows = list(csv.DictReader(open(CSV_PATH)))
    aero_weekly = load_aero_weekly()
    by_pair = defaultdict(list)
    for r in rows:
        by_pair[r["pair"]].append(r)

    # Build both daily and epoch data per pair
    pair_data = {}
    for pair in PAIRS:
        prs = sorted(by_pair[pair], key=lambda r: r["date"])
        epoch_rows = aggregate_to_epochs(prs, aero_weekly.get(pair, {}))

        pair_data[pair] = {
            # Daily
            "daily_dates":   [r["date"] for r in prs],
            "daily_aero":    [f(r["aero_volume_tvl_pct"]) for r in prs],
            "daily_manual":  [f(r["manual_vt_pct"]) for r in prs],
            "daily_strat1":  [f(r["strat1_vt_pct"]) for r in prs],
            "daily_strat2":  [f(r["strat2_vt_pct"]) for r in prs],
            # Epoch
            "epoch_labels":  [f"E{r['epoch']} ({r['epoch_start']})" for r in epoch_rows],
            "epoch_partial": [r["partial"] for r in epoch_rows],
            "epoch_aero":    [r["aero_vt"]   for r in epoch_rows],
            "epoch_manual":  [r["manual_vt"] for r in epoch_rows],
            "epoch_strat1":  [r["s1_vt"]     for r in epoch_rows],
            "epoch_strat2":  [r["s2_vt"]     for r in epoch_rows],
            # Metadata
            "s1_name": prs[-1]["strat1_asset_in"] + "-in",
            "s2_name": prs[-1]["strat2_asset_in"] + "-in",
            "pool_addr": prs[0]["address"],
        }

    data_json = json.dumps(pair_data)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Hydrex — V/T by Bucket (Daily / Epoch)</title>
  <script src="https://cdn.plot.ly/plotly-2.35.0.min.js"></script>
  <style>
    :root {{
      --bg: #0f1115; --panel: #171a21; --border: #262a33;
      --text: #e7ecf2; --muted: #9aa4b2; --accent: #4ea8ff;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; padding: 24px; background: var(--bg); color: var(--text);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; }}
    .subtitle {{ color: var(--muted); margin-bottom: 18px; font-size: 13px; }}
    .topbar {{ display: flex; justify-content: space-between; align-items: flex-end;
               margin-bottom: 22px; flex-wrap: wrap; gap: 12px; }}
    .topbar-meta {{ color: var(--muted); font-size: 12px; text-align: right; }}
    .controls {{ background: var(--panel); border: 1px solid var(--border);
                  border-radius: 10px; padding: 14px 18px; margin-bottom: 22px;
                  display: flex; gap: 24px; align-items: center; flex-wrap: wrap; }}
    .control-group {{ display: flex; gap: 16px; align-items: center; }}
    .control-group strong {{ font-size: 12px; color: var(--muted);
                              text-transform: uppercase; letter-spacing: 0.5px; }}
    label {{ font-size: 13px; color: var(--text); cursor: pointer; }}
    input[type="checkbox"], input[type="radio"] {{ accent-color: var(--accent); margin-right: 4px; }}
    select {{ background: var(--bg); color: var(--text); border: 1px solid var(--border);
              border-radius: 6px; padding: 4px 10px; font-size: 13px; }}
    .pool {{ background: var(--panel); border: 1px solid var(--border);
              border-radius: 10px; padding: 20px; margin-bottom: 20px; }}
    .pool-title {{ font-size: 16px; font-weight: 700; margin-bottom: 4px; }}
    .pool-address {{ font-family: ui-monospace, monospace; font-size: 11px;
                      color: var(--muted); margin-bottom: 8px; word-break: break-all; }}
    .chart {{ min-height: 360px; }}
    a {{ color: var(--accent); }}
    .note {{ font-size: 11px; color: var(--muted); font-style: italic; }}
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>V/T by Bucket</h1>
      <p class="subtitle" id="subtitle">Toggle daily vs epoch view. Aero on daily view is weekly÷7;
         on epoch view it's the real weekly number from <a href="data/aero_vs_hydrex_combined.csv">aero_vs_hydrex CSV</a>.</p>
    </div>
    <div class="topbar-meta">
      <a href="index.html">← Aero vs Hydrex</a> &nbsp;|&nbsp;
      <a href="bootstrap.html">Bootstrap</a> &nbsp;|&nbsp;
      <a href="ichi.html">ICHI Diagnostic</a>
    </div>
  </div>

  <div class="controls">
    <div class="control-group">
      <strong>View</strong>
      <label><input type="radio" name="view" value="daily"> Daily</label>
      <label><input type="radio" name="view" value="epoch" checked> Epoch (weekly)</label>
    </div>
    <div class="control-group">
      <strong>Y-axis</strong>
      <label><input type="radio" name="yscale" value="linear" checked> Linear</label>
      <label><input type="radio" name="yscale" value="log"> Log</label>
    </div>
    <div class="control-group">
      <strong>Y-cap</strong>
      <select id="ycap">
        <option value="0" selected>Auto (no cap)</option>
        <option value="300">Hide >300%</option>
        <option value="500">Hide >500%</option>
        <option value="1000">Hide >1000%</option>
        <option value="2500">Hide >2500%</option>
      </select>
    </div>
    <div class="control-group" id="smooth-group">
      <strong>Smoothing</strong>
      <select id="smooth">
        <option value="1" selected>None</option>
        <option value="3">3-day avg</option>
        <option value="7">7-day avg</option>
      </select>
    </div>
  </div>

  <div id="charts"></div>

<script>
const DATA = {data_json};
const PAIRS = {json.dumps(PAIRS)};

const COLORS = {{
  aero:   "#ffd43b",
  manual: "#10b981",
  strat1: "#6f8aff",
  strat2: "#f59e0b",
}};

function rollingAvg(arr, window) {{
  if (window <= 1) return arr;
  const out = new Array(arr.length).fill(null);
  for (let i = 0; i < arr.length; i++) {{
    const start = Math.max(0, i - window + 1);
    const slice = arr.slice(start, i + 1).filter(v => v !== null && !isNaN(v));
    if (slice.length === 0) continue;
    out[i] = slice.reduce((a, b) => a + b, 0) / slice.length;
  }}
  return out;
}}

function renderAll() {{
  const view  = document.querySelector('input[name="view"]:checked').value;
  const yscale = document.querySelector('input[name="yscale"]:checked').value;
  const ycap   = parseFloat(document.getElementById('ycap').value);
  const smooth = parseInt(document.getElementById('smooth').value);

  // Hide smoothing control on epoch view (weekly data already smoothed)
  document.getElementById('smooth-group').style.display = view === 'daily' ? '' : 'none';

  // Update subtitle
  const sub = document.getElementById('subtitle');
  if (view === 'epoch') {{
    sub.textContent = 'Per-epoch (Thu-Wed) V/T = epoch volume / avg TVL. Aero uses real weekly numbers from the existing comparison CSV. Partial epochs (less than 7 days of data) are marked.';
  }} else {{
    sub.textContent = 'Daily V/T = daily volume / TVL snapshot at 00:00 UTC. Aero is weekly÷7 approximation. Try 7-day smoothing to see trend.';
  }}

  const container = document.getElementById('charts');
  container.innerHTML = '';

  for (const pair of PAIRS) {{
    const d = DATA[pair];
    const panel = document.createElement('div');
    panel.className = 'pool';

    let xLabels, aero, manual, s1, s2, marker_size, partialFlags;
    if (view === 'daily') {{
      xLabels = d.daily_dates;
      aero   = rollingAvg(d.daily_aero,   smooth);
      manual = rollingAvg(d.daily_manual, smooth);
      s1     = rollingAvg(d.daily_strat1, smooth);
      s2     = rollingAvg(d.daily_strat2, smooth);
      marker_size = 4;
      partialFlags = null;
    }} else {{
      xLabels = d.epoch_labels;
      aero   = d.epoch_aero;
      manual = d.epoch_manual;
      s1     = d.epoch_strat1;
      s2     = d.epoch_strat2;
      marker_size = 9;
      partialFlags = d.epoch_partial;
    }}

    panel.innerHTML = `
      <div class="pool-title">${{pair}}</div>
      <div class="pool-address">${{d.pool_addr}}</div>
      ${{view === 'epoch' && partialFlags && partialFlags.some(p => p) ? '<div class="note">Note: first or last epoch may be partial (less than 7 days of data); marked with hollow marker.</div>' : ''}}
      <div class="chart" id="chart-${{pair.replace('/','-')}}"></div>
    `;
    container.appendChild(panel);

    // Y-cap behaviour: HIDE values above cap (set to null), don't clamp.
    // Clamping creates a misleading flat line at the cap value.
    const cap = ycap > 0 ? ycap : Infinity;
    const clamp = arr => arr.map(v => v === null || isNaN(v) ? null : (v > cap ? null : v));

    // Marker symbols: hollow for partial epochs
    function markers(arr, color) {{
      if (!partialFlags) return {{ size: marker_size, color }};
      return {{
        size: marker_size,
        color: partialFlags.map(p => p ? 'rgba(0,0,0,0)' : color),
        line: {{ color, width: 2 }},
      }};
    }}

    const traces = [
      {{ x: xLabels, y: clamp(aero),   mode: 'lines+markers', name: 'Aero',
         line: {{ color: COLORS.aero,   width: 2.5 }}, marker: markers(aero, COLORS.aero),
         hovertemplate: 'Aero: %{{y:.1f}}%<extra></extra>' }},
      {{ x: xLabels, y: clamp(manual), mode: 'lines+markers', name: 'Manual',
         line: {{ color: COLORS.manual, width: 2.5 }}, marker: markers(manual, COLORS.manual),
         hovertemplate: 'Manual: %{{y:.1f}}%<extra></extra>' }},
      {{ x: xLabels, y: clamp(s1),     mode: 'lines+markers', name: `Strat 1 (${{d.s1_name}})`,
         line: {{ color: COLORS.strat1, width: 2.5 }}, marker: markers(s1, COLORS.strat1),
         hovertemplate: 'Strat 1: %{{y:.1f}}%<extra></extra>' }},
      {{ x: xLabels, y: clamp(s2),     mode: 'lines+markers', name: `Strat 2 (${{d.s2_name}})`,
         line: {{ color: COLORS.strat2, width: 2.5 }}, marker: markers(s2, COLORS.strat2),
         hovertemplate: 'Strat 2: %{{y:.1f}}%<extra></extra>' }},
    ];

    const layout = {{
      margin: {{ l: 60, r: 30, t: 10, b: view === 'epoch' ? 90 : 50 }},
      paper_bgcolor: '#171a21',
      plot_bgcolor: '#171a21',
      font: {{ color: '#e7ecf2' }},
      xaxis: {{
        gridcolor: 'rgba(255,255,255,0.04)',
        zerolinecolor: 'rgba(255,255,255,0.08)',
        color: '#9aa4b2',
        tickangle: view === 'epoch' ? -25 : 0,
      }},
      yaxis: {{
        type: yscale,
        title: {{ text: view === 'epoch' ? 'V/T per epoch (%)' : 'V/T per day (%)',
                  font: {{ size: 12, color: '#9aa4b2' }} }},
        gridcolor: 'rgba(255,255,255,0.04)',
        zerolinecolor: 'rgba(255,255,255,0.08)',
        color: '#9aa4b2',
        tickformat: '.0f',
        ticksuffix: '%',
      }},
      hovermode: 'x unified',
      legend: {{ orientation: 'h', x: 0.5, xanchor: 'center', y: -0.22,
                 font: {{ size: 11, color: '#e7ecf2' }} }},
    }};

    Plotly.newPlot(`chart-${{pair.replace('/','-')}}`, traces, layout, {{
      displayModeBar: true,
      scrollZoom: true,
      responsive: true,
      displaylogo: false,
      modeBarButtonsToRemove: ['lasso2d', 'select2d'],
    }});
  }}
}}

document.querySelectorAll('input[name="view"], input[name="yscale"], #ycap, #smooth').forEach(el =>
  el.addEventListener('change', renderAll)
);
renderAll();
</script>
</body>
</html>
"""
    OUT_HTML.write_text(html)
    print(f"✓ Wrote chart to {OUT_HTML}")


if __name__ == "__main__":
    main()
