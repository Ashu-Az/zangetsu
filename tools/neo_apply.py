"""Apply to Naukri Neo (the Pro AI job agent) suggestions through Neo's own flow, the way a person does it.

What Neo actually does (walked through on 14 Oct 2026):
  1. You ask it in the chat ("Find jobs matching my profile"); it answers with job cards. Loading the page alone
     shows no jobs, which is why the old version of this tool found only 0-4 leftover cards.
  2. A card opens a side panel with "Apply now" and "Apply with custom Resume".
  3. "Apply with custom Resume": Neo writes a tailored resume (about 30-60 s), then offers "Apply with tailored
     Resume" - or, when tailoring fails, "Use original Resume".
  4. "Recruiter from X has a few quick questions": "Add missing details" opens the usual Naukri question drawer
     (same selectors as a normal Naukri apply), answered here with the bot's rules, cache and model.
Every job is checked first on its job page in a side tab (title exclusions, experience fit, already applied), and
the result is confirmed on the job page afterwards ("Applied"). Results go to output/applications.csv.
    python tools/neo_apply.py [--dry-run] [--max N] [job ids...]
"""
import json
import logging
import re
import sys
import time

sys.path.insert(0, ".")
from naukri_bot.answers_store import AnswerCache  # noqa: E402
from naukri_bot.browser import Browser  # noqa: E402
from naukri_bot.config import Settings, load_profile  # noqa: E402
from naukri_bot.job_page import detect_apply_mode, scrape_job  # noqa: E402
from naukri_bot.main import QuotaReached, handle_naukri_apply, make_llm  # noqa: E402
from naukri_bot.models import ApplyMode  # noqa: E402
from naukri_bot.questionnaire import DrawerHandler  # noqa: E402
from naukri_bot import selectors as S  # noqa: E402
from naukri_bot.ranking import eligibility  # noqa: E402
from naukri_bot.tracker import SUBMITTED_STATUSES, Tracker  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("neo")
NEO = "https://www.naukri.com/job-apply-agent?navBarVisibility=false"
CARD = ".aGn-uI-job-tuple-title"
TECH_TITLE = re.compile(r"engineer|developer|scientist|programmer|\bsde\b|\bai\b|\bml\b|machine learning|deep learning|computer vision|\bvision\b|"
                        r"\bdata\b|software|python|full[- ]?stack|back[- ]?end|front[- ]?end|devops|mlops|llm|gen ?ai|nlp|"
                        r"analyst|architect|research|automation|cloud|\bit\b", re.I)
CHAT_BOX = "textarea, input[placeholder*='message' i], [contenteditable=true]"
ASKS = ["Find jobs matching my profile", "Show me more jobs like these", "Show me more jobs like these"]
dry = "--dry-run" in sys.argv
MAX = int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else 15
ONLY = re.compile(sys.argv[sys.argv.index("--only") + 1], re.I) if "--only" in sys.argv else None
# --force with --only: Ashu picked this job himself, so the title filters (excluded / non-tech titles) do not apply
FORCE = ONLY is not None and "--force" in sys.argv
# suggestions not applied yet (cap, cooldown, unconfirmed): retried first on the next run, for up to 4 days
PENDING = "state/neo_pending.json"


def _load_pending() -> dict:
    try:
        d = json.load(open(PENDING))
        return {j: v for j, v in d.items() if time.time() - v.get("first_seen", 0) < 4 * 86400}
    except (OSError, ValueError):
        return {}


def _jobs_in(obj, out):
    """(title, jobId) pairs anywhere in Neo's agent-response JSON (its jobList entries)."""
    if isinstance(obj, dict):
        jid = str(obj.get("jobId") or obj.get("jobid") or "")
        if obj.get("title") and re.fullmatch(r"\d{9,}", jid):
            out.append((" ".join(obj["title"].split()), jid))
        for v in obj.values():
            _jobs_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _jobs_in(v, out)


class Side:
    """Run job-page checks in a second tab so the Neo chat (which does not survive a reload) stays open."""
    def __init__(self, b: Browser):
        self.b = b

    def __enter__(self):
        self.main = self.b.page
        self.b.page = self.b.context.new_page()
        return self.b

    def __exit__(self, *exc):
        try:
            self.b.page.close()
        finally:
            self.b.page = self.main
            self.main.bring_to_front()


def close_panel(pg) -> None:
    """Close an unfinished question drawer and the job side panel: both cover the chat box and the other cards."""
    for _ in range(2):
        drawer = pg.locator(S.DRAWER)
        if not (drawer.count() and drawer.first.is_visible()):
            break
        pg.keyboard.press("Escape"); time.sleep(1)
        if drawer.count() and drawer.first.is_visible():
            x = pg.locator(S.DRAWER_CLOSE)
            if x.count() and x.first.is_visible():
                x.first.click(); time.sleep(1)
    for _ in range(3):
        back = pg.locator("[aria-label='Close job details']")
        if not (back.count() and back.first.is_visible()):
            return
        try:
            close = visible_button(pg, r"^\s*Close\s*$")
            (close or back.first).click(timeout=3000)
        except Exception:
            pg.keyboard.press("Escape")
        time.sleep(1)


def ask_neo(pg, text: str, wait_s: int = 90) -> int:   # Neo can take over a minute to answer (21 Oct 2026: 0 cards at 45 s)
    """Type a request into Neo's chat; return how many job cards are on the page once new ones arrive."""
    close_panel(pg)
    before = pg.locator(CARD).count()
    box = pg.locator(CHAT_BOX).last
    try:
        box.click(timeout=5000)
    except Exception:
        # something still covers the chat box (a job panel left open after a skipped card, 21 Oct 2026)
        pg.screenshot(path=f"output/debug/{time.strftime('%Y%m%d-%H%M%S')}-neo-chatbox-covered.png")
        close_panel(pg); pg.keyboard.press("Escape"); time.sleep(1)
        try:
            box.click(timeout=5000)
        except Exception:
            box.scroll_into_view_if_needed(timeout=3000)
            box.evaluate("e => e.focus()")
    pg.keyboard.type(text, delay=35)
    pg.keyboard.press("Enter")
    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(2)
        if pg.locator(CARD).count() > before:
            time.sleep(4)                       # let the whole batch render
            break
    return pg.locator(CARD).count()


def visible_button(pg, pattern: str):
    loc = pg.get_by_text(re.compile(pattern, re.I))
    for i in range(loc.count() - 1, -1, -1):
        try:
            if loc.nth(i).is_visible():
                return loc.nth(i)
        except Exception:
            pass
    return None


RESUME_DONE = ("Apply with tailored Resume", "Use original Resume")


def _buttons(pg, label: str):
    """Clickable controls with exactly this label. Neo also echoes every choice as a user chat bubble
    (div.aGn-uI-um__text "Apply with custom Resume"), which must never be clicked (Razorthink, 14 Oct 2026)."""
    return pg.locator("button, [role=button], a").filter(has_text=re.compile(rf"^\s*{re.escape(label)}\s*$", re.I))


def _count(pg, label: str) -> int:
    return _buttons(pg, label).count()


AFTER_LAST_APPLY = """(e) => {
  // the user's own "Apply with custom Resume" / "Apply now" bubble marks where the current job starts in the chat
  const bubbles = [...document.querySelectorAll('.aGn-uI-um__text')]
      .filter(x => /^\\s*(Apply with custom Resume|Apply now)\\s*$/i.test(x.innerText));
  const b = bubbles.pop();
  return !b || !!(b.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING);
}"""


def _panel_button(pg, label: str):
    loc = _buttons(pg, label)
    for i in range(loc.count() - 1, -1, -1):
        try:
            if loc.nth(i).is_visible():
                return loc.nth(i)
        except Exception:
            pass
    return None


def new_button(pg, label: str, baseline: dict | None = None):
    """The button with this label that belongs to THIS job: the newest one that comes after the user's latest
    "Apply with custom Resume" bubble. Earlier jobs' buttons stay in the chat (clicking them applied nothing,
    Tech Mahindra 14 Oct), and counting them by index broke when an old job's buttons disappeared (Bain, 16 Oct)."""
    loc = _buttons(pg, label)
    for i in range(loc.count() - 1, -1, -1):
        try:
            el = loc.nth(i)
            if el.is_visible() and el.evaluate(AFTER_LAST_APPLY):
                return el
        except Exception:
            pass
    return None


def _click(el) -> None:
    """Click, and when something sits on top of the button (Neo's "Neo's take" card slides over the panel's
    Apply buttons; Tata Elxsi, 20 Oct) click the element itself instead of waiting 15 s for it to clear."""
    try:
        el.click(timeout=5000)
    except Exception:
        el.scroll_into_view_if_needed(timeout=3000)
        el.evaluate("e => e.click()")


def neo_apply_one(b: Browser, llm, cache, tracker, job) -> str:
    """The open side panel's job, step by step and only with the buttons Neo shows for THIS job:
    custom resume -> wait until the resume step is done -> tailored resume (else original) -> answer the recruiter's
    questions -> "Successfully applied". Returns applied / partial:<why> / failed:<why>."""
    pg = b.page
    labels = RESUME_DONE + ("Add missing details", "Skip this job")
    baseline = {l: _count(pg, l) for l in labels}
    success0 = len(re.findall(r"Successfully applied to", pg.inner_text("body"), re.I))
    btn = _panel_button(pg, "Apply with custom Resume") or _panel_button(pg, "Apply now")
    if btn is None:
        return "failed:no apply button in Neo's panel"
    used = btn.inner_text().strip()
    _click(btn)
    log.info("  clicked %r", used)
    # "Apply now" has no resume step: Neo goes straight to the recruiter's questions
    # (16 Oct 2026: Infosys/Indium failed after 4 min waiting for resume buttons
    # while "Add missing details" was on screen)
    step, answered, answered_at = ("resume" if used == "Apply with custom Resume" else "questions"), None, 0.0
    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(3)
        body = pg.inner_text("body")
        if any(t.lower() in body[-1500:].lower() for t in S.DAILY_QUOTA_TEXT):
            raise QuotaReached()
        if len(re.findall(r"Successfully applied to", body, re.I)) > success0:
            log.info("  Neo: successfully applied")
            break
        if step == "resume":
            # the resume step is over when THIS job's "Use original Resume" appears (with "Apply with tailored
            # Resume" next to it when tailoring worked); take the tailored one when offered
            if new_button(pg, "Add missing details", baseline) is not None:
                step = "questions"   # Neo skipped tailoring and went straight to questions
                continue
            if new_button(pg, "Use original Resume", baseline) is None and new_button(pg, "Apply with tailored Resume", baseline) is None:
                continue
            time.sleep(2)
            el = new_button(pg, "Apply with tailored Resume", baseline)
            choice = "Apply with tailored Resume"
            if el is None:
                el, choice = new_button(pg, "Use original Resume", baseline), "Use original Resume"
            _click(el); step = "questions"
            log.info("  resume ready -> clicked %r", choice)
            continue
        if step == "questions":
            el = new_button(pg, "Add missing details", baseline)
            if el is None:
                continue
            _click(el); step = "answering"
            log.info("  clicked 'Add missing details'")
            handler = DrawerHandler(b, llm, cache, tracker, job, assist=False)
            if handler.wait_for_drawer(timeout_s=10):
                answered = handler.run()
                log.info("  questions: %d answered, completed=%s %s", len(answered.answered), answered.completed, answered.notes[:80])
            answered_at = time.time()
            continue
        if step == "answering" and time.time() > answered_at + 45:
            break        # answered: give Neo 45 s from the last answer to post its result (Cimpress, 21 Oct 2026: the
                         # old fixed deadline had already passed during a slow resume step), then confirm
    # confirm on the job page: "Applied" replaces the Apply button
    with Side(b):
        try:
            b.goto(job.url)
            ok = detect_apply_mode(b) == ApplyMode.ALREADY_APPLIED
        except Exception:
            ok = False
    if ok and answered is not None and (not answered.completed or answered.notes.startswith("needs model")):
        return "partial:" + answered.notes[:120]
    return "applied" if ok else f"failed:not confirmed on the job page (stopped at step '{step}')"


def main() -> None:
    settings, profile, tracker, cache = Settings(), load_profile(), Tracker(), AnswerCache()
    # Neo suggests posts up to a few weeks old; the 1-day freshness rule is for search results, not Neo's picks
    # (14 Oct 2026: every Neo card was skipped as too_old)
    import dataclasses
    settings = dataclasses.replace(settings, max_job_age_days=max(settings.max_job_age_days, 30))
    llm = None
    try:
        llm = make_llm(settings)                 # open questions in Neo's drawer need the model too
    except Exception as e:
        log.warning("no model available (%s): open questions will be left for you", str(e)[:80])
    pending = _load_pending()
    from naukri_bot.cooldown import paused_until
    until = paused_until("naukri")
    if until and not dry:
        log.warning("Naukri cooldown until %s: Neo run skipped (%d pending kept)", until, len(pending))
        return
    applied = 0
    with Browser(settings) as b:
        b.ensure_logged_in()
        pg = b.page
        ids: dict[str, str] = {}                 # card title -> job id, from Neo's agent responses

        def on(r):
            if "agent-response" in r.url or "jobapi/v4/job/" in r.url:
                try:
                    found = []
                    _jobs_in(r.json(), found)
                    for t, j in found:
                        ids.setdefault(t.lower(), j)
                except Exception:
                    pass
        pg.on("response", on)
        pg.goto(NEO, wait_until="domcontentloaded", timeout=60000)
        time.sleep(12)
        done_ids: set[str] = set()
        rounds = 0
        try:
            while applied < MAX and rounds < len(ASKS):
                try:
                    n = ask_neo(pg, ASKS[rounds]); rounds += 1
                except Exception as e:
                    log.warning("Neo: could not ask for more jobs (%s); stopping Neo for this run", str(e)[:120])
                    break
                titles = [" ".join(t.split()) for t in pg.locator(CARD).all_inner_texts()]
                log.info("Neo round %d: %d cards %s", rounds, n, titles[-8:])
                progress = False
                for i, title in enumerate(titles):
                    if applied >= MAX:
                        break
                    if ONLY and not ONLY.search(title):
                        continue
                    close_panel(pg)
                    card = pg.locator(CARD).nth(i)
                    try:
                        card.scroll_into_view_if_needed(timeout=5000); card.click(); time.sleep(5)
                    except Exception as e:
                        log.info("  could not open card %r: %s", title, str(e)[:60]); continue
                    jid = ids.get(title.lower())
                    if not jid:
                        log.info("  %s: no job id seen yet", title); continue
                    if jid in done_ids:
                        continue
                    done_ids.add(jid); progress = True
                    url = f"https://www.naukri.com/job-listings-{jid}"
                    with Side(b):
                        job = scrape_job(b, url)
                        mode = detect_apply_mode(b)
                    log.info("%s | %s | %s | %s", job.title or title, job.company, job.experience, mode.value)
                    prev = (tracker.seen.get(job.job_id or jid) or {}).get("status", "")
                    if mode == ApplyMode.ALREADY_APPLIED or prev in SUBMITTED_STATUSES:
                        log.info("  already applied"); pending.pop(jid, None); continue
                    # Neo has matched the job to the profile: no key-skill tick or search-keyword title list needed;
                    # excluded titles (senior, lead, ...) and the experience fit still apply
                    reason = eligibility(job, mode, settings, {**profile, "require_match": [], "include_title_keywords": []})
                    # the search-keyword list is relaxed for Neo, but the title must still be a technical role
                    # (Okta "Enterprise Account Executive" was applied to, 15 Oct 2026)
                    if not reason and not TECH_TITLE.search(job.title or title):
                        reason = "not_a_tech_title"
                    if reason in ("excluded_title", "title_not_in_include_list", "not_a_tech_title") and FORCE:
                        log.info("  title filter (%s) overridden: picked by Ashu", reason); reason = None
                    if reason:
                        log.info("  skip: %s", reason); pending.pop(jid, None); continue
                    if mode != ApplyMode.NAUKRI:
                        log.info("  apply mode %s: left for the company-site flow", mode.value); pending.pop(jid, None); continue
                    if dry:
                        log.info("  (dry run) would apply with Neo's custom resume"); continue
                    try:
                        status = neo_apply_one(b, llm, cache, tracker, job)
                    except QuotaReached:
                        raise
                    except Exception as e:                 # one job's trouble must not end the whole Neo run
                        status = f"failed:{type(e).__name__}: {str(e).splitlines()[0][:120]}"
                    kind, _, note = status.partition(":")
                    tracker.record_application(job, "neo_custom_resume", {"partial": "applied_partial"}.get(kind, kind), notes=note[:200])
                    log.info("  -> %s %s", kind, note[:100])
                    if kind in ("applied", "partial"):
                        applied += 1; pending.pop(jid, None)
                    else:
                        pending.setdefault(jid, {"title": title, "first_seen": time.time()})
                        b.save_debug(f"neo-{kind}-{jid}")
                    close_panel(pg)
                if not progress and rounds > 1:
                    break
            # jobs kept from earlier runs, or passed on the command line: the bot's own Naukri apply
            extra = [] if ONLY else [a for a in sys.argv[1:] if a.isdigit() and a != str(MAX)] + [j for j in list(pending) if j not in done_ids]
            for jid in extra:
                if applied >= MAX or dry:
                    break
                job = scrape_job(b, f"https://www.naukri.com/job-listings-{jid}")
                mode = detect_apply_mode(b)
                if mode == ApplyMode.ALREADY_APPLIED:
                    pending.pop(jid, None); continue
                if eligibility(job, mode, settings, {**profile, "require_match": [], "include_title_keywords": []}) or mode != ApplyMode.NAUKRI:
                    pending.pop(jid, None); continue
                if handle_naukri_apply(b, llm, cache, job, tracker, dry_run=False, assist=False):
                    applied += 1; pending.pop(jid, None)
        except QuotaReached:
            log.warning("Naukri daily limit reached")
        finally:
            pg.remove_listener("response", on)
    if not dry:
        json.dump(pending, open(PENDING, "w"), indent=1)
    log.info("Neo: applied %d (%d kept for the next run)", applied, len(pending))


if __name__ == "__main__":
    main()
