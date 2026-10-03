"""Drive Naukri's post-Apply chatbot drawer: read each question, answer it, submit.

Question types seen on Naukri: single-choice radio, multi-choice checkbox, a
suggestor (text box with autocomplete list), a native <select>, and free text in a
contenteditable div. Every answer comes from the answer cache or from Claude.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from playwright.sync_api import TimeoutError as PWTimeout

from . import selectors as S
from .answers_store import AnswerCache
from .humanize import pause
from .llm import LLM, QuestionAnswer
from .models import JobPosting
from .tracker import Tracker

log = logging.getLogger(__name__)


@dataclass
class QuestionnaireResult:
    completed: bool
    answered: list[dict] = field(default_factory=list)
    notes: str = ""


class NeedsModel(Exception):
    """Raised when a question needs the language model and none is configured."""


class DrawerHandler:
    def __init__(self, browser, llm: LLM | None, cache: AnswerCache, tracker: Tracker, job: JobPosting):
        self.b = browser
        self.page = browser.page
        self.llm = llm
        self.cache = cache
        self.tracker = tracker
        self.job = job
        self.transcript: list[str] = []

    # ---------- DOM reads ----------
    def drawer_visible(self) -> bool:
        return self.b.first_visible(S.DRAWER) is not None

    def wait_for_drawer(self, timeout_s: float = 8) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.drawer_visible():
                pause(1.5, 2.5)
                return True
            time.sleep(0.5)
        return False

    def latest_bot_message(self) -> str:
        loc = self.page.locator(S.BOT_MESSAGES)
        n = loc.count()
        for i in range(n - 1, -1, -1):
            try:
                t = loc.nth(i).inner_text().strip()
            except Exception:
                continue
            if t:
                return t
        return ""

    def _visible_labels(self, container_sel: str, label_sel: str) -> list[str]:
        cont = self.b.first_visible(container_sel)
        if not cont:
            return []
        labels = cont.locator(label_sel)
        out = []
        for i in range(labels.count()):
            try:
                t = labels.nth(i).inner_text().strip()
            except Exception:
                t = ""
            if t:
                out.append(t)
        return out

    def detect_input(self) -> tuple[str | None, list[str]]:
        """Return (kind, options). kind is None when no input is currently shown."""
        opts = self._visible_labels(S.RADIO_CONTAINER, S.RADIO_LABEL)
        if opts:
            return "radio", opts
        opts = self._visible_labels(S.CHECKBOX_CONTAINER, S.CHECKBOX_LABEL)
        if opts:
            return "checkbox", opts
        sel = self.b.first_visible(S.DROPDOWN)
        if sel:
            options = sel.locator("option")
            labels = [options.nth(i).inner_text().strip() for i in range(options.count())]
            labels = [l for l in labels if l and l.lower() not in ("select", "select an option", "--select--")]
            return "dropdown", labels
        if self.b.first_visible(S.SUGGESTOR_INPUT):
            return "suggestor", []
        if self.b.first_visible(S.TEXT_INPUT):
            return "text", []
        return None, []

    # ---------- DOM writes ----------
    def _click_send(self) -> bool:
        btn = self.b.first_visible(S.SEND_BUTTON)
        if not btn:
            return False
        cls = (btn.get_attribute("class") or "") + " " + (btn.evaluate("e => e.parentElement ? e.parentElement.className : ''") or "")
        if S.SEND_DISABLED_CLASS in cls.lower():
            return False
        btn.click()
        return True

    def _click_options(self, container_sel: str, label_sel: str, indices: list[int]) -> None:
        cont = self.b.first_visible(container_sel)
        labels = cont.locator(label_sel)
        for idx in indices:
            if 0 <= idx < labels.count():
                labels.nth(idx).click()
                pause(0.4, 0.9)

    def fill(self, kind: str, answer: QuestionAnswer, options: list[str]) -> None:
        if kind == "radio":
            self._click_options(S.RADIO_CONTAINER, S.RADIO_LABEL, answer.selected_option_indices[:1])
        elif kind == "checkbox":
            self._click_options(S.CHECKBOX_CONTAINER, S.CHECKBOX_LABEL, answer.selected_option_indices)
        elif kind == "dropdown":
            sel = self.b.first_visible(S.DROPDOWN)
            idx = answer.selected_option_indices[0] if answer.selected_option_indices else None
            label = options[idx] if idx is not None and idx < len(options) else answer.answer_text
            sel.select_option(label=label)
        elif kind == "suggestor":
            inp = self.b.first_visible(S.SUGGESTOR_INPUT)
            inp.click()
            inp.fill("")
            self.page.keyboard.type(answer.answer_text, delay=random.randint(50, 130))
            pause(1.0, 1.8)
            opt = self.b.first_visible(S.SUGGESTOR_OPTION)
            if opt:
                opt.click()
        else:  # free text in contenteditable
            box = self.b.first_visible(S.TEXT_INPUT)
            box.click()
            self.page.keyboard.type(answer.answer_text, delay=random.randint(35, 110))
        pause(0.5, 1.2)
        if not self._click_send():
            # chatbot text box also submits on Enter
            if kind in ("text", "suggestor"):
                self.page.keyboard.press("Enter")

    def close_drawer(self) -> None:
        try:
            if not self.b.click_if_visible(S.DRAWER_CLOSE):
                self.page.keyboard.press("Escape")
        except Exception:
            pass

    # ---------- answer sourcing ----------
    def _get_answer(self, question: str, kind: str, options: list[str], rejected: str | None) -> tuple[QuestionAnswer, str]:
        if not rejected:
            hit = self.cache.lookup(question, options)
            if hit:
                return QuestionAnswer(answer_text=hit["answer_text"], selected_option_indices=hit["indices"],
                                      reusable=True, skip=False, confidence=1.0), "cache"
        from .rules import rule_answer
        from .config import load_profile
        ruled = rule_answer(question, options, kind, load_profile())
        if ruled is not None:
            return ruled, "rules"
        if not self.llm:
            raise NeedsModel(question)
        ans = self.llm.answer_question(question, options, kind, self.job, self.transcript, rejected_previous=rejected)
        # sanity: option questions must carry a valid index; recover from label text if needed
        if kind in ("radio", "checkbox", "dropdown") and options:
            valid = [i for i in ans.selected_option_indices if 0 <= i < len(options)]
            if not valid and ans.answer_text:
                valid = [i for i, o in enumerate(options) if o.strip().lower() == ans.answer_text.strip().lower()]
            if not valid and not ans.skip:
                ans.skip = True
            ans.selected_option_indices = valid
            if valid:
                ans.answer_text = ", ".join(options[i] for i in valid)
        return ans, "llm"

    # ---------- main loop ----------
    def run(self, max_rounds: int = 30) -> QuestionnaireResult:
        result = QuestionnaireResult(completed=False)
        last_key = None
        repeats = 0
        idle = 0
        for _ in range(max_rounds):
            if not self.drawer_visible():
                result.completed = True
                result.notes = "drawer closed"
                return result
            question = self.latest_bot_message()
            if any(t.lower() in question.lower() for t in S.APPLY_SUCCESS_TEXT):
                result.completed = True
                result.notes = "success message"
                return result
            kind, options = self.detect_input()
            if kind is None:
                idle += 1
                if idle >= 8:
                    result.completed = self.b.text_present(S.APPLY_SUCCESS_TEXT) or not self.drawer_visible()
                    result.notes = "no input shown; assumed finished" if result.completed else "stalled"
                    return result
                time.sleep(1.5)
                continue
            idle = 0

            key = (question, kind, tuple(options))
            rejected = None
            if key == last_key:
                repeats += 1
                if repeats == 1:
                    log.info("   same question still showing; re-sending")
                    if self._click_send():
                        pause(2, 3)
                        continue
                if repeats >= 3:
                    result.notes = f"answer rejected 3x: {question[:80]}"
                    self.close_drawer()
                    return result
                rejected = result.answered[-1]["answer"] if result.answered else "(unknown)"
            else:
                repeats = 0

            log.info("   Q[%s]: %s", kind, question[:120])
            if options:
                log.info("      options: %s", options)
            try:
                ans, source = self._get_answer(question, kind, options, rejected)
            except NeedsModel:
                result.notes = f"needs model: {question[:100]}"
                log.info("   deferred, open question needs the model")
                self.close_drawer()
                return result
            except Exception as e:
                result.notes = f"answer generation failed: {e}"
                log.error(result.notes)
                self.close_drawer()
                return result

            if ans.skip or (not ans.answer_text and not ans.selected_option_indices):
                result.notes = f"could not answer truthfully: {question[:80]}"
                log.warning("   skipping job: %s", result.notes)
                self.close_drawer()
                return result

            log.info("   A(%s): %s", source, ans.answer_text[:160])
            try:
                self.fill(kind, ans, options)
            except (PWTimeout, Exception) as e:
                result.notes = f"could not fill answer: {e}"
                log.error(result.notes)
                self.close_drawer()
                return result

            result.answered.append({"question": question, "kind": kind, "options": options,
                                    "answer": ans.answer_text, "source": source})
            self.transcript.append(f"Q: {question}\nA: {ans.answer_text}")
            self.tracker.record_answer(self.job, question, kind, options, ans.answer_text, source)
            if source in ("llm", "rules") and ans.reusable and not rejected:
                self.cache.save(question, options, ans.answer_text, ans.selected_option_indices)
            if rejected:
                self.cache.forget(question, options)
            last_key = key
            pause(2.0, 3.5)

        result.notes = "max rounds reached"
        return result
