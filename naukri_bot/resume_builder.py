"""Turn a TailoredResume into Markdown, HTML, PDF (via Chromium) and DOCX files."""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import OUTPUT_DIR, TEMPLATE_DIR
from .llm import TailoredResume
from .models import JobPosting

log = logging.getLogger(__name__)

_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]))


def safe_slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return s[:max_len].rstrip("-") or "job"


def resume_basename(job: JobPosting) -> str:
    return f"{date.today():%Y-%m-%d}_{safe_slug(job.company, 30)}_{safe_slug(job.title, 40)}_{job.job_id}"


def render_markdown(r: TailoredResume) -> str:
    out = [f"# {r.name}", r.headline, "", r.contact_line, "", "## Summary", r.summary, ""]
    if r.skills:
        out.append("## Skills")
        out += [f"- **{g.name}:** {', '.join(g.items)}" for g in r.skills]
        out.append("")
    if r.experience:
        out.append("## Experience")
        for e in r.experience:
            loc = f", {e.location}" if e.location else ""
            out.append(f"### {e.title} — {e.company}{loc} ({e.dates})")
            out += [f"- {b}" for b in e.bullets]
            out.append("")
    if r.projects:
        out.append("## Projects")
        for p in r.projects:
            link = f" {p.link}" if p.link else ""
            out.append(f"- **{p.name}** — {p.description}{link}")
        out.append("")
    if r.education:
        out.append("## Education")
        for ed in r.education:
            det = f" — {ed.details}" if ed.details else ""
            out.append(f"- {ed.degree}, {ed.institution} ({ed.dates}){det}")
        out.append("")
    if r.certifications:
        out.append("## Certifications")
        out += [f"- {c}" for c in r.certifications]
    return "\n".join(out).strip() + "\n"


def render_html(r: TailoredResume) -> str:
    return _env.get_template("resume.html").render(r=r)


def write_docx(r: TailoredResume, path: Path) -> None:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    doc.add_heading(r.name, level=0)
    doc.add_paragraph(r.headline).runs[0].bold = True
    doc.add_paragraph(r.contact_line)
    doc.add_heading("Summary", level=1)
    doc.add_paragraph(r.summary)
    if r.skills:
        doc.add_heading("Skills", level=1)
        for g in r.skills:
            p = doc.add_paragraph()
            p.add_run(f"{g.name}: ").bold = True
            p.add_run(", ".join(g.items))
    if r.experience:
        doc.add_heading("Experience", level=1)
        for e in r.experience:
            loc = f", {e.location}" if e.location else ""
            p = doc.add_paragraph()
            p.add_run(f"{e.title} — {e.company}{loc}").bold = True
            p.add_run(f"   {e.dates}")
            for b in e.bullets:
                doc.add_paragraph(b, style="List Bullet")
    if r.projects:
        doc.add_heading("Projects", level=1)
        for pj in r.projects:
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(f"{pj.name} — ").bold = True
            p.add_run(pj.description + (f" {pj.link}" if pj.link else ""))
    if r.education:
        doc.add_heading("Education", level=1)
        for ed in r.education:
            det = f" — {ed.details}" if ed.details else ""
            doc.add_paragraph(f"{ed.degree}, {ed.institution} ({ed.dates}){det}")
    if r.certifications:
        doc.add_heading("Certifications", level=1)
        for c in r.certifications:
            doc.add_paragraph(c, style="List Bullet")
    doc.save(str(path))


def html_to_pdf(html: str, path: Path, playwright=None) -> None:
    """Render with headless Chromium. Reuses the running Playwright instance if given."""
    own = playwright is None
    if own:
        from playwright.sync_api import sync_playwright
        playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=str(path), format="A4", print_background=True,
                 margin={"top": "14mm", "bottom": "14mm", "left": "15mm", "right": "15mm"})
        browser.close()
    finally:
        if own:
            playwright.stop()


def build_resume_files(r: TailoredResume, job: JobPosting, out_dir: Path | None = None, playwright=None) -> dict[str, str]:
    out_dir = out_dir or OUTPUT_DIR / "resumes"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / resume_basename(job)
    files: dict[str, str] = {}

    md_path = base.with_suffix(".md")
    md_path.write_text(render_markdown(r), encoding="utf-8")
    files["md"] = str(md_path)

    html = render_html(r)
    html_path = base.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")
    files["html"] = str(html_path)

    try:
        pdf_path = base.with_suffix(".pdf")
        html_to_pdf(html, pdf_path, playwright=playwright)
        files["pdf"] = str(pdf_path)
    except Exception as e:  # PDF is best-effort; the HTML/MD always exist
        log.warning("PDF render failed: %s", e)

    try:
        docx_path = base.with_suffix(".docx")
        write_docx(r, docx_path)
        files["docx"] = str(docx_path)
    except Exception as e:
        log.warning("DOCX render failed: %s", e)

    log.info("Resume written: %s", files.get("pdf") or files["md"])
    return files
