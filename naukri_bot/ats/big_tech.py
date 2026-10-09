"""Find fresh India roles at big tech companies that run their own career sites (Amazon, Google).

Both expose search data without login:
  Amazon  https://www.amazon.jobs/en/search.json?base_query=..&normalized_country_code[]=IND&sort=recent
  Google  https://www.google.com/about/careers/applications/jobs/results?location=India&q=..&sort_by=date
          (the job list is embedded in the page as AF_initDataCallback 'ds:1')
Applying needs the candidate's own Amazon / Google account, so these are leads: the bot ranks them and the
user signs in once in the bot's browser.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ..models import JobPosting
from ..ranking import skill_overlap_too_low, title_excluded, title_included
from .levels import dedupe_roles, experience_label, html_to_text, india_only, is_india, too_senior
from .top_companies import OpenRole

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/130 Safari/537.36", "Accept": "application/json"}
QUERIES = ["software engineer", "software development engineer", "machine learning engineer", "backend engineer",
           "AI engineer", "python developer", "full stack engineer"]


def _get(url: str) -> bytes:
    for attempt in range(3):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25).read()
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 2:
                raise
            time.sleep(10 * (attempt + 1))
    return b""


def amazon(queries=QUERIES, per_query: int = 100) -> list[OpenRole]:
    out = {}
    for q in queries:
        u = ("https://www.amazon.jobs/en/search.json?" + urllib.parse.urlencode(
            {"base_query": q, "normalized_country_code[]": "IND", "result_limit": per_query, "sort": "recent"}))
        try:
            d = json.loads(_get(u))
        except Exception as e:
            log.warning("amazon %r: %s", q, e); continue
        for j in d.get("jobs", []):
            try:
                posted = datetime.strptime(re.sub(r"\s+", " ", j["posted_date"]), "%B %d, %Y").replace(tzinfo=timezone.utc)
            except Exception:
                posted = None
            url = "https://www.amazon.jobs" + j["job_path"]
            basic = "BASIC QUALIFICATIONS\n" + html_to_text(j.get("basic_qualifications", ""))
            jd = "\n".join([html_to_text(j.get("description", "")), basic,
                            "PREFERRED QUALIFICATIONS\n" + html_to_text(j.get("preferred_qualifications", ""))])
            out[url] = OpenRole("amazon", "amazon", j["title"].strip(), j.get("location", ""), posted, url,
                                j.get("url_next_step") or url, jd, experience_label(basic))
        time.sleep(1)
    return list(out.values())


def google(queries=QUERIES, pages: int = 4) -> list[OpenRole]:
    out = {}
    for q in queries:
        for page in range(1, pages + 1):
            u = ("https://www.google.com/about/careers/applications/jobs/results?" +
                 urllib.parse.urlencode({"location": "India", "q": q, "sort_by": "date", "page": page}))
            try:
                b = _get(u).decode()
                m = re.search(r"AF_initDataCallback\(\{key: 'ds:1', hash: '\d+', data:", b)
                d, _ = json.JSONDecoder().raw_decode(b[m.end():])
            except Exception as e:
                log.warning("google %r p%d: %s", q, page, e); break
            jobs = d[0] or []
            for j in jobs:
                jid, title = j[0], j[1]
                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
                url = f"https://www.google.com/about/careers/applications/jobs/results/{jid}-{slug}"
                quals = html_to_text((j[4] or [None, ""])[1] or "")
                minq = re.split(r"(?i)preferred qualifications", quals)[0]
                if not re.search(r"(?i)minimum qualifications", minq):
                    minq = "Minimum qualifications:\n" + minq
                locs = " / ".join(l[0] for l in (j[9] or []) if l)
                posted = datetime.fromtimestamp(j[12][0], tz=timezone.utc) if j[12] else None
                jd = "\n".join([html_to_text((j[3] or [None, ""])[1] or ""), quals, html_to_text((j[10] or [None, ""])[1] or "")])
                out[url] = OpenRole("google", "google", title, locs, posted, url, j[2] or url, jd, experience_label(minq),
                                    india_only(locs))
            if len(jobs) < 20:
                break
            time.sleep(1)
    return list(out.values())


def survey(profile: dict, max_age_days: int = 14, years: float = 2, slack: int = 1) -> tuple[list[OpenRole], dict]:
    roles = amazon() + google()
    stats = {"roles_total": len(roles)}
    now = datetime.now(timezone.utc)
    keep = []
    for r in roles:
        # Amazon is queried by country code (its "IN, KA, Virtual" style locations may name no city)
        if r.ats != "amazon" and r.location and not is_india(r.location):
            continue
        if r.posted and (now - r.posted).days > max_age_days:
            continue
        if title_excluded(r.title, profile) or not title_included(r.title, profile):
            continue
        # SDE II at Amazon asks 3+ years: judged against the basic / minimum qualifications
        if too_senior(r.title, r.jd):
            continue
        if r.experience and not JobPosting(job_id="", url="", experience=r.experience).experience_fits(years, slack=slack):
            continue
        if skill_overlap_too_low(JobPosting(job_id="", url="", description=r.jd), profile):
            continue
        keep.append(r)
    keep.sort(key=lambda r: r.posted or datetime(2000, 1, 1, tzinfo=timezone.utc), reverse=True)
    keep = dedupe_roles(keep)
    stats["fit"] = len(keep)
    return keep, stats
