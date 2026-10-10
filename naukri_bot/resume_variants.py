"""Role-focused resume variants: build the PDFs, and pick the right one for a job.

profile/resume_variants.yaml holds one shared core (experience, education) plus a summary, skill order and
project selection per variant. Every variant makes the same claims; only the emphasis changes, so no
model is needed and nothing new is asserted.

Build:  python -m naukri_bot.resume_variants
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .config import OUTPUT_DIR, PROFILE_DIR
from .llm import TailoredResume

VARIANTS_FILE = PROFILE_DIR / "resume_variants.yaml"

# job title words -> variant; first match wins, so the most specific families come first.
# Full-stack and generic software roles get the balanced full-stack + AI resume (10 Oct 2026); pure AI/ML/data
# roles get ai_ml (which also lists full-stack skills); explicitly backend roles get backend.
_RULES = [
    ("ai_ml", r"\bai\b|a\.i\.|artificial intelligence|machine learning|\bml\b|gen\s?ai|generative|llm|nlp|"
              r"data scien|deep learning|computer vision|prompt|agentic|mlops|ai/ml|ai-ml"),
    ("full_stack_ai", r"full[\s-]?stack|mern|mean|front[\s-]?end|react|next\.?js|web developer|ui developer|"
                      r"software (engineer|developer)|\bsde\b|\bswe\b|product engineer|application developer"),
    ("backend", r"backend|back-end|python|django|fastapi|flask|node|api"),
]
DEFAULT_VARIANT = "full_stack_ai"


def load_variants(path: Path = VARIANTS_FILE) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def variant_for(title: str, skills: list[str] | None = None) -> str:
    """Variant key for a job: by title first, then by the job's key skills, default the balanced full_stack_ai."""
    t = (title or "").lower()
    rules = dict(_RULES)
    if re.search(rules["ai_ml"], t) and re.search(r"full[\s-]?stack|mern|mean|front[\s-]?end|react|next\.?js", t):
        return "full_stack_ai"                     # "Gen AI Full Stack Developer": exactly the balanced profile
    for key, pat in _RULES:
        if re.search(pat, t):
            return key
    s = " ".join(skills or []).lower()
    for key, pat in _RULES:
        if re.search(pat, s):
            return key
    return DEFAULT_VARIANT


def build_resume(data: dict, key: str) -> TailoredResume:
    core, v = data["core"], data["variants"][key]
    return TailoredResume(
        name=core["name"], headline=v["headline"], contact_line=core["contact_line"], summary=v["summary"],
        skills=v["skills"], experience=core["experience"], education=core["education"],
        projects=[data["projects"][p] for p in v["projects"]], certifications=core.get("certifications", []),
        keywords_used=[], fit_score=0, fit_notes="",
    )


def variant_file(key: str, data: dict | None = None) -> str:
    return (data or load_variants())["variants"][key]["file"]


def build_all(out_dir: Path | None = None) -> list[Path]:
    from .resume_builder import html_to_pdf, render_html
    data = load_variants()
    out_dir = out_dir or OUTPUT_DIR / "resumes"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for key, v in data["variants"].items():
        pdf = out_dir / v["file"]
        html = render_html(build_resume(data, key))
        pdf.with_suffix(".html").write_text(html, encoding="utf-8")
        html_to_pdf(html, pdf)
        paths.append(pdf)
    return paths


if __name__ == "__main__":
    for p in build_all():
        print("written", p)
