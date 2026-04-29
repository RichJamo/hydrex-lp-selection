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


def get_pool_metrics(hydrex_epoch: int, pool_address: str) -> dict:
    """Pull TVL/vol/fees for a specific pool in a specific epoch."""
    r = requests.get(f"{HYDREX_EPOCH_API}/{hydrex_epoch}", timeout=20)
    r.raise_for_status()
    data = r.json()
    pools = data.get("pools", [])
    target = pool_address.lower()
    for p in pools:
        if (p.get("poolAddress") or "").lower() == target:
            return {
                "tvl_start_usd": float(p.get("startTvl") or 0),
                "tvl_end_usd": float(p.get("endTvl") or 0),
                "volume_usd": float(p.get("volume") or 0),
                "fees_usd": float(p.get("fees") or 0),
                "title": p.get("title", ""),
            }
    return {
        "tvl_start_usd": 0,
        "tvl_end_usd": 0,
        "volume_usd": 0,
        "fees_usd": 0,
        "title": "",
    }


def get_incentives_for_pool(pool_address: str, epoch_start_iso: str, epoch_end_iso: str) -> float:
    """Sum oHYDX rewards across campaigns whose window overlaps the epoch."""
    r = requests.get(CAMPAIGNS_API, timeout=20)
    r.raise_for_status()
    camps = r.json().get("campaigns", [])
    target = pool_address.lower()
    epoch_start = dt.datetime.fromisoformat(epoch_start_iso.replace("Z", "+00:00"))
    epoch_end = dt.datetime.fromisoformat(epoch_end_iso.replace("Z", "+00:00"))

    total_wei = 0
    for c in camps:
        if (c.get("poolId") or "").lower() != target:
            continue
        c_start = dt.datetime.fromisoformat((c.get("startTimestamp") or "").replace("Z", "+00:00"))
        c_end = dt.datetime.fromisoformat((c.get("endTimestamp") or "").replace("Z", "+00:00"))
        # campaign overlaps epoch if c_start < epoch_end AND c_end > epoch_start
        if c_start < epoch_end and c_end > epoch_start:
            total_wei += int(c.get("totalRewards", "0"))

    return total_wei / 1e18  # convert wei to oHYDX tokens


def compute_metrics(pool_address: str, hydrex_epoch: int, epoch_start_iso: str,
                    epoch_end_iso: str, hydx_price: float) -> dict:
    pool = get_pool_metrics(hydrex_epoch, pool_address)
    ohydx = get_incentives_for_pool(pool_address, epoch_start_iso, epoch_end_iso)
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
    """Regenerate bootstrap.html with the latest tracker data."""
    rows = []
    if TRACKER_CSV.exists():
        with open(TRACKER_CSV, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

    # Build the embedded data for the dashboard
    data_json = json.dumps(rows)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Hydrex Bootstrap LP Tracker</title>
<style>
  :root {{ --bg:#0d1117; --panel:#161b22; --border:#30363d; --text:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --green:#3fb950; --red:#f85149; }}
  body {{ margin:0; padding:24px; background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  h1 {{ margin:0 0 6px; font-size:20px; }}
  .subtitle {{ color:var(--muted); margin-bottom:18px; font-size:13px; }}
  .nav {{ margin-bottom:24px; }}
  .nav a {{ color:var(--accent); text-decoration:none; padding:6px 12px; border:1px solid var(--border); border-radius:6px; margin-right:8px; }}
  .nav a.active {{ background:var(--accent); color:var(--bg); border-color:var(--accent); }}
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
</style>
</head>
<body>
<h1>Hydrex Bootstrap LP Tracker</h1>
<div class="subtitle">Pools incentivized weekly via voting power direction. Wed 23:59 UTC measurement window.</div>

<div class="nav">
  <a href="index.html">Aero vs Hydrex</a>
  <a href="bootstrap.html" class="active">Bootstrap Tracker</a>
</div>

<div id="summary" class="summary"></div>

<table id="tracker">
  <thead>
    <tr>
      <th>Epoch</th>
      <th>Pair</th>
      <th class="num">TVL Avg</th>
      <th class="num">Volume</th>
      <th class="num">Fees</th>
      <th class="num">Incentives ($)</th>
      <th class="num">Fees/TVL</th>
      <th class="num">Vol/TVL</th>
      <th class="num">Fee Tier</th>
      <th class="num">$TVL / $Inc</th>
      <th class="num">$Fees / $Inc</th>
    </tr>
  </thead>
  <tbody id="tracker-body"></tbody>
</table>

<div class="footer">Updated automatically every Wednesday 23:59 UTC. <a href="data/bootstrap_tracker.csv" download style="color:var(--accent)">↓ Download CSV</a></div>

<script>
const ROWS = {data_json};

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

function render() {{
  const tbody = document.getElementById('tracker-body');
  if (!ROWS.length) {{
    tbody.innerHTML = '<tr><td colspan="11" class="empty">No bootstrap data yet. Tracker initializes after the first Wed 23:59 UTC measurement.</td></tr>';
    document.getElementById('summary').innerHTML = '<div class="card"><div class="card-label">Pools tracked</div><div class="card-value">0</div></div>';
    return;
  }}

  // Summary
  const totalIncentives = ROWS.reduce((s,r) => s + (Number(r.incentives_usd)||0), 0);
  const totalTvl = ROWS.reduce((s,r) => s + (Number(r.tvl_avg_usd)||0), 0);
  const totalFees = ROWS.reduce((s,r) => s + (Number(r.fees_usd)||0), 0);
  const avgTvlPerInc = totalIncentives > 0 ? totalTvl / totalIncentives : 0;
  const avgFeesPerInc = totalIncentives > 0 ? totalFees / totalIncentives : 0;

  document.getElementById('summary').innerHTML = `
    <div class="card"><div class="card-label">Pools Tracked</div><div class="card-value">${{ROWS.length}}</div><div class="card-sub">across ${{new Set(ROWS.map(r=>r.hydrex_epoch)).size}} epochs</div></div>
    <div class="card"><div class="card-label">Total Incentive Spend</div><div class="card-value">${{fmt(totalIncentives)}}</div><div class="card-sub">in oHYDX (USD value)</div></div>
    <div class="card"><div class="card-label">Avg $TVL / $Incentive</div><div class="card-value">${{ratio(avgTvlPerInc)}}</div><div class="card-sub">vs Aero benchmark ~$30</div></div>
    <div class="card"><div class="card-label">Avg $Fees / $Incentive</div><div class="card-value">${{ratio(avgFeesPerInc)}}</div><div class="card-sub">direct ROI on incentive</div></div>
  `;

  // Sort newest epoch first
  ROWS.sort((a,b) => Number(b.hydrex_epoch) - Number(a.hydrex_epoch) || (a.pair||'').localeCompare(b.pair||''));

  tbody.innerHTML = ROWS.map(r => `
    <tr>
      <td>${{r.hydrex_epoch}} <span style="color:var(--muted);font-size:11px">${{(r.epoch_start||'').slice(5)}}</span></td>
      <td><strong>${{r.pair}}</strong></td>
      <td class="num">${{fmt(r.tvl_avg_usd)}}</td>
      <td class="num">${{fmt(r.volume_usd)}}</td>
      <td class="num">${{fmt(r.fees_usd)}}</td>
      <td class="num">${{fmt(r.incentives_usd)}}</td>
      <td class="num">${{pct(r.fees_tvl_pct)}}</td>
      <td class="num">${{pct(r.volume_tvl_pct)}}</td>
      <td class="num">${{pct(r.fees_volume_pct)}}</td>
      <td class="num"><strong>${{ratio(r.tvl_per_incentive_usd)}}</strong></td>
      <td class="num"><strong>${{ratio(r.fees_per_incentive_usd)}}</strong></td>
    </tr>
  `).join('');
}}

render();
</script>
</body>
</html>
"""
    DASHBOARD_HTML.write_text(html)


def main():
    picks = json.loads(PICKS_FILE.read_text())
    weeks = picks.get("weeks", [])
    if not weeks:
        print("No bootstrap weeks configured", file=sys.stderr)
        sys.exit(1)

    # Process the latest week
    week = weeks[-1]
    hydrex_epoch = week["hydrex_epoch"]
    aero_epoch = week.get("aero_epoch", hydrex_epoch + 107)
    epoch_start = week["epoch_start"] + "T00:00:00Z"
    epoch_end = week["epoch_end"] + "T00:00:00Z"
    pools = week.get("pools", [])

    print(f"Bootstrap update: Hydrex epoch {hydrex_epoch} ({week['epoch_start']} → {week['epoch_end']})")
    print(f"Pools: {len(pools)}")

    hydx_price = get_hydx_price()
    print(f"HYDX price: ${hydx_price:.6f} → oHYDX = ${hydx_price * OHYDX_DISCOUNT:.6f}")

    rows_added = []
    for pool in pools:
        print(f"\n  Processing {pool['pair']} ({pool['pool_address']})...")
        m = compute_metrics(pool["pool_address"], hydrex_epoch, epoch_start, epoch_end, hydx_price)
        row = {
            "hydrex_epoch": hydrex_epoch,
            "aero_epoch": aero_epoch,
            "epoch_start": week["epoch_start"],
            "epoch_end": week["epoch_end"],
            "pair": pool["pair"],
            "pool_address": pool["pool_address"],
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
