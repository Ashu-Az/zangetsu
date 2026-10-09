"""Pure-function tests for job discovery (no network)."""
from types import SimpleNamespace

import pytest

from naukri_bot.ats import big_tech, top_companies, workday_jobs  # noqa: F401  (imports must not need the network)
from naukri_bot.ats.levels import (dedupe, dedupe_roles, experience_label, html_to_text, india_only, is_india,
                                   min_years_required, too_senior)

AMAZON = ("BASIC QUALIFICATIONS\n- 3+ years of non-internship professional software development experience\n"
          "- 2+ years of non-internship design or architecture experience")
FOUNDERS = "Our founders bring 15+ years of experience... You have 1-3 years of experience"


def test_amazon_basic_quals_binding_requirement():
    assert min_years_required(AMAZON) == 3
    assert experience_label(AMAZON) == "3+ years"


def test_amazon_from_html():
    h = ("<p>Build things.</p><b>BASIC QUALIFICATIONS</b><br/>- 3+ years of non-internship professional software "
         "development experience<br/>- 2+ years of non-internship design or architecture experience<br/>"
         "<b>PREFERRED QUALIFICATIONS</b><br/>- 5+ years of full software development life cycle experience")
    assert min_years_required(html_to_text(h)) == 3


def test_founders_years_ignored():
    assert min_years_required(FOUNDERS) == 1
    assert experience_label(FOUNDERS) == "1 - 3 years"


@pytest.mark.parametrize("text,want", [
    ("Company with 20 years of history. Requirements:\n- At least 2 years of experience in Python", 2),
    ("We are looking for engineers with 2+ years of professional experience.", 2),
    ("Minimum Qualifications:\nBachelor's degree.\n4 to 6 years of industry experience.\n"
     "Preferred Qualifications:\n8+ years of experience", 4),
    ("Experience: 2-4 years", 2),
    ("Founded 12 years ago, we build tools.", None),
    ("Python 3 years. Nice team.", None),
    ("", None),
])
def test_min_years_required(text, want):
    assert min_years_required(text) == want


def test_years_do_not_cross_lines():
    assert min_years_required("Founded in 2010, 10 years\nexperience with Python is a must") is None


@pytest.mark.parametrize("loc,india,only", [
    ("Bengaluru, Karnataka, India", True, True),
    ("Indianapolis, IN", False, False),
    ("Indiana, USA", False, False),
    ("Remote - India", True, True),
    ("Hyderabad / London", True, False),
    (["Pune", "Remote"], True, True),
    ("New York, NY", False, False),
])
def test_india_detection(loc, india, only):
    assert is_india(loc) is india
    assert india_only(loc) is only


@pytest.mark.parametrize("title,text,want", [
    ("Software Engineer II", AMAZON, True),
    ("Software Engineer II", "You have 1-3 years of experience", False),
    ("SDE I", "", False),
    ("Senior AI Engineer", "", True),
    ("Sr. Software Engineer", "", True),
    ("Software Engineer 3", "", True),
    ("Software Engineer III", "", True),
    ("SDE-2", "", False),
    ("Staff Engineer", "", True),
    ("Principal ML Scientist", "", True),
    ("Lead Backend Developer", "", True),
    ("Machine Learning Engineer", "", False),
    ("Member of Technical Staff", "", False),
    ("Software Engineer, L5", "", True),
    ("Software Engineer (E4+)", "", True),
])
def test_too_senior(title, text, want):
    assert too_senior(title, text) is want


def test_board_lists_have_no_duplicates():
    for board in (top_companies.GREENHOUSE, top_companies.LEVER, top_companies.ASHBY):
        assert len(board) == len({b.lower() for b in board})
    keys = [(c[1], c[3]) for c in workday_jobs.COMPANIES]
    assert len(keys) == len(set(keys))
    assert "mongodb" in top_companies.GREENHOUSE and "meesho" in top_companies.LEVER
    assert not any("%" in b for b in top_companies.ASHBY) and "Pulsora Inc" in top_companies.ASHBY


def test_dedupe_preserves_order():
    assert dedupe(["a", "b", "A", "c", "b"]) == ["a", "b", "c"]


def test_dedupe_roles():
    R = lambda c, t, u: SimpleNamespace(company=c, title=t, apply_url=u)
    roles = [R("x", "AI Engineer", "u1"), R("y", "Other", "u1"), R("x", "AI  engineer!", "u2"), R("x", "SDE", "u3")]
    assert [r.apply_url for r in dedupe_roles(roles)] == ["u1", "u3"]
