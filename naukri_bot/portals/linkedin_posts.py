"""Hiring posts on LinkedIn: find recent "we're hiring" posts that fit the profile and draft outreach.

Searches LinkedIn post search (latest first, past 24 h), keeps posts that name a relevant role, are about India
(an Indian city, India, remote-India, LPA or a rupee sign) and fit the candidate's experience. Every kept post is
appended to output/leads/linkedin_posts.csv; posts that give an email address get an email draft, posts by a
person without one get a short LinkedIn message draft. Nothing is sent: drafts are for the candidate to review.

    python -m naukri_bot leads
"""
from __future__ import annotations

import csv
import hashlib
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from ..config import OUTPUT_DIR
from ..models import JobPosting
from ..ranking import title_excluded
from .linkedin import experience_from_text

log = logging.getLogger(__name__)

LEADS_DIR = OUTPUT_DIR / "leads"
POSTS_CSV = LEADS_DIR / "linkedin_posts.csv"
COLUMNS = ["found_at", "post_age", "author", "author_url", "author_is_person", "role", "experience", "emails",
           "apply_links", "india_hint", "resume", "key", "text"]

POST = "[data-view-name='feed-full-update']"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# recruiters often obfuscate: "hr [at] acme [dot] com", "careers(at)acme.com", "jobs at acme dot in"
_AT = r"\s*(?:@|\[at\]|\(at\)|\{at\}|\s at\s)\s*"
_DOT = r"\s*(?:\.|\[dot\]|\(dot\)|\{dot\}|\s dot\s)\s*"
OBFUSCATED = re.compile(rf"([A-Za-z0-9._%+-]+){_AT}([A-Za-z0-9-]+(?:{_DOT}[A-Za-z0-9-]+)*){_DOT}([A-Za-z]{{2,}})\b", re.I)
NOT_EMAIL_HOSTS = re.compile(r"(?i)\b(linkedin|lnkd)\b")


def extract_emails(text: str) -> list[str]:
    """Plain and obfuscated addresses, normalised to user@domain.tld."""
    found = {e.strip(".-_").lower() for e in EMAIL.findall(text)}
    for user, host, tld in OBFUSCATED.findall(text):
        host = re.sub(_DOT, ".", host, flags=re.I)
        cand = f"{user}@{host}.{tld}".lower().rstrip(".")
        if EMAIL.fullmatch(cand) and not NOT_EMAIL_HOSTS.search(cand) and not re.fullmatch(r"\d+@.*", cand):
            found.add(cand)
    return sorted(e for e in found if not NOT_EMAIL_HOSTS.search(e.split("@")[1]))
INDIA = re.compile(r"\b(india|indian|bengaluru|bangalore|hyderabad|pune|mumbai|delhi|ncr|gurugram|gurgaon|noida|chennai|"
                   r"kolkata|ahmedabad|jaipur|kochi|indore|chandigarh|lpa|lakhs?)\b|₹|remote\s*\(?india", re.I)
# Roles the candidate targets (no bare "data ... engineer": data engineering is a different job)
ROLE_CORE = (r"(?:gen\s?ai|generative ai|ai\s?/\s?ml|ai|ml|machine learning|llm|nlp|applied ai|agentic ai|python|backend|back-end|"
             r"full[\s-]?stack|software|sde|mern|django|fastapi)\s*[\w/&+\-]{0,20}?\s*(?:engineers?|developers?|scientists?)\b|"
             r"data scientists?|sde[\s-]?(?:i{1,2}|[12])?")
# explicit role fields first ("Role: X", "Job Title: X", "Position: X", "hiring a/an X")
ROLE_FIELD = re.compile(r"(?:role|job title|position|designation|opening)\s*[:\-–]\s*([^\n|•]{3,70})", re.I)
ROLE_HIRING = re.compile(r"(?:is hiring|are hiring|we.?re hiring|hiring)\s*(?:for\s*)?(?:an?\s+|multiple\s+|\d+\s+)?([^\n.!,(]{3,60})", re.I)
ROLE = re.compile(r"((?:[\w.]+\s+){0,2}(?:" + ROLE_CORE + r"))", re.I)   # keep 2 words before: catches "Staff", "Senior"
# a hiring post says so plainly; articles about "the job market" do not
HIRING = re.compile(r"\b(is hiring|are hiring|we.?re hiring|we are looking for|hiring for|#hiring|now hiring|urgent(ly)? hiring|"
                    r"send (your )?(cv|resume)|share (your )?(cv|resume)|drop (your )?(cv|resume)|dm me|apply (here|now|via|at))\b", re.I)
# LinkedIn's "you've been searching a lot" wall: stop and cool down rather than push on
THROTTLED = re.compile(r"(too many|unusual) (searches|requests|activity|traffic)|reached the (commercial use|search) limit|"
                       r"we.ve (temporarily )?(paused|restricted)|try again (later|tomorrow)", re.I)
JUNK = re.compile(r"\b(intern(ship)?|trainee|fresher|support|sales|recruiter|hr\b|teacher|trainer)\b", re.I)


def _signature(profile: dict) -> str:
    """Name, phone and email from profile.yaml (no links: Gmail rewrites them into google.com/url redirects)."""
    contact = " | ".join(x for x in [profile.get("phone", ""), profile.get("email", "")] if x)
    return f"{profile.get('name', '')}\n{contact}"


def extract_role(text: str) -> str:
    """The advertised role: an explicit field or "hiring a X" if it names a target role, else the first target-role phrase."""
    for rx in (ROLE_FIELD, ROLE_HIRING):
        for m in rx.finditer(text):
            cand = " ".join(m.group(1).split())
            if re.search(ROLE_CORE, cand, re.I):
                return cand
            if rx is ROLE_FIELD:
                return cand      # an explicit field naming another job (e.g. "Tech Support Intern") is the job
    m = ROLE.search(text)
    return " ".join(m.group(1).split()).strip(" -/") if m else ""


def default_queries(profile: dict) -> list[str]:
    roles = ["AI engineer", "GenAI engineer", "machine learning engineer", "python developer", "LLM engineer",
             "data scientist", "full stack developer"]
    places = ["India", "Bangalore", "Gurgaon", "Noida", "Hyderabad", "Pune"]
    # few, broad searches: LinkedIn pauses search on accounts that search in bursts (seen live 5 Oct 2026).
    # Half ask for posts where the recruiter wants resumes by email (the ones worth emailing).
    return ([f"hiring {r} send resume" for r in ("AI engineer", "python developer", "full stack developer")]
            + [f"hiring {r} India" for r in ("GenAI engineer", "machine learning engineer", "data scientist")])


def parse_post(text: str, author: str, author_url: str, age: str, profile: dict) -> dict | None:
    """A lead dict if the post is a relevant, India-based hiring post that fits the profile, else None."""
    if not HIRING.search(text):
        return None
    # a real opening tells you how to act on it; job-market articles do not
    if not (extract_emails(text) or re.search(r"lnkd\.in/|https?://", text) or
            re.search(r"\b(dm me|inbox me|send (your )?(cv|resume)|share (your )?(cv|resume)|drop (your )?(cv|resume)|"
                      r"apply (here|now|via|at|through)|comment (below|interested)|whatsapp)\b", text, re.I)):
        return None
    role = extract_role(text)
    if not role or not re.search(ROLE_CORE, role, re.I) or JUNK.search(role) or title_excluded(role, profile):
        return None
    india = INDIA.search(text)
    if not india:
        return None
    exp = experience_from_text(text)
    years = profile.get("total_experience_years")
    if exp and years is not None and not JobPosting(job_id="", url="", experience=exp).experience_fits(float(years), slack=1):
        return None
    from ..resume_variants import variant_file, variant_for
    return {
        "found_at": datetime.now().isoformat(timespec="minutes"), "post_age": age, "author": author,
        "author_url": author_url, "author_is_person": "/in/" in author_url, "role": role, "experience": exp,
        "emails": ", ".join(extract_emails(text)),
        "apply_links": ", ".join(sorted(set(re.findall(r"https?://lnkd\.in/\w+", text)))),
        "india_hint": india.group(0), "resume": variant_file(variant_for(role)),
        "key": hashlib.sha1((author + text[:300]).encode()).hexdigest()[:16], "text": text[:3000],
    }


def collect(page, queries: list[str], profile: dict, scrolls: int = 4) -> list[dict]:
    leads: dict[str, dict] = {}
    empty_streak = 0
    for q in queries:
        url = ("https://www.linkedin.com/search/results/content/?keywords=" + quote(q)
               + "&datePosted=%22past-24h%22&sortBy=%22date_posted%22")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(random.uniform(5, 7))
            if "login" in page.url or "checkpoint" in page.url or "authwall" in page.url:
                log.error("LinkedIn asked for login/verification; stopping (%s)", page.url[:80])
                break
            if THROTTLED.search(page.inner_text("body")):
                log.error("LinkedIn paused search on this account (unusual traffic). Stopping; LinkedIn cooldown 48 h.")
                from ..cooldown import pause
                pause("linkedin", 48, "search paused: unusual traffic")
                break
            # results often render well after load (seen 10 Oct 2026: 11 posts one time, 0 the next): wait, reload once
            for attempt in range(2):
                try:
                    page.wait_for_selector(POST, timeout=15000)
                    break
                except Exception:
                    if attempt == 0:
                        page.reload(wait_until="domcontentloaded"); time.sleep(random.uniform(5, 7))
            for _ in range(scrolls):
                page.mouse.wheel(0, 2500); time.sleep(random.uniform(1.5, 2.5))
            # expand "…more" so the full post text (emails are often at the end) is in the DOM
            for btn in page.locator(f"{POST} button:has-text('more')").all()[:40]:
                try:
                    btn.click(timeout=1500); time.sleep(0.2)
                except Exception:
                    pass
            raw = page.evaluate("""(sel) => Array.from(document.querySelectorAll(sel)).map(p => {
                const a = p.querySelector('a[href*="/in/"], a[href*="/company/"], a[href*="/showcase/"]');
                const name = Array.from(p.querySelectorAll('a[href*="/in/"], a[href*="/company/"]'))
                                  .map(x => x.innerText.trim()).find(t => t) || '';
                const txt = p.innerText || '';
                const age = (txt.match(/\\n(now|\\d+\\s?[mhdw]|\\d+\\s?(?:min|hr|hour|day)s?)\\s?•/) || [, ''])[1];
                return {author: name.split('\\n')[0], url: a ? a.href.split('?')[0] : '', age: age, text: txt};
            })""", POST)
        except Exception as e:
            log.warning("query %r failed: %s", q, e.__class__.__name__)
            continue
        kept = 0
        for r in raw:
            lead = parse_post(r["text"], r["author"], r["url"], r["age"], profile)
            if lead and lead["key"] not in leads:
                leads[lead["key"]] = lead
                kept += 1
        log.info("%-45s %2d posts, %d relevant", q[:45], len(raw), kept)
        empty_streak = empty_streak + 1 if not raw else 0
        if empty_streak >= 3:
            log.error("3 searches in a row returned no posts: stopping (LinkedIn may be throttling)")
            break
        time.sleep(random.uniform(20, 40))
    return list(leads.values())


def save(leads: list[dict]) -> list[dict]:
    """Append new leads to the CSV; return only the ones not seen in earlier runs."""
    LEADS_DIR.mkdir(parents=True, exist_ok=True)
    known = set()
    if POSTS_CSV.exists():
        with open(POSTS_CSV, newline="", encoding="utf-8") as f:
            known = {r["key"] for r in csv.DictReader(f)}
    new = [l for l in leads if l["key"] not in known]
    first = not POSTS_CSV.exists()
    with open(POSTS_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        if first:
            w.writeheader()
        w.writerows(new)
    return new


def _first_name(author: str) -> str:
    parts = author.split()
    return parts[0] if parts and parts[0][0].isupper() else ""


def draft(lead: dict, profile: dict) -> str:
    """Markdown block with an email draft (when an address was posted) or a LinkedIn message draft."""
    hi = f"Hi {_first_name(lead['author'])}," if lead["author_is_person"] and _first_name(lead["author"]) else "Hello,"
    company = "" if lead["author_is_person"] else f" at {lead['author']}"
    role = lead["role"]
    body = (f"{hi}\n\nI saw your post about the {role} opening{company}. I'm an AI engineer with about two years of "
            "experience: I built an enterprise RAG pipeline over 1,000+ documents at 85% retrieval accuracy and cut API "
            "latency from 400 ms to under 150 ms for 10,000+ users. I currently build reinforcement-learning environments "
            "for LLM post-training (Python, FastAPI, PyTorch).\n\n"
            f"I can join within {profile.get('notice_period_days', 15)} days. My resume is attached; happy to share more "
            "or take a quick call.\n\nThanks,\n" + _signature(profile))
    head = f"### {role} · {lead['author']} · posted {lead['post_age'] or '?'} ago\n"
    if lead["emails"]:
        return (head + f"**Email to:** {lead['emails']}  \n**Subject:** Application for {role} – {profile.get('name', '')} "
                f"(AI Engineer, ~2 yrs, {profile.get('notice_period_days', 15)}-day notice)  \n"
                f"**Attach:** output/resumes/{lead['resume']}\n\n```\n{body}\n```\n")
    if lead["author_is_person"]:
        note = (f"{hi} I saw your post about the {role} role. I'm an AI engineer (~2 yrs: production RAG at 85% "
                "retrieval accuracy, API latency 400→150 ms for 10k+ users), can join in 15 days. Could I send you my resume?")
        return head + f"**LinkedIn message to:** {lead['author_url']}  \n*(no email in the post; {len(note)} chars)*\n\n```\n{note[:300]}\n```\n"
    links = lead["apply_links"] or "(no link in the post)"
    return head + f"**Company post, no email.** Apply via: {links}\n"


def write_drafts(new: list[dict], profile: dict) -> Path:
    path = LEADS_DIR / f"outreach_{datetime.now():%Y%m%d-%H%M}.md"
    with_email = [l for l in new if l["emails"]]
    people = [l for l in new if not l["emails"] and l["author_is_person"]]
    rest = [l for l in new if not l["emails"] and not l["author_is_person"]]
    parts = [f"# Outreach drafts ({datetime.now():%d %b %Y %H:%M})\n",
             f"{len(with_email)} email drafts, {len(people)} LinkedIn messages, {len(rest)} company posts. "
             "Review each before sending; nothing has been sent.\n"]
    for title, group in (("Email drafts", with_email), ("LinkedIn messages", people), ("Company posts", rest)):
        if group:
            parts.append(f"\n## {title}\n")
            parts += [draft(l, profile) for l in group]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path
