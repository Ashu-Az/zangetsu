"""Cache of reusable question→answer pairs so repeated recruiter questions cost nothing."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from .config import STATE_DIR
from .tracker import _write_atomic


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
        """Best cached answer for this question, with `indices` re-computed for the options as shown now.
        Stored answers are option *labels*, so a reordered option list cannot flip Yes and No."""
        q = normalize(question)
        if not q:                      # non-Latin text normalizes to "", which would match everything
            return None
        cur = [normalize(o) for o in options]
        opts = sorted(cur)
        best, best_ratio = None, 0.0
        for e in self.entries:
            if e["options"] != opts:
                continue
            ratio = difflib.SequenceMatcher(None, q, e["question"]).ratio()
            if ratio > best_ratio:
                best, best_ratio = e, ratio
        if not best or best_ratio < threshold:
            return None
        if not options:
            return {**best, "indices": []}
        labels = best.get("labels")
        if labels is None:             # entry from before labels were stored: positions are not trustworthy
            return None
        try:
            indices = [cur.index(l) for l in labels]
        except ValueError:
            return None
        return {**best, "indices": indices, "answer_text": ", ".join(options[i] for i in indices)}

    def save(self, question: str, options: list[str], answer_text: str, indices: list[int], source: str = "auto") -> None:
        q = normalize(question)
        if not q:
            return
        opts = sorted(normalize(o) for o in options)
        labels = [normalize(options[i]) for i in indices if 0 <= i < len(options)]
        if options and not labels:
            return
        self.entries = [e for e in self.entries if not (e["question"] == q and e["options"] == opts)]
        self.entries.append({"question": q, "raw_question": question, "options": opts,
                             "raw_options": options, "answer_text": answer_text, "labels": labels, "source": source})
        _write_atomic(self.path, json.dumps(self.entries, indent=2, ensure_ascii=False))

    def forget(self, question: str, options: list[str]) -> None:
        q = normalize(question)
        opts = sorted(normalize(o) for o in options)
        self.entries = [e for e in self.entries if not (e["question"] == q and e["options"] == opts)]
        _write_atomic(self.path, json.dumps(self.entries, indent=2, ensure_ascii=False))
