"""build_site.py — assemble the public dashboard site for GitHub Pages.

Regenerates the dashboards from the latest committed data plus live API pulls,
stamps a build-time "Updated at" badge on every page, and stages everything into
_site/ for the deploy-pages action.

Runs in CI on a schedule (every ~30 min). It does NOT modify or commit the
canonical tracker — any files it regenerates live only inside the deployed
artifact, so frequent refreshes never touch git history.
"""
import datetime as dt
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "_site"

# Pages to publish. Missing files are skipped (e.g. picks.html until it exists).
PAGES = ["index.html", "live.html", "picks.html", "retention.html", "bootstrap.html", "hydrex_pools.html"]

STAMP = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# --- Canonical navigation -----------------------------------------------------
# One nav bar for the whole site. Each page's own (inconsistent) nav is stripped
# and replaced with this, so tabs/names/order/position are identical everywhere.
NAV_TABS = [
    ("index.html", "Aero vs Hydrex"),
    ("live.html", "⟳ Live"),
    ("picks.html", "Selection"),
    ("retention.html", "Retention"),
    ("bootstrap.html", "Bootstrap"),
    ("hydrex_pools.html", "Hydrex Daily"),
]

# Existing nav wrappers to remove (first match only): the generated dashboards +
# index use this inline-styled div; hydrex_pools uses .topbar-meta.
_NAV_STRIP = [
    re.compile(r'<div style="margin-bottom:18px">.*?</div>', re.S),
    re.compile(r'<div class="topbar-meta">.*?</div>', re.S),
]


def canonical_nav(active: str) -> str:
    base = ("color:#58a6ff;text-decoration:none;padding:6px 12px;border:1px solid #30363d;"
            "border-radius:6px;margin-right:8px;display:inline-block;"
            "font:600 13px -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif")
    act = ";background:#58a6ff;color:#0d1117;border-color:#58a6ff"
    links = "".join(
        f'<a href="{href}" style="{base}{act if href == active else ""}">{label}</a>'
        for href, label in NAV_TABS
    )
    return f'<div style="padding:14px 0 18px;line-height:2.4">{links}</div>'


def normalize_nav(html: str, active: str) -> str:
    """Strip the page's own nav and inject the canonical one right after <body>."""
    for pat in _NAV_STRIP:
        html = pat.sub("", html, count=1)
    return re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + "\n" + canonical_nav(active),
                  html, count=1)


def stamp_badge(html: str) -> str:
    """Inject a small fixed 'Updated …' badge before </body>."""
    badge = (
        '<div style="position:fixed;bottom:8px;right:10px;background:#161b22;'
        'color:#8b949e;font:11px/1.4 -apple-system,Segoe UI,sans-serif;'
        'padding:4px 10px;border:1px solid #30363d;border-radius:8px;z-index:9999">'
        f'⟳ Updated {STAMP}</div>'
    )
    return html.replace("</body>", badge + "\n</body>", 1) if "</body>" in html else html + badge


def regenerate():
    """Rebuild the dashboards we own from the latest data (read-only; no commits)."""
    # Retention: refresh market totals (Hydrex epoch API) + rebuild retention.html.
    # sys.executable so we use the same interpreter locally (python3) and in CI.
    subprocess.run(
        [sys.executable, "retention_scorecard.py", "--refresh-market", "--no-color", "--no-html"],
        check=True, cwd=ROOT,
    )
    subprocess.run(
        [sys.executable, "retention_scorecard.py", "--no-color"],  # rebuild HTML from cached fees
        check=True, cwd=ROOT,
    )
    # Bootstrap: rebuild bootstrap.html from the committed tracker (does NOT record an epoch).
    import weekly_bootstrap_update as wb
    wb.render_dashboard()
    # Live: current-epoch snapshot from the Hydrex APIs (read-only; writes live.html only).
    subprocess.run([sys.executable, "weekly_bootstrap_update.py", "--live"], check=True, cwd=ROOT)


def main():
    regenerate()

    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()

    published = []
    for name in PAGES:
        src = ROOT / name
        if src.exists():
            (SITE / name).write_text(stamp_badge(normalize_nav(src.read_text(), name)))
            published.append(name)

    # index.html fetches data/aero_vs_hydrex_combined.csv at runtime, and pages link
    # to data/*.csv downloads — ship the data dir alongside.
    if (ROOT / "data").exists():
        shutil.copytree(ROOT / "data", SITE / "data", dirs_exist_ok=True)

    print(f"Built _site/ at {STAMP} — {len(published)} pages: {', '.join(published)}")


if __name__ == "__main__":
    main()
