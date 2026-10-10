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


def _metrics(text: str):
    """Bold the numbers recruiters scan for (400 ms, 85%, 10,000+, 30 s), escaping everything else."""
    from markupsafe import Markup, escape
    # standalone figures only: never digits inside names (ADA-002, BM25, Qwen2.5), HTML entities or years
    pat = r"(?<![A-Za-z0-9&#.\-])(\d[\d,.]*\+?(?:\s?(?:%|ms|s)\b|%)?)(?![A-Za-z0-9])"
    def bold(m):
        v = m.group(1)
        return v if re.fullmatch(r"(19|20)\d\d", v) or re.fullmatch(r"\d", v) else f"<b>{v}</b>"
    return Markup(re.sub(pat, bold, str(escape(text))))


_env.filters["metrics"] = _metrics


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
                 margin={"top": "10mm", "bottom": "10mm", "left": "12mm", "right": "12mm"})
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


def compact_pdf(r, path: Path) -> Path:
    """A small, email-friendly PDF of the same resume: built-in Helvetica (no embedded fonts), A4, compressed.
    Typically 5-10 KB instead of ~60 KB for the Chromium render, with identical content."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer
    from xml.sax.saxutils import escape as esc

    navy = colors.HexColor("#1f3a5f")
    base = ParagraphStyle("base", fontName="Helvetica", fontSize=9.2, leading=11.6, alignment=TA_LEFT)
    name = ParagraphStyle("name", parent=base, fontName="Helvetica-Bold", fontSize=17, leading=20, textColor=navy)
    head = ParagraphStyle("head", parent=base, fontName="Helvetica-Bold", fontSize=10.2, leading=13, textColor=navy)
    small = ParagraphStyle("small", parent=base, fontSize=8.6, leading=10.6, textColor=colors.HexColor("#444444"))
    sect = ParagraphStyle("sect", parent=base, fontName="Helvetica-Bold", fontSize=9.6, leading=12, textColor=navy,
                          spaceBefore=5, spaceAfter=1)
    bullet = ParagraphStyle("bullet", parent=base, leftIndent=0)

    def section(title):
        return [Paragraph(title.upper(), sect), HRFlowable(width="100%", thickness=0.6, color=navy, spaceAfter=2)]

    def bullets(items):
        return ListFlowable([ListItem(Paragraph(esc(b), bullet), leftIndent=9, value="•") for b in items],
                            bulletType="bullet", start="•", leftIndent=9, bulletFontSize=8)

    story = [Paragraph(esc(r.name), name), Paragraph(esc(r.headline), head), Paragraph(esc(r.contact_line), small),
             Spacer(1, 3)]
    story += section("Professional Summary") + [Paragraph(esc(r.summary), base)]
    story += section("Technical Skills")
    for g in r.skills:
        story.append(Paragraph(f"<b>{esc(g.name)}:</b> {esc(', '.join(g.items))}", base))
    story += section("Professional Experience")
    for e in r.experience:
        right = f"  <font color='#555555'>{esc(e.dates)}</font>" if getattr(e, "dates", "") else ""
        story.append(Paragraph(f"<b>{esc(e.title)}</b> | {esc(e.company)}"
                               + (f", {esc(e.location)}" if getattr(e, "location", "") else "") + right, base))
        story.append(bullets(e.bullets))
        story.append(Spacer(1, 2))
    if r.projects:
        story += section("Projects")
        story.append(bullets([f"{p.name}: {p.description}" + (f" ({p.link})" if getattr(p, "link", "") else "")
                              for p in r.projects]))
    if r.education:
        story += section("Education")
        for ed in r.education:
            parts = [getattr(ed, "degree", ""), getattr(ed, "institution", "") or getattr(ed, "school", "")]
            story.append(Paragraph(f"<b>{esc(' | '.join(x for x in parts if x))}</b>"
                                   + (f"  <font color='#555555'>{esc(ed.dates)}</font>" if getattr(ed, "dates", "") else ""), base))
    if r.certifications:
        story += section("Certifications & Achievements") + [bullets(r.certifications)]
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=13 * mm, rightMargin=13 * mm, topMargin=11 * mm,
                            bottomMargin=11 * mm, title=f"{r.name} - Resume", author=r.name, pageCompression=1)
    doc.build(story)
    return path
