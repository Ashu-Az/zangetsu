"""Phase-1 survey cache and the filter/ordering rules for phase 2."""
from __future__ import annotations

import re

import json
import time
from dataclasses import asdict
from pathlib import Path

from .config import STATE_DIR, Settings
from .tracker import _write_atomic
from .models import ApplyMode, JobPosting

UNKNOWN_APPLICANTS = 10**9


class SurveyCache:
    """Stores (posting, apply mode) per job id so a re-run within N hours skips re-scraping."""

    def __init__(self, path: Path | None = None, ttl_hours: int = 24):
        self.path = path or STATE_DIR / "survey.json"
        self.ttl = ttl_hours * 3600
        self.data: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}

    def get(self, job_id: str) -> tuple[JobPosting, ApplyMode] | None:
        e = self.data.get(job_id)
        if not e or time.time() - e["ts"] > self.ttl:
            return None
        return JobPosting(**e["job"]), ApplyMode(e["mode"])

    def put(self, job: JobPosting, mode: ApplyMode) -> None:
        self.data[job.job_id] = {"ts": time.time(), "job": asdict(job), "mode": mode.value}
        _write_atomic(self.path, json.dumps(self.data, ensure_ascii=False))


def parse_age_days(text: str) -> float | None:
    """'Posted: Just now' / 'Few Hours Ago' / 'Today' -> 0; '1 day ago' -> 1; '3 Days Ago' -> 3;
    '2 weeks ago' -> 14; '30+ Days Ago' -> 31; unknown -> None."""
    import re
    t = (text or "").lower()
    if not t.strip():
        return None
    # hours count as a fraction of a day, so "2 hours ago" ranks fresher than "19 hours ago" (22 Oct 2026)
    m = re.search(r"(\d+)\s*(minute|min|hour|hr)s?\b", t)
    if m:
        return round(int(m.group(1)) / (1440 if m.group(2).startswith("min") else 24), 3)
    if any(k in t for k in ("just now", "few hours", "hour ago", "hours ago", "minute", "today")):
        return 0.0
    t = re.sub(r"\b(a|an)\s+(day|week|month|year)\b", r"1 \2", t)          # "a year ago" (Wellfound)
    m = re.search(r"(\d+)\s*(\+)?\s*(day|week|month|year)", t)
    if not m:
        return None
    n = int(m.group(1)) + (1 if m.group(2) else 0)
    return float(n * {"day": 1, "week": 7, "month": 30, "year": 365}[m.group(3)])


def parse_salary_lpa(text: str) -> tuple[float, float] | None:
    """'8-12 Lacs P.A.' -> (8, 12); '3.5-6 Lacs PA' -> (3.5, 6); '50,000-75,000 P.A.' -> (0.5, 0.75);
    '15 Lacs P.A.' -> (15, 15); '1-1.5 Cr P.A.' -> (100, 150); 'Not Disclosed' / '' -> None."""
    import re
    t = (text or "").lower().replace(",", "")
    if not t.strip() or "not disclosed" in t or "unpaid" in t:
        return None
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t)]
    if not nums:
        return None
    lo, hi = nums[0], nums[1] if len(nums) > 1 else nums[0]
    if "cr" in t:
        lo, hi = lo * 100, hi * 100
    elif "lac" in t or "lpa" in t or "lakh" in t:
        # mixed units: "50,000-1 Lacs P.A." is 50,000 rupees to 1 lakh
        lo, hi = [n / 100000 if n >= 1000 else n for n in (lo, hi)]
    elif hi >= 1000:                      # plain rupees per annum
        lo, hi = lo / 100000, hi / 100000
    if "month" in t or "p.m" in t:
        lo, hi = lo * 12, hi * 12
    return (lo, hi)


def salary_too_low(salary_text: str, settings: Settings) -> str | None:
    """Reason string if the posting's salary rules it out, else None."""
    if not settings.min_salary_lpa:
        return None
    rng = parse_salary_lpa(salary_text)
    if rng is None:
        return "salary_undisclosed" if settings.skip_undisclosed_salary else None
    # Judge by the top of the range: the floor is negotiable inside a band, so "5-13 Lacs" passes an
    # 8 LPA floor; only a band that cannot reach the floor at all ("3-6 Lacs") is ruled out.
    if rng[1] < settings.min_salary_lpa:
        return f"salary_below_{settings.min_salary_lpa:g}lpa:{salary_text.strip()}"
    return None


def too_old(posted: str, settings: Settings) -> bool:
    if not settings.max_job_age_days:
        return False
    age = parse_age_days(posted)
    return age is not None and age > settings.max_job_age_days


def _kw_in(keyword: str, text: str) -> bool:
    """Short keywords (ai, ml, sde, llm) must be whole words: "ai" must not match "Mumbai",
    "ml" must not match "html". Longer ones match as substrings ("intern" catches "internship")."""
    k = keyword.lower().strip()
    if not k:
        return False
    if len(k) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", text) is not None
    return k in text


GENERIC_TITLES = {"engineer", "developer", "programmer", "software engineer", "executive"}


def title_included(title: str, profile: dict) -> bool:
    inc = profile.get("include_title_keywords") or []
    t = re.sub(r"\s+", " ", title.lower())   # "AI  Security Engineer" (double space) must still match "ai ... engineer" keywords
    core = re.sub(r"[^a-z ]", " ", t).split()
    if " ".join(core) in {"engineer", "developer", "programmer", "executive"}:
        return False  # a bare "Engineer" says nothing about the role (observed: an energy-plant post)
    # punctuation-free copy: "AI / ML Developer" (Amantra, 24 Oct 2026) must match "ai/ml" and "ai ml"
    flat = " ".join(core)
    if not inc or any(_kw_in(k, t) or _kw_in(" ".join(re.sub(r"[^a-z ]", " ", k.lower()).split()), flat) for k in inc):
        return True
    # broad match (Ashu, 25 Oct 2026: "it should do regex not exact search, we don't have to miss any job"): any
    # software / data / AI title counts; non-software roles that share words ("Power Engineer", "Sales Analyst") do not
    tech = re.search(r"\b(develop\w*|programmer|engineer\w*|sde|swe|scientist|analyst|data|ai|ml|llm|nlp|gen ?ai|python|java\w*|"
                     r"react\w*|node\w*|angular|full ?-?stack|front ?-?end|back ?-?end|software|web|cloud|devops|mlops|aiops|"
                     r"automation|vision|database|sql|api|integration|technical|tech|it|application|platform|bi)\b", flat)
    non_software = re.search(r"\b(sales|marketing|hr|recruit\w*|account\w*|finance|valuation|mechanical|civil|electrical|power|"
                             r"hardware|silicon|synthesis|telephony|rf|chemical|biomedical|pharma\w*|clinical|mainframe|"
                             r"customer|support|bpo|teacher|tutor|nurse|doctor|legal|supply chain|logistics coordinator|"
                             r"designer|ux|ui ux|graphic|content|seo|domain manager|business development)\b", flat)
    return bool(tech) and not non_software


def title_excluded(title: str, profile: dict) -> bool:
    # "Member of Technical Staff" is an IC title at every level (Handshake, Composio), not a staff-level role
    t = re.sub(r"members? of (the )?technical staff", "mts", title.lower())
    # EY's "GDS-Staff" / "Staff Consultant" is its junior grade (2-4 yrs), not a staff-level role (Neo, 15 Oct 2026)
    t = re.sub(r"\bgds[- ]staff\b|\bstaff (consultant|analyst)\b", "gds", t)
    # "Senior Associate" / "Senior Analyst" are 2-4 year grades (Amgen, LatentView), not senior roles (24 Oct 2026)
    t = re.sub(r"\b(senior|sr\.?) (associate|analyst)\b", r"\2", t)
    if any(_kw_in(k, t) for k in profile.get("exclude_title_keywords", []) or []):
        return True
    # whole words only: "java" must not exclude "JavaScript Developer", "rust" must not hit "Trust & Safety"
    return any(re.search(rf"(?<![a-z0-9]){re.escape(w.lower())}(?![a-z0-9])", t)
               for w in profile.get("exclude_title_words", []) or [])


def company_excluded(company: str, profile: dict) -> bool:
    c = (company or "").lower()
    return any(k.lower() in c for k in profile.get("exclude_companies", []) or [])


def dedupe_company_title(pairs: list) -> list:
    """Keep only the first (best-ranked) posting per company + title, so one employer cannot flood the list."""
    import re
    seen, out = set(), []
    for job, mode in pairs:
        key = (re.sub(r"[^a-z0-9]+", " ", job.company.lower()).strip(), re.sub(r"[^a-z0-9]+", " ", job.title.lower()).strip())
        if key in seen:
            continue
        seen.add(key)
        out.append((job, mode))
    return out


def candidate_skills(profile: dict) -> set[str]:
    return {s.lower().strip() for s in (profile.get("skills_primary") or []) + (profile.get("skills_secondary") or [])
            + list((profile.get("skill_years") or {}).keys()) if s and s.strip()}


def skill_overlap_too_low(job: JobPosting, profile: dict) -> str | None:
    """Reason string if the posting's key skills share fewer than `min_skill_overlap` of the candidate's skills.
    Counts distinct candidate skills found as whole words, so "llm" does not match inside "llmops"."""
    need = int(profile.get("min_skill_overlap") or 0)
    # LinkedIn / Wellfound pages carry no key-skills list: judge those by the job description instead
    text = (" | ".join(job.skills) if job.skills else job.description).lower()
    if not need or not text.strip():
        return None
    hits = {s for s in candidate_skills(profile)
            if re.search(r"(?<![a-z0-9+#])" + re.escape(s) + r"(?![a-z0-9+#])", text)}
    return None if len(hits) >= need else f"skill_overlap_{len(hits)}_of_{need}"


def eligibility(job: JobPosting, mode: ApplyMode, settings: Settings, profile: dict) -> str | None:
    """Return None if the job should be applied to, else a short reason to skip it."""
    if company_excluded(job.company, profile):
        return "excluded_company"
    if mode in (ApplyMode.ALREADY_APPLIED, ApplyMode.EXPIRED, ApplyMode.UNKNOWN):
        return mode.value
    if title_excluded(job.title, profile):
        return "excluded_title"
    if not title_included(job.title, profile):
        return "title_not_in_include_list"
    if too_old(job.posted, settings):
        return f"too_old:{job.posted.replace('Posted:', '').strip()}"
    low = salary_too_low(job.salary, settings)
    if low:
        return low
    years = float(settings.experience_years) if settings.experience_years else None
    if not job.experience_fits(years, slack=settings.experience_slack):
        return f"experience_mismatch:{job.experience}"
    # Naukri's own match score: require the ticks the profile asks for (only when Naukri showed the block)
    for need in profile.get("require_match") or []:
        if job.match and job.match.get(need.lower()) is False:
            return f"no_match_tick:{need}"
    low_overlap = skill_overlap_too_low(job, profile)
    if low_overlap:
        return low_overlap
    cap = settings.max_applicants if mode == ApplyMode.COMPANY_SITE else settings.max_applicants_direct
    if cap and job.applicants is not None and job.applicants > cap:
        return f"too_many_applicants:{job.applicants}"
    return None


def tier_of(applicants: int | None, tiers: list[int]) -> int:
    if applicants is None:
        return len(tiers) + 1
    for i, bound in enumerate(sorted(tiers)):
        if applicants < bound:
            return i
    return len(tiers)


_PRIORITY: list[str] | None = None


def is_priority_company(company: str) -> bool:
    """Top product companies / GCCs from profile.priority_companies (word match, case-insensitive)."""
    global _PRIORITY
    if _PRIORITY is None:
        try:
            from .config import load_profile
            _PRIORITY = [c.lower() for c in (load_profile().get("priority_companies") or [])]
        except Exception:
            _PRIORITY = []
    c = (company or "").lower()
    return any(re.search(rf"(^|\W){re.escape(p)}(\W|$)", c) for p in _PRIORITY)


def sort_key(job: JobPosting, settings: Settings):
    """Fewest applicants first, then the freshest posting (Ashu, 22 Oct 2026: "increasing order of number of
    applicants and fresh few hours"); top companies and Naukri match ticks only break ties. Unknown counts last."""
    a = job.applicants if job.applicants is not None else UNKNOWN_APPLICANTS
    age = parse_age_days(job.posted)
    return (a, age if age is not None else 99, 0 if is_priority_company(job.company) else 1, -job.match_count())
