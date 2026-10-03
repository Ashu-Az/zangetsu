"""Plain data containers shared across modules."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class ApplyMode(str, Enum):
    NAUKRI = "naukri_apply"            # plain Apply button, no questions
    QUESTIONNAIRE = "questionnaire"    # Apply button that opens the chatbot drawer
    COMPANY_SITE = "company_site"      # redirects to the employer's career page
    ALREADY_APPLIED = "already_applied"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass
class JobCard:
    job_id: str
    title: str
    company: str
    url: str
    experience: str = ""
    location: str = ""
    snippet: str = ""
    posted: str = ""
    skills: list[str] = field(default_factory=list)


@dataclass
class JobPosting:
    job_id: str
    url: str
    title: str = ""
    company: str = ""
    experience: str = ""
    salary: str = ""
    location: str = ""
    posted: str = ""
    applicants: int | None = None      # parsed from "Applicants: 37" / "50+" / "Less than 10"
    openings: int | None = None
    description: str = ""
    skills: list[str] = field(default_factory=list)
    role: str = ""
    industry: str = ""
    department: str = ""
    employment_type: str = ""
    role_category: str = ""
    education: str = ""
    about_company: str = ""

    def experience_range(self) -> tuple[int | None, int | None]:
        """'2 - 5 years' -> (2, 5); '3+ years' -> (3, None); '' -> (None, None)."""
        import re
        m = re.search(r"(\d+)\s*-\s*(\d+)", self.experience)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"(\d+)\s*\+?", self.experience)
        return (int(m.group(1)), None) if m else (None, None)

    def experience_fits(self, years: float | None, slack: int = 0) -> bool:
        """True if a candidate with `years` experience is inside the posting's range (+slack on the low end)."""
        if years is None:
            return True
        lo, hi = self.experience_range()
        if lo is not None and years + slack < lo:
            return False
        if hi is not None and years > hi:
            return False
        return True

    def to_prompt_text(self, max_desc_chars: int = 6000) -> str:
        desc = self.description[:max_desc_chars]
        lines = [
            f"Title: {self.title}",
            f"Company: {self.company}",
            f"Location: {self.location}",
            f"Experience required: {self.experience}",
            f"Salary: {self.salary}",
            f"Applicants so far: {self.applicants if self.applicants is not None else 'unknown'}",
            f"Role: {self.role} | Department: {self.department} | Industry: {self.industry}",
            f"Employment type: {self.employment_type}",
            f"Education: {self.education}",
            f"Key skills: {', '.join(self.skills)}",
            "",
            "Job description:",
            desc,
        ]
        if self.about_company:
            lines += ["", "About the company:", self.about_company[:800]]
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return asdict(self)
