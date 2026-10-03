"""Build the general (non-tailored) resume that gets uploaded to job portals.

Reads profile/master_resume.yaml (copy profile/master_resume.example.yaml to start) and writes
output/resumes/<resume_file>.{pdf,docx,html}, where resume_file comes from profile/profile.yaml.

Run:  python profile/build_master_resume.py
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from naukri_bot.config import load_profile  # noqa: E402
from naukri_bot.llm import TailoredResume  # noqa: E402
from naukri_bot.resume_builder import html_to_pdf, render_html, write_docx  # noqa: E402

src = ROOT / "profile" / "master_resume.yaml"
if not src.exists():
    sys.exit(f"{src} not found. Copy profile/master_resume.example.yaml to it and fill it in.")
resume = TailoredResume(**yaml.safe_load(src.read_text()))
stem = Path(load_profile().get("resume_file") or f"{resume.name.replace(' ', '_')}_Resume.pdf").stem

out = ROOT / "output" / "resumes"
out.mkdir(parents=True, exist_ok=True)
html = render_html(resume)
(out / f"{stem}.html").write_text(html)
html_to_pdf(html, out / f"{stem}.pdf")
write_docx(resume, out / f"{stem}.docx")
print("written to", out / stem, "{.pdf,.docx,.html}")
