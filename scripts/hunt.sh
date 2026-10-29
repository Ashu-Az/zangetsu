#!/bin/bash
# Daytime job hunt, one site at a time at a steady human pace (no LinkedIn; paused sites are skipped).
# Every page load and application is spaced 8-14 s apart (MIN_DELAY/MAX_DELAY); sites follow each other with an
# ordinary short gap. Order: Naukri recommended -> refresh -> NVites -> early access -> Naukri search -> Hirist ->
# Indeed -> recruiter-email drafts -> Naukri "apply on company site" jobs (capture links, tailored resume per JD,
# fill the employer form; anything needing a CAPTCHA or sign-in is reported, not forced).
# Usage: scripts/hunt.sh [search|company]   ("search" skips recommended/refresh/NVites/early access;
#        "company" runs only the company-site stage)
cd "$(dirname "$0")/.." || exit 1
LOG="output/cycles/hunt-$(date +%Y%m%d-%H%M).log"; mkdir -p output/cycles
PY=.venv/bin/python
gap() { s=$(( 10 + RANDOM % 11 )); echo "--- next step in ${s}s $(date +%H:%M)"; sleep "$s"; }
{
  echo "=== hunt $(date)"
  if [ "${1:-}" != "company" ]; then
  if [ "${1:-}" != "search" ]; then
    $PY -m naukri_bot run --source recommended --only naukri --max 25 --assist --max-applicants 100000
    gap
    $PY -m naukri_bot refresh
    $PY -m naukri_bot nvites --max 10
    $PY -m naukri_bot early-access --max 30
    gap
  fi
  $PY -m naukri_bot run --source search --only naukri --max 25 --assist --max-applicants 100000
  gap
  $PY -m naukri_bot portal hirist --max 15
  gap
  $PY -m naukri_bot portal indeed --max 15
  $PY -m naukri_bot outreach --max 10
  gap
  fi
  # company-site jobs: capture each employer link, then apply with a resume tailored to its JD
  $PY -m naukri_bot run --only company --no-tailor --assist --max-applicants 100000
  $PY - <<'PYEOF'
import csv, datetime
today = datetime.date.today().isoformat()
done = open("output/applications.csv", encoding="utf-8").read()
rows = [r for r in csv.DictReader(open("output/company_site_jobs.csv", encoding="utf-8"))
        if r["timestamp"].startswith(today) and r.get("external_url") and r["external_url"] not in done.replace(r["external_url"], "", 1)]
open("output/cycles/company-today.txt", "w").write("".join(f'{r["external_url"]} | {r["title"]} | {r["naukri_url"]}\n' for r in rows))
print("company-site jobs to apply:", len(rows))
PYEOF
  [ -s output/cycles/company-today.txt ] && $PY -m naukri_bot company-apply --file output/cycles/company-today.txt --submit --no-wait --browser-profile "${ATS_PROFILE:-ats-profile-cycle}"
  echo "=== done $(date)"
} >> "$LOG" 2>&1
echo "$LOG"
