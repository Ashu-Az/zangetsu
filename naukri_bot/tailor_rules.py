"""Tailor the resume to one job description without a model and without adding anything.

Takes the role-focused variant (AI/ML, Backend, Full-stack) and reorders what is already true so the parts the
job description asks for come first: skills inside each group, skill groups, bullets inside each job, and the
three projects with the most overlap. A hand-written plan (output/resumes/custom/*.json) may re-word the
headline, summary and skills, but every result goes through the grounding check (naukri_bot/grounding.py):
skill items not backed by the candidate's facts are dropped, and an ungrounded headline/summary falls back to the
variant's own wording. What was dropped is logged and returned in report["dropped"].
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from .config import OUTPUT_DIR, load_profile
from .grounding import FactCorpus, Ungrounded, build_fact_corpus, enforce
from .resume_variants import build_resume, load_variants, variant_for

log = logging.getLogger(__name__)

PLAN_DIR = OUTPUT_DIR / "resumes" / "custom"


def _terms(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9+#./-]{1,30}", text or "")}


GENERIC = {"learning", "data", "engineering", "environments", "environment", "systems", "system", "services", "api",
           "apis", "and", "the", "with", "for", "in", "of", "to", "on", "model", "models", "development", "design",
           "production", "platform", "tools", "experience", "team", "using", "based", "end", "work"}


def _hits(item: str, jd: str) -> int:
    """How strongly the JD asks for this item: the whole name scores 3; otherwise each distinctive word scores 1
    (generic words like "learning" or "data" never count on their own)."""
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(item)}(?![A-Za-z0-9])", jd, re.I):
        return 3
    words = [w for w in re.split(r"[\s,/()\-]+", item) if len(w) > 1 and w.lower() not in GENERIC]
    return sum(1 for w in words if re.search(rf"(?<![A-Za-z0-9]){re.escape(w)}(?![A-Za-z0-9])", jd, re.I))


def _corpus(data: dict, profile: dict | None = None) -> FactCorpus:
    return build_fact_corpus(data, load_profile() if profile is None else profile)


def _tailor(data: dict, jd_text: str, title: str, skills: list[str] | None, corpus: FactCorpus) -> tuple[object, dict]:
    if skills is None and jd_text:
        from .ats_score import extract_requirements
        skills = extract_requirements(jd_text)
    key = variant_for(title, skills)
    variant = data["variants"][key]
    # projects: the 3 with the most JD overlap from ALL projects (still only real projects)
    scored = sorted(data["projects"], key=lambda k: -_hits(data["projects"][k]["description"] + " " + data["projects"][k]["name"], jd_text))
    data = dict(data, variants=dict(data["variants"], **{key: dict(variant, projects=scored[:3])}))
    r = build_resume(data, key)
    # skills: JD-mentioned items first inside each group, then groups with most hits first
    for g in r.skills:
        g.items = sorted(g.items, key=lambda it: -_hits(it, jd_text))
    r.skills = sorted(r.skills, key=lambda g: -sum(_hits(i, jd_text) for i in g.items))
    # bullets: most relevant first within each job (order of jobs stays chronological)
    for e in r.experience:
        e.bullets = sorted(e.bullets, key=lambda b: -_hits(b, jd_text))
    dropped = enforce(r, corpus, variant["headline"], variant["summary"])
    mine = {i.lower() for g in r.skills for i in g.items}
    matched = sorted(i for i in mine if _hits(i, jd_text) >= 3)
    return r, {"variant": key, "projects": scored[:3], "matched_skills": matched, "dropped": dropped}


def tailor(jd_text: str, title: str = "", skills: list[str] | None = None) -> tuple[object, dict]:
    """(TailoredResume, report) for this job. report lists the matched JD terms, what moved, and in "dropped" any
    ungrounded content removed. `skills` are the job's key skills if known (else extracted from the JD); they pick
    the variant when the title alone does not."""
    data = load_variants()
    return _tailor(data, jd_text, title, skills, _corpus(data))


def apply_plan(r, plan: dict, data: dict, corpus: FactCorpus) -> list[Ungrounded]:
    """Apply a hand-written plan (headline, summary, skills, projects, keywords) to `r` in place, then enforce
    grounding: ungrounded skill items are dropped and an ungrounded headline/summary reverts to the wording `r`
    had before the plan (the variant's own). Returns the dropped items."""
    from .llm import ProjectItem, SkillGroup
    base_headline, base_summary = r.headline, r.summary
    r.headline = plan.get("headline") or r.headline
    r.summary = plan.get("summary") or r.summary
    if plan.get("skills"):
        r.skills = [SkillGroup(name=s["name"], items=list(s["items"])) for s in plan["skills"]]
    projects = data["projects"]
    if plan.get("projects"):
        r.projects = [ProjectItem(**projects[k]) for k in plan["projects"] if k in projects]
    kws = [k.lower() for k in plan.get("keywords", [])]
    dropped: list[Ungrounded] = []
    # optional "experience": {"<company substring>": ["bullet in the JD's words", ...]} - the same real work, re-worded
    # for this job (Ashu, 13 Oct 2026). Each bullet must pass the grounding check (no new tool, practice or metric);
    # ungrounded bullets are dropped, and if none survive the role keeps its original bullets.
    from .grounding import check_text
    for key, bullets in (plan.get("experience") or {}).items():
        for e in r.experience:
            if key.lower() in e.company.lower():
                good = []
                for b in bullets:
                    bad = check_text(b, f"experience:{e.company}", corpus)
                    if bad:
                        dropped += bad
                        log.warning("re-worded bullet dropped (%s): %s", ", ".join(u.item for u in bad), b[:90])
                    else:
                        good.append(b)
                if good:
                    e.bullets = good
    for e in r.experience:                                   # most JD-relevant bullet first
        e.bullets = sorted(e.bullets, key=lambda b: -sum(k in b.lower() for k in kws))
    return dropped + enforce(r, corpus, base_headline, base_summary)


def url_hash(url: str) -> str:
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:8] if url and url.strip() else ""


def _job_stem(company: str, title: str, url: str = "") -> str:
    from .resume_builder import safe_slug
    stem = f"{safe_slug(company, 20)}_{safe_slug(title, 30)}"
    return f"{stem}_{url_hash(url)}" if url_hash(url) else stem


def plan_path(company: str, title: str, url: str = "") -> Path | None:
    """The plan for this job: the URL-keyed file first, then the legacy company_title file. None if neither."""
    for p in dict.fromkeys([PLAN_DIR / f"{_job_stem(company, title, url)}.json", PLAN_DIR / f"{_job_stem(company, title)}.json"]):
        if p.exists():
            return p
    return None


def tailored_pdf(jd_text: str, title: str, company: str, playwright=None, url: str = "",
                 skills: list[str] | None = None, compact: bool = False) -> tuple[Path, dict]:
    """Tailored PDF for one job and its report. Pass the job `url` so different postings with the same company and
    title get separate plan/PDF files (without it the old company_title naming is used)."""
    from .ats_score import report as ats_report, resume_text
    from .resume_builder import html_to_pdf, render_html, safe_slug
    data = load_variants()
    corpus = _corpus(data)
    r, report = _tailor(data, jd_text, title, skills, corpus)
    # a hand-written plan for this exact job overrides the reordering; it may only re-word and re-order facts
    plan = plan_path(company, title, url)
    if plan:
        report["dropped"] = apply_plan(r, json.loads(plan.read_text()), data, corpus)
        report["plan"] = plan.name
    # the job title is the first thing an ATS matches: lead the headline with it when the resume does not say it
    clean_title = re.sub(r"\s+-\s+\d+\s*$", "", title or "").strip()      # "AI/ML Engineer - 02" -> "AI/ML Engineer"
    if clean_title and not re.search(re.escape(clean_title), r.headline or "", re.I):
        r.headline = f"{clean_title} | {r.headline}"
    from .grounding import check_text
    from .llm import SkillGroup

    def grounded(k: str) -> bool:
        # Ashu, 18 Oct 2026: only keywords that are TRUE for Ashu may be added. Positively present in the facts
        # (resume, profile skills, confirmed skills) AND not flagged: an unknown tool ("SnapLogic") passes
        # check_text but is not in the corpus, so it is never added. Keywords he does not have stay missing and
        # the score is reported as it is, never padded with false skills.
        return corpus.has_phrase(k) and not check_text(k, "skills", corpus)

    # match score (naukri_bot/ats_bimetric.py: skills, job title, experience, education, domain, certifications)
    # must reach ats_target (Ashu: 95, part of the protocol since 9 Oct 2026). Tailoring can only raise skills and
    # the title; years, degree and certificates are facts and are reported, not changed.
    from .ats_bimetric import bimetric, summary
    profile = load_profile()
    target = int(profile.get("ats_target") or 0)
    res = bimetric(r, jd_text, title, skills, profile)
    first = summary(res)
    if target and res["score"] < target:
        # clean JD phrases before they reach the resume (TCS, 26 Oct 2026: "including microservices", "rag pipelines")
        acr = {"rag": "RAG", "llm": "LLM", "llms": "LLMs", "api": "API", "apis": "APIs", "sdk": "SDK", "sdks": "SDKs",
               "aws": "AWS", "eks": "EKS", "ml": "ML", "ai": "AI", "nlp": "NLP", "sql": "SQL", "iac": "IaC", "ci/cd": "CI/CD"}
        def tidy(t: str) -> str:
            t = re.sub(r"^(including|and|with|or|such as|like|strong|good|hands-on)\s+", "", t.strip(), flags=re.I)
            words = [acr.get(w.lower(), w) for w in t.split()]
            if words and words[0] == words[0].lower():
                words[0] = words[0][:1].upper() + words[0][1:]
            return " ".join(words)
        def clean(items):
            out, seen = [], set()
            for t in items:
                c = tidy(t)
                if re.search(r"\byears?\b|\d|\.\s|\b(be|btech|b\.tech|degree|engineering)\b|^cd\b", c, re.I):
                    continue                      # a sentence fragment from the JD ("Years. be engineering"), not a skill
                if c and c.lower() not in seen:
                    seen.add(c.lower()); out.append(c)
            return out
        candidates = clean(res["categories"].get("skills", {}).get("missing", []))
        # merge note (29 Oct 2026): the other machine's branch added every missing JD skill; the grounded-only rule
        # stays - skills Ashu does not have are reported (ats_ungrounded), not written into the resume
        missing = [k for k in candidates if grounded(k)]
        list_only = clean(res["categories"].get("skills", {}).get("skills_list_only", []))
        if missing:
            r.skills = list(r.skills) + [SkillGroup(name="Also worked with", items=list(missing))]
        if missing or list_only:          # the summary counts as experience for the engines: name the toolkit there
            r.summary = (r.summary or "").rstrip() + " Toolkit: " + ", ".join(dict.fromkeys(missing + list_only)) + "."
        report["added_for_ats"] = report["ats_added"] = list(missing)
        report["ats_ungrounded"] = [k for k in candidates if k not in missing]
        res = bimetric(r, jd_text, title, skills, profile)
        log.info("  ATS match %s -> %s%s", first, summary(res), "" if res["score"] >= target else
                 f"  (below {target}: limited by facts it cannot change)")
    else:
        log.info("  ATS match %s", first)
    cov = ats_report(resume_text(r), jd_text, skills)           # plain keyword coverage, kept for reference
    # no ats_target set: the plain keyword-coverage pass (target 90) with the same grounded-only rule
    if not target and cov["score"] < 90 and cov["missing"]:
        add = [k for k in cov["missing"] if grounded(k)]
        if add:
            r.skills = list(r.skills) + [SkillGroup(name="Also used", items=add)]
            cov = ats_report(resume_text(r), jd_text, skills)
            res = bimetric(r, jd_text, title, skills, profile)
            report["ats_added"] = add
    report["ats"], report["ats_hit"], report["ats_missing"] = res["score"], cov["matched"], cov["missing"]
    report["bimetric"], report["coverage"] = res, cov
    out = OUTPUT_DIR / "resumes" / "tailored"
    out.mkdir(parents=True, exist_ok=True)
    # recruiters see the file name: keep it clean
    who = safe_slug(data["core"]["name"], 40).replace("-", "_")
    path = out / f"{who}_{_job_stem(company, title, url)}.pdf"
    if compact:                                    # email attachments: ~5 KB, built-in fonts, same content
        from .resume_builder import compact_pdf
        compact_pdf(r, path.with_name(path.stem + "_compact.pdf"))
        return path.with_name(path.stem + "_compact.pdf"), report
    html_to_pdf(render_html(r), path, playwright=playwright)
    return path, report
