"""build_site.py — assemble the public dashboard site for GitHub Pages.

Regenerates the dashboards from the latest committed data plus live API pulls,
stamps a build-time "Updated at" badge on every page, and stages everything into
_site/ for the deploy-pages action.

Runs in CI on a schedule (every ~30 min). It does NOT modify or commit the
canonical tracker — any files it regenerates live only inside the deployed
artifact, so frequent refreshes never touch git history.
"""
import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "_site"

# Pages to publish. Missing files are skipped (e.g. picks.html until it exists).
PAGES = ["index.html", "picks.html", "retention.html", "bootstrap.html", "hydrex_pools.html"]

STAMP = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


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


def main():
    regenerate()

    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir()

    published = []
    for name in PAGES:
        src = ROOT / name
        if src.exists():
            (SITE / name).write_text(stamp_badge(src.read_text()))
            published.append(name)

    # index.html fetches data/aero_vs_hydrex_combined.csv at runtime, and pages link
    # to data/*.csv downloads — ship the data dir alongside.
    if (ROOT / "data").exists():
        shutil.copytree(ROOT / "data", SITE / "data", dirs_exist_ok=True)

    print(f"Built _site/ at {STAMP} — {len(published)} pages: {', '.join(published)}")


if __name__ == "__main__":
    main()
