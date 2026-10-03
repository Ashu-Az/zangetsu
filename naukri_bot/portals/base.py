"""Common adapter interface and the shared apply engine for every portal."""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..models import ApplyMode, JobCard, JobPosting
from ..ranking import eligibility, sort_key, too_old, title_excluded, title_included, company_excluded, dedupe_company_title
from ..tracker import Tracker

log = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    status: str                    # applied | applied_partial | deferred | failed | skipped | saved_for_manual
    notes: str = ""
    questions: list[dict] = field(default_factory=list)
    external_url: str = ""


class Portal:
    """Subclass per site. Each method is small and site-specific; the engine does the rest."""
    name = "portal"
    profile_dir = "portal-profile"
    daily_cap = 50

    def __init__(self, browser, settings: Settings, profile: dict):
        import dataclasses
        self.b = browser
        self.page = browser.page
        self.settings = dataclasses.replace(settings)
        self.profile = profile
        override = (profile.get("portal_max_age_days") or {}).get(self.name)
        if override:
            self.settings.max_job_age_days = int(override)

    # --- site hooks
    def search_urls(self) -> list[str]:
        raise NotImplementedError

    def collect_cards(self, url: str) -> list[JobCard]:
        raise NotImplementedError

    def scrape(self, card: JobCard) -> tuple[JobPosting, ApplyMode]:
        raise NotImplementedError

    def apply(self, job: JobPosting, mode: ApplyMode, answerer) -> ApplyResult:
        raise NotImplementedError

    # --- shared engine
    def run(self, tracker: Tracker, answerer, max_apps: int, dry_run: bool = False) -> dict:
        stats = {"applied": 0, "deferred": 0, "saved_for_manual": 0, "skipped": 0, "failed": 0}
        cards: list[JobCard] = []
        seen_ids: set[str] = set()
        for url in self.search_urls():
            try:
                found = self.collect_cards(url)
            except Exception as e:
                log.warning("[%s] search failed %s: %s", self.name, url, e.__class__.__name__)
                continue
            new = [c for c in found if c.job_id not in seen_ids]
            seen_ids.update(c.job_id for c in new)
            cards.extend(new)
            log.info("[%s] %s -> %d cards, %d new", self.name, url[:80], len(found), len(new))
            time.sleep(random.uniform(2, 4))
        fresh = [c for c in cards if not tracker.is_seen(self._tid(c.job_id))]
        log.info("[%s] %d cards, %d not processed before", self.name, len(cards), len(fresh))

        # phase 1: survey
        eligible: list[tuple[JobPosting, ApplyMode]] = []
        extra = [k.lower() for k in (self.profile.get("portal_exclude_titles") or {}).get(self.name, [])]
        for i, card in enumerate(fresh, 1):
            if any(k in card.title.lower() for k in extra):
                tracker.mark_seen(self._tid(card.job_id), "portal_title_filtered"); stats["skipped"] += 1; continue
            if title_excluded(card.title, self.profile) or not title_included(card.title, self.profile) or company_excluded(card.company, self.profile):
                tracker.mark_seen(self._tid(card.job_id), "title_or_company_filtered"); stats["skipped"] += 1; continue
            if too_old(card.posted, self.settings):
                tracker.mark_seen(self._tid(card.job_id), "too_old"); stats["skipped"] += 1; continue
            try:
                job, mode = self.scrape(card)
            except Exception as e:
                log.warning("[%s] [%d/%d] scrape failed: %s", self.name, i, len(fresh), e.__class__.__name__); continue
            reason = eligibility(job, mode, self.settings, self.profile)
            log.info("[%s] [%d/%d] %-14s appl=%-5s %-12s %s — %s%s", self.name, i, len(fresh), mode.value, job.applicants if job.applicants is not None else "?",
                     (job.posted or "")[:12], job.title[:40], job.company[:22], f"  -> skip ({reason})" if reason else "")
            if reason:
                if not reason.startswith("too_many_applicants"):
                    tracker.mark_seen(self._tid(job.job_id), reason)
                stats["skipped"] += 1; continue
            eligible.append((job, mode))
            time.sleep(random.uniform(1.5, 3.5))
        eligible.sort(key=lambda jm: (jm[1] == ApplyMode.COMPANY_SITE, sort_key(jm[0], self.settings)))
        eligible = dedupe_company_title(eligible)
        log.info("[%s] %d eligible", self.name, len(eligible))

        # phase 2: apply
        for n, (job, mode) in enumerate(eligible, 1):
            if stats["applied"] >= max_apps:
                log.info("[%s] cap reached (%d)", self.name, max_apps); break
            log.info("[%s] [%d/%d] appl=%s %s — %s", self.name, n, len(eligible), job.applicants if job.applicants is not None else "?", job.title, job.company)
            if dry_run:
                tracker.record_application(job, f"{self.name}:{mode.value}", "dry_run", remember=False); continue
            try:
                res = self.apply(job, mode, answerer)
            except Exception as e:
                log.exception("[%s] apply failed: %s", self.name, e)
                res = ApplyResult("failed", str(e)[:200])
            tracker.record_application(job, f"{self.name}:{mode.value}", res.status, questions_answered=len(res.questions),
                                       notes=(res.notes or res.external_url)[:200], remember=False)
            if res.status not in ("deferred", "failed"):      # failed ones get one more chance on the next run
                tracker.mark_seen(self._tid(job.job_id), res.status)
            for q in res.questions:
                tracker.record_answer(job, q.get("question", ""), q.get("kind", ""), q.get("options", []), q.get("answer", ""), q.get("source", ""))
            key = res.status if res.status in stats else ("applied" if res.status.startswith("applied") else "failed")
            stats[key] += 1
            time.sleep(random.uniform(self.settings.min_delay, self.settings.max_delay))
        return stats

    def _tid(self, job_id: str) -> str:
        return f"{self.name}:{job_id}"
