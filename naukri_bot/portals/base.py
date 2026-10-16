"""Common adapter interface and the shared apply engine for every portal."""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..models import ApplyMode, JobCard, JobPosting
from ..ranking import eligibility, sort_key, too_old, title_excluded, title_included, company_excluded, dedupe_company_title
from ..tracker import Tracker

from ..humanize import between_applies

log = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    status: str                    # applied | applied_partial | deferred | failed | skipped | saved_for_manual
    notes: str = ""
    questions: list[dict] = field(default_factory=list)
    external_url: str = ""


def _query_key(url: str) -> str:
    """The search a URL belongs to, without its page number (start=10, page=2, pageNum=3)."""
    return re.sub(r"([?&](start|page|pageNum|p)=)\d+", r"\1", url)


class Portal:
    """Subclass per site. Each method is small and site-specific; the engine does the rest."""
    name = "portal"
    profile_dir = "portal-profile"
    daily_cap = 50
    max_views = 0            # job pages opened per run (0 = no cap)

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
        stats = {"applied": 0, "submitted_unconfirmed": 0, "deferred": 0, "saved_for_manual": 0, "skipped": 0, "failed": 0}
        cards: list[JobCard] = []
        seen_ids: set[str] = set()
        exhausted: set[str] = set()     # searches that already returned an empty page
        stale: dict[str, int] = {}      # consecutive pages per search with nothing unprocessed
        for url in self.search_urls():
            query = _query_key(url)
            if query in exhausted:
                continue
            try:
                found = self.collect_cards(url)
            except Exception as e:
                log.warning("[%s] search failed %s: %s", self.name, url, e.__class__.__name__)
                continue
            if not found:
                exhausted.add(query)
            new = [c for c in found if c.job_id not in seen_ids]
            seen_ids.update(c.job_id for c in new)
            cards.extend(new)
            unseen = sum(not tracker.is_seen(self._tid(c.job_id)) for c in new)
            log.info("[%s] %s -> %d cards, %d new, %d not processed before", self.name, url[:80], len(found), len(new), unseen)
            # deeper pages of a search that keeps returning processed jobs are repeats (Hirist 13 Oct 2026: 965 cards
            # over 65 pages, 11 unprocessed): stop that search after two such pages in a row
            stale[query] = 0 if unseen else stale.get(query, 0) + 1
            if stale[query] >= 2:
                exhausted.add(query)
            time.sleep(random.uniform(2, 4))
        fresh = [c for c in cards if not tracker.is_seen(self._tid(c.job_id))]
        if self.max_views and len(fresh) > self.max_views:
            log.info("[%s] viewing only the %d newest of %d fresh jobs this run (account-safety cap)", self.name, self.max_views, len(fresh))
            fresh = fresh[: self.max_views]
        log.info("[%s] %d cards, %d not processed before", self.name, len(cards), len(fresh))

        # phase 1: survey
        eligible: list[tuple[JobPosting, ApplyMode]] = []
        recent_roles = tracker.recent_role_keys()
        scrape_failures = 0       # consecutive: a login wall / bot check / redesign fails every page the same way
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
                log.warning("[%s] [%d/%d] scrape failed: %s", self.name, i, len(fresh), e.__class__.__name__)
                scrape_failures += 1
                if scrape_failures >= 3:
                    self.b.save_debug(f"{self.name}-scrape-stop")
                    log.error("[%s] 3 job pages in a row failed to load: stopping this portal (logged out? bot check?)", self.name)
                    return stats
                continue
            if mode == ApplyMode.UNKNOWN or not job.title:
                # a page that did not render is not a decision about the job: retry next run
                log.warning("[%s] [%d/%d] page state unknown, will retry next run: %s", self.name, i, len(fresh), card.url)
                scrape_failures += 1
                if scrape_failures >= 3:
                    self.b.save_debug(f"{self.name}-unknown-stop")
                    log.error("[%s] 3 job pages in a row unreadable: stopping this portal (logged out? bot check?)", self.name)
                    return stats
                continue
            scrape_failures = 0
            reason = eligibility(job, mode, self.settings, self.profile)
            if not reason and tracker.role_key(job.company, job.title) in recent_roles:
                reason = "same_role_applied_recently"
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
        apply_failures = 0
        for n, (job, mode) in enumerate(eligible, 1):
            if stats["applied"] + stats["submitted_unconfirmed"] >= max_apps:   # an unconfirmed submit may be an application
                log.info("[%s] cap reached (%d)", self.name, max_apps); break
            log.info("[%s] [%d/%d] appl=%s %s — %s", self.name, n, len(eligible), job.applicants if job.applicants is not None else "?", job.title, job.company)
            if dry_run:
                tracker.record_application(job, f"{self.name}:{mode.value}", "dry_run", remember=False); continue
            try:
                res = self.apply(job, mode, answerer)
            except Exception as e:
                if "logged out" in str(e):                 # every next job would hit the same sign-in wall
                    log.error("[%s] %s", self.name, e); break
                log.exception("[%s] apply failed: %s", self.name, e)
                res = ApplyResult("failed", str(e)[:200])
            if res.status == "quota":
                log.warning("[%s] %s: stopping; this job stays queued", self.name, res.notes)
                tracker.record_application(job, f"{self.name}:{mode.value}", "quota", notes=res.notes, remember=False)
                break
            if res.status in ("failed", "deferred"):
                self.b.save_debug(f"{self.name}-{res.status}-{job.job_id}")
            tracker.record_application(job, f"{self.name}:{mode.value}", res.status, questions_answered=len(res.questions),
                                       notes=(res.notes or res.external_url)[:200], remember=False)
            tracker.mark_seen(self._tid(job.job_id), res.status)   # failed / deferred retry per tracker.RETRY_LIMITS
            for q in res.questions:
                tracker.record_answer(job, q.get("question", ""), q.get("kind", ""), q.get("options", []), q.get("answer", ""), q.get("source", ""))
            key = res.status if res.status in stats else ("applied" if res.status.startswith("applied") else "failed")
            stats[key] += 1
            apply_failures = apply_failures + 1 if res.status == "failed" else 0
            if apply_failures >= 3:
                log.error("[%s] 3 applications in a row failed: stopping this portal (layout change?)", self.name)
                break
            between_applies(self.settings)
        return stats

    def _tid(self, job_id: str) -> str:
        return f"{self.name}:{job_id}"
