"""LinkedIn hiring posts with an email address -> one .eml per post, with a resume tailored to the post text and
scored with the ATS match (target from profile.ats_target, 95). Send them with tools/gmail_web_send.py.

    .venv/bin/python tools/post_outreach.py                 # posts found today
    .venv/bin/python tools/post_outreach.py --since 2026-10-21

Skips anyone already emailed (state/outreach_sent.json), personal recruiter-agency inboxes that ask candidates to pay,
and posts for senior / lead roles or roles far outside Ashu's stack.
"""
from __future__ import annotations

import csv
import json
import logging
import re
import sys
from datetime import date
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from naukri_bot.config import load_profile  # noqa: E402
from naukri_bot.tailor_rules import tailored_pdf  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("post_outreach")
csv.field_size_limit(10_000_000)

SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|architect|manager|head|director|vp)\b", re.I)
OFF_STACK = re.compile(r"\b(sap|salesforce|\.net|c#|php|golang|ios|android|flutter|mainframe|tester|qa)\b", re.I)
MIN_YEARS = re.compile(r"(\d+)\s*(?:\+|-|to|–)\s*(\d+)?\s*(?:years?|yrs)", re.I)
SCAM = re.compile(r"registration fee|pay(ment)? of rs|security deposit|training fee", re.I)


def years_ok(text: str) -> bool:
    m = MIN_YEARS.search(text or "")
    return not m or int(m.group(1)) <= 3                  # Ashu has 2 years; "3+" is still worth an email


def first_name(author: str) -> str:
    # a person's first name only: "NR Consulting" gave "Hi Nr," (22 Oct 2026), so initials and company names -> ""
    if re.search(r"\b(consult\w*|solutions?|technolog\w*|services|placements?|careers?|hr|recruit\w*|pvt|ltd|inc|llp|group|labs?|"
                 r"centre|center|institute|university|college|foundation|academy|systems|software|metalogyc)\b",
                 author or "", re.I):
        return ""
    n = re.split(r"[\s,|]+", (author or "").strip())
    return n[0].capitalize() if n and n[0].isalpha() and 3 <= len(n[0]) <= 12 else ""


def clean_role(role: str) -> str:
    """'LLMEngineer' -> 'LLM Engineer', '#FullStackDeveloper #MobileAppDeveloper' -> 'Full Stack Developer',
    'Full Stack Developer – GB04' -> 'Full Stack Developer', 'ready production Python/Data Engineers' -> 'Python/Data Engineer'
    (24 Oct 2026 drafts)."""
    r = role.split("#")[0].strip() or role.lstrip("#").split("#")[0].strip()
    r = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", r)          # camelCase / acronym boundaries
    r = re.sub(r"\s*[–—-]\s*[A-Z]{1,4}\d+\b.*$", "", r)                           # requisition codes
    r = re.sub(r"^(ready|immediate|urgent|hiring|looking for|production|ready production)\s+", "", r, flags=re.I)
    r = re.sub(r"^(production|ready)\s+", "", r, flags=re.I)
    r = re.sub(r"^for an?\s+", "", r, flags=re.I)                                   # "For an AI Systems Engineer"
    r = re.sub(r"\s*[-–(]?\s*\d+\s*(-|to|–|\+)\s*\d*\s*(years?|yrs).*$", "", r, flags=re.I)   # "... 2-10 years of Experience"
    r = re.sub(r"\b(Engineer|Developer|Scientist|Analyst)s\b", r"\1", r)
    r = re.sub(r"\s+", " ", r).strip(" :|-–•")
    return (r[0].upper() + r[1:]) if r else "Software Engineer"


def compose(role: str, author: str, matched: list[str], profile: dict) -> tuple[str, str]:
    name = profile.get("name", "")
    subject = f"Application for {role} - {name} (2 yrs, immediate joiner)"
    greet = f"Hi {first_name(author)}," if first_name(author) else "Hello,"
    # only skills Ashu really uses: the post's Angular / Spring Boot / dbt are not "the stack I work in every day"
    mine = {x.lower() for x in (profile.get("skills_primary") or []) + (profile.get("skills_secondary") or [])}
    mine |= {"machine learning", "generative ai", "rest apis", "rest api", "llm", "nlp", "sql", "docker", "microservices"}
    skills = [m for m in matched if len(m.split()) <= 3 and m.lower() in mine][:5]
    skills_line = (f" The role asks for {', '.join(skills[:-1])} and {skills[-1]}, which is the stack I work in every day."
                   if len(skills) >= 2 else "")
    body = "\n".join([
        greet, "",
        f"I saw your post about the {role} opening and would like to apply. I am a Python Full Stack AI/ML Software "
        f"Engineer with 2 years of experience building LLM and RAG features, FastAPI services and REST APIs in production.{skills_line}",
        "",
        "Highlights: an enterprise RAG pipeline over 1,000+ documents at 85% retrieval accuracy, API latency cut from "
        "400 ms to under 150 ms for 10,000+ users, and ML inference pipelines with Redis caching.",
        "",
        "I can join immediately. I've attached a resume tailored to this role and would be glad to talk.",
        "", "Thanks,", name, f"+91-{profile.get('phone_national', '')} | {profile.get('email', '')}",
    ])
    return subject, body


def main() -> int:
    since = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else date.today().isoformat()
    profile = load_profile()
    sent = json.loads((ROOT / "state/outreach_sent.json").read_text()) if (ROOT / "state/outreach_sent.json").exists() else {}
    out = ROOT / "output" / "outreach" / f"{date.today().isoformat()}-posts"
    out.mkdir(parents=True, exist_ok=True)
    done_here = {p.stem.split("__")[-1] for p in out.glob("*.eml")}
    rows = list(csv.DictReader(open(ROOT / "output/leads/linkedin_posts.csv", newline="", encoding="utf-8")))
    n = 0
    seen = set()
    for r in rows:
        if (r.get("found_at") or "") < since or not r.get("emails"):
            continue
        email = r["emails"].split(";")[0].split(",")[0].strip().lower()
        role = clean_role(re.sub(r"\s+", " ", r.get("role") or "Software Engineer").strip(" :|-–•\t")[:70] or "Software Engineer")
        if not re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)*\.(com|in|io|ai|co|org|net|tech|dev|space|us|uk|app)", email):
            log.info("skip %-40s (not a complete email address)", email)
            continue
        text = r.get("text") or ""
        key = re.sub(r"[^a-z0-9]+", "-", email)
        why = ("already emailed" if email in sent or email in seen or key in done_here else
               "senior role" if SENIOR.search(role) else
               "off-stack" if OFF_STACK.search(role) else
               "needs more years" if not years_ok(r.get("experience", "") + " " + text[:600]) else
               "asks for a fee" if SCAM.search(text) else
               "college address (student placement)" if re.search(r"\.edu(\.in)?$|\.ac\.in$", email) and not re.search(r"\.res\.in$", email) else "")
        if why:
            log.info("skip %-40s %s (%s)", email, role[:40], why)
            continue
        seen.add(email)
        company = (r.get("author") or "company").split("|")[0].strip()[:30] or "company"
        pdf, rep = tailored_pdf(text, role, company, url=email, compact=True)
        subject, body = compose(role, r.get("author", ""), rep.get("ats_hit") or [], profile)
        msg = EmailMessage()
        msg["To"], msg["Subject"] = email, subject
        msg.set_content(body)
        msg.add_attachment(Path(pdf).read_bytes(), maintype="application", subtype="pdf", filename="Ashu_Azad_Resume.pdf")
        n += 1
        path = out / f"{n:02d}-{re.sub(r'[^a-z0-9]+', '-', role.lower())[:40]}__{key}.eml"
        path.write_bytes(bytes(msg))
        log.info("draft %-40s %-40s ATS %s", email, role[:40], rep.get("ats"))
    log.info("%d post emails ready in %s", n, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
