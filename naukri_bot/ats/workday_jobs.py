"""Find fresh, relevant India roles on big companies' Workday career sites (public JSON API, no login).

Each company's site exposes POST /wday/cxs/<tenant>/<site>/jobs (search) and GET .../<externalPath> (details).
Jobs are kept when they are in India, posted within WORKDAY_MAX_AGE_DAYS, have a wanted title, an experience
range the candidate fits, and enough skill overlap with the job description.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass

from ..models import JobPosting
from ..ranking import skill_overlap_too_low, title_excluded, title_included
from .levels import dedupe, dedupe_roles, experience_label, html_to_text, india_only, is_india, too_senior

log = logging.getLogger(__name__)

# (company, tenant, wd host, site) - each verified to answer the public search API (Oct 2026)
COMPANIES = [
    ("Mastercard", "mastercard", "wd1", "CorporateCareers"), ("Adobe", "adobe", "wd5", "external_experienced"),
    ("HP", "hp", "wd5", "ExternalCareerSite"), ("Intel", "intel", "wd1", "External"),
    ("NVIDIA", "nvidia", "wd5", "NVIDIAExternalCareerSite"), ("Citi", "citi", "wd5", "2"),
    ("KLA", "kla", "wd1", "Search"), ("athenahealth", "athenahealth", "wd1", "External"),
    ("Expedia", "expedia", "wd108", "search"), ("Salesforce", "salesforce", "wd12", "External_Career_Site"),
    ("Workday", "workday", "wd5", "Workday"),
    # added 7 Oct 2026 (each answered the public search API)
    ("Target", "target", "wd5", "targetcareers"), ("PayPal", "paypal", "wd1", "jobs"), ("Autodesk", "autodesk", "wd1", "Ext"),
    ("Thomson Reuters", "thomsonreuters", "wd5", "External_Career_Site"), ("Analog Devices", "analogdevices", "wd1", "External"),
    ("Micron", "micron", "wd1", "External"), ("Equinix", "equinix", "wd1", "External"), ("GE HealthCare", "gehc", "wd5", "GEHC_ExternalSite"),
    ("Philips", "philips", "wd3", "jobs-and-careers"), ("BlackRock", "blackrock", "wd1", "BlackRock_Professional"),
    ("Morgan Stanley", "ms", "wd5", "External"), ("Cisco", "cisco", "wd5", "Cisco_Careers"), ("Visa", "visa", "wd5", "Visa"),
    ("Fidelity", "fmr", "wd1", "FidelityCareers"), ("State Street", "statestreet", "wd1", "Global"), ("Nasdaq", "nasdaq", "wd1", "Global_External_Site"),
    ("Bristol Myers Squibb", "bristolmyerssquibb", "wd5", "BMS"), ("Cigna / Evernorth", "cigna", "wd5", "cignacareers"),
    ("Lowe's", "lowes", "wd5", "LWS_External_CS"), ("Deutsche Bank", "db", "wd3", "DBWebsite"), ("Barclays", "barclays", "wd3", "External_Career_Site_Barclays"),
    ("Caterpillar", "cat", "wd5", "CaterpillarCareers"),
    # added 8 Oct 2026
    ("Ciena", "ciena", "wd5", "Careers"), ("Motorola Solutions", "motorolasolutions", "wd5", "Careers"), ("AstraZeneca", "astrazeneca", "wd3", "Careers"),
    ("Synchrony", "synchronyfinancial", "wd5", "Careers"), ("Wolters Kluwer", "wk", "wd3", "External"), ("LSEG", "lseg", "wd3", "Careers"),
    ("Mondelez", "mdlz", "wd3", "External"), ("Marsh McLennan", "mmc", "wd1", "Careers"), ("Travelers", "travelers", "wd5", "External"),
    # 9 Oct 2026: India tech centres with AI/backend roles for 0-4 yrs (verified via the cxs jobs API)
    ("Amgen", "amgen", "wd1", "Careers"), ("Citi", "citi", "wd5", "2"), ("Medtronic", "medtronic", "wd1", "MedtronicCareers"),
    ("NXP", "nxp", "wd3", "careers"), ("Expedia", "expedia", "wd108", "search"), ("Stryker", "stryker", "wd1", "StrykerCareers"),
    ("Fiserv", "fiserv", "wd5", "EXT"), ("Broadridge", "broadridge", "wd5", "Careers"), ("Equifax", "equifax", "wd5", "External"),
    ("FIS", "fis", "wd5", "SearchJobs"), ("S&P Global", "spgi", "wd5", "SPGI_Careers"), ("Cadence", "cadence", "wd1", "External_Careers"),
    ("Applied Materials", "amat", "wd1", "External"), ("HPE", "hpe", "wd5", "Jobsathpe"), ("Nike", "nike", "wd1", "nke"),
    ("Northern Trust", "ntrs", "wd1", "northerntrust"), ("KLA", "kla", "wd1", "Search"), ("Broadcom", "broadcom", "wd1", "External_Career"),
    ("Warner Bros. Discovery", "warnerbros", "wd5", "global"), ("Sanofi", "sanofi", "wd3", "SanofiCareers"), ("Zoom", "zoom", "wd5", "Zoom"),
    ("Rockwell Automation", "rockwellautomation", "wd1", "External_Rockwell_Automation"), ("Abbott", "abbott", "wd5", "abbottcareers"),
    ("Trimble", "trimble", "wd1", "TrimbleCareers"), ("TransUnion", "transunion", "wd5", "TransUnion"), ("Invesco", "invesco", "wd1", "IVZ"),
    ("The Hartford", "thehartford", "wd5", "Careers_External"), ("Chevron", "chevron", "wd5", "jobs"), ("Boeing", "boeing", "wd1", "EXTERNAL_CAREERS"), ("CVS Health", "cvshealth", "wd1", "CVS_Health_Careers"),
]
# the list repeats some sites (Citi, Expedia, KLA): search each (tenant, site) once
COMPANIES = dedupe(COMPANIES, key=lambda c: (c[1].lower(), c[3].lower()))


@dataclass
class WorkdayJob:
    company: str
    title: str
    location: str
    posted: str
    url: str
    jd: str
    experience: str
    req_id: str
    india_only: bool = True   # False when the role also lists non-Indian locations


class WorkdayMaintenance(Exception):
    """Workday's platform-wide maintenance window (weekends): every tenant redirects to a maintenance page."""


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Accept": "application/json"})
    resp = urllib.request.urlopen(req, timeout=20)
    if "maintenance" in resp.geturl():
        raise WorkdayMaintenance(resp.geturl())
    return json.load(resp)


def _get(url: str) -> dict:
    return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=20))


def _age_days(posted: str) -> int:
    p = posted.lower()
    if "today" in p:
        return 0
    if "yesterday" in p:
        return 1
    m = re.search(r"(\d+)\+?\s*day", p)
    return int(m.group(1)) + (1 if "+" in p else 0) if m else 99


def discover(profile: dict, queries: list[str], max_age_days: int = 7, years: float = 2, slack: int = 1) -> list[WorkdayJob]:
    found: dict[str, WorkdayJob] = {}
    for company, tenant, wd, site in COMPANIES:
        base = f"https://{tenant}.{wd}.myworkdayjobs.com"
        api = f"{base}/wday/cxs/{tenant}/{site}"
        for q in queries:
            try:
                res = _post(f"{api}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": f"{q} India"})
            except WorkdayMaintenance:
                log.warning("Workday is in its maintenance window (redirects to its maintenance page); "
                            "skipping Workday discovery this run")
                return []
            except Exception as e:
                log.warning("%s: search failed (%s)", company, type(e).__name__)
                break
            for p in res.get("jobPostings", []):
                path = p.get("externalPath", "")
                if not path or path in found:
                    continue
                title, loc, posted = p.get("title", ""), p.get("locationsText", ""), p.get("postedOn", "")
                if not is_india(loc + " " + path.replace("-", " ")) and "Locations" not in loc:
                    continue
                if _age_days(posted) > max_age_days:
                    continue
                if title_excluded(title, profile) or not title_included(title, profile) or too_senior(title):
                    continue
                try:
                    info = _get(f"{api}{path}")["jobPostingInfo"]
                except Exception:
                    continue
                # accepted when any listed location is in India; india_only tells whether it is only India
                locs = [info.get("location", "")] + list(info.get("additionalLocations", []) or [])
                if not is_india(locs):
                    continue
                jd = html_to_text(info.get("jobDescription", ""))
                if too_senior(title, jd):        # "Engineer II" asking 3+ years
                    continue
                exp = experience_label(jd)
                if exp and not JobPosting(job_id="", url="", experience=exp).experience_fits(years, slack=slack):
                    continue
                if skill_overlap_too_low(JobPosting(job_id="", url="", description=jd), profile):
                    continue
                found[path] = WorkdayJob(company, title, info.get("location", loc), posted, f"{base}/en-US/{site}{path}",
                                         jd, exp, (p.get("bulletFields") or [""])[0], india_only(locs))
            time.sleep(0.8)
    return dedupe_roles(sorted(found.values(), key=lambda j: _age_days(j.posted)))
