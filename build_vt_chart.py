"""Generate V/T + F/T + F/V charts (daily + epoch) from extended_ichi_history.csv.

3 metric views:
  - V/T = Volume / TVL × 100  (capital turnover)
  - F/T = Fees / TVL × 100    (yield on TVL)
  - F/V = Fees / Volume × 100 (effective fee rate; pool-level only)

For V/T and F/T: 4 lines (Aero, Manual, Strat 1, Strat 2).
For F/V: 2 lines only (Hydrex pool, Aero pool) since per-bucket F/V collapses
to pool-level under the active%-share approximation.

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
    """Aero-epoch numbering. Epoch 127 starts 2026-01-29 (Thu)."""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    base = datetime(2026, 1, 29, tzinfo=timezone.utc)
    days = (d - base).days
    epoch = 127 + (days // 7)
    epoch_start = base + timedelta(days=(days // 7) * 7)
    return epoch, epoch_start.strftime("%Y-%m-%d")


def aggregate_to_epochs(daily_rows, aero_weekly_pair):
    by_epoch = defaultdict(list)
    for r in daily_rows:
        ep, ep_start = epoch_of(r["date"])
        by_epoch[ep].append((r, ep_start))

    out = []
    for ep in sorted(by_epoch.keys()):
        days = by_epoch[ep]
        ep_start = days[0][1]
        n = len(days)

        pool_vol  = sum(f(r["volume"]) or 0 for r, _ in days)
        pool_fees = sum(f(r["fees"])   or 0 for r, _ in days)
        manual_vol = sum(f(r["manual_est_volume"]) or 0 for r, _ in days)
        s1_vol     = sum(f(r["strat1_est_volume"]) or 0 for r, _ in days)
        s2_vol     = sum(f(r["strat2_est_volume"]) or 0 for r, _ in days)
        manual_fees = sum(f(r["manual_est_fees"]) or 0 for r, _ in days)
        s1_fees     = sum(f(r["strat1_est_fees"]) or 0 for r, _ in days)
        s2_fees     = sum(f(r["strat2_est_fees"]) or 0 for r, _ in days)

        pool_tvl   = sum(f(r["tvl"])         or 0 for r, _ in days) / n
        manual_tvl = sum(f(r["manual_tvl"])  or 0 for r, _ in days) / n
        s1_tvl     = sum(f(r["strat1_tvl"])  or 0 for r, _ in days) / n
        s2_tvl     = sum(f(r["strat2_tvl"])  or 0 for r, _ in days) / n

        def vt(vol, tvl): return (vol / tvl * 100) if tvl > 0 else None
        def ft(fees, tvl): return (fees / tvl * 100) if tvl > 0 else None
        def fv(fees, vol): return (fees / vol * 100) if vol > 0 else None

        aero = aero_weekly_pair.get(ep)
        aero_vt = aero_ft = aero_fv = None
        if aero and aero["tvl"] > 0:
            aero_vt = aero["vol"] / aero["tvl"] * 100
            aero_ft = aero["fees"] / aero["tvl"] * 100
            if aero["vol"] > 0:
                aero_fv = aero["fees"] / aero["vol"] * 100

        out.append({
            "epoch": ep,
            "aero_epoch": ep + 107,
            "epoch_start": ep_start,
            "days_in_epoch": n,
            "partial": n < 7,
            # V/T
            "pool_vt":   round(vt(pool_vol,   pool_tvl), 2)   if vt(pool_vol, pool_tvl)   is not None else None,
            "aero_vt":   round(aero_vt, 2)   if aero_vt   is not None else None,
            "manual_vt": round(vt(manual_vol, manual_tvl), 2) if vt(manual_vol, manual_tvl) is not None else None,
            "s1_vt":     round(vt(s1_vol,     s1_tvl), 2)     if vt(s1_vol,     s1_tvl)     is not None else None,
            "s2_vt":     round(vt(s2_vol,     s2_tvl), 2)     if vt(s2_vol,     s2_tvl)     is not None else None,
            # F/T
            "pool_ft":   round(ft(pool_fees,   pool_tvl), 4)   if ft(pool_fees, pool_tvl)   is not None else None,
            "aero_ft":   round(aero_ft, 4) if aero_ft is not None else None,
            "manual_ft": round(ft(manual_fees, manual_tvl), 4) if ft(manual_fees, manual_tvl) is not None else None,
            "s1_ft":     round(ft(s1_fees,     s1_tvl), 4)     if ft(s1_fees,     s1_tvl)     is not None else None,
            "s2_ft":     round(ft(s2_fees,     s2_tvl), 4)     if ft(s2_fees,     s2_tvl)     is not None else None,
            # F/V (pool-level)
            "pool_fv": round(fv(pool_fees, pool_vol), 4) if fv(pool_fees, pool_vol) is not None else None,
            "aero_fv": round(aero_fv, 4) if aero_fv is not None else None,
        })
    return out


def main():
    rows = list(csv.DictReader(open(CSV_PATH)))
    aero_weekly = load_aero_weekly()
    by_pair = defaultdict(list)
    for r in rows:
        by_pair[r["pair"]].append(r)

    pair_data = {}
    for pair in PAIRS:
        prs = sorted(by_pair[pair], key=lambda r: r["date"])
        epoch_rows = aggregate_to_epochs(prs, aero_weekly.get(pair, {}))

        # Daily F/V at pool level
        def daily_fv(r):
            vol = f(r["volume"]); fees = f(r["fees"])
            return (fees / vol * 100) if vol and vol > 0 else None

        pair_data[pair] = {
            # Daily V/T
            "daily_dates":   [r["date"] for r in prs],
            "daily_aero_vt":   [f(r["aero_volume_tvl_pct"]) for r in prs],
            "daily_manual_vt": [f(r["manual_vt_pct"]) for r in prs],
            "daily_strat1_vt": [f(r["strat1_vt_pct"]) for r in prs],
            "daily_strat2_vt": [f(r["strat2_vt_pct"]) for r in prs],
            # Daily F/T
            "daily_aero_ft":   [f(r["aero_fees_tvl_pct"]) for r in prs],
            "daily_manual_ft": [f(r["manual_ft_pct"]) for r in prs],
            "daily_strat1_ft": [f(r["strat1_ft_pct"]) for r in prs],
            "daily_strat2_ft": [f(r["strat2_ft_pct"]) for r in prs],
            # Daily F/V (pool-level only)
            "daily_pool_fv": [daily_fv(r) for r in prs],
            "daily_aero_fv": [f(r["aero_fees_volume_pct"]) for r in prs],
            # Epoch
            "epoch_labels":  [f"E{r['epoch']} ({r['epoch_start']})" for r in epoch_rows],
            "epoch_partial": [r["partial"] for r in epoch_rows],
            # Epoch V/T
            "epoch_aero_vt":   [r["aero_vt"]   for r in epoch_rows],
            "epoch_manual_vt": [r["manual_vt"] for r in epoch_rows],
            "epoch_strat1_vt": [r["s1_vt"]     for r in epoch_rows],
            "epoch_strat2_vt": [r["s2_vt"]     for r in epoch_rows],
            # Epoch F/T
            "epoch_aero_ft":   [r["aero_ft"]   for r in epoch_rows],
            "epoch_manual_ft": [r["manual_ft"] for r in epoch_rows],
            "epoch_strat1_ft": [r["s1_ft"]     for r in epoch_rows],
            "epoch_strat2_ft": [r["s2_ft"]     for r in epoch_rows],
            # Epoch F/V (pool-level)
            "epoch_pool_fv":   [r["pool_fv"]   for r in epoch_rows],
            "epoch_aero_fv":   [r["aero_fv"]   for r in epoch_rows],
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
  <title>Hydrex — V/T, F/T, F/V by Bucket</title>
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
      <h1 id="page-title">Volume / TVL by bucket</h1>
      <p class="subtitle" id="subtitle"></p>
    </div>
    <div class="topbar-meta">
      <a href="index.html">← Aero vs Hydrex</a> &nbsp;|&nbsp;
      <a href="bootstrap.html">Bootstrap</a> &nbsp;|&nbsp;
      <a href="ichi.html">ICHI Diagnostic</a>
    </div>
  </div>

  <div class="controls">
    <div class="control-group">
      <strong>Metric</strong>
      <label><input type="radio" name="metric" value="vt" checked> V/T</label>
      <label><input type="radio" name="metric" value="ft"> F/T</label>
      <label><input type="radio" name="metric" value="fv"> F/V <span class="note">(pool-level)</span></label>
    </div>
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
        <option value="0.3">Hide >0.3%</option>
        <option value="1">Hide >1%</option>
        <option value="5">Hide >5%</option>
        <option value="50">Hide >50%</option>
        <option value="300">Hide >300%</option>
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
  pool:   "#ff7b72",   // for F/V Hydrex pool line
}};

const TITLES = {{
  vt: "Volume / TVL by bucket",
  ft: "Fees / TVL by bucket",
  fv: "Fees / Volume (effective fee rate, pool-level)",
}};

const SUBTITLES = {{
  vt: {{
    daily: 'Daily V/T = daily volume / TVL × 100. Aero is weekly÷7 approximation.',
    epoch: 'Epoch V/T = epoch volume / avg TVL × 100. Aero uses real weekly numbers.',
  }},
  ft: {{
    daily: 'Daily F/T = daily fees / TVL × 100. Yield on capital per day.',
    epoch: 'Epoch F/T = epoch fees / avg TVL × 100. Yield on capital per week.',
  }},
  fv: {{
    daily: 'Daily F/V = pool fees / pool volume × 100. Effective fee rate. Per-bucket collapses to pool-level under active% approximation, so only Hydrex pool + Aero pool shown.',
    epoch: 'Epoch F/V = pool fees / pool volume × 100. Effective fee rate per week.',
  }},
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
  const metric = document.querySelector('input[name="metric"]:checked').value;  // vt | ft | fv
  const view  = document.querySelector('input[name="view"]:checked').value;     // daily | epoch
  const yscale = document.querySelector('input[name="yscale"]:checked').value;
  const ycap   = parseFloat(document.getElementById('ycap').value);
  const smooth = parseInt(document.getElementById('smooth').value);

  // Hide smoothing on epoch view
  document.getElementById('smooth-group').style.display = view === 'daily' ? '' : 'none';

  // Update header
  document.getElementById('page-title').textContent = TITLES[metric];
  document.getElementById('subtitle').textContent = SUBTITLES[metric][view];

  const container = document.getElementById('charts');
  container.innerHTML = '';

  for (const pair of PAIRS) {{
    const d = DATA[pair];
    const panel = document.createElement('div');
    panel.className = 'pool';

    // Pick the right data series based on metric + view
    let xLabels, aero, manual, s1, s2, pool, marker_size, partialFlags, is_fv;
    is_fv = (metric === 'fv');

    if (view === 'daily') {{
      xLabels = d.daily_dates;
      marker_size = 4;
      partialFlags = null;
      if (metric === 'vt') {{
        aero   = rollingAvg(d.daily_aero_vt,   smooth);
        manual = rollingAvg(d.daily_manual_vt, smooth);
        s1     = rollingAvg(d.daily_strat1_vt, smooth);
        s2     = rollingAvg(d.daily_strat2_vt, smooth);
      }} else if (metric === 'ft') {{
        aero   = rollingAvg(d.daily_aero_ft,   smooth);
        manual = rollingAvg(d.daily_manual_ft, smooth);
        s1     = rollingAvg(d.daily_strat1_ft, smooth);
        s2     = rollingAvg(d.daily_strat2_ft, smooth);
      }} else {{
        aero = rollingAvg(d.daily_aero_fv, smooth);
        pool = rollingAvg(d.daily_pool_fv, smooth);
      }}
    }} else {{
      xLabels = d.epoch_labels;
      marker_size = 9;
      partialFlags = d.epoch_partial;
      if (metric === 'vt') {{
        aero = d.epoch_aero_vt;   manual = d.epoch_manual_vt;
        s1   = d.epoch_strat1_vt; s2     = d.epoch_strat2_vt;
      }} else if (metric === 'ft') {{
        aero = d.epoch_aero_ft;   manual = d.epoch_manual_ft;
        s1   = d.epoch_strat1_ft; s2     = d.epoch_strat2_ft;
      }} else {{
        aero = d.epoch_aero_fv;   pool = d.epoch_pool_fv;
      }}
    }}

    panel.innerHTML = `
      <div class="pool-title">${{pair}}</div>
      <div class="pool-address">${{d.pool_addr}}</div>
      ${{view === 'epoch' && partialFlags && partialFlags.some(p => p) ? '<div class="note">Note: hollow markers = partial epoch (<7 days of data).</div>' : ''}}
      <div class="chart" id="chart-${{pair.replace('/','-')}}"></div>
    `;
    container.appendChild(panel);

    const cap = ycap > 0 ? ycap : Infinity;
    const clamp = arr => arr ? arr.map(v => v === null || isNaN(v) ? null : (v > cap ? null : v)) : null;

    function markers(arr, color) {{
      if (!partialFlags) return {{ size: marker_size, color }};
      return {{
        size: marker_size,
        color: partialFlags.map(p => p ? 'rgba(0,0,0,0)' : color),
        line: {{ color, width: 2 }},
      }};
    }}

    const unit = is_fv ? '%' : '%';
    const valFmt = is_fv ? '.4f' : '.1f';
    const yTitle = metric === 'vt' ? (view === 'epoch' ? 'V/T per epoch (%)' : 'V/T per day (%)')
                 : metric === 'ft' ? (view === 'epoch' ? 'F/T per epoch (%)' : 'F/T per day (%)')
                 : 'F/V (%)';

    let traces = [];
    if (is_fv) {{
      // Pool-level: 2 lines
      traces = [
        {{ x: xLabels, y: clamp(aero), mode: 'lines+markers', name: 'Aero pool',
           line: {{ color: COLORS.aero, width: 2.5 }}, marker: markers(aero, COLORS.aero),
           hovertemplate: 'Aero: %{{y:' + valFmt + '}}' + unit + '<extra></extra>' }},
        {{ x: xLabels, y: clamp(pool), mode: 'lines+markers', name: 'Hydrex pool',
           line: {{ color: COLORS.pool, width: 2.5 }}, marker: markers(pool, COLORS.pool),
           hovertemplate: 'Hydrex: %{{y:' + valFmt + '}}' + unit + '<extra></extra>' }},
      ];
    }} else {{
      traces = [
        {{ x: xLabels, y: clamp(aero),   mode: 'lines+markers', name: 'Aero',
           line: {{ color: COLORS.aero,   width: 2.5 }}, marker: markers(aero, COLORS.aero),
           hovertemplate: 'Aero: %{{y:' + valFmt + '}}' + unit + '<extra></extra>' }},
        {{ x: xLabels, y: clamp(manual), mode: 'lines+markers', name: 'Manual',
           line: {{ color: COLORS.manual, width: 2.5 }}, marker: markers(manual, COLORS.manual),
           hovertemplate: 'Manual: %{{y:' + valFmt + '}}' + unit + '<extra></extra>' }},
        {{ x: xLabels, y: clamp(s1),     mode: 'lines+markers', name: `Strat 1 (${{d.s1_name}})`,
           line: {{ color: COLORS.strat1, width: 2.5 }}, marker: markers(s1, COLORS.strat1),
           hovertemplate: 'Strat 1: %{{y:' + valFmt + '}}' + unit + '<extra></extra>' }},
        {{ x: xLabels, y: clamp(s2),     mode: 'lines+markers', name: `Strat 2 (${{d.s2_name}})`,
           line: {{ color: COLORS.strat2, width: 2.5 }}, marker: markers(s2, COLORS.strat2),
           hovertemplate: 'Strat 2: %{{y:' + valFmt + '}}' + unit + '<extra></extra>' }},
      ];
    }}

    const tickFmt = is_fv ? '.3f' : '.0f';

    const layout = {{
      margin: {{ l: 70, r: 30, t: 10, b: view === 'epoch' ? 90 : 50 }},
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
        title: {{ text: yTitle, font: {{ size: 12, color: '#9aa4b2' }} }},
        gridcolor: 'rgba(255,255,255,0.04)',
        zerolinecolor: 'rgba(255,255,255,0.08)',
        color: '#9aa4b2',
        tickformat: tickFmt,
        ticksuffix: '%',
      }},
      hovermode: 'x unified',
      legend: {{ orientation: 'h', x: 0.5, xanchor: 'center', y: -0.22,
                 font: {{ size: 11, color: '#e7ecf2' }} }},
    }};

    Plotly.newPlot(`chart-${{pair.replace('/','-')}}`, traces, layout, {{
      displayModeBar: true, scrollZoom: true, responsive: true,
      displaylogo: false, modeBarButtonsToRemove: ['lasso2d', 'select2d'],
    }});
  }}
}}

document.querySelectorAll('input[name="metric"], input[name="view"], input[name="yscale"], #ycap, #smooth').forEach(el =>
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
