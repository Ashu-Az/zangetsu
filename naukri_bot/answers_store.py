"""Cache of reusable question→answer pairs so repeated recruiter questions cost nothing."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from .config import STATE_DIR


def normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class AnswerCache:
    def __init__(self, path: Path | None = None):
        self.path = path or STATE_DIR / "answer_cache.json"
        self.entries: list[dict] = []
        if self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.entries = []

    def lookup(self, question: str, options: list[str], threshold: float = 0.9) -> dict | None:
        q = normalize(question)
        opts = sorted(normalize(o) for o in options)
        best, best_ratio = None, 0.0
        for e in self.entries:
            if e["options"] != opts:
                continue
            ratio = difflib.SequenceMatcher(None, q, e["question"]).ratio()
            if ratio > best_ratio:
                best, best_ratio = e, ratio
        return best if best and best_ratio >= threshold else None

    def save(self, question: str, options: list[str], answer_text: str, indices: list[int]) -> None:
        q = normalize(question)
        opts = sorted(normalize(o) for o in options)
        self.entries = [e for e in self.entries if not (e["question"] == q and e["options"] == opts)]
        self.entries.append({"question": q, "raw_question": question, "options": opts,
                             "raw_options": options, "answer_text": answer_text, "indices": indices})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False), encoding="utf-8")

    def forget(self, question: str, options: list[str]) -> None:
        q = normalize(question)
        opts = sorted(normalize(o) for o in options)
        self.entries = [e for e in self.entries if not (e["question"] == q and e["options"] == opts)]
        self.path.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False), encoding="utf-8")
