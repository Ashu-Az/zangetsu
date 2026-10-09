"""Grounding check: every skill, tool, practice and metric on a tailored resume must come from the candidate's facts.

A tailored resume may re-order, re-word and relabel real experience (MongoDB -> "NoSQL (MongoDB)", FAISS ->
"vector search"), but must never add a skill, tool, practice, metric, employer, date or degree the candidate does
not have. The fact sources are profile/resume_variants.yaml (skills, experience, projects, education,
certifications) and profile/profile.yaml (skills_primary, skills_secondary, skill_years keys, and the optional
`extra_skills_confirmed` list where the candidate adds skills they genuinely have but that are not written down
elsewhere, e.g. "Agile/Scrum" or "Code reviews").

    corpus = build_fact_corpus(load_variants(), load_profile())
    problems = check_resume(resume, corpus)      # -> [Ungrounded(item, where, reason), ...]

Relabels are allowed only through ALIASES below: an explicit, reviewable table of "label <- facts that justify it".
Practices that cannot be inferred from tools (Agile/Scrum, distributed systems, client-facing delivery, code
reviews, SDLC, benchmarking ...) are deliberately absent: they pass only if they are in the facts or confirmed.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

# label -> the fact terms that make the label an honest relabel. A label is grounded when any of its sources is.
ALIASES: dict[str, list[str]] = {
    "nosql": ["mongodb", "redis"],
    "vector search": ["faiss", "pinecone", "chroma"],
    "vector database": ["faiss", "pinecone", "chroma"],
    "vector databases": ["faiss", "pinecone", "chroma"],
    "rest": ["rest apis", "rest api", "fastapi", "express", "django rest"],
    "rest apis": ["fastapi", "express", "django rest"],
    "restful apis": ["fastapi", "express", "django rest", "rest apis"],
    "restful": ["fastapi", "express", "django rest", "rest apis"],
    "http": ["rest apis", "fastapi", "express"],          # building REST APIs is HTTP + JSON
    "json": ["rest apis", "fastapi", "express"],
    "ci/cd": ["github actions"],
    "containerization": ["docker", "kubernetes"],
    "containers": ["docker", "kubernetes"],
    "devops": ["github actions"],                         # only via CI/CD + containers actually used
    "llm": ["openai", "azure openai", "langchain", "llms"],
    "llms": ["openai", "azure openai", "langchain", "llm"],
    "prompt engineering": ["llm", "openai"],
    "data structures & algorithms": ["leetcode"],
    "data structures and algorithms": ["leetcode"],
    "data structures": ["leetcode"],
    "algorithms": ["leetcode"],
    "dsa": ["leetcode"],
    "problem solving": ["leetcode"],
    "problem-solving": ["leetcode"],
    "oop": ["python", "java"],
    "object-oriented programming": ["python", "java"],
    "object oriented programming": ["python", "java"],
    "html5": ["react", "next.js", "tailwind"],
    "css3": ["react", "next.js", "tailwind"],
    "html": ["react", "next.js", "tailwind"],
    "css": ["react", "next.js", "tailwind"],
    "tailwind css": ["tailwind"],
    "ui": ["react", "next.js"],
    "frontend": ["react", "next.js"],
    "front end": ["react", "next.js"],
    "front-end": ["react", "next.js"],
    "ecmascript": ["javascript"],
    "es6": ["javascript"],
    "neural networks": ["pytorch", "tensorflow", "deep learning"],
    "deep learning": ["pytorch", "tensorflow"],
    "sql": ["postgresql", "mysql"],
    "relational databases": ["postgresql", "mysql"],
    "relational database": ["postgresql", "mysql"],
    "orm": ["sqlalchemy", "django orm"],
    "object-relational mapping": ["sqlalchemy", "django orm"],
    # spelling variants of the same tool
    "postgres": ["postgresql"],
    "k8s": ["kubernetes"],
    "react.js": ["react"],
    "reactjs": ["react"],
    "nodejs": ["node.js"],
    "nextjs": ["next.js"],
    "express.js": ["express"],
    "huggingface": ["hugging face"],
    "genai": ["generative ai"],
    "gen ai": ["generative ai"],
    "scikit-learn": ["scikit-learn"],
    "django": ["django rest"],
    "django rest framework": ["django rest"],
    # LLM tool-use is what these labels mean
    "tool calling": ["tool-use"],
    "function calling": ["tool-use"],
    "tool use": ["tool-use"],
    "agentic": ["tool-use"],
    "agents": ["tool-use"],
    "ai agents": ["tool-use"],
}

# Curated tech / practice vocabulary: these terms are checked wherever they appear (headline, summary, skills).
# Ordinary English words are never flagged; only these.
VOCAB: list[str] = [
    # languages
    "Python", "Java", "JavaScript", "TypeScript", "Golang", "Rust", "C++", "C#", "Scala", "Kotlin", "Ruby", "PHP",
    "Swift", "Perl", "Bash", "Shell scripting", "MATLAB", "Dart", "Solidity",
    # web / backend
    "Django", "Flask", "FastAPI", "Spring Boot", "Spring", "Hibernate", ".NET", "ASP.NET", "Node.js", "Express",
    "NestJS", "React", "React Native", "Angular", "Vue", "Vue.js", "Next.js", "Redux", "Svelte", "jQuery", "Tailwind",
    "Bootstrap", "HTML", "HTML5", "CSS", "CSS3", "SASS", "GraphQL", "gRPC", "REST APIs", "WebSockets", "Kafka",
    "RabbitMQ", "Celery", "Microservices", "MERN", "MEAN", "OAuth", "JWT", "Nginx", "Laravel",
    # data
    "SQL", "NoSQL", "PostgreSQL", "MySQL", "MongoDB", "Redis", "SQLite", "SQL Server", "Oracle", "Cassandra",
    "DynamoDB", "Elasticsearch", "Elastic", "TimescaleDB", "Snowflake", "BigQuery", "Databricks", "Spark",
    "PySpark", "Hadoop", "Hive", "Airflow", "dbt", "ETL", "Tableau", "Power BI", "Looker", "Excel", "SQLAlchemy",
    "Pandas", "NumPy", "Transact SQL", "T-SQL", "PL/SQL",
    # cloud / devops
    "AWS", "Azure", "GCP", "Google Cloud", "Lambda", "EC2", "S3", "SageMaker", "Vertex AI", "Bedrock", "Docker",
    "Kubernetes", "Helm", "Terraform", "Ansible", "Jenkins", "GitHub Actions", "GitLab", "CI/CD", "DevOps", "MLOps",
    "Linux", "Git", "Prometheus", "Grafana", "Datadog", "Containerization", "Serverless", "Microsoft Fabric",
    # AI / ML
    "Machine Learning", "Deep Learning", "NLP", "Computer Vision", "LLM", "LLMs", "RAG", "Generative AI", "GenAI",
    "LoRA", "QLoRA", "RLHF", "DPO", "Fine-tuning", "Prompt engineering", "Embeddings", "Vector search",
    "Vector database", "FAISS", "Pinecone", "Chroma", "Weaviate", "Milvus", "Qdrant", "OpenAI", "Azure OpenAI",
    "LangChain", "LangGraph", "LlamaIndex", "CrewAI", "AutoGen", "Hugging Face", "Transformers", "PyTorch",
    "TensorFlow", "Keras", "Scikit-Learn", "XGBoost", "LightGBM", "OpenCV", "YOLO", "BERT", "DistilBERT", "GPT",
    "Reinforcement learning", "Neural networks", "Agentic", "MCP", "Ollama", "vLLM", "Gemini", "Claude",
    # practices that must be in the facts (no alias can create them)
    "Agile", "Scrum", "Kanban", "JIRA", "SDLC", "TDD", "BDD", "Distributed systems", "System design",
    "Code reviews", "Code review", "Benchmarking", "Client-facing", "Stakeholder management", "Unit testing",
    "Integration testing", "Pytest", "Selenium", "Cypress", "Jest", "Load testing", "OOP", "Object-oriented",
    "Data structures", "Algorithms", "Design patterns", "Event-driven", "High availability",
    "Mentoring", "Team lead",
]
# matched case-sensitively in prose: they are also ordinary English words ("rest of", "spring", "swift")
CASE_SENSITIVE = {"REST", "Rust", "Swift", "Spring", "Elastic", "Lambda", "Oracle", "Express", "Excel", "Bash",
                  "Dart", "Chroma", "Gemini", "Claude", "Hive", "GPT", "MCP", "Helm", "Jest", "Looker", "React"}
# acronyms that are fine in a headline/summary without being a skill claim
_ACRONYM_OK = {"AI", "ML", "II", "III", "IV", "IT", "UI", "UX", "API", "APIS", "SDE", "SWE", "HR", "B2B", "B2C",
               "US", "UK", "SAAS", "QA", "ID", "CTC", "LPA", "PDF", "PDFS", "MS"}

_STOP = {"and", "or", "the", "a", "an", "of", "in", "on", "for", "with", "to", "via", "from", "by", "at", "as", "into",
         "using", "use", "based", "end", "e.g", "eg", "etc", "including", "plus", "across", "over", "&", "/", "-"}
# vague nouns that never make a claim on their own (a phrase made only of these must match the facts literally)
_GENERIC = {"development", "developer", "developers", "engineering", "engineer", "application", "applications",
            "app", "apps", "service", "services", "system", "systems", "solution", "solutions", "work", "skills",
            "experience", "programming", "software", "technologies", "technology", "framework", "frameworks",
            "practices", "concepts", "fundamentals", "basics", "knowledge", "tools", "tooling", "design",
            "architecture", "architectures", "enabled", "powered", "driven"}


@dataclass
class Ungrounded:
    item: str
    where: str           # "skills:<group>", "headline" or "summary"
    reason: str

    def __str__(self) -> str:
        return f"{self.where}: {self.item!r} ({self.reason})"


def _norm(text: str) -> str:
    t = (text or "").lower().replace("’", "'").replace("–", "-").replace("—", "-")
    t = re.sub(r"(\d),(?=\d{3})", r"\1", t)                     # 10,000 -> 10000
    t = re.sub(r"\bopenai api\b", "openai api", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9+#]*(?:\.[a-z0-9]+)*\+?%?", _norm(text))


def _stem(w: str) -> str:
    w = w.rstrip("%")
    if re.fullmatch(r"[\d.+]+", w):
        return w.rstrip("+")
    w = re.sub(r"is(ation|ations|ing|ed|es|e)$", r"iz\1", w)    # British -> US spelling
    for suf in ("izations", "ization", "izing", "ized", "izes", "ize", "ations", "ation", "ings", "ing", "ers", "er",
                "ies", "es", "ed", "s", "e"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[: -len(suf)]
            break
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "s":      # debugg -> debug
        w = w[:-1]
    return w


def _in_text(phrase: str, text: str) -> bool:
    p = _norm(phrase)
    return bool(p) and re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", text) is not None


class FactCorpus(set):
    """Normalised phrases and tokens from every fact source (a set), plus the joined text and word stems."""

    def __init__(self, phrases):
        super().__init__(p for p in (_norm(x) for x in phrases) if p)
        self.text = " | ".join(sorted(self))
        self.stems = {_stem(t) for t in _tokens(self.text)}

    def add_phrase(self, phrase: str) -> None:
        p = _norm(phrase)
        if p and p not in self:
            self.add(p)
            self.text += " | " + p
            self.stems |= {_stem(t) for t in _tokens(p)}

    def has_phrase(self, phrase: str) -> bool:
        p = _norm(phrase)
        return p in self or _in_text(p, self.text)


def _strings(obj) -> list[str]:
    if obj is None:
        return []
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, (list, tuple, set)):
        return [s for v in obj for s in _strings(v)]
    return [str(obj)] if isinstance(obj, (int, float)) else []


def build_fact_corpus(variants_core: dict, profile: dict | None = None) -> FactCorpus:
    """Every fact source as one normalised corpus. `variants_core` is the loaded resume_variants.yaml (or just its
    `core` section); `profile` is profile.yaml. Contact details are not facts about skills and are left out."""
    data = variants_core or {}
    core = data.get("core", data)
    facts: list[str] = []
    for e in core.get("experience", []) or []:
        facts += _strings({k: e.get(k) for k in ("title", "company", "dates", "bullets")})
    facts += _strings(core.get("education"))
    facts += _strings(core.get("certifications"))
    for p in (data.get("projects") or {}).values():
        facts += _strings({k: p.get(k) for k in ("name", "description", "tech")})
    for v in (data.get("variants") or {}).values():
        for g in v.get("skills", []) or []:
            facts += [g.get("name", "")] + list(g.get("items", []))
    prof = profile or {}
    facts += _strings(prof.get("skills_primary")) + _strings(prof.get("skills_secondary"))
    facts += list((prof.get("skill_years") or {}).keys())
    facts += _strings(prof.get("extra_skills_confirmed"))
    facts += _strings({k: prof.get(k) for k in ("current_title", "highest_education")})
    corpus = FactCorpus(facts)
    changed = True
    while changed:                                   # aliases can chain (openai -> llm -> prompt engineering)
        changed = False
        for label, sources in ALIASES.items():
            if not corpus.has_phrase(label) and any(corpus.has_phrase(s) for s in sources):
                corpus.add_phrase(label)
                changed = True
    return corpus


def _vocab_regex(term: str) -> re.Pattern:
    flags = 0 if term in CASE_SENSITIVE else re.I
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9]|\.[A-Za-z])", flags)


_VOCAB = [(t, _vocab_regex(t)) for t in sorted(VOCAB, key=len, reverse=True)] + [("REST", _vocab_regex("REST"))]


def vocab_terms(text: str) -> list[str]:
    """Curated tech/practice terms mentioned in text (longest match wins: "Azure OpenAI" before "Azure")."""
    found, taken = [], []
    for term, rx in _VOCAB:
        for m in rx.finditer(text or ""):
            if any(a <= m.start() < b or a < m.end() <= b for a, b in taken):
                continue
            taken.append((m.start(), m.end()))
            if term not in found:
                found.append(term)
    return found


def _phrase_grounded(phrase: str, corpus: FactCorpus) -> bool:
    """A phrase is grounded when it appears in the facts, or every distinctive word in it does."""
    p = _norm(phrase).strip(" .,;:-/&")
    if not p:
        return True
    if corpus.has_phrase(p):
        return True
    words = [w for w in _tokens(p) if w not in _STOP and len(w) > 1]
    content = [w for w in words if w not in _GENERIC]
    if not content:
        return False                     # only vague words ("System design" is not a fact unless written down)
    return all(_stem(w) in corpus.stems or corpus.has_phrase(w) for w in content)


def _term_grounded(term: str, corpus: FactCorpus) -> bool:
    return corpus.has_phrase(term)


def check_skill_item(item: str, corpus: FactCorpus) -> str | None:
    """None if the item is grounded, else the reason."""
    bad = [t for t in vocab_terms(item) if not _term_grounded(t, corpus)]
    if bad:
        return "not in facts: " + ", ".join(bad)
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", item.strip())
    main, paren = (m.group(1), m.group(2)) if m else (item, "")
    if _phrase_grounded(main, corpus):
        return None
    if paren and all(_phrase_grounded(part, corpus) for part in re.split(r";|,(?!\d{3})", paren) if part.strip()):
        return None
    return "no matching fact"


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[,+%]", "", n) for n in re.findall(r"\d[\d,]*(?:\.\d+)?\+?%?", text or "")}


def check_text(text: str, where: str, corpus: FactCorpus) -> list[Ungrounded]:
    """Headline/summary: flag curated tech/practice terms, unknown acronyms and metrics not in the facts."""
    out = [Ungrounded(t, where, "term not in facts") for t in vocab_terms(text) if not _term_grounded(t, corpus)]
    flagged = {u.item.lower() for u in out}
    for a in dict.fromkeys(re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,5}(?![A-Za-z0-9])", text or "")):
        if a.upper() not in _ACRONYM_OK and a.lower() not in flagged and not corpus.has_phrase(a):
            out.append(Ungrounded(a, where, "acronym not in facts"))
    known = _numbers(corpus.text)
    for n in sorted(_numbers(text) - known):
        out.append(Ungrounded(n, where, "number/metric not in facts"))
    return out


def _get(resume, key, default=None):
    return resume.get(key, default) if isinstance(resume, dict) else getattr(resume, key, default)


def check_resume(resume, corpus: FactCorpus) -> list[Ungrounded]:
    """Every ungrounded skill item, and every ungrounded term in the headline and summary. `resume` is a
    TailoredResume or a dict with headline/summary/skills (skills as [{name, items}] or SkillGroup objects)."""
    out = check_text(_get(resume, "headline", "") or "", "headline", corpus)
    out += check_text(_get(resume, "summary", "") or "", "summary", corpus)
    for g in _get(resume, "skills", []) or []:
        name, items = _get(g, "name", ""), _get(g, "items", []) or []
        for it in items:
            reason = check_skill_item(it, corpus)
            if reason:
                out.append(Ungrounded(it, f"skills:{name}", reason))
    return out


def enforce(resume, corpus: FactCorpus, fallback_headline: str = "", fallback_summary: str = "") -> list[Ungrounded]:
    """Make `resume` (a TailoredResume) grounded in place: drop ungrounded skill items and empty groups, and replace
    an ungrounded headline/summary with the fallback (the variant's own wording). Returns what was dropped."""
    problems = check_resume(resume, corpus)
    for u in problems:
        log.warning("ungrounded resume content dropped: %s", u)
    bad_items = {(u.where, u.item) for u in problems if u.where.startswith("skills:")}
    if bad_items:
        for g in resume.skills:
            g.items = [i for i in g.items if (f"skills:{g.name}", i) not in bad_items]
        resume.skills = [g for g in resume.skills if g.items]
    if fallback_headline and any(u.where == "headline" for u in problems):
        resume.headline = fallback_headline
    if fallback_summary and any(u.where == "summary" for u in problems):
        resume.summary = fallback_summary
    return problems
