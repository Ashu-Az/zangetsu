"""CLI entry point.

  python -m naukri_bot login                 # open the browser, log in once (cookies persist)
  python -m naukri_bot run [--max N] [--dry-run] [--only naukri|company|all] [--survey-only]
  python -m naukri_bot scrape <job_url>      # print what the bot sees on a job page
  python -m naukri_bot tailor <job_url>      # build a tailored resume for one job
  python -m naukri_bot ask "question" [--options "A,B,C"]   # test the answer model
"""
from __future__ import annotations

import random
import argparse
from pathlib import Path
import json
import logging
import re
import sys

from playwright.sync_api import TimeoutError as PWTimeout

from . import selectors as S
from .answers_store import AnswerCache
from .browser import Browser
from .config import OUTPUT_DIR, Settings, load_profile, load_resume_markdown, profile_is_example
from .humanize import pause, scroll_like_reader
from .job_page import detect_apply_mode, scrape_job
from .llm import LLM
from .models import ApplyMode, JobPosting
from .questionnaire import DrawerHandler
from .ranking import SurveyCache, company_excluded, dedupe_company_title, eligibility, sort_key, title_excluded, title_included, too_old
from .resume_builder import build_resume_files
from .search import build_search_urls, collect_cards, collect_recommended
from .tracker import Tracker

log = logging.getLogger("naukri_bot")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


def llm_available() -> bool:
    """Credentials the Anthropic SDK can use: an API key / auth token, or an `ant auth login` profile."""
    import os
    from pathlib import Path
    if os.getenv("ANTHROPIC_API_KEY", "").strip() or os.getenv("ANTHROPIC_AUTH_TOKEN", "").strip() or os.getenv("GROQ_API_KEY", "").strip():
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()


def make_llm(settings: Settings) -> LLM:
    if profile_is_example():
        log.warning("Using profile/*.example.* files. Copy them to profile/profile.yaml and profile/resume.md and fill in YOUR details.")
    return LLM(settings, load_profile(), load_resume_markdown())


# ------------------------------------------------------------------ company-site flow
def capture_external_url(browser: Browser) -> str:
    """Click 'Apply on company site' and return the employer URL it opens (new tab or same tab)."""
    page = browser.page
    before = page.url
    try:
        with page.context.expect_page(timeout=12000) as popup_info:
            browser.click_if_visible(S.COMPANY_SITE_BUTTON)
        new_page = popup_info.value
        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=20000)
            pause(2, 3)
        except PWTimeout:
            pass
        url = new_page.url
        new_page.close()
        return url
    except PWTimeout:
        pause(2, 3)
        if page.url != before:
            url = page.url
            page.go_back(wait_until="domcontentloaded")
            return url
        return ""


def handle_company_site(browser: Browser, llm: LLM, job: JobPosting, tracker: Tracker, dry_run: bool, tailor: bool) -> None:
    external = "" if dry_run else capture_external_url(browser)
    log.info("  company site URL: %s", external or "(not captured)")
    files, fit_score, fit_notes = {}, "", ""
    if tailor and not dry_run and llm is not None:
        try:
            tailored = llm.tailor_resume(job)
            files = build_resume_files(tailored, job, playwright=browser.pw)
            fit_score, fit_notes = tailored.fit_score, tailored.fit_notes
            log.info("  fit %s/100 — %s", fit_score, fit_notes)
        except Exception as e:      # keep the row and the captured URL; the resume can be built later with `tailor`
            log.warning("  resume tailoring failed: %s", e)
            fit_notes = f"tailoring failed: {str(e)[:150]}"
    tracker.record_company_site(job, external, fit_score, fit_notes, files,
                                status="dry_run" if dry_run else "pending_manual")
    tracker.record_application(job, ApplyMode.COMPANY_SITE.value, "saved_for_manual" if not dry_run else "dry_run",
                               notes=external, resume_pdf=files.get("pdf", ""))


# ------------------------------------------------------------------ naukri apply flow
def handle_naukri_apply(browser: Browser, llm: LLM, cache: AnswerCache, job: JobPosting, tracker: Tracker, dry_run: bool, assist: bool = False) -> bool:
    if dry_run:
        tracker.record_application(job, ApplyMode.NAUKRI.value, "dry_run")
        return False
    # Naukri refuses applies past its cap with a silent HTTP 403 from the apply API: the button stays "Apply"
    # and no message appears (verified 9 Oct 2026). Watch the response so we stop instead of hammering it.
    refused: list[int] = []

    def on_response(resp):
        if "apply-workflow" in resp.url and "/apply" in resp.url and resp.status in (403, 429):
            refused.append(resp.status)

    browser.page.on("response", on_response)
    try:
        return _apply_and_confirm(browser, llm, cache, job, tracker, assist, refused)
    finally:
        browser.page.remove_listener("response", on_response)


def _refused(job: JobPosting, tracker: Tracker, refused: list[int]) -> None:
    from .cooldown import pause as cooldown
    cooldown("naukri", 12, f"apply API refused with HTTP {refused[0]} (daily cap or throttle)")
    tracker.record_application(job, ApplyMode.NAUKRI.value, "quota",
                               notes=f"Naukri refused the apply (HTTP {refused[0]}); will retry after cooldown", remember=False)
    raise QuotaReached()


def _apply_and_confirm(browser: Browser, llm: LLM, cache: AnswerCache, job: JobPosting, tracker: Tracker,
                       assist: bool, refused: list[int]) -> bool:
    if not browser.click_if_visible(S.APPLY_BUTTON):
        tracker.record_application(job, ApplyMode.NAUKRI.value, "failed", notes="apply button vanished")
        return False
    pause(2, 3)
    if refused:
        _refused(job, tracker, refused)
    if browser.text_present(S.DAILY_QUOTA_TEXT):
        browser.save_debug(f"naukri-quota-{job.job_id}")
        tracker.record_application(job, ApplyMode.NAUKRI.value, "quota", notes="daily quota reached", remember=False)
        raise QuotaReached()

    handler = DrawerHandler(browser, llm, cache, tracker, job, assist=assist)
    if handler.wait_for_drawer(timeout_s=8):
        log.info("  questionnaire drawer opened")
        res = handler.run()
        pause(1.5, 2.5)
        # Naukri registers the application on the Apply click; the questions are supplementary.
        applied = confirm_applied(browser, job)
        if applied and (res.notes.startswith("needs model") or not res.completed):
            status = "applied_partial"       # application is in, questionnaire incomplete
        else:
            status = "applied" if applied else "failed"
        if status != "applied":
            browser.save_debug(f"naukri-{status}-{job.job_id}")
        tracker.record_application(job, ApplyMode.QUESTIONNAIRE.value, status,
                                   questions_answered=len(res.answered), notes=res.notes)
        return applied

    applied = confirm_applied(browser, job)
    if not applied and refused:
        _refused(job, tracker, refused)
    if not applied:
        browser.save_debug(f"naukri-failed-{job.job_id}")
    tracker.record_application(job, ApplyMode.NAUKRI.value, "applied" if applied else "failed",
                               notes="" if applied else "no success confirmation")
    return applied


class QuotaReached(Exception):
    pass


def confirm_applied(browser: Browser, job: JobPosting) -> bool:
    """Naukri replaces the Apply button with 'Applied' once an application exists. Reload and check."""
    if browser.text_present(S.APPLY_SUCCESS_TEXT):
        return True
    try:
        browser.goto(job.url, settle=(2.5, 3.5))
        return detect_apply_mode(browser) == ApplyMode.ALREADY_APPLIED
    except Exception:
        return False


# ------------------------------------------------------------------ commands
def cmd_login(settings: Settings, args) -> int:
    settings.login_wait_seconds = max(settings.login_wait_seconds, 600)   # a first login with OTP takes a while
    with Browser(settings) as b:
        ok = b.ensure_logged_in()
        log.info("Logged in: %s", ok)
        return 0 if ok else 1


def cmd_scrape(settings: Settings, args) -> int:
    with Browser(settings) as b:
        job = scrape_job(b, args.url)
        mode = detect_apply_mode(b)
        print(json.dumps(job.as_dict(), indent=2, ensure_ascii=False))
        print("apply mode:", mode.value)
    return 0


def cmd_tailor(settings: Settings, args) -> int:
    llm = make_llm(settings)
    with Browser(settings) as b:
        job = scrape_job(b, args.url)
        tailored = llm.tailor_resume(job)
        files = build_resume_files(tailored, job, playwright=b.pw)
    print(json.dumps({"fit_score": tailored.fit_score, "fit_notes": tailored.fit_notes, **files}, indent=2))
    return 0


def cmd_ask(settings: Settings, args) -> int:
    llm = make_llm(settings)
    options = [o.strip() for o in (args.options or "").split(",") if o.strip()]
    kind = "radio" if options else "text"
    ans = llm.answer_question(args.question, options, kind, None, [])
    print(ans.model_dump_json(indent=2))
    return 0


def survey_jobs(b: Browser, cards, settings: Settings, profile: dict, tracker: Tracker, survey: SurveyCache):
    """Phase 1: open every fresh job once, record posting + apply mode, decide eligibility."""
    eligible, skipped = [], 0
    recent_roles = tracker.recent_role_keys()
    for i, card in enumerate(cards, 1):
        if title_excluded(card.title, profile) or not title_included(card.title, profile):
            log.info("  [%d/%d] title filtered: %s — %s", i, len(cards), card.title[:60], card.company[:25])
            tracker.mark_seen(card.job_id, "title_filtered")
            skipped += 1
            continue
        if too_old(card.posted, settings):
            tracker.mark_seen(card.job_id, "too_old")
            skipped += 1
            continue
        if company_excluded(card.company, profile):
            tracker.mark_seen(card.job_id, "excluded_company")
            skipped += 1
            continue
        cached = survey.get(card.job_id)
        if cached:
            job, mode = cached
        else:
            try:
                job = scrape_job(b, card.url)
                mode = detect_apply_mode(b)
            except Exception as e:
                log.warning("  [%d/%d] could not load %s (%s)", i, len(cards), card.url, e.__class__.__name__)
                continue
            if not job.title or mode == ApplyMode.UNKNOWN:
                # half-loaded page, login wall or bot check: retry next run instead of dropping the job
                log.warning("  [%d/%d] page did not load properly (%s); will retry next run", i, len(cards), card.url)
                b.save_debug(f"naukri-unloaded-{card.job_id}")
                continue
            survey.put(job, mode)
            pause(1.0, 2.5)
        reason = eligibility(job, mode, settings, profile)
        if not reason and Tracker.role_key(job.company, job.title) in recent_roles:
            reason = "same_role_applied_recently"
        if not reason and card.source == "recommended":
            # Naukri's recommendations are broad: take only full matches (all 4 ticks green)
            need = profile.get("require_match_recommended") or []
            if job.match and any(not job.match.get(k.lower()) for k in need):
                reason = "recommended_not_full_match"
            elif not job.match and need:
                reason = "recommended_no_match_score"
        ticks = "".join("✓" if v else "✗" for v in job.match.values()) or "----"
        log.info("  [%d/%d] %-6s match=%s applicants=%-5s %-14s %s — %s%s", i, len(cards), mode.value[:6], ticks,
                 job.applicants if job.applicants is not None else "?", job.posted.replace("Posted:", "").strip()[:14],
                 job.title[:40], job.company[:22],
                 f"  -> skip ({reason})" if reason else "")
        if reason:
            if not reason.startswith("too_many_applicants"):   # applicant counts change; re-check next run
                tracker.mark_seen(job.job_id, reason)
            skipped += 1
            continue
        eligible.append((job, mode))
    # one-click Naukri jobs first (the bot applies to those), then company-site jobs; fewest applicants first within each
    eligible.sort(key=lambda jm: (jm[1] == ApplyMode.COMPANY_SITE, sort_key(jm[0], settings)))
    before = len(eligible)
    eligible = dedupe_company_title(eligible)
    if before != len(eligible):
        log.info("  dropped %d repeat postings (same company and title)", before - len(eligible))
    return eligible, skipped


def write_ranked_csv(eligible, path):
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "match", "applicants", "posted", "salary", "mode", "title", "company", "experience", "location", "url"])
        for i, (job, mode) in enumerate(eligible, 1):
            w.writerow([i, f"{job.match_count()}/{len(job.match)}" if job.match else "", job.applicants if job.applicants is not None else "", job.posted.replace("Posted:", "").strip(), job.salary, mode.value, job.title,
                        job.company, job.experience, job.location, job.url])


def cmd_run(settings: Settings, args) -> int:
    if args.keywords:
        settings.keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not settings.keywords:
        log.error("No KEYWORDS configured (.env) and none passed with --keywords.")
        return 1
    if args.max_applicants:
        settings.max_applicants = args.max_applicants
        settings.max_applicants_direct = args.max_applicants      # the cap Naukri direct-apply jobs are checked against
    max_apps = args.max if args.max is not None else settings.max_applications
    from .cooldown import paused_until
    until = paused_until("naukri")
    # company-site jobs only click "Apply on company site" (no Naukri apply API, no quota), so they run during a pause
    if until and not args.dry_run and args.only != "company":
        log.error("Naukri cooldown until %s; not applying.", until)
        return 0
    done_today = Tracker().applied_last_24h()     # Naukri's cap is a rolling 24 h window
    remaining = max(settings.daily_quota - settings.priority_reserve - done_today, 0)
    if remaining < max_apps:
        log.info("Daily quota %d (%d kept for Neo/NVites), applied in the last 24 h %d -> this run may apply to %d more.",
                 settings.daily_quota, settings.priority_reserve, done_today, remaining)
        max_apps = remaining
    profile = load_profile()
    if profile_is_example() and not (args.dry_run or args.survey_only):
        log.error("profile/profile.yaml or profile/resume.md is missing: the bot would answer recruiters with the "
                  "EXAMPLE candidate's facts. Create them first (see README > Setup).")
        return 1
    applying = not (args.dry_run or args.survey_only)
    no_model = args.no_llm or not llm_available()
    if applying and no_model and not (args.allow_partial or args.assist):
        log.error("No model available (%s). Naukri registers an application on the Apply click, so questionnaire "
                  "jobs would be submitted half-answered. Set ANTHROPIC_API_KEY, or pass --allow-partial to accept that.",
                  "--no-llm" if args.no_llm else "no ANTHROPIC_API_KEY / credentials found")
        return 1
    llm = None if (not applying or no_model) else make_llm(settings)
    if applying and no_model:
        log.warning("Running WITHOUT a model (--allow-partial): questionnaire jobs may be submitted with unanswered questions.")
    cache = AnswerCache()
    tracker = Tracker(read_only=args.dry_run)
    survey = SurveyCache(ttl_hours=settings.survey_cache_hours)
    stats = {"applied": 0, "company_site": 0, "skipped": 0, "failed": 0}
    unconfirmed_streak = 0

    with Browser(settings) as b:
        if not b.ensure_logged_in():
            return 1
        cards = [] if args.source == "recommended" else collect_cards(b, build_search_urls(settings))
        if args.source in ("all", "recommended"):
            seen_ids = {c.job_id for c in cards}
            reco = collect_recommended(b)
            for c in reco:                  # a recommended job keeps its stricter rule even if search also found it
                if c.job_id in seen_ids:
                    next(x for x in cards if x.job_id == c.job_id).source = "recommended"
            cards += [c for c in reco if c.job_id not in seen_ids]
        fresh = [c for c in cards if not tracker.is_seen(c.job_id)]
        log.info("%d jobs found, %d not processed before", len(cards), len(fresh))

        log.info("=" * 60)
        log.info("PHASE 1: surveying %d jobs (title / experience / applicant filters)", len(fresh))
        eligible, stats["skipped"] = survey_jobs(b, fresh, settings, profile, tracker, survey)
        ranked_path = OUTPUT_DIR / "ranked_jobs.csv"
        write_ranked_csv(eligible, ranked_path)
        log.info("%d eligible jobs, ordered by applicants ascending -> %s", len(eligible), ranked_path)
        for i, (job, mode) in enumerate(eligible[:15], 1):
            log.info("  %2d. %-5s %-14s %-13s %s — %s", i, job.applicants if job.applicants is not None else "?",
                     job.posted.replace("Posted:", "").strip()[:14], mode.value, job.title[:42], job.company[:22])
        if args.survey_only:
            return 0

        log.info("=" * 60)
        log.info("PHASE 2: applying (max %d Naukri applications, %d company-site resumes)",
                 max_apps, settings.max_company_site_resumes)
        try:
            for n, (job, mode) in enumerate(eligible, 1):
                if stats["applied"] >= max_apps and (args.only == "naukri" or stats["company_site"] >= settings.max_company_site_resumes):
                    log.info("Limits reached.")
                    break
                log.info("[%d/%d] applicants=%s %s — %s", n, len(eligible),
                         job.applicants if job.applicants is not None else "?", job.title, job.company)
                try:
                    b.goto(job.url)
                    scroll_like_reader(b.page, steps=2)
                    mode = detect_apply_mode(b)
                except Exception as e:
                    log.warning("  could not load job: %s", e)
                    stats["failed"] += 1
                    continue
                log.info("  mode: %s", mode.value)

                if mode == ApplyMode.UNKNOWN:
                    b.save_debug(f"naukri-unknown-{job.job_id}")
                    tracker.record_application(job, mode.value, "skipped", notes="page state unknown; retry next run", remember=False)
                    stats["skipped"] += 1
                elif mode in (ApplyMode.ALREADY_APPLIED, ApplyMode.EXPIRED):
                    tracker.record_application(job, mode.value, "skipped")
                    stats["skipped"] += 1
                elif mode == ApplyMode.COMPANY_SITE:
                    if args.only == "naukri" or stats["company_site"] >= settings.max_company_site_resumes:
                        log.info("  company-site job left for a later run")
                        stats["skipped"] += 1
                    else:
                        try:
                            handle_company_site(b, llm, job, tracker, args.dry_run, tailor=not args.no_tailor)
                            stats["company_site"] += 1
                        except Exception as e:
                            log.exception("  company-site handling failed: %s", e)
                            tracker.record_application(job, mode.value, "failed", notes=str(e)[:200])
                            stats["failed"] += 1
                else:
                    if args.only == "company" or stats["applied"] >= max_apps:
                        stats["skipped"] += 1
                        continue
                    try:
                        ok = handle_naukri_apply(b, llm, cache, job, tracker, args.dry_run, assist=args.assist)
                        stats["applied" if ok else "failed"] += 1
                        unconfirmed_streak = 0 if ok else unconfirmed_streak + 1
                        if unconfirmed_streak >= 3 and not args.dry_run:
                            # Silent refusals look exactly like this; don't keep clicking a site that says no.
                            from .cooldown import pause as cooldown
                            cooldown("naukri", 6, "3 applies in a row without confirmation")
                            log.warning("3 Naukri applies in a row were not confirmed; stopping and pausing Naukri 6 h.")
                            break
                    except QuotaReached:
                        # no more Naukri applies today, but company-site jobs later in the list still go ahead
                        log.warning("Naukri daily application quota reached; no more Naukri applies this run.")
                        max_apps = stats["applied"]
                    except Exception as e:
                        log.exception("  apply failed: %s", e)
                        tracker.record_application(job, mode.value, "failed", notes=str(e)[:200])
                        stats["failed"] += 1
                b.random_idle()
        except KeyboardInterrupt:
            log.warning("Interrupted by user.")

    log.info("=" * 60)
    log.info("applied: %(applied)d | company-site saved: %(company_site)d | skipped: %(skipped)d | failed: %(failed)d", stats)
    log.info("applications.csv / company_site_jobs.csv / answers.csv / ranked_jobs.csv are in output/, resumes in output/resumes/")
    return 0


def cmd_profile(settings: Settings, args) -> int:
    from . import naukri_profile
    sections = args.sections or ["headline", "skills", "summary"]
    with Browser(settings) as b:
        if not b.ensure_logged_in():
            return 1
        results = naukri_profile.run(b, sections, new_only=args.new_only)
    for k, v in results.items():
        log.info("%-9s %s", k, "OK" if v else "FAILED")
    return 0 if all(results.values()) else 1


def cmd_leads(settings: Settings, args) -> int:
    """Read recent LinkedIn hiring posts, save relevant ones and write outreach drafts (nothing is sent)."""
    from .config import STATE_DIR
    from .portals import linkedin_posts as LP
    from .cooldown import paused_until
    until = paused_until("linkedin")
    # Ashu paused LinkedIn *applying* (a preference); reading posts is still wanted (8 Oct 2026). Safety cooldowns still block.
    if until and "paused by Ashu" not in until:
        log.error("LinkedIn cooldown until %s; not searching.", until)
        return 1
    profile = load_profile()
    Settings.browser_profile_dir = property(lambda self: STATE_DIR / "linkedin-profile")
    queries = [q.strip() for q in args.queries.split(",")] if args.queries else LP.default_queries(profile)
    with Browser(settings) as b:
        leads = LP.collect(b.page, queries, profile)
    new = LP.save(leads)
    path = LP.write_drafts(new, profile)
    log.info("%d relevant hiring posts, %d new (%d with an email address) -> %s",
             len(leads), len(new), sum(1 for l in new if l["emails"]), path)
    return 0


def cmd_workday(settings: Settings, args) -> int:
    """Fill one Workday application up to Review; with --submit, submit when every answer is from confirmed facts.
    Without --submit the window stays open for you to check and submit."""
    import os
    from .ats.workday import WorkdayApplier
    from .config import STATE_DIR
    from .resume_variants import variant_file, variant_for
    email, password = os.getenv("WORKDAY_EMAIL", "").strip(), os.getenv("WORKDAY_PASSWORD", "").strip()
    if not (email and password):
        log.error("Set WORKDAY_EMAIL and WORKDAY_PASSWORD in .env first.")
        return 1
    profile = load_profile()
    tracker = Tracker()
    import csv as _csv
    from .tracker import SUBMITTED_STATUSES
    if tracker.applications_csv.exists() and any(
            r.get("url", "").strip() == args.url.strip() and r.get("status") in SUBMITTED_STATUSES
            for r in _csv.DictReader(open(tracker.applications_csv, newline="", encoding="utf-8"))):
        log.warning("Already submitted (or possibly submitted) to %s; not applying again.", args.url[:100])
        return 0
    resume = args.resume or os.path.join("output", "resumes", variant_file(variant_for(args.title or "")))
    Settings.browser_profile_dir = property(lambda self: STATE_DIR / "workday-profile")
    with Browser(settings) as b:
        res = WorkdayApplier(b.page, profile, email, password, resume, assist=not args.no_assist, submit=args.submit).run(args.url)
        log.info("Workday: %s - %s", res.status, res.notes)
        tracker.record_application(JobPosting(job_id=args.url, url=args.url, title=args.title or "", company=company_from_url(args.url)),
                                   "workday", res.status, notes=(res.notes or "")[:200], resume_pdf=str(resume), remember=False)
        unknown = [f["label"] for f in res.filled if f["source"] == "unknown"]
        if unknown:
            log.info("unanswered fields: %s", unknown)
        if res.status in ("ready_for_review", "needs_you", "needs_verification") and not args.no_assist:
            log.info("The window stays open: review, fix anything, and press Submit yourself. Close the window when done.")
            try:
                b.page.wait_for_event("close", timeout=0)
            except Exception:
                pass
    return 0 if res.status == "ready_for_review" else 1


def cmd_workday_run(settings: Settings, args) -> int:
    """Find fitting India jobs on big companies' Workday sites, tailor the resume to each JD, fill the application
    up to Review, then wait for you to check it and press Submit (the bot never submits in this mode)."""
    import os
    from .ats.workday import WorkdayApplier
    from .ats.workday_jobs import discover
    from .config import STATE_DIR
    from .tailor_rules import tailored_pdf
    email, password = os.getenv("WORKDAY_EMAIL", "").strip(), os.getenv("WORKDAY_PASSWORD", "").strip()
    if not (email and password):
        log.error("Set WORKDAY_EMAIL and WORKDAY_PASSWORD in .env first.")
        return 1
    profile, tracker = load_profile(), Tracker()
    years = float(settings.experience_years or profile.get("total_experience_years") or 2)
    queries = ["AI engineer", "machine learning engineer", "data scientist", "python developer", "software engineer"]
    jobs = discover(profile, queries, max_age_days=args.days, years=years, slack=settings.experience_slack)
    recent = tracker.recent_role_keys()
    jobs = [j for j in jobs if not tracker.is_seen(f"workday:{j.url}") and Tracker.role_key(j.company, j.title) not in recent]
    # one posting per role: companies list the same role once per location/team ("Storage AI Engineer" x4)
    uniq = {}
    for j in jobs:
        uniq.setdefault(Tracker.role_key(j.company, j.title), j)
    jobs = list(uniq.values())
    log.info("%d Workday jobs to apply to", len(jobs))
    for j in jobs[: args.max]:
        log.info("  %s | %s | %s | %s", j.company, j.title, j.location, j.posted)
    if args.dry_run:
        return 0
    Settings.browser_profile_dir = property(lambda self: STATE_DIR / "workday-profile")
    done = 0
    with Browser(settings) as b:
        for j in jobs[: args.max]:
            log.info("=" * 60)
            log.info("%s - %s (%s)", j.company, j.title, j.posted)
            pdf, rep = tailored_pdf(j.jd, j.title, j.company, playwright=b.pw, url=j.url)
            log.info("  resume: %s (JD skills matched: %s)", pdf.name, ", ".join(rep["matched_skills"][:8]) or "-")
            job = JobPosting(job_id=j.req_id or j.url, url=j.url, title=j.title, company=j.company, location=j.location)
            w = WorkdayApplier(b.page, profile, email, password, str(pdf), assist=not args.submit, submit=args.submit, jd=j.jd, job_location=j.location)
            try:
                res = w.run(j.url)
            except Exception as e:                 # one site's surprise must not stop the batch
                w.snapshot("crash")
                from .ats.workday import WorkdayResult
                res = WorkdayResult("needs_you", f"crashed: {str(e)[:150]}")
            status = res.status
            if status == "applied":
                done += 1
            elif status == "ready_for_review" and not args.submit:
                # you check the Review page and press Submit; the bot watches for Workday's confirmation
                if w.ask_human(f"{j.company}: check the Review page and press Submit (or close the tab to skip)", timeout_s=900) or True:
                    body = b.page.inner_text("body") if not b.page.is_closed() else ""
                    if re.search(r"application (has been )?submitted|successfully submitted|thank you for applying|congratulations", body, re.I):
                        status = "applied"; done += 1
            tracker.record_application(job, "workday", status, notes=res.notes[:200], resume_pdf=str(pdf), remember=False)
            tracker.mark_seen(f"workday:{j.url}", status)
            log.info("  -> %s %s", status, res.notes[:120])
    log.info("Workday: %d applications submitted this run", done)
    return 0


def company_from_url(url: str) -> str:
    """Company name for file names and logs: the board name on hosted job boards, else the site's domain name."""
    m = (re.search(r"greenhouse\.io/embed/job_app\?for=([a-z0-9-]+)", url) or re.search(r"greenhouse\.io/([a-z0-9-]+)/jobs", url)
         or re.search(r"jobs\.lever\.co/([a-z0-9-]+)", url) or re.search(r"jobs\.ashbyhq\.com/([a-z0-9-]+)", url)
         or re.search(r"apply\.workable\.com/([a-z0-9-]+)", url) or re.search(r"https?://([a-z0-9-]+)\.zohorecruit", url))
    if m:
        return m.group(1)
    host = re.sub(r"^https?://", "", url).split("/")[0]
    parts = [p for p in host.split(".") if p not in ("www", "careers", "jobs", "apply", "ops", "in", "com", "co", "io", "net", "org")]
    return parts[0] if parts else host


def cmd_company_apply(settings: Settings, args) -> int:
    """Apply on company career sites from a list of URLs: tailor the resume to each page's job description, fill
    the form by its labels, then stop before Submit (or submit with --submit)."""
    from .ats.generic_form import GenericFormApplier
    from .config import STATE_DIR
    from .tailor_rules import tailored_pdf
    profile, tracker = load_profile(), Tracker()
    entries = []
    for line in Path(args.file).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # "apply_url | role | jd_url": role and jd_url are optional. jd_url is the posting itself, for when the
        # apply link is a sign-in or embedded form page that does not contain the job description.
        parts = [x.strip() for x in line.split(" | ")]
        entries.append((parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))
    # never submit twice: drop jobs already applied to (same URL, or same company + role)
    import csv as _csv
    from .tracker import SUBMITTED_STATUSES
    applied_urls = {r.get("url", "").strip() for r in _csv.DictReader(open(tracker.applications_csv, newline="", encoding="utf-8"))
                    if r.get("status") in SUBMITTED_STATUSES} if tracker.applications_csv.exists() else set()
    recent = tracker.recent_role_keys(include_saved=False)   # a job saved for manual apply is exactly what this applies to
    kept = []
    for url, role, jd_url in entries:
        if url in applied_urls or (role and Tracker.role_key(company_from_url(url), role) in recent):
            log.info("skip (already applied): %s", url[:100]); continue
        if role and title_excluded(role, profile):            # same title filters as every other source
            log.info("skip (excluded title): %s | %s", role[:60], url[:80]); continue
        if re.search(r"intern(ship)?s?\b|trainee|fresher", url + " " + role, re.I):   # internship pages are not the role
            log.info("skip (internship/trainee page): %s", url[:100]); continue
        from .cooldown import paused_until as _paused
        host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        held = next((h for h in ("smartrecruiters", "workday", "greenhouse", "lever", "ashby", "pyjamahr", "keka") if h in host and _paused(h)), None)
        if held:   # the ATS flagged us as a bot: stay off it until the cooldown ends
            log.info("skip (%s cooldown until %s): %s", held, _paused(held), url[:100]); continue
        kept.append((url, role, jd_url))
    entries = kept
    Settings.browser_profile_dir = property(lambda self, d=args.browser_profile: STATE_DIR / d)
    with Browser(settings) as b:
        first = True
        left_for_person = 0       # tabs kept open for Ashu (needs_you / CAPTCHA / ready to submit)
        last_left = False
        for url, role, jd_url in entries:
            if url in applied_urls:                    # the same URL twice in one file
                log.info("skip (already attempted this run): %s", url[:100]); continue
            applied_urls.add(url)
            # never reuse a tab that was left for the person: they may be typing in it (Ashu lost a form he was
            # filling when the next job loaded in the same tab, 16 Oct 2026)
            if not first and (args.hold or last_left):
                b.page = b.context.new_page()          # each form stays open in its own tab
            first = False
            log.info("=" * 60)
            log.info("%s", url[:110])
            try:
                if jd_url:
                    b.goto(jd_url)
                    jd = b.body_text()
                    b.goto(url)
                else:
                    b.goto(url)
                    jd = b.body_text()
                title = role or (b.page.locator("h1").first.inner_text().strip() if b.page.locator("h1").count() else "")
                company = company_from_url(url)
                pdf, rep = tailored_pdf(jd, title, company, playwright=b.pw, url=url)
                log.info("  role: %s | resume: %s (matched: %s)", title[:60], pdf.name, ", ".join(rep["matched_skills"][:6]) or "-")
                if "myworkdayjobs.com" in url or "myworkdaysite.com" in url:
                    # Workday has its own multi-step applier (account, autofill review, experience/education pages)
                    import os
                    from .ats.workday import WorkdayApplier
                    res = WorkdayApplier(b.page, profile, os.getenv("WORKDAY_EMAIL", "").strip(),
                                         os.getenv("WORKDAY_PASSWORD", "").strip(), str(pdf),
                                         assist=False, submit=args.submit).run(url)
                else:
                    # Greenhouse, Lever, Ashby, Keka, Zoho Recruit and plain career pages: label-driven filler
                    gf = GenericFormApplier(b.page, profile, str(pdf), role=title, submit=args.submit,
                                            code_file=str(Path("state/security-code.txt").resolve()) if args.submit else None)
                    if "naukri.com" in (jd_url or ""):
                        gf.fallback_country = "India"   # pipeline only keeps Naukri postings in Indian locations
                    res = gf.run(url)
            except Exception as e:
                b.save_debug("company-apply-crash")
                log.warning("  crashed: %s", str(e)[:150])
                # With --submit the crash may have happened after the click: record it as possibly submitted
                # (final), so the next cycle can't send the same application again.
                job = JobPosting(job_id=url, url=url, title=role, company=company_from_url(url))
                tracker.record_application(job, "company_site", "crashed_after_submit" if args.submit else "failed",
                                           notes=f"crashed: {str(e)[:150]}", remember=False)
                continue
            b.save_debug(f"company-{company}-{res.status}")
            log.info("  -> %s: %s", res.status, res.notes)
            last_left = res.status in ("needs_you", "ready_to_submit") or "CAPTCHA" in (res.notes or "")
            if last_left:
                left_for_person += 1
                log.info("  (this tab stays open for you; the next form opens in a new tab)")
            job = JobPosting(job_id=url, url=url, title=title, company=company)
            tracker.record_application(job, "company_site", res.status, notes=res.notes[:200], resume_pdf=str(pdf), remember=False)
            if args.hold:
                continue
            if not args.no_wait and (res.status == "ready_to_submit" and not args.submit or "CAPTCHA" in res.notes):
                log.info("  form filled: %s - check it in the window and press Submit yourself (waiting up to 5 min)",
                         "type the CAPTCHA," if "CAPTCHA" in res.notes else "")
                try:
                    import subprocess
                    subprocess.run(["osascript", "-e", f'display notification "{company}: check the form and submit" with title "Job form ready" sound name "Glass"'],
                                   timeout=5, capture_output=True)
                except Exception:
                    pass
                b.page.wait_for_timeout(300000)
        if args.hold or left_for_person:
            log.info("%d form(s) are open in tabs for you. Finish each, press Submit; close the window when done.",
                     left_for_person if not args.hold else len(entries))
            try:
                b.context.wait_for_event("close", timeout=0)
            except Exception:
                pass
    return 0


def cmd_nvites(settings: Settings, args) -> int:
    """Apply to Naukri NVites (recruiter invitations), after the usual filters; save external form links."""
    from . import nvites
    from .cooldown import paused_until
    from .ranking import eligibility
    profile, tracker, cache = load_profile(), Tracker(read_only=args.dry_run), AnswerCache()
    with Browser(settings) as b:
        if not b.ensure_logged_in():
            log.error("Not logged in to Naukri."); return 1
        invites = nvites.collect(b)
        log.info("NVites: %d (%d unread, %d already applied)", len(invites), sum(i.unread for i in invites),
                 sum(i.applied for i in invites))
        # invites that only point to an employer form: hand them to the company-site pipeline
        ext = [(u, i) for i in invites if not i.applied for u in i.external_urls]
        if ext:
            path = Path("inputs/naukri-nvite-forms.txt"); path.parent.mkdir(exist_ok=True)
            have = path.read_text() if path.exists() else ""
            new = [f"{u} | {i.title}" for u, i in ext if u not in have]
            if new:
                path.write_text(have + "\n".join(new) + "\n")
                log.info("saved %d employer form link(s) from NVites to %s", len(new), path)
        until = paused_until("naukri")
        if until and not args.dry_run:
            log.error("Naukri cooldown until %s; listing only.", until); return 0
        remaining = max(settings.daily_quota - tracker.applied_last_24h(), 0)
        applied = 0
        for inv in invites:
            job = inv.posting()
            tag = f"{inv.title[:40]} — {inv.company[:25]}"
            if inv.applied or tracker.is_seen(job.job_id):
                continue
            reason = eligibility(job, ApplyMode.NAUKRI, settings, profile)
            if reason:
                log.info("  skip %s (%s)", tag, reason)
                tracker.mark_seen(job.job_id, reason); continue
            if args.dry_run:
                log.info("  would apply: %s", tag); continue
            if applied >= min(remaining, args.max):
                log.info("cap reached (%d this run, %d left in Naukri's 24 h window)", applied, remaining); break
            if not nvites.open_invite(b, inv) or not nvites.apply_button(b).count():
                log.info("  %s: no Apply button now", tag); continue
            refused: list[int] = []
            on_response = lambda r: refused.append(r.status) if ("apply-workflow" in r.url and r.status in (403, 429)) else None
            b.page.on("response", on_response)
            try:
                log.info("  applying: %s", tag)
                nvites.apply_button(b).click(); pause(2, 3)
                handler = DrawerHandler(b, None, cache, tracker, job, assist=args.assist)
                answered = []
                if handler.wait_for_drawer(timeout_s=8):
                    res = handler.run(); answered = res.answered; pause(1.5, 2.5)
                ok = nvites.open_invite(b, inv) and nvites.is_applied(b)
            finally:
                b.page.remove_listener("response", on_response)
            if refused and not ok:
                from .cooldown import pause as cooldown
                cooldown("naukri", 12, f"apply API refused with HTTP {refused[0]} (daily cap or throttle)")
                tracker.record_application(job, "nvite", "quota", notes=f"refused HTTP {refused[0]}", remember=False)
                log.warning("Naukri refused the apply (HTTP %s); stopping.", refused[0]); break
            tracker.record_application(job, "nvite", "applied" if ok else "failed", questions_answered=len(answered),
                                       notes="" if ok else "no 'Applied' state after clicking Apply")
            applied += ok
            b.random_idle()
        log.info("NVites applied this run: %d", applied)
    return 0


TOP_SITES_FILE = Path("inputs/naukri-top-company-sites.txt")
TOP_ROTATION = Path("state/top_company_rotation.json")


def _company_matches(card_company: str, wanted: str) -> bool:
    c, w = re.sub(r"[^a-z0-9]+", " ", card_company.lower()), re.sub(r"[^a-z0-9]+", " ", wanted.lower()).strip()
    return bool(w) and re.search(rf"\b{re.escape(w)}\b", c) is not None


def cmd_top_company_sites(settings: Settings, args) -> int:
    """For each priority company (profile.priority_companies), search Naukri for its fresh postings that fit, and
    collect the employer links of 'Apply on company site' jobs into inputs/naukri-top-company-sites.txt
    (the format company-apply reads). Direct-apply jobs are left to the normal Naukri run.
    Companies rotate: each run covers the next --companies of the list."""
    import json as _json
    from .ranking import eligibility, title_excluded, title_included, too_old
    from .search import _slug
    profile, tracker = load_profile(), Tracker(read_only=args.dry_run)
    companies = list(dict.fromkeys(profile.get("priority_companies") or []))
    if not companies:
        log.error("profile.priority_companies is empty."); return 1
    state = _json.loads(TOP_ROTATION.read_text()) if TOP_ROTATION.exists() else {"next": 0}
    start = state.get("next", 0) % len(companies)
    batch = (companies[start:] + companies[:start])[:args.companies]
    have = TOP_SITES_FILE.read_text() if TOP_SITES_FILE.exists() else ""
    found = []
    with Browser(settings) as b:
        if not b.ensure_logged_in():
            log.error("Not logged in to Naukri."); return 1
        for company in batch:
            # "/<role>-jobs?k=<role>, <company>" is what Naukri understands as role-at-company (verified 10 Oct 2026);
            # "/<company>-jobs" alone is a plain keyword search that returns any posting mentioning the name.
            from urllib.parse import quote
            urls = [f"https://www.naukri.com/{_slug(r)}-jobs?k={quote(r + ', ' + company)}"
                    f"&experience={settings.experience_years or 2}&jobAge={args.days}"
                    for r in [x.strip() for x in args.roles.split(",") if x.strip()]]
            cards = [c for c in collect_cards(b, urls) if _company_matches(c.company, company)]
            cards = [c for c in cards if title_included(c.title, profile) and not title_excluded(c.title, profile)
                     and not too_old(c.posted, settings) and not tracker.is_seen(c.job_id)]
            log.info("[%s] %d fitting posting(s)", company, len(cards))
            for c in cards[:args.per_company]:
                try:
                    job = scrape_job(b, c.url)
                    mode = detect_apply_mode(b)
                except Exception as e:
                    log.warning("  scrape failed %s: %s", c.url[:80], e.__class__.__name__); continue
                if mode != ApplyMode.COMPANY_SITE:
                    continue
                reason = eligibility(job, mode, settings, profile)
                if reason:
                    log.info("  skip %s (%s)", job.title[:50], reason); continue
                ext = "" if args.dry_run else capture_external_url(b)
                if not ext.startswith("http") or ext in have:
                    continue
                found.append(f"{ext} | {job.title}")
                have += ext + "\n"
                tracker.record_company_site(job, ext, "", f"top company: {company}", {}, status="queued")
                log.info("  + %s — %s -> %s", company, job.title[:50], ext[:90])
                b.random_idle()
    if found and not args.dry_run:
        TOP_SITES_FILE.parent.mkdir(exist_ok=True)
        with open(TOP_SITES_FILE, "a") as f:
            f.write("\n".join(found) + "\n")
    if not args.dry_run:
        TOP_ROTATION.write_text(_json.dumps({"next": (start + len(batch)) % len(companies)}))
    log.info("top companies %s..%s: %d new employer link(s) -> %s", batch[0], batch[-1], len(found), TOP_SITES_FILE)
    return 0


EARLY_URL = "https://www.naukri.com/mnjuser/recommended-earjobs"
def _parse_early_card(text: str) -> dict | None:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    exp_i = next((i for i, l in enumerate(lines) if re.fullmatch(r"\d+\s*-\s*\d+\s*Yrs", l)), None)
    if exp_i is None or not lines:
        return None
    salary = lines[exp_i + 1] if exp_i + 1 < len(lines) else ""
    loc = lines[exp_i + 2] if exp_i + 2 < len(lines) else ""
    try:
        end = lines.index("Hiring for one of these companies")
    except ValueError:
        end = len(lines)
    return {"title": lines[0], "company": lines[2] if len(lines) > 2 else "", "experience": lines[exp_i].replace("Yrs", "Years"),
            "salary": salary if "Lacs" in salary or "disclosed" in salary.lower() else "", "location": loc,
            "skills": lines[exp_i + 3:end]}


_EARLY_BTNS = "[...document.querySelectorAll('*')].filter(e => e.childElementCount === 0 && e.innerText && e.innerText.trim() === 'Share interest')"
_EARLY_TEXTS_JS = "() => " + _EARLY_BTNS + """.map(btn => { let n = btn; for (let i = 0; i < 12 && n.parentElement; i++) { n = n.parentElement;
    if (/\\d+\\s*-\\s*\\d+\\s*Yrs/.test(n.innerText) && n.innerText.split('Share interest').length === 2) return n.innerText; } return ''; })"""
_EARLY_CLICK_JS = "i => { const bs = " + _EARLY_BTNS + "; bs[i].scrollIntoView({block: 'center'}); bs[i].click(); return bs.length; }"


def cmd_early_access(settings: Settings, args) -> int:
    """Naukri Pro 'Early access roles': share interest in every role that passes the usual filters.

    A shared card leaves the list and Naukri re-renders the page, so each round reloads the list, reads every card
    in one pass, and clicks the first matching one (about 4 s per role)."""
    from .ranking import eligibility
    profile, tracker = load_profile(), Tracker(read_only=args.dry_run)
    skip: set[tuple] = set()
    shared = 0
    with Browser(settings) as b:
        if not b.ensure_logged_in():
            log.error("Not logged in to Naukri."); return 1
        pg = b.page
        while shared < args.max:
            pg.goto(EARLY_URL, wait_until="domcontentloaded")
            try:
                pg.wait_for_selector("text=Share interest", timeout=20000)
            except Exception:
                log.info("no early-access roles on the page"); break
            for _ in range(6):                                  # the list renders lazily
                pg.mouse.wheel(0, 3000); pause(0.3, 0.5)
            texts = pg.evaluate(_EARLY_TEXTS_JS)
            pick = None
            for i, text in enumerate(texts):
                info = _parse_early_card(text)
                if not info:
                    continue
                key = (info["title"], info["experience"], info["salary"], info["location"][:30])
                if key in skip:
                    continue
                job = JobPosting(job_id="early:" + re.sub(r"[^a-z0-9]+", "-", " ".join(key).lower())[:150], url=EARLY_URL,
                                 title=info["title"], experience=info["experience"], salary=info["salary"],
                                 location=info["location"], skills=info["skills"])
                reason = eligibility(job, ApplyMode.NAUKRI, settings, profile)
                if not reason and re.search(r"\b(sr\.?|senior)\b", info["title"], re.I):
                    reason = "senior_title"
                skip.add(key)
                if reason:
                    log.debug("  skip %s (%s)", info["title"], reason); continue
                pick = (i, job)
                break
            if not pick:
                log.info("no more matching roles (%d left on the page)", len(texts)); break
            i, job = pick
            tag = f"{job.title[:40]} | {job.experience} | {job.salary}"
            if args.dry_run:
                log.info("  would share interest: %s", tag); continue
            pg.evaluate(_EARLY_CLICK_JS, i); pause(2, 3)
            tracker.record_application(job, "early_access", "interest_shared", remember=False)
            shared += 1
            log.info("  shared interest %d: %s", shared, tag)
            from .humanize import steady_wait               # same steady rhythm as everything else
            steady_wait(settings)
    log.info("early access: shared interest in %d role(s)", shared)
    return 0


def cmd_outreach(settings: Settings, args) -> int:
    """Email recruiters (Naukri JDs and LinkedIn posts that ask for resumes by email) with a tailored resume."""
    from playwright.sync_api import sync_playwright
    from . import outreach
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    with sync_playwright() as pw:
        results = outreach.run(settings, load_profile(), send=args.send, max_n=args.max, sources=sources, playwright=pw)
    sent = sum(r["status"] == "emailed" for r in results)
    drafted = sum(r["status"] == "drafted" for r in results)
    log.info("outreach: %d emailed, %d drafted, %d skipped -> %s", sent, drafted, len(results) - sent - drafted, outreach.OUT_DIR)
    return 0


def cmd_refresh(settings: Settings, args) -> int:
    """Re-save the Naukri headline so the profile shows as updated today (run 2-3 times a day)."""
    from . import naukri_profile
    with Browser(settings) as b:
        if not b.ensure_logged_in():
            log.error("Not logged in to Naukri."); return 1
        ok, msg = naukri_profile.refresh(b, force=args.force)
    log.info("profile refresh: %s", msg)
    return 0 if ok or msg.startswith("skipped") else 1


def cmd_portal(settings: Settings, args) -> int:
    from .portals.runner import run_portal
    return run_portal(args.site, args.max, args.dry_run)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="naukri_bot", description="Naukri auto-apply bot")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="log in once; the session is stored in state/browser-profile")
    r = sub.add_parser("run", help="search and apply")
    r.add_argument("--keywords", help="comma-separated, overrides KEYWORDS from .env")
    r.add_argument("--max", type=int, help="max Naukri applications this run")
    r.add_argument("--only", choices=["all", "naukri", "company"], default="all")
    r.add_argument("--dry-run", action="store_true", help="scrape and classify only; no clicks, no LLM calls")
    r.add_argument("--no-tailor", action="store_true", help="save company-site jobs to CSV without building resumes")
    r.add_argument("--survey-only", action="store_true", help="run phase 1 only and write output/ranked_jobs.csv")
    r.add_argument("--no-llm", action="store_true", help="no model: questionnaire jobs end half-answered, so this needs --allow-partial")
    r.add_argument("--allow-partial", action="store_true", help="allow applying without a model (questionnaires may be left incomplete)")
    r.add_argument("--assist", action="store_true",
                   help="when no rule/model can answer a question, alert you and wait (5 min) for you to answer it in the browser")
    r.add_argument("--max-applicants", type=int, help="override MAX_APPLICANTS")
    r.add_argument("--source", choices=["all", "search", "recommended"], default="all",
                   help="keyword search, Naukri's Recommended jobs page, or both (default)")
    s = sub.add_parser("scrape", help="show parsed job data for a URL")
    s.add_argument("url")
    t = sub.add_parser("tailor", help="build a tailored resume for a job URL")
    t.add_argument("url")
    pr = sub.add_parser("profile", help="push profile/naukri_profile.yaml to your Naukri profile")
    pr.add_argument("--new-only", action="store_true", help="projects: only add entries that are not on the profile yet")
    pr.add_argument("sections", nargs="*", choices=["headline", "skills", "summary", "employment", "projects", "resume", "itskills", "accomplishments", "career", "education", "dedupe-projects"], help="default: headline skills summary")
    po = sub.add_parser("portal", help="search and apply on another portal (linkedin, hirist, wellfound, indeed)")
    po.add_argument("site", choices=["all", "linkedin", "hirist", "wellfound", "indeed"], help="'all' runs Naukri then every portal in turn")
    po.add_argument("--max", type=int, help="max applications this run")
    po.add_argument("--dry-run", action="store_true", help="survey and rank only")
    wd = sub.add_parser("workday", help="fill a Workday application up to Review (you submit)")
    wd.add_argument("url")
    wd.add_argument("--title", help="job title, to pick the AI/ML, Backend or Full-stack resume")
    wd.add_argument("--no-assist", action="store_true", help="do not wait for you on unknown fields")
    wd.add_argument("--submit", action="store_true", help="press Submit at Review when every answer is from confirmed facts")
    wd.add_argument("--resume", help="PDF to upload instead of the role variant (e.g. a JD-tailored resume)")
    wr = sub.add_parser("workday-run", help="find Workday jobs, tailor resume, fill to Review; you press Submit")
    wr.add_argument("--max", type=int, default=5)
    wr.add_argument("--days", type=int, default=7, help="posted within this many days")
    wr.add_argument("--dry-run", action="store_true", help="only list the jobs it would apply to")
    wr.add_argument("--submit", action="store_true",
                    help="submit each application when every answer is from confirmed facts (authorized by Ashu, 6 Oct 2026)")
    ca = sub.add_parser("company-apply", help="apply on company career sites from a URL list (tailored resume per job)")
    ca.add_argument("--file", required=True, help='one URL per line; optional " | Role" after the URL')
    ca.add_argument("--submit", action="store_true", help="press Submit when every field came from profile/rules")
    ca.add_argument("--no-wait", action="store_true", help="do not pause on each filled form")
    ca.add_argument("--browser-profile", default="ats-profile", help="separate profile lets two runs (or an open window) coexist")
    ca.add_argument("--hold", action="store_true", help="open every form in its own tab, fill, and keep the window open for you")
    nv = sub.add_parser("nvites", help="apply to Naukri NVites (recruiter invitations) that pass the filters")
    nv.add_argument("--max", type=int, default=15)
    nv.add_argument("--dry-run", action="store_true", help="list what it would apply to")
    nv.add_argument("--assist", action="store_true", help="wait for you on questions no rule can answer")
    tc = sub.add_parser("top-company-sites", help="Naukri postings of priority companies that apply on the company site -> URL list")
    tc.add_argument("--companies", type=int, default=15, help="how many priority companies this run (rotates)")
    tc.add_argument("--per-company", type=int, default=3)
    tc.add_argument("--days", type=int, default=7, help="posted within this many days")
    tc.add_argument("--roles", default="software engineer, ai engineer", help="comma-separated role searches per company")
    tc.add_argument("--dry-run", action="store_true")
    ea = sub.add_parser("early-access", help="Naukri Pro early-access roles: share interest in ones that pass the filters")
    ea.add_argument("--max", type=int, default=10)
    ea.add_argument("--dry-run", action="store_true")
    ou = sub.add_parser("outreach", help="email recruiters a tailored resume (drafts unless --send)")
    ou.add_argument("--send", action="store_true", help="send through Gmail (needs GMAIL_ADDRESS and GMAIL_APP_PASSWORD)")
    ou.add_argument("--max", type=int, default=10)
    ou.add_argument("--sources", default="naukri,linkedin")
    rf = sub.add_parser("refresh", help="re-save the Naukri headline so the profile shows as updated today")
    rf.add_argument("--force", action="store_true", help="ignore the 3-hour minimum gap")
    le = sub.add_parser("leads", help="LinkedIn hiring posts -> leads CSV + outreach drafts (nothing is sent)")
    le.add_argument("--queries", help='comma-separated searches, e.g. "hiring AI engineer Bangalore, hiring python developer India"')
    a = sub.add_parser("ask", help="test how the model answers a question")
    a.add_argument("question")
    a.add_argument("--options")

    args = p.parse_args(argv)
    setup_logging(args.verbose)
    settings = Settings()
    cmd = {"login": cmd_login, "run": cmd_run, "scrape": cmd_scrape, "tailor": cmd_tailor, "ask": cmd_ask, "profile": cmd_profile, "portal": cmd_portal, "leads": cmd_leads, "workday": cmd_workday, "workday-run": cmd_workday_run, "company-apply": cmd_company_apply, "refresh": cmd_refresh, "nvites": cmd_nvites, "top-company-sites": cmd_top_company_sites, "early-access": cmd_early_access, "outreach": cmd_outreach}[args.cmd]
    sys.exit(cmd(settings, args))
