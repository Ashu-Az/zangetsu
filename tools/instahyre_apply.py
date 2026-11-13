"""Apply to Instahyre jobs in bulk, the way a person does it, from the bot's own signed-in browser profile.

How it works (13 Nov 2026):
  1. Instahyre's search page only shows the first 30 results, but its JSON feed takes an offset:
     /api/v1/job_search?skills=<kw>&status=0&offset=<n>&limit=30  (called from the page, with the session cookie).
     For every keyword it reads up to --pages pages and keeps jobs whose title fits (GOOD) and is not excluded (BAD).
  2. Each job's public page has an "Apply now" button. The script opens it, skips jobs whose minimum experience is
     above --max-min-years, clicks Apply now and waits for "Application sent!".
  3. Some jobs apply on the company's own site (Workday, Zoho, Oracle...) or on LinkedIn; those show no confirmation
     and are logged as "external". LinkedIn is blocked in the bot's browsers unless LINKEDIN_ENABLED=1.

Sign in first (the candidate's own Google sign-in, never the bot):
    .venv/bin/python tools/instahyre_apply.py --login           # opens instahyre.com; sign in, then close the window
Then:
    .venv/bin/python tools/instahyre_apply.py --max 150
    .venv/bin/python tools/instahyre_apply.py --keywords "Python,LLM" --max 50 --dry-run

After each run, open the Instahyre inbox: recruiters often send a questionnaire link (/questionnaire/<job>/<id>)
within minutes. Answer it from the profile (years per skill, notice / last working day in the format asked, CTC).
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from naukri_bot.config import OUTPUT_DIR, STATE_DIR, Settings  # noqa: E402

GOOD = re.compile(r"python|\bai\b|\bml\b|machine learning|llm|gen ?ai|generative|full[ -]?stack|backend|back-end|"
                  r"software (engineer|developer)|\bsde\b|sde[- ]?1|node|react|next\.?js|data scien|nlp|agent|\brag\b|"
                  r"fastapi|django|forward deployed|founding engineer|applied ai|mern|golang|computer vision|"
                  r"typescript|flask|prompt engineer", re.I)
BAD = re.compile(r"senior|\bsr\.?\b|lead|manager|head|director|principal|staff|architect|\bvp\b|intern|trainee|java\b|"
                 r"\.net|c#|php|salesforce|\bsap\b|\bqa\b|test|devops|\bsre\b|ios|android|flutter|sales|marketing|"
                 r"business|partnership|recruit|\bhr\b|designer|ui/ux|data engineer|embedded|mobile|\biii\b|\bii\b|"
                 r"expert|math|physics|chemistry|biology|curriculum", re.I)
KEYWORDS = ["Python", "Generative AI", "LLM", "Artificial Intelligence", "Machine Learning", "Full Stack", "Node.js",
            "React.js", "FastAPI", "Django", "Next.js", "Data Science", "NLP", "LangChain", "Software Engineer", "Backend"]


def _browser(profile_dir: str):
    Settings.browser_profile_dir = property(lambda self: STATE_DIR / profile_dir)
    from naukri_bot.browser import Browser
    return Browser(Settings())


def fetch(page, kw: str, offset: int) -> list:
    q = urllib.parse.quote(kw)
    return page.evaluate(f"""async () => {{
        const r = await fetch('/api/v1/job_search?company_size=0&job_type=0&skills={q}&status=0&offset={offset}&limit=30',
                              {{credentials: 'include'}});
        if (!r.ok) return [];
        const j = await r.json();
        return (j.objects || []).map(o => [o.id, o.title, o.employer.company_name, o.public_url, o.interview_status]);
    }}""")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--login", action="store_true", help="open Instahyre for the candidate to sign in")
    ap.add_argument("--keywords", default=",".join(KEYWORDS))
    ap.add_argument("--pages", type=int, default=10, help="result pages (30 jobs each) per keyword")
    ap.add_argument("--max", type=int, default=120, help="stop after this many applications")
    ap.add_argument("--max-min-years", type=int, default=3, help="skip jobs whose minimum experience is higher")
    ap.add_argument("--profile", default="instahyre-profile", help="browser profile folder under state/")
    ap.add_argument("--dry-run", action="store_true", help="list the jobs it would apply to")
    args = ap.parse_args()

    log_path = OUTPUT_DIR / "cycles" / f"instahyre-{datetime.now():%Y%m%d-%H%M}.json"
    log: list = []
    with _browser(args.profile) as b:
        p = b.page
        p.goto("https://www.instahyre.com/candidate/opportunities/?matching=true", wait_until="domcontentloaded")
        if args.login or "login" in p.url:
            print("Sign in to Instahyre in this window, then close it.")
            while b.context.pages and not all(pg.is_closed() for pg in b.context.pages):
                time.sleep(3)
            return 0
        time.sleep(5)
        cands: dict = {}
        for kw in [k.strip() for k in args.keywords.split(",") if k.strip()]:
            for off in range(0, args.pages * 30, 30):
                rows = fetch(p, kw, off)
                if not rows:
                    break
                for jid, title, co, url, status in rows:
                    if jid not in cands and status == 0 and GOOD.search(title) and not BAD.search(title):
                        cands[jid] = (co, title, url)
                time.sleep(0.4)
        print(f"{len(cands)} candidate jobs")
        applied = 0
        for co, title, url in cands.values():
            if applied >= args.max:
                break
            if args.dry_run:
                log.append((co, title, "would apply"))
                continue
            try:
                p.goto(url, wait_until="domcontentloaded")
                try:
                    p.locator("button:visible, a:visible").filter(
                        has_text=re.compile(r"Apply now|Application sent")).first.wait_for(timeout=15000)
                except Exception:
                    pass
                body = p.inner_text("body")
                if "Application sent" in body:
                    log.append((co, title, "already"))
                    continue
                m = re.search(r"(\d+)\s*-\s*(\d+)\s*Years", body)
                if m and int(m.group(1)) > args.max_min_years:
                    log.append((co, title, "skip " + m.group(0)))
                    continue
                btn = p.locator("button:visible, a:visible").filter(has_text=re.compile(r"^\s*Apply now\s*$"))
                if not btn.count():
                    log.append((co, title, "no button"))
                    continue
                btn.first.click()
                try:
                    p.get_by_text("Application sent").first.wait_for(timeout=10000)
                    applied += 1
                    log.append((co, title, "APPLIED"))
                    print(f"applied: {co} - {title}")
                except Exception:
                    log.append((co, title, "external"))
                for other in list(b.context.pages):        # external apply pages open in new tabs
                    if other != p:
                        other.close()
            except Exception as e:
                log.append((co, title, "error " + str(e)[:80]))
            log_path.write_text(json.dumps(log, indent=1))
        log_path.write_text(json.dumps(log, indent=1))
        print(f"applied {applied}; log: {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
