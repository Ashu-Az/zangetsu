"""Regression tests for bugs that produced wrong answers, lost jobs or wrong quotas."""
import json
from datetime import datetime, timedelta

from naukri_bot.answers_store import AnswerCache
from naukri_bot.models import JobPosting
from naukri_bot.rules import rule_answer
from naukri_bot.tracker import Tracker

P = {"notice_period_days": 15, "total_experience_years": 2, "skills_primary": ["Python", "Django"],
     "skill_years": {"python": 2, "django": 2, "aws": 0}}


def ans(q, opts):
    r = rule_answer(q, opts, "radio", P)
    return None if r is None else r.answer_text


# ---- answer cache: stored by option label, never by position
def test_cache_follows_label_when_options_are_reordered(tmp_path):
    cache = AnswerCache(tmp_path / "c.json")
    cache.save("Are you willing to relocate to Pune?", ["Yes", "No"], "Yes", [0])
    hit = cache.lookup("Are you willing to relocate to Pune?", ["No", "Yes"])
    assert hit["indices"] == [1] and hit["answer_text"] == "Yes"


def test_cache_ignores_entries_without_labels(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps([{"question": "are you willing to relocate", "options": ["no", "yes"],
                                 "raw_question": "", "raw_options": ["Yes", "No"], "answer_text": "Yes", "indices": [0]}]))
    assert AnswerCache(path).lookup("Are you willing to relocate?", ["No", "Yes"]) is None


def test_cache_never_matches_non_latin_questions(tmp_path):
    cache = AnswerCache(tmp_path / "c.json")
    cache.save("क्या आप स्थानांतरित हो सकते हैं?", ["Yes", "No"], "Yes", [0])      # not stored
    cache.save("Notice period?", ["Yes", "No"], "No", [1])
    assert cache.lookup("क्या आपके पास पासपोर्ट है?", ["Yes", "No"]) is None


# ---- rules: skill questions vs ex-employee questions
def test_worked_with_a_listed_skill_is_yes():
    assert ans("Have you worked with Django?", ["Yes", "No"]) == "Yes"


def test_worked_here_before_is_no():
    assert ans("Have you worked with us before?", ["Yes", "No"]) == "No"
    assert ans("Have you ever worked for this company?", ["Yes", "No"]) == "No"


def test_unlisted_or_zero_year_skill_is_no():
    assert ans("Do you have experience in AWS?", ["Yes", "No"]) == "No"
    assert ans("Do you have experience in Kubernetes?", ["Yes", "No"]) == "No"


def test_no_canned_paragraph_for_describe_questions():
    p = dict(P, assume_skill_experience=True)
    assert rule_answer("Do you have experience with SAP? If yes, describe briefly", [], "text", p) is None


# ---- tracker: dry runs, retries, per-portal quota
def job(i="1"):
    return JobPosting(job_id=i, url=f"https://x/{i}", title="Engineer", company="Acme")


def test_dry_run_records_nothing_as_processed(tmp_path):
    t = Tracker(tmp_path / "o", tmp_path / "s", read_only=True)
    t.record_application(job(), "naukri_apply", "dry_run")
    assert not Tracker(tmp_path / "o", tmp_path / "s").is_seen("1")


def test_failed_job_is_retried_once_after_cooldown(tmp_path):
    t = Tracker(tmp_path / "o", tmp_path / "s")
    t.mark_seen("1", "failed")
    assert t.is_seen("1")                                   # cooling down
    t.seen["1"]["timestamp"] = (datetime.now() - timedelta(hours=7)).isoformat()
    assert not t.is_seen("1")                               # due for its second attempt
    t.mark_seen("1", "failed")
    t.seen["1"]["timestamp"] = (datetime.now() - timedelta(hours=7)).isoformat()
    assert t.is_seen("1") and t.seen["1"]["attempts"] == 2  # out of attempts


def test_applied_is_final(tmp_path):
    t = Tracker(tmp_path / "o", tmp_path / "s")
    t.mark_seen("1", "applied")
    t.seen["1"]["timestamp"] = (datetime.now() - timedelta(days=3)).isoformat()
    assert t.is_seen("1")


def test_daily_count_is_per_portal(tmp_path):
    t = Tracker(tmp_path / "o", tmp_path / "s")
    t.record_application(job("1"), "naukri_apply", "applied")
    t.record_application(job("2"), "questionnaire", "applied_partial")
    t.record_application(job("3"), "linkedin:naukri_apply", "applied")
    t.record_application(job("4"), "naukri_apply", "quota", remember=False)
    assert t.applied_today("naukri") == 2
    assert t.applied_today("linkedin") == 1
    assert t.applied_today("indeed") == 0


def test_seen_file_written_atomically(tmp_path):
    t = Tracker(tmp_path / "o", tmp_path / "s")
    t.mark_seen("1", "applied")
    assert json.loads((tmp_path / "s" / "seen_jobs.json").read_text())["1"]["status"] == "applied"
    assert not list((tmp_path / "s").glob("*.tmp"))


# ---- relevance: salary units and skill overlap (from the first live survey, 4 Oct 2026)
def test_mixed_unit_salary():
    from naukri_bot.ranking import parse_salary_lpa
    assert parse_salary_lpa("50,000-1 Lacs P.A.") == (0.5, 1)
    assert parse_salary_lpa("2-5 Lacs P.A.") == (2, 5)


def test_skill_overlap_gate():
    from naukri_bot.ranking import skill_overlap_too_low
    prof = {"skills_primary": ["Python", "LangChain", "Machine Learning"], "min_skill_overlap": 2}
    j = JobPosting(job_id="1", url="u", skills=["avionics", "c++", "python scripting"])
    assert skill_overlap_too_low(j, prof) == "skill_overlap_1_of_2"
    j.skills = ["langchain", "machine learning", "node.js"]
    assert skill_overlap_too_low(j, prof) is None
    j.skills = []                                   # no key skills listed: do not block
    assert skill_overlap_too_low(j, prof) is None


def test_whole_word_title_excludes():
    from naukri_bot.ranking import title_excluded
    prof = {"exclude_title_words": ["java", "rust"]}
    assert title_excluded("Java - Fullstack Developer", prof)
    assert title_excluded("Rust Backend Engineer", prof)
    assert not title_excluded("JavaScript Developer", prof)
    assert not title_excluded("Trust & Safety ML Engineer", prof)


def test_skill_overlap_falls_back_to_description():
    from naukri_bot.ranking import skill_overlap_too_low
    prof = {"skills_primary": ["Python", "FastAPI", "LangChain"], "min_skill_overlap": 2}
    j = JobPosting(job_id="1", url="u", description="We build services in Go and Rust on Kubernetes.")
    assert skill_overlap_too_low(j, prof) == "skill_overlap_0_of_2"
    j.description = "Python and FastAPI services with LangChain agents."
    assert skill_overlap_too_low(j, prof) is None


# ---- from the first live applications (5 Oct 2026)
def test_last_working_day_when_not_serving_notice():
    p = dict(P, serving_notice=False)
    assert rule_answer("What is ypur Last working day?(if serving notice)", [], "text", p).answer_text == "Not serving notice"
    assert rule_answer("Last working day?", ["Not serving notice", "Within 15 days"], "radio", p).selected_option_indices == [0]


def test_naukri_intro_bubble_is_not_a_question(tmp_path, monkeypatch):
    from tests.test_questionnaire_logic import make
    monkeypatch.setattr("naukri_bot.questionnaire.pause", lambda *a, **k: None)
    steps = [("The recruiter requires more information related to your skills to consider your apply.", "text", []),
             ("What is your notice period?", "radio", ["15 days or less", "1 month"])]
    h, llm, cache = make(tmp_path, steps)
    real_wait = h.wait_for_next_message
    def intro_then_question(timeout_s=8):
        if h.steps and h.steps[0][0].startswith("The recruiter"):
            h.steps.pop(0); h.posted += 1                # the bot posts the real question
        return real_wait(timeout_s)
    h.wait_for_next_message = intro_then_question
    res = h.run()
    assert [a["question"] for a in res.answered] == ["What is your notice period?"]


def test_aws_titles_are_excluded():
    from naukri_bot.ranking import title_excluded
    prof = {"exclude_title_words": ["aws", "terraform", "bedrock"]}
    assert title_excluded("AWS Bedrock Developer-Terraform-Python", prof)
    assert not title_excluded("Python Developer (Django, Flask)", prof)


def test_experience_from_description():
    from naukri_bot.portals.linkedin import experience_from_text as e
    assert e("We need 3-5 years of experience in Python") == "3 - 5 years"
    assert e("4+ years of hands-on Python experience") == "4+ years"
    assert e("Experience: 2-4 years") == "2 - 4 years"
    assert e("Founded 20 years ago. Experience: not required") == ""
    assert e("We have 10 years in business; 1 to 3 years of relevant experience") == "1 - 3 years"


# ---- from the first LinkedIn run (5 Oct 2026)
def test_ctc_in_rupees_when_asked():
    p = dict(P, current_ctc_lpa=7.5, expected_ctc_lpa=13)
    t = lambda q: rule_answer(q, [], "text", p).answer_text
    assert t("Please enter your current ctc in INR") == "750000"
    assert t("Please enter your expected ctc in INR") == "1300000"
    assert t("What is your current monthly salary?") == "62500"
    assert t("What is your current CTC (in lakhs)?") == "7.5"


def test_unknown_experience_fact_is_not_answered_no():
    assert ans("Have you worked in a startup, 0 to 1 product environment?", ["Yes", "No"]) is None
    assert ans("Have you worked directly with GPU-based model training?", ["Yes", "No"]) is None


def test_willingness_and_licence():
    p = dict(P, default_willingness="Yes")
    assert rule_answer("Are you willing to undergo a background check?", ["Yes", "No"], "radio", p).answer_text == "Yes"
    assert rule_answer("Are you comfortable working in a fast-paced environment?", ["Yes", "No"], "radio", p).answer_text == "Yes"
    assert rule_answer("Are you comfortable with night shifts?", ["Yes", "No"], "radio", p).answer_text == "No"
    assert rule_answer("Do you have a valid driver's license?", ["Yes", "No"], "radio", p) is None   # not in profile
    assert rule_answer("Please enter your online portfolio URL", [], "text",
                       dict(p, links={"github": "https://github.com/x"})).answer_text == "https://github.com/x"


def test_us_hours_shifts_are_declined_evening_accepted():
    r = lambda q: rule_answer(q, ["Yes", "No"], "radio", P).answer_text
    assert r("Are you comfortable working in EST shift?") == "No"
    assert r("Are you okay with US shift timings?") == "No"
    assert r("Are you comfortable to work in remote setting for 3pm-12am shift?") == "Yes"


def test_naukri_match_score_filter_and_order():
    from naukri_bot.config import Settings
    from naukri_bot.models import ApplyMode
    from naukri_bot.ranking import eligibility, sort_key
    prof = {"require_match": ["keyskills", "work experience"]}
    s = Settings(); s.min_salary_lpa = 0; s.experience_years = ""
    weak = JobPosting(job_id="1", url="u", title="AI Engineer", applicants=5,
                      match={"early applicant": True, "keyskills": False, "location": True, "work experience": True})
    strong = JobPosting(job_id="2", url="u", title="AI Engineer", applicants=40,
                        match={"early applicant": True, "keyskills": True, "location": True, "work experience": True})
    unknown = JobPosting(job_id="3", url="u", title="AI Engineer", applicants=1)          # no match block shown
    assert eligibility(weak, ApplyMode.NAUKRI, s, prof) == "no_match_tick:keyskills"
    assert eligibility(strong, ApplyMode.NAUKRI, s, prof) is None
    assert eligibility(unknown, ApplyMode.NAUKRI, s, prof) is None
    assert sorted([unknown, strong], key=lambda j: sort_key(j, s))[0] is unknown         # fewest applicants first (Ashu, 22 Oct 2026)
    fresh = JobPosting(job_id="4", url="u", title="AI Engineer", applicants=40, posted="2 hours ago")
    stale = JobPosting(job_id="5", url="u", title="AI Engineer", applicants=40, posted="20 hours ago")
    assert sorted([stale, fresh], key=lambda j: sort_key(j, s))[0] is fresh              # same count: the freshest first


def test_months_nationality_qualification():
    p = dict(P, total_experience_years=2, nationality="Indian", highest_education="B.Tech in Information Technology")
    r = lambda q, o: rule_answer(q, o, "dropdown", p)
    assert r("Please select your total additional months of experience:*", [str(i) for i in range(12)]).answer_text == "0"
    assert rule_answer("Nationality*", [], "text", p).answer_text == "Indian"
    assert r("What is your Highest Qualification?", ["10th", "12th", "B.Tech/B.E.", "M.Tech"]).answer_text == "B.Tech/B.E."


def test_linkedin_hiring_post_filter():
    from naukri_bot.portals.linkedin_posts import parse_post
    prof = {"total_experience_years": 2, "exclude_title_keywords": ["senior", "lead", "intern"]}
    keep = parse_post("We're hiring an AI Engineer (2-4 years of experience) in Bangalore! Share your CV at jobs@acme.ai",
                      "Priya Sharma", "https://www.linkedin.com/in/priya", "2h", prof)
    assert keep and keep["role"] == "AI Engineer" and keep["emails"] == "jobs@acme.ai" and keep["author_is_person"]
    assert parse_post("We're hiring a BI Engineer to join our graduate programme in Johannesburg", "Syspro",
                      "https://www.linkedin.com/company/syspro/", "now", prof) is None          # not India
    assert parse_post("Hiring Senior Machine Learning Engineer, 8+ years of experience, Pune", "X",
                      "https://www.linkedin.com/in/x", "1h", prof) is None                      # senior / too much experience


def test_hiring_post_parser_rejects_live_false_positives():
    from naukri_bot.portals.linkedin_posts import parse_post
    prof = {"total_experience_years": 2, "exclude_title_keywords": ["senior", "lead", "intern", "staff"]}
    person = ("Priya", "https://www.linkedin.com/in/p", "1h")
    assert parse_post("Job Title: Tech Support Intern\nCompany: GAO Group, USA & Canada. We're hiring! Software Engineer "
                      "roles also open. Send CV to hr@gao.com, India", *person, prof) is None          # explicit field wins
    assert parse_post("Procore is hiring Staff GTM AI Engineer in Bengaluru, India", *person, prof) is None
    assert parse_post("Where are the tech jobs in India? Companies are hiring heavily in backend engineering.", *person, prof) is None
    juspay = parse_post("JUSPAY IS HIRING! Role: Software Development Engineer – Backend. Location: Bangalore. 1-3 years experience. Apply here: https://lnkd.in/abc",
                        *person, prof)
    assert juspay and juspay["role"].startswith("Software Development Engineer")


def test_same_role_reposted_under_new_id_is_recognised(tmp_path):
    t = Tracker(tmp_path / "o", tmp_path / "s")
    t.record_application(JobPosting(job_id="180926035177", url="u", title="Generative Ai 2- 5 yrs", company="Infosys"), "naukri_apply", "applied")
    keys = t.recent_role_keys()
    assert Tracker.role_key("Infosys Limited", "Generative AI 2-5 yrs") in keys
    assert Tracker.role_key("Infosys", "Python Developer") not in keys


def test_assist_mode_waits_for_a_human_instead_of_abandoning(tmp_path, monkeypatch):
    from tests.test_questionnaire_logic import make
    monkeypatch.setattr("naukri_bot.questionnaire.pause", lambda *a, **k: None)
    steps = [("Describe a project where you used Kubernetes in production", "text", []),
             ("What is your notice period?", "radio", ["15 days or less", "1 month"])]

    class NoAnswerLLM:            # the model has nothing truthful to say -> skip
        def answer_question(self, *a, **k):
            from naukri_bot.llm import QuestionAnswer
            return QuestionAnswer(answer_text="", selected_option_indices=[], reusable=False, skip=True, confidence=0.1)

    h, _, _ = make(tmp_path, steps, llm=NoAnswerLLM())
    h.assist = True
    asked = []
    def human(question, kind, options, timeout_s=300):
        asked.append(question); h.steps.pop(0); h.posted += 1
        return True
    h.ask_human = human
    res = h.run()
    assert asked == ["Describe a project where you used Kubernetes in production"]
    assert res.completed and [a["source"] for a in res.answered] == ["human", "rules"]


def test_jd_tailoring_reorders_without_inventing():
    from naukri_bot.tailor_rules import tailor, _hits
    jd = "We need SQL, PySpark and machine learning experience; Python and Docker preferred."
    assert _hits("reinforcement-learning environments", jd) == 0          # generic word alone does not count
    assert _hits("Python", jd) == 3 and _hits("Docker", jd) == 3
    r, rep = tailor(jd, "Machine Learning Engineer")
    all_items = {i for g in r.skills for i in g.items}
    from naukri_bot.resume_variants import load_variants
    v = load_variants()["variants"]["ai_ml"]
    assert all_items == {i for g in v["skills"] for i in g["items"]}        # same skills, only reordered
    assert "python" in rep["matched_skills"] and "docker" in rep["matched_skills"]


def test_experience_in_years_is_total_and_per_annum_is_rupees():
    p = dict(P, total_experience_years=2, current_ctc_lpa=7.5, expected_ctc_lpa=14)
    t = lambda q: rule_answer(q, [], "text", p).answer_text
    assert t("Experience in Years") == "2"
    assert t("How many years of experience do you have in Python?") == "2"
    assert t("Expected Salary (Per Annum)") == "1400000"
    assert t("Expected CTC (LPA)") == "14"


def test_company_form_answers_never_pull_profile_words_into_questions():
    from naukri_bot.ats.generic_form import GenericFormApplier
    prof = dict(P, first_name="Ashu", location="Delhi", current_company="Quess Corp Limited", gender="Male",
                links={"github": "https://github.com/x"}, work_authorized_in="India", restrictive_covenant=False)
    g = GenericFormApplier(None, prof, "r.pdf")
    v = lambda l: g.value_for(l)[0]
    assert v("To your knowledge, were you referred to this position by a current employee at our school partner?") == "No"
    assert v("Are you subject to any employment agreements with your current employer that would restrict you?") == "No"
    assert v("Will you require sponsorship to continue working in your current location?") in ("No", None)
    assert v("In what countries do you have the unrestricted right to work?") == "India"
    assert v("What is the source of your right to work where this role is based?") == "Citizen"
    assert v("Location (City)") == "Delhi"            # short field labels still use the profile
    assert v("Preferred Name*") == "Ashu"
