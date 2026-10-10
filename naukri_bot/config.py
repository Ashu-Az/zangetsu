"""Settings loaded from .env plus the candidate profile and master resume."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
PROFILE_DIR = PROJECT_DIR / "profile"
TEMPLATE_DIR = PROJECT_DIR / "templates"
OUTPUT_DIR = PROJECT_DIR / "output"
STATE_DIR = PROJECT_DIR / "state"

load_dotenv(PROJECT_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in _env(name).split(",") if x.strip()]


@dataclass
class Settings:
    naukri_email: str = field(default_factory=lambda: _env("NAUKRI_EMAIL"))
    naukri_password: str = field(default_factory=lambda: _env("NAUKRI_PASSWORD"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-opus-5-5"))
    llm_fallbacks: bool = field(default_factory=lambda: _env("LLM_FALLBACKS", "1") not in ("0", "false", "no"))
    keywords: list[str] = field(default_factory=lambda: _env_list("KEYWORDS"))
    location: str = field(default_factory=lambda: _env("LOCATION"))
    experience_years: str = field(default_factory=lambda: _env("EXPERIENCE_YEARS"))
    max_applicants: int = field(default_factory=lambda: _env_int("MAX_APPLICANTS", 100))
    # cap for one-click Naukri jobs; 0 = no cap (they reach 100+ within hours of posting)
    max_applicants_direct: int = field(default_factory=lambda: _env_int("MAX_APPLICANTS_DIRECT", 0))
    daily_quota: int = field(default_factory=lambda: _env_int("DAILY_QUOTA", 50))
    applicant_tiers: list[int] = field(default_factory=lambda: [int(x) for x in _env_list("APPLICANT_TIERS")] or [50, 100])
    survey_cache_hours: int = field(default_factory=lambda: _env_int("SURVEY_CACHE_HOURS", 6))
    max_job_age_days: int = field(default_factory=lambda: _env_int("MAX_JOB_AGE_DAYS", 1))
    experience_slack: int = field(default_factory=lambda: _env_int("EXPERIENCE_SLACK", 1))
    min_salary_lpa: float = field(default_factory=lambda: float(_env("MIN_SALARY_LPA", "10") or 0))
    skip_undisclosed_salary: bool = field(default_factory=lambda: _env("SKIP_UNDISCLOSED_SALARY", "0") in ("1", "true", "yes"))
    pages_per_keyword: int = field(default_factory=lambda: _env_int("PAGES_PER_KEYWORD", 2))
    max_applications: int = field(default_factory=lambda: _env_int("MAX_APPLICATIONS", 25))
    max_company_site_resumes: int = field(default_factory=lambda: _env_int("MAX_COMPANY_SITE_RESUMES", 10))
    login_wait_seconds: int = field(default_factory=lambda: _env_int("LOGIN_WAIT_SECONDS", 240))
    min_delay: float = field(default_factory=lambda: float(_env("MIN_DELAY", "6")))
    max_delay: float = field(default_factory=lambda: float(_env("MAX_DELAY", "15")))
    break_min: float = field(default_factory=lambda: float(_env("BREAK_MIN", "120")))   # short break every 4-7 applies
    break_max: float = field(default_factory=lambda: float(_env("BREAK_MAX", "360")))
    browser_executable: str = field(default_factory=lambda: _env("BROWSER_EXECUTABLE"))

    @property
    def browser_profile_dir(self) -> Path:
        return STATE_DIR / "browser-profile"


def load_profile() -> dict:
    """Candidate facts from profile/profile.yaml (falls back to the example file)."""
    path = PROFILE_DIR / "profile.yaml"
    if not path.exists():
        path = PROFILE_DIR / "profile.example.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_resume_markdown() -> str:
    path = PROFILE_DIR / "resume.md"
    if not path.exists():
        path = PROFILE_DIR / "resume.example.md"
    return path.read_text(encoding="utf-8")


def profile_is_example() -> bool:
    return not (PROFILE_DIR / "profile.yaml").exists() or not (PROFILE_DIR / "resume.md").exists()
