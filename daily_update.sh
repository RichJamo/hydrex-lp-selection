#!/usr/bin/env bash
# Daily update: pull pool metrics + detect param changes + regenerate chart
set -e
cd "$(dirname "$0")"
PY=$(command -v python3)
LOG="daily_update.log"

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') === starting daily_update.sh" >> "$LOG"
"$PY" hydrex_daily_pull.py      >> "$LOG" 2>&1 || echo "  daily_pull FAILED" >> "$LOG"
"$PY" hydrex_param_changes.py   >> "$LOG" 2>&1 || echo "  param_changes FAILED" >> "$LOG"
"$PY" build_hydrex_pools_chart.py >> "$LOG" 2>&1 || echo "  chart_build FAILED" >> "$LOG"

# Auto-commit if anything changed (optional)
if git -C "$(pwd)" diff --quiet data/ hydrex_pools.html; then
  echo "  no changes to commit" >> "$LOG"
else
  git -C "$(pwd)" add data/hydrex_pools_daily.csv data/hydrex_param_changes.csv hydrex_pools.html
  git -C "$(pwd)" commit -m "Daily pool tracker update $(date +%Y-%m-%d)" >> "$LOG" 2>&1 || true
  git -C "$(pwd)" push >> "$LOG" 2>&1 || true
fi
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') === done" >> "$LOG"
