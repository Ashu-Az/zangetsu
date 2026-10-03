from conftest import FIXTURES

from naukri_bot.search import parse_cards, build_search_urls
from naukri_bot.job_page import parse_job_page
from naukri_bot.config import Settings


def test_parse_search_cards():
    cards = parse_cards((FIXTURES / "search_page.html").read_text())
    assert len(cards) >= 15
    c = cards[0]
    assert c.job_id.isdigit() and len(c.job_id) >= 9
    assert c.title and c.company and c.url.startswith("https://www.naukri.com/job-listings-")
    assert c.experience and c.location


def test_parse_job_page():
    url = "https://www.naukri.com/job-listings-data-scientist-python-genai-llm-rag-infosys-hyderabad-chennai-bengaluru-2-to-5-years-190826038477"
    job = parse_job_page((FIXTURES / "job_page.html").read_text(), url)
    assert job.job_id == "190826038477"
    assert "Data Scientist" in job.title
    assert job.company.startswith("Infosys")
    assert "2 - 5 years" in job.experience
    assert "Hyderabad" in job.location
    assert len(job.description) > 500
    assert "generative ai" in [s.lower() for s in job.skills]
    assert job.industry == "IT Services & Consulting"
    assert job.employment_type.startswith("Full Time")
    assert "B.Tech" in job.education
    text = job.to_prompt_text()
    assert "Key skills:" in text and "Job description:" in text


def test_build_search_urls(monkeypatch):
    s = Settings(keywords=["data scientist", "ML engineer"], location="bangalore", pages_per_keyword=2, experience_years="3", max_job_age_days=0, min_salary_lpa=0)
    urls = build_search_urls(s)
    assert urls[0] == "https://www.naukri.com/data-scientist-jobs-in-bangalore?experience=3"
    assert urls[1] == "https://www.naukri.com/data-scientist-jobs-in-bangalore-2?experience=3"
    assert urls[2].startswith("https://www.naukri.com/ml-engineer-jobs-in-bangalore")
    s2 = Settings(keywords=["data scientist"], location="", pages_per_keyword=1, experience_years="", max_job_age_days=0, min_salary_lpa=0)
    assert build_search_urls(s2) == ["https://www.naukri.com/data-scientist-jobs"]


def test_company_and_experience_are_clean():
    from naukri_bot.job_page import _clean_company
    assert _clean_company("Infosys 3.5 52.7K Reviews") == "Infosys"
    assert _clean_company("VOIS 3.8 1.6K Reviews") == "VOIS"
    assert _clean_company("Acme Pvt Ltd") == "Acme Pvt Ltd"
    job = parse_job_page((FIXTURES / "job_page.html").read_text(), "https://x-190826038477")
    assert job.company == "Infosys"
    assert job.experience == "2 - 5 years"
    assert not job.description.lower().startswith("job description")
