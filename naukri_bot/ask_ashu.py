"""Inbox of questions the bot could not answer, for Ashu to settle once (output/questions_for_ashu.csv).

Every site's "no answer" path records the question here, deduplicated on its normalised wording, so a new question is
seen and answered (as a rule, profile fact or canned answer) instead of being lost in a run log (Ashu, 14 Oct 2026).
"""
from __future__ import annotations

import csv
import re
from datetime import datetime

from .config import OUTPUT_DIR

PATH = OUTPUT_DIR / "questions_for_ashu.csv"
FIELDS = ["first_seen", "last_seen", "times", "site", "company", "question", "options", "status"]
# page furniture and fields the person fills by design, not questions
SKIP = re.compile(r"^(captcha|upload|resume|cv|first|last|middle|name|email|phone|address|city|state|zip|postal|country)\b"
                  r"|^(work experience|education) \d|my experience \(not filled", re.I)


def _norm(q: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", q.lower())).strip()[:160]


def record(question: str, options: list[str] | None = None, site: str = "", company: str = "") -> None:
    q = " ".join(str(question or "").split())
    if len(q) < 4 or SKIP.search(q):
        return
    try:
        rows = list(csv.DictReader(open(PATH, encoding="utf-8"))) if PATH.exists() else []
        now = datetime.now().isoformat(timespec="seconds")
        key = _norm(q)
        for r in rows:
            if _norm(r["question"]) == key:
                r["last_seen"], r["times"] = now, str(int(r.get("times") or 1) + 1)
                if company and company not in r["company"]:
                    r["company"] = (r["company"] + "; " + company).strip("; ")[:200]
                break
        else:
            rows.append({"first_seen": now, "last_seen": now, "times": "1", "site": site, "company": company,
                         "question": q[:600], "options": " | ".join(options or [])[:600], "status": "new"})
        PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader(); w.writerows(rows)
    except Exception:
        pass                                  # never let the inbox break an application
