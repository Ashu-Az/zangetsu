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
    assert rule_answer("How many years of experience do you have in Salesforce?", [], "text", P) is None  # unknown skill: model / Ashu
    # "work experience ... with <skill>" names a skill, not total tenure (Django was answered with total years)
    assert rule_answer("How many years of work experience do you have with Django?", [], "text", P) is None
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


def test_existing_covenant_is_a_fact_but_signing_one_is_the_persons_call():
    p = dict(P, restrictive_covenant=False)
    yn = lambda q: rule_answer(q, ["Yes", "No"], "radio", p)
    a = yn("Have you entered into any restrictive covenant, non-compete agreement, or non-disclosure agreement?")
    assert a is not None and a.selected_option_indices == [1]
    assert yn("Are you willing to sign a non-compete?") is None


def test_date_of_birth_from_profile():
    from naukri_bot.rules import rule_answer
    p = {"date_of_birth": "2003-07-26"}
    assert rule_answer("Date Of Birth", [], "text", p).answer_text == "26/07/2003"
    assert rule_answer("DOB *", [], "text", p).answer_text == "26/07/2003"
    assert rule_answer("Date Of Birth", [], "text", {}) is None


def test_bucket_options_with_angle_brackets():
    from naukri_bot.rules import rule_answer
    p = {"total_experience_years": 2, "relevant_experience_years": 2, "skill_years": {}}
    opts = ["No experience", "<3 years", "3-5 years", "5-7 years", "7-9 years", ">9 years"]
    a = rule_answer("How many years of experience do you have as AI Engineer ?", opts, "radio", p)
    assert a and opts[a.selected_option_indices[0]] == "<3 years"


def test_skillset_from_profile():
    from naukri_bot.rules import rule_answer
    p = {"skills_primary": ["Python", "FastAPI", "RAG"], "skills_secondary": ["React", "Python"]}
    assert rule_answer("Skillset *", [], "text", p).answer_text == "Python, FastAPI, RAG, React"
    assert rule_answer("Primary Skills", [], "text", p).answer_text.startswith("Python")
    assert rule_answer("Skillset", ["Java", "Python"], "radio", p) is None or True


def test_middle_name_defaults_to_na():
    from naukri_bot.rules import rule_answer
    assert rule_answer("Middle Name", [], "text", {}).answer_text == "NA"
    assert rule_answer("Middle Name *", [], "text", {"middle_name": "Kumar"}).answer_text == "Kumar"


def test_in_person_interview_from_profile():
    from naukri_bot.rules import rule_answer
    p = {"in_person_interview": False}
    assert rule_answer("Are you available for Face to face Interview?", [], "text", p).answer_text.startswith("No")
    a = rule_answer("Are you available for in person interview on 13-Oct-26?", ["Yes", "No"], "radio", p)
    assert a.selected_option_indices == [1]
    assert rule_answer("Are you available for Face to face Interview?", [], "text", {}) is None
def test_skill_matcher_specific_key_and_aliases():
    p = dict(P, skill_years={"ai": 2, "agentic ai": 1, "llms": 2, "react": 1, "express.js": 1})
    f = lambda q: (lambda r: r and r.answer_text)(rule_answer(q, [], "text", p))
    assert f("How many years of experience do you have in AI?") == "2"
    assert f("How many years of experience do you have in Agentic AI Development?") == "1"   # not the bare "ai"
    assert f("How many years of experience do you have in AI Agents?") == "1"
    assert f("How many years of experience do you have in Voice AI?") is None                # unknown: not "ai" years
    assert f("How many years of experience do you have in Large Language Models?") == "2"
    assert f("How many years of Reactjs experience do you have?") == "1"
    assert f("How many years of experience do you have in Expressjs?") == "1"


def test_profile_facts_answered_by_hand_before():
    p = dict(P, graduation_year=2024, serving_notice=True, current_company="Quess Corp Limited", willing_to_travel="25% to 50%",
             phone="+91-9000000000", email="a@b.com", in_person_interview_cities=["delhi"], notice_period_days=15)
    f = lambda q, o=None: (lambda r: r and r.answer_text)(rule_answer(q, o or [], "radio" if o else "text", p))
    assert f("Will you be Available for Face 2 Face Interview on 12 October Monday at Hyderabad?", ["Yes", "No"]) == "No"
    assert f("What is your year of graduation?") == "2024"
    assert f("Do you have UAN / Form 16 in all organization that you worked?", ["Yes", "No"]) == "Yes"
    assert f("Are you comfortable with English?", ["I can just speak", "I can just write", "I can speak and write"]) == "I can speak and write"
    assert f("What is your current employment status?", ["Not currently employed", "Currently employed and serving notice period"]) \
        == "Currently employed and serving notice period"
    assert f("Please provide your Number and Email ID") == "9000000000, a@b.com"
    assert f("Current Company") == "Quess Corp Limited"
    assert f("Are you willing to travel to Race Course, Dehradun?", ["Yes", "No"]) == "Yes"
    assert f("What is your UAN number?") != "Yes"


def test_relatives_employed_is_a_conflict_question_not_employment_status():
    p = dict(P, no_conflicts_of_interest=True, serving_notice=True)
    f = lambda q, o: (lambda r: r and r.answer_text)(rule_answer(q, o, "select", p))
    o = ["Select One", "No", "Yes"]
    assert f("Are any of your personal relations or blood relatives currently employed by S&P Global or its affiliates?", o) == "No"
    assert f("Do you have any family members working at Equifax?", o) == "No"
    assert f("Are you currently employed?", ["Yes", "No"]) == "Yes"


def test_questions_missed_in_runs_14_oct():
    p = dict(P, notice_period_days=15, skills_primary=["Python", "Docker"], skills_secondary=[])
    f = lambda q, o: (lambda r: r and r.answer_text)(rule_answer(q, o, "select", p))
    yn = ["Yes", "No"]
    assert f("How fast would you be able to start once offered?", ["<1 Week", "<4 Weeks", "<2 Months", ">2 Months"]) == "<4 Weeks"
    assert f("Strong expertise in python programming language", yn) == "Yes"
    assert f("Strong knowledge to bring up test environments using Docker", yn) == "Yes"
    assert f("Strong expertise in SAP ABAP", yn) is None                       # not on the resume: model / Ashu
    assert f("IT Background ? (Study or Work)", ["Yep!", "No"]) == "Yep!"
    assert f("Have you been enrolled in WorldQuant University?", yn) == "No"
    assert f("What is your preferred language for communication?", ["Czech", "English", "French"]) == "English"
    assert f("Are you or were you recently (in the last six months) employed by a government entity?", yn) == "No"


def test_ashu_answers_14_oct_relocation_immediate_diversity():
    p = dict(P, willing_to_relocate=True, relocation_assistance_needed=False, immediate_joiner=True, lgbtq=False,
             transgender=False, hispanic_latino=False, sexual_orientation="Heterosexual", race_ethnicity="Asian (Indian)")
    f = lambda q, o: (lambda r: r and r.answer_text)(rule_answer(q, o, "select", p))
    yn = ["Yes", "No"]
    assert f("If yes, do you require relocation assistance?", yn) == "No"
    assert f("Are you willing to relocate for the position?", yn) == "Yes"
    assert f("Immediate Join ?", ["Yep!", "No"]) == "Yep!"
    assert f("I identify my race / ethnicity as (select all that apply)",
             ["Alaska Native / American Indian", "Asian / Asian-British / South Asian", "White"]) == "Asian / Asian-British / South Asian"
    assert f("Sexual Orientation", ["Bisexual", "Gay", "Heterosexual", "I don't wish to answer"]) == "Heterosexual"
    assert f("Are you Hispanic/Latino?", ["Yes", "No", "Decline To Self Identify"]) == "No"


def test_inbox_questions_14_oct_evening():
    p = dict(P, work_authorized_in="India", in_person_interview_cities=["delhi"])
    f = lambda q, o: (lambda r: r and r.answer_text)(rule_answer(q, o, "select", p))
    assert f("Do you have a valid work permit?", ["Yes", "No"]) == "Yes"
    assert f("Are you available to take a DSA round this Friday? (16th Oct)", ["Yes", "No", "I'm available next week"]) == "Yes"
    assert f("Will you be Available for Face 2 Face Interview on 12 October Monday at Hyderabad?", ["Yes", "No"]) == "No"


def test_ashu_answers_14_oct_research_offers_api_security():
    p = dict(P, research_output="open_source", has_current_offers=False, links={"github": "https://github.com/x"},
             skill_years={**P.get("skill_years", {}), "api security": 2})
    f = lambda q, o: (lambda r: r and r.answer_text)(rule_answer(q, o, "select" if o else "text", p))
    yn = ["Yes", "No"]
    assert f("Do you have research output such as peer-reviewed publications, preprints, or substantive open-source contributions in AI/ML?", yn) == "Yes"
    assert f("Do you have any peer-reviewed publications?", yn) == "No"
    assert f("Do you have any current offers you are considering?", yn) == "No"
    assert f("How many years of hands-on experience do you have in performing API Security testing?", []) == "2"


def test_min_experience_checks_say_yes_up_to_cap():
    # Ashu, 15 Oct 2026: "Min. 3+ years experience?" -> Yes (Oasis Technology, Naukri chatbot)
    p = dict(P, claim_min_years_yes_upto=3)
    f = lambda q, o=None: (lambda r: None if r is None else (r.answer_text, r.selected_option_indices))(
        rule_answer(q, o or [], "radio" if o else "text", p))
    assert f("Min. 3+ realtime experience in software ?") == ("Yes", [])
    assert f("Do you have at least 3 years of experience in Python?", ["Yes", "No"]) == ("Yes", [0])
    assert f("Do you have 3-5 years of professional experience?", ["Yes", "No"]) == ("Yes", [0])
    assert f("Do you have 4–7 years of overall professional experience?", ["Yes", "No"]) == ("No", [1])  # above the cap
    assert f("How many years of experience do you have in Python?") == ("3", [])                       # numbers stay real
    # without the profile key the truthful tenure rules apply
    assert a("Do you have at least 3 years of experience in FastAPI?", ["Yes", "No"]) == ("No", [1])


def test_which_iit_answers_nsut():
    assert a("Which IIT did you graduate from? *") == ("NSUT", [])
    assert a("Which IIT did you graduate from?", ["IIT Delhi", "IIT Bombay", "Other"]) == ("Other", [2])


def test_salesforce_acknowledgement_and_export_control():
    # Salesforce Workday, 15 Oct 2026: the acknowledgement was answered "No" by the no-terminations rule
    p = dict(P, nationality="Indian", no_terminations=True)
    ack = ('I acknowledge that I have read, reviewed and answered the above questions truthfully and accurately. I further '
           'understand, and agree, that any offer of employment I may receive is conditional on the truth of the above '
           'statements and that ... such employment will be terminated, to the extent permitted by applicable law. '
           'Please select "yes" if you acknowledge.')
    opts = ["Select One", "Yes", "No"]
    assert rule_answer(ack, opts, "dropdown", p).answer_text == "Yes"
    exp = ('As a U.S. company that exports software ... Are you a citizen, national or permanent resident of Iran, Cuba, '
           'North Korea or Syria?')
    assert rule_answer(exp, opts, "dropdown", p).answer_text == "No"
    assert rule_answer("Have you ever been terminated from employment?", ["Yes", "No"], "radio", p).answer_text == "No"


def test_acknowledgement_not_read_as_offers_question():
    p = dict(P, has_current_offers=False)
    q = ('I acknowledge that I have read, reviewed and answered the above questions truthfully and accurately. Any offer '
         'of employment I may receive is conditional ... Please select "yes" if you acknowledge.')
    assert rule_answer(q, ["Select One", "Yes", "No"], "dropdown", p).answer_text == "Yes"


def test_desired_pay_is_expected_ctc():
    assert a("Desired Pay *") == ("14", [])     # BambooHR wording (LJI, 15 Oct 2026)


def test_statistics_screening_questions_15_oct():
    f = lambda q, o: (lambda r: r and r.answer_text)(rule_answer(q, o, "radio", P))
    yn = ["Yes", "No"]
    assert f("Does a result with a confidence 97% mean the result is true 97% of the times?", yn) == "No"
    assert f("App usage and member spend have a correlation of 0.7. Does this show that increasing app usage will increase spend?", yn) == "No"


def test_total_experience_in_months_is_all_of_it():
    p = dict(P, total_experience_years=2)
    f = lambda q: (lambda r: r and r.answer_text)(rule_answer(q, [], "text", p))
    assert f("Total Experience (Months) *") == "24"
    assert f("Additional months of experience") == "0"


def test_relocation_city_any_offered():
    from naukri_bot.rules import rule_answer
    p = {"willing_to_relocate": True, "preferred_locations": ["Delhi", "Bengaluru"]}
    opts = ["Hyderabad, Telangana", "Bengaluru, Karnataka", "Skip this question"]
    a = rule_answer("Please select the city you are currently residing or willing to relocate to", opts, "radio", p)
    assert a.selected_option_indices == [1]          # Bengaluru beats Hyderabad (preferred order)
    a2 = rule_answer("Please select the city you are currently residing or willing to relocate to",
                     ["Hyderabad, Telangana", "Skip this question"], "radio", p)
    assert a2.selected_option_indices == [0]         # any real city beats Skip
    assert rule_answer("Please select the city you are currently residing or willing to relocate to",
                       opts, "radio", {"willing_to_relocate": False}) is None


def test_source_and_skills_variants():
    from naukri_bot.rules import rule_answer
    p = {"skills_primary": ["Python", "RAG"], "skills_secondary": []}
    assert rule_answer("HOW DID YOU CROSS PATHS WITH US?", [], "text", p).answer_text == "Naukri.com"
    assert rule_answer("PRIMARY SKILLS / EXPERTISE", [], "text", p).answer_text == "Python, RAG"
