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
    def answer(question: str, options: list[str], kind: str):
        hit = cache.lookup(question, options)
        if hit:
            from ..llm import QuestionAnswer
            return QuestionAnswer(answer_text=hit["answer_text"], selected_option_indices=hit["indices"], reusable=True, skip=False, confidence=1.0)
        return rule_answer(question, options, kind, profile)
    return answer


def run_all(max_apps: int | None, dry_run: bool) -> int:
    """Naukri first (its own engine), then every portal adapter; each uses its own browser profile and daily cap."""
    import subprocess, sys
    order = ["naukri", "linkedin", "hirist", "wellfound", "indeed"]
    for site in order:
        log.info("=" * 60); log.info("PORTAL: %s", site)
        if site == "naukri":
            args = [sys.executable, "-m", "naukri_bot", "run", "--no-llm"] + (["--dry-run"] if dry_run else []) + (["--max", str(max_apps)] if max_apps else [])
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
    settings = Settings()
    Settings.browser_profile_dir = property(lambda self, d=cls.profile_dir: STATE_DIR / d)
    profile = load_profile()
    tracker = Tracker()
    cap = max_apps if max_apps is not None else min(cls.daily_cap, settings.daily_quota)
    with Browser(settings) as b:
        portal = cls(b, settings, profile)
        stats = portal.run(tracker, make_answerer(profile, AnswerCache()), cap, dry_run=dry_run)
    log.info("[%s] %s", site, " | ".join(f"{k}: {v}" for k, v in stats.items()))
    return 0
