"""Employer-form filling (generic career-site forms and Workday): the pure-logic parts, no browser.
All profile values here are fictional."""
import pytest

from naukri_bot.ats.generic_form import (LEGAL_CLAIM, GenericFormApplier, company_matches, confirmation_seen,
                                         consent_only, job_country_from)
from naukri_bot.ats.workday import WorkdayApplier

PROFILE = {
    "name": "Test Person", "first_name": "Test", "last_name": "Person", "email": "test@example.com",
    "phone_national": "9000000000", "city": "Sample City", "location": "Sample City", "postal_code": "100001",
    "current_company": "Acme Widgets Pvt Ltd", "current_title": "Software Engineer",
    "current_ctc_lpa": 9.5, "expected_ctc_lpa": 20, "ctc_negotiable": True, "notice_period_days": 30,
    "degree": "B.E.", "major": "Electronics", "school": "Example Institute of Technology (EIT), Sample City",
    "school_aliases": ["EIT", "Example Institute of Technology", "Example Institute"],
    "graduation_year": 2020, "gpa": 8.1, "marks_10th_percent": 91.5, "marks_12th_percent": 88,
    "skill_years": {"python": 3, "typescript": 1}, "relevant_experience_years": 3,
    "work_authorized_in": "India", "links": {"github": "https://github.com/example"},
}


class FakePage:
    def __init__(self, text="", url="https://careers.example.com/jobs/123"):
        self.text, self.url, self.closed = text, url, False

    def evaluate(self, js, *a, **k):
        return self.text

    def inner_text(self, sel):
        return self.text

    def is_closed(self):
        return self.closed

    def close(self):
        self.closed = True


def applier(text="", url="https://careers.example.com/jobs/123", **over):
    return GenericFormApplier(FakePage(text, url), dict(PROFILE, **over), "r.pdf")


# ------------------------------------------------------------------ legal claims vs plain consent
@pytest.mark.parametrize("label", [
    "I confirm that I am legally authorized to work in the United States",
    "I understand that this offer is subject to a satisfactory background check",
    "I certify that I have never been convicted of a criminal offence",
    "I am willing to relocate to the job location",
    "I agree to sign a 2 year service bond",
    "I acknowledge that I will not require visa sponsorship",
    "I confirm I am a U.S. citizen or permanent resident",
    "I agree to a pre-employment drug test",
    "I confirm my salary expectations stated above",
])
def test_legal_claims_are_never_plain_consent(label):
    assert LEGAL_CLAIM.search(label)
    assert not consent_only(label)


@pytest.mark.parametrize("label", [
    "I have read and agree to the Privacy Policy",
    "I agree to the Terms of Use",
    "I consent to the processing of my personal data for recruitment purposes",
    "Data retention: keep my data for future roles",
])
def test_privacy_terms_and_data_processing_are_consent(label):
    assert consent_only(label)


def test_value_for_confirms_privacy_but_not_legal_acknowledgements():
    g = applier("Location: Bengaluru, India")
    assert g.value_for("Please confirm receipt of our privacy notice")[0] == "Confirmed"
    assert g.value_for("I understand that I must be eligible to work in the US without sponsorship")[0] is None
    assert g.value_for("By selecting 'I acknowledge', I confirm I have no criminal record")[0] is None
    assert g._pick("I understand that I may be asked to relocate", ["Yes", "No"]) is None


# ------------------------------------------------------------------ where the job is
@pytest.mark.parametrize("text,url,want", [
    ("Software Engineer\nBengaluru, Karnataka, India\nFull time", "", "India"),
    ("Backend Developer - Gurugram", "", "India"),
    ("Backend Developer", "https://jobs.example.com/hyderabad-backend-developer", "India"),
    ("Software Engineer\nIndianapolis, IN\nFull time", "", "other"),
    ("Software Engineer - Indianapolis, Indiana", "", "other"),
    ("Data Engineer, London, United Kingdom", "", "other"),
    ("Remote - US", "", "other"),
    ("Hiring in Pune and Toronto", "", "mixed"),
    ("Join us! Contact us. About us.", "", None),
    ("Indian Ocean logistics team", "", None),
])
def test_job_country_from(text, url, want):
    assert job_country_from(text, url) == want


def test_indianapolis_is_not_india():
    assert job_country_from("Indianapolis, IN") != "India"


def test_job_country_is_cached_per_url():
    g = applier("Bengaluru, India")
    assert g.job_country() == "India"
    g.pg.text = "Seattle, WA"
    assert g.job_country() == "India"                # same URL: cached
    g.pg.url = "https://careers.example.com/jobs/456"
    assert g.job_country() == "other"


def test_work_authorization_only_for_jobs_in_the_authorized_country():
    india = applier("Backend Engineer\nPune, India")
    assert india.value_for("Are you legally authorized to work in the country?")[0] == "Yes"
    assert india.value_for("Are you currently eligible to work in the country where this job is located?")[0] == "Yes"
    assert india.value_for("Will you now or in the future require visa sponsorship?")[0] == "No"
    us = applier("Backend Engineer\nIndianapolis, IN")
    assert us.value_for("Are you legally authorized to work in the country?")[0] is None
    assert us.value_for("Will you now or in the future require visa sponsorship?")[0] is None
    nowhere = applier("Backend Engineer")
    assert nowhere.value_for("Are you legally authorized to work in the country?")[0] is None
    # a question naming the country is answered from the profile alone
    assert nowhere.value_for("Are you legally authorized to work in India?")[0] == "Yes"
    assert india.value_for("Are you legally authorized to work in the United States?")[0] is None
    # about the candidate, not the job: still answered
    assert nowhere.value_for("Are you located in India?")[0] == "Yes"


# ------------------------------------------------------------------ values from the profile
def test_education_values_come_from_the_profile():
    g = applier()
    assert g.value_for("Degree")[0] == "B.E."
    assert g.value_for("University")[0] == PROFILE["school"]
    assert g.value_for("Major")[0] == "Electronics"
    assert g._question_value("name of the college you graduated from") == PROFILE["school"]
    assert g._question_value("school name") == "Example Institute"          # short multi-word alias for search lists
    assert g._question_value("current city") == "Sample City"


def test_missing_profile_values_give_no_answer():
    g = applier(school=None, school_aliases=[], degree=None, major=None, city=None, current_ctc_lpa=None,
                expected_ctc_lpa=None, skill_years={}, skills_primary=[], relevant_experience_years=None)
    for q in ("name of the college", "school name", "field of study", "current city", "expected compensation",
              "current ctc", "primary programming language"):
        assert g._question_value(q) is None, q


def test_ctc_and_primary_language_are_derived():
    g = applier()
    assert g._question_value("expected annual compensation") == "INR 20 LPA (negotiable)"
    assert g._question_value("current ctc") == "INR 9.5 LPA"
    assert applier(ctc_negotiable=False)._question_value("expected compensation") == "INR 20 LPA"
    assert g._question_value("what is your primary programming language") == \
        "Python (about 3 years in production), also TypeScript"
    assert g._pick("Current CTC", ["<5 LPA", "5-10 LPA", "10-15 LPA"]) == "5-10 LPA"


@pytest.mark.parametrize("label", ["Start Date Month", "Start date year", "End Date Month*", "End date year",
                                   "Employment start date (month)", "End month", "Start year"])
def test_entry_dates_are_left_for_the_person(label):
    g = applier()
    assert g.value_for(label)[0] is None
    assert g._question_value(label.lower()) is None
    assert g._pick(label, ["January", "February", "October"]) is None


def test_school_level_fields_never_get_university_values():
    g = applier()
    assert g.value_for("12th Percentage")[0] == "88"
    assert g.value_for("Class X Marks (%)")[0] == "91.5"
    assert g.value_for("10th percentage*")[0] == "91.5"
    for label in ("12th Year of Passing", "10th School Name", "HSC Board", "Class XII CGPA", "High School GPA",
                  "SSC Institute", "Secondary school"):
        assert g.value_for(label)[0] is None, label
    assert g._pick("12th Year of Passing", ["2014", "2016", "2020"]) is None
    assert g.value_for("CGPA")[0] == "8.1"                     # the degree's own fields still work
    assert g.value_for("Graduation Year")[0] == "2020"


def test_company_matches():
    assert company_matches("Acme Widgets", "Acme Widgets Pvt Ltd")
    assert company_matches("ACME WIDGETS PVT LTD", "Acme Widgets Pvt Ltd")
    assert not company_matches("Other Corp", "Acme Widgets Pvt Ltd")
    assert not company_matches("", "Acme Widgets Pvt Ltd")


# ------------------------------------------------------------------ did Submit work?
def test_confirmation_needs_new_evidence():
    form = "Apply for this job\nName\nEmail\nThank you for your interest in Acme\nSubmit application"
    assert not confirmation_seen(form, form)                                     # "thank you" was there already
    assert not confirmation_seen(form, form + "\nPlease fill all required fields")
    assert confirmation_seen(form, "Thank you for applying! We'll be in touch.")
    assert confirmation_seen(form, form, "https://x.com/apply", "https://x.com/apply/thank-you")
    assert not confirmation_seen(form, form, "https://x.com/apply", "https://x.com/apply?step=2")
    assert confirmation_seen(form, "Your details are now with our recruiting team", form_gone=True)
    assert not confirmation_seen(form, "Submitting your application, please wait", form_gone=True)
    assert not confirmation_seen(form, form, form_gone=False)


def test_close_extra_pages_closes_only_tabs_this_application_opened():
    g = applier()
    other_company_tab, mine, form_tab = FakePage(), FakePage(), FakePage()
    g._opened = [mine, form_tab]
    g.close_extra_pages(keep=form_tab)
    assert mine.closed and not form_tab.closed and not other_company_tab.closed and not g.pg.closed


# ------------------------------------------------------------------ Workday
def workday(**over):
    return WorkdayApplier(None, dict(PROFILE, **over), "me@example.com", "pw", "r.pdf", assist=False, submit=True)


def test_workday_submit_blocked_by_unknown_fields():
    w = workday()
    w._note("First Name", "Test", "profile")
    w._unknown("Do you have a valid work permit?", ["Yes", "No"])
    res = w._submit()
    assert res.status == "ready_for_review" and "unanswered" in res.notes


def test_workday_submit_blocked_by_untrusted_sources():
    w = workday()
    w._note("First Name", "Test", "profile")
    w._note("School or University", "Other", "fallback (school not listed)")
    assert w.submit_blockers() == ([], ["School or University"])
    assert w._submit().status == "ready_for_review"
    w2 = workday()
    w2._note("Are you legally authorized to work in the country?", "Yes", w2._rule_source("Are you legally authorized to work in the country?"))
    assert w2.submit_blockers()[1]


def test_workday_unknown_cleared_when_filled_later():
    w = workday()
    w._unknown("Country", [])
    w._note("Country", "India", "profile/rules")
    assert w.submit_blockers() == ([], [])


def test_workday_school_match_is_exact():
    w = workday()
    inst = PROFILE["school"]
    assert w.school_option_matches("Example Institute of Technology", inst)
    assert w.school_option_matches("Example Institute of Technology (EIT)", inst)
    assert w.school_option_matches("EIT", inst)
    assert w.school_option_matches("example institute", inst)
    assert not w.school_option_matches("Example Institute of Technology and Management", inst)
    assert not w.school_option_matches("Example Inst", inst)
    assert not w.school_option_matches("Example Institute of Technology, Other City Campus", inst)


@pytest.mark.parametrize("label", ["Where did you hear about us", "How did you hear about this job? *",
                                   "Where did you first learn about this opening?"])
def test_heard_about_us_picks_a_job_source(label):
    # Detect Technologies (15 Oct 2026): "Where did you hear about us" with no Naukri/Other option stopped the form
    g = applier(how_heard=["Other", "Naukri", "Job Board", "LinkedIn", "Website"])
    assert g._pick(label, ["Linkedin", "Twitter", "Web", "Facebook"]) == "Linkedin"
    assert g._pick(label, ["Naukri", "Referral", "Other"]) == "Other"


@pytest.mark.parametrize("label", ["job applied for", "position applying for *", "role applying for"])
def test_position_applying_for_text_gets_the_role(label):
    g = applier()
    g.role = "AI/ML Engineer"
    assert g._question_value(label) == "AI/ML Engineer"


def test_workday_disability_group_with_generic_label():
    # PayPal (26 Oct 2026): the disability checkbox group is labelled only "Please check one of the boxes below:"
    opts = ["I DON'T WISH TO ANSWER (India)", "NO. I DON'T HAVE A DISABILITY (India)",
            "YES, I HAVE A DISABILITY (or previously had a disability) (India)"]
    assert workday(disability=False)._choice_for("Please check one of the boxes below:", opts) == 1
    assert workday(disability=False)._choice_for("Please check one of the boxes below:", ["Red", "Blue"]) is None


def test_workday_younger_than_18():
    assert workday()._choice_for("Are you younger than 18 years old?", ["Yes", "No"]) == 1


def test_workday_how_heard_career_site():
    # Cigna (29 Oct 2026): no job-board option, only Agency / Career Site / Recruiter / Referred by Employee
    opts = ["Select One", "Agency", "Career Site", "Recruiter", "Referred by Employee"]
    assert workday(how_heard=[])._choice_for("How Did You Hear About Us?", opts) == 2
