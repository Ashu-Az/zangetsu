"""Shared helpers for the job-discovery modules (top_companies, big_tech, workday_jobs).

- html_to_text: job-description HTML -> plain text that keeps line / bullet breaks (the experience parser needs them)
- is_india / india_only: location tests with word boundaries ("Indianapolis, IN" and "Indiana" are not India)
- min_years_required / experience_required: years of experience a posting asks for, read from its requirements
- too_senior: one seniority rule for every discovery source
- dedupe / dedupe_roles: order-preserving de-duplication of board lists and survey results
"""
from __future__ import annotations

import html
import re
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------- text

def html_to_text(h: str | None) -> str:
    """Strip tags (unescaping twice: Greenhouse double-escapes), keeping block / <br> / <li> breaks as newlines."""
    s = html.unescape(html.unescape(h or ""))
    s = re.sub(r"(?i)<\s*li\b[^>]*>", "\n- ", s)
    s = re.sub(r"(?i)<\s*(?:br|/p|/li|/h[1-6]|/div|/ul|/ol|/tr|p|div|h[1-6]|ul|ol)\b[^>]*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s).replace("\xa0", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r" *\n[\s]*", "\n", s)
    return s.strip()


# ---------------------------------------------------------------- location

_INDIA = re.compile(
    r"\b(?:india|bengaluru|bangalore|hyderabad|pune|gurgaon|gurugram|noida|chennai|mumbai|navi mumbai|new delhi|delhi|"
    r"kolkata|ahmedabad|kochi|cochin|trivandrum|thiruvananthapuram|coimbatore|jaipur|indore|chandigarh|mohali|"
    r"vadodara|nagpur|bhubaneswar|mysore|mysuru|karnataka|telangana|maharashtra|tamil nadu|haryana|kerala)\b", re.I)
_NOT_INDIA = re.compile(r"\bindiana(?:polis)?\b", re.I)
# location parts that say nothing about the country
_NEUTRAL = re.compile(r"^\W*(?:remote|hybrid|on-?site|in-?office|anywhere|multiple locations|\d+\s+locations?)?\W*$", re.I)


def is_india(location: str | Iterable[str] | None) -> bool:
    """True if the location (or any of several locations) is in India."""
    if location is None:
        return False
    text = location if isinstance(location, str) else " / ".join(location)
    return bool(_INDIA.search(_NOT_INDIA.sub(" ", text)))


def _location_parts(location: str | Iterable[str]) -> list[str]:
    items = [location] if isinstance(location, str) else list(location)
    parts: list[str] = []
    for it in items:
        parts += re.split(r"\s*(?:/|;|\||\n|•|\bor\b)\s*", it or "")
    return [p.strip() for p in parts if p and p.strip()]


def india_only(location: str | Iterable[str] | None) -> bool:
    """True if every listed location is in India (country-less parts like 'Remote' are ignored).
    'Hyderabad / London' is in India (is_india) but not India-only."""
    if location is None:
        return False
    parts = [p for p in _location_parts(location) if not _NEUTRAL.match(p)]
    return bool(parts) and all(is_india(p) for p in parts)


# ---------------------------------------------------------------- experience

_HEADS = (r"basic qualifications|minimum qualifications|required qualifications|qualifications|requirements|"
          r"job requirements|required skills|what you(?:'|’| wi)?ll need|what you need|what you bring|"
          r"what we(?:'|’)?re looking for|what we are looking for|you have|you(?:'|’)?ll have|who you are|must[- ]haves?")
_ENDS = (r"preferred qualifications|preferred skills|preferred|nice[- ]to[- ]haves?|good[- ]to[- ]haves?|bonus(?: points)?|"
         r"benefits|perks|what we offer|about us|about the team|about the company|responsibilities|"
         r"what you(?:'|’)?ll do|what you will do|equal opportunity")
_NOT_REQ = r"(?<!preferred )(?<!desired )(?<!bonus )(?<!additional )"
# a heading on its own line, or a heading followed by a colon anywhere (flattened text)
_REQ_START = re.compile(rf"(?im)^[ \t]*(?:[-*•#]+[ \t]*)?(?:{_HEADS})[ \t]*:?[ \t]*$|{_NOT_REQ}\b(?:{_HEADS})[ \t]*:", re.I)
_REQ_END = re.compile(rf"(?im)^[ \t]*(?:[-*•#]+[ \t]*)?(?:{_ENDS})\b[^\n]{{0,30}}$|\b(?:{_ENDS})[ \t]*:", re.I)

_YEARS = re.compile(
    r"(?<![\d.])(?:(?:at\s*least|minimum(?:\s+of)?|min\.?|over|more\s+than)\s+)?"
    r"(?P<lo>\d{1,2})(?:\.\d+)?\s*(?:\+|plus)?\s*"
    r"(?:(?:-|–|—|to)\s*(?P<hi>\d{1,2})(?:\.\d+)?\s*\+?\s*)?"
    r"(?:years?|yrs?)\b", re.I)
_KEYWORD_AFTER = re.compile(
    r"\W*(?:[\w'’/&.-]+\W+){0,6}?(?:experience|experienced|exp|professional|industry|software|development|engineering|"
    r"hands-on|relevant|programming|coding|role|position)\b", re.I)
_KEYWORD_BEFORE = re.compile(r"\b(?:experience|exp)\b(?:\s+(?:required|level|needed))?\s*[:\-–]?\s*$", re.I)
# sentences about the company, not the candidate ("our founders bring 15+ years", "company with 20 years of history")
_ABOUT_COMPANY = re.compile(r"\b(?:we|we've|we're|our|ours|company|founded|founders?|team has|history|in business)\b", re.I)
_ABOUT_CANDIDATE = re.compile(r"\b(?:you|your|candidates?|applicants?|looking for|seeking|requires?|required|must|should|"
                              r"ideal(?:ly)?|need)\b", re.I)
_PREFERRED = re.compile(r"\b(?:preferred|nice to have|good to have|is a plus|a plus|bonus)\b", re.I)


def _requirement_sections(text: str) -> list[str]:
    out, pos = [], 0
    for m in _REQ_START.finditer(text):
        if m.start() < pos:
            continue
        end = _REQ_END.search(text, m.end())
        stop = end.start() if end else len(text)
        out.append(text[m.end():stop])
        pos = stop
    return out


def _segments(text: str) -> list[str]:
    """Lines and inline bullets: a years mention never pairs with words across one of these."""
    segs = []
    for line in text.split("\n"):
        segs += re.split(r"\s+[•·▪●◦]\s*|\s+[-*–]\s+(?=[A-Za-z])", line)
    return [s for s in segs if s.strip()]


def _sentences(seg: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?;])\s+(?=[A-Z(\"'])", seg) if s.strip()]


def _mentions(seg: str, skip_preferred: bool) -> list[tuple[int, int | None]]:
    found = []
    for sent in _sentences(seg):
        if _ABOUT_COMPANY.search(sent) and not _ABOUT_CANDIDATE.search(sent):
            continue
        if skip_preferred and _PREFERRED.search(sent):
            continue
        for m in _YEARS.finditer(sent):
            after = sent[m.end():]
            if re.match(r"\W*(?:ago|old)\b", after, re.I):
                continue
            if not (_KEYWORD_AFTER.match(after) or _KEYWORD_BEFORE.search(sent[max(0, m.start() - 40):m.start()])):
                continue
            lo, hi = int(m.group("lo")), int(m.group("hi")) if m.group("hi") else None
            if lo > 30 or (hi is not None and (hi < lo or hi > 40)):
                continue
            found.append((lo, hi))
    return found


def experience_required(text: str | None) -> tuple[int, int | None] | None:
    """(min, max) years the posting requires, or None if it states none.

    Inside a requirements section every bullet is required, so the binding requirement is the largest per-bullet
    minimum ("3+ yrs software development" + "2+ yrs design" -> 3); alternatives inside one bullet take the smaller.
    Without a section the whole text is scanned and the smallest real mention wins."""
    text = text or ""
    for sec in _requirement_sections(text):
        per_seg = [min(ms) for seg in _segments(sec) if (ms := _mentions(seg, skip_preferred=True))]
        if per_seg:
            return max(per_seg)
    allm = [mm for seg in _segments(text) for mm in _mentions(seg, skip_preferred=False)]
    return min(allm) if allm else None


def min_years_required(text: str | None) -> int | None:
    req = experience_required(text)
    return req[0] if req else None


def experience_label(text: str | None) -> str:
    """'3+ years' / '1 - 3 years' / '' - the format JobPosting.experience_fits reads."""
    req = experience_required(text)
    if not req:
        return ""
    lo, hi = req
    return f"{lo} - {hi} years" if hi is not None else f"{lo}+ years"


# ---------------------------------------------------------------- seniority

_SENIOR_WORDS = re.compile(r"\b(?:senior|sr|staff|principal|lead|manager|architect|director|head|vp|vice president|"
                           r"distinguished|fellow)\b", re.I)
_ROLE = r"(?:sde|swe|sdet|mts|engineer|developer|scientist|analyst|programmer)"
_HIGH_LEVEL = re.compile(rf"\b(?:iii|iv)\b|\b{_ROLE}\s*[-,]?\s*(?:[3-9]|vi?)\b|"
                         r"\b(?:l(?:[5-9]|1\d)|e(?:[4-9]))\b\+?", re.I)
_MID_LEVEL = re.compile(rf"\b{_ROLE}\s*[-,]?\s*(?:ii|2)\b|\bii\b", re.I)


def too_senior(title: str, text: str | None = None) -> bool:
    """True for Senior/Sr/Staff/Principal/Lead/Manager/Architect/Director/Head titles and level III+ / 3+ / L5+ / E4+.
    Level II / 2 counts as too senior only when the description requires 3+ years."""
    t = re.sub(r"(?i)\bmember of technical staff\b", "MTS", title or "")
    if _SENIOR_WORDS.search(t) or _HIGH_LEVEL.search(t):
        return True
    if _MID_LEVEL.search(t):
        need = min_years_required(text)
        return need is not None and need >= 3
    return False


# ---------------------------------------------------------------- de-duplication

def dedupe(items: Iterable[T], key: Callable[[T], object] | None = None) -> list[T]:
    """Order-preserving de-duplication (strings compare case-insensitively)."""
    key = key or (lambda x: x.lower() if isinstance(x, str) else x)
    seen, out = set(), []
    for it in items:
        k = key(it)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def normalize_title(title: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", (title or "").lower()).split())


def dedupe_roles(roles: Iterable[T]) -> list[T]:
    """Keep the first of roles sharing an apply_url, or the same (company, normalized title)."""
    seen_url, seen_ct, out = set(), set(), []
    for r in roles:
        url = (getattr(r, "apply_url", "") or getattr(r, "url", "") or "").strip()
        ct = ((getattr(r, "company", "") or "").strip().lower(), normalize_title(getattr(r, "title", "")))
        if (url and url in seen_url) or ct in seen_ct:
            continue
        if url:
            seen_url.add(url)
        seen_ct.add(ct)
        out.append(r)
    return out
