#!/bin/bash
# One job-hunt cycle: profile refresh, NVites, Naukri + Hirist, Naukri top-company sites, Workday and
# Greenhouse/Lever/Ashby top companies (tailored, grounded resume per job; verified submits).
# Usage: scripts/cycle.sh [morning]   ("morning" also refreshes the Naukri profile so recruiters see it as updated today)
cd "$(dirname "$0")/.." || exit 1
LOG="output/cycles/$(date +%Y%m%d-%H%M).log"; mkdir -p output/cycles
PY=.venv/bin/python
{
  echo "=== cycle $(date)"
  # 1. keep the Naukri profile "updated today" (re-saves the headline; skips if refreshed in the last 3 h)
  $PY -m naukri_bot refresh
  if [ "${1:-}" = "morning" ]; then $PY -m naukri_bot profile itskills; fi
  # 2. recruiter invitations first: the strongest leads (same filters, quota and cooldown as search)
  $PY -m naukri_bot nvites --max 10
  # 3. Naukri search and Hirist use separate browser profiles, so they run side by side
  $PY -m naukri_bot run --source all --max 30 --only naukri --no-llm --assist --max-applicants 100000 &
  $PY -m naukri_bot portal hirist --max 15 &
  wait
  # 4. priority companies' Naukri postings that apply on the company site -> inputs/naukri-top-company-sites.txt
  $PY -m naukri_bot top-company-sites --companies 12
  # 5. Workday top companies, newest first, tailored resume per JD
  $PY -m naukri_bot workday-run --submit --days 3 --max 8
  # Greenhouse / Lever / Ashby top companies and funded startups: new fits only, at most 3 per cycle
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
import os
extra = []
for f in ("inputs/naukri-top-company-sites.txt", "inputs/naukri-nvite-forms.txt"):
    if os.path.exists(f):
        extra += [l.strip() for l in open(f) if l.strip() and not l.startswith("#")]
extra = [l for l in extra if l.split(" | ")[0] not in done][:3]
new = [r for r in keep if r.apply_url.split("token=")[-1] not in done and r.url not in done and r.apply_url not in done
       and per_co[r.company.lower()] < 2                                     # at most 2 applications per company per month
       and not re.search(r"\binfra(structure)?\b|devops|\bsre\b|site reliability", r.title, re.I)][:3]
print("top boards:", st, "new fits:", [(r.company, r.title) for r in new])
lines = [f"{r.apply_url} | {r.title}" for r in new] + extra
if lines:
    # company-apply routes each URL to the right form adapter (Workday vs label-driven) and skips anything
    # already submitted or possibly submitted
    open("output/cycles/top-new.txt", "w").write("\n".join(lines) + "\n")
    subprocess.run([".venv/bin/python", "-m", "naukri_bot", "company-apply", "--file", "output/cycles/top-new.txt",
                    "--submit", "--no-wait", "--browser-profile", "ats-profile-cycle"])
EOF
  echo "=== done $(date)"
} >> "$LOG" 2>&1
grep -hE "applied: [0-9]|submitted this run|-> (applied|needs_you)|new fits" "$LOG" | grep -v httpx | tail -20
