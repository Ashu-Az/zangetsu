from naukri_bot.rules import rule_answer

P = {"notice_period_days": 15, "current_ctc_lpa": 7.5, "expected_ctc_lpa": 14, "total_experience_years": 2,
     "relevant_experience_years": 2, "willing_to_relocate": True, "location": "Gurugram",
     "skills_primary": ["Python", "FastAPI", "PyTorch"], "skills_secondary": ["Flask"],
     "skill_years": {"python": 3, "fastapi": 1.5, "machine learning": 3, "aws": 0, "java": 1}}


def a(q, opts=None):
    r = rule_answer(q, opts or [], "radio" if opts else "text", P)
    return None if r is None else (r.answer_text, r.selected_option_indices)


def test_notice_period():
    assert a("What is your notice period?") == ("15", [])
    assert a("What is your notice period?", ["15 Days or less", "1 Month", "2 Months", "3 Months", "More than 3 Months"]) == ("15 Days or less", [0])
    assert a("Notice period", ["Immediate", "0-15 days", "30 days", "60 days"]) == ("0-15 days", [1])


def test_ctc():
    assert a("What is your current CTC in Lacs per annum?") == ("7.5", [])
    assert a("What is your expected CTC in Lacs per annum?") == ("14", [])
    assert a("Expected CTC?", ["5-10 Lacs", "10-15 Lacs", "15-20 Lacs"]) == ("10-15 Lacs", [1])


def test_experience():
    assert a("How many years of experience do you have in Python?") == ("3", [])
    assert a("How many years of experience do you have in FastAPI?") == ("1.5", [])
    assert a("How many years of experience do you have in Salesforce?") == ("0", [])
    assert a("What is your total experience?") == ("2", [])
    assert a("Total years of work experience", ["0-1 years", "1-3 years", "3-5 years"]) == ("1-3 years", [1])
    assert a("Do you have experience in Python?", ["Yes", "No"]) == ("Yes", [0])
    assert a("Do you have experience in AWS?", ["Yes", "No"]) == ("No", [1])
    assert a("Do you have experience in SAP ABAP?", ["Yes", "No"]) == ("No", [1])


def test_relocation_location_and_unknown():
    assert a("Are you willing to relocate to Pune?", ["Yes", "No"]) == ("Yes", [0])
    assert a("What is your current location?") == ("Gurugram", [])
    assert a("Are you comfortable with working from office 5 days a week?", ["Yes", "No"]) == ("Yes", [0])
    assert a("Why do you want to join us?") is None
    assert a("Describe a challenging project you worked on.") is None
    assert a("What is your date of birth?") is None


def test_assume_skill_experience():
    p = dict(P, assume_skill_experience=True, default_skill_years=1)
    f = lambda q, o=None: (lambda r: (r.answer_text, r.selected_option_indices))(rule_answer(q, o or [], "radio" if o else "text", p))
    assert f("Do you have experience in SAP ABAP?", ["Yes", "No"]) == ("Yes", [0])
    assert f("Do you have experience in AWS?", ["Yes", "No"]) == ("Yes", [0])
    assert f("How many years of experience do you have in Salesforce?") == ("1", [])
    assert f("How many years of experience do you have in AWS?") == ("1", [])          # listed with 0 -> default
    assert f("How many years of experience do you have in Python?") == ("3", [])        # known skills keep their real number
    assert f("Do you know Kotlin?", ["Yes", "No"]) == ("Yes", [0])


def test_overall_experience_range_uses_real_tenure():
    p = dict(P, assume_skill_experience=True, default_skill_years=1)
    f = lambda q, o: (lambda r: (r.answer_text, r.selected_option_indices))(rule_answer(q, o, "radio", p))
    assert f("Do you have 4–7 years of overall professional experience in Python/AI/Software?", ["Yes", "No"]) == ("No", [1])
    assert f("Do you have 1–3 years of overall work experience?", ["Yes", "No"]) == ("Yes", [0])
    assert f("Do you have 2+ years of professional experience?", ["Yes", "No"]) == ("Yes", [0])
    assert f("Do you have 1–3 years of hands-on experience in AI/GenAI development?", ["Yes", "No"]) == ("Yes", [0])


def test_work_mode_interview_history_and_availability():
    p = dict(P, assume_skill_experience=True)
    t = lambda q: rule_answer(q, [], "text", p).answer_text
    r = lambda q, o: (lambda x: (x.answer_text, x.selected_option_indices))(rule_answer(q, o, "radio", p))
    assert t("Are you ready work in Hybrid(Pune) mode?") == "Yes"
    assert r("Are you comfortable with night shifts?", ["Yes", "No"]) == ("No", [1])
    assert t("Have you ever appeared for an Interview at any Exl location during the last 6 months?") == "No"
    assert t("Are you available to start immediately?") == "I can join within 15 days (notice period)."
    assert r("Are you available to start immediately?", ["Yes", "No"]) == ("No", [1])   # 15-day notice is not immediate
    assert t("What is your earliest start date?").startswith("Within 15 days")


def test_numeric_text_answers_are_digits_only():
    p = dict(P, assume_skill_experience=True)
    t = lambda q: rule_answer(q, [], "text", p).answer_text
    assert t("What is your notice period (in days)?") == "15"
    assert t("Notice period") == "15"
    assert t("How many days do you need to join?") == "15"
    assert t("Are you available to start immediately?") == "I can join within 15 days (notice period)."


def test_links_lwd_office_and_canned():
    p = dict(P, assume_skill_experience=True, links={"linkedin": "https://www.linkedin.com/in/your-handle", "github": "https://github.com/your-handle"},
             canned_answers={"recent project": "S-SPPO reproduction", "why (do you want|join)": "Because LLM systems"})
    t = lambda q: rule_answer(q, [], "text", p).answer_text
    r = lambda q, o: (lambda x: (x.answer_text, x.selected_option_indices))(rule_answer(q, o, "radio", p))
    assert t("Please enter your LinkedIn profile URL") == "https://www.linkedin.com/in/your-handle"
    assert t("LinkedIn Profile") == "https://www.linkedin.com/in/your-handle"
    assert t("How many days are left in your last working day?") == "15"
    assert r("Are you ready to work from Bengaluru, India office?", ["Yes", "No"]) == ("Yes", [0])
    assert t("Please describe your most recent project involving LLMs") == "S-SPPO reproduction"
    assert t("Why do you want to join us?") == "Because LLM systems"


def test_live_indeed_questions_2026_10_04():
    """Real smartapply questions that got numeric nonsense before (Indeed, 4 Oct 2026)."""
    p = dict(P, assume_skill_experience=True, skill_years={"python": 3}, total_experience_years=2,
             current_ctc_lpa=7.5, expected_ctc_lpa=14, notice_period_days=15)
    a = lambda q: rule_answer(q, [], "text", p).answer_text
    assert a("How many years of Python experience do you have?\xa0*") == "3"
    assert a("What is your current CTC & Notice period?") == "Current CTC: 7.5 LPA; Notice period: 15 days"
    # a "describe" answer must be specific to the skill, so rules leave it to the model (no canned paragraph)
    assert rule_answer("Do you have hands-on experience with Azure Data Lake and Databricks? If yes, please briefly describe", [], "text", p) is None
    assert a("Must have exp in Python,AWS Cloud Services,LangChain") == "Yes."
    assert a("Do you have 5-7 years of total experience?") == "I have 2 years of total professional experience."
    assert a("What is your NP? Mention in days") == "15"
    assert a("If you were referred by an employee, list their name") == "Not referred"


def test_immediate_availability_is_honest_about_notice():
    p = dict(P, notice_period_days=15)
    assert rule_answer("Are you available to start immediately?", [], "text", p).answer_text == "I can join within 15 days (notice period)."
    assert rule_answer("Are you available to start immediately?", ["Yes", "No"], "radio", p).selected_option_indices == [1]
    assert rule_answer("Can you join within 30 days?", ["Yes", "No"], "radio", p).selected_option_indices == [0]
    assert rule_answer("How soon can you join?", [], "text", p).answer_text == "Within 15 days"


def test_sensitive_questions_are_never_auto_answered():
    """Review 9 Oct 2026: these were answered "Yes" by the willingness/consent rules."""
    p = dict(P, default_willingness="Yes", assume_skill_experience=True)
    for q in ["Are you willing to undergo a medical examination?", "Are you comfortable with a drug and alcohol test?",
              "Are you willing to sign a 2 year service agreement?", "I agree to a 2 year bond with a penalty of 2 lakh",
              "Are you okay with an unpaid internship?", "Are you comfortable working 6 days a week?",
              "I declare that I have no medical conditions", "Have you passed through any physical ailment or illness?",
              "Are you able to perform the essential functions with or without reasonable accommodation?",
              "Are you willing to sign a non-compete?", "Is a stipend of 15k okay?"]:
        assert rule_answer(q, ["Yes", "No"], "radio", p) is None, q


def test_skill_tenure_is_judged_against_real_years():
    p = dict(P, skill_years={"python": 3, "llm": 2}, assume_skill_experience=True)
    yn = lambda q: ["Yes", "No"][rule_answer(q, ["Yes", "No"], "radio", p).selected_option_indices[0]]
    assert yn("Do you have 5+ years of experience in Python?") == "No"
    assert yn("Do you have at least 2 years of experience with Python?") == "Yes"
    assert yn("Do you have 3-5 years of experience in Python?") == "Yes"
    assert yn("Do you have at least 4 years of experience with LLMs?") == "No"
    assert yn("Do you have experience in Python?") == "Yes"
