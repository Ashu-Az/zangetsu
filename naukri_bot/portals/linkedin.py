"""LinkedIn adapter: fresh Easy Apply jobs via the lightweight search endpoint, applicant counts
from the job page, and the Easy Apply dialog driven by the rule-based answerer."""
from __future__ import annotations

import logging
import os
import re
import time
import random
from urllib.parse import quote_plus

from ..models import ApplyMode, JobCard, JobPosting
from ..ranking import parse_age_days
from .base import ApplyResult, Portal

log = logging.getLogger(__name__)
DEFAULT_RESUME = "resume.pdf"  # override with resume_file in profile.yaml


class LinkedInPortal(Portal):
    name = "linkedin"
    profile_dir = "linkedin-profile"
    daily_cap = 25   # keep LinkedIn volume low; it restricts accounts that look scripted

    @property
    def resume_name(self) -> str:
        return self.profile.get("resume_file") or DEFAULT_RESUME

    # ---------- search ----------
    def search_urls(self) -> list[str]:
        urls = []
        hours = max(1, int(self.settings.max_job_age_days or 1) * 24)
        for kw in self.settings.keywords:
            for start in (0, 25, 50, 75):
                urls.append("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
                            f"keywords={quote_plus(kw)}&location=India&f_TPR=r{hours * 3600}&f_AL=true&f_E=2%2C3%2C4&sortBy=DD&start={start}")
        return urls

    def collect_cards(self, url: str) -> list[JobCard]:
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(random.uniform(2.5, 4))
        raw = self.page.evaluate("""() => Array.from(document.querySelectorAll('li')).map(li => {
            const a = li.querySelector('a[href*="/jobs/view/"]'); if (!a) return null;
            const t = s => ((li.querySelector(s) || {}).innerText || '').trim();
            const m = a.href.match(/(\\d{8,})/);
            return {id: m ? m[1] : '', title: t('h3'), company: t('h4'), loc: t('.job-search-card__location'), time: t('time')};
        }).filter(x => x && x.id)""")
        return [JobCard(job_id=r["id"], title=r["title"], company=r["company"], url=f"https://www.linkedin.com/jobs/view/{r['id']}/",
                        location=r["loc"], posted=r["time"]) for r in raw]

    # ---------- scrape ----------
    @staticmethod
    def _applicants(text: str) -> int | None:
        m = re.search(r"Over\s+(\d+)\s+applicants", text, re.I)
        if m:
            return int(m.group(1)) + 1
        m = re.search(r"first\s+(\d+)\s+applicants", text, re.I)
        if m:
            return max(int(m.group(1)) - 5, 1)
        m = re.search(r"(\d[\d,]*)\s+applicants?", text, re.I)
        return int(m.group(1).replace(",", "")) if m else None

    def scrape(self, card: JobCard) -> tuple[JobPosting, ApplyMode]:
        pg = self.page
        pg.goto(card.url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(random.uniform(4, 6))
        main = pg.locator("main").first.inner_text() if pg.locator("main").count() else pg.inner_text("body")
        job = JobPosting(job_id=card.job_id, url=card.url, title=card.title, company=card.company, location=card.location, posted=card.posted)
        meta = re.search(r"([^\n]*ago[^\n]*)", main)
        if meta:
            job.posted = (re.search(r"(\d+\s+\w+\s+ago)", meta.group(1)) or [None, card.posted])[1]
        job.applicants = self._applicants(main)
        i = main.find("About the job")
        job.description = main[i + 13:i + 9000].strip() if i >= 0 else main[:6000]
        for lab in ("Hybrid", "Remote", "On-site"):
            if f"\n{lab}\n" in main:
                job.location = f"{job.location} ({lab})"
                break
        for lab in ("Full-time", "Contract", "Internship", "Part-time"):
            if f"\n{lab}\n" in main:
                job.employment_type = lab
                break
        if re.search(r"\n(Applied|Application submitted|You applied)\b", main):
            return job, ApplyMode.ALREADY_APPLIED
        if pg.locator("button[aria-label^='Easy Apply']").count():
            return job, ApplyMode.NAUKRI
        if pg.locator("button[aria-label^='Apply'], a[aria-label^='Apply']").count():
            return job, ApplyMode.COMPANY_SITE
        return job, ApplyMode.UNKNOWN

    # ---------- apply ----------
    def _modal(self):
        return self.page.locator("dialog[open], [role='dialog']").last

    def _answer_page(self, dlg, job, answerer, result: ApplyResult) -> bool:
        """Fill every question on the current Easy Apply page. Returns False if something could not be answered."""
        pg = self.page
        ok = True
        # radio / checkbox groups: role=radio elements carry the question in aria-label
        groups: dict[str, list] = {}
        for el in dlg.locator("[role='radio'], [role='checkbox']").all():
            q = (el.get_attribute("aria-label") or "").strip()
            if q:
                groups.setdefault(q, []).append(el)
        for q, els in groups.items():
            opts = [e.inner_text().strip().split("\n")[0] for e in els]
            if any(e.get_attribute("aria-checked") == "true" for e in els):
                continue
            ans = answerer(q, opts, "radio")
            idx = ans.selected_option_indices[0] if ans and ans.selected_option_indices else None
            if idx is None:
                yes = next((i for i, o in enumerate(opts) if o.lower() == "yes"), None)
                idx = yes if yes is not None else None
            if idx is None:
                result.notes = f"unanswered: {q[:80]}"; ok = False; continue
            els[idx].click(); time.sleep(0.4)
            result.questions.append({"question": q, "kind": "radio", "options": opts, "answer": opts[idx], "source": "rules" if ans else "default-yes"})
        # selects
        for sel in dlg.locator("select").all():
            q = (sel.get_attribute("aria-label") or "").strip() or dlg.evaluate("(s) => (document.querySelector('label[for=\"'+s.id+'\"]')||{}).innerText || ''", sel.element_handle()).strip()
            if not q or q.lower().startswith("email"):
                continue
            opts = [o.strip() for o in sel.locator("option").all_inner_texts()]
            real = [o for o in opts if o and not o.lower().startswith("select")]
            cur = sel.locator("option:checked").inner_text().strip() if sel.locator("option:checked").count() else ""
            if cur and not cur.lower().startswith("select"):
                continue
            ql = q.lower()
            if ql.startswith("phone country") or ("country" in ql and "code" in ql):
                pick = next((o for o in real if "+91" in o or o.lower().startswith("india")), None)
                if pick: sel.select_option(label=pick); time.sleep(0.3); continue
            if ql.startswith("country") or ql == "country*":
                pick = next((o for o in real if o.lower().startswith("india")), None)
                if pick: sel.select_option(label=pick); time.sleep(0.3); continue
            ans = answerer(q, real, "dropdown")
            choice = real[ans.selected_option_indices[0]] if ans and ans.selected_option_indices else (next((o for o in real if o.lower() == "yes"), None))
            if choice is None:
                result.notes = f"unanswered: {q[:80]}"; ok = False; continue
            sel.select_option(label=choice); time.sleep(0.3)
            result.questions.append({"question": q, "kind": "dropdown", "options": real, "answer": choice, "source": "rules" if ans else "default-yes"})
        # text inputs / textareas
        for inp in dlg.locator("input[type='text'], input:not([type]), textarea").all():
            q = (inp.get_attribute("aria-label") or "").strip()
            if not q or inp.input_value().strip():
                continue
            ql = q.lower()
            if ql.startswith(("email", "first name", "last name", "city", "location")):
                continue
            if ql.startswith(("mobile", "phone")):
                text = re.sub(r"\D", "", str(self.profile.get("phone", "")))[-10:]
                if not text:
                    continue
                inp.click(); pg.keyboard.type(text, delay=random.randint(40, 90)); time.sleep(0.3)
                result.questions.append({"question": q, "kind": "text", "options": [], "answer": text, "source": "profile"}); continue
            if ql.startswith("postal") or "pin code" in ql or "zip" in ql:
                text = str(self.profile.get("postal_code", "") or "")
                if text:
                    inp.click(); pg.keyboard.type(text, delay=60); time.sleep(0.3)
                continue
            ans = answerer(q, [], "text")
            text = ans.answer_text if ans and ans.answer_text else None
            if text is None and re.search(r"how many years|years of (work )?experience", q, re.I):
                text = str(self.profile.get("default_skill_years", 1))
            # numeric-looking fields (notice period, years, days, CTC) must be digits only
            if text and re.search(r"how many|years|days|notice|number of|ctc|salary|lpa|lakh", ql) and re.search(r"\d", text):
                text = re.search(r"\d+(?:\.\d+)?", text).group(0)
            if text is None:
                result.notes = f"unanswered: {q[:80]}"; ok = False; continue
            inp.click(); pg.keyboard.type(text, delay=random.randint(40, 90)); time.sleep(0.3)
            result.questions.append({"question": q, "kind": "text", "options": [], "answer": text, "source": "rules" if ans else "default"})
        return ok

    def _resume_page(self, dlg) -> None:
        pg = self.page
        if self.resume_name.lower() not in dlg.inner_text().lower():
            path = os.path.abspath(os.path.join("output", "resumes", self.resume_name))
            if os.path.exists(path) and dlg.locator("button:has-text('Upload resume')").count():
                with pg.expect_file_chooser(timeout=10000) as fc:
                    dlg.locator("button:has-text('Upload resume')").first.click()
                fc.value.set_files(path); time.sleep(6)
        # pick our resume's radio
        for r in dlg.locator("[role='radio'], input[type='radio']").all():
            try:
                container = r.locator("xpath=ancestor::div[contains(., '%s')][1]" % self.resume_name)
                if container.count():
                    r.click(); time.sleep(0.5); return
            except Exception:
                continue
        # fallback: click the text itself
        t = dlg.get_by_text(self.resume_name, exact=False).first
        if t.count():
            t.click(); time.sleep(0.5)

    def apply(self, job: JobPosting, mode: ApplyMode, answerer) -> ApplyResult:
        pg = self.page
        if mode == ApplyMode.COMPANY_SITE:
            return ApplyResult("saved_for_manual", external_url=job.url)
        if pg.url.split("?")[0].rstrip("/") != job.url.rstrip("/"):
            pg.goto(job.url, wait_until="domcontentloaded", timeout=60000); time.sleep(random.uniform(3, 5))
        btn = pg.locator("button[aria-label^='Easy Apply']").first
        for attempt in range(2):
            try:
                pg.wait_for_selector("button[aria-label^='Easy Apply']", timeout=15000); break
            except Exception:
                if attempt == 0:
                    pg.reload(); time.sleep(random.uniform(5, 7))
        if not btn.count():
            main = pg.locator("main").first.inner_text() if pg.locator("main").count() else ""
            if re.search(r"\n(Applied|Application submitted)\b", main):
                return ApplyResult("skipped", "already applied")
            if pg.locator("button[aria-label^='Apply'], a[aria-label^='Apply']").count():
                return ApplyResult("saved_for_manual", "external apply", external_url=job.url)
            if re.search(r"No longer accepting applications", main, re.I):
                return ApplyResult("skipped", "no longer accepting applications")
            return ApplyResult("failed", "no Easy Apply button after reload")
        btn.click(); time.sleep(random.uniform(3, 4.5))
        result = ApplyResult("failed")
        for step in range(12):
            dlg = self._modal()
            if not dlg.count():
                return ApplyResult("failed", "dialog vanished")
            text = dlg.inner_text()
            if re.search(r"application (was )?sent|Your application was sent|Application submitted", text, re.I):
                result.status = "applied"; break
            if "Resume" in text and dlg.locator("button:has-text('Upload resume')").count():
                self._resume_page(dlg)
            elif "Additional Questions" in text or dlg.locator("[role='radio'], select, input[type='text'], textarea").count():
                if not self._answer_page(dlg, job, answerer, result):
                    self._dismiss(); return ApplyResult("deferred", result.notes, result.questions)
            submit = dlg.locator("button[aria-label='Submit application'], button:has-text('Submit application')")
            if submit.count() and submit.first.is_visible():
                follow = dlg.locator("input[type='checkbox'][id*='follow'], label:has-text('Follow')").first
                try:
                    if follow.count() and follow.is_checked():
                        follow.click(); time.sleep(0.3)
                except Exception:
                    pass
                submit.first.click(); time.sleep(random.uniform(4, 6))
                after = self._modal().inner_text() if self._modal().count() else pg.locator("main").first.inner_text()
                result.status = "applied" if re.search(r"application (was )?sent|submitted|applied", after, re.I) else "applied_partial"
                result.notes = result.notes or "submitted"
                break
            nxt = dlg.locator("button[aria-label='Continue to next step'], button:has-text('Next'), button[aria-label='Review your application'], button:has-text('Review')").first
            if not nxt.count():
                self._dismiss(); return ApplyResult("failed", "no next/submit button", result.questions)
            nxt.click(); time.sleep(random.uniform(2.5, 3.5))
            errs = self._modal().evaluate("el => Array.from(el.querySelectorAll('[class*=error], [role=alert]')).filter(e=>e.offsetParent!==null && e.innerText.trim()).map(e=>e.innerText.trim().slice(0,60))") if self._modal().count() else []
            if errs and step > 0 and "Additional Questions" in text:
                # we already tried answering this page; a remaining error means a field we cannot fill
                self._dismiss(); return ApplyResult("deferred", "validation: " + "; ".join(errs[:3]), result.questions)
        self._dismiss()
        if result.status == "failed" and not result.notes:
            result.status = "deferred"; result.notes = "dialog did not reach submit within 12 steps"
        return result

    def _dismiss(self) -> None:
        pg = self.page
        try:
            d = self._modal()
            if d.count():
                btn = d.locator("button[aria-label='Dismiss'], button[aria-label='Done'], button:has-text('Done')").first
                if btn.count():
                    btn.click(); time.sleep(1.5)
            disc = pg.locator("button:has-text('Discard')")
            if disc.count() and disc.first.is_visible():
                disc.first.click(); time.sleep(1)
        except Exception:
            pass
