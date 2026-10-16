"""Drive Naukri's post-Apply chatbot drawer: read each question, answer it, submit.

Question types seen on Naukri: single-choice radio, multi-choice checkbox, a
suggestor (text box with autocomplete list), a native <select>, and free text in a
contenteditable div. Every answer comes from the answer cache or from Claude.
"""
from __future__ import annotations

import logging
import random
import re
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


INFO_MESSAGE = re.compile(r"recruiter requires more information|please answer (the following|a few)|"
                          r"kindly answer all the recruiter|thank you for showing interest|"   # Neo's drawer intro (14 Oct 2026)
                          r"answer (a few|the following) questions|to consider your appl", re.I)


class NeedsModel(Exception):
    """Raised when a question needs the language model and none is configured."""


class DrawerHandler:
    def __init__(self, browser, llm: LLM | None, cache: AnswerCache, tracker: Tracker, job: JobPosting, assist: bool = False):
        self.b = browser
        self.page = browser.page
        self.llm = llm
        self.cache = cache
        self.tracker = tracker
        self.job = job
        self.transcript: list[str] = []
        self.msg_count_at_answer: int | None = None   # bot-message count when we last sent an answer
        self.assist = assist                           # ask the person at the keyboard instead of abandoning a question

    # ---------- DOM reads ----------
    # Controls outside the question drawer that match the drawer selectors: Naukri's header search bar
    # ("[class*='suggestor'] input") and Neo's own chat box and Send button. On the Neo page the search bar is open,
    # and the bot typed answers into it (14 Oct 2026), so every drawer lookup skips them.
    _NOT_DRAWER = "header, [class*='nI-gnb'], [class*='gnb'], [class*='qsb'], [class*='searchBar'], [class*='search-bar'], " \
                  ".aGn-uI-footer-container, [class*='aGn-uI-footer']"

    def _fv(self, selector: str):
        loc = self.page.locator(selector)
        for i in range(min(loc.count(), 15)):
            el = loc.nth(i)
            try:
                if el.is_visible() and not el.evaluate("(e, s) => !!e.closest(s)", self._NOT_DRAWER):
                    return el
            except Exception:
                continue
        return None

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

    def bot_message_count(self) -> int:
        try:
            return self.page.locator(S.BOT_MESSAGES).count()
        except Exception:
            return 0

    def wait_for_next_message(self, timeout_s: float = 8) -> bool:
        """After an answer, wait until the bot posts something new, so the previous question's text
        is never paired with the next question's options."""
        if self.msg_count_at_answer is None:
            return True
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.bot_message_count() > self.msg_count_at_answer or not self.drawer_visible():
                time.sleep(1.0)          # typing indicator -> real text
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
        cont = self._fv(container_sel)
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
        sel = self._fv(S.DROPDOWN)
        if sel:
            options = sel.locator("option")
            labels = [options.nth(i).inner_text().strip() for i in range(options.count())]
            labels = [l for l in labels if l and l.lower() not in ("select", "select an option", "--select--")]
            return "dropdown", labels
        if self._fv(S.SUGGESTOR_INPUT):
            return "suggestor", []
        if self._fv(S.TEXT_INPUT):
            return "text", []
        return None, []

    # ---------- DOM writes ----------
    def _click_send(self) -> bool:
        btn = self._fv('.sendMsg') or self._fv(S.SEND_BUTTON)   # the Save button itself, not its full-width row (Neo drawer)
        if not btn:
            return False
        cls = (btn.get_attribute("class") or "") + " " + (btn.evaluate("e => e.parentElement ? e.parentElement.className : ''") or "")
        if S.SEND_DISABLED_CLASS in cls.lower():
            return False
        btn.click()
        return True

    def _click_options(self, container_sel: str, label_sel: str, indices: list[int]) -> None:
        cont = self._fv(container_sel)
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
            sel = self._fv(S.DROPDOWN)
            idx = answer.selected_option_indices[0] if answer.selected_option_indices else None
            label = options[idx] if idx is not None and idx < len(options) else answer.answer_text
            sel.select_option(label=label)
        elif kind == "suggestor":
            inp = self._fv(S.SUGGESTOR_INPUT)
            inp.click()
            inp.fill("")
            self.page.keyboard.type(answer.answer_text, delay=random.randint(50, 130))
            pause(1.0, 1.8)
            opt = self._fv(S.SUGGESTOR_OPTION)
            if opt:
                opt.click()
        else:  # free text in contenteditable
            box = self._fv(S.TEXT_INPUT)
            box.click()
            box.evaluate("e => { if (e.isContentEditable) e.innerText = ''; else e.value = ''; }")
            self.page.keyboard.type(answer.answer_text, delay=random.randint(35, 110))
        pause(0.5, 1.2)
        if not self._click_send():
            # chatbot text box also submits on Enter
            if kind in ("text", "suggestor"):
                self.page.keyboard.press("Enter")

    # ---------- assisted mode ----------
    def ask_human(self, question: str, kind: str, options: list[str], timeout_s: int = 300) -> bool:
        """Leave the question open, alert the person at the keyboard, and wait for them to answer it in the browser.
        Returns True once the chat moves on (new bot message or the drawer closed); the answer is saved for reuse."""
        import subprocess
        log.warning("   >>> YOUR TURN: answer in the Chromium window: %s", question[:120])
        from .ask_ashu import record
        record(question, options, "naukri", getattr(self.job, "company", ""))
        try:
            msg = question.replace('"', "'")[:120]
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Naukri bot needs your answer" sound name "Glass"'],
                           timeout=5, capture_output=True)
            subprocess.Popen(["say", "Naukri needs your answer"])
        except Exception:
            pass
        base = self.bot_message_count()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(1.5)
            if not self.drawer_visible() or self.bot_message_count() > base:
                answer = self.latest_user_message()
                log.info("   A(you): %s", answer[:160] or "(answered)")
                self.tracker.record_answer(self.job, question, kind, options, answer or "(answered in browser)", "human")
                if answer:
                    idx = [i for i, o in enumerate(options) if o.strip().lower() == answer.strip().lower()]
                    if not options or idx:
                        self.cache.save(question, options, answer, idx, source="human")   # same question next time: answered automatically
                self.transcript.append(f"Q: {question}\nA: {answer}")
                return True
        log.warning("   no answer within %ds", timeout_s)
        return False

    def latest_user_message(self) -> str:
        loc = self.page.locator(S.USER_MESSAGES)
        for i in range(loc.count() - 1, -1, -1):
            try:
                t = loc.nth(i).inner_text().strip()
            except Exception:
                continue
            if t:
                return t
        return ""

    def close_drawer(self) -> None:
        try:
            if not self.b.click_if_visible(S.DRAWER_CLOSE):
                self.page.keyboard.press("Escape")
        except Exception:
            pass

    # ---------- answer sourcing ----------
    def _get_answer(self, question: str, kind: str, options: list[str], rejected: str | None) -> tuple[QuestionAnswer, str]:
        from .rules import rule_answer
        from .config import load_profile
        # An answer the person gave by hand beats every rule: it is how a misfiring rule gets corrected.
        if not rejected:
            hit = self.cache.lookup(question, options)
            if hit and hit.get("source") == "human":
                return QuestionAnswer(answer_text=hit["answer_text"], selected_option_indices=hit["indices"],
                                      reusable=True, skip=False, confidence=1.0), "cache"
        ruled = rule_answer(question, options, kind, load_profile())
        if ruled is not None and not rejected:
            return ruled, "rules"
        if not rejected:
            hit = self.cache.lookup(question, options)
            if hit:
                return QuestionAnswer(answer_text=hit["answer_text"], selected_option_indices=hit["indices"],
                                      reusable=True, skip=False, confidence=1.0), "cache"
        if ruled is not None and not self.llm:
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
            got_new = self.wait_for_next_message()
            question = self.latest_bot_message()
            if any(t.lower() in question.lower() for t in S.APPLY_SUCCESS_TEXT):
                result.completed = True
                result.notes = "success message"
                return result
            if INFO_MESSAGE.search(question) and "?" not in question:
                # Naukri's intro bubble ("The recruiter requires more information ..."): not a question
                self.msg_count_at_answer = self.bot_message_count()
                if self.wait_for_next_message(timeout_s=6):
                    continue
            kind, options = self.detect_input()
            if kind is None:
                idle += 1
                if idle >= 8 and self.assist and self.drawer_visible() and not self.b.text_present(S.APPLY_SUCCESS_TEXT):
                    # an input the bot cannot see or fill (date picker, file upload, ...): hand it to the person
                    if self.ask_human(question or "(unrecognised input in the questionnaire)", "unknown", []):
                        idle = 0
                        self.msg_count_at_answer = None
                        continue
                if idle >= 8:
                    result.completed = self.b.text_present(S.APPLY_SUCCESS_TEXT) or not self.drawer_visible()
                    result.notes = "no input shown; assumed finished" if result.completed else "stalled"
                    return result
                time.sleep(1.5)
                continue
            idle = 0

            key = (question, kind, tuple(options))
            rejected = None
            if key == last_key or (not got_new and last_key and question == last_key[0]):
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
                if self.assist and self.ask_human(question, kind, options):
                    result.answered.append({"question": question, "kind": kind, "options": options, "answer": "(you)", "source": "human"})
                    last_key = None
                    self.msg_count_at_answer = None
                    continue
                from .ask_ashu import record
                record(question, options, "naukri", getattr(self.job, "company", ""))
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
                # "How many years of experience in <tool not on the resume>?": the truthful answer is 0, and the
                # application should still go in (Robot Framework, Tech Mahindra, 16 Oct 2026; Ashu: No/0 only when
                # the tech was never used). Never for tenure-in-total questions, only a named skill.
                if kind == "text" and not options and re.search(r"how many years|years of experience|experience (do you have )?in", question, re.I) \
                        and not re.search(r"total|overall|relevant|professional|work experience\??$", question, re.I):
                    from .llm import QuestionAnswer
                    ans, source = QuestionAnswer(answer_text="0", selected_option_indices=[], reusable=True, skip=False,
                                                 confidence=0.9), "rules:not-on-resume"
                    from .ask_ashu import record
                    record(question, options, "naukri", getattr(self.job, "company", ""))   # Ashu can correct it later
            if ans.skip or (not ans.answer_text and not ans.selected_option_indices):
                if self.assist and self.ask_human(question, kind, options):
                    result.answered.append({"question": question, "kind": kind, "options": options, "answer": "(you)", "source": "human"})
                    last_key = None
                    self.msg_count_at_answer = None
                    continue
                result.notes = f"could not answer truthfully: {question[:80]}"
                log.warning("   skipping job: %s", result.notes)
                self.close_drawer()
                return result

            log.info("   A(%s): %s", source, ans.answer_text[:160])
            self.msg_count_at_answer = self.bot_message_count()   # before sending: a fast reply must count as new
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
            if source == "llm" and ans.reusable and not rejected:
                self.cache.save(question, options, ans.answer_text, ans.selected_option_indices)
            if rejected:
                self.cache.forget(question, options)
            last_key = key
            pause(2.0, 3.5)

        result.notes = "max rounds reached"
        return result
