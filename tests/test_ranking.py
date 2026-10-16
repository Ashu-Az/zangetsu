from conftest import FIXTURES

from naukri_bot.config import Settings
from naukri_bot.job_page import parse_count, parse_job_page
from naukri_bot.models import ApplyMode, JobPosting
from naukri_bot.ranking import SurveyCache, eligibility, sort_key, tier_of, title_included


def test_parse_count():
    assert parse_count("Applicants: 37") == 37
    assert parse_count("Applicants: 50+") == 51
    assert parse_count("Applicants: 100+") == 101
    assert parse_count("Applicants: Less than 10") == 9
    assert parse_count("Applicants: 1,234") == 1234
    assert parse_count("Applicants: 1.2K") == 1200
    assert parse_count("Openings: 5") == 5
    assert parse_count("Applicants:") is None


def test_fixture_has_applicants():
    job = parse_job_page((FIXTURES / "job_page.html").read_text(), "https://x-190826038477")
    assert isinstance(job.applicants, int) and job.applicants >= 0
    assert isinstance(job.openings, int)


def test_experience_fits():
    assert JobPosting(job_id="1", url="u", experience="2 - 5 years").experience_fits(2)
    assert JobPosting(job_id="1", url="u", experience="0 - 2 years").experience_fits(2)
    assert not JobPosting(job_id="1", url="u", experience="3 - 8 years").experience_fits(2)
    assert not JobPosting(job_id="1", url="u", experience="0 - 1 years").experience_fits(2)
    assert JobPosting(job_id="1", url="u", experience="").experience_fits(2)
    assert JobPosting(job_id="1", url="u", experience="5 - 9 years").experience_fits(None)


def test_tiers_and_sort_order():
    s = Settings(applicant_tiers=[50, 100], max_applicants=100)
    assert tier_of(0, s.applicant_tiers) == 0 and tier_of(49, s.applicant_tiers) == 0
    assert tier_of(50, s.applicant_tiers) == 1 and tier_of(99, s.applicant_tiers) == 1
    assert tier_of(100, s.applicant_tiers) == 2
    assert tier_of(None, s.applicant_tiers) == 3
    jobs = [JobPosting(job_id=str(i), url="u", applicants=a) for i, a in enumerate([80, None, 12, 50, 3, 99])]
    ordered = [j.applicants for j in sorted(jobs, key=lambda j: sort_key(j, s))]
    assert ordered == [3, 12, 50, 80, 99, None]


def test_eligibility_rules():
    s = Settings(experience_years="2", max_applicants=100, experience_slack=0)
    profile = {"include_title_keywords": ["data scientist", "full stack"], "exclude_title_keywords": ["intern", "lead"]}
    ok = JobPosting(job_id="1", url="u", title="Data Scientist", experience="1 - 4 years", applicants=40)
    assert eligibility(ok, ApplyMode.NAUKRI, s, profile) is None
    assert eligibility(ok, ApplyMode.ALREADY_APPLIED, s, profile) == "already_applied"
    assert eligibility(JobPosting(job_id="2", url="u", title="Data Scientist Intern", experience="0 - 2", applicants=1), ApplyMode.NAUKRI, s, profile) == "excluded_title"
    assert eligibility(JobPosting(job_id="3", url="u", title="Java Developer", experience="1 - 4", applicants=1), ApplyMode.NAUKRI, s, profile) == "title_not_in_include_list"
    assert eligibility(JobPosting(job_id="4", url="u", title="Full Stack Developer", experience="5 - 9 years", applicants=1), ApplyMode.NAUKRI, s, profile).startswith("experience_mismatch")
    assert eligibility(JobPosting(job_id="5", url="u", title="Full Stack Developer", experience="1 - 3 years", applicants=101), ApplyMode.COMPANY_SITE, s, profile) == "too_many_applicants:101"
    assert eligibility(JobPosting(job_id="6", url="u", title="Full Stack Developer", experience="1 - 3 years", applicants=None), ApplyMode.COMPANY_SITE, s, profile) is None
    assert title_included("anything", {})


def test_survey_cache_roundtrip_and_ttl(tmp_path):
    c = SurveyCache(tmp_path / "s.json", ttl_hours=24)
    job = JobPosting(job_id="9", url="u", title="DS", applicants=7, skills=["a"])
    c.put(job, ApplyMode.COMPANY_SITE)
    got = SurveyCache(tmp_path / "s.json", ttl_hours=24).get("9")
    assert got and got[0].applicants == 7 and got[1] == ApplyMode.COMPANY_SITE
    assert SurveyCache(tmp_path / "s.json", ttl_hours=0).get("9") is None


def test_freshness_parsing_and_filter():
    from naukri_bot.ranking import parse_age_days, too_old
    assert parse_age_days("Posted: Just now") == 0
    assert parse_age_days("Few Hours Ago") == 0
    assert parse_age_days("Posted: 5 hours ago") == 0
    assert parse_age_days("Today") == 0
    assert parse_age_days("1 Day Ago") == 1
    assert parse_age_days("Posted: 4 days ago") == 4
    assert parse_age_days("Posted: 1 week ago") == 7
    assert parse_age_days("30+ Days Ago") == 31
    assert parse_age_days("") is None
    s = Settings(max_job_age_days=1)
    assert not too_old("Few Hours Ago", s) and not too_old("1 Day Ago", s) and not too_old("", s)
    assert too_old("2 Days Ago", s) and too_old("Posted: 1 week ago", s)
    job_old = JobPosting(job_id="1", url="u", title="Data Scientist", experience="1 - 4 years", applicants=5, posted="Posted: 3 days ago")
    assert eligibility(job_old, ApplyMode.NAUKRI, Settings(experience_years="2", max_job_age_days=1, min_salary_lpa=0), {}).startswith("too_old")
    urls = __import__("naukri_bot.search", fromlist=["build_search_urls"]).build_search_urls(Settings(keywords=["ai engineer"], location="", pages_per_keyword=1, experience_years="2", max_job_age_days=1, min_salary_lpa=0))
    assert urls == ["https://www.naukri.com/ai-engineer-jobs?jobAge=1"]


def test_salary_filter():
    from naukri_bot.ranking import parse_salary_lpa, salary_too_low
    from naukri_bot.search import build_search_urls, ctc_filter_params
    assert parse_salary_lpa("8-12 Lacs P.A.") == (8, 12)
    assert parse_salary_lpa("3.5-6 Lacs PA") == (3.5, 6)
    assert parse_salary_lpa("15 Lacs P.A.") == (15, 15)
    assert parse_salary_lpa("50,000-75,000 P.A.") == (0.5, 0.75)
    assert parse_salary_lpa("1-1.5 Cr P.A.") == (100, 150)
    assert parse_salary_lpa("Not Disclosed") is None and parse_salary_lpa("") is None
    s = Settings(min_salary_lpa=10, skip_undisclosed_salary=False)
    assert salary_too_low("4-8 Lacs P.A.", s).startswith("salary_below_10lpa")
    assert salary_too_low("8-12 Lacs P.A.", s) is None          # top of range reaches the floor
    assert salary_too_low("12-18 Lacs P.A.", s) is None
    assert salary_too_low("5 - 13 LPA", Settings(min_salary_lpa=8)) is None   # band can reach the floor
    assert salary_too_low("3-6 Lacs P.A.", Settings(min_salary_lpa=8)).startswith("salary_below_8lpa")
    assert salary_too_low("Not Disclosed", s) is None
    assert salary_too_low("Not Disclosed", Settings(min_salary_lpa=10, skip_undisclosed_salary=True)) == "salary_undisclosed"
    assert ctc_filter_params(10) == ["ctcFilter=10to15", "ctcFilter=15to25", "ctcFilter=25to50", "ctcFilter=50to75", "ctcFilter=75to100"]
    job = JobPosting(job_id="1", url="u", title="Data Scientist", experience="1 - 4 years", applicants=5, salary="3-5 Lacs P.A.")
    assert eligibility(job, ApplyMode.NAUKRI, Settings(experience_years="2", min_salary_lpa=10, max_job_age_days=0), {}).startswith("salary_below")
    url = build_search_urls(Settings(keywords=["ai engineer"], location="", pages_per_keyword=1, experience_years="2", max_job_age_days=1, min_salary_lpa=10))[0]
    # salary is filtered per posting, never via Naukri's ctcFilter facet (it breaks keyword matching)
    assert url == "https://www.naukri.com/ai-engineer-jobs?jobAge=1"


def test_direct_apply_cap_is_separate():
    s = Settings(experience_years="2", max_applicants=100, max_applicants_direct=0, max_job_age_days=0, min_salary_lpa=0)
    job = JobPosting(job_id="1", url="u", title="AI Engineer", experience="1 - 4 years", applicants=101)
    assert eligibility(job, ApplyMode.NAUKRI, s, {}) is None
    assert eligibility(job, ApplyMode.COMPANY_SITE, s, {}) == "too_many_applicants:101"
    s2 = Settings(experience_years="2", max_applicants=100, max_applicants_direct=100, max_job_age_days=0, min_salary_lpa=0)
    assert eligibility(job, ApplyMode.NAUKRI, s2, {}) == "too_many_applicants:101"


def test_company_blocklist_and_dedupe():
    from naukri_bot.ranking import company_excluded, dedupe_company_title
    prof = {"exclude_companies": ["Accenture"]}
    assert company_excluded("Accenture Solutions Pvt Ltd", prof) and not company_excluded("Cisco", prof)
    s = Settings(experience_years="2", max_job_age_days=0, min_salary_lpa=0)
    j = JobPosting(job_id="1", url="u", title="AI Engineer", company="Accenture", experience="1 - 4 years", applicants=5)
    assert eligibility(j, ApplyMode.COMPANY_SITE, s, prof) == "excluded_company"
    mk = lambda i, c, t: (JobPosting(job_id=str(i), url="u", title=t, company=c), ApplyMode.NAUKRI)
    pairs = [mk(1, "Infosys", "Python Developer"), mk(2, "Infosys ", "Python  Developer"), mk(3, "Infosys", "AI Engineer"), mk(4, "PwC", "Python Developer")]
    assert [j.job_id for j, _ in dedupe_company_title(pairs)] == ["1", "3", "4"]


def test_ey_gds_staff_is_not_a_staff_level_title():
    from naukri_bot.ranking import title_excluded
    p = {"exclude_title_keywords": ["staff", "senior"]}
    assert not title_excluded("EY GDS-Staff-AppSec-AI-Automation-Engineer", p)
    assert title_excluded("Staff Software Engineer", p)
