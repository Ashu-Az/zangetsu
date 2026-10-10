#!/bin/bash
# Daytime job hunt, one site at a time at a steady human pace (no LinkedIn).
# Every page load and application is spaced 8-14 s apart (MIN_DELAY/MAX_DELAY); sites follow each other with an
# ordinary short gap. Naukri (refresh, NVites, early access, search) -> Hirist -> Indeed -> recruiter-email drafts.
# Usage: scripts/hunt.sh [search]   ("search" starts at the Naukri search, skipping refresh/NVites/early access)
cd "$(dirname "$0")/.." || exit 1
LOG="output/cycles/hunt-$(date +%Y%m%d-%H%M).log"; mkdir -p output/cycles
PY=.venv/bin/python
gap() { s=$(( 10 + RANDOM % 11 )); echo "--- next site in ${s}s $(date +%H:%M)"; sleep "$s"; }
{
  echo "=== hunt $(date)"
  if [ "${1:-}" != "search" ]; then
    $PY -m naukri_bot refresh
    $PY -m naukri_bot nvites --max 10
    $PY -m naukri_bot early-access --max 30
  fi
  $PY -m naukri_bot run --source all --only naukri --max 25 --assist
  gap
  $PY -m naukri_bot portal hirist --max 15
  gap
  $PY -m naukri_bot portal indeed --max 15
  $PY -m naukri_bot outreach --max 10
  echo "=== done $(date)"
} >> "$LOG" 2>&1
echo "$LOG"
