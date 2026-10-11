"""Core safety fixes from the 9-10 Oct 2026 review."""
from datetime import datetime

from naukri_bot.answers_store import AnswerCache
from naukri_bot.models import JobPosting
from naukri_bot.portals.runner import make_answerer
from naukri_bot.tracker import Tracker


def _job(i, title="AI Engineer", company="Acme"):
    return JobPosting(job_id=str(i), url=f"https://example.com/{i}", title=title, company=company)


def test_human_answer_beats_rules(tmp_path):
    cache = AnswerCache(path=tmp_path / "cache.json")
    profile = {"notice_period_days": 15}
    ask = make_answerer(profile, cache)
    assert ask("What is your notice period in days?", [], "text").answer_text == "15"        # rule
    cache.save("What is your notice period in days?", [], "30", [], source="human")          # person corrected it
    assert ask("What is your notice period in days?", [], "text").answer_text == "30"
    cache.save("Why do you want this job?", [], "auto text", [], source="auto")
    assert ask("Why do you want this job?", [], "text").answer_text == "auto text"           # auto cache still used


def test_unconfirmed_submit_is_final_and_counted(tmp_path):
    t = Tracker(output_dir=tmp_path, state_dir=tmp_path)
    t.record_application(_job(1), "linkedin:naukri_apply", "submitted_unconfirmed", remember=False)
    t.mark_seen("linkedin:1", "submitted_unconfirmed")
    assert t.is_seen("linkedin:1")                                # never retried
    assert t.applied_today("linkedin") == 1                       # counts toward the cap
    assert Tracker.role_key("Acme", "AI Engineer") in t.recent_role_keys()   # blocks the same role


def test_only_naukri_rows_use_naukri_quota(tmp_path):
    t = Tracker(output_dir=tmp_path, state_dir=tmp_path)
    for i, mode in enumerate(["naukri_apply", "questionnaire", "company_site", "workday", "email"]):
        t.record_application(_job(i, company=f"C{i}"), mode, "applied", remember=False)
    assert t.applied_today("naukri") == 2
    assert t.applied_last_24h("naukri") == 2


def test_nvite_remote_location_is_not_work_mode():
    from naukri_bot.nvites import parse_details
    text = "SDE - Remote\nRemote\n1 - 6 Years5 - 13 Lacs pa\nJob description\nBuild backend APIs in Python."
    skills, jd = parse_details(text)
    assert skills == [] and "Python" in jd
    skills, _ = parse_details("AI Engineer\nHybrid\nPython, FastAPI, LLM\nJob description\nx")
    assert skills == ["Python", "FastAPI", "LLM"]


def test_steady_pacing(monkeypatch):
    import types
    from naukri_bot import humanize
    clock = [1000.0]
    slept = []
    monkeypatch.setattr(humanize.time, "monotonic", lambda: clock[0])
    def sleep(x):
        slept.append(x); clock[0] += x
    monkeypatch.setattr(humanize.time, "sleep", sleep)
    monkeypatch.setattr(humanize, "_last_action", 0.0)
    s = types.SimpleNamespace(min_delay=8, max_delay=14)
    humanize.steady_wait(s)                       # first action: no wait
    for _ in range(40):
        clock[0] += 1.0                           # the page work itself took a second
        humanize.steady_wait(s)
    assert slept and all(7.999 <= g + 1.0 <= 14.001 for g in slept)   # every gap 8-14 s, no bursts, no long breaks


def test_saved_for_manual_does_not_block_company_apply(tmp_path):
    from naukri_bot.tracker import Tracker
    t = Tracker.__new__(Tracker)
    t.applications_csv = tmp_path / "a.csv"
    t.applications_csv.write_text("timestamp,company,title,status\n2099-01-01T00:00:00,Prodigal,Forward Deployed Engineer,saved_for_manual\n")
    assert Tracker.role_key("Prodigal", "Forward Deployed Engineer") in t.recent_role_keys()
    assert not t.recent_role_keys(include_saved=False)


def test_age_in_years():
    from naukri_bot.ranking import parse_age_days
    assert parse_age_days("1 year ago") == 365 and parse_age_days("a year ago") == 365
    assert parse_age_days("POSTED 4 YEARS AGO") == 4 * 365 and parse_age_days("3 days ago") == 3
