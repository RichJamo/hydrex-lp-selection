"""
weekly_bootstrap_update.py — Hydrex LP bootstrap measurement.

Runs every Wednesday 23:59 UTC. For the 2 pools Austin picked this week (in
bootstrap_picks.json), pulls per-epoch metrics from Hydrex APIs, computes
9 metrics including capital efficiency ($TVL/$Incentive) and ROI on
incentive spend ($Fees/$Incentive), appends a row per pool to
bootstrap_tracker.csv, and regenerates bootstrap.html.

Data sources:
  - Pool TVL / Volume / Fees: staging.api.hydrex.fi/stats/clamm-pool-epoch-data/{hydrex_epoch}
  - Incentive campaigns: incentives-api.hydrex.fi/campaigns
  - HYDX price (DEXScreener): used to convert oHYDX -> USD via HYDX*0.7
"""

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PICKS_FILE = SCRIPT_DIR / "bootstrap_picks.json"
TRACKER_CSV = SCRIPT_DIR / "data" / "bootstrap_tracker.csv"
DASHBOARD_HTML = SCRIPT_DIR / "bootstrap.html"

HYDREX_EPOCH_API = "https://staging.api.hydrex.fi/stats/clamm-pool-epoch-data"
CAMPAIGNS_API = "https://incentives-api.hydrex.fi/campaigns"
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search"

OHYDX_DISCOUNT = 0.7  # oHYDX price = HYDX * 0.7


def get_hydx_price() -> float:
    """Pull current HYDX price from DEXScreener."""
    r = requests.get(f"{DEXSCREENER_SEARCH}?q=HYDX%20base", timeout=15)
    r.raise_for_status()
    for p in r.json().get("pairs", []):
        if p.get("chainId") == "base" and p.get("baseToken", {}).get("symbol", "").upper() == "HYDX":
            price = float(p.get("priceUsd") or 0)
            if price > 0:
                return price
    raise RuntimeError("HYDX price not found on DEXScreener")


def fetch_epoch_data(hydrex_epoch: int) -> dict:
    """Fetch the FULL epoch data once. Returns dict keyed by pool_address (lowercase)."""
    r = requests.get(f"{HYDREX_EPOCH_API}/{hydrex_epoch}", timeout=60)
    r.raise_for_status()
    pools = r.json().get("pools", [])
    return {(p.get("poolAddress") or "").lower(): p for p in pools}


def fetch_campaigns() -> list:
    """Fetch ALL campaigns once. Returns list."""
    r = requests.get(CAMPAIGNS_API, timeout=60)
    r.raise_for_status()
    return r.json().get("campaigns", [])


def get_pool_metrics_from_cache(epoch_pools: dict, pool_address: str) -> dict:
    """Look up pool metrics from a pre-fetched epoch dict. No API call."""
    p = epoch_pools.get(pool_address.lower())
    if not p:
        return {"tvl_start_usd": 0, "tvl_end_usd": 0, "volume_usd": 0, "fees_usd": 0, "title": ""}
    return {
        "tvl_start_usd": float(p.get("startTvl") or 0),
        "tvl_end_usd": float(p.get("endTvl") or 0),
        "volume_usd": float(p.get("volume") or 0),
        "fees_usd": float(p.get("fees") or 0),
        "title": p.get("title", ""),
    }


def get_incentives_from_cache(campaigns: list, pool_address: str,
                               epoch_start_iso: str, epoch_end_iso: str) -> float:
    """Sum oHYDX rewards from a pre-fetched campaigns list. No API call."""
    target = pool_address.lower()
    epoch_start = dt.datetime.fromisoformat(epoch_start_iso.replace("Z", "+00:00"))
    epoch_end = dt.datetime.fromisoformat(epoch_end_iso.replace("Z", "+00:00"))

    total_wei = 0
    for c in campaigns:
        if (c.get("poolId") or "").lower() != target:
            continue
        c_start = dt.datetime.fromisoformat((c.get("startTimestamp") or "").replace("Z", "+00:00"))
        c_end = dt.datetime.fromisoformat((c.get("endTimestamp") or "").replace("Z", "+00:00"))
        if c_start < epoch_end and c_end > epoch_start:
            total_wei += int(c.get("totalRewards", "0"))
    return total_wei / 1e18


def lookup_pair_name(pool_address: str) -> str:
    """Fallback subgraph lookup for pair name when pool isn't in current epoch yet."""
    try:
        q = '{ pool(id: "%s") { token0 { symbol } token1 { symbol } } }' % pool_address.lower()
        r = requests.post("https://analytics-subgraph.hydrex.fi/", json={"query": q}, timeout=15)
        p = r.json().get("data", {}).get("pool")
        if p:
            return f"{p['token0']['symbol']}/{p['token1']['symbol']}"
    except Exception:
        pass
    return ""


def compute_metrics(pool_address: str, epoch_pools: dict, campaigns: list,
                    epoch_start_iso: str, epoch_end_iso: str, hydx_price: float) -> dict:
    """Compute all 9 metrics for one pool, using pre-fetched epoch + campaign caches."""
    pool = get_pool_metrics_from_cache(epoch_pools, pool_address)
    if not pool["title"]:
        # Pool not in epoch data (e.g., just-added, no activity yet) — derive from subgraph
        pool["title"] = lookup_pair_name(pool_address)
    ohydx = get_incentives_from_cache(campaigns, pool_address, epoch_start_iso, epoch_end_iso)
    incentives_usd = ohydx * hydx_price * OHYDX_DISCOUNT

    tvl_avg = (pool["tvl_start_usd"] + pool["tvl_end_usd"]) / 2 if pool["tvl_end_usd"] > 0 else pool["tvl_start_usd"]
    fees_tvl_pct = (pool["fees_usd"] / tvl_avg * 100) if tvl_avg > 0 else 0
    volume_tvl_pct = (pool["volume_usd"] / tvl_avg * 100) if tvl_avg > 0 else 0
    fees_volume_pct = (pool["fees_usd"] / pool["volume_usd"] * 100) if pool["volume_usd"] > 0 else 0
    tvl_per_inc = (tvl_avg / incentives_usd) if incentives_usd > 0 else 0
    fees_per_inc = (pool["fees_usd"] / incentives_usd) if incentives_usd > 0 else 0

    return {
        **pool,
        "tvl_avg_usd": tvl_avg,
        "ohydx_distributed": ohydx,
        "hydx_price_at_report": hydx_price,
        "incentives_usd": incentives_usd,
        "fees_tvl_pct": fees_tvl_pct,
        "volume_tvl_pct": volume_tvl_pct,
        "fees_volume_pct": fees_volume_pct,
        "tvl_per_incentive_usd": tvl_per_inc,
        "fees_per_incentive_usd": fees_per_inc,
    }


def append_row(row: dict):
    fieldnames = [
        "hydrex_epoch", "aero_epoch", "epoch_start", "epoch_end", "pair",
        "pool_address", "tvl_start_usd", "tvl_end_usd", "tvl_avg_usd",
        "volume_usd", "fees_usd", "ohydx_distributed", "hydx_price_at_report",
        "incentives_usd", "fees_tvl_pct", "volume_tvl_pct", "fees_volume_pct",
        "tvl_per_incentive_usd", "fees_per_incentive_usd",
    ]
    file_exists = TRACKER_CSV.exists() and TRACKER_CSV.stat().st_size > 0
    with open(TRACKER_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fieldnames})


def render_dashboard():
    """Regenerate bootstrap.html with two tabs: Table view + Dashboard chart view."""
    rows = []
    if TRACKER_CSV.exists():
        with open(TRACKER_CSV, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

    data_json = json.dumps(rows)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Hydrex Bootstrap LP Tracker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{ --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --green:#3fb950; --red:#f85149; --orange:#d29922; --purple:#bc8cff; --pink:#ff7b72; }}
  body {{ margin:0; padding:24px; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  h1 {{ margin:0 0 6px; font-size:20px; }}
  .subtitle {{ color:var(--muted); margin-bottom:18px; font-size:13px; }}
  .tab-nav {{ display:flex; gap:0; margin-bottom:24px; border-bottom:1px solid var(--border); }}
  .tab-btn {{ background:none; border:none; color:var(--muted); padding:10px 18px; cursor:pointer; font-size:13px; font-weight:600; text-transform:uppercase; letter-spacing:0.5px; border-bottom:2px solid transparent; transition:all 0.15s; }}
  .tab-btn:hover {{ color:var(--text); }}
  .tab-btn.active {{ color:var(--accent); border-bottom-color:var(--accent); }}
  .summary {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:24px; }}
  .card {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; }}
  .card-label {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:1px; margin-bottom:6px; }}
  .card-value {{ font-size:22px; font-weight:600; }}
  .card-sub {{ color:var(--muted); font-size:12px; margin-top:4px; }}
  table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--border); border-radius:10px; overflow:hidden; }}
  th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid var(--border); font-size:13px; }}
  th {{ background:rgba(255,255,255,0.03); color:var(--muted); font-weight:600; text-transform:uppercase; font-size:11px; letter-spacing:0.5px; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  tr:last-child td {{ border-bottom:none; }}
  .empty {{ text-align:center; color:var(--muted); padding:40px; }}
  .footer {{ margin-top:24px; color:var(--muted); font-size:11px; text-align:center; }}

  .dashboard-layout {{ display:grid; grid-template-columns:200px 1fr; gap:20px; }}
  .pool-toggles {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; height:fit-content; position:sticky; top:20px; }}
  .pool-toggles h3 {{ margin:0 0 12px; font-size:11px; text-transform:uppercase; letter-spacing:1px; color:var(--muted); }}
  .pool-toggle {{ display:flex; align-items:center; gap:8px; padding:6px 0; cursor:pointer; font-size:13px; }}
  .pool-toggle input {{ accent-color:var(--accent); }}
  .pool-toggle .swatch {{ width:10px; height:10px; border-radius:2px; flex-shrink:0; }}
  .charts-grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:16px; }}
  .chart-panel {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:16px; }}
  .chart-panel.full {{ grid-column:1 / -1; }}
  .chart-panel h3 {{ margin:0 0 8px; font-size:13px; font-weight:600; }}
  .chart-panel .chart-sub {{ color:var(--muted); font-size:11px; margin-bottom:10px; }}
  .chart-wrap {{ position:relative; height:220px; }}
</style>
</head>
<body>
<h1>Hydrex Bootstrap LP Tracker</h1>
<div class="subtitle">Pools incentivized weekly via voting power direction. Wed 22:30 UTC measurement (pre-epoch flip).</div>

<div style="margin-bottom:18px">
  <a href="index.html" style="color:var(--accent);text-decoration:none;padding:6px 12px;border:1px solid var(--border);border-radius:6px;margin-right:8px">Aero vs Hydrex</a>
  <a href="bootstrap.html" style="text-decoration:none;padding:6px 12px;border:1px solid var(--accent);background:var(--accent);color:var(--bg);border-radius:6px;margin-right:8px">Bootstrap Tracker</a>
</div>

<div class="tab-nav">
  <button class="tab-btn active" data-tab="table">Table</button>
  <button class="tab-btn" data-tab="dashboard">Dashboard</button>
</div>

<div id="summary" class="summary"></div>

<!-- Table tab -->
<div id="tab-table">
  <table id="tracker">
    <thead>
      <tr>
        <th>Epoch</th>
        <th>Pair</th>
        <th class="num">TVL Avg</th>
        <th class="num">Volume</th>
        <th class="num">Fees</th>
        <th class="num">Incentives ($)</th>
        <th class="num">$Fees / $TVL</th>
        <th class="num">$Vol / $TVL</th>
        <th class="num">Fee Tier</th>
        <th class="num">$TVL / $Inc</th>
        <th class="num">$Fees / $Inc</th>
      </tr>
    </thead>
    <tbody id="tracker-body"></tbody>
  </table>
</div>

<!-- Dashboard tab -->
<div id="tab-dashboard" style="display:none">
  <div class="dashboard-layout">
    <div class="pool-toggles">
      <h3>Pools</h3>
      <div id="toggle-list"></div>
    </div>
    <div class="charts-grid">
      <div class="chart-panel"><h3>TVL (avg)</h3><div class="chart-sub">Capital sourced per epoch</div><div class="chart-wrap"><canvas id="chart-tvl"></canvas></div></div>
      <div class="chart-panel"><h3>Fees</h3><div class="chart-sub">Revenue generated per epoch</div><div class="chart-wrap"><canvas id="chart-fees"></canvas></div></div>
      <div class="chart-panel"><h3>$TVL / $Incentive</h3><div class="chart-sub">Capital efficiency. Aero benchmark ≈ $30</div><div class="chart-wrap"><canvas id="chart-tvl-per-inc"></canvas></div></div>
      <div class="chart-panel"><h3>$Fees / $Incentive</h3><div class="chart-sub">Direct ROI on incentive spend</div><div class="chart-wrap"><canvas id="chart-fees-per-inc"></canvas></div></div>
      <div class="chart-panel full"><h3>$ Fees per $1 TVL</h3><div class="chart-sub">Weekly pool yield rate (in $)</div><div class="chart-wrap"><canvas id="chart-fees-tvl"></canvas></div></div>
    </div>
  </div>
</div>

<div class="footer">Updated weekly. <a href="data/bootstrap_tracker.csv" download style="color:var(--accent)">↓ Download CSV</a></div>

<script>
// Discontinued pools — kept in the CSV for history, but hidden from the dashboard.
// Add a pair here to drop it from the legend/charts/table without deleting its data.
const EXCLUDED_PAIRS = new Set(['SOSO/USDC','MAG7.ssi/USDC','cbMEGA/WETH','deSPXA/USDC']);
const ALL_ROWS = {data_json};
const ROWS = ALL_ROWS.filter(r => !EXCLUDED_PAIRS.has(r.pair));

const POOL_COLORS = ['#58a6ff','#3fb950','#d29922','#bc8cff','#ff7b72','#f85149','#39d4cf','#ff9f43','#ff6b9d','#a5d6ff'];

function fmt(n, dec=2) {{
  if (n === '' || n === null || n === undefined || isNaN(n)) return '–';
  n = Number(n);
  if (Math.abs(n) >= 1_000_000) return '$' + (n/1_000_000).toFixed(dec) + 'M';
  if (Math.abs(n) >= 1_000) return '$' + (n/1_000).toFixed(dec) + 'K';
  return '$' + n.toFixed(dec);
}}
function pct(n) {{
  if (n === '' || n === null || n === undefined || isNaN(n)) return '–';
  return Number(n).toFixed(2) + '%';
}}
function ratio(n) {{
  if (n === '' || n === null || n === undefined || isNaN(n) || Number(n) === 0) return '–';
  return '$' + Number(n).toFixed(2);
}}
// Smart $-per-$ formatter: decimal precision scales with magnitude
function ratioSmall(n) {{
  if (n === '' || n === null || n === undefined || isNaN(n) || Number(n) === 0) return '–';
  n = Number(n);
  if (Math.abs(n) >= 1) return '$' + n.toFixed(2);
  if (Math.abs(n) >= 0.01) return '$' + n.toFixed(4);
  return '$' + n.toFixed(5);
}}

// === Active rows (filtered by toggle state) ===
function activeRows() {{
  // If no toggles set up yet, treat all as active
  if (!Object.keys(poolMeta).length) return ROWS;
  return ROWS.filter(r => poolMeta[r.pair] && poolMeta[r.pair].active);
}}

// === Summary cards (reactive to pool toggles) ===
function renderSummary() {{
  const rows = activeRows();
  if (!rows.length) {{
    document.getElementById('summary').innerHTML = '<div class="card"><div class="card-label">Pools tracked</div><div class="card-value">0</div><div class="card-sub">No active pools</div></div>';
    return;
  }}
  const totalIncentives = rows.reduce((s,r) => s + (Number(r.incentives_usd)||0), 0);
  const totalTvl = rows.reduce((s,r) => s + (Number(r.tvl_avg_usd)||0), 0);
  const totalFees = rows.reduce((s,r) => s + (Number(r.fees_usd)||0), 0);
  const avgTvlPerInc = totalIncentives > 0 ? totalTvl / totalIncentives : 0;
  const avgFeesPerInc = totalIncentives > 0 ? totalFees / totalIncentives : 0;
  const activePools = new Set(rows.map(r => r.pair)).size;
  const activeEpochs = new Set(rows.map(r => r.hydrex_epoch)).size;
  document.getElementById('summary').innerHTML = `
    <div class="card"><div class="card-label">Active Pool-Epochs</div><div class="card-value">${{rows.length}}</div><div class="card-sub">${{activePools}} pool${{activePools===1?'':'s'}} × ${{activeEpochs}} epoch${{activeEpochs===1?'':'s'}}</div></div>
    <div class="card"><div class="card-label">Total Incentive Spend</div><div class="card-value">${{fmt(totalIncentives)}}</div><div class="card-sub">selected pools (USD)</div></div>
    <div class="card"><div class="card-label">Avg $TVL / $Incentive</div><div class="card-value">${{ratio(avgTvlPerInc)}}</div><div class="card-sub">vs Aero benchmark ≈ $30</div></div>
    <div class="card"><div class="card-label">Avg $Fees / $Incentive</div><div class="card-value">${{ratio(avgFeesPerInc)}}</div><div class="card-sub">direct ROI on incentive</div></div>
  `;
}}

// === Table tab (always shows ALL rows regardless of toggle state) ===
function renderTable() {{
  const tbody = document.getElementById('tracker-body');
  if (!ROWS.length) {{
    tbody.innerHTML = '<tr><td colspan="11" class="empty">No bootstrap data yet. Tracker initializes after the first weekly measurement.</td></tr>';
    return;
  }}
  const sorted = [...ROWS].sort((a,b) => Number(b.hydrex_epoch) - Number(a.hydrex_epoch) || (a.pair||'').localeCompare(b.pair||''));
  tbody.innerHTML = sorted.map(r => `
    <tr>
      <td>${{r.hydrex_epoch}} <span style="color:var(--muted);font-size:11px">${{(r.epoch_start||'').slice(5)}}</span></td>
      <td><strong>${{r.pair}}</strong></td>
      <td class="num">${{fmt(r.tvl_avg_usd)}}</td>
      <td class="num">${{fmt(r.volume_usd)}}</td>
      <td class="num">${{fmt(r.fees_usd)}}</td>
      <td class="num">${{fmt(r.incentives_usd)}}</td>
      <td class="num">${{ratioSmall(Number(r.fees_tvl_pct)/100)}}</td>
      <td class="num">${{ratioSmall(Number(r.volume_tvl_pct)/100)}}</td>
      <td class="num">${{pct(r.fees_volume_pct)}}</td>
      <td class="num"><strong>${{ratio(r.tvl_per_incentive_usd)}}</strong></td>
      <td class="num"><strong>${{ratio(r.fees_per_incentive_usd)}}</strong></td>
    </tr>
  `).join('');
}}

// === Dashboard tab ===
const charts = {{}};
const poolMeta = {{}};   // pair => {{ color, active }}

function buildPoolMeta() {{
  const pairs = [...new Set(ROWS.map(r => r.pair))].sort();
  pairs.forEach((pair, idx) => {{
    poolMeta[pair] = poolMeta[pair] || {{
      color: POOL_COLORS[idx % POOL_COLORS.length],
      active: true,
    }};
  }});
}}

function renderToggles() {{
  const list = document.getElementById('toggle-list');
  list.innerHTML = Object.entries(poolMeta).map(([pair, meta]) => `
    <label class="pool-toggle">
      <input type="checkbox" data-pair="${{pair}}" ${{meta.active ? 'checked' : ''}} />
      <span class="swatch" style="background:${{meta.color}}"></span>
      <span>${{pair}}</span>
    </label>
  `).join('');
  list.querySelectorAll('input[type=checkbox]').forEach(cb => {{
    cb.addEventListener('change', e => {{
      poolMeta[e.target.dataset.pair].active = e.target.checked;
      updateAllCharts();
      renderSummary();  // top cards update with toggled pools
    }});
  }});
}}

function buildSeries(metricKey, transform) {{
  // Group by pool, ordered by epoch ascending. Optional `transform` reshapes raw values.
  const epochs = [...new Set(ROWS.map(r => Number(r.hydrex_epoch)))].sort((a,b) => a-b);
  const datasets = Object.entries(poolMeta)
    .filter(([_, m]) => m.active)
    .map(([pair, m]) => {{
      const data = epochs.map(e => {{
        const row = ROWS.find(r => r.pair === pair && Number(r.hydrex_epoch) === e);
        if (!row) return null;
        const v = Number(row[metricKey]);
        return transform ? transform(v) : v;
      }});
      return {{
        label: pair,
        data: data,
        borderColor: m.color,
        backgroundColor: m.color + '33',
        tension: 0.2,
        spanGaps: true,
        pointRadius: 4,
        pointHoverRadius: 6,
      }};
    }});
  return {{ labels: epochs.map(e => 'Ep ' + e), datasets: datasets }};
}}

function chartOpts(yLabel, formatter) {{
  return {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ display:false }},
      tooltip: {{
        callbacks: {{
          label: ctx => ctx.dataset.label + ': ' + (ctx.parsed.y === null ? '–' : formatter(ctx.parsed.y))
        }}
      }}
    }},
    scales: {{
      x: {{ ticks:{{ color:'#8b949e', font:{{ size:11 }}}}, grid:{{ color:'#30363d' }} }},
      y: {{ ticks:{{ color:'#8b949e', font:{{ size:11 }}, callback: v => formatter(v) }}, grid:{{ color:'#30363d' }}, title:{{ display:true, text:yLabel, color:'#8b949e', font:{{ size:11 }}}} }}
    }}
  }};
}}

function dollarFmt(v) {{
  if (Math.abs(v) >= 1_000_000) return '$' + (v/1_000_000).toFixed(1) + 'M';
  if (Math.abs(v) >= 1_000) return '$' + (v/1_000).toFixed(1) + 'K';
  return '$' + Number(v).toFixed(2);
}}

function dollarFmtSmall(v) {{
  // For small ratios like fees-per-tvl: scale precision with magnitude
  if (v === null || isNaN(v)) return '–';
  if (Math.abs(v) >= 1) return '$' + Number(v).toFixed(2);
  if (Math.abs(v) >= 0.01) return '$' + Number(v).toFixed(4);
  return '$' + Number(v).toFixed(5);
}}

function buildAllCharts() {{
  const configs = [
    // [canvas_id, csv_field, y_label, formatter, optional_transform]
    ['chart-tvl', 'tvl_avg_usd', 'TVL (USD)', dollarFmt, null],
    ['chart-fees', 'fees_usd', 'Fees (USD)', dollarFmt, null],
    ['chart-tvl-per-inc', 'tvl_per_incentive_usd', '$ TVL per $1 incentive', dollarFmt, null],
    ['chart-fees-per-inc', 'fees_per_incentive_usd', '$ Fees per $1 incentive', dollarFmtSmall, null],
    ['chart-fees-tvl', 'fees_tvl_pct', '$ Fees per $1 TVL', dollarFmtSmall, v => v / 100],
  ];
  configs.forEach(([id, metric, yLabel, fmt, transform]) => {{
    const ctx = document.getElementById(id);
    if (!ctx) return;
    if (charts[id]) charts[id].destroy();
    charts[id] = new Chart(ctx, {{
      type:'line',
      data: buildSeries(metric, transform),
      options: chartOpts(yLabel, fmt),
    }});
  }});
}}

function updateAllCharts() {{
  buildAllCharts();
}}

// === Tab switching ===
function setupTabs() {{
  document.querySelectorAll('.tab-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const tab = btn.dataset.tab;
      document.getElementById('tab-table').style.display = tab === 'table' ? '' : 'none';
      document.getElementById('tab-dashboard').style.display = tab === 'dashboard' ? '' : 'none';
      // Lazy-build charts on first dashboard click (Chart.js needs visible canvas)
      if (tab === 'dashboard' && Object.keys(charts).length === 0 && ROWS.length) {{
        buildAllCharts();
      }}
    }});
  }});
}}

// === Init ===
buildPoolMeta();   // populate before renderSummary so toggle state is read correctly
renderSummary();
renderTable();
renderToggles();
setupTabs();
</script>
</body>
</html>
"""
    DASHBOARD_HTML.write_text(html)


def select_week_to_measure(weeks: list) -> dict:
    """Pick the week whose epoch is CLOSING at script time.

    Designed to run Wed 23:59 UTC just before epoch flip. Returns the week
    containing 'now'. Falls back gracefully if no exact match:
      - if 'now' is before any week: error (no data yet)
      - if 'now' is after all weeks: most recent week
      - allows up to 24h grace after epoch_end so late-running cron still
        measures the just-closed epoch instead of the upcoming one.

    Critical: do NOT default to weeks[-1] — Austin adds upcoming epochs to
    the picks file BEFORE the cron runs, so weeks[-1] is the NEXT epoch
    (with no data yet), not the one we want to measure.
    """
    now = dt.datetime.now(dt.timezone.utc)
    GRACE = dt.timedelta(hours=24)

    for w in weeks:
        start = dt.datetime.fromisoformat(w["epoch_start"] + "T00:00:00+00:00")
        end   = dt.datetime.fromisoformat(w["epoch_end"]   + "T00:00:00+00:00")
        # Window: from start, through end + 24h grace
        if start <= now < end + GRACE:
            return w

    # If we're past every configured week, measure the most recent one
    weeks_sorted = sorted(weeks, key=lambda w: w["epoch_start"])
    if now >= dt.datetime.fromisoformat(weeks_sorted[-1]["epoch_end"] + "T00:00:00+00:00"):
        return weeks_sorted[-1]

    # 'now' is before any configured week — nothing to measure
    raise RuntimeError(
        f"No bootstrap week covers current time {now.isoformat()}. "
        f"Earliest configured: {weeks_sorted[0]['epoch_start']}"
    )


def row_already_recorded(hydrex_epoch: int, pool_address: str) -> bool:
    """Check if tracker already has this (epoch, pool) row with non-stub data."""
    if not TRACKER_CSV.exists():
        return False
    with open(TRACKER_CSV, newline="") as f:
        for r in csv.DictReader(f):
            if (int(r.get("hydrex_epoch") or 0) == hydrex_epoch
                and (r.get("pool_address") or "").lower() == pool_address.lower()):
                # Only treat as recorded if it has real volume/TVL (not a 0-stub)
                try:
                    if float(r.get("volume_usd") or 0) > 0 or float(r.get("tvl_avg_usd") or 0) > 0:
                        return True
                except ValueError:
                    pass
    return False


def purge_stub_rows_for_epoch(hydrex_epoch: int) -> int:
    """Remove any zero-stub rows for the given epoch so they get re-recorded with real data.
    A stub row is one with volume_usd == 0 AND tvl_avg_usd == 0. Returns rows removed."""
    if not TRACKER_CSV.exists():
        return 0
    with open(TRACKER_CSV, newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    keep = []
    removed = 0
    for r in rows:
        if int(r.get("hydrex_epoch") or 0) == hydrex_epoch:
            try:
                vol = float(r.get("volume_usd") or 0)
                tvl = float(r.get("tvl_avg_usd") or 0)
                if vol == 0 and tvl == 0:
                    removed += 1
                    continue
            except ValueError:
                pass
        keep.append(r)
    if removed:
        with open(TRACKER_CSV, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(keep)
    return removed


def select_week_by_epoch(weeks: list, hydrex_epoch: int) -> dict:
    """Return the configured week for a specific hydrex_epoch (for backfills)."""
    for w in weeks:
        if int(w.get("hydrex_epoch", -1)) == hydrex_epoch:
            return w
    configured = ", ".join(str(w.get("hydrex_epoch")) for w in weeks)
    raise SystemExit(
        f"Epoch {hydrex_epoch} not in {PICKS_FILE.name}. Configured: {configured}"
    )


def preview_epoch(pools, epoch_pools, campaigns, epoch_start_iso, epoch_end_iso, hydx_price):
    """Print a live mid-epoch snapshot WITHOUT touching the tracker.

    Fees accrue over the epoch, but the incentive figure (campaign totalRewards)
    is the FULL-epoch amount, so so-far f/i and fee/TVL are understated. The
    'proj' columns pro-rate fees by 1/elapsed_fraction for a comparable
    full-epoch estimate. TVL is the live current value (accurate as a snapshot).
    """
    now = dt.datetime.now(dt.timezone.utc)
    start = dt.datetime.fromisoformat(epoch_start_iso.replace("Z", "+00:00"))
    end = dt.datetime.fromisoformat(epoch_end_iso.replace("Z", "+00:00"))
    total = (end - start).total_seconds()
    elapsed = max(0.0, min((now - start).total_seconds(), total))
    frac = elapsed / total if total > 0 else 1.0

    if now >= end:
        print(f"\nEpoch already closed — figures are final.")
    else:
        print(f"\n⚠ PARTIAL: {elapsed/86400:.1f} of {total/86400:.0f} days elapsed "
              f"({frac:.0%}). Fees so-far are understated vs the full incentive; "
              f"'proj' columns pro-rate fees by 1/{frac:.2f}.")

    hdr = (f"{'PAIR':16} {'TVL now':>10} {'fees s/f':>9} {'incentive':>10} "
           f"{'f/i s/f':>8} {'f/i proj':>9} {'feeTVL proj':>12}")
    print("\n" + hdr)
    print("-" * len(hdr))
    rows = []
    for pool in pools:
        m = compute_metrics(pool["pool_address"], epoch_pools, campaigns,
                            epoch_start_iso, epoch_end_iso, hydx_price)
        pair = pool.get("pair") or m.get("title") or pool["pool_address"][:10]
        fi_sf = m["fees_per_incentive_usd"]
        rows.append((pair, m, fi_sf, fi_sf / frac if frac > 0 else fi_sf,
                     m["fees_tvl_pct"] / frac if frac > 0 else m["fees_tvl_pct"]))
    for pair, m, fi_sf, fi_proj, feetvl_proj in sorted(rows, key=lambda x: -x[3]):
        print(f"{pair[:16]:16} {m['tvl_end_usd']:>10,.0f} {m['fees_usd']:>9,.0f} "
              f"{m['incentives_usd']:>10,.0f} {fi_sf:>8.2f} {fi_proj:>9.2f} {feetvl_proj:>10.2f}%")
    print("-" * len(hdr))
    print("(read-only — tracker not modified)")


def main():
    ap = argparse.ArgumentParser(description="Record per-epoch bootstrap outcomes.")
    ap.add_argument(
        "--epoch", type=int, default=None,
        help="Hydrex epoch to measure (backfill). Default: auto-select the closing epoch.",
    )
    ap.add_argument(
        "--preview", action="store_true",
        help="Read-only mid-epoch snapshot: print live metrics WITHOUT writing the tracker "
             "or dashboard. Fees/f-i are partial; a pro-rated projection is also shown.",
    )
    args = ap.parse_args()

    picks = json.loads(PICKS_FILE.read_text())
    weeks = picks.get("weeks", [])
    if not weeks:
        print("No bootstrap weeks configured", file=sys.stderr)
        sys.exit(1)

    # --epoch backfills a specific past epoch; otherwise auto-select the CLOSING
    # epoch — not weeks[-1], which is typically the upcoming one.
    week = select_week_by_epoch(weeks, args.epoch) if args.epoch is not None \
        else select_week_to_measure(weeks)
    hydrex_epoch = week["hydrex_epoch"]
    aero_epoch = week.get("aero_epoch", hydrex_epoch + 107)
    epoch_start = week["epoch_start"] + "T00:00:00Z"
    epoch_end = week["epoch_end"] + "T00:00:00Z"
    pools = week.get("pools", [])

    mode = "PREVIEW (read-only, partial)" if args.preview else "update"
    print(f"Bootstrap {mode}: Hydrex epoch {hydrex_epoch} ({week['epoch_start']} → {week['epoch_end']})")
    print(f"Pools: {len(pools)}")

    # Clear any zero-stub rows from earlier botched runs of this epoch.
    # Skipped in preview — preview must never mutate the tracker.
    if not args.preview:
        purged = purge_stub_rows_for_epoch(hydrex_epoch)
        if purged:
            print(f"Purged {purged} stub row(s) for epoch {hydrex_epoch} — will re-record with real data")

    # Fetch heavy endpoints ONCE, reuse across all pools (was 2N+1 calls, now exactly 3)
    print("\nFetching APIs (3 calls total, regardless of pool count)...")
    hydx_price = get_hydx_price()
    print(f"  HYDX: ${hydx_price:.6f} → oHYDX = ${hydx_price * OHYDX_DISCOUNT:.6f}")
    epoch_pools = fetch_epoch_data(hydrex_epoch)
    print(f"  Epoch {hydrex_epoch} data: {len(epoch_pools)} pools cached")
    campaigns = fetch_campaigns()
    print(f"  Campaigns: {len(campaigns)} cached")

    if args.preview:
        preview_epoch(pools, epoch_pools, campaigns, epoch_start, epoch_end, hydx_price)
        return

    rows_added = []
    for pool in pools:
        # Pool address is the canonical key — pair name is just a display label
        # and gets auto-derived from epoch data or subgraph
        addr = pool["pool_address"]
        if row_already_recorded(hydrex_epoch, addr):
            print(f"\n  Skipping {addr} — already recorded for epoch {hydrex_epoch}")
            continue
        print(f"\n  Processing {addr}...")
        m = compute_metrics(addr, epoch_pools, campaigns, epoch_start, epoch_end, hydx_price)
        # Display pair: prefer config label, fallback to derived title
        pair_display = pool.get("pair") or m.get("title") or "?"
        row = {
            "hydrex_epoch": hydrex_epoch,
            "aero_epoch": aero_epoch,
            "epoch_start": week["epoch_start"],
            "epoch_end": week["epoch_end"],
            "pair": pair_display,
            "pool_address": addr,
            "tvl_start_usd": round(m["tvl_start_usd"], 2),
            "tvl_end_usd": round(m["tvl_end_usd"], 2),
            "tvl_avg_usd": round(m["tvl_avg_usd"], 2),
            "volume_usd": round(m["volume_usd"], 2),
            "fees_usd": round(m["fees_usd"], 2),
            "ohydx_distributed": round(m["ohydx_distributed"], 4),
            "hydx_price_at_report": round(hydx_price, 6),
            "incentives_usd": round(m["incentives_usd"], 2),
            "fees_tvl_pct": round(m["fees_tvl_pct"], 4),
            "volume_tvl_pct": round(m["volume_tvl_pct"], 4),
            "fees_volume_pct": round(m["fees_volume_pct"], 4),
            "tvl_per_incentive_usd": round(m["tvl_per_incentive_usd"], 2),
            "fees_per_incentive_usd": round(m["fees_per_incentive_usd"], 4),
        }
        append_row(row)
        rows_added.append(row)
        print(f"    TVL avg: ${m['tvl_avg_usd']:,.0f} | Vol: ${m['volume_usd']:,.0f} | Fees: ${m['fees_usd']:,.2f}")
        print(f"    Incentives: {m['ohydx_distributed']:,.2f} oHYDX = ${m['incentives_usd']:,.2f}")
        print(f"    $TVL/$Inc: ${m['tvl_per_incentive_usd']:,.2f} | $Fees/$Inc: ${m['fees_per_incentive_usd']:,.4f}")

    render_dashboard()
    print(f"\nDashboard regenerated: {DASHBOARD_HTML}")
    print(f"Tracker rows added: {len(rows_added)}")


if __name__ == "__main__":
    main()
