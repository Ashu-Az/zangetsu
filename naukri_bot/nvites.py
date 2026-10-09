"""Naukri NVites: jobs a recruiter invited the candidate to apply for (naukri.com/mnjuser/inbox).

Invites are the strongest leads on Naukri (a recruiter already picked the profile), so they run before keyword
search. The inbox is a two-pane page: invite cards on the left, the selected job with an Apply button on the right.
Every invite still goes through the normal filters (title, company, salary floor, experience fit), and applying
uses the same questionnaire handling, refusal detection and cooldown as search applies.

Some invites only say "To apply, fill the form: https://...": those links are saved for the company-site pipeline.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from .models import ApplyMode, JobPosting

log = logging.getLogger(__name__)

INBOX_URL = "https://www.naukri.com/mnjuser/inbox"
CARD = ".card.inbox-company-card"
DETAILS = ".card-details-container"
EXP_LINE = re.compile(r"^(\d+\s*-\s*\d+\s*Years?)\s*(.*)$", re.I)
WORK_MODE = re.compile(r"^(in office|hybrid|remote|work from home|wfh)$", re.I)
URL = re.compile(r"https?://[^\s)>\"']+")


@dataclass
class NVite:
    index: int
    title: str
    company: str
    location: str
    experience: str
    salary: str
    applied: bool
    unread: bool
    skills: list[str] = field(default_factory=list)
    description: str = ""
    external_urls: list[str] = field(default_factory=list)

    @property
    def job_id(self) -> str:
        key = re.sub(r"[^a-z0-9]+", "-", f"{self.company} {self.title} {self.location}".lower()).strip("-")
        return "nvite:" + key[:120]

    def posting(self) -> JobPosting:
        return JobPosting(job_id=self.job_id, url=INBOX_URL, title=self.title, company=self.company,
                          experience=self.experience, salary=self.salary, location=self.location,
                          description=self.description, skills=self.skills)


def parse_card(text: str) -> dict | None:
    """Card text, one field per line: [tagline] / title / location / "2 - 6 Years<salary>" / company / rating / date / [Applied].
    The experience line is the anchor: title and location sit above it, company below."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        m = EXP_LINE.match(line)
        if m and i >= 2:
            return {"title": lines[i - 2], "location": lines[i - 1], "experience": m.group(1).strip(),
                    "salary": m.group(2).strip() or "Not disclosed",
                    "company": lines[i + 1] if i + 1 < len(lines) else "",
                    "applied": any(l == "Applied" for l in lines[i:])}
    return None


def parse_details(text: str) -> tuple[list[str], str]:
    """(skills, job description) from the right-hand pane."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    skills: list[str] = []
    for i, line in enumerate(lines):
        if WORK_MODE.match(line) and i + 1 < len(lines):
            skills = [s.strip() for s in lines[i + 1].split(",") if s.strip()]
            break
    jd = text.split("Job description", 1)[1].strip() if "Job description" in text else ""
    return skills, jd


def collect(browser, max_scrolls: int = 8) -> list[NVite]:
    pg = browser.page
    browser.goto(INBOX_URL, settle=(5, 7))
    last = -1
    for _ in range(max_scrolls):                      # the list loads more cards as it scrolls
        n = pg.locator(CARD).count()
        if n == last or n == 0:
            break
        last = n
        pg.locator(CARD).last.scroll_into_view_if_needed()
        time.sleep(1.5)
    out: list[NVite] = []
    for i in range(pg.locator(CARD).count()):
        card = pg.locator(CARD).nth(i)
        info = parse_card(card.inner_text())
        if not info:
            continue
        unread = "unread" in (card.get_attribute("class") or "")
        card.click()
        time.sleep(2)
        details = pg.locator(DETAILS).first.inner_text() if pg.locator(DETAILS).count() else ""
        skills, jd = parse_details(details)
        out.append(NVite(index=i, unread=unread, skills=skills, description=jd,
                         external_urls=sorted(set(u.rstrip(".,;") for u in URL.findall(jd))), **info))
    return out


def open_invite(browser, invite: NVite) -> bool:
    """Select the invite's card so its Apply button is in the details pane. False if it moved or vanished."""
    pg = browser.page
    if not pg.url.startswith(INBOX_URL):
        browser.goto(INBOX_URL, settle=(5, 7))
    cards = pg.locator(CARD)
    for i in range(cards.count()):
        info = parse_card(cards.nth(i).inner_text())
        if info and info["title"] == invite.title and info["company"] == invite.company:
            cards.nth(i).click()
            time.sleep(2)
            return True
    return False


def apply_button(browser):
    pane = browser.page.locator(DETAILS).first
    return pane.locator("button:has-text('Apply')").filter(has_not_text="Applied").first


def is_applied(browser) -> bool:
    pane = browser.page.locator(DETAILS).first
    return pane.locator("button:has-text('Applied')").count() > 0


MODE = ApplyMode.NAUKRI
