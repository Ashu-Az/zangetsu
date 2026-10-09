"""Build Naukri search URLs and harvest job cards from result pages."""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from . import selectors as S
from .config import Settings
from .models import JobCard

log = logging.getLogger(__name__)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


CTC_BUCKETS = [(0, 3), (3, 6), (6, 10), (10, 15), (15, 25), (25, 50), (50, 75), (75, 100)]


def ctc_filter_params(min_lpa: float) -> list[str]:
    """Naukri's salary facet: one ctcFilter per bucket. Keep every bucket whose top is above the floor.
    The facet also applies to postings that hide their salary, using the recruiter's entered range."""
    if not min_lpa:
        return []
    return [f"ctcFilter={lo}to{hi}" for lo, hi in CTC_BUCKETS if hi > min_lpa]


def build_search_urls(settings: Settings, keywords: list[str] | None = None) -> list[str]:
    """Naukri URL scheme (verified 2026-10): /{kw}-jobs, /{kw}-jobs-2, /{kw}-jobs-in-{loc}-2."""
    urls = []
    for kw in keywords or settings.keywords:
        base = f"https://www.naukri.com/{_slug(kw)}-jobs"
        if settings.location:
            base += f"-in-{_slug(settings.location)}"
        for page in range(1, settings.pages_per_keyword + 1):
            url = base if page == 1 else f"{base}-{page}"
            params = []
            # No experience= here: verified live 2026-10-05 that it makes Naukri drop the keyword. "data scientist"
            # returned 926 matching jobs without it and 120 generic ones with it, the same pool for every keyword.
            # Experience is checked per posting in ranking.eligibility (JobPosting.experience_fits).
            if settings.max_job_age_days:
                params.append(f"jobAge={settings.max_job_age_days}")   # Naukri's "Freshness" filter
            # No ctcFilter here: verified live 2026-10-04 that Naukri's salary facet shrinks results to the
            # few postings with a disclosed salary and stops honouring the keyword (every keyword returned
            # the same ~16 mixed jobs). The salary floor is enforced per posting in ranking.eligibility.
            if params:
                url += "?" + "&".join(params)
            urls.append(url)
    return urls


def parse_cards(html: str) -> list[JobCard]:
    soup = BeautifulSoup(html, "lxml")
    cards: list[JobCard] = []
    for wrap in soup.select(S.JOB_CARD):
        a = wrap.select_one(S.CARD_TITLE)
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if href.startswith("/"):
            href = "https://www.naukri.com" + href
        job_id = wrap.get("data-job-id") or (re.search(r"-(\d{9,})$", href.split("?")[0]) or [None, ""])[1]

        def txt(sel: str) -> str:
            el = wrap.select_one(sel)
            return el.get_text(" ", strip=True) if el else ""

        cards.append(JobCard(
            job_id=str(job_id),
            title=a.get_text(" ", strip=True),
            company=txt(S.CARD_COMPANY),
            url=href,
            experience=txt(S.CARD_EXPERIENCE),
            location=txt(S.CARD_LOCATION),
            snippet=txt(S.CARD_DESC),
            posted=txt(S.CARD_POSTED),
            skills=[li.get_text(" ", strip=True) for li in wrap.select(S.CARD_SKILLS)],
        ))
    return cards


def _base_of(url: str) -> str:
    """Search URL without its page suffix: /ai-engineer-jobs-3?x -> /ai-engineer-jobs?x."""
    return re.sub(r"(-jobs(?:-in-[a-z0-9-]+?)?)-\d+(\?|$)", r"\1\2", url)


RECOMMENDED_URL = "https://www.naukri.com/mnjuser/recommendedjobs"
RECOMMENDED_TABS = ["profile", "apply", "preference", "similar_jobs"]   # Profile, Applies, Preferences, You might like


def parse_recommended(html: str) -> list[JobCard]:
    """Cards on the Recommended jobs page: article.jobTuple[data-job-id], no links (verified 2026-10-05)."""
    soup = BeautifulSoup(html, "lxml")
    out = []
    for a in soup.select("article.jobTuple[data-job-id]"):
        jid = a.get("data-job-id", "").strip()
        title = a.select_one("p.title")
        if not jid or not title:
            continue

        def txt(sel: str) -> str:
            el = a.select_one(sel)
            return el.get_text(" ", strip=True) if el else ""

        out.append(JobCard(job_id=jid, title=title.get_text(" ", strip=True), company=txt(".subTitle"),
                           url=f"https://www.naukri.com/job-listings-{jid}", experience=txt("li.experience"),
                           location=txt("li.location"), snippet=txt(".job-description"),
                           posted=txt(".plcHolder .fw500") or txt(".plcHolder"),
                           skills=[li.get_text(" ", strip=True) for li in a.select("li.dot")], source="recommended"))
    return out


def collect_recommended(browser) -> list[JobCard]:
    """Every card from every tab of Naukri's Recommended jobs page."""
    cards: dict[str, JobCard] = {}
    browser.goto(RECOMMENDED_URL)
    for tab in RECOMMENDED_TABS:
        try:
            browser.page.locator(f".tab-wrapper#{tab} .tab-list-item, #{tab} .tab-list-item, #{tab}").first.click(timeout=5000)
            browser.page.wait_for_selector("article.jobTuple[data-job-id]", timeout=10000)
            for _ in range(8):                      # the list loads as you scroll
                browser.page.mouse.wheel(0, 3000)
                browser.page.wait_for_timeout(700)
        except Exception as e:
            log.warning("Recommended tab %s: %s", tab, e.__class__.__name__)
            continue
        found = parse_recommended(browser.page.content())
        new = [c for c in found if c.job_id not in cards]
        cards.update({c.job_id: c for c in new})
        log.info("Recommended / %-12s %d cards, %d new", tab, len(found), len(new))
    return list(cards.values())


def collect_cards(browser, urls: list[str]) -> list[JobCard]:
    seen: set[str] = set()
    out: list[JobCard] = []
    exhausted: set[str] = set()     # searches whose last page was short: Naukri serves page 1 again past the end
    for url in urls:
        if _base_of(url) in exhausted:
            continue
        log.info("Search: %s", url)
        try:
            browser.goto(url)
            browser.page.wait_for_selector(S.JOB_CARD, timeout=15000)
        except Exception as e:
            log.warning("No job cards on %s (%s)", url, e.__class__.__name__)
            continue
        found = parse_cards(browser.page.content())
        if len(found) < 20:
            exhausted.add(_base_of(url))
        new = [c for c in found if c.job_id not in seen]
        seen.update(c.job_id for c in new)
        out.extend(new)
        log.info("  %d cards, %d new", len(found), len(new))
    return out
