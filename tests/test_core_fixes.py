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
