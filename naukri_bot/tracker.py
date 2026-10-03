"""CSV logs of every application attempt plus a JSON set of already-processed job ids."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from .config import OUTPUT_DIR, STATE_DIR

APPLICATIONS_COLUMNS = ["timestamp", "job_id", "title", "company", "location", "experience", "url",
                        "mode", "status", "questions_answered", "notes", "resume_pdf"]
COMPANY_SITE_COLUMNS = ["timestamp", "job_id", "title", "company", "location", "experience", "salary",
                        "skills", "naukri_url", "external_url", "fit_score", "fit_notes",
                        "resume_pdf", "resume_docx", "resume_md", "status"]
ANSWERS_COLUMNS = ["timestamp", "job_id", "company", "question", "kind", "options", "answer", "source"]


def _append(path: Path, columns: list[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in columns})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Tracker:
    def __init__(self, output_dir: Path | None = None, state_dir: Path | None = None):
        self.output_dir = output_dir or OUTPUT_DIR
        self.state_dir = state_dir or STATE_DIR
        self.applications_csv = self.output_dir / "applications.csv"
        self.company_site_csv = self.output_dir / "company_site_jobs.csv"
        self.answers_csv = self.output_dir / "answers.csv"
        self.seen_path = self.state_dir / "seen_jobs.json"
        self.seen: dict[str, dict] = {}
        if self.seen_path.exists():
            try:
                self.seen = json.loads(self.seen_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.seen = {}

    def applied_today(self) -> int:
        """Applications with status 'applied' recorded today (for the daily quota)."""
        if not self.applications_csv.exists():
            return 0
        today = datetime.now().date().isoformat()
        with open(self.applications_csv, newline="", encoding="utf-8") as f:
            return sum(1 for r in csv.DictReader(f) if r.get("status") in ("applied", "applied_partial") and r.get("timestamp", "").startswith(today))

    def is_seen(self, job_id: str) -> bool:
        return job_id in self.seen

    def mark_seen(self, job_id: str, status: str) -> None:
        self.seen[job_id] = {"status": status, "timestamp": _now()}
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        self.seen_path.write_text(json.dumps(self.seen, indent=2), encoding="utf-8")

    def record_application(self, job, mode: str, status: str, questions_answered: int = 0,
                           notes: str = "", resume_pdf: str = "", remember: bool = True) -> None:
        _append(self.applications_csv, APPLICATIONS_COLUMNS, {
            "timestamp": _now(), "job_id": job.job_id, "title": job.title, "company": job.company,
            "location": job.location, "experience": job.experience, "url": job.url, "mode": mode,
            "status": status, "questions_answered": questions_answered, "notes": notes, "resume_pdf": resume_pdf,
        })
        if remember:
            self.mark_seen(job.job_id, status)

    def record_company_site(self, job, external_url: str, fit_score, fit_notes: str, files: dict, status: str = "pending_manual") -> None:
        _append(self.company_site_csv, COMPANY_SITE_COLUMNS, {
            "timestamp": _now(), "job_id": job.job_id, "title": job.title, "company": job.company,
            "location": job.location, "experience": job.experience, "salary": job.salary,
            "skills": ", ".join(job.skills), "naukri_url": job.url, "external_url": external_url,
            "fit_score": fit_score, "fit_notes": fit_notes,
            "resume_pdf": files.get("pdf", ""), "resume_docx": files.get("docx", ""), "resume_md": files.get("md", ""),
            "status": status,
        })

    def record_answer(self, job, question: str, kind: str, options: list[str], answer: str, source: str) -> None:
        _append(self.answers_csv, ANSWERS_COLUMNS, {
            "timestamp": _now(), "job_id": job.job_id if job else "", "company": job.company if job else "",
            "question": question, "kind": kind, "options": " | ".join(options), "answer": answer, "source": source,
        })
