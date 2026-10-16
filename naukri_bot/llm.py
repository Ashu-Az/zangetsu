"""Claude-backed answering of recruiter questions and resume tailoring.

Both calls use structured outputs (`client.beta.messages.parse` with a Pydantic
`output_format`) so the bot never has to parse free text, and server-side refusal
fallbacks so a safety decline on the primary model is retried automatically.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

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

Never claim an action has already been done when it has not: do not say a form was filled, a test taken, an email sent or a document uploaded. If a question asks whether something was done, answer with what will happen ("I will fill it today") or set skip.

How to think about every question (do this before answering)
1. Work out what the recruiter is really asking and why: a filter (experience, location, notice, CTC, skill), a willingness check (relocate, travel, shifts, WFO, contract), a yes/no declaration, or a free-text screen. Read the options: they tell you the format and what counts as a "good" answer.
2. Ashu urgently needs a job (his last day is 20 Oct 2026). The goal of every answer is to keep him in the shortlist. So:
   - Willingness, comfort, availability and "are you OK with" questions: answer Yes, unless the CONFIRMED FACTS list it as a No.
   - "Do you have experience with X?": Yes when X is in his skills or resume, or is a natural part of work he has done (prompt engineering, LLM APIs, REST APIs, OOP, data preprocessing, model evaluation, Git, Linux basics, agile teamwork, debugging). For years, use the closest related skill's years.
   - Answer No only when the true answer is No (passport, night shifts, bonds, walk-ins outside Delhi NCR, a technology he has never used).
   - Pick the most favourable option that is still true. Never pick a lower bucket than the facts allow.
3. Then answer in exactly the format asked: a number, an exact option label, Yes/No, or 1-3 short sentences.

Hard rules
- Truth only. Every fact must come from the profile or the resume. If neither supports an answer, set skip=true rather than guess.
- Numbers: notice period, CTC (in lakhs per annum, e.g. "12" or "12 LPA"), years of experience and similar come straight from the profile. If the question gives options (buckets), pick the bucket that contains the profile value.
- "Do you have experience in X?" style questions: say Yes only if X (or an obvious synonym) is in skills_primary, skills_secondary, or the resume.
- Open questions ("why do you want this role", "describe your experience with X"): write 1-3 plain sentences in the first person. Specific, calm, no hype. Mention one concrete thing from the resume that relates to the job.
- Style: plain conversational Indian-English, no bullet points, no markdown, no exclamation marks, no "I am excited", no "As an AI", no "Dear Hiring Manager". Do not restate the question. Short questions get short answers; a numeric question gets just the number.
- For option questions, put the chosen option's exact label text in answer_text and its index in selected_option_indices. For multi-select, choose every option that is true.
- If the questionnaire rejected a previous answer (shown as REJECTED_PREVIOUS), the format was wrong: give the value in the simplest possible form (digits only, or an exact option label).

{facts}

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
        # Groq (OpenAI-compatible chat API) when GROQ_API_KEY is set and no Anthropic API key is (Ashu, 12 Oct 2026)
        self.groq_key = os.getenv("GROQ_API_KEY", "").strip() if not os.getenv("ANTHROPIC_API_KEY", "").strip() else ""
        self.client = None if self.groq_key else anthropic.Anthropic()
        profile_yaml = yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)
        # the candidate's confirmed facts live in profile/confirmed_facts.md (git-ignored: personal data stays local)
        facts_path = Path(__file__).resolve().parent.parent / "profile" / "confirmed_facts.md"
        facts = facts_path.read_text(encoding="utf-8") if facts_path.exists() else ""
        self._answer_system = ANSWER_SYSTEM.format(profile=profile_yaml, resume=resume_md, facts=facts)
        if self.groq_key:   # Groq free tier: 8k tokens/minute, so send the fact sheet + resume, not the full profile YAML
            self._answer_system = ANSWER_SYSTEM.format(profile="(the CONFIRMED FACTS above are the profile)", resume=resume_md, facts=facts)
        self._resume_system = RESUME_SYSTEM.format(profile=profile_yaml, resume=resume_md)

    # ---- core call ----
    def _parse_groq(self, system: str, user: str, output_format, max_tokens: int):
        """One JSON-mode chat call to Groq, validated against the same Pydantic schema."""
        schema = json.dumps(output_format.model_json_schema())
        body = {
            "model": os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
            "temperature": 0.2,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system + "\n\nReply with ONE JSON object matching this JSON Schema, nothing else:\n" + schema},
                {"role": "user", "content": user},
            ],
        }
        body["max_completion_tokens"] = min(max_tokens, 2000)   # the cap counts against the per-minute token budget
        last = None
        for attempt in range(6):
            req = urllib.request.Request("https://api.groq.com/openai/v1/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {self.groq_key}", "Content-Type": "application/json",
                                                  "User-Agent": "zangetsu-jobbot/1.0"})   # Cloudflare 1010 blocks urllib's default agent
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    text = json.loads(r.read())["choices"][0]["message"]["content"]
                return output_format.model_validate_json(text)
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read()[:300]!r}"
                if e.code == 429:                     # rate limited: wait as long as Groq asks, then retry
                    m = re.search(r"try again in ([\d.]+)(ms|s)", last)
                    wait = (float(m.group(1)) / (1000 if m.group(2) == "ms" else 1)) if m else 10 * (attempt + 1)
                    time.sleep(min(wait + 1, 60)); continue
                if e.code >= 500:
                    time.sleep(3); continue
                break
            except (ValueError, KeyError) as e:       # malformed / schema-invalid JSON: ask again
                last = str(e)[:300]
        raise RuntimeError(f"Groq call failed: {last}")

    def _parse(self, system: str, user: str, output_format, effort: str, max_tokens: int):
        if self.groq_key:
            return self._parse_groq(system, user, output_format, max_tokens)
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
        parts.append("REMEMBER: first work out what this question is really asking, then answer truthfully from the confirmed "
                     "facts in the way most likely to get Ashu shortlisted (Yes for willingness and skills he really has; "
                     "No only when the true answer is No).")
        parts.append(f"QUESTION TYPE: {kind}")
        parts.append(f"QUESTION: {question}")
        if options:
            parts.append("OPTIONS:\n" + "\n".join(f"[{i}] {o}" for i, o in enumerate(options)))
        user = "\n\n".join(parts)
        ans = self._parse(self._answer_system, user, QuestionAnswer, effort="medium", max_tokens=8000)   # thinking counts toward max_tokens
        # plain keyboard characters only: non-breaking hyphens, em/en dashes and curly quotes read as machine-written
        for a, b in (("\u2011", "-"), ("\u2010", "-"), ("\u2013", "-"), ("\u2014", ", "), ("\u2019", "'"), ("\u2018", "'"),
                     ("\u201c", '"'), ("\u201d", '"'), ("\u202f", " "), ("\u00a0", " ")):
            ans.answer_text = ans.answer_text.replace(a, b)
        return ans

    def tailor_resume(self, job: JobPosting) -> TailoredResume:
        user = "JOB POSTING:\n" + job.to_prompt_text() + "\n\nProduce the tailored resume now."
        return self._parse(self._resume_system, user, TailoredResume, effort="high", max_tokens=16000)
