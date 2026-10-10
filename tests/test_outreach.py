from naukri_bot.outreach import Lead, _greeting, clean_title, compose, lookalike_domain
from naukri_bot.portals.linkedin_posts import extract_emails


def test_obfuscated_and_plain_emails():
    assert extract_emails("mail careers [at] zeta [dot] in or hr@acme.com.") == ["careers@zeta.in", "hr@acme.com"]
    assert extract_emails("see linkedin.com/in/someone, call at 5pm") == []


def test_lookalike_domains_are_flagged():
    assert lookalike_domain("payal@fractcal.ai", "Fractal Analytics")          # typo of the company name
    assert not lookalike_domain("careers@eucloid.com", "Eucloid Data Solutions")
    assert not lookalike_domain("hr@nextrive.com", "Tymtix Solutions")           # agency domain: different, fine


def test_titles_are_cleaned():
    assert clean_title("Gen AI _Opportunity at Fractal Analytics", "Fractal Analytics") == "Gen AI"
    assert clean_title("Hiring | AI Engineer | StatusNeo", "StatusNeo") == "AI Engineer"
    assert clean_title("Urgent Requirement AI ML Developer") == "AI ML Developer"


def test_greeting_and_body_use_only_profile_facts():
    lead = Lead(email="careers@acme.com", title="AI Engineer", company="Acme", jd="", url="", source="naukri")
    assert _greeting(lead) == "Hello Acme hiring team,"
    assert _greeting(Lead(email="priya.s@acme.com", title="", company="Acme", jd="", url="", source="x")) == "Hi Priya,"
    profile = {"name": "Rohan Mehta", "total_experience_years": 2, "notice_period_days": 30, "current_title": "AI Engineer",
               "outreach_highlights": ["Built a RAG assistant over 1,000+ documents"], "phone": "+91-XXXXXXXXXX"}
    subject, body = compose(lead, profile, {"matched": ["Python", "FastAPI", "RAG"]})
    assert subject == "Application for AI Engineer - Rohan Mehta (2 yrs, 30-day notice)"
    assert "Python, FastAPI and RAG" in body and "Built a RAG assistant" in body and "30 days" in body
