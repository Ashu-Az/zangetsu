from naukri_bot.ats_bimetric import bimetric
from naukri_bot.llm import EducationItem, ExperienceItem, SkillGroup, TailoredResume

PROFILE = {"total_experience_years": 2}


def _resume(skills=("Python", "FastAPI"), bullets=("Built REST APIs in Python with FastAPI",), headline="Python Developer"):
    return TailoredResume(name="A", headline=headline, contact_line="", summary="",
                          skills=[SkillGroup(name="Core", items=list(skills))],
                          experience=[ExperienceItem(title="Software Engineer", company="X", dates="2024 - Present", bullets=list(bullets))],
                          projects=[], education=[EducationItem(degree="B.Tech, Information Technology", institution="NSUT")],
                          keywords_used=[], fit_score=0, fit_notes="")


JD = """Python Developer
Requirements:
- 2 - 5 years of experience
- Python, FastAPI, Docker
Education
UG: Any Graduate
"""


def test_experience_backed_skill_beats_list_only():
    r = _resume(skills=("Python", "FastAPI", "Docker"))
    sk = bimetric(r, JD, "Python Developer", profile=PROFILE)["categories"]["skills"]
    assert "Docker" in sk["skills_list_only"] and "Python" in sk["in_experience"]
    assert sk["score"] < 100


def test_missing_skill_lowers_score_and_is_fixable():
    res = bimetric(_resume(), JD, "Python Developer", profile=PROFILE)
    assert "Docker" in res["categories"]["skills"]["missing"]
    assert "Docker" in res["fixable"]["skills"]


def test_years_and_degree_and_title():
    res = bimetric(_resume(), JD, "Python Developer", profile=PROFILE)["categories"]
    assert res["experience"]["score"] == 100
    assert res["education"]["score"] == 100            # UG: any graduate -> bachelor's is enough
    assert res["job_title"]["score"] == 100


def test_under_experienced_is_penalised():
    jd = JD.replace("2 - 5 years", "5 - 8 years")
    assert bimetric(_resume(), jd, "Python Developer", profile=PROFILE)["categories"]["experience"]["score"] < 50


def test_categories_the_jd_omits_are_left_out():
    res = bimetric(_resume(), "Python, FastAPI", "Python Developer", profile=PROFILE)
    assert "experience" not in res["categories"] and "certifications" not in res["categories"]
