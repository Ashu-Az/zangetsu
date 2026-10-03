"""Claude-backed answering of recruiter questions and resume tailoring.

Both calls use structured outputs (`client.beta.messages.parse` with a Pydantic
`output_format`) so the bot never has to parse free text, and server-side refusal
fallbacks so a safety decline on the primary model is retried automatically.
"""
from __future__ import annotations

import logging
import yaml
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from .config import Settings
from .models import JobPosting

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


# ---------- output schemas ----------
class QuestionAnswer(BaseModel):
    answer_text: str = Field(description="The answer exactly as it should be typed. Empty when skip is true.")
    selected_option_indices: list[int] = Field(default_factory=list, description="0-based indices of chosen options for radio/checkbox/dropdown questions. One index for single-choice.")
    reusable: bool = Field(description="True if the same answer would be correct for any employer (notice period, CTC, skills…). False for job-specific prose.")
    skip: bool = Field(description="True only if the profile has no truthful basis for an answer.")
    confidence: float = Field(ge=0, le=1)


class SkillGroup(BaseModel):
    name: str
    items: list[str]


class ExperienceItem(BaseModel):
    title: str
    company: str
    location: str = ""
    dates: str
    bullets: list[str]


class ProjectItem(BaseModel):
    name: str
    description: str
    link: str = ""


class EducationItem(BaseModel):
    degree: str
    institution: str
    dates: str = ""
    details: str = ""


class TailoredResume(BaseModel):
    name: str
    headline: str = Field(description="One line, e.g. 'Data Scientist · NLP & LLM systems', aligned to the job title.")
    contact_line: str = Field(description="Location · email · phone · links, separated by ' · '")
    summary: str = Field(description="2-4 sentences, first person implied, mirrors the job's vocabulary where truthful.")
    skills: list[SkillGroup]
    experience: list[ExperienceItem]
    projects: list[ProjectItem]
    education: list[EducationItem]
    certifications: list[str] = Field(default_factory=list)
    keywords_used: list[str] = Field(description="JD keywords that were truthfully woven into the resume.")
    fit_score: int = Field(ge=0, le=100, description="Honest estimate of how well the candidate matches the job.")
    fit_notes: str = Field(description="One or two sentences: biggest gaps and strongest matches.")


# ---------- prompts ----------
ANSWER_SYSTEM = """You are filling in job-application questionnaires on Naukri.com on behalf of the candidate described below. You answer exactly as the candidate would, so that a recruiter reading the answer cannot tell it was not typed by a person.

Hard rules
- Truth only. Every fact must come from the profile or the resume. If neither supports an answer, set skip=true rather than guess.
- Numbers: notice period, CTC (in lakhs per annum, e.g. "12" or "12 LPA"), years of experience and similar come straight from the profile. If the question gives options (buckets), pick the bucket that contains the profile value.
- "Do you have experience in X?" style questions: say Yes only if X (or an obvious synonym) is in skills_primary, skills_secondary, or the resume.
- Open questions ("why do you want this role", "describe your experience with X"): write 1-3 plain sentences in the first person. Specific, calm, no hype. Mention one concrete thing from the resume that relates to the job.
- Style: plain conversational Indian-English, no bullet points, no markdown, no exclamation marks, no "I am excited", no "As an AI", no "Dear Hiring Manager". Do not restate the question. Short questions get short answers; a numeric question gets just the number.
- For option questions, put the chosen option's exact label text in answer_text and its index in selected_option_indices. For multi-select, choose every option that is true.
- If the questionnaire rejected a previous answer (shown as REJECTED_PREVIOUS), the format was wrong: give the value in the simplest possible form (digits only, or an exact option label).

Candidate profile (YAML):
{profile}

Candidate master resume (Markdown):
{resume}
"""

RESUME_SYSTEM = """You tailor a candidate's master resume to one specific job posting. Output a complete resume as structured data.

Rules
- Never invent employers, titles, dates, degrees, numbers or tools. Everything must be traceable to the master resume or profile. You may rephrase, reorder, merge, drop, and emphasise.
- Mirror the job description's vocabulary where it is truthful (e.g. if the JD says "GenAI" and the resume says "LLM applications", say "GenAI / LLM applications").
- Put the most relevant skills first and the most relevant bullets first in each role. Keep metrics. Cut bullets irrelevant to this job if the resume is long; aim for what fits one to two A4 pages.
- Summary: 2-4 sentences positioned for this exact job title and company; no clichés ("results-driven", "passionate"), no first-person pronoun.
- Keep contact details exactly as given.
- fit_score is an honest assessment; do not inflate it.

Candidate profile (YAML):
{profile}

Master resume (Markdown):
{resume}
"""


class LLM:
    def __init__(self, settings: Settings, profile: dict, resume_md: str):
        self.settings = settings
        self.client = anthropic.Anthropic()
        profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
        self._answer_system = ANSWER_SYSTEM.format(profile=profile_yaml, resume=resume_md)
        self._resume_system = RESUME_SYSTEM.format(profile=profile_yaml, resume=resume_md)

    # ---- core call ----
    def _parse(self, system: str, user: str, output_format, effort: str, max_tokens: int):
        kwargs = dict(
            model=self.settings.llm_model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_format=output_format,
            output_config={"effort": effort},
        )
        if self.settings.llm_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            resp = self.client.beta.messages.parse(**kwargs)
        except anthropic.BadRequestError as e:
            if self.settings.llm_fallbacks and "fallback" in str(e).lower():
                log.warning("Fallbacks not accepted by this endpoint; retrying without. (%s)", e.message)
                self.settings.llm_fallbacks = False
                return self._parse(system, user, output_format, effort, max_tokens)
            raise
        if resp.stop_reason == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            raise RuntimeError(f"Model refused the request (category={cat}).")
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("Model output was truncated (max_tokens).")
        return resp.parsed_output

    # ---- public API ----
    def answer_question(
        self,
        question: str,
        options: list[str],
        kind: str,
        job: Optional[JobPosting],
        transcript: list[str],
        rejected_previous: str | None = None,
    ) -> QuestionAnswer:
        parts = []
        if job:
            parts.append("JOB BEING APPLIED TO:\n" + job.to_prompt_text(max_desc_chars=2500))
        if transcript:
            parts.append("EARLIER QUESTIONS AND YOUR ANSWERS IN THIS APPLICATION:\n" + "\n".join(transcript[-12:]))
        if rejected_previous:
            parts.append(f"REJECTED_PREVIOUS: {rejected_previous}")
        parts.append(f"QUESTION TYPE: {kind}")
        parts.append(f"QUESTION: {question}")
        if options:
            parts.append("OPTIONS:\n" + "\n".join(f"[{i}] {o}" for i, o in enumerate(options)))
        user = "\n\n".join(parts)
        return self._parse(self._answer_system, user, QuestionAnswer, effort="medium", max_tokens=2000)

    def tailor_resume(self, job: JobPosting) -> TailoredResume:
        user = "JOB POSTING:\n" + job.to_prompt_text() + "\n\nProduce the tailored resume now."
        return self._parse(self._resume_system, user, TailoredResume, effort="high", max_tokens=16000)
