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
    o = option.lower()
    o = re.sub(r"^\s*(\d{1,2}(?:\.\d+)?)\s*,\s*(\d{1,2}(?:\.\d+)?)\s*(years?|yrs?)?\s*$", r"\1-\2 \3", o)   # "2,4" is a range
    o = o.replace(",", "")
    if any(k in o for k in ("immediate", "serving notice")) and not re.search(r"\d", o):
        return (0, 0)
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", o)]
    if not nums:
        return None
    mult = 30 if "month" in o else 7 if "week" in o else 1
    if unit_hint == "money":
        mult = 1
    nums = [n * mult for n in nums]
    if re.match(r"\s*<\s*=", o):                     # "<=3 years"
        return (0, nums[-1])
    if re.match(r"\s*<", o):                          # "<3 years" excludes 3
        return (0, nums[-1] - 0.01)
    if re.match(r"\s*>", o):                          # ">9 years"
        return (nums[0] + (0 if "=" in o else 0.01), float("inf"))
    if re.search(r"less than|below|under\b|fewer than", o) and "or less" not in o:
        return (0, nums[-1] - 0.01)                 # "Less than 2" excludes 2
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


def with_unit(question: str, text: str) -> str:
    """For free-text boxes, a bare number reads as nonsense ("Current annual salary: 7.5").
    Number inputs keep digits only; textareas get the unit the question implies."""
    if not re.fullmatch(r"\d+(?:\.\d+)?", (text or "").strip()):
        return text
    q = question.lower()
    if float(text) >= 1000 or re.search(r"\((days?|years?|months?|per annum|in lpa|lpa|inr|₹)\)", q):
        return text                                 # rupee amounts, or the label already names the unit: digits only
    if re.search(r"\b(ctc|salary|compensation|package|lpa)\b", q):
        return f"{text} LPA (negotiable)" if re.search(r"expect|desired", q) else f"{text} LPA"
    if re.search(r"notice|days|joining|join|start", q):
        return f"{text} days"
    if re.search(r"years|experience|\bexp\b", q):
        return f"{text} years"
    return text


def _answer(text: str, idx: int | None = None, options: list[str] | None = None) -> QuestionAnswer:
    if idx is not None and options:
        return QuestionAnswer(answer_text=options[idx], selected_option_indices=[idx], reusable=True, skip=False, confidence=0.95)
    return QuestionAnswer(answer_text=text, selected_option_indices=[], reusable=True, skip=False, confidence=0.95)


SENSITIVE = re.compile(
    r"medical (exam|check|test|condition|histor|fitness|certificate)|health (check|condition|issue|problem|declaration|status)"
    r"|good health|physical(ly)? (fit|ailment|condition|disab)|ailment|illness|disab|reasonable accommodation"
    r"|\bdrug|alcohol|substance (abuse|use|test)|pregnan"
    r"|\bbond\b|service agreement|(service|employment|minimum) (commitment|tenure)|lock-?in|penalty|security deposit|\bdeposit\b"
    r"|stipend|unpaid|without (pay|salary)|\b[67] days (a|per|in a) week|six days|long (working )?hours|non-?compete")


def _tenure_required(q: str) -> float | None:
    """Minimum years a question asks for: "5+ years", "3-5 years", "at least 4 years", "minimum 2 yrs"."""
    m = re.search(r"(?:at least|minimum(?: of)?|min\.?|more than|over)\s*(\d+(?:\.\d+)?)\s*(?:years|yrs)", q) or \
        re.search(r"(\d+(?:\.\d+)?)\s*(?:\+|plus)?\s*(?:[-–]|to)?\s*\d*\s*\+?\s*(?:years|yrs)", q)
    return float(m.group(1)) if m else None


def rule_answer(question: str, options: list[str], kind: str, profile: dict) -> QuestionAnswer | None:
    q = " ".join(question.lower().split())
    # Recruiter shorthand and typos seen in the wild: "What is your NP?", "overal year of exp", "YoE".
    q = re.sub(r"\bnp\b", "notice period", q)
    q = re.sub(r"\byoe\b", "years of experience", q)
    q = re.sub(r"\bexp\b\.?", "experience", q)
    q = re.sub(r"\boveral\b", "overall", q)
    q = re.sub(r"\bintr?ested\b|\bintersted\b|\binterseted\b", "interested", q)
    q = q.rstrip(" *:").strip()          # required-field marker: "How many years of Python?\xa0*"
    has_opts = bool(options)

    # date of birth: from profile.date_of_birth (YYYY-MM-DD), in the DD/MM/YYYY form Indian forms expect
    if re.fullmatch(r"(your )?(date of birth|dob|birth ?date)( \(dd/mm/yyyy\))?", q) and profile.get("date_of_birth") and not has_opts:
        y, m, d = str(profile["date_of_birth"]).split("-")
        return _answer(f"{d}/{m}/{y}")

    # middle name: the profile name has none -> "NA" (required text fields reject blanks)
    if re.fullmatch(r"(your )?middle name", q) and not has_opts:
        return _answer(str(profile.get("middle_name") or "NA"))

    # skill-set fields: the confirmed primary skills, then secondary ones (comma list, ~12 items)
    if not has_opts and re.fullmatch(r"(your |primary |key |technical |core |main )?(skill ?sets?|skills|tech(nical)? stack|key skills)( \(.*\))?", q):
        sk = list(dict.fromkeys((profile.get("skills_primary") or []) + (profile.get("skills_secondary") or [])))[:12]
        if sk:
            return _answer(", ".join(sk))

    # ---- never decided on the candidate's behalf: health, disability, drug tests, money/time commitments.
    # Returning None defers the job to the person. Found by review on 9 Oct 2026: the willingness and consent
    # rules were answering "Yes" to medical exams, drug tests, 2-year bonds with penalties and unpaid work.
    # ...except that "have you entered into / are you bound by a non-compete?" is a fact the profile records
    # (restrictive_covenant); only "would you sign one?" is the person's call.
    existing_covenant = re.search(r"non-?compete|restrictive covenant", q) and \
        re.search(r"have you (ever )?(entered|signed|agreed)|are you (currently )?(bound|subject|party)|do you have (any )?(existing|current)", q)
    if SENSITIVE.search(q) and not (existing_covenant and "restrictive_covenant" in profile):
        return None

    # ---- employer uses AI in hiring: consent (same as Coinbase's "may use AI tools", accepted 6 Oct 2026)
    if re.search(r"(uses?|use of|using) (artificial intelligence|ai\b|ai tools)", q) and re.search(r"consent|agree|accept|acknowledge|understand|okay|ok\b|comfortable|confirm|opt-?in", q) or \
       (re.search(r"artificial intelligence|\bai\b", q) and any(re.match(r"\s*opt[- ]?in\b", o, re.I) for o in options)):
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.match(r"\s*(yes|opt[- ]?in|i (agree|consent|accept|understand|acknowledge)|agree|accept)", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes")

    # ---- "if not selected, may we consider you for other roles / keep your profile?" -> Yes
    if re.search(r"(not selected|not shortlisted).{0,80}(other|similar|future|alternate)( \w+)? (roles?|positions?|openings?|opportunit|jobs?)|consider(ed)? (you|your profile)? ?for (other|similar|future)|contact(ing)? you (about|for|regarding|on) (future|other|similar|new)|future (job )?(opportunities|openings|roles)|keep your (profile|resume|cv) (on file|for future)", q):
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.match(r"\s*(yes|y|sure|ok)\b", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes")

    # ---- shift preference as a multi-choice list: day + evening (3pm-12am) OK; night / rotating / US-UK hours no
    if has_opts and kind == "checkbox" and re.search(r"shift", q):
        ok = [k for k, o in enumerate(options) if re.search(r"\bday\b|general|morning|evening|afternoon|\bmid\b", o, re.I)
              and not re.search(r"night|rotat|\b(us|uk)\b|graveyard", o, re.I)]
        return _answer("", ok[0], options) if len(ok) == 1 else (QuestionAnswer(answer_text=", ".join(options[k] for k in ok), selected_option_indices=ok, reusable=True, skip=False, confidence=0.95) if ok else None)

    # ---- identity / right-to-work documents on hire (same answer as Adobe's, 6 Oct 2026)
    if re.search(r"(verification|proof|documentation|documents?) (of|establishing) (your )?(identi(ty|fy)|eligibility|right to work)|verify your identi(ty|fy)", q):
        if has_opts:
            i = _yes_no(options, True)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes")

    # ---- consent to recruiters contacting the candidate by SMS / WhatsApp / text about the application -> yes (more calls)
    if re.search(r"(reach out|contact|message|text|notify) (to )?(me|you).{0,30}\b(sms|text messages?|whatsapp)\b|\b(sms|whatsapp|text message)s?\b.{0,40}(consent|agree|opt[- ]?in)", q):
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.match(r"\s*(yes|i agree|agree|opt[- ]?in|i consent)", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes")

    # ---- "select the city you are residing in or willing to relocate to": Delhi when listed, else the only city on offer
    if has_opts and re.search(r"city.{0,40}(resid|live|based|relocate)|(resid|relocate).{0,40}\bcity\b", q):
        i = next((k for k, o in enumerate(options) if re.search(r"\b(new )?delhi\b|\bncr\b", o, re.I)), None)
        if i is None and profile.get("willing_to_relocate"):
            real = [k for k, o in enumerate(options) if o.strip() and not re.search(r"^(select|other|none)", o, re.I)]
            i = real[0] if len(real) == 1 else None
        return _answer("", i, options) if i is not None else None

    # ---- "This is a work from office opportunity - <place>" (a statement to acknowledge): OK, Ashu will relocate
    if profile.get("willing_to_relocate") and re.search(r"(this is|it is|it's|role is) an? (work from office|wfo|on-?site|office[- ]based)", q) \
            and not re.search(r"night|rotational|shift", q):
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.match(r"\s*(yes|ok|okay|sure|i am (fine|ok|comfortable)|agree|comfortable|interested|fine)", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes, I'm okay with working from the office and willing to relocate")

    # ---- face-to-face / in-person / walk-in interview rounds: only in Delhi NCR (Ashu, 8 Oct 2026)
    cities = [c.lower() for c in profile.get("in_person_interview_cities") or []]
    if cities and re.search(r"face[ -]?to[ -]?face|\bf2f\b|in[- ]person (interview|round|discussion)|walk[- ]?in|offline (interview|round)|visit (our|the) office for", q):
        local = any(c in q for c in cities)
        named_elsewhere = re.search(r"\b(bengaluru|bangalore|pune|hyderabad|chennai|mumbai|kolkata|ahmedabad|kochi|indore|jaipur|dehradun|chandigarh|coimbatore|mysore|nagpur|trivandrum)\b", q)
        if local or named_elsewhere:
            yes = bool(local and not named_elsewhere)
            if has_opts:
                i = _yes_no(options, yes)
                return _answer("", i, options) if i is not None else None
            return _answer("Yes" if yes else "No, I can attend virtual rounds, or in person in Delhi NCR")

    # ---- "where are you residing in <city> (area)?" for a city other than Delhi: say where, and relocation
    m = re.search(r"(where|which area|which locality).{0,30}(resid|liv|stay|located|based).{0,12} in ([a-z ]+?)(\(|\?|$| area)", q)
    if m and not has_opts and not re.search(r"delhi|ncr|noida|gurgaon|gurugram", m.group(3)):
        reloc = " and willing to relocate to " + m.group(3).strip().title() if profile.get("willing_to_relocate") else ""
        area = profile.get("locality")
        return _answer(f"Currently in {profile.get('location', 'Delhi')}" + (f" ({area})" if area else "") + reloc)

    # ---- military / armed-forces service (profile.veteran is False)
    if profile.get("veteran") is False and re.search(r"(served|serve|service) in (the )?(military|armed forces|army|navy|air force|defen[cs]e)|\bex-?servicem[ae]n\b|\bveteran\b", q):
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.match(r"\s*(no|n)\b|\s*i am not|not a (protected )?veteran", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer("No")

    # ---- contract-to-hire (C2H): open to it (Ashu, 8 Oct 2026); plain contracts / bonds keep their own rules
    if profile.get("open_to_contract_to_hire") and re.search(r"contract[- ]to[- ]hire|\bc2h\b|contract to permanent|contract-to-perm", q):
        if has_opts:
            i = _yes_no(options, True)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes")
    # ---- name exactly as on Aadhaar / government ID: confirmed
    if has_opts and profile.get("name_matches_id_documents") and re.search(r"name.{0,40}(exactly )?as (shown |it appears |mentioned )?(on|in) (my |your )?(aadha+r|pan|passport|government|govt)", q + " " + " ".join(options)):
        i = next((k for k, o in enumerate(options) if re.search(r"(provided|entered|is) my name exactly as|exactly as (shown|on)", o, re.I)), None)
        return _answer("", i, options) if i is not None else None

    # ---- simple facts: age 18+, notice period yes/no, school/college marks
    if re.search(r"(at least|over|above) 18( years)?( of age| years old)?|18 years (of age|or older)|legal (working )?age", q):
        if has_opts:
            i = _yes_no(options, True)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes")
    if re.fullmatch(r"(do|does) you have (a |any )?notice period\??", q):
        days = _num(profile.get("notice_period_days"))
        if has_opts:
            i = _yes_no(options, bool(days))
            return _answer("", i, options) if i is not None else None
        return _answer(f"Yes, {_fmt(days)} days" if days else "No")
    for pat, key in ((r"^(10th|class 10|ssc|matric)\b.{0,25}(%|percent|marks|score|cgpa)", "marks_10th_percent"),
                     (r"^(12th|class 12|hsc|intermediate)\b.{0,25}(%|percent|marks|score|cgpa)", "marks_12th_percent"),
                     (r"^(grad(uation)?|ug|b\.?tech|degree|bachelor'?s?)\b.{0,25}(%|percent|marks|score)", "marks_grad_percent")):
        if not has_opts and re.search(pat, q) and profile.get(key) is not None:
            return _answer(_fmt(_num(profile.get(key))))

    # ---- government IDs / bank details: never shared in a pre-screening form (Ashu, 8 Oct 2026)
    if re.search(r"\bpan\b( card)?( number| no)?|aadha+r|passport (number|no)|bank account|ifsc|uan number|\bssn\b|voter id|driving licen[cs]e number", q) \
            and not re.search(r"pandemic|panel|company|japan|expan", q):
        if has_opts:
            return None
        return _answer("I'll share this after shortlisting / at the offer stage for background verification.")

    # ---- "What area have you most experience in?" with choices: AI/ML first (Ashu: "Backend / AI (LLM, RAG, FastAPI)")
    if has_opts and re.search(r"(area|domain|field)s? (have|do) you (have )?(most|the most|maximum) experience|main area of (experience|expertise)|area of expertise", q):
        for pat in (r"^ai\b|ai ?/ ?ml|machine learning|artificial intelligence|gen ?ai", r"back-?end", r"full[- ]?stack", r"python"):
            i = next((k for k, o in enumerate(options) if re.search(pat, o, re.I)), None)
            if i is not None:
                return _answer("", i, options)
        return None


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
        links = profile.get("links") or {}
        v = links.get("portfolio") or links.get("github"); return _answer(v) if v else None
    # ---- canned answers for open-ended prompts (first match wins)
    for pat, text in (profile.get("canned_answers") or {}).items():
        if re.search(pat, q, re.I):
            return _answer(text) if not has_opts else None
    # ---- consent / acknowledgement prompts ("I am allowing X to contact me", "I agree to the privacy policy")
    if re.search(r"\bi (agree|consent|acknowledge|confirm|accept|am allowing|allow|authori[sz]e|certify|declare|understand)\b|privacy (policy|notice)|terms (and|&) conditions", q):
        if has_opts:
            i = _yes_no(options, True)
            if i is None:
                i = next((k for k, o in enumerate(options) if re.search(r"agree|accept|i consent|acknowledge", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes")

    # ---- referral / former employee: the candidate was not referred and has not worked there
    if re.search(r"referred by|referral|who referred|employee referral", q):
        if has_opts:
            i = _yes_no(options, False)
            return _answer("", i, options) if i is not None else None
        return _answer("Not referred")
    if re.search(r"\bex[- ]?\w*\s+employee|former employee|previously (worked|employed)|worked (with|for|at) (us|this company|our company) (before|earlier|previously)", q):
        if has_opts:
            i = _yes_no(options, False)
            return _answer("", i, options) if i is not None else None
        return _answer("No")

    # ---- one free-text box asking for several facts: "current CTC & notice period", "expected CTC and NP"
    asks_ctc = bool(re.search(r"\b(ctc|salary|compensation|package)\b", q))
    if not has_opts and asks_ctc and "notice" in q:
        parts = []
        if "current" in q or "present" in q or not ("expect" in q):
            parts.append(f"Current CTC: {_fmt(_num(profile.get('current_ctc_lpa')) or 0)} LPA")
        if "expect" in q:
            parts.append(f"Expected CTC: {_fmt(_num(profile.get('expected_ctc_lpa')) or 0)} LPA")
        parts.append(f"Notice period: {_fmt(_num(profile.get('notice_period_days')) or 0)} days")
        return _answer("; ".join(parts))

    # ---- general experience in years ("How many years of relevant work experience do you have?"): not a skill named "relevant work"
    yes_no_opts = has_opts and all(re.match(r"\s*(yes|no)\b", o, re.I) for o in options)
    if not yes_no_opts and not re.search(r"\d+\s*[-–to+]", q) and not re.match(r"^(do|does|have|has|are|is) you\b", q) and \
            re.search(r"(how many|no\.? of|number of|total|overall) (years|yrs)? ?(of )?(your )?(relevant|total|overall|professional|work|industry|it)( work| professional| industry)? experience", q) \
            or re.fullmatch(r"(years of |yrs of )?(relevant|total|overall|professional|work)( work)? experience( in years)?\??", q):
        yrs = _num(profile.get("relevant_experience_years" if "relevant" in q else "total_experience_years"))
        if yrs is not None:
            if has_opts:
                i = _pick_bucket(yrs, options)
                return _answer("", i, options) if i is not None else None
            return numeric(yrs)
    # ---- notice buy-out: not needed, the last working day is already set
    if re.search(r"buy ?-?out", q) and profile.get("serving_notice") and profile.get("last_working_day"):
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.search(r"not (needed|required|applicable)|n/?a|^no\b", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer(f"Not needed - my last working day is {profile['last_working_day']}")

    # ---- free-text yes/no about skills/experience ("Do you have hands-on experience with X? If yes, describe",
    # "Must have exp in Python, AWS, LangChain"). A number here reads as nonsense to a recruiter.
    yes_no_form = re.match(r"^(do|does|have|has|are|is|can|did|would|will) you\b", q) or q.startswith(("must have", "should have"))
    skill_topic = re.search(r"experience|hands[- ]on|familiar|worked (with|on)|knowledge|exposure|proficien|comfortable|used\b", q)
    # work-condition questions have their own rules (night shifts -> No, relocation, office days, ...)
    if re.search(r"shift|night|weekend|travel|relocat|commut|office|on-?site|hybrid|remote|wfo|bond|contract|notice|ctc|salary", q):
        skill_topic = None
    assume = bool(profile.get("assume_skill_experience"))
    tenure_range = re.search(r"\b(\d+)\s*[-–to+]+\s*(\d+)?\s*\+?\s*(years|yrs)", q)
    if yes_no_form and skill_topic and not tenure_range and _tenure_required(q) is None:
        # "Yes" only for a skill the candidate lists. Anything else goes to the model (or defers):
        # a blanket yes is a claim the candidate may not be able to back up in an interview.
        known = bool(_skill_years(q, profile))
        asks_detail = re.search(r"describe|explain|elaborate|briefly|details?|share|how|which", q)
        if known or assume:
            if has_opts:
                i = _yes_no(options, True)
                if i is not None:
                    return _answer("", i, options)
            elif not asks_detail:      # a description must be specific to the skill: leave it to the model
                return _answer("Yes.")
            return None
        # not a listed skill (or listed with 0 years): fall through to the experience rules below,
        # which answer from the skill_years table (0 years -> No)

    # ---- resigned? (serving notice means the resignation is in)
    if re.search(r"have you (already )?resigned|resignation (been )?(submitted|accepted|put)", q):
        yes = bool(profile.get("serving_notice"))
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.match(r"\s*(yes|y)\b" if yes else r"\s*(no|n)\b", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes" if yes else "No")

    # ---- preferred employment type / links
    if re.search(r"(preferred|desired) (type of )?employment|employment type|type of (job|employment|role) (are you|do you)", q):
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.search(r"full[- ]?time|permanent", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer("Full-time")
    links = profile.get("links") or {}
    if not has_opts and re.fullmatch(r"(personal |portfolio )?(website|portfolio)( url| link)?", q) and (links.get("portfolio") or links.get("github")):
        return _answer(links.get("portfolio") or links.get("github"))

    # ---- bankruptcy / financial delinquency / litigation (confirmed none)
    if (profile.get("no_legal_issues") and re.search(r"bankrupt|financial delinquen|litigation|insolven", q)) or \
       (profile.get("no_criminal_record") and re.search(r"criminal|convicted|conviction|offen[cs]e", q)) or \
       (profile.get("no_terminations") and re.search(r"(suspended|terminated|dismissed|fired|let go)", q)
            and re.search(r"employ|job|service|position|work", q)):
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.match(r"\s*no\b", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer("No")

    # ---- "are you based out of <city>?" -> truthful for Delhi/NCR; elsewhere No + relocation note
    # only when the question asks where the CANDIDATE is ("are you based out of X?"), never "this role is based in X, are you ok?"
    m = re.search(r"(?:are you|do you|you are|currently) (?:currently )?(based|located|living|residing|reside|live) (out of|in|at|near) ([a-z .]+?)(\?|,|$| and | or )", q)
    if m and not re.search(r"india\b|country", m.group(3)):
        here = re.search(r"delhi|ncr|noida|gurgaon|gurugram|ghaziabad|faridabad", m.group(3))
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.match(r"\s*(yes|y)\b" if here else r"\s*(no|n)\b", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        if here:
            where = ", ".join(x for x in [profile.get("locality"), profile.get("city_area") or profile.get("location")] if x)
            return _answer(where if re.match(r"(where|which|what)", q) and where else f"Yes, I'm based in {profile.get('location', 'Delhi')}")
        return _answer("No, I'm based in Delhi, but I'm willing to relocate" if profile.get("willing_to_relocate") else "No, I'm based in Delhi")

    # ---- prior employment at the hiring company: never worked there
    if re.search(r"prior .{0,25}employment (end|start) date|date (you )?(left|leaving) .{0,20}(company|employment)|"
                 r"if (you )?(previously|formerly) (worked|employed)", q) and not has_opts:
        return _answer("N/A - never employed there")

    # ---- "are you serving notice?" (yes/no)
    if re.search(r"(are you|currently) (currently )?serving (your |the )?notice|serving notice period\??$", q) and \
            not re.search(r"last working|lwd|how many|days", q):
        yes = bool(profile.get("serving_notice"))
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.match(r"\s*(yes|y)\b" if yes else r"\s*(no|n)\b", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes" if yes else "No")

    # ---- last working day: only meaningful while serving notice
    asks_day_count = re.search(r"how many days|no\.? of days|number of days", q) and "if serving" not in q
    if re.search(r"last working (day|date)|\blwd\b|relieving date", q) and not asks_day_count:
        if profile.get("serving_notice") and profile.get("last_working_day"):
            return _answer(str(profile["last_working_day"])) if not has_opts else None
        if not profile.get("serving_notice"):
            if has_opts:
                i = next((k for k, o in enumerate(options) if re.search(r"not serving|n/?a|no\b", o, re.I)), None)
                return _answer("", i, options) if i is not None else None
            return _answer("Not serving notice")
        return None

    # ---- notice period / days left to the last working day / time to start after an offer
    if ("notice" in q or "last working day" in q or re.search(r"\blwd\b", q) or (has_opts and re.search(r"how (soon|early|quickly) (can |could )?you (can )?join", q))
            or re.search(r"(after|of) accepting (an |the )?offer|before (you can )?start(ing)? work", q)):
        days = _num(profile.get("notice_period_days"))
        if days is None:
            return None
        if has_opts:
            i = _pick_bucket(days, options)
            if i is None:               # "Immediate / 1 Month / 2 Months": the shortest option that still covers the notice
                fits = [(rng[1], k) for k, o in enumerate(options) if (rng := _range_of(o)) and rng[1] >= days]
                i = min(fits)[1] if fits else None
            return _answer("", i, options) if i is not None else None
        return _answer(_fmt(days))      # digits only: free-text notice fields are usually numeric

    # ---- CTC / salary, in the unit the question asks for (LPA by default; rupees / monthly when stated)
    if re.search(r"\b(ctc|salary|compensation|package)\b|(annual|salary|pay|ctc) expectations?|expected (pay|annual)", q):
        if re.search(r"expect|desired|desire|salary expectation|looking for", q):
            lpa = _num(profile.get("expected_ctc_lpa"))
        elif "current" in q or "present" in q or "last drawn" in q or "annual" in q:
            lpa = _num(profile.get("current_ctc_lpa"))
        else:
            return None
        in_rupees = re.search(r"\b(inr|rupees|rs)\b|₹|per annum|annual|p\.a\.", q) and not re.search(r"lakh|lac|lpa", q)
        monthly = re.search(r"per month|monthly|/\s*month", q)
        if lpa is not None and not has_opts and (in_rupees or monthly):
            rupees = lpa * 100000 / (12 if monthly else 1)
            return _answer(str(int(round(rupees))))
        return numeric(lpa, "money")

    # ---- "additional months" of experience (LinkedIn asks years and months separately)
    if re.search(r"additional months|months of (total )?experience|experience.{0,20}\bmonths\b", q) and "years" not in q.split("months")[0][-15:]:
        total = _num(profile.get("total_experience_years"))
        if total is not None:
            months = int(round((total - int(total)) * 12))
            if has_opts:
                i = next((k for k, o in enumerate(options) if o.strip() == str(months)), None)
                return _answer("", i, options) if i is not None else None
            return _answer(str(months))
    # ---- experience
    if "experience" in q or re.search(r"\bhow many years\b", q) or "years of exp" in q:
        asks_yes_no = has_opts and _yes_no(options, True) is not None
        # "Do you have 4-7 years of overall/professional experience?" -> judge the range against real tenure
        rng = re.search(r"(\d+)\s*[-–to+]+\s*(\d+)?\s*\+?\s*(?:years|yrs)", q)
        if not has_opts and rng and re.match(r"^(do|have|are) you", q) and re.search(r"overall|total|professional|work experience|industry experience|experience", q):
            total = _num(profile.get("total_experience_years"))
            if total is not None:
                return _answer(f"I have {_fmt(total)} years of total professional experience.")
        if asks_yes_no and rng and re.search(r"overall|total|professional|work experience|industry experience", q):
            total = _num(profile.get("total_experience_years"))
            lo = float(rng.group(1)); hi = float(rng.group(2)) if rng.group(2) else float("inf")
            if total is not None:
                return _answer("", _yes_no(options, lo <= total <= hi or (total >= lo and hi == float("inf"))), options)
        total_markers = ("total", "overall", "work experience", "relevant experience", "years of experience do you have?", "professional experience",
                         "experience in years", "experience (years)", "experience (in years)", "years of experience")
        m = re.search(r"experience (?:do you have |have you )?(?:in|with|on|of|using|working (?:in|with|on))\s+(.+?)(?:\?|$)", q) or \
            re.search(r"(?:years|yrs)(?: of)? (?:experience )?(?:in|with|on|using)\s+(.+?)(?:\?|$)", q) or \
            re.search(r"(?:years|yrs) of (?:hands-on |professional |relevant |practical )?(.+?) (?:experience|exp)\b", q)
        if m and re.fullmatch(r"\s*(years?|yrs?|months?)\b.*", m.group(1)):
            m = None                                   # "Experience in Years" is the unit, not a skill called "years"
        if m and not any(k in q for k in ("total", "overall")):
            yrs = _skill_years(m.group(1), profile)
            assume = bool(profile.get("assume_skill_experience"))
            need = _tenure_required(q)
            if need is not None:
                # "Do you have 5+ years of Python?" is a claim about tenure: answer from real years only.
                # None = no specific skill named ("AI/GenAI development"): a domain question, judge by total tenure.
                # 0 = a named skill the candidate does not list: 0 years.
                have = yrs if yrs is not None else (_num(profile.get("total_experience_years")) or 0.0)
                if asks_yes_no:
                    return _answer("", _yes_no(options, have >= need), options)
                if not has_opts and re.match(r"^(do|have|are) you", q):
                    return _answer(f"I have {_fmt(have)} years of hands-on experience with this." if have
                                   else "Not yet at that level; I have worked with related tools.")
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
    if re.search(r"\b(current|present) (location|city)\b", q) and not re.search(r"sponsor|visa|authori[sz]", q) or "where are you (currently )?(located|based)" in q or q.strip(" ?") in ("location", "current location", "city"):
        loc = profile.get("location")
        if not loc:
            return None
        if has_opts:
            for i, o in enumerate(options):
                if str(loc).lower() in o.lower() or re.search(r"\b(new delhi|delhi ncr|ncr)\b", o, re.I):
                    return _answer("", i, options)
            i = next((k for k, o in enumerate(options) if re.match(r"\s*(other|others|none of the above)\b", o, re.I)), None)
            return _answer("", i, options) if i is not None else None      # city not listed: "Other", never a city we don't live in
        return _answer(str(loc))
    if re.search(r"(comfortable|ok|okay|fine|willing|open) (with|to) (working |work )?(from |at |in )?(the )?(office|onsite|on-site|hybrid|wfo|remote|location)", q):
        if has_opts:
            i = _yes_no(options, True)
            return _answer("", i, options) if i is not None else None
        return _answer("Yes")

    # ---- work mode readiness: "Are you ready to work in Hybrid (Pune) mode?", "Ok with WFO?"
    if re.search(r"\b(hybrid|on-?site|work from office|wfo|remote|night shifts?|rotational|office|work from [a-z]+|relocat|shifts?)\b", q) and re.search(r"ready|comfortable|ok|okay|open|willing|fine|able|can you|interested|prefer|agree", q):
        # evening shifts are fine; night work (incl. US/UK/EST hours from India) and rotational are not
        yes = not re.search(r"night shifts?|rotational|\b(est|pst|cst|us|uk|usa)\b.{0,12}(shift|hours|time)|graveyard", q)
        if has_opts:
            i = _yes_no(options, yes); return _answer("", i, options) if i is not None else None
        return _answer("Yes" if yes else "No")
    # ---- "Have you ever appeared for an interview / applied / worked at <company> ...?" -> No
    if re.search(r"have you (ever )?(appeared|attended|interviewed|applied|been interviewed)\b", q) or (
            re.search(r"have you (ever )?(worked|been employed)\b", q)
            and re.search(r"\b(before|earlier|previously|in the past)\b|(with|for|at) (us|this (company|organi[sz]ation)|our)\b", q)):
        if has_opts:
            i = _yes_no(options, False); return _answer("", i, options) if i is not None else None
        return _answer("No")
    # ---- availability / joining date
    if re.search(r"available to (start|join)|start date|join(ing)? date|when can you (start|join)|can you (start|join)|(join|start) within|how soon|earliest|days (do you need |to |for )?(to )?join|days to start|time to join", q) or ("immediately" in q and "available" in q):
        days = _num(profile.get("notice_period_days"))
        # What window is the question asking about? "immediately" means about a week at most;
        # "within 30 days" / "2 weeks" / "1 month" gives the window explicitly.
        window = 15.0
        w = re.search(r"within (\d+)\s*(day|week|month)", q)
        if w:
            window = float(w.group(1)) * {"day": 1, "week": 7, "month": 30}[w.group(2)]
        elif "immediate" in q:
            window = 7.0
        fits = days is not None and days <= window
        if has_opts:
            i = _yes_no(options, fits)
            if i is None and days is not None:
                i = _pick_bucket(days, options)
            return _answer("", i, options) if i is not None else None
        if days is None:
            return None
        if re.search(r"days|notice|how many|number", q) and not w:
            return _answer(_fmt(days))
        if re.search(r"date|when|how soon|earliest", q):
            return _answer("Immediately" if days == 0 else f"Within {_fmt(days)} days")
        return _answer("Yes" if fits else f"I can join within {_fmt(days)} days (notice period).")
    # ---- work authorization / visa sponsorship: answered for the country the candidate is authorized in (India).
    # The bot only applies to India-based roles; a question naming another country gets no automatic answer.
    auth_country = str(profile.get("work_authorized_in") or "").lower()
    if auth_country and re.search(r"(legally )?authori[sz]ed (\(or eligible\) |or eligible )?to work|eligible to work|right to work|work permit|"
                                  r"verification of (both )?your identity and authori[sz]ation", q):
        other = re.search(r"\b(united states|usa|u\.s\.|uk|united kingdom|canada|europe|germany|singapore|australia)\b", q)
        if not other:
            if has_opts:
                i = _yes_no(options, True); return _answer("", i, options) if i is not None else None
            return _answer("Yes")
    if auth_country and re.search(r"(require|need).{0,40}sponsorship|visa sponsorship|sponsor.{0,20}visa", q):
        other = re.search(r"\b(united states|usa|u\.s\.|uk|united kingdom|canada|europe|germany|singapore|australia)\b", q)
        if not other:
            if has_opts:
                i = _yes_no(options, False); return _answer("", i, options) if i is not None else None
            return _answer("No")
    # ---- restrictive covenants / non-compete (a fact the candidate confirmed in the profile)
    if "restrictive_covenant" in profile and re.search(r"restrictive covenant|non-?compete|non-?solicit", q):
        yes = bool(profile["restrictive_covenant"])
        if has_opts:
            i = _yes_no(options, yes); return _answer("", i, options) if i is not None else None
        return _answer("Yes" if yes else "No")
    # ---- GPA / CGPA / percentage of the degree
    if profile.get("gpa") and re.search(r"\b(c?gpa|grade point|overall result|cumulative grade)\b", q):
        return _answer(str(profile["gpa"])) if not has_opts else None
    # ---- conflict-of-interest declarations (confirmed "No" for all by the candidate; see profile.no_conflicts_of_interest)
    if profile.get("no_conflicts_of_interest") and re.search(
            r"related to anyone|close personal relationship|relative.{0,40}(employee|work)|government (office|agency|official)|"
            r"any relatives|covered relationships?|senior management|"
            r"oversight over|negotiate.{0,60}contracts|sign commercial|outside employment|outside activit|conflict of interest|"
            r"politically exposed", q):
        if has_opts:
            i = _yes_no(options, False); return _answer("", i, options) if i is not None else None
        return _answer("No")
    # ---- "Are you an Indian citizen?" -> compare with the profile's nationality
    m_cit = re.search(r"are you an? (\w+) (citizen|national)\b", q)
    if m_cit and profile.get("nationality"):
        yes = m_cit.group(1).lower()[:4] == str(profile["nationality"]).lower()[:4]
        if has_opts:
            i = _yes_no(options, yes); return _answer("", i, options) if i is not None else None
        return _answer("Yes" if yes else "No")
    # ---- nationality / citizenship
    if re.search(r"\bnationality\b|\bcitizenship\b", q) and profile.get("nationality"):
        nat = str(profile["nationality"])
        if has_opts:
            i = next((k for k, o in enumerate(options) if nat.lower()[:5] in o.lower()), None)
            return _answer("", i, options) if i is not None else None
        return _answer(nat)
    # ---- highest qualification
    if re.search(r"highest (qualification|education|degree)|educational qualification", q) and profile.get("highest_education"):
        if has_opts:
            i = next((k for k, o in enumerate(options) if re.search(r"b\.?\s?tech|b\.?e\b|bachelor|graduat|under.?grad", o, re.I)
                      and not re.search(r"post|master|m\.?tech", o, re.I)), None)
            return _answer("", i, options) if i is not None else None
        return _answer(str(profile["highest_education"]))
    # ---- driving licence: only from the profile (a fact, never assumed)
    if re.search(r"driv(er|ing)['’]?s? licen[cs]e", q):
        if "driving_licence" not in profile:
            return None
        yes = bool(profile["driving_licence"])
        if has_opts:
            i = _yes_no(options, yes); return _answer("", i, options) if i is not None else None
        return _answer("Yes" if yes else "No")
    # ---- generic willingness: background checks, fast-paced teams, learning new tools ...
    if (re.search(r"\b(are you|would you be|will you be|r u)\s+(comfortable|willing|ok|okay|open|fine|ready|able)\b", q)
            or re.search(r"background (check|verification)|\bbgv\b", q)) and \
            not re.search(r"night|rotational|shift|weekend|bond|contract|c2h|third[- ]party|relocat|travel|salary|ctc|pay cut", q) \
            and profile.get("default_willingness"):
        yes = str(profile["default_willingness"]).lower().startswith("y")
        if has_opts:
            i = _yes_no(options, yes); return _answer("", i, options) if i is not None else None
        return _answer("Yes" if yes else "No")
    # ---- yes/no skill checks: "Do you have hands-on experience..." handled above; "Do you know X?" here
    if has_opts and _yes_no(options, True) is not None and re.search(r"\b(do you know|are you (proficient|skilled|familiar)|have you (worked|used))\b", q):
        yrs = _skill_years(q, profile)
        if yrs is None and not profile.get("assume_skill_experience"):
            return None
        return _answer("", _yes_no(options, True if profile.get("assume_skill_experience") else bool(yrs and yrs > 0)), options)

    return None


def availability_date(profile: dict):
    """First day the candidate can join: the day after the last working day when serving notice, else today + notice."""
    from datetime import date, datetime, timedelta
    lwd = str(profile.get("last_working_day") or "").strip()
    if profile.get("serving_notice") and lwd:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(lwd, fmt).date() + timedelta(days=1)
            except ValueError:
                pass
    days = _num(profile.get("notice_period_days"))
    return date.today() + timedelta(days=int(days or 0)) if days is not None else None
