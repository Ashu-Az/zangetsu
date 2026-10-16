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
        # a "Remote" location also matches WORK_MODE; the line after it is then experience/salary, not skills
        if WORK_MODE.match(line) and i + 1 < len(lines) and not EXP_LINE.match(lines[i + 1]):
            skills = [s.strip() for s in lines[i + 1].split(",") if s.strip()]
            break
    jd = text.split("Job description", 1)[1].strip() if "Job description" in text else ""
    return skills, jd


def _dismiss_overlays(pg) -> None:
    """Close promo modals (e.g. 'Naukri 360 Pro unlocked') whose backdrop swallows clicks on the inbox cards."""
    for _ in range(3):
        if not pg.locator(".mc__backdrop--show, [class*=backdrop][class*=show]").count():
            return
        closed = False
        for sel in (".mc__close", "[class*=modal] [class*=close]", "[aria-label='Close']", "button:has-text('Close')",
                    "button:has-text('Maybe later')", "button:has-text('Skip')"):
            btn = pg.locator(sel)
            if btn.count() and btn.first.is_visible():
                try:
                    btn.first.click(timeout=3000); closed = True; break
                except Exception:
                    pass
        if not closed:
            pg.keyboard.press("Escape")
        time.sleep(1)


def _click_card(pg, card) -> None:
    """Click an inbox card; if an overlay still intercepts, click it in the page (React listens on the element)."""
    _dismiss_overlays(pg)
    try:
        card.click(timeout=5000)
    except Exception:
        card.evaluate("e => e.click()")


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
        try:
            # the inbox list can shrink while it is read (card 22 vanished, 14 Oct 2026): skip, don't crash
            if i >= pg.locator(CARD).count():
                break
            info = parse_card(card.inner_text(timeout=5000))
            if not info:
                continue
            unread = "unread" in (card.get_attribute("class", timeout=5000) or "")
            _click_card(pg, card)
            time.sleep(2)
            details = pg.locator(DETAILS).first.inner_text() if pg.locator(DETAILS).count() else ""
        except Exception:
            continue
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
        try:
            info = parse_card(cards.nth(i).inner_text(timeout=5000))
        except Exception:
            continue
        if info and info["title"] == invite.title and info["company"] == invite.company:
            _click_card(pg, cards.nth(i))
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
