#!/bin/bash
# Ashu's job-hunt protocol (18 Oct 2026), one steady pass at a human pace (8-14 s between actions):
#   1. Naukri Neo suggestions        filtered card by card; Neo-tailored resume; recruiter questions answered
#                                     by the rules, then the model (API key)
#   2. Naukri recommended + NVites   apply-button jobs, fewest applicants first, model answers the questions
#   3. Naukri search                 same, fewest applicants first
#   4. Naukri "apply on company site" jobs: save the list, build a resume tailored to each JD (ATS keyword
#      coverage pushed to 90+ with skills Ashu really has), fill and submit the employer form
#   5. Startups and top companies found on the web (Greenhouse / Lever / Ashby boards) and Workday
#   6. Wellfound, Hirist                job boards
#   7. Recruiter emails (drafts)
# Usage: scripts/protocol.sh
cd "$(dirname "$0")/.." || exit 1
LOG="output/cycles/protocol-$(date +%Y%m%d-%H%M).log"; mkdir -p output/cycles
PY=.venv/bin/python
gap() { s=$(( 10 + RANDOM % 11 )); echo "--- next step in ${s}s $(date +%H:%M)"; sleep "$s"; }
# a browser profile no open window holds (forms left open for Ashu keep theirs locked)
free_profile() {
  for p in ats-profile-2 ats-profile-3 ats-profile-4 ats-profile-5 ats-profile-6 ats-profile-7 ats-profile-8 ats-profile; do
    lock="state/$p/SingletonLock"
    pid=$(readlink "$lock" 2>/dev/null | sed 's/.*-//')
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then echo "$p"; return; fi
  done
  echo ats-profile-manual
}
{
  echo "=== protocol $(date)"
  echo "=== 1. Neo"
  $PY -m naukri_bot neo --max 8
  gap
  echo "=== 2. recommended + NVites"
  $PY -m naukri_bot run --source recommended --only naukri --max 25 --max-applicants 100000
  $PY -m naukri_bot nvites --max 10
  $PY -m naukri_bot early-access --max 30
  gap
  echo "=== 3. Naukri search"
  $PY -m naukri_bot run --source search --only naukri --max 25 --max-applicants 100000
  gap
  echo "=== 4. company sites"
  $PY -m naukri_bot run --only company --no-tailor --max-applicants 100000
  $PY - <<'PYEOF'
import csv, datetime
today = datetime.date.today().isoformat()
done = open("output/applications.csv", encoding="utf-8").read()
rows = [r for r in csv.DictReader(open("output/company_site_jobs.csv", encoding="utf-8"))
        if r["timestamp"].startswith(today) and r.get("external_url") and r["external_url"] not in done.replace(r["external_url"], "", 1)]
open("output/cycles/company-today.txt", "w").write("".join(f'{r["external_url"]} | {r["title"]} | {r["naukri_url"]}\n' for r in rows))
print("company-site jobs to apply:", len(rows))
PYEOF
  [ -s output/cycles/company-today.txt ] && $PY -m naukri_bot company-apply --file output/cycles/company-today.txt --submit --no-wait --browser-profile "$(free_profile)"
  gap
  echo "=== 5. startups + top companies (web-found ATS boards, Workday)"
  $PY -m naukri_bot workday-run --submit --days 3 --max 8
  $PY - <<'PYEOF'
import yaml, logging, subprocess, csv, collections, re
from datetime import datetime, timedelta
logging.basicConfig(level=logging.ERROR)
from naukri_bot.ats.top_companies import survey
p = yaml.safe_load(open("profile/profile.yaml"))
done = open("output/applications.csv").read()
keep, st = survey(p, max_age_days=3)
cutoff = (datetime.now() - timedelta(days=30)).isoformat()
per_co = collections.Counter(r["company"].lower() for r in csv.DictReader(open("output/applications.csv"))
                             if r["status"] == "applied" and r["timestamp"] >= cutoff)
new = [r for r in keep if r.apply_url.split("token=")[-1] not in done and r.url not in done and r.apply_url not in done
       and per_co[r.company.lower()] < 2
       and not re.search(r"\binfra(structure)?\b|devops|\bsre\b|site reliability", r.title, re.I)][:8]
print("top boards:", st, "new fits:", [(r.company, r.title) for r in new])
if new:
    open("output/cycles/top-new.txt", "w").write("\n".join(f"{r.apply_url} | {r.title}" for r in new) + "\n")
    prof = subprocess.run(["bash", "-c", "source <(sed -n '/^free_profile()/,/^}/p' scripts/protocol.sh); free_profile"],
                          capture_output=True, text=True).stdout.strip() or "ats-profile-3"
    subprocess.run([".venv/bin/python", "-m", "naukri_bot", "company-apply", "--file", "output/cycles/top-new.txt",
                    "--submit", "--no-wait", "--browser-profile", prof])
PYEOF
  gap
  echo "=== 6. Wellfound, Hirist"
  $PY -m naukri_bot portal wellfound --max 15
  gap
  $PY -m naukri_bot portal hirist --max 15
  gap
  echo "=== 7. recruiter emails"
  $PY -m naukri_bot outreach --max 10
  echo "=== done $(date)"
} >> "$LOG" 2>&1
echo "$LOG"
