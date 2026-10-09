"""Survey top tech companies' public job boards (Greenhouse, Lever, Ashby) for fresh India roles that fit.

All three expose public JSON without login:
  Greenhouse  https://boards-api.greenhouse.io/v1/boards/<board>/jobs?content=true
  Lever       https://api.lever.co/v0/postings/<company>?mode=json
  Ashby       https://api.ashbyhq.com/posting-api/job-board/<org>
Boards that do not exist answer 404 and are skipped, so the list below can be generous.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from ..models import JobPosting
from ..ranking import skill_overlap_too_low, title_excluded, title_included
from .levels import dedupe, dedupe_roles, experience_label, html_to_text, india_only, is_india, too_senior

log = logging.getLogger(__name__)

GREENHOUSE = ["databricks", "stripe", "mongodb", "coinbase", "rubrik", "postman", "cloudflare", "datadog", "elastic",
              "twilio", "okta", "cohesity", "dropbox", "gitlab", "pinterest", "airbnb", "samsara", "figma", "discord",
              "robinhood", "instacart", "doordash", "lyft", "affirm", "brex", "chime", "anthropic", "scaleai", "groww",
              "phonepe", "meesho", "zetaglobal", "sprinklr", "druva", "harness", "zscaler", "nutanix", "hashicorp",
              "confluent", "mindtickle", "browserstack", "innovaccer", "observeai", "sigmoid", "fivetran", "amplitude",
              "intercom", "gojek", "grab", "rapido", "jupiter", "slice", "dunzo", "urbancompany", "nykaa", "lenskart",
              "inmobi", "clevertap", "hackerrank", "netskope", "zeta", "uniphore", "gupshup", "yellowai", "haptik", "observe",
              "nutanix", "rubrik", "purestorage", "snowflake", "mongodb", "newrelic", "sumologic", "toast", "gong", "notion",
              "verkada", "plaid", "chime", "wise", "revolut", "checkout", "deliveryhero", "trustpilot", "canva", "atlassian", "flash", "lemurianlabs",
    # 9 Oct 2026 research: India AI/backend openings, 0-4 yrs, verified via public APIs
    "gleanwork", "emergentlabsinc", "razorpaysoftwareprivatelimited", "abnormalsecurity", "devrev", "singlestore", "highradius", "turing", "gravitonresearchcapital", "towerresearchcapital", "dscout", "dialpad", "truveta", "project44", "celonis", "anaplan", "smartsheet", "coursera", "starburst", "enterpret", "truefoundry", "celigo", "lynxanalytics", "getwellnetwork", "bswiftindia", "instawork", "particle41llc", "alphasense", "bitgo", "pubmatic", "poppulo", "blueshift", "headoutcareers", "worldquant"]
LEVER = ["palantir", "plaid", "cred", "razorpay", "swiggy", "zomato", "sharechat", "dream11", "upgrad", "unacademy",
         "meesho", "paytm", "spotify", "netflix", "atlan", "zepto", "khatabook", "smallcase", "postman", "hevo",
         "chargebee", "freshworks", "leadsquared", "darwinbox", "whatfix", "simpl", "tracxn", "fampay", "bharatpe",
         "meesho", "jar", "fi", "kreditbee", "spinny", "cars24", "ola", "urbancompany", "pocketfm", "kuku", "sarvam", "krutrim",
    # 9 Oct 2026 research: India AI/backend openings, 0-4 yrs, verified via public APIs
    "hevodata", "netomi", "drivetrain", "100ms", "veeva", "outreach", "acceldata", "zimperium", "jumpcloud", "outcomesai", "portcast", "entrata"]
ASHBY = ["openai", "notion", "ramp", "rippling", "perplexity", "linear", "vercel", "supabase", "replit", "anysphere",
         "deel", "cohere", "elevenlabs", "mistral", "modal", "retool", "posthog", "zapier", "glean", "sarvam",
         "mercor", "lovable", "cursor", "harvey", "decagon", "sierra", "writer", "hebbia", "rippling", "airwallex", "plivo", "composio", "ema", "wispr-flow", "spotdraft", "cynlr", "smallest", "freehand", "pramaana-labs", "hang",
    # 9 Oct 2026 research: India AI/backend openings, 0-4 yrs, verified via public APIs
    "handshake", "cartesia", "cerebras", "commure", "level-ai", "tekion", "atlys", "kong", "socure", "aiprise", "plane", "granica", "broccoli", "blitzy", "kognitos", "ciroos", "weave", "Meraki-Labs", "pravah", "bolna", "outmarket", "certifyos", "kaigentic", "Pulsora Inc", "supa"]

# the lists above grew by hand and repeat some slugs (mongodb, rubrik, chime, nutanix, meesho, rippling, ...)
GREENHOUSE, LEVER, ASHBY = dedupe(GREENHOUSE), dedupe(LEVER), dedupe(ASHBY)


@dataclass
class OpenRole:
    company: str
    ats: str
    title: str
    location: str
    posted: datetime | None
    url: str
    apply_url: str
    jd: str
    experience: str
    india_only: bool = True   # False when the role also lists non-Indian locations


def _get(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    return json.load(urllib.request.urlopen(req, timeout=20))


def _q(slug: str) -> str:
    return urllib.parse.quote(slug, safe="")


def _ts(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _greenhouse(board: str) -> list[OpenRole]:
    d = _get(f"https://boards-api.greenhouse.io/v1/boards/{_q(board)}/jobs?content=true")
    # updated_at moves on every edit, so a months-old posting looks fresh: prefer the first-published date
    # apply on Greenhouse's own hosted form (company sites embed it in iframes that are harder to drive)
    return [OpenRole(board, "greenhouse", j["title"], (j.get("location") or {}).get("name", ""),
                     _ts(j.get("first_published") or j.get("created_at") or j.get("updated_at")),
                     j["absolute_url"], f"https://boards.greenhouse.io/embed/job_app?for={_q(board)}&token={j['id']}",
                     html_to_text(j.get("content", "")), "")
            for j in d.get("jobs", [])]


def _lever(co: str) -> list[OpenRole]:
    d = _get(f"https://api.lever.co/v0/postings/{_q(co)}?mode=json")
    out = []
    for j in d:
        cats = j.get("categories") or {}
        loc = " / ".join(filter(None, [cats.get("location", "")] + (cats.get("allLocations") or [])))
        # each list is a titled section ("Requirements", "Nice to have"): keep its heading for the experience parser
        jd = "\n".join([html_to_text(j.get("descriptionPlain") or j.get("description", ""))] + [
            html_to_text(l.get("text", "")) + "\n" + html_to_text(l.get("content", "")) for l in j.get("lists", [])])
        out.append(OpenRole(co, "lever", j["text"], loc, _ts(j.get("createdAt")), j["hostedUrl"], j.get("applyUrl", j["hostedUrl"]), jd, ""))
    return out


def _ashby(org: str) -> list[OpenRole]:
    d = _get(f"https://api.ashbyhq.com/posting-api/job-board/{_q(org)}")
    out = []
    for j in d.get("jobs", []):
        locs = [j.get("location", "")] + [a.get("location", "") for a in j.get("secondaryLocations") or []]
        out.append(OpenRole(org, "ashby", j["title"], " / ".join(filter(None, locs)), _ts(j.get("publishedAt")),
                            j.get("jobUrl", ""), j.get("applyUrl", j.get("jobUrl", "")), html_to_text(j.get("descriptionHtml", "")), ""))
    return out


def survey(profile: dict, max_age_days: int = 14, years: float = 2, slack: int = 1) -> tuple[list[OpenRole], dict]:
    tasks = [(_greenhouse, b) for b in GREENHOUSE] + [(_lever, c) for c in LEVER] + [(_ashby, o) for o in ASHBY]
    stats = {"boards_found": 0, "roles_total": 0, "india": 0}
    roles: list[OpenRole] = []

    def run(t):
        fn, name = t
        try:
            return fn(name)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(run, tasks):
            if res is None:
                continue
            stats["boards_found"] += 1
            stats["roles_total"] += len(res)
            roles.extend(res)
    now = datetime.now(timezone.utc)
    keep = []
    for r in roles:
        if not is_india(r.location):
            continue
        stats["india"] += 1
        r.india_only = india_only(r.location)
        if r.posted and (now - r.posted).days > max_age_days:
            continue
        if title_excluded(r.title, profile) or not title_included(r.title, profile):
            continue
        if too_senior(r.title, r.jd):
            continue
        r.experience = experience_label(r.jd)
        if r.experience and not JobPosting(job_id="", url="", experience=r.experience).experience_fits(years, slack=slack):
            continue
        if skill_overlap_too_low(JobPosting(job_id="", url="", description=r.jd), profile):
            continue
        keep.append(r)
    keep.sort(key=lambda r: r.posted or datetime(2000, 1, 1, tzinfo=timezone.utc), reverse=True)
    keep = dedupe_roles(keep)   # same role reachable from two boards / listed once per location
    stats["fit"] = len(keep)
    return keep, stats
