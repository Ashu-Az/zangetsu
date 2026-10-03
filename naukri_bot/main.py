"""CLI entry point.

  python -m naukri_bot login                 # open the browser, log in once (cookies persist)
  python -m naukri_bot run [--max N] [--dry-run] [--only naukri|company|all] [--survey-only]
  python -m naukri_bot scrape <job_url>      # print what the bot sees on a job page
  python -m naukri_bot tailor <job_url>      # build a tailored resume for one job
  python -m naukri_bot ask "question" [--options "A,B,C"]   # test the answer model
"""
from __future__ import annotations

import argparse
import json
import logging
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
from .search import build_search_urls, collect_cards
from .tracker import Tracker

log = logging.getLogger("naukri_bot")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


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
        tailored = llm.tailor_resume(job)
        files = build_resume_files(tailored, job, playwright=browser.pw)
        fit_score, fit_notes = tailored.fit_score, tailored.fit_notes
        log.info("  fit %s/100 — %s", fit_score, fit_notes)
    tracker.record_company_site(job, external, fit_score, fit_notes, files,
                                status="dry_run" if dry_run else "pending_manual")
    tracker.record_application(job, ApplyMode.COMPANY_SITE.value, "saved_for_manual" if not dry_run else "dry_run",
                               notes=external, resume_pdf=files.get("pdf", ""))


# ------------------------------------------------------------------ naukri apply flow
def handle_naukri_apply(browser: Browser, llm: LLM, cache: AnswerCache, job: JobPosting, tracker: Tracker, dry_run: bool) -> bool:
    if dry_run:
        tracker.record_application(job, ApplyMode.NAUKRI.value, "dry_run")
        return False
    if not browser.click_if_visible(S.APPLY_BUTTON):
        tracker.record_application(job, ApplyMode.NAUKRI.value, "failed", notes="apply button vanished")
        return False
    pause(2, 3)
    if browser.text_present(S.DAILY_QUOTA_TEXT):
        tracker.record_application(job, ApplyMode.NAUKRI.value, "failed", notes="daily quota reached")
        raise QuotaReached()

    handler = DrawerHandler(browser, llm, cache, tracker, job)
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
        tracker.record_application(job, ApplyMode.QUESTIONNAIRE.value, status,
                                   questions_answered=len(res.answered), notes=res.notes)
        return applied

    applied = confirm_applied(browser, job)
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
    for i, card in enumerate(cards, 1):
        if title_excluded(card.title, profile) or not title_included(card.title, profile):
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
            survey.put(job, mode)
            pause(1.0, 2.5)
        reason = eligibility(job, mode, settings, profile)
        log.info("  [%d/%d] %-6s applicants=%-5s %-14s %s — %s%s", i, len(cards), mode.value[:6],
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
        w.writerow(["rank", "applicants", "posted", "salary", "mode", "title", "company", "experience", "location", "url"])
        for i, (job, mode) in enumerate(eligible, 1):
            w.writerow([i, job.applicants if job.applicants is not None else "", job.posted.replace("Posted:", "").strip(), job.salary, mode.value, job.title,
                        job.company, job.experience, job.location, job.url])


def cmd_run(settings: Settings, args) -> int:
    if args.keywords:
        settings.keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not settings.keywords:
        log.error("No KEYWORDS configured (.env) and none passed with --keywords.")
        return 1
    if args.max_applicants:
        settings.max_applicants = args.max_applicants
    max_apps = args.max if args.max is not None else settings.max_applications
    done_today = Tracker().applied_today()
    remaining = max(settings.daily_quota - done_today, 0)
    if remaining < max_apps:
        log.info("Daily quota %d, already applied today %d -> this run may apply to %d more.", settings.daily_quota, done_today, remaining)
        max_apps = remaining
    profile = load_profile()
    llm = None if (args.dry_run or args.survey_only or args.no_llm) else make_llm(settings)
    if args.no_llm:
        log.warning("Running WITHOUT a model: plain Apply only; questionnaire jobs are deferred, company-site jobs saved without tailored resumes.")
    cache = AnswerCache()
    tracker = Tracker()
    survey = SurveyCache(ttl_hours=settings.survey_cache_hours)
    stats = {"applied": 0, "company_site": 0, "skipped": 0, "failed": 0}

    with Browser(settings) as b:
        if not b.ensure_logged_in():
            return 1
        cards = collect_cards(b, build_search_urls(settings))
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

                if mode in (ApplyMode.ALREADY_APPLIED, ApplyMode.EXPIRED, ApplyMode.UNKNOWN):
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
                        ok = handle_naukri_apply(b, llm, cache, job, tracker, args.dry_run)
                        stats["applied" if ok else "failed"] += 1
                    except QuotaReached:
                        log.warning("Naukri daily application quota reached; stopping.")
                        break
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
    r.add_argument("--no-llm", action="store_true", help="no model: plain Apply only, defer questionnaire jobs, no tailored resumes")
    r.add_argument("--max-applicants", type=int, help="override MAX_APPLICANTS")
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
    a = sub.add_parser("ask", help="test how the model answers a question")
    a.add_argument("question")
    a.add_argument("--options")

    args = p.parse_args(argv)
    setup_logging(args.verbose)
    settings = Settings()
    cmd = {"login": cmd_login, "run": cmd_run, "scrape": cmd_scrape, "tailor": cmd_tailor, "ask": cmd_ask, "profile": cmd_profile, "portal": cmd_portal}[args.cmd]
    sys.exit(cmd(settings, args))
