"""CLI glue: `python -m naukri_bot portal <site> [--max N] [--dry-run]`."""
from __future__ import annotations

import logging

from ..answers_store import AnswerCache
from ..browser import Browser
from ..config import STATE_DIR, Settings, load_profile
from ..rules import rule_answer
from ..tracker import Tracker

log = logging.getLogger(__name__)


def make_answerer(profile: dict, cache: AnswerCache):
    from ..llm import QuestionAnswer

    def cached(hit):
        return QuestionAnswer(answer_text=hit["answer_text"], selected_option_indices=hit["indices"],
                              reusable=True, skip=False, confidence=1.0)

    def answer(question: str, options: list[str], kind: str):
        hit = cache.lookup(question, options)
        if hit and hit.get("source") == "human":       # the person's own answer beats every rule
            return cached(hit)
        ruled = rule_answer(question, options, kind, profile)
        if ruled is not None:
            return ruled
        return cached(hit) if hit else None
    return answer


def run_all(max_apps: int | None, dry_run: bool) -> int:
    """Naukri first (its own engine), then every portal adapter; each uses its own browser profile and daily cap."""
    import subprocess, sys
    order = ["naukri", "linkedin", "hirist", "wellfound", "indeed"]
    for site in order:
        log.info("=" * 60); log.info("PORTAL: %s", site)
        if site == "naukri":
            args = [sys.executable, "-m", "naukri_bot", "run"] + (["--dry-run"] if dry_run else []) + (["--max", str(max_apps)] if max_apps else [])
        else:
            args = [sys.executable, "-m", "naukri_bot", "portal", site] + (["--dry-run"] if dry_run else []) + (["--max", str(max_apps)] if max_apps else [])
        try:
            subprocess.run(args, check=False)
        except Exception as e:
            log.error("[%s] crashed: %s", site, e)
    return 0


def run_portal(site: str, max_apps: int | None, dry_run: bool) -> int:
    if site == "all":
        return run_all(max_apps, dry_run)
    from .linkedin import LinkedInPortal
    portals = {"linkedin": LinkedInPortal}
    try:
        from .hirist import HiristPortal; portals["hirist"] = HiristPortal
    except ImportError:
        pass
    try:
        from .wellfound import WellfoundPortal; portals["wellfound"] = WellfoundPortal
    except ImportError:
        pass
    try:
        from .indeed import IndeedPortal; portals["indeed"] = IndeedPortal
    except ImportError:
        pass
    cls = portals[site]
    from ..cooldown import paused_until
    until = paused_until(site)
    if until:
        log.warning("[%s] skipped: cooldown until %s", site, until)
        return 0
    settings = Settings()
    Settings.browser_profile_dir = property(lambda self, d=cls.profile_dir: STATE_DIR / d)
    profile = load_profile()
    tracker = Tracker(read_only=dry_run)
    daily = min(cls.daily_cap, settings.daily_quota)
    left = max(daily - tracker.applied_today(site), 0)        # caps are per day, across runs
    cap = min(max_apps, left) if max_apps is not None else left
    if cap == 0 and not dry_run:
        log.info("[%s] daily cap of %d already reached today", site, daily)
        return 0
    with Browser(settings) as b:
        portal = cls(b, settings, profile)
        stats = portal.run(tracker, make_answerer(profile, AnswerCache()), cap, dry_run=dry_run)
    log.info("[%s] %s", site, " | ".join(f"{k}: {v}" for k, v in stats.items()))
    return 0
