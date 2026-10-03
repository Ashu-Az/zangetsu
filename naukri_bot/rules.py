"""Answer the standard recruiter questions straight from profile.yaml, with no model.

Covers the questions Naukri's chatbot asks most: notice period, current / expected CTC,
total experience, years in a skill, relocation, current location, and yes/no skill checks.
Returns None for anything else (open-ended prose), so the caller can defer the job.
"""
from __future__ import annotations

import re

from .llm import QuestionAnswer


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _fmt(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else f"{n:g}"


def _skill_years(skill_text: str, profile: dict) -> float | None:
    """Years for the skill named in the question, or 0 if it is not a known skill, or None if no skill is named."""
    t = skill_text.lower()
    table = {k.lower(): float(v) for k, v in (profile.get("skill_years") or {}).items()}
    best = None
    for name, yrs in sorted(table.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"(?<![a-z0-9+#.])" + re.escape(name) + r"(?![a-z0-9+#])", t):
            best = yrs if best is None else max(best, yrs)
    if best is not None:
        return best
    known = [s.lower() for s in (profile.get("skills_primary") or []) + (profile.get("skills_secondary") or [])]
    for s in sorted(known, key=len, reverse=True):
        if re.search(r"(?<![a-z0-9+#.])" + re.escape(s) + r"(?![a-z0-9+#])", t):
            primary = s in [x.lower() for x in profile.get("skills_primary") or []]
            return float(profile.get("relevant_experience_years") or 1) if primary else 1.0
    return None


def _range_of(option: str, unit_hint: str = "") -> tuple[float, float] | None:
    """Numeric range an option label covers: '0-15 days' -> (0, 15), '15 Days or less' -> (0, 15),
    'More than 3 months' -> (91, inf), '1 Month' -> (30, 30), 'Immediate' -> (0, 0), '10-15 Lacs' -> (10, 15)."""
    o = option.lower().replace(",", "")
    if any(k in o for k in ("immediate", "serving notice")) and not re.search(r"\d", o):
        return (0, 0)
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", o)]
    if not nums:
        return None
    mult = 30 if "month" in o else 7 if "week" in o else 1
    if unit_hint == "money":
        mult = 1
    nums = [n * mult for n in nums]
    if "less" in o or "below" in o or "upto" in o or "up to" in o or "within" in o:
        return (0, nums[-1])
    if "more" in o or "above" in o or "+" in o or "greater" in o:
        return (nums[0] + (0 if "+" in o else 1), float("inf"))
    return (min(nums), max(nums)) if len(nums) > 1 else (nums[0], nums[0])


def _pick_bucket(value: float, options: list[str], unit_hint: str = "") -> int | None:
    best, best_width = None, None
    for i, o in enumerate(options):
        r = _range_of(o, unit_hint)
        if r and r[0] <= value <= r[1]:
            width = r[1] - r[0]
            if best is None or width < best_width:
                best, best_width = i, width
    return best


def _yes_no(options: list[str], yes: bool) -> int | None:
    want = "yes" if yes else "no"
    for i, o in enumerate(options):
        if o.strip().lower() == want or o.strip().lower().startswith(want + " ") or o.strip().lower().startswith(want + ","):
            return i
    return None


def _answer(text: str, idx: int | None = None, options: list[str] | None = None) -> QuestionAnswer:
    if idx is not None and options:
        return QuestionAnswer(answer_text=options[idx], selected_option_indices=[idx], reusable=True, skip=False, confidence=0.95)
    return QuestionAnswer(answer_text=text, selected_option_indices=[], reusable=True, skip=False, confidence=0.95)


def rule_answer(question: str, options: list[str], kind: str, profile: dict) -> QuestionAnswer | None:
    q = " ".join(question.lower().split())
    has_opts = bool(options)

    def numeric(value: float | None, unit_hint: str = "") -> QuestionAnswer | None:
        if value is None:
            return None
        if has_opts:
            i = _pick_bucket(value, options, unit_hint)
            return _answer("", i, options) if i is not None else None
        return _answer(_fmt(value))

    # ---- links
    if "linkedin" in q and ("url" in q or "profile" in q or "link" in q):
        v = (profile.get("links") or {}).get("linkedin"); return _answer(v) if v else None
    if "github" in q and ("url" in q or "profile" in q or "link" in q):
        v = (profile.get("links") or {}).get("github"); return _answer(v) if v else None
    if "portfolio" in q and ("url" in q or "link" in q or "website" in q):
        v = (profile.get("links") or {}).get("portfolio"); return _answer(v) if v else None
    # ---- canned answers for open-ended prompts (first match wins)
    for pat, text in (profile.get("canned_answers") or {}).items():
        if re.search(pat, q, re.I):
            return _answer(text) if not has_opts else None
    # ---- notice period / last working day
    if "notice" in q or "last working day" in q or "lwd" in q:
        days = _num(profile.get("notice_period_days"))
        if days is None:
            return None
        if has_opts:
            i = _pick_bucket(days, options)
            return _answer("", i, options) if i is not None else None
        return _answer(_fmt(days))      # digits only: free-text notice fields are usually numeric

    # ---- CTC / salary
    if re.search(r"\b(ctc|salary|compensation|package)\b", q):
        if "expect" in q:
            return numeric(_num(profile.get("expected_ctc_lpa")), "money")
        if "current" in q or "present" in q or "last drawn" in q or "annual" in q:
            return numeric(_num(profile.get("current_ctc_lpa")), "money")
        return None

    # ---- experience
    if "experience" in q or re.search(r"\bhow many years\b", q) or "years of exp" in q:
        asks_yes_no = has_opts and _yes_no(options, True) is not None
        # "Do you have 4-7 years of overall/professional experience?" -> judge the range against real tenure
        rng = re.search(r"(\d+)\s*[-–to+]+\s*(\d+)?\s*\+?\s*(?:years|yrs)", q)
        if asks_yes_no and rng and re.search(r"overall|total|professional|work experience|industry experience", q):
            total = _num(profile.get("total_experience_years"))
            lo = float(rng.group(1)); hi = float(rng.group(2)) if rng.group(2) else float("inf")
            if total is not None:
                return _answer("", _yes_no(options, lo <= total <= hi or (total >= lo and hi == float("inf"))), options)
        total_markers = ("total", "overall", "work experience", "relevant experience", "years of experience do you have?", "professional experience")
        m = re.search(r"experience (?:do you have |have you )?(?:in|with|on|of|using|working (?:in|with|on))\s+(.+?)(?:\?|$)", q) or \
            re.search(r"(?:years|yrs)(?: of)? (?:experience )?(?:in|with|on|using)\s+(.+?)(?:\?|$)", q)
        if m and not any(k in q for k in ("total", "overall")):
            yrs = _skill_years(m.group(1), profile)
            assume = bool(profile.get("assume_skill_experience"))
            if asks_yes_no:
                return _answer("", _yes_no(options, True if assume else bool(yrs and yrs > 0)), options)
            if not yrs:      # unknown skill, or one listed with 0 years
                yrs = float(profile.get("default_skill_years") or 1) if assume else 0.0
            return numeric(yrs)
        if any(k in q for k in total_markers) or re.search(r"experience\??$", q):
            return numeric(_num(profile.get("total_experience_years")))
        return None

    # ---- relocation / location
    if "relocat" in q:
        yes = bool(profile.get("willing_to_relocate"))
        if has_opts:
            i = _yes_no(options, yes)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes" if yes else "No")
    if re.search(r"\b(current|present) (location|city)\b", q) or "where are you (currently )?(located|based)" in q or q.strip(" ?") in ("location", "current location", "city"):
        loc = profile.get("location")
        if not loc:
            return None
        if has_opts:
            for i, o in enumerate(options):
                if str(loc).lower() in o.lower():
                    return _answer("", i, options)
            return None
        return _answer(str(loc))
    if re.search(r"(comfortable|ok|okay|fine|willing|open) (with|to) (working |work )?(from |at |in )?(the )?(office|onsite|on-site|hybrid|wfo|remote|location)", q):
        if has_opts:
            i = _yes_no(options, True)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes")

    # ---- work mode readiness: "Are you ready to work in Hybrid (Pune) mode?", "Ok with WFO?"
    if re.search(r"\b(hybrid|on-?site|work from office|wfo|remote|night shifts?|rotational|office|work from [a-z]+|relocat)\b", q) and re.search(r"ready|comfortable|ok|okay|open|willing|fine|able|can you", q):
        yes = not re.search(r"night shifts?|rotational", q)
        if has_opts:
            i = _yes_no(options, yes); return _answer("", i, options) if i is not None else None
        return _answer("Yes" if yes else "No")
    # ---- "Have you ever appeared for an interview / applied / worked at <company> ...?" -> No
    if re.search(r"have you (ever )?(appeared|attended|interviewed|applied|been interviewed|worked)\b", q) and re.search(r"interview|appl|employ|work", q):
        if has_opts:
            i = _yes_no(options, False); return _answer("", i, options) if i is not None else None
        return _answer("No")
    # ---- availability / joining date
    if re.search(r"available to start|start date|join(ing)? date|when can you (start|join)|how soon|earliest|days (do you need |to |for )?(to )?join|days to start|time to join", q) or ("immediately" in q and "available" in q):
        days = _num(profile.get("notice_period_days"))
        if has_opts:
            i = _yes_no(options, (days or 0) <= 15)
            if i is None and days is not None:
                i = _pick_bucket(days, options)
            return _answer("", i, options) if i is not None else None
        if days is None:
            return None
        if re.search(r"days|notice|how many|number", q):
            return _answer(_fmt(days))
        if re.search(r"date|when|how soon|earliest", q):
            return _answer("Immediately" if days == 0 else f"Within {_fmt(days)} days")
        return _answer("Yes" if days <= 15 else f"Within {_fmt(days)} days")
    # ---- yes/no skill checks: "Do you have hands-on experience..." handled above; "Do you know X?" here
    if has_opts and _yes_no(options, True) is not None and re.search(r"\b(do you know|are you (proficient|skilled|familiar)|have you (worked|used))\b", q):
        yrs = _skill_years(q, profile)
        return _answer("", _yes_no(options, True if profile.get("assume_skill_experience") else bool(yrs and yrs > 0)), options)

    return None
