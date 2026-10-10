"""CSV logs of every application attempt plus a JSON set of already-processed job ids."""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from .config import OUTPUT_DIR, STATE_DIR

APPLICATIONS_COLUMNS = ["timestamp", "job_id", "title", "company", "location", "experience", "url",
                        "mode", "status", "questions_answered", "notes", "resume_pdf"]
COMPANY_SITE_COLUMNS = ["timestamp", "job_id", "title", "company", "location", "experience", "salary",
                        "skills", "naukri_url", "external_url", "fit_score", "fit_notes",
                        "resume_pdf", "resume_docx", "resume_md", "status"]
# Statuses that get another try on a later run: status -> total attempts allowed.
RETRY_LIMITS = {"failed": 2, "deferred": 3, "needs_you": 3, "ready_for_review": 3, "needs_verification": 3}
RETRY_AFTER_HOURS = 6
# Outcomes where an application may have reached the employer. They are final (not in RETRY_LIMITS), count toward
# daily caps, and block the same role for 30 days, so an unconfirmed submit is never sent a second time.
SUBMITTED_STATUSES = ("applied", "applied_partial", "submitted_unconfirmed", "unconfirmed", "crashed_after_submit")

ANSWERS_COLUMNS = ["timestamp", "job_id", "company", "question", "kind", "options", "answer", "source"]


def _append(path: Path, columns: list[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in columns})


def _write_atomic(path: Path, text: str) -> None:
    """Write to a temp file and rename, so a crash mid-write cannot leave a truncated JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


NAUKRI_MODES = {"naukri_apply", "questionnaire", "nvite"}


class Tracker:
    def __init__(self, output_dir: Path | None = None, state_dir: Path | None = None, read_only: bool = False):
        self.read_only = read_only           # dry runs: log nothing as processed, so a real run still sees every job
        self.output_dir = output_dir or OUTPUT_DIR
        self.state_dir = state_dir or STATE_DIR
        self.applications_csv = self.output_dir / "applications.csv"
        self.company_site_csv = self.output_dir / "company_site_jobs.csv"
        self.answers_csv = self.output_dir / "answers.csv"
        self.seen_path = self.state_dir / "seen_jobs.json"
        self._touched: set[str] = set()
        self.seen: dict[str, dict] = {}
        if self.seen_path.exists():
            try:
                self.seen = json.loads(self.seen_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.seen = {}

    def applied_last_24h(self, portal: str = "naukri") -> int:
        """Applications in the last 24 hours. Naukri enforces its apply cap over a rolling window, not per
        calendar day: on 9 Oct 2026 a 11:11 run was refused because 8 Oct's 49 applications still counted."""
        if not self.applications_csv.exists():
            return 0
        since = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")

        def on_portal(mode: str) -> bool:
            # Naukri rows are "naukri_apply"/"questionnaire"; workday, company_site and email rows are separate channels
            return mode in NAUKRI_MODES if portal == "naukri" else mode.startswith(portal + ":")

        with open(self.applications_csv, newline="", encoding="utf-8") as f:
            return sum(1 for r in csv.DictReader(f)
                       if r.get("status") in SUBMITTED_STATUSES and r.get("timestamp", "") >= since
                       and on_portal(r.get("mode", "")))

    def applied_today(self, portal: str = "naukri") -> int:
        """Applications recorded today on one portal (for that portal's daily quota).
        Naukri rows have a bare mode ("questionnaire"); other portals write "<site>:<mode>"."""
        if not self.applications_csv.exists():
            return 0
        today = datetime.now().date().isoformat()

        def on_portal(mode: str) -> bool:
            # Naukri rows are "naukri_apply"/"questionnaire"; workday, company_site and email rows are separate channels
            return mode in NAUKRI_MODES if portal == "naukri" else mode.startswith(portal + ":")

        with open(self.applications_csv, newline="", encoding="utf-8") as f:
            return sum(1 for r in csv.DictReader(f)
                       if r.get("status") in SUBMITTED_STATUSES and r.get("timestamp", "").startswith(today)
                       and on_portal(r.get("mode", "")))

    @staticmethod
    def role_key(company: str, title: str) -> str:
        import re
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
        company = re.sub(r"\b(limited|ltd|pvt|private|inc|llp|technologies|solutions)\b", "", norm(company)).strip()
        return f"{company}|{norm(title)}"

    def recent_role_keys(self, days: int = 30, include_saved: bool = True) -> set[str]:
        """company+title of everything applied to (and, with include_saved, saved for manual) in the last `days` days, on any portal. Companies repost
        one role under several job ids (Infosys: one per city group), so the job id alone does not catch repeats."""
        if not self.applications_csv.exists():
            return set()
        from datetime import timedelta
        since = (datetime.now() - timedelta(days=days)).isoformat()
        with open(self.applications_csv, newline="", encoding="utf-8") as f:
            return {self.role_key(r.get("company", ""), r.get("title", "")) for r in csv.DictReader(f)
                    if r.get("timestamp", "") >= since
                    and r.get("status") in SUBMITTED_STATUSES + ((("saved_for_manual",)) if include_saved else ())}

    def is_seen(self, job_id: str) -> bool:
        """True if the job needs no further attempt. failed / deferred jobs come back after
        RETRY_AFTER_HOURS until they have used RETRY_LIMITS attempts."""
        e = self.seen.get(job_id)
        if e is None:
            return False
        limit = RETRY_LIMITS.get(e.get("status", ""))
        if limit is None or e.get("attempts", 1) >= limit:
            return True
        try:
            age_h = (datetime.now() - datetime.fromisoformat(e["timestamp"])).total_seconds() / 3600
        except (KeyError, ValueError):
            return False
        return age_h < RETRY_AFTER_HOURS

    def mark_seen(self, job_id: str, status: str) -> None:
        if self.read_only:
            return
        # several bots (Naukri, Indeed, Hirist...) share this file: merge the latest copy from disk before writing,
        # so one run never resurrects or drops another run's entries
        try:
            disk = json.loads(self.seen_path.read_text())
        except (OSError, ValueError):
            disk = {}
        disk.update({k: self.seen[k] for k in self._touched if k in self.seen})   # only what this run wrote
        self.seen = disk
        prev = self.seen.get(job_id) or {}
        attempts = prev.get("attempts", 1) + 1 if prev.get("status") == status and status in RETRY_LIMITS else 1
        self.seen[job_id] = {"status": status, "timestamp": _now(), "attempts": attempts}
        self._touched.add(job_id)
        _write_atomic(self.seen_path, json.dumps(self.seen, indent=2))

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
