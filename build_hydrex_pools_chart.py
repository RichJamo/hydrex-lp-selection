"""Generate hydrex_pools.html — daily charts of TVL, Volume, V/T, F/T, F/V
for the 5 Hydrex pools we track against Aero. Vertical lines annotate when
fee parameters changed for each pool.

Reads from:
  data/hydrex_pools_daily.csv      (daily metrics)
  data/hydrex_param_changes.csv    (param change events)

Outputs:
  hydrex_pools.html                (interactive Plotly page)
"""

import csv
import json
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DAILY_CSV = SCRIPT_DIR / "data" / "hydrex_pools_daily.csv"
PARAMS_CSV = SCRIPT_DIR / "data" / "hydrex_param_changes.csv"
OUT_HTML = SCRIPT_DIR / "hydrex_pools.html"

PAIRS = ["WETH/cbBTC", "WETH/USDC", "USDC/cbBTC", "WETH/EURC", "WETH/cbXRP"]
COLORS = {
    "WETH/cbBTC": "#ff5c5c",
    "WETH/USDC": "#4ea8ff",
    "USDC/cbBTC": "#16a085",
    "WETH/EURC": "#e67e22",
    "WETH/cbXRP": "#b47cff",
}


def f(x):
    try: return float(x)
    except: return None


def main():
    # Load daily metrics
    by_pair = defaultdict(list)
    with open(DAILY_CSV) as fp:
        for r in csv.DictReader(fp):
            by_pair[r["pair"]].append(r)
    for pair, rs in by_pair.items():
        rs.sort(key=lambda r: r["date"])

    # Load param changes per pair
    params_by_pair = defaultdict(list)
    if PARAMS_CSV.exists():
        with open(PARAMS_CSV) as fp:
            for r in csv.DictReader(fp):
                params_by_pair[r["pair"]].append(r)

    # Build per-pair JS data
    data = {}
    for pair in PAIRS:
        rs = by_pair.get(pair, [])
        data[pair] = {
            "dates": [r["date"] for r in rs],
            "tvl":   [f(r["tvl_usd"]) for r in rs],
            "vol":   [f(r["volume_usd"]) for r in rs],
            "fees":  [f(r["fees_usd"]) for r in rs],
            "v_t":   [f(r["vol_per_tvl_pct"]) for r in rs],
            "f_t":   [f(r["fees_per_tvl_pct"]) for r in rs],
            "f_v":   [f(r["fees_per_vol_pct"]) for r in rs],
            "params": [
                {
                    "date": p["date"],
                    "base": int(p["baseFee"]),
                    "a1": int(p["alpha1"]),
                    "a2": int(p["alpha2"]),
                    "b1": int(p["beta1"]),
                    "b2": int(p["beta2"]),
                    "g1": int(p["gamma1"]),
                    "g2": int(p["gamma2"]),
                    "max": int(p["max_fee_pips"]),
                }
                for p in params_by_pair.get(pair, [])
            ],
        }

    data_json = json.dumps(data)
    pairs_json = json.dumps(PAIRS)
    colors_json = json.dumps(COLORS)

    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Hydrex Pool Daily Tracker</title>
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
    input[type="radio"], input[type="checkbox"] {{ accent-color: var(--accent); margin-right: 4px; }}
    .pool {{ background: var(--panel); border: 1px solid var(--border);
              border-radius: 10px; padding: 20px; margin-bottom: 20px; }}
    .pool-title {{ font-size: 16px; font-weight: 700; margin-bottom: 4px; }}
    .pool-addr {{ font-family: ui-monospace, monospace; font-size: 11px;
                   color: var(--muted); margin-bottom: 8px; word-break: break-all; }}
    .pool-params {{ font-size: 11px; color: var(--muted); margin-bottom: 10px;
                     font-family: ui-monospace, monospace; }}
    .chart {{ min-height: 320px; }}
    a {{ color: var(--accent); }}
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>Hydrex Pool Daily Tracker</h1>
      <p class="subtitle">TVL, Volume, V/T, F/T, F/V — daily resolution, annotated with fee-parameter changes (orange dashed vertical lines).</p>
    </div>
    <div class="topbar-meta">
      <a href="index.html">Aero vs Hydrex</a> &nbsp;|&nbsp;
      <a href="bootstrap.html">Bootstrap</a> &nbsp;|&nbsp;
      <a href="ichi.html">ICHI Diagnostic</a> &nbsp;|&nbsp;
      <a href="vt_chart.html">V/T by Bucket</a>
    </div>
  </div>

  <div class="controls">
    <div class="control-group">
      <strong>Metric</strong>
      <label><input type="radio" name="metric" value="tvl" checked> TVL</label>
      <label><input type="radio" name="metric" value="vol"> Volume</label>
      <label><input type="radio" name="metric" value="fees"> Fees</label>
      <label><input type="radio" name="metric" value="v_t"> V/T</label>
      <label><input type="radio" name="metric" value="f_t"> F/T</label>
      <label><input type="radio" name="metric" value="f_v"> F/V</label>
    </div>
    <div class="control-group">
      <strong>View</strong>
      <label><input type="radio" name="mode" value="absolute" checked> Absolute value</label>
      <label><input type="radio" name="mode" value="delta_pct"> Day-over-day %</label>
      <label><input type="radio" name="mode" value="delta_abs"> Day-over-day Δ</label>
    </div>
    <div class="control-group">
      <strong>Smoothing</strong>
      <label><input type="radio" name="smooth" value="raw"> Raw only</label>
      <label><input type="radio" name="smooth" value="ma7" checked> Raw + 7-day MA</label>
      <label><input type="radio" name="smooth" value="ma14"> Raw + 14-day MA</label>
      <label><input type="radio" name="smooth" value="ma7only"> 7-day MA only</label>
    </div>
    <div class="control-group">
      <strong>Y-axis</strong>
      <label><input type="radio" name="yscale" value="linear" checked> Linear</label>
      <label><input type="radio" name="yscale" value="log"> Log</label>
    </div>
    <div class="control-group">
      <label><input type="checkbox" id="show-params" checked> Show param-change markers</label>
    </div>
  </div>

  <div id="charts"></div>

<script>
const DATA = {data_json};
const PAIRS = {pairs_json};
const COLORS = {colors_json};

const METRIC_INFO = {{
  tvl:  {{ label: 'TVL (USD)',                tickFmt: ',.0f', prefix: '$' }},
  vol:  {{ label: 'Daily Volume (USD)',       tickFmt: ',.0f', prefix: '$' }},
  fees: {{ label: 'Daily Fees (USD)',         tickFmt: ',.2f', prefix: '$' }},
  v_t:  {{ label: 'V/T (% per day)',          tickFmt: '.1f',  suffix: '%' }},
  f_t:  {{ label: 'F/T (% per day)',          tickFmt: '.4f',  suffix: '%' }},
  f_v:  {{ label: 'F/V — effective fee (%)',  tickFmt: '.4f',  suffix: '%' }},
}};

// Rolling mean (window = N days). Returns array of same length, leading nulls
// until enough data, then trailing N-day average.
function rollingMean(arr, window) {{
  const out = new Array(arr.length).fill(null);
  for (let i = 0; i < arr.length; i++) {{
    if (i < window - 1) continue;
    const slice = arr.slice(i - window + 1, i + 1).filter(v => v !== null && !isNaN(v));
    if (slice.length === 0) continue;
    out[i] = slice.reduce((a, b) => a + b, 0) / slice.length;
  }}
  return out;
}}

// Day-over-day absolute delta. out[i] = arr[i] - arr[i-1]
function deltaAbs(arr) {{
  const out = [null];
  for (let i = 1; i < arr.length; i++) {{
    const prev = arr[i-1], cur = arr[i];
    if (prev === null || cur === null || isNaN(prev) || isNaN(cur)) out.push(null);
    else out.push(cur - prev);
  }}
  return out;
}}

// Day-over-day percent change. out[i] = (arr[i] - arr[i-1]) / arr[i-1] × 100
function deltaPct(arr) {{
  const out = [null];
  for (let i = 1; i < arr.length; i++) {{
    const prev = arr[i-1], cur = arr[i];
    if (prev === null || prev === 0 || cur === null || isNaN(prev) || isNaN(cur)) out.push(null);
    else out.push(((cur - prev) / prev) * 100);
  }}
  return out;
}}

function renderAll() {{
  const metric = document.querySelector('input[name="metric"]:checked').value;
  const mode = document.querySelector('input[name="mode"]:checked').value;
  const smooth = document.querySelector('input[name="smooth"]:checked').value;
  const yscale = document.querySelector('input[name="yscale"]:checked').value;
  const showParams = document.getElementById('show-params').checked;
  const info = METRIC_INFO[metric];

  // Customize hover format based on mode
  let displayPrefix = info.prefix || '';
  let displaySuffix = info.suffix || '';
  let displayFmt = info.tickFmt;
  let yTitle = info.label;
  if (mode === 'delta_pct') {{
    displayPrefix = ''; displaySuffix = '%'; displayFmt = '+,.1f'; yTitle = info.label + ' — daily % change';
  }} else if (mode === 'delta_abs') {{
    displayFmt = info.prefix ? '+,.0f' : '+,.4f';
    yTitle = info.label + ' — daily Δ';
  }}

  const container = document.getElementById('charts');
  container.innerHTML = '';

  for (const pair of PAIRS) {{
    const d = DATA[pair];
    const color = COLORS[pair];

    const panel = document.createElement('div');
    panel.className = 'pool';

    const latestParams = d.params.length ? d.params[d.params.length - 1] : null;
    const paramsLine = latestParams
      ? `Current params (last changed ${{latestParams.date}}): base=${{latestParams.base}}  a1=${{latestParams.a1}}  a2=${{latestParams.a2}}  b1=${{latestParams.b1}}  b2=${{latestParams.b2}}  g1=${{latestParams.g1}}  g2=${{latestParams.g2}}  max=${{latestParams.max}} pips`
      : 'No param changes recorded';

    panel.innerHTML = `
      <div class="pool-title" style="color:${{color}}">${{pair}}</div>
      <div class="pool-params">${{paramsLine}}</div>
      <div class="chart" id="chart-${{pair.replace('/','-')}}"></div>
    `;
    container.appendChild(panel);

    // Step 1: transform raw values per mode
    let yValues = d[metric];
    if (mode === 'delta_abs') yValues = deltaAbs(yValues);
    else if (mode === 'delta_pct') yValues = deltaPct(yValues);

    // Step 2: compute smoothing variants
    const ma7 = rollingMean(yValues, 7);
    const ma14 = rollingMean(yValues, 14);

    const hoverTemplate = displayPrefix + '%{{y:' + displayFmt + '}}' + displaySuffix + '<extra></extra>';

    const traces = [];
    // Raw line (unless hiding for "ma7only")
    if (smooth !== 'ma7only') {{
      traces.push({{
        x: d.dates, y: yValues, mode: 'lines',
        line: {{ color, width: 1.2 }},
        opacity: smooth === 'raw' ? 1 : 0.35,
        name: 'Daily', hovertemplate: hoverTemplate,
      }});
    }}
    if (smooth === 'ma7' || smooth === 'ma7only') {{
      traces.push({{
        x: d.dates, y: ma7, mode: 'lines',
        line: {{ color, width: 2.5 }},
        name: '7-day MA', hovertemplate: hoverTemplate,
      }});
    }} else if (smooth === 'ma14') {{
      traces.push({{
        x: d.dates, y: ma14, mode: 'lines',
        line: {{ color, width: 2.5 }},
        name: '14-day MA', hovertemplate: hoverTemplate,
      }});
    }}

    // Zero baseline for delta modes
    if (mode !== 'absolute') {{
      traces.push({{
        x: d.dates, y: d.dates.map(() => 0), mode: 'lines',
        line: {{ color: 'rgba(255,255,255,0.2)', width: 1, dash: 'dot' }},
        name: 'zero', showlegend: false, hoverinfo: 'skip',
      }});
    }}

    // Param change vertical lines as shapes + annotations
    const shapes = [];
    const annotations = [];
    if (showParams) {{
      d.params.forEach((p, i) => {{
        shapes.push({{
          type: 'line', xref: 'x', yref: 'paper',
          x0: p.date, x1: p.date, y0: 0, y1: 1,
          line: {{ color: '#ff9933', width: 1, dash: 'dash' }},
        }});
        annotations.push({{
          x: p.date, y: 1, xref: 'x', yref: 'paper',
          text: `b=${{p.base}}/a=${{p.a1}}.${{p.a2}}/b=${{p.b1}}.${{p.b2}}/g=${{p.g1}}.${{p.g2}}`,
          showarrow: false, textangle: -45, yanchor: 'bottom',
          font: {{ size: 9, color: '#ff9933' }},
          xshift: 0, yshift: 4,
        }});
      }});
    }}

    const layout = {{
      margin: {{ l: 70, r: 20, t: 30, b: 50 }},
      paper_bgcolor: '#171a21',
      plot_bgcolor: '#171a21',
      font: {{ color: '#e7ecf2' }},
      xaxis: (function() {{
        // Compute the max date in this pool's series so range buttons anchor correctly
        const maxDate = d.dates[d.dates.length - 1];
        const maxMs = new Date(maxDate + 'T00:00:00Z').getTime();
        const minDate = d.dates[0];
        const dayMs = 24 * 60 * 60 * 1000;
        function backDays(n) {{
          return [new Date(maxMs - n * dayMs).toISOString().slice(0,10), maxDate];
        }}
        return {{
          gridcolor: 'rgba(255,255,255,0.04)',
          color: '#9aa4b2',
          type: 'date',
          autorange: false,
          range: backDays(30),  // default view = last 30 days
          rangeselector: {{
            buttons: [
              {{ label: '7D',  method: 'relayout', args: [{{'xaxis.range': backDays(7) }}]  }},
              {{ label: '30D', method: 'relayout', args: [{{'xaxis.range': backDays(30) }}] }},
              {{ label: '90D', method: 'relayout', args: [{{'xaxis.range': backDays(90) }}] }},
              {{ label: 'YTD', method: 'relayout', args: [{{'xaxis.range': [maxDate.slice(0,4) + '-01-01', maxDate] }}] }},
              {{ label: 'All', method: 'relayout', args: [{{'xaxis.range': [minDate, maxDate] }}] }},
            ],
            bgcolor: '#1b1f27',
            activecolor: '#3a8dff',
            bordercolor: '#262a33',
            font: {{ color: '#e7ecf2', size: 11 }},
            x: 0, y: 1.12, xanchor: 'left', yanchor: 'bottom',
          }},
          rangeslider: {{
            visible: true,
            thickness: 0.06,
            bgcolor: '#1b1f27',
            bordercolor: '#262a33',
            // Always show full dataset range in the slider, regardless of zoom
            range: [minDate, maxDate],
          }},
        }};
      }})(),
      yaxis: {{
        type: (mode !== 'absolute' && yscale === 'log') ? 'linear' : yscale,  // log + signed delta breaks
        title: {{ text: yTitle, font: {{ size: 11, color: '#9aa4b2' }} }},
        gridcolor: 'rgba(255,255,255,0.07)',
        color: '#9aa4b2',
        tickprefix: displayPrefix,
        ticksuffix: displaySuffix,
        tickformat: displayFmt.startsWith('+') ? displayFmt.slice(1) : displayFmt,  // for axis ticks, no + sign
      }},
      shapes,
      annotations,
      hovermode: 'x unified',
      dragmode: 'zoom',  // click+drag on chart to box-zoom into any range
      showlegend: smooth !== 'raw',
      legend: {{
        orientation: 'h',
        x: 1, xanchor: 'right',
        y: 1.02, yanchor: 'bottom',
        bgcolor: 'rgba(23,26,33,0.7)',
        bordercolor: '#262a33',
        borderwidth: 1,
        font: {{ size: 11, color: '#e7ecf2' }},
      }},
    }};

    const divId = `chart-${{pair.replace('/','-')}}`;
    Plotly.newPlot(divId, traces, layout, {{
      displayModeBar: true, scrollZoom: true, responsive: true,
      displaylogo: false, modeBarButtonsToRemove: ['lasso2d', 'select2d'],
    }}).then(gd => {{
      // Compute the y-range from the data points visible inside the current x-range.
      // Falls back to autorange if the window contains no valid points.
      function computeYRange(graphDiv, xRange) {{
        if (!xRange || xRange === 'autorange') return null;
        const xMin = new Date(xRange[0]).getTime();
        const xMax = new Date(xRange[1]).getTime();
        let yMin = Infinity, yMax = -Infinity;
        (graphDiv.data || []).forEach(tr => {{
          // Skip the dashed-zero baseline and any helper trace
          if (tr.name === 'zero' || tr.hoverinfo === 'skip') return;
          if (!tr.x || !tr.y) return;
          for (let i = 0; i < tr.x.length; i++) {{
            const ts = new Date(tr.x[i]).getTime();
            if (ts < xMin || ts > xMax) continue;
            const v = tr.y[i];
            if (v === null || v === undefined || isNaN(v)) continue;
            if (v < yMin) yMin = v;
            if (v > yMax) yMax = v;
          }}
        }});
        if (!isFinite(yMin) || !isFinite(yMax)) return null;
        // Add 8% headroom so the line doesn't touch the edges
        const pad = (yMax - yMin) * 0.08 || Math.abs(yMax) * 0.1 || 0.0001;
        return [yMin - pad, yMax + pad];
      }}

      // Sync x-axis range across all pool charts AND rescale each chart's y-axis
      // to the visible window. Uses a SHARED global flag so programmatic relayouts
      // on sibling charts don't recursively re-trigger sync.
      gd.on('plotly_relayout', ev => {{
        if (window.__rangeSyncing) return;
        let newRange = null;
        if (ev['xaxis.range']) {{
          newRange = ev['xaxis.range'];
        }} else if (ev['xaxis.range[0]'] !== undefined && ev['xaxis.range[1]'] !== undefined) {{
          newRange = [ev['xaxis.range[0]'], ev['xaxis.range[1]']];
        }} else if (ev['xaxis.autorange'] === true) {{
          newRange = 'autorange';
        }}
        if (!newRange) return;
        window.__rangeSyncing = true;

        const allIds = PAIRS.map(p => `chart-${{p.replace('/','-')}}`);
        const tasks = allIds.map(id => {{
          const targetGd = document.getElementById(id);
          let update;
          if (newRange === 'autorange') {{
            update = {{ 'xaxis.autorange': true, 'yaxis.autorange': true }};
          }} else {{
            update = {{ 'xaxis.range': newRange, 'xaxis.autorange': false }};
            const yRange = computeYRange(targetGd, newRange);
            if (yRange) {{
              update['yaxis.range'] = yRange;
              update['yaxis.autorange'] = false;
            }} else {{
              update['yaxis.autorange'] = true;
            }}
          }}
          return Plotly.relayout(id, update);
        }});
        Promise.all(tasks).finally(() => {{ window.__rangeSyncing = false; }});
      }});
    }});
  }}
}}

document.querySelectorAll('input[name="metric"], input[name="mode"], input[name="smooth"], input[name="yscale"], #show-params').forEach(el =>
  el.addEventListener('change', renderAll)
);
renderAll();
</script>
</body>
</html>
"""

    OUT_HTML.write_text(html)
    print(f"Wrote {OUT_HTML}")


if __name__ == "__main__":
    main()
