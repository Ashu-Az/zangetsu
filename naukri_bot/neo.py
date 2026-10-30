"""Naukri Neo (naukri.com/job-apply-agent): the AI agent's job suggestions, applied to one by one.

Neo shows suggestion cards ("AI Software Engineer | Doctutorials | Hyderabad | 2-7 Yrs | 10-18 Lacs PA").
Clicking a card's title opens its details (applicant count, must-have skills) with its own "Apply now".
Every card goes through the same filters as search (title, company, experience, salary); a card that fails is
left alone, and when nothing on the page fits, "I don't like these jobs" asks Neo for a different set.

Apply flow (18 Oct 2026, Ashu): Apply now -> "Tailor Resume first" when Neo offers it (else the original resume)
-> "Add missing details" -> the recruiter's questions in Neo's drawer, answered by the rules and then the model
(DrawerHandler) -> "Successfully applied to <company>".
"""
from __future__ import annotations

import logging
import re
import time

from .models import JobPosting

log = logging.getLogger(__name__)

URL = "https://www.naukri.com/job-apply-agent"
TITLE = "[class*=job-tuple-title]"
EXP = re.compile(r"^\d+\s*-\s*\d+\s*Yrs?$", re.I)


def read_cards(pg) -> list[dict]:
    """Suggestion cards on the page: title, company, location, experience, salary (from the card's text)."""
    raw = pg.evaluate("""() => [...document.querySelectorAll('[class*=job-tuple-title]')].map(t => {
        let c = t; for (let i = 0; i < 6 && c.parentElement; i++) { c = c.parentElement; if ((c.innerText || '').match(/Yrs/)) break; }
        return [t.innerText.trim(), (c.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean)]; })""")
    out, seen = [], set()
    for title, lines in raw:
        if title in seen:
            continue
        seen.add(title)
        i = lines.index(title) if title in lines else 0
        rest = lines[i + 1:]
        company = rest[0] if rest else ""
        exp = next((l for l in rest if EXP.match(l)), "")
        loc = rest[rest.index(exp) - 1] if exp and rest.index(exp) > 0 else ""
        sal = next((l for l in rest if re.search(r"lacs|lpa|not disclosed", l, re.I)), "")
        out.append({"title": title, "company": company, "location": loc, "experience": exp.replace("Yrs", "Years"),
                    "salary": sal or "Not disclosed"})
    return out


def _posting(card: dict) -> JobPosting:
    key = re.sub(r"[^a-z0-9]+", "-", f"{card['company']} {card['title']}".lower()).strip("-")
    return JobPosting(job_id="neo:" + key[:120], url=URL, title=card["title"], company=card["company"],
                      experience=card["experience"], salary=card["salary"], location=card["location"])


def _click_text(pg, text: str, timeout: int = 5000) -> bool:
    loc = pg.get_by_text(text, exact=True)
    for k in range(loc.count() - 1, -1, -1):          # the newest message is last
        el = loc.nth(k)
        try:
            if el.is_visible():
                el.click(timeout=timeout); return True
        except Exception:
            continue
    return False


def _wait_for(pg, rx: str, timeout_s: float = 25, tail: int = 0) -> str | None:
    """First match of rx in the page; with tail, only in the last `tail` characters (the newest chat messages,
    so an older card's "Apply now" further up does not count: PwC, 30 Oct 2026)."""
    end = time.time() + timeout_s
    while time.time() < end:
        body = pg.inner_text("body")
        m = re.search(rx, body[-tail:] if tail else body, re.I)
        if m:
            return m.group(0)
        time.sleep(1.5)
    return None


def apply_card(b, card: dict, handler_factory) -> str:
    """Apply to one card. Returns 'applied' | 'failed:<why>'."""
    pg = b.page
    before = pg.inner_text("body").count("Successfully applied")
    t = pg.locator(TITLE, has_text=card["title"]).last          # newest copy of the card
    try:
        t.scroll_into_view_if_needed(timeout=5000); t.click(timeout=5000)
    except Exception:                                            # cards in a sideways carousel: click in the page
        t.evaluate("e => e.click()")
    time.sleep(4)
    if not _click_text(pg, "Apply now"):
        tail = pg.inner_text("body")[-300:].replace("\n", " | ")
        if re.search(r"\bApplied\b|already applied", tail, re.I):
            return "failed:already applied"
        b.save_debug("neo-no-apply")
        return "failed:no Apply now (" + tail[-120:] + ")"
    step = _wait_for(pg, r"Tailor Resume first|Apply with original Resume|Add missing details|Successfully applied", 20, tail=1500)
    if step and "Tailor Resume first" in step:
        _click_text(pg, "Tailor Resume first ✨") or _click_text(pg, "Tailor Resume first")
        # Neo writes the tailored resume, then offers to apply with it
        nxt = _wait_for(pg, r"Apply with (tailored|customi[sz]ed|updated|new) Resume|Add missing details|Successfully applied", 90, tail=1500)
        if nxt and re.match(r"Apply with", nxt):
            _click_text(pg, nxt)
        elif nxt is None:
            log.info("    tailored resume did not arrive; applying with the original")
            _click_text(pg, "Apply with original Resume")
    elif step and "original" in step:
        _click_text(pg, "Apply with original Resume")
    step = _wait_for(pg, r"Add missing details|Successfully applied", 30, tail=1500)
    if step and "Add missing details" in step:
        _click_text(pg, "Add missing details"); time.sleep(4)
        handler = handler_factory(_posting(card))
        res = handler.run()
        log.info("    answered %d question(s)", len(res.answered))
    # the confirmation must name this company and be among the newest messages: older chat history loading in
    # raised the plain "Successfully applied" count and logged Tuteck as applied when it was not (30 Oct 2026)
    # Naukri may name the posting consultancy instead ("Successfully applied to Glauben Technologies" under an
    # Infosys card, 30 Oct 2026), so the card's company or its title must follow the confirmation
    co, ti = re.escape(card["company"][:25]), re.escape(card["title"][:30])
    if _wait_for(pg, r"Successfully applied to[\s\S]{0,300}?(" + co + "|" + ti + ")", 25, tail=2500) \
            and pg.inner_text("body").count("Successfully applied") > before:
        return "applied"
    b.save_debug("neo-not-confirmed")
    return "failed:no 'Successfully applied' message"


def run(b, settings, profile, tracker, handler_factory, max_apps: int, rounds: int = 6) -> int:
    from .ranking import eligibility
    from .models import ApplyMode
    pg = b.page
    b.goto(URL, settle=(6, 8))
    if re.search(r"access denied|you don.t have permission to access", pg.inner_text("body"), re.I):
        # Akamai bot block (19 Oct 2026): stop touching Naukri for a while instead of retrying into a longer ban
        from .cooldown import pause
        pause("naukri", 8, "Akamai 'Access Denied' on Neo (bot detection)")
        log.error("Naukri returned Access Denied (bot block); Naukri paused 8 h.")
        return 0
    # the chat renders a few seconds after load: wait for cards or one of Neo's openers
    _wait_for(pg, r"Suggestions for you|No new jobs|Show missed jobs|Apply now|Apply to all", 20)
    applied = 0
    recent = tracker.recent_role_keys(days=60, include_saved=False)   # applied elsewhere already (Easemytrip, 11 Oct)
    for rnd in range(rounds):
        cards = read_cards(pg)
        if not cards:
            # Neo's openers when it has no cards (19 Oct 2026): "Show missed jobs" after "No new jobs at the moment",
            # or the "Welcome back!" suggestion chips ("Show me latest Machine Learning Engineer openings")
            body = pg.inner_text("body")
            chip = next((m.group(0).strip() for rx in (r"Show missed jobs", r"Show me latest [^\n|]+? openings", r"Find jobs matching my profile",
                                                         r"Find me [^\n|]+? jobs")
                         for m in [re.search(rx, body)] if m), None)
            if chip and _click_text(pg, chip):
                log.info("  asked Neo: %s", chip)
                end = time.time() + 90                       # cards took ~50 s to arrive (19 Oct 2026)
                while time.time() < end and not pg.locator(TITLE).count() and \
                        not re.search(r"No new jobs|couldn.t find", pg.inner_text("body")):
                    time.sleep(3)
                last = -1                                    # cards stream in one by one: wait until the count settles
                for _ in range(10):
                    n = pg.locator(TITLE).count()
                    if n == last:
                        break
                    last = n; time.sleep(4)
                cards = read_cards(pg)
                if not cards:
                    log.info("  Neo said: %s", pg.inner_text("body")[-500:].replace("\n", " | "))
        log.info("Neo round %d: %d suggestion(s)", rnd + 1, len(cards))
        fits = []
        for c in cards:
            job = _posting(c)
            why = ("already handled" if tracker.is_seen(job.job_id) else
                   "already applied (same company + title)" if tracker.role_key(c["company"], c["title"]) in recent else
                   eligibility(job, ApplyMode.NAUKRI, settings, profile))
            log.info("  %-45s | %-28s | %-10s -> %s", c["title"][:45], c["company"][:28], c["experience"], why or "fits")
            if why:
                if why not in ("already handled",) and not why.startswith("already applied"):
                    tracker.mark_seen(job.job_id, why)
                continue
            fits.append(c)
        for c in fits:
            if applied >= max_apps:
                return applied
            log.info("  applying: %s — %s", c["title"], c["company"])
            try:
                result = apply_card(b, c, handler_factory)
            except Exception as e:
                result = f"failed:{str(e)[:80]}"
            job = _posting(c)
            tracker.record_application(job, "neo", "applied" if result == "applied" else "failed",
                                       notes="" if result == "applied" else result)
            log.info("    -> %s", result)
            applied += result == "applied"
            b.random_idle()
        if fits:
            continue
        # nothing here fits: ask Neo for a different set
        if not _click_text(pg, "I don't like these jobs") and not _click_text(pg, "I don't like this job"):
            break
        time.sleep(10)
    return applied
