"""Local resume-to-job match score modelled on how enterprise matching engines (Textkernel/Sovren "Bimetric"
scoring) are described: the resume and the job description are compared category by category, and the categories
the job actually specifies are weighted into one 0-100 score.

    skills          the JD's hard skills (ats_score.extract_requirements: vocabulary, Naukri key-skill tags, JD
                    phrases, the job's own skill list). A skill shown in work experience or projects counts in full;
                    one that only sits in the skills list counts 0.8 (engines rank experience-backed skills higher).
    job_title       the JD title against the resume headline and job titles (normalised word overlap)
    experience      the resume's total years against the JD's "X - Y years" range
    education       the JD's degree requirement (any graduate / bachelor's / engineering / master's) against the resume
    domain          the role family (ML/AI, backend, full-stack, data, frontend, devops, mobile, QA) against the resume
    certifications  only when the JD names one

A category the JD says nothing about is left out and the weights are re-normalised, as the engines do. This is a
local model of that method, not a call to Textkernel: the scale is the same idea, the numbers are ours.

    bimetric(resume, jd_text, title="", skills=None, profile=None) -> {"score", "categories": {name: {...}}, "fixable"}
"""
from __future__ import annotations

import re

from .ats_score import _has, extract_requirements

WEIGHTS = {"skills": 0.45, "job_title": 0.20, "experience": 0.15, "education": 0.08, "domain": 0.08, "certifications": 0.04}

_TITLE_STOP = {"senior", "sr", "junior", "jr", "lead", "principal", "staff", "associate", "ii", "iii", "i", "iv", "of",
               "and", "the", "a", "an", "for", "in", "with", "to", "immediate", "joiner", "remote", "hybrid", "urgent",
               "hiring", "opening", "role", "position", "level", "mid"}
# words that mean the same thing in a job title
_TITLE_SYN = {"developer": "engineer", "dev": "engineer", "programmer": "engineer", "swe": "engineer", "sde": "engineer",
              "ai/ml": "ml", "aiml": "ml", "machine": "ml", "learning": "ml", "artificial": "ai", "intelligence": "ai",
              "genai": "generative", "llm": "generative", "backend": "back-end", "frontend": "front-end",
              "fullstack": "full-stack", "full": "full-stack", "stack": "full-stack", "scientist": "scientist"}

DOMAINS = {
    "ml_ai": r"machine learning|\bml\b|\bai\b|artificial intelligence|deep learning|\bnlp\b|computer vision|\bllms?\b|generative|gen ?ai|data scien",
    "backend": r"back-?end|\bapis?\b|microservices|fastapi|django|flask|spring|node\.?js|server[- ]side",
    "fullstack": r"full[- ]?stack|\bmern\b|react.{0,40}node|frontend and backend",
    "data": r"data engineer|\betl\b|data pipeline|spark|airflow|warehouse|\bsql\b|analytics",
    "frontend": r"front-?end|\breact\b|angular|vue|\bui\b|css",
    "devops": r"devops|\bsre\b|kubernetes|terraform|ci/cd|infrastructure",
    "mobile": r"android|\bios\b|react native|flutter|kotlin|swift",
    "qa": r"\bqa\b|test automation|selenium|testing|sdet",
}
_DEGREE_LEVELS = [
    ("phd", r"\bph\.?d\b|doctorate"),
    ("master", r"\bmaster'?s?\b|\bm\.?tech\b|\bm\.?e\b|\bm\.?s\b|\bmca\b|\bmba\b|post ?graduate|\bpg\b(?!:)"),
    ("bachelor", r"\bbachelor'?s?\b|\bb\.?tech\b|\bb\.?e\b|\bb\.?sc\b|\bbca\b|\bb\.?s\b|graduate|\bug\b|engineering degree|degree"),
]
_CERT = re.compile(r"(?i)\b(certifi(?:ed|cation)s?|certificate)\b[^.\n]{0,60}")


def _norm_title_words(t: str) -> set[str]:
    words = re.findall(r"[a-z][a-z/+-]*", (t or "").lower())
    out = set()
    for w in words:
        if w in _TITLE_STOP or len(w) < 2:
            continue
        out.add(_TITLE_SYN.get(w, w))
    return out


def _years_range(jd_text: str, profile_title: str = "") -> tuple[float | None, float | None]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|to|–)\s*(\d+(?:\.\d+)?)\s*(?:\+\s*)?(?:years?|yrs)", jd_text or "", re.I)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs)", jd_text or "", re.I)
    return (float(m.group(1)), None) if m else (None, None)


def _resume_years(r, profile: dict | None) -> float:
    if profile and profile.get("total_experience_years") is not None:
        try:
            return float(profile["total_experience_years"])
        except (TypeError, ValueError):
            pass
    return 0.0


def _level(text: str) -> int:
    for i, (_, rx) in enumerate(_DEGREE_LEVELS):
        if re.search(rx, text or "", re.I):
            return len(_DEGREE_LEVELS) - i          # phd 3, master 2, bachelor 1
    return 0


def bimetric(r, jd_text: str, title: str = "", skills: list[str] | None = None, profile: dict | None = None) -> dict:
    jd = jd_text or ""
    cats: dict[str, dict] = {}

    # ---- skills: experience-backed counts 1.0, skills-list-only 0.8, absent 0
    reqs = extract_requirements(jd, skills)
    work = "\n".join([e.title + " " + " ".join(e.bullets) for e in r.experience] +
                     [p.name + " " + p.description for p in r.projects] + [r.summary or ""])
    listed = "\n".join(g.name + " " + " ".join(g.items) for g in r.skills) + "\n" + (r.headline or "")
    if reqs:
        pts, in_work, list_only, missing = 0.0, [], [], []
        for k in reqs:
            if _has(k, work):
                pts += 1.0; in_work.append(k)
            elif _has(k, listed):
                pts += 0.8; list_only.append(k)
            else:
                missing.append(k)
        cats["skills"] = {"score": round(100 * pts / len(reqs)), "in_experience": in_work, "skills_list_only": list_only,
                          "missing": missing, "required": reqs}

    # ---- job title
    jd_title = title or ""
    if not jd_title:
        m = re.search(r"(?im)^\s*role\s*:?\s*\n?\s*([^\n,]{3,60})", jd)
        jd_title = m.group(1) if m else ""
    want = _norm_title_words(jd_title)
    if want:
        best, best_t = 0.0, ""
        for t in [r.headline or ""] + [e.title for e in r.experience]:
            have = _norm_title_words(t)
            ov = len(want & have) / len(want) if want else 0
            if ov > best:
                best, best_t = ov, t
        cats["job_title"] = {"score": round(100 * best), "job_title": jd_title.strip(), "closest": best_t,
                             "missing_words": sorted(want - _norm_title_words(best_t))}

    # ---- experience years
    lo, hi = _years_range(jd)
    yrs = _resume_years(r, profile)
    if lo is not None:
        if yrs >= lo and (hi is None or yrs <= hi + 1):
            s = 100
        elif yrs < lo:
            s = max(0, round(100 - 35 * (lo - yrs)))         # each missing year costs 35
        else:
            s = 85                                         # over-qualified: small penalty
        cats["experience"] = {"score": s, "required": f"{lo:g}" + (f"-{hi:g}" if hi is not None else "+") + " years",
                              "resume_years": yrs}

    # ---- education
    edu_text = " ".join(f"{e.degree} {e.institution} {e.details}" for e in r.education)
    jd_edu = re.search(r"(?is)(education|qualification)s?\s*:?(.{0,300})", jd)
    need = _level(jd_edu.group(2)) if jd_edu else _level(" ".join(re.findall(r"(?i)[^.\n]*(?:degree|b\.?tech|bachelor|master|graduate)[^.\n]*", jd)))
    if need:
        have = _level(edu_text)
        if re.search(r"(?i)any graduate|any (ug|degree)|(^|\n)\s*ug\s*:", jd):
            need = 1                        # Naukri "UG: ... PG: ...": either degree is accepted
        cats["education"] = {"score": 100 if have >= need else (60 if have else 0),
                             "required_level": ["", "bachelor", "master", "phd"][need],
                             "resume_level": ["none", "bachelor", "master", "phd"][have]}

    # ---- domain / role family
    # a role family counts when the title names it or the JD shows it at least twice (one stray "UI" or "testing"
    # made a Triosoft ML job look like front-end and QA, 21 Oct 2026)
    jd_dom = {d for d, rx in DOMAINS.items()
              if re.search(rx, jd_title, re.I) or len({m.group(0).lower() for m in re.finditer(rx, jd[:4000], re.I)}) >= 2}
    if jd_dom:
        res_all = (r.headline or "") + " " + (r.summary or "") + " " + work + " " + listed
        hit = {d for d in jd_dom if re.search(DOMAINS[d], res_all, re.I)}
        cats["domain"] = {"score": round(100 * len(hit) / len(jd_dom)), "job": sorted(jd_dom), "missing": sorted(jd_dom - hit)}

    # ---- certifications (only when the JD names one)
    certs = [m.group(0).strip() for m in _CERT.finditer(jd)]
    if certs:
        have_c = " ".join(getattr(r, "certifications", []) or [])
        cats["certifications"] = {"score": 100 if have_c.strip() else 0, "job_mentions": certs[:3]}

    wsum = sum(WEIGHTS[c] for c in cats)
    total = round(sum(WEIGHTS[c] * cats[c]["score"] for c in cats) / wsum) if wsum else 0
    # what tailoring can still raise: skills and the title. Years, degree and certificates are facts it cannot change.
    fixable = {"skills": cats.get("skills", {}).get("missing", []) + cats.get("skills", {}).get("skills_list_only", []),
               "job_title": cats.get("job_title", {}).get("missing_words", [])}
    return {"score": total, "categories": cats, "fixable": fixable, "weights": {c: WEIGHTS[c] for c in cats}}


def summary(res: dict) -> str:
    """'78 (skills 72, job_title 100, experience 65, education 100, domain 100)'"""
    return f"{res['score']} (" + ", ".join(f"{c} {v['score']}" for c, v in res["categories"].items()) + ")"
