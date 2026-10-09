#!/bin/bash
# One job-hunt cycle (8 Oct 2026): Naukri + Hirist, Workday top companies, Greenhouse/Lever/Ashby top companies.
# Usage: scripts/cycle.sh [morning]   ("morning" also refreshes the Naukri profile so recruiters see it as updated today)
cd "$(dirname "$0")/.." || exit 1
LOG="output/cycles/$(date +%Y%m%d-%H%M).log"; mkdir -p output/cycles
PY=.venv/bin/python
{
  echo "=== cycle $(date)"
  if [ "${1:-}" = "morning" ]; then $PY -m naukri_bot profile itskills; fi
  # Naukri and Hirist use separate browser profiles, so they run side by side
  :
  $PY -m naukri_bot portal hirist --max 15 &
  wait
  # Workday top companies, newest first, tailored resume per JD
  $PY -m naukri_bot workday-run --submit --days 3 --max 8
  # Greenhouse / Lever / Ashby top companies and funded startups: new fits only, at most 6 per cycle
  $PY - <<'EOF'
import yaml, logging, subprocess
logging.basicConfig(level=logging.ERROR)
from naukri_bot.ats.top_companies import survey
p = yaml.safe_load(open("profile/profile.yaml"))
done = open("output/applications.csv").read()
keep, st = survey(p, max_age_days=3)
import csv, collections, re
from datetime import datetime, timedelta
cutoff = (datetime.now() - timedelta(days=30)).isoformat()
per_co = collections.Counter(r["company"].lower() for r in csv.DictReader(open("output/applications.csv"))
                             if r["status"] == "applied" and r["timestamp"] >= cutoff)
new = [r for r in keep if r.apply_url.split("token=")[-1] not in done and r.url not in done and r.apply_url not in done
       and per_co[r.company.lower()] < 2                                     # at most 2 applications per company per month
       and not re.search(r"\binfra(structure)?\b|devops|\bsre\b|site reliability", r.title, re.I)][:6]
print("top boards:", st, "new fits:", [(r.company, r.title) for r in new])
if new:
    open("output/cycles/top-new.txt", "w").write("\n".join(f"{r.apply_url} | {r.title}" for r in new) + "\n")
    subprocess.run([".venv/bin/python", "-m", "naukri_bot", "company-apply", "--file", "output/cycles/top-new.txt",
                    "--submit", "--no-wait", "--browser-profile", "ats-profile-3"])
EOF
  echo "=== done $(date)"
} >> "$LOG" 2>&1
grep -hE "applied: [0-9]|submitted this run|-> (applied|needs_you)|new fits" "$LOG" | grep -v httpx | tail -20
