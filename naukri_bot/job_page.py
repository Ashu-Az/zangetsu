"""Scrape a Naukri job-detail page and classify its apply button."""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from . import selectors as S
from .models import ApplyMode, JobPosting

log = logging.getLogger(__name__)


def _text(soup, selector: str, sep: str = " ") -> str:
    el = soup.select_one(selector)
    return el.get_text(sep, strip=True) if el else ""


def parse_count(text: str) -> int | None:
    """'Applicants: 37' -> 37, 'Less than 10' -> 9, '1,234' -> 1234, '1.2K' -> 1200.

    Naukri caps the display at '100+' (and uses '50+' etc. elsewhere). A trailing '+'
    means "more than", so it maps to N+1 and lands in the next tier: '100+' -> 101.
    """
    t = text.lower().replace(",", "")
    if "less than" in t or "fewer than" in t:
        m = re.search(r"(\d+)", t)
        return max(int(m.group(1)) - 1, 0) if m else None
    m = re.search(r"(\d+(?:\.\d+)?)\s*([km])?\s*(\+)?", t.split(":")[-1])
    if not m:
        return None
    n = float(m.group(1))
    if m.group(2) == "k":
        n *= 1000
    elif m.group(2) == "m":
        n *= 1_000_000
    return int(n) + (1 if m.group(3) else 0)


def _clean_company(text: str) -> str:
    """'Infosys 3.5 52.7K Reviews' -> 'Infosys'."""
    text = re.sub(r"\s*\d+(\.\d+)?\s*(\d+(\.\d+)?[KkMm]?\s*)?Reviews?.*$", "", text, flags=re.I)
    return text.strip(" |,")


def _job_id_from_url(url: str) -> str:
    m = re.search(r"-(\d{9,})(?:\?|$)", url)
    return m.group(1) if m else url.rstrip("/").split("-")[-1]


def parse_job_page(html: str, url: str) -> JobPosting:
    soup = BeautifulSoup(html, "lxml")
    job = JobPosting(job_id=_job_id_from_url(url), url=url)
    job.title = _text(soup, S.JD_TITLE)
    comp = soup.select_one(S.JD_COMPANY_LINK) or soup.select_one(S.JD_COMPANY)
    job.company = _clean_company(comp.get_text(" ", strip=True) if comp else "")
    exp_raw = _text(soup, S.JD_EXPERIENCE)
    m = re.search(r"(\d+\s*-\s*\d+\s*years?|\d+\+?\s*years?)", exp_raw, re.I)
    job.experience = m.group(1) if m else exp_raw.split("|")[0].strip()
    job.salary = _text(soup, S.JD_SALARY)
    job.location = re.sub(r"\s*,\s*", ", ", _text(soup, S.JD_LOCATION))
    stats = [s.get_text(" ", strip=True) for s in soup.select(S.JD_STATS)]
    job.posted = next((s for s in stats if "Posted" in s), "")
    for st in stats:
        low = st.lower()
        if "applicant" in low:
            job.applicants = parse_count(st)
        elif "opening" in low:
            job.openings = parse_count(st)

    desc = soup.select_one(S.JD_DESCRIPTION)
    job.description = desc.get_text("\n", strip=True) if desc else ""
    job.description = re.sub(r"^Job description\s*\n", "", job.description, flags=re.I)

    details = _text(soup, S.JD_DETAILS, sep="|")
    for label, attr in [("Role:", "role"), ("Industry Type:", "industry"), ("Department:", "department"),
                        ("Employment Type:", "employment_type"), ("Role Category:", "role_category")]:
        m = re.search(re.escape(label) + r"\s*\|?\s*([^|]+)", details)
        if m:
            setattr(job, attr, m.group(1).strip(" ,"))

    job.education = _text(soup, S.JD_EDUCATION, sep=" ").replace("Education", "", 1).strip()
    job.skills = sorted({a.get_text(" ", strip=True) for a in soup.select(S.JD_KEY_SKILLS) if a.get_text(strip=True)})
    for item in soup.select(S.MATCH_ITEM):
        label = item.get_text(" ", strip=True).lower()
        if label:
            job.match[label] = item.select_one(S.MATCH_OK_ICON) is not None
    about = soup.select_one(S.JD_ABOUT_COMPANY)
    if about:
        job.about_company = about.get_text(" ", strip=True)[:1500]
    return job


def scrape_job(browser, url: str) -> JobPosting:
    browser.goto(url)
    try:
        browser.page.wait_for_selector(S.JD_TITLE, timeout=15000)
    except Exception:
        pass
    try:   # the match-score block renders after the header
        browser.page.wait_for_selector(f"{S.MATCH_ITEM} i", timeout=5000)
    except Exception:
        pass
    return parse_job_page(browser.page.content(), browser.page.url)


def detect_apply_mode(browser) -> ApplyMode:
    if browser.text_present(S.JOB_EXPIRED_TEXT):
        return ApplyMode.EXPIRED
    if browser.first_present(S.ALREADY_APPLIED):
        return ApplyMode.ALREADY_APPLIED
    if browser.first_present(S.COMPANY_SITE_BUTTON):
        return ApplyMode.COMPANY_SITE
    apply_btn = browser.first_present(S.APPLY_BUTTON)
    if apply_btn:
        label = (apply_btn.inner_text() or "").strip().lower()
        if "applied" in label:
            return ApplyMode.ALREADY_APPLIED
        if "company" in label:
            return ApplyMode.COMPANY_SITE
        return ApplyMode.NAUKRI
    return ApplyMode.UNKNOWN
