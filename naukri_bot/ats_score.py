"""Keyword-coverage heuristic: what share of a job description's hard-skill keywords appear in the resume text.

This is NOT a real ATS score. Recruiter tools (Workday, Greenhouse, Naukri RMS) each rank differently; this only
counts literal, whole-word keyword overlap, case-insensitive, with exact spelling variants of the same name
(Node.js / NodeJS, CI/CD / CICD, Postgres / PostgreSQL). Short ambiguous aliases are deliberately not used:
"js" does not count as JavaScript, "ts" not as TypeScript, "github actions" not as Git, and "rest" only counts
when written as the acronym REST (so "the rest of the team" is not a REST match).

    report(resume_text, jd_text)   -> {score, matched, missing, must_have, nice_to_have, requirements, kind}
    extract_requirements(jd_text)  -> hard-skill keywords from the JD (curated vocabulary + key-skills lines)
    score(resume_text, keywords)   -> (percent, matched, missing) against any keyword list (kept for callers)
"""
from __future__ import annotations

import re

from .grounding import CASE_SENSITIVE, VOCAB

# exact spelling variants of the same name; every member of a group satisfies every other member
_GROUPS = [
    ["node.js", "nodejs", "node js"], ["next.js", "nextjs"], ["React", "react.js", "reactjs"],
    ["vue", "vue.js", "vuejs"], ["Express", "express.js", "expressjs"], ["ci/cd", "cicd", "ci cd", "ci-cd"],
    ["postgresql", "postgres"], ["kubernetes", "k8s"], ["hugging face", "huggingface"],
    ["llm", "llms", "large language model", "large language models"], ["gen ai", "genai", "generative ai"],
    ["rest apis", "rest api", "restful apis", "restful api", "restful", "rest"],
    ["scikit-learn", "scikit learn", "sklearn"], ["golang", "go lang"], ["machine learning", "ml"],
    ["natural language processing", "nlp"],
    ["github actions", "github action"], ["object-oriented programming", "object oriented programming", "oop"],
    ["data structures & algorithms", "data structures and algorithms", "dsa"],
    ["vector database", "vector databases", "vector db"], ["fine-tuning", "fine tuning", "finetuning"],
    ["transact sql", "t-sql", "tsql"], ["power bi", "powerbi"], ["google cloud", "gcp"],
    ["python 3", "python"],
]
VARIANTS: dict[str, list[str]] = {}
for _g in _GROUPS:
    for _t in _g:
        VARIANTS.setdefault(_t.lower(), [])
        VARIANTS[_t.lower()] += [x for x in _g if x != _t and x not in VARIANTS[_t.lower()]]
# one-way: the specific term satisfies the broad keyword, not the reverse
VARIANTS["ai/ml"] = ["ai-ml", "ai ml", "machine learning", "ml"]
VARIANTS["python 3"] = ["python"]
VARIANTS["python"] = []
VARIANTS["github"] = ["github actions"]

# matched case-sensitively: also ordinary English words ("rest", "react", "express", "spring")
_CASE = set(CASE_SENSITIVE) | {"REST", "React"}


def _rx(t: str) -> re.Pattern:
    flags = 0 if t in _CASE else re.I
    if t.lower() == "rest":                                       # the acronym only, never "the rest of"
        t, flags = "REST", 0
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9+#]|\.[A-Za-z])", flags)


def _has(term: str, text: str) -> bool:
    for t in [term] + VARIANTS.get(term.lower(), []):
        if _rx(t).search(text or ""):
            return True
    return False


def score(resume_text: str, keywords: list[str]) -> tuple[int, list[str], list[str]]:
    """(percent, matched, missing) for a keyword list (keyword-coverage heuristic, not a real ATS score)."""
    kws = [k for k in dict.fromkeys(k.strip() for k in keywords) if k]
    if not kws:
        return 0, [], []
    hit = [k for k in kws if _has(k, resume_text)]
    return round(100 * len(hit) / len(kws)), hit, [k for k in kws if k not in hit]


_MUST = re.compile(r"(requirements?|qualifications?|must[\s-]have|what you(?:'|’)?ll need|what we(?:'|’)?re looking for|"
                   r"required|you (?:should )?have|skills? required|key skills|desired candidate profile|who you are)", re.I)
_NICE = re.compile(r"(nice[\s-]to[\s-]have|good[\s-]to[\s-]have|preferred|bonus|plus points?|desirable|"
                   r"added advantage|optional)", re.I)
_OTHER = re.compile(r"(about (?:us|the (?:company|role|team))|responsibilities|what you(?:'|’)?ll do|benefits|perks|"
                    r"why join|role overview|job description|the role|equal opportunity|how to apply)", re.I)


_KEY_SKILLS = re.compile(r"(?i)^\s*(?:key\s+)?skills?(?:\s+required)?\s*[:\-]\s*\S")


def _sections(jd_text: str) -> dict[str, str]:
    """Split the JD into must / nice / other by its headings (short lines naming a section)."""
    parts = {"must": [], "nice": [], "other": []}
    cur, found = "other", False
    for line in (jd_text or "").splitlines():
        s = line.strip().strip("#*:-• ").strip()
        heading = s and len(s) <= 60 and (line.rstrip().endswith(":") or len(s.split()) <= 6)
        if _KEY_SKILLS.match(line):                                # "Key skills: a, b, c" lines are requirements
            cur, found = "must", True
        elif heading and _NICE.search(s):
            cur, found = "nice", True
        elif heading and _MUST.search(s):
            cur, found = "must", True
        elif heading and _OTHER.search(s) and len(s.split()) <= 6:
            cur = "other"
        parts[cur].append(line)
    out = {k: "\n".join(v) for k, v in parts.items()}
    out["found"] = "yes" if found else ""
    return out


_VOCAB_RX = [(t, _rx(t)) for t in sorted(VOCAB, key=len, reverse=True)] + [("REST", _rx("REST"))]


def _vocab_in(text: str) -> list[str]:
    found, taken = [], []
    for term, rx in _VOCAB_RX:
        for t in [term] + VARIANTS.get(term.lower(), []):
            for m in (_rx(t) if t != term else rx).finditer(text or ""):
                if any(a <= m.start() < b or a < m.end() <= b for a, b in taken):
                    continue
                taken.append((m.start(), m.end()))
                if term not in found:
                    found.append(term)
    return found


def _key_skill_lines(text: str) -> list[str]:
    """Items of a 'Key skills: a, b, c' / 'Skills: ...' line (Naukri JDs), each at most 3 words."""
    out = []
    for m in re.finditer(r"(?im)^\s*(?:key\s+)?skills?(?:\s+required)?\s*[:\-]\s*(.+)$", text or ""):
        for it in re.split(r"[,|;•]", m.group(1)):
            it = it.strip(" .")
            if it and len(it.split()) <= 3 and len(it) <= 30:
                out.append(it)
    return out


def _dedupe(terms: list[str]) -> list[str]:
    seen, out = set(), []
    for t in terms:
        keys = {t.lower(), *VARIANTS.get(t.lower(), [])}
        if not (keys & seen):
            out.append(t)
        seen |= keys
    return out


def extract_requirements(jd_text: str) -> list[str]:
    """Hard-skill keywords the job description asks for: curated tech vocabulary found in the JD (requirement
    sections first when the JD has them) plus the items of any 'Key skills:' line."""
    sec = _sections(jd_text)
    ordered = (_vocab_in(sec["must"]) + _vocab_in(sec["nice"]) + _vocab_in(sec["other"])) if sec["found"] else _vocab_in(jd_text)
    return _dedupe(ordered + _key_skill_lines(jd_text))


def report(resume_text: str, jd_text: str) -> dict:
    """Keyword-coverage heuristic (not a real ATS score) of the resume against the JD's own requirements:
    {kind, score, matched, missing (true gaps), requirements, must_have, nice_to_have}. must_have / nice_to_have
    are {score, matched, missing} when the JD has detectable sections, else None."""
    reqs = extract_requirements(jd_text)
    pct, hit, miss = score(resume_text, reqs)
    out = {"kind": "keyword-coverage heuristic (not an ATS score)", "score": pct, "matched": hit, "missing": miss,
           "requirements": reqs, "must_have": None, "nice_to_have": None}
    sec = _sections(jd_text)
    if sec["found"]:
        must = _dedupe(_vocab_in(sec["must"]) + _key_skill_lines(jd_text))
        nice = [t for t in _dedupe(must + _vocab_in(sec["nice"]))[len(must):]]     # a must-have is never also "nice"
        for key, terms in (("must_have", must), ("nice_to_have", nice)):
            if terms:
                p, h, m = score(resume_text, terms)
                out[key] = {"score": p, "matched": h, "missing": m}
    return out


def resume_text(r) -> str:
    parts = [r.headline, r.summary] + [g.name + " " + " ".join(g.items) for g in r.skills]
    parts += [e.title + " " + " ".join(e.bullets) for e in r.experience]
    parts += [p.name + " " + p.description + " " + " ".join(getattr(p, "tech", []) or []) for p in r.projects]
    parts += list(getattr(r, "certifications", []) or [])
    return "\n".join(parts)
