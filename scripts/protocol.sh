#!/bin/bash
# Ashu's job-hunt protocol, run as parallel lanes (15 Nov 2026). Each lane uses its own browser profile, so they
# can run side by side; inside a lane the steps keep the human pace (60-120 s between steps).
#   naukri    1. Neo  2. recommended + NVites + early access  3. Naukri search  4. company-site forms of Naukri jobs
#             (the ONLY lane that touches Naukri: two Naukri sessions share one daily quota and caused the 29 Oct 403)
#   boards    5. Workday + top-company Greenhouse / Lever / Ashby boards (workday-profile, ats-profile-N)
#   wellfound 6. Wellfound (wellfound-profile)
#   hirist    6. Hirist (hirist-profile)
#   instahyre 6b. Instahyre (freelance-profile)
#   mail      7. recruiter emails, AFTER every lane: it sends through Gmail in freelance-profile, which Instahyre uses
# Each lane logs to the main log with a [lane] prefix. Usage: scripts/protocol.sh
cd "$(dirname "$0")/.." || exit 1
LOG="output/cycles/protocol-$(date +%Y%m%d-%H%M).log"; mkdir -p output/cycles
PY=.venv/bin/python
gap() { s=$(( 60 + RANDOM % 61 )); echo "--- next step in ${s}s $(date +%H:%M)"; sleep "$s"; }
# a browser profile no open window holds (forms left open for Ashu keep theirs locked)
free_profile() {
  for p in ats-profile-2 ats-profile-3 ats-profile-4 ats-profile-5 ats-profile-6 ats-profile-7 ats-profile-8 ats-profile; do
    lock="state/$p/SingletonLock"
    pid=$(readlink "$lock" 2>/dev/null | sed 's/.*-//')
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then echo "$p"; return; fi
  done
  echo ats-profile-manual
}
# run a lane: every output line goes to the main log with the lane name in front
lane() { name=$1; shift; { echo "=== [$name] start $(date +%H:%M)"; "$@"; echo "=== [$name] done $(date +%H:%M)"; } 2>&1 | sed -u "s/^/[$name] /" >> "$LOG"; }

naukri_lane() {
  echo "=== 1. Neo"
  $PY tools/neo_apply.py
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
  # background: it stays open for any forms left for Ashu; the lane must not wait on that
  [ -s output/cycles/company-today.txt ] && $PY -m naukri_bot company-apply --file output/cycles/company-today.txt --submit --no-wait --browser-profile "$(free_profile)" > "output/cycles/company-apply-$(date +%Y%m%d-%H%M).log" 2>&1 &
}

boards_lane() {
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
    subprocess.Popen([".venv/bin/python", "-m", "naukri_bot", "company-apply", "--file", "output/cycles/top-new.txt",
                    "--submit", "--no-wait", "--browser-profile", prof],
                     stdout=open(datetime.now().strftime("output/cycles/top-apply-%Y%m%d-%H%M.log"), "w"), stderr=subprocess.STDOUT,
                     start_new_session=True)
PYEOF
}

instahyre_lane() {
  # Instahyre is signed in (Google) inside state/freelance-profile; free that profile first (tools/drive.py holds it)
  pkill -f "tools/drive.py start"; sleep 3
  $PY tools/instahyre_apply.py --profile freelance-profile --max "${INSTAHYRE_MAX:-40}" --pages 3
}

echo "=== protocol $(date) (parallel lanes)" >> "$LOG"
lane naukri    naukri_lane &
sleep 20; lane boards    boards_lane &
sleep 20; lane wellfound $PY -m naukri_bot portal wellfound --max 15 &
sleep 20; lane hirist    $PY -m naukri_bot portal hirist --max 15 &
sleep 20; lane instahyre instahyre_lane &
wait $(jobs -p)
{
  echo "=== 7. recruiter emails (drafted, then sent through Gmail web)"
  # LinkedIn is paused (13 Nov 2026), so leads come from Naukri JD emails only
  $PY -m naukri_bot outreach --sources naukri --max 15
  bash tools/send_outreach.sh "output/outreach/$(date +%Y-%m-%d)"
  echo "=== done $(date)"
} 2>&1 | sed -u "s/^/[mail] /" >> "$LOG"
echo "$LOG"
