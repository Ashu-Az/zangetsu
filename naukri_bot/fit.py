"""Would Ashu apply to this job? A small, explainable fit model on top of the title rules.

Titles lie in both directions: startups post "Founding Engineer" or "Builder" for Python/LLM work, and a title can
pass the keyword list while the job is really sourcing, accounts payable or partnerships (Databricks "Tech Sourcer",
Okta "AP & Expenses Analyst", FreJun "Strategic Partnerships Associate", 12-13 Nov 2026). So the job description
decides, and every decision comes with its reasons.

Signals (all from the posting text: title + description + key skills + role fields):
  - core:     the candidate's own stack named in the JD (Python, LLM/RAG, ML, React/Node, FastAPI...)
  - non_tech: phrases that only appear in non-engineering jobs (quotas, cold calling, sourcing candidates, ledgers...)
  - off_stack: a different primary stack (Java/Spring, .NET, PHP, Salesforce, SAP, mobile, embedded, RTL...) that
              dominates the JD while the candidate's stack is barely mentioned
  - junior:   internship / stipend / campus programmes
  - learned:  Ashu's own yes/no feedback (state/fit_feedback.jsonl), as word log-odds over title + JD signal words

decide() returns ("apply" | "skip" | "unsure", score, reasons). "unsure" is never auto-skipped; the caller can apply
(volume rule: never miss jobs) or queue it for Ashu. Feedback:
    python -m naukri_bot fit "<title>" --jd <file|text>        # explain a decision
    python -m naukri_bot fit-feedback no "Tech Sourcer" --jd "...sourcing candidates..."   # teach it
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path

from .config import STATE_DIR

FEEDBACK_PATH = STATE_DIR / "fit_feedback.jsonl"

# The candidate's stack, weighted: what he leads with counts more.
CORE = {
    3: [r"python", r"llms?", r"large language models?", r"gen(erative)? ?ai", r"rag", r"retrieval[- ]augmented",
        r"langchain", r"langgraph", r"llamaindex", r"agentic", r"ai agents?", r"prompt engineering", r"fastapi",
        r"machine learning", r"deep learning", r"pytorch", r"nlp", r"embeddings?", r"vector (db|database|search)s?"],
    2: [r"django", r"flask", r"react(\.js)?", r"next\.?js", r"node(\.js)?", r"typescript", r"mern", r"full[- ]?stack",
        r"backend", r"rest(ful)? apis?", r"microservices", r"data scien\w*", r"scikit[- ]learn", r"tensorflow",
        r"hugging ?face", r"transformers", r"computer vision", r"mlops", r"fine[- ]tun\w*", r"openai"],
    1: [r"javascript", r"sql", r"postgres\w*", r"mongodb", r"redis", r"docker", r"kubernetes", r"aws", r"gcp", r"azure",
        r"git", r"ci/cd", r"pandas", r"numpy", r"apis?", r"software engineer\w*", r"golang|go lang"],
}
NON_TECH = [
    r"sales targets?", r"revenue targets?", r"quota", r"cold call\w*", r"lead generation", r"client acquisition",
    r"business development (executive|manager|associate)", r"strategic partnerships?", r"partnership (manager|development)",
    r"channel partners?", r"go[- ]to[- ]market (lead|manager|strategy)", r"crm (hygiene|management)",
    r"sourc(e|ing) (candidates|talent)", r"talent acquisition (partner|specialist|executive)", r"full[- ]cycle recruit\w*", r"accounts payable", r"invoice processing", r"(bank|account|vendor) reconciliations?", r"general ledger",
    r"expense (reports|management)", r"payroll", r"tally", r"gst returns?", r"audit(ing)? (team|procedures)",
    r"scrum master", r"(conversation|call) quality", r"customer support (executive|associate|representative)", r"teaching (faculty|assistant)", r"tutors?\b",
    r"curriculum (design|development)",
    r"content writ\w*", r"social media", r"\bseo\b", r"graphic design", r"resource management group|\brmg\b",
    r"phd (in )?(physics|chemistry|biology|mathematics)", r"\bmathematics expert\b",
]
OFF_STACK = [
    r"java\b(?!script)", r"spring( boot)?", r"j2ee", r"\.net", r"c#", r"asp\.net", r"php", r"laravel", r"salesforce",
    r"apex", r"\bsap\b", r"abap", r"mainframe", r"cobol", r"ios", r"swift", r"android", r"kotlin", r"flutter",
    r"embedded", r"firmware", r"verilog", r"\brtl\b", r"vlsi", r"plc", r"scada", r"servicenow", r"pega", r"oracle ebs",
]
JUNIOR = [r"\binternship\b", r"\bintern\b", r"stipend", r"summer analyst", r"campus (hiring|program)", r"\btrainee\b",
          r"fresher(s)? only", r"graduate (trainee|program)"]


def _label(pattern: str) -> str:
    """A readable name for a pattern: 'java\\b(?!script)' -> 'java', 'sourc(e|ing) (candidates|talent)' -> 'source candidates'."""
    p = re.sub(r"\(\?![^)]*\)", "", pattern)                       # lookaheads
    p = re.sub(r"\((\w+)\|[^)]*\)", r"\1", p)                       # (a|b) -> a
    p = re.sub(r"\\b|\\w\*|\?|\[- \]|\[\.\]|\\", lambda m: " " if m.group(0) == "[- ]" else "", p)
    p = p.split("|")[0]
    return re.sub(r"\s+", " ", p.replace("(", "").replace(")", "")).strip()


def _count(patterns, text: str) -> list[str]:
    return [p for p in patterns if re.search(rf"(?<![a-z0-9]){p}(?![a-z0-9])", text)]


def posting_text(job) -> str:
    parts = [getattr(job, f, "") or "" for f in ("title", "description", "role", "role_category", "department")]
    parts.append(" ".join(getattr(job, "skills", None) or []))
    return " ".join(parts).lower()


_WORD = re.compile(r"[a-z][a-z.+#]{1,24}")
_STOP = set("the and for with you our are will this that your from have has who can job role team work years year "
            "experience ability strong good about all any not but more such using use etc into across new".split())


def _tokens(title: str, text: str) -> list[str]:
    """Title words, plus the JD words that match any signal list (keeps the learned part on meaningful words)."""
    words = [w for w in _WORD.findall((title or "").lower()) if w not in _STOP]
    signal = set()
    for group in (CORE[3], CORE[2], CORE[1], NON_TECH, OFF_STACK, JUNIOR):
        for p in _count(group, text):
            signal.add("jd:" + re.sub(r"\W+", "_", p)[:30])
    return ["t:" + w for w in words] + sorted(signal)


class Feedback:
    """Ashu's labels: one JSON line per decision {"title", "company", "text", "label": 1|0}."""

    def __init__(self, path: Path = FEEDBACK_PATH):
        self.path = path
        self.pos, self.neg, self.n_pos, self.n_neg = Counter(), Counter(), 0, 0
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                toks = set(_tokens(r.get("title", ""), (r.get("text") or "").lower()))
                if r.get("label"):
                    self.pos.update(toks); self.n_pos += 1
                else:
                    self.neg.update(toks); self.n_neg += 1

    def add(self, title: str, text: str, label: bool, company: str = "") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps({"title": title, "company": company, "text": (text or "")[:4000], "label": int(label)}) + "\n")

    def log_odds(self, title: str, text: str) -> tuple[float, list[str]]:
        """Naive-Bayes style evidence from past labels (Laplace-smoothed); 0 with no feedback."""
        if not (self.n_pos and self.n_neg):
            return 0.0, []
        total, why = 0.0, []
        for t in set(_tokens(title, text)):
            p = (self.pos[t] + 1) / (self.n_pos + 2)
            n = (self.neg[t] + 1) / (self.n_neg + 2)
            w = math.log(p / n)
            if abs(w) >= 0.9 and (self.pos[t] + self.neg[t]) >= 2:
                total += w
                why.append(f"{t} {w:+.1f}")
        return max(-6.0, min(6.0, total)), sorted(why, key=lambda s: -abs(float(s.split()[-1])))[:4]


_FEEDBACK: Feedback | None = None


def feedback() -> Feedback:
    global _FEEDBACK
    if _FEEDBACK is None:
        _FEEDBACK = Feedback()
    return _FEEDBACK


def decide(job, profile: dict | None = None, fb: Feedback | None = None) -> tuple[str, float, list[str]]:
    text = posting_text(job)
    title = (getattr(job, "title", "") or "").lower()
    reasons: list[str] = []
    core = {w: _count(pats, text) for w, pats in CORE.items()}
    core_score = sum(w * len(h) for w, h in core.items())
    core_names = [p for w in (3, 2, 1) for p in core[w]]
    non_tech = _count(NON_TECH, text)
    off = _count(OFF_STACK, text)
    junior = _count(JUNIOR, title) + _count([r"stipend"], text[:800])

    score = min(core_score, 24) / 2.0                              # 0..12
    if core_names:
        reasons.append("stack: " + ", ".join(_label(p) for p in core_names[:6]))
    if non_tech:
        # a stray word ("reconciliation" in a data job, "SEO" in a web job, an AI product for accounts payable) must
        # not sink a real engineering JD: the penalty is full only when the JD names little of the stack
        score -= (3.0 if core_score < 12 else 1.0) * len(non_tech)
        reasons.append("non-engineering: " + ", ".join(_label(p) for p in non_tech[:3]))
    lead = len(core[3]) + len(core[2])
    off_stack = bool(off and len(off) >= 2 and lead <= 1)
    if off_stack:
        # Java / .NET / PHP-first jobs: Ashu applies to these too (claim-all-tools rule), so they are "unsure"
        # (still applied to by default), never an automatic skip; his feedback can turn them into skips
        reasons.append("other primary stack: " + ", ".join(_label(p) for p in off[:3]))
    if junior:
        score -= 4.0
        reasons.append("internship/campus: " + ", ".join(_label(p) for p in junior[:2]))

    thin = len(re.sub(r"\s+", " ", getattr(job, "description", "") or "")) < 300   # listing saved without its JD

    fb = fb if fb is not None else feedback()
    learned, why = fb.log_odds(getattr(job, "title", ""), text)
    if learned:
        score += learned
        reasons.append("your past choices: " + "; ".join(why))

    software_title = bool(re.search(r"software|developer|\bsde\b|\bswe\b|programmer|\bai\b|\bml\b|machine learning|"
                                    r"data scien|full[- ]?stack|backend|back-end|frontend|front-end|llm|gen ?ai|"
                                    r"python|prompt engineer|forward deployed|founding engineer|member of technical staff|\bmts\b", title))
    if non_tech and core_score < 8 and (len(non_tech) >= 2 or (core_score < 4 and not software_title)):
        return "skip", score, reasons
    if _count(JUNIOR, title):                     # an internship / campus title: he is not a fresher
        return "skip", score, reasons
    if junior and core_score < 12:
        return "skip", score, reasons
    if learned <= -3.0:                            # his own "no" for jobs like this outweighs every safety net
        return "skip", score, reasons
    if off_stack:
        return ("skip" if learned <= -2.0 else "unsure"), score, reasons
    if score >= 4.0:
        return "apply", score, reasons
    if (thin or software_title) and len(non_tech) <= (1 if software_title else 0) and not junior:
        reasons.append("job description not available: decide by title" if thin else
                       "software title but the JD names little of the stack")
        return "unsure", score, reasons
    if score <= 0.0:
        return "skip", score, reasons
    return "unsure", score, reasons


SEED_NO = [  # roles Ashu turned down (12-13 Nov 2026); seeds the learned part
    ("Tech Sourcer (Contract)", "source candidates for engineering roles, full-cycle recruiting, talent acquisition"),
    ("AP & Expenses Projects Analyst", "accounts payable, invoice processing, expense reports, reconciliation, general ledger"),
    ("Strategic Partnerships Associate", "partnerships, business development, client acquisition, revenue targets"),
    ("Conversation Quality Analyst", "conversation quality, call quality, customer support transcripts"),
    ("Analyst - RMG", "resource management group, staffing allocation, utilisation reports"),
    ("Scrum Master, Platform Delivery", "scrum master, sprint ceremonies, agile coaching"),
    ("Summer Analyst 2027", "summer analyst internship programme for students, campus hiring"),
    ("Maths LLM Evaluation Expert", "mathematics expert, PhD in mathematics, competition math problems"),
]
