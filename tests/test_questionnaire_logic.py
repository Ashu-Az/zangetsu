"""Exercise DrawerHandler.run() against a scripted fake page: no browser, no API."""
from naukri_bot.questionnaire import DrawerHandler
from naukri_bot.answers_store import AnswerCache
from naukri_bot.tracker import Tracker
from naukri_bot.models import JobPosting
from naukri_bot.llm import QuestionAnswer


class FakeLLM:
    def __init__(self):
        self.calls = []

    def answer_question(self, question, options, kind, job, transcript, rejected_previous=None):
        self.calls.append((question, kind, tuple(options), rejected_previous))
        if "notice" in question.lower():
            return QuestionAnswer(answer_text="30 days", selected_option_indices=[1], reusable=True, skip=False, confidence=0.95)
        if "why" in question.lower():
            return QuestionAnswer(answer_text="I have shipped two RAG systems and want to do it at scale.",
                                  selected_option_indices=[], reusable=False, skip=False, confidence=0.8)
        return QuestionAnswer(answer_text="", selected_option_indices=[], reusable=False, skip=True, confidence=0.1)


class ScriptedHandler(DrawerHandler):
    """Replaces every DOM method with a script of (question, kind, options) steps."""

    def __init__(self, steps, llm, cache, tracker, job):
        self.b = self.page = None
        self.llm, self.cache, self.tracker, self.job = llm, cache, tracker, job
        self.transcript = []
        self.steps = list(steps)
        self.filled = []

    def drawer_visible(self):
        return bool(self.steps)

    def latest_bot_message(self):
        return self.steps[0][0]

    def detect_input(self):
        return self.steps[0][1], list(self.steps[0][2])

    def fill(self, kind, answer, options):
        self.filled.append((kind, answer.answer_text, list(answer.selected_option_indices)))
        self.steps.pop(0)

    def close_drawer(self):
        self.steps.clear()

    def _click_send(self):
        return False


def make(tmp_path, steps, llm=None):
    llm = llm or FakeLLM()
    cache = AnswerCache(tmp_path / "cache.json")
    tracker = Tracker(output_dir=tmp_path / "out", state_dir=tmp_path / "state")
    job = JobPosting(job_id="1", url="u", title="DS", company="Acme")
    return ScriptedHandler(steps, llm, cache, tracker, job), llm, cache


def test_answers_radio_then_text_and_caches_reusable(tmp_path, monkeypatch):
    monkeypatch.setattr("naukri_bot.questionnaire.pause", lambda *a, **k: None)
    h, llm, cache = make(tmp_path, [
        ("What is your notice period?", "radio", ["Immediate", "30 days", "60 days"]),
        ("Why do you want to join Acme?", "text", []),
    ])
    res = h.run()
    assert res.completed and len(res.answered) == 2
    assert h.filled[0] == ("radio", "30 days", [1])
    assert h.filled[1][0] == "text"
    assert cache.lookup("What is your notice period?", ["Immediate", "30 days", "60 days"])["indices"] == [1]
    # second application: cache hit, no LLM call for the radio question
    h2, llm2, _ = make(tmp_path, [("What is your notice period?", "radio", ["Immediate", "30 days", "60 days"])])
    h2.run()
    assert llm2.calls == []


def test_skips_job_when_unanswerable(tmp_path, monkeypatch):
    monkeypatch.setattr("naukri_bot.questionnaire.pause", lambda *a, **k: None)
    h, llm, _ = make(tmp_path, [("Do you hold a US work visa?", "radio", ["Yes", "No"])])
    res = h.run()
    assert not res.completed and "could not answer" in res.notes


def test_invalid_index_recovered_from_label(tmp_path, monkeypatch):
    monkeypatch.setattr("naukri_bot.questionnaire.pause", lambda *a, **k: None)

    class BadIndexLLM(FakeLLM):
        def answer_question(self, *a, **k):
            return QuestionAnswer(answer_text="No", selected_option_indices=[7], reusable=True, skip=False, confidence=0.9)

    h, _, _ = make(tmp_path, [("Rotational shifts ok?", "radio", ["Yes", "No"])], llm=BadIndexLLM())
    h.run()
    assert h.filled[0] == ("radio", "No", [1])
