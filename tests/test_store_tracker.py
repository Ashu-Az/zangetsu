import csv
import json

from naukri_bot.answers_store import AnswerCache
from naukri_bot.tracker import Tracker
from naukri_bot.models import JobPosting


def test_answer_cache_roundtrip(tmp_path):
    cache = AnswerCache(tmp_path / "cache.json")
    cache.save("What is your notice period?", ["0-15 days", "30 days", "60 days"], "30 days", [1])
    hit = cache.lookup("What is your notice period ?", ["60 days", "30 days", "0-15 days"])
    assert hit and hit["indices"] == [1]
    # different options => different question
    assert cache.lookup("What is your notice period?", ["Immediate", "1 month"]) is None
    # low similarity => miss
    assert cache.lookup("What is your current CTC?", ["0-15 days", "30 days", "60 days"]) is None
    cache.forget("What is your notice period?", ["0-15 days", "30 days", "60 days"])
    assert cache.lookup("What is your notice period?", ["0-15 days", "30 days", "60 days"]) is None
    assert json.loads((tmp_path / "cache.json").read_text()) == []


def test_tracker_csvs(tmp_path):
    t = Tracker(output_dir=tmp_path / "out", state_dir=tmp_path / "state")
    job = JobPosting(job_id="123", url="https://x", title="DS", company="Acme", location="Pune", experience="2-5")
    t.record_application(job, "naukri_apply", "applied", questions_answered=3)
    t.record_company_site(job, "https://acme.com/careers/1", 82, "strong on NLP", {"pdf": "/tmp/a.pdf"})
    t.record_answer(job, "Notice period?", "radio", ["30", "60"], "30", "llm")
    assert t.is_seen("123")
    t2 = Tracker(output_dir=tmp_path / "out", state_dir=tmp_path / "state")
    assert t2.is_seen("123") and not t2.is_seen("999")
    rows = list(csv.DictReader(open(tmp_path / "out" / "applications.csv")))
    assert rows[0]["status"] == "applied" and rows[0]["questions_answered"] == "3"
    rows = list(csv.DictReader(open(tmp_path / "out" / "company_site_jobs.csv")))
    assert rows[0]["external_url"] == "https://acme.com/careers/1" and rows[0]["fit_score"] == "82"
    rows = list(csv.DictReader(open(tmp_path / "out" / "answers.csv")))
    assert rows[0]["options"] == "30 | 60"
