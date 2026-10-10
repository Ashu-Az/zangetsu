"""Grounding check (no invented skills on a tailored resume) and the honest keyword-coverage heuristic."""
import json
from pathlib import Path

import pytest
import yaml

from naukri_bot.ats_score import extract_requirements, report, score
from naukri_bot.grounding import build_fact_corpus, check_resume, check_skill_item, check_text, enforce
from naukri_bot.llm import SkillGroup, TailoredResume

ROOT = Path(__file__).resolve().parent.parent

# a fictional candidate
VARIANTS = {
    "core": {
        "name": "Test Person",
        "contact_line": "Somewhere",
        "experience": [{"title": "Backend Engineer", "company": "Example Ltd", "location": "Pune", "dates": "2023 – Present",
                        "bullets": ["Built FastAPI services on MongoDB and PostgreSQL for 5,000+ users.",
                                    "Built a semantic search over FAISS embeddings; shipped with Docker and GitHub Actions."]}],
        "education": [{"degree": "B.E., Computer Engineering", "institution": "Example Institute", "dates": "2019 – 2023"}],
        "certifications": ["300+ LeetCode problems"],
    },
    "projects": {"bot": {"name": "Chatbot", "link": "", "description": "RAG assistant with LangChain and OpenAI API."}},
    "variants": {"backend": {"file": "x.pdf", "headline": "Backend Engineer · Python, FastAPI",
                             "summary": "Backend engineer building Python services.", "projects": ["bot"],
                             "skills": [{"name": "Backend", "items": ["Python", "FastAPI", "MongoDB", "PostgreSQL", "Docker"]}]}},
}
PROFILE = {"skills_primary": ["Python", "FastAPI"], "skills_secondary": ["React"], "skill_years": {"python": 3}}


@pytest.fixture
def corpus():
    return build_fact_corpus(VARIANTS, PROFILE)


def test_invented_practices_are_dropped(corpus):
    assert check_skill_item("Agile/Scrum collaboration", corpus)
    assert check_skill_item("Distributed systems", corpus)
    assert check_skill_item("Kubernetes", corpus)
    assert check_skill_item("Code reviews", corpus)
    assert check_skill_item("AWS (Docker)", corpus)              # a real parenthetical does not excuse a fake tool


def test_honest_relabels_pass(corpus):
    for item in ["NoSQL (MongoDB)", "Vector search (FAISS)", "Vector search (FAISS embedding search)", "NoSQL",
                 "CI/CD (GitHub Actions)", "Containerization", "REST APIs", "SQL", "LLMs", "Prompt engineering",
                 "Data structures & algorithms (LeetCode 300+)", "OOP (Python)", "Python", "Scalability (5,000+ users)"]:
        assert check_skill_item(item, corpus) is None, item


def test_headline_and_summary_terms(corpus):
    bad = check_text("Backend Engineer · Kubernetes expert · Python", "headline", corpus)
    assert [u.item for u in bad] == ["Kubernetes"]
    assert check_text("Backend Engineer · Python, FastAPI, NoSQL", "headline", corpus) == []
    # an invented metric is flagged, a real one is not
    assert [u.item for u in check_text("Served 5,000+ users and cut latency 90%.", "summary", corpus)] == ["90"]
    assert check_text("Worked closely with the rest of the team.", "summary", corpus) == []


def test_enforce_drops_items_and_falls_back(corpus):
    r = TailoredResume(name="T", headline="Backend Engineer · Kubernetes, Spark", contact_line="", summary="Agile engineer.",
                       skills=[SkillGroup(name="Core", items=["Python", "Distributed systems"]),
                               SkillGroup(name="Process", items=["Agile/Scrum collaboration"])],
                       experience=[], projects=[], education=[], keywords_used=[], fit_score=0, fit_notes="")
    dropped = enforce(r, corpus, "Backend Engineer · Python", "Backend engineer building Python services.")
    assert {u.item for u in dropped} >= {"Distributed systems", "Agile/Scrum collaboration", "Kubernetes", "Spark", "Agile"}
    assert [(g.name, g.items) for g in r.skills] == [("Core", ["Python"])]       # empty group removed
    assert r.headline == "Backend Engineer · Python" and r.summary.startswith("Backend engineer")


def test_extra_skills_confirmed_grounds_a_practice():
    c = build_fact_corpus(VARIANTS, dict(PROFILE, extra_skills_confirmed=["Agile/Scrum", "Distributed systems"]))
    assert check_skill_item("Agile/Scrum", c) is None and check_skill_item("Agile (Scrum)", c) is None
    assert check_skill_item("Distributed systems", c) is None


# ---- keyword-coverage heuristic -------------------------------------------------------------------------------

def test_short_aliases_do_not_over_match():
    assert score("Built services in Node.js and TypeScript", ["JavaScript"])[0] == 0      # no 'js' alias
    assert score("Handed it over to the rest of the team", ["REST"])[0] == 0
    assert score("Handed it over to the rest of the team", ["REST APIs"])[0] == 0
    assert score("Built REST APIs", ["REST"])[0] == 100
    assert score("CI/CD with GitHub Actions", ["Git"])[0] == 0
    assert score("Java and Python", ["JavaScript"])[0] == 0 and score("JavaScript", ["Java"])[0] == 0
    assert score("Machine learning in production", ["ML"])[0] == 100
    assert score("ML models", ["machine learning"])[0] == 100
    assert score("HTML5 pages", ["ML"])[0] == 0
    assert score("React.js, Postgres", ["React", "PostgreSQL"])[0] == 100


SAMPLE_JD = """About the role
You will react quickly to customer needs alongside the rest of the team.

Requirements:
- 2+ years with Python, Django and REST APIs
- Experience with PostgreSQL and Docker
- Strong JavaScript

Nice to have:
- Kubernetes, AWS
- Familiarity with Kafka
"""


def test_extract_requirements_from_jd():
    reqs = extract_requirements(SAMPLE_JD)
    assert {"Python", "Django", "REST APIs", "PostgreSQL", "Docker", "JavaScript", "Kubernetes", "AWS", "Kafka"} <= set(reqs)
    assert "React" not in reqs                    # "react quickly" is English, not the library
    assert reqs.index("Python") < reqs.index("Kafka")                         # must-haves come first


def test_report_breaks_down_must_and_nice():
    rep = report("Python, Django, REST APIs, PostgreSQL, Docker, Kubernetes", SAMPLE_JD)
    assert "heuristic" in rep["kind"]
    assert set(rep["missing"]) == {"JavaScript", "AWS", "Kafka"}
    assert rep["must_have"]["missing"] == ["JavaScript"]
    assert set(rep["nice_to_have"]["missing"]) == {"AWS", "Kafka"}
    assert 0 < rep["score"] < 100


# ---- the real plans against the real facts ----------------------------------------------------------------------

PLANS = sorted((ROOT / "output" / "resumes" / "custom").glob("*.json"))


@pytest.mark.skipif(not PLANS or not (ROOT / "profile" / "profile.yaml").exists(), reason="no real plans/profile here")
def test_every_real_plan_runs_through_the_check():
    from naukri_bot.resume_variants import build_resume, load_variants
    from naukri_bot.tailor_rules import apply_plan
    data = load_variants()
    profile = yaml.safe_load((ROOT / "profile" / "profile.yaml").read_text(encoding="utf-8"))
    corpus = build_fact_corpus(data, profile)
    for key in data["variants"]:                                   # the variants themselves are all grounded
        assert check_resume(build_resume(data, key), corpus) == [], key
    for path in PLANS:
        plan = json.loads(path.read_text())
        r = build_resume(data, "ai_ml")
        dropped = apply_plan(r, plan, data, corpus)
        assert check_resume(r, corpus) == [], path.name             # what is left is grounded
        for u in dropped:
            print(f"{path.name}: {u}")


def test_company_names_are_not_skills():
    from naukri_bot.ats_score import extract_requirements
    reqs = [r.lower() for r in extract_requirements("Our clients include American Express and Walmart. You know Python and LangChain.")]
    assert "express" not in reqs and "python" in reqs
