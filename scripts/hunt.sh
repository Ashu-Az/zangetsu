#!/bin/bash
# Daytime job hunt, one site at a time with human-like gaps (no LinkedIn).
# Naukri (refresh, NVites, early access, search) -> Hirist -> Indeed -> recruiter-email drafts.
# Usage: scripts/hunt.sh            (questions no rule can answer wait for you in the browser: --assist)
cd "$(dirname "$0")/.." || exit 1
LOG="output/cycles/hunt-$(date +%Y%m%d-%H%M).log"; mkdir -p output/cycles
PY=.venv/bin/python
gap() { s=$(( $1 + RANDOM % ($2 - $1 + 1) )); echo "--- break ${s}s $(date +%H:%M)"; sleep "$s"; }
{
  echo "=== hunt $(date)"
  $PY -m naukri_bot refresh
  $PY -m naukri_bot nvites --max 10
  gap 60 180
  $PY -m naukri_bot early-access --max 30
  gap 90 240
  $PY -m naukri_bot run --source all --only naukri --max 25 --assist
  gap 180 420
  $PY -m naukri_bot portal hirist --max 15
  gap 180 420
  $PY -m naukri_bot portal indeed --max 15
  $PY -m naukri_bot outreach --max 10
  echo "=== done $(date)"
} >> "$LOG" 2>&1
echo "$LOG"
