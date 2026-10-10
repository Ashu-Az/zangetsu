"""Recruiter email outreach: a short, specific email with a resume tailored to the job, sent from the candidate's Gmail.

Leads come from two places the bot already reads:
  * Naukri job descriptions that ask for resumes by email ("send your CV to hr@...") - state/survey.json
  * LinkedIn hiring posts with an email address - output/leads/linkedin_posts.csv (`python -m naukri_bot leads`)

Every lead passes the same filters as an application (title, salary floor, experience fit, excluded companies), and
outreach is deliberately conservative, because a careless batch damages the sender's reputation:
  * never the same address twice, and at most one email per company domain per 30 days
  * a daily cap (OUTREACH_DAILY_CAP, default 15) and a pause of 45-90 s between sends
  * the resume is tailored with the grounding check (nothing outside the candidate's facts)
  * without --send, nothing leaves the machine: each email is written as a .eml draft next to its PDF

Sending uses Gmail SMTP with an App Password (Google Account > Security > App passwords):
  GMAIL_ADDRESS=you@gmail.com
  GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
"""
from __future__ import annotations

import csv
import json
import logging
import os
import random
import re
import smtplib
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from .config import OUTPUT_DIR, STATE_DIR, Settings
from .models import ApplyMode, JobPosting
from .portals.linkedin_posts import extract_emails
from .ranking import eligibility
from .tracker import Tracker

log = logging.getLogger(__name__)

SENT_PATH = STATE_DIR / "outreach_sent.json"
OUT_DIR = OUTPUT_DIR / "outreach"
# addresses that are never a recruiter's inbox
NOT_RECRUITER = re.compile(r"(?i)naukri|noreply|no-reply|donotreply|example\.|privacy|grievance|abuse|support@|"
                           r"unsubscribe|postmaster|webmaster|@sentry|\.png$|\.jpg$")
GENERIC_LOCAL = re.compile(r"(?i)^(hr|jobs?|careers?|recruit(ment|ing)?|talent|hiring|resume|cv|apply|info|hello|contact|team)\b")


@dataclass
class Lead:
    email: str
    title: str
    company: str
    jd: str
    url: str
    source: str                    # naukri | linkedin
    experience: str = ""
    salary: str = ""
    posted: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return self.email.split("@", 1)[1].lower()

    def posting(self) -> JobPosting:
        return JobPosting(job_id=f"email:{self.email}:{self.title}"[:150], url=self.url or f"mailto:{self.email}",
                          title=self.title, company=self.company, experience=self.experience, salary=self.salary,
                          description=self.jd, posted=self.posted)


# ------------------------------------------------------------------ leads
def naukri_leads() -> list[Lead]:
    path = STATE_DIR / "survey.json"
    if not path.exists():
        return []
    out = []
    for _, v in json.loads(path.read_text()).items():
        job = v.get("job", v) if isinstance(v, dict) else {}
        text = " ".join(str(job.get(f, "")) for f in ("description", "about_company"))
        emails = [e for e in extract_emails(text) if not NOT_RECRUITER.search(e)]
        if emails:
            out.append(Lead(email=emails[0], title=job.get("title", ""), company=job.get("company", ""), jd=text,
                            url=job.get("url", ""), source="naukri", experience=job.get("experience", ""),
                            salary=job.get("salary", ""), posted=job.get("posted", "")))
    return out


def linkedin_leads(days: int = 3) -> list[Lead]:
    path = OUTPUT_DIR / "leads" / "linkedin_posts.csv"
    if not path.exists():
        return []
    since = (datetime.now() - timedelta(days=days)).isoformat()
    out = []
    for r in csv.DictReader(open(path, newline="", encoding="utf-8")):
        if r.get("found_at", "") < since:
            continue
        emails = [e.strip() for e in (r.get("emails") or "").split(",") if e.strip() and not NOT_RECRUITER.search(e)]
        if emails:
            out.append(Lead(email=emails[0], title=r.get("role", ""), company=r.get("author", ""), jd=r.get("text", ""),
                            url=r.get("author_url", ""), source="linkedin", experience=r.get("experience", "")))
    return out


def _sent() -> dict:
    try:
        return json.loads(SENT_PATH.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def already_contacted(tracker: Tracker) -> tuple[set[str], set[str]]:
    """(addresses ever emailed, company domains emailed in the last 30 days), from the outreach log and the
    application log (earlier manual emails were recorded there with mode 'email')."""
    addresses, recent_domains = set(), set()
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    for addr, e in _sent().items():
        addresses.add(addr)
        if e.get("at", "") >= cutoff:
            recent_domains.add(addr.split("@")[1])
    if tracker.applications_csv.exists():
        for r in csv.DictReader(open(tracker.applications_csv, newline="", encoding="utf-8")):
            if r.get("mode", "").startswith("email"):
                addr = r.get("url", "").replace("mailto:", "").strip().lower()
                if "@" in addr:
                    addresses.add(addr)
                    if r.get("timestamp", "") >= cutoff:
                        recent_domains.add(addr.split("@")[1])
    return addresses, recent_domains


def clean_title(title: str, company: str = "") -> str:
    """'Gen AI _Opportunity at Fractal Analytics' -> 'Gen AI'; 'Hiring | AI Engineer | StatusNeo' -> 'AI Engineer'."""
    t = re.sub(r"[_|]+", " | ", title or "")
    parts = [x.strip(" -") for x in t.split("|") if x.strip(" -")]
    parts = [x for x in parts if not re.fullmatch(r"(?i)(urgent(ly)? )?(hiring|requirement|opening|job)s?( for)?|.*\bopportunity\b.*", x)
             and not (company and x.lower() == company.lower())] or parts
    t = re.sub(r"(?i)\s*(urgent(ly)?\s+)?(hiring|requirement|opportunity|opening)\s*(for|at)?\s*$", "", parts[0] if parts else title)
    t = re.sub(r"(?i)^(urgent(ly)?\s+)?(requirement|hiring|opening)s?\s+(for\s+(an?\s+)?)?", "", t)
    t = re.sub(rf"(?i)\s+at\s+{re.escape(company)}$", "", t) if company else t
    return re.sub(r"\s+", " ", t).strip(" -:") or title


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def lookalike_domain(email: str, company: str) -> bool:
    """True when the address's domain is a near-miss of the company's name ('fractcal.ai' for Fractal): a typo that
    bounces, or a scam lookalike. Recruiting-agency domains that differ completely are fine."""
    label = email.split("@")[1].split(".")[0].lower()
    for word in re.findall(r"[a-z]{5,}", (company or "").lower()):
        if label != word and 0 < _edit_distance(label, word) <= 2 and abs(len(label) - len(word)) <= 2:
            return True
    return False


def select(settings: Settings, profile: dict, tracker: Tracker, sources: list[str], max_n: int) -> list[Lead]:
    leads = (naukri_leads() if "naukri" in sources else []) + (linkedin_leads() if "linkedin" in sources else [])
    addresses, domains = already_contacted(tracker)
    chosen, seen_domains = [], set()
    for lead in leads:
        if lead.email in addresses or lead.domain in domains or lead.domain in seen_domains:
            continue
        reason = eligibility(lead.posting(), ApplyMode.NAUKRI, settings, profile)
        years = float(settings.experience_years) if settings.experience_years else None
        # a cold email needs a closer fit than a one-click apply: at most 1 year above the candidate's experience
        if not reason and lead.experience and not lead.posting().experience_fits(years, slack=1):
            reason = f"experience_too_far_for_email:{lead.experience}"
        if not reason and lookalike_domain(lead.email, lead.company):
            reason = f"lookalike_domain:{lead.domain}"
        if reason:
            log.debug("skip %s (%s): %s", lead.email, lead.title, reason)
            continue
        chosen.append(lead)
        seen_domains.add(lead.domain)
        if len(chosen) >= max_n:
            break
    return chosen


# ------------------------------------------------------------------ message
def _greeting(lead: Lead) -> str:
    local = lead.email.split("@")[0]
    if GENERIC_LOCAL.match(local) or not re.match(r"[a-z]{3,}", local):
        return f"Hello {lead.company} hiring team," if lead.company else "Hello,"
    first = re.split(r"[._\-\d]", local)[0]
    return f"Hi {first.capitalize()},"


def compose(lead: Lead, profile: dict, coverage: dict) -> tuple[str, str]:
    """(subject, plain-text body). Every claim comes from the profile or the grounded resume."""
    name = profile.get("name", "")
    years = profile.get("total_experience_years", "")
    notice = profile.get("notice_period_days", "")
    title = clean_title(lead.title, lead.company) or "the open role"
    subject = f"Application for {title} - {name} ({years} yrs, {notice}-day notice)"
    matched = [m for m in coverage.get("matched", []) if len(m) > 1][:5]
    skills_line = (f"The role asks for {', '.join(matched[:-1])} and {matched[-1]}, which is the stack I work in every day."
                   if len(matched) >= 2 else "My background lines up closely with what the role describes.")
    highlights = profile.get("outreach_highlights") or []
    body = "\n".join([
        _greeting(lead),
        "",
        f"I came across the {title} opening{' at ' + lead.company if lead.company else ''} and would like to apply. "
        f"I am a {profile.get('current_title', 'software engineer')} with {years} years of experience. {skills_line}",
        "",
        *[f"- {h}" for h in highlights[:3]],
        "" if highlights else "",
        f"My notice period is {notice} days. I've attached a resume tailored to this role and would be glad to talk.",
        "",
        "Thanks,",
        name,
        " | ".join(x for x in [profile.get("phone", ""), (profile.get("links") or {}).get("linkedin", ""),
                               (profile.get("links") or {}).get("github", "")] if x),
    ])
    return subject, re.sub(r"\n{3,}", "\n\n", body)


def build_message(lead: Lead, profile: dict, sender: str, subject: str, body: str, pdf: Path) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{profile.get('name', '')} <{sender}>"
    msg["To"] = lead.email
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf",
                       filename=f"{profile.get('name', 'Resume').replace(' ', '_')}_Resume.pdf")
    return msg


# ------------------------------------------------------------------ run
def run(settings: Settings, profile: dict, send: bool, max_n: int, sources: list[str], playwright=None) -> list[dict]:
    from .tailor_rules import tailored_pdf
    tracker = Tracker()
    cap = int(os.getenv("OUTREACH_DAILY_CAP", "15"))
    sent_today = sum(1 for e in _sent().values() if e.get("at", "").startswith(datetime.now().date().isoformat()))
    budget = max(0, min(max_n, cap - sent_today)) if send else max_n
    leads = select(settings, profile, tracker, sources, budget)
    log.info("outreach: %d lead(s) selected (sent today %d, daily cap %d)", len(leads), sent_today, cap)
    sender, password = os.getenv("GMAIL_ADDRESS", "").strip(), os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if send and not (sender and password):
        log.error("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env to send (writing drafts instead).")
        send = False
    day_dir = OUT_DIR / datetime.now().strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    smtp = None
    if send:
        smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60)
        smtp.login(sender, password)
    results = []
    try:
        for i, lead in enumerate(leads):
            pdf, rep = tailored_pdf(lead.jd, lead.title, lead.company, playwright=playwright, url=lead.url or lead.email,
                                    compact=True)
            coverage = rep.get("coverage") or {"matched": rep.get("ats_hit", [])}
            subject, body = compose(lead, profile, coverage)
            msg = build_message(lead, profile, sender or profile.get("email", ""), subject, body, Path(pdf))
            slug = re.sub(r"[^a-z0-9]+", "-", f"{lead.company}-{lead.title}".lower())[:60].strip("-")
            eml = day_dir / f"{i + 1:02d}-{slug}.eml"
            eml.write_bytes(bytes(msg))
            status = "drafted"
            if send:
                smtp.send_message(msg)
                status = "emailed"
                sent = _sent()
                sent[lead.email] = {"at": datetime.now().isoformat(timespec="seconds"), "title": lead.title,
                                    "company": lead.company, "source": lead.source}
                SENT_PATH.write_text(json.dumps(sent, indent=1))
                tracker.record_application(lead.posting(), "email:outreach", "applied", remember=False,
                                           notes=f"emailed {lead.email} with tailored resume ({lead.source})", resume_pdf=str(pdf))
            log.info("  %s: %s | %s -> %s (coverage %s%%)", status, lead.title[:40], lead.company[:25], lead.email,
                     rep.get("ats", "?"))
            results.append({"email": lead.email, "title": lead.title, "company": lead.company, "status": status,
                            "eml": str(eml), "pdf": str(pdf), "coverage": rep.get("ats"), "subject": subject})
            if send and i + 1 < len(leads):
                time.sleep(random.uniform(45, 90))
    finally:
        if smtp:
            smtp.quit()
    (day_dir / "summary.json").write_text(json.dumps(results, indent=1))
    return results
