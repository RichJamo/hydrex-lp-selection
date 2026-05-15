"""Generate a daily V/T chart from extended_ichi_history.csv.

4 subplots (one per pair), each with 4 lines:
  - Aerodrome V/T (approximated daily)
  - Manual V/T
  - Strategy 1 V/T
  - Strategy 2 V/T

Output: hydrex-lp-dashboard/vt_chart.html (interactive Plotly).
"""

import csv
import json
from collections import defaultdict
from pathlib import Path

CSV_PATH = Path("/Users/kingofcrystals/hydrexbd/extended_ichi_history.csv")
OUT_HTML = Path("/Users/kingofcrystals/hydrexbd/hydrex-lp-dashboard/vt_chart.html")

PAIRS = ["WETH/cbBTC", "WETH/USDC", "USDC/cbBTC", "WETH/EURC"]


def f(x):
    try: return float(x)
    except: return None


def main():
    rows = list(csv.DictReader(open(CSV_PATH)))
    by_pair = defaultdict(list)
    for r in rows:
        by_pair[r["pair"]].append(r)

    # Build data per pair
    pair_data = {}
    for pair in PAIRS:
        prs = sorted(by_pair[pair], key=lambda r: r["date"])
        dates = [r["date"] for r in prs]
        pair_data[pair] = {
            "dates": dates,
            "aero":   [f(r["aero_volume_tvl_pct"]) for r in prs],
            "manual": [f(r["manual_vt_pct"]) for r in prs],
            "strat1": [f(r["strat1_vt_pct"]) for r in prs],
            "strat2": [f(r["strat2_vt_pct"]) for r in prs],
            "s1_name": prs[-1]["strat1_asset_in"] + "-in",
            "s2_name": prs[-1]["strat2_asset_in"] + "-in",
            "pool_addr": prs[0]["address"],
        }

    # Embed everything as JS data
    data_json = json.dumps(pair_data)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Hydrex — Daily V/T by Bucket</title>
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
    label {{ font-size: 13px; color: var(--text); cursor: pointer; }}
    input[type="checkbox"] {{ accent-color: var(--accent); margin-right: 4px; }}
    select {{ background: var(--bg); color: var(--text); border: 1px solid var(--border);
              border-radius: 6px; padding: 4px 10px; font-size: 13px; }}
    .pool {{ background: var(--panel); border: 1px solid var(--border);
              border-radius: 10px; padding: 20px; margin-bottom: 20px; }}
    .pool-title {{ font-size: 16px; font-weight: 700; margin-bottom: 4px; }}
    .pool-address {{ font-family: ui-monospace, monospace; font-size: 11px;
                      color: var(--muted); margin-bottom: 8px; word-break: break-all; }}
    .chart {{ min-height: 360px; }}
    a {{ color: var(--accent); }}
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>Daily V/T by Bucket — April 1 → May 14</h1>
      <p class="subtitle">Volume/TVL ratio per day. Aerodrome is approximated from weekly CSV (÷ 7).
         Manual / Strat 1 / Strat 2 are derived from active liquidity share × pool daily volume.</p>
    </div>
    <div class="topbar-meta">
      <a href="index.html">← Aero vs Hydrex</a> &nbsp;|&nbsp;
      <a href="bootstrap.html">Bootstrap Tracker</a> &nbsp;|&nbsp;
      <a href="ichi.html">ICHI Diagnostic</a>
    </div>
  </div>

  <div class="controls">
    <div class="control-group">
      <strong style="font-size:12px;color:var(--muted);text-transform:uppercase;">Y-axis</strong>
      <label><input type="radio" name="yscale" value="linear" checked> Linear</label>
      <label><input type="radio" name="yscale" value="log"> Log</label>
    </div>
    <div class="control-group">
      <strong style="font-size:12px;color:var(--muted);text-transform:uppercase;">Y-cap</strong>
      <select id="ycap">
        <option value="0">Auto (no cap)</option>
        <option value="300" selected>Cap at 300%</option>
        <option value="500">Cap at 500%</option>
        <option value="1000">Cap at 1000%</option>
      </select>
    </div>
    <div class="control-group">
      <strong style="font-size:12px;color:var(--muted);text-transform:uppercase;">Smoothing</strong>
      <select id="smooth">
        <option value="1" selected>None (raw daily)</option>
        <option value="3">3-day rolling avg</option>
        <option value="7">7-day rolling avg</option>
      </select>
    </div>
  </div>

  <div id="charts"></div>

<script>
const DATA = {data_json};
const PAIRS = {json.dumps(PAIRS)};

const COLORS = {{
  aero:   "#ffd43b",  // yellow
  manual: "#10b981",  // green
  strat1: "#6f8aff",  // royal blue
  strat2: "#f59e0b",  // amber
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
  const yscale = document.querySelector('input[name="yscale"]:checked').value;
  const ycap = parseFloat(document.getElementById('ycap').value);
  const smooth = parseInt(document.getElementById('smooth').value);

  const container = document.getElementById('charts');
  container.innerHTML = '';

  for (const pair of PAIRS) {{
    const d = DATA[pair];
    const panel = document.createElement('div');
    panel.className = 'pool';
    panel.innerHTML = `
      <div class="pool-title">${{pair}}</div>
      <div class="pool-address">${{d.pool_addr}}</div>
      <div class="chart" id="chart-${{pair.replace('/','-')}}"></div>
    `;
    container.appendChild(panel);

    // Apply smoothing
    const aero   = rollingAvg(d.aero,   smooth);
    const manual = rollingAvg(d.manual, smooth);
    const s1     = rollingAvg(d.strat1, smooth);
    const s2     = rollingAvg(d.strat2, smooth);

    // Apply y cap (set values > cap to null so they're hidden but x-axis remains)
    const cap = ycap > 0 ? ycap : Infinity;
    const clamp = arr => arr.map(v => v === null || isNaN(v) ? null : (v > cap ? cap : v));

    const traces = [
      {{ x: d.dates, y: clamp(aero),   mode: 'lines+markers', name: 'Aero (approx)',
         line: {{ color: COLORS.aero,   width: 2 }}, marker: {{ size: 4 }},
         hovertemplate: 'Aero: %{{y:.1f}}%<extra></extra>' }},
      {{ x: d.dates, y: clamp(manual), mode: 'lines+markers', name: 'Manual',
         line: {{ color: COLORS.manual, width: 2 }}, marker: {{ size: 4 }},
         hovertemplate: 'Manual: %{{y:.1f}}%<extra></extra>' }},
      {{ x: d.dates, y: clamp(s1),     mode: 'lines+markers', name: `Strat 1 (${{d.s1_name}})`,
         line: {{ color: COLORS.strat1, width: 2 }}, marker: {{ size: 4 }},
         hovertemplate: 'Strat 1: %{{y:.1f}}%<extra></extra>' }},
      {{ x: d.dates, y: clamp(s2),     mode: 'lines+markers', name: `Strat 2 (${{d.s2_name}})`,
         line: {{ color: COLORS.strat2, width: 2 }}, marker: {{ size: 4 }},
         hovertemplate: 'Strat 2: %{{y:.1f}}%<extra></extra>' }},
    ];

    const layout = {{
      margin: {{ l: 60, r: 30, t: 10, b: 50 }},
      paper_bgcolor: '#171a21',
      plot_bgcolor: '#171a21',
      font: {{ color: '#e7ecf2' }},
      xaxis: {{
        gridcolor: 'rgba(255,255,255,0.04)',
        zerolinecolor: 'rgba(255,255,255,0.08)',
        color: '#9aa4b2',
      }},
      yaxis: {{
        type: yscale,
        title: {{ text: 'V/T (%)', font: {{ size: 12, color: '#9aa4b2' }} }},
        gridcolor: 'rgba(255,255,255,0.04)',
        zerolinecolor: 'rgba(255,255,255,0.08)',
        color: '#9aa4b2',
        tickformat: '.0f',
        ticksuffix: '%',
      }},
      hovermode: 'x unified',
      legend: {{ orientation: 'h', x: 0.5, xanchor: 'center', y: -0.18,
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

document.querySelectorAll('input[name="yscale"], #ycap, #smooth').forEach(el =>
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
