"""naukri_bot/fit.py: would Ashu apply? (15 Nov 2026)"""
from naukri_bot import fit
from naukri_bot.models import JobPosting


def J(title, jd):
    return JobPosting(job_id="t", url="", title=title, description=jd)


EMPTY = fit.Feedback(path=fit.STATE_DIR / "__no_feedback_in_tests__")
LONG_STACK = ("Build our LLM agent platform in Python with FastAPI, LangGraph and RAG over customer documents; "
              "React front end, Postgres, Docker on AWS. You will own features end to end and ship weekly. " * 2)


def test_roles_he_turned_down_are_skipped():
    for title, text in fit.SEED_NO:
        assert fit.decide(J(title, text), fb=EMPTY)[0] == "skip", title


def test_vague_startup_title_with_his_stack_is_applied():
    for title in ("Founding Engineer", "Member of Technical Staff", "Builder", "Engineer"):
        verdict, score, why = fit.decide(J(title, LONG_STACK), fb=EMPTY)
        assert verdict == "apply", (title, why)


def test_other_primary_stack_is_unsure_not_skipped():
    jd = ("Work across our Java Spring Boot services and J2EE batch jobs with an Angular admin console; "
          "on-call rotation, code reviews and production support for the payments platform. ") * 3
    assert fit.decide(J("Software Engineer", jd), fb=EMPTY)[0] == "unsure"


def test_one_domain_word_does_not_sink_a_software_job():
    jd = ("Build Python services and REST APIs for our accounts payable automation product; FastAPI, Postgres, "
          "Docker, React dashboard, LLM-based invoice extraction.") * 2
    assert fit.decide(J("Software Engineer", jd), fb=EMPTY)[0] != "skip"


def test_job_without_description_is_unsure():
    assert fit.decide(J("AI Prompt Engineers", ""), fb=EMPTY)[0] == "unsure"


def test_internship_title_is_skipped():
    assert fit.decide(J("Software Engineer Intern", "Stipend 15k. " + LONG_STACK), fb=EMPTY)[0] == "skip"


def test_feedback_turns_off_stack_into_skip(tmp_path):
    fb = fit.Feedback(path=tmp_path / "fb.jsonl")
    java = "Java Spring Boot microservices, J2EE, Hibernate, Oracle, Angular. " * 4
    for _ in range(3):
        fb.add("Java Developer", java, False)
        fb.add("Python Developer", LONG_STACK, True)
    fb = fit.Feedback(path=tmp_path / "fb.jsonl")
    assert fit.decide(J("Java Backend Developer", java), fb=fb)[0] == "skip"
    assert fit.decide(J("Python Developer", LONG_STACK), fb=fb)[0] == "apply"


def test_readable_reasons():
    assert fit._label(r"java\b(?!script)") == "java"
    assert fit._label(r"sourc(e|ing) (candidates|talent)") == "source candidates"
