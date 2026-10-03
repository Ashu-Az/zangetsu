from naukri_bot.llm import TailoredResume, SkillGroup, ExperienceItem, ProjectItem, EducationItem
from naukri_bot.models import JobPosting
from naukri_bot.resume_builder import build_resume_files, render_markdown, resume_basename


def sample_resume() -> TailoredResume:
    return TailoredResume(
        name="Rohan Mehta", headline="Data Scientist · GenAI & LLM systems",
        contact_line="Bengaluru · a@b.com · +91-99 · github.com/x",
        summary="Three years building ML systems. Reproduced S-SPPO. Shipped a RAG assistant.",
        skills=[SkillGroup(name="ML", items=["PyTorch", "scikit-learn"]), SkillGroup(name="LLM", items=["RAG", "LangChain"])],
        experience=[ExperienceItem(title="Data Scientist", company="Example Corp", location="Bengaluru", dates="2023 – Present",
                                   bullets=["Built churn model lifting precision 0.41→0.63.", "Shipped RAG assistant; -18% AHT."])],
        projects=[ProjectItem(name="S-SPPO", description="PyTorch reproduction", link="https://github.com/your-handle/s-sppo")],
        education=[EducationItem(degree="B.Tech CS", institution="Example University", dates="2018 – 2022", details="CGPA 8.4")],
        certifications=["AWS CCP"], keywords_used=["GenAI", "RAG"], fit_score=78, fit_notes="Strong on LLM; light on Spark.",
    )


def test_render_markdown_and_files(tmp_path):
    r = sample_resume()
    md = render_markdown(r)
    assert md.startswith("# Rohan Mehta") and "## Experience" in md and "0.41→0.63" in md
    job = JobPosting(job_id="190826038477", url="https://x", title="Data Scientist | Python | GenAI", company="Infosys Ltd.")
    assert resume_basename(job).endswith("_Infosys-Ltd_Data-Scientist-Python-GenAI_190826038477")
    files = build_resume_files(r, job, out_dir=tmp_path)
    for key in ("md", "html", "pdf", "docx"):
        assert key in files, f"missing {key}: {files}"
        assert (tmp_path / files[key].split("/")[-1]).stat().st_size > 500
    with open(files["pdf"], "rb") as f:
        assert f.read(5) == b"%PDF-"
