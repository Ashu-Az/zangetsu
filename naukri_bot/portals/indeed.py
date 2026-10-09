"""Indeed adapter: date-sorted last-24h search, 'Easily apply' jobs, smartapply multi-step form driven by the rule-based answerer."""
from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import quote_plus

from ..models import ApplyMode, JobCard, JobPosting
from ..rules import availability_date, with_unit
from .linkedin import experience_from_text
from .base import ApplyResult, Portal

log = logging.getLogger(__name__)


class IndeedPortal(Portal):
    name = "indeed"
    profile_dir = "indeed-profile"
    daily_cap = 40

    def search_urls(self) -> list[str]:
        days = max(1, int(self.settings.max_job_age_days or 1))
        return [f"https://in.indeed.com/jobs?q={quote_plus(kw)}&l=India&fromage={days}&sort=date&start={s}" for kw in self.settings.keywords for s in (0, 10, 20, 30, 40)]

    def collect_cards(self, url: str) -> list[JobCard]:
        pg = self.page
        pg.goto(url, wait_until="domcontentloaded", timeout=60000); time.sleep(random.uniform(5, 7))
        if "captcha" in pg.title().lower() or pg.locator("iframe[src*='captcha'], #challenge-running").count():
            log.warning("[indeed] bot check on search page; waiting 20s for it to clear"); time.sleep(20)
        raw = pg.evaluate("""() => Array.from(document.querySelectorAll('[data-jk]')).map(e => { const c = e.closest('.job_seen_beacon, li, .result') || e; return {jk: e.getAttribute('data-jk'), text: c.innerText}; })""")
        out, seen = [], set()
        for r in raw:
            if not r["jk"] or r["jk"] in seen:
                continue
            seen.add(r["jk"])
            lines = [l.strip() for l in r["text"].split("\n") if l.strip()]
            easy = any("easily apply" in l.lower() for l in lines)
            lines2 = [l for l in lines if "easily apply" not in l.lower()]
            title = lines2[0] if lines2 else ""
            company = lines2[1] if len(lines2) > 1 else ""
            loc = lines2[2] if len(lines2) > 2 else ""
            sal = next((l for l in lines2 if "₹" in l), "")
            posted = next((l for l in lines2 if re.search(r"ago|just posted|today", l, re.I)), "today")
            out.append(JobCard(job_id=r["jk"], title=title, company=company, url=f"https://in.indeed.com/viewjob?jk={r['jk']}", location=loc, posted=posted, snippet=sal, skills=["easily apply"] if easy else []))
        return out

    def scrape(self, card: JobCard) -> tuple[JobPosting, ApplyMode]:
        pg = self.page
        pg.goto(card.url, wait_until="domcontentloaded", timeout=60000); time.sleep(random.uniform(4, 6))
        t = pg.inner_text("body")
        job = JobPosting(job_id=card.job_id, url=card.url, title=card.title, company=card.company, location=card.location, posted=card.posted, salary=card.snippet)
        i = t.find("Full job description"); job.description = t[i:i + 8000] if i >= 0 else t[:6000]
        job.experience = experience_from_text(job.description)
        m = re.search(r"₹[\d,]+(?:\s*-\s*₹[\d,]+)?\s*(?:a|per)\s*(year|month)", t)
        if m and not job.salary:
            job.salary = m.group(0)
        if re.search(r"You applied|Applied on \w+|Application submitted", t) and not pg.locator("a:has-text('Apply now'), button:has-text('Apply now')").count():
            return job, ApplyMode.ALREADY_APPLIED
        if pg.locator("a:has-text('Apply now'), button:has-text('Apply now')").count():
            return job, ApplyMode.NAUKRI
        if pg.locator("a:has-text('Apply on company site'), button:has-text('Apply on company site')").count():
            return job, ApplyMode.COMPANY_SITE
        return job, ApplyMode.UNKNOWN

    def _fill_page(self, ap, answerer, res: ApplyResult) -> bool:
        ok = True
        # radio groups by name
        groups: dict[str, list] = {}
        for r in ap.locator("input[type='radio']").all():
            groups.setdefault(r.get_attribute("name") or "", []).append(r)
        for name, radios in groups.items():
            if any(r.is_checked() for r in radios):
                continue
            q = radios[0].evaluate("e => { const f = e.closest('fieldset'); return f ? (f.querySelector('legend')||{}).innerText || '' : (e.closest('div')?.parentElement?.innerText||'').split('\\n')[0]; }").strip()
            opts = [ap.locator(f"label[for='{r.get_attribute('id')}']").inner_text().strip() if r.get_attribute("id") and ap.locator(f"label[for='{r.get_attribute('id')}']").count() else r.evaluate("e => (e.closest('label')||e.parentElement).innerText").strip() for r in radios]
            ans = answerer(q, opts, "radio")
            idx = ans.selected_option_indices[0] if ans and ans.selected_option_indices else None
            if idx is None:
                log.info("    unanswered radio: %r options=%s", q[:300], opts)
                res.notes = f"unanswered: {q[:80]}"; ok = False; continue
            radios[idx].evaluate("e => e.click()"); time.sleep(0.4)
            res.questions.append({"question": q, "kind": "radio", "options": opts, "answer": opts[idx], "source": "rules"})
        # checkbox groups ("Please share your shift preference": Night / Day / Evening ...)
        cgroups: dict[str, list] = {}
        for c in ap.locator("input[type='checkbox']").all():
            cgroups.setdefault(c.get_attribute("name") or "", []).append(c)
        for name, boxes in cgroups.items():
            if len(boxes) < 2 or any(b.is_checked() for b in boxes):
                continue
            q = boxes[0].evaluate("e => { const f = e.closest('fieldset'); return f ? (f.querySelector('legend')||{}).innerText || '' : ''; }").strip()
            if not q:
                continue
            opts = [b.evaluate("e => ((e.id && document.querySelector('label[for=\"'+e.id+'\"]')) || e.closest('label') || e.parentElement).innerText").strip() for b in boxes]
            ans = answerer(q, opts, "checkbox")
            idxs = ans.selected_option_indices if ans and ans.selected_option_indices else []
            if not idxs:
                if "*" in q:
                    log.info("    unanswered checkbox group: %r options=%s", q[:200], opts)
                    res.notes = f"unanswered: {q[:80]}"; ok = False
                continue
            for k in idxs:
                boxes[k].evaluate("e => e.click()"); time.sleep(0.3)
            res.questions.append({"question": q, "kind": "checkbox", "options": opts, "answer": ", ".join(opts[k] for k in idxs), "source": "rules"})
        for sel in ap.locator("select").all():
            sid = sel.get_attribute("id") or ""
            q = (ap.locator(f"label[for='{sid}']").inner_text().strip() if sid and ap.locator(f"label[for='{sid}']").count() else sel.get_attribute("aria-label") or "").strip()
            opts = [o.strip() for o in sel.locator("option").all_inner_texts()]
            real = [o for o in opts if o and not o.lower().startswith(("select", "choose"))]
            cur = sel.input_value()
            if cur and not cur.lower().startswith(("select", "choose")):
                continue
            ans = answerer(q, real, "dropdown")
            choice = real[ans.selected_option_indices[0]] if ans and ans.selected_option_indices else None
            if choice is None:
                res.notes = f"unanswered: {q[:80]}"; ok = False; continue
            sel.select_option(label=choice); time.sleep(0.3)
            res.questions.append({"question": q, "kind": "dropdown", "options": real, "answer": choice, "source": "rules"})
        for inp in ap.locator("input[type='text'], input[type='number'], input:not([type]), textarea").all():
            if inp.input_value().strip() or not inp.is_visible():
                continue
            iid = inp.get_attribute("id") or ""
            q = (ap.locator(f"label[for='{iid}']").inner_text().strip() if iid and ap.locator(f"label[for='{iid}']").count() else inp.get_attribute("aria-label") or inp.get_attribute("placeholder") or "").strip()
            if not q or re.search(r"email|phone|name|city|search", q, re.I):
                continue
            ans = answerer(q, [], "text")
            text = ans.answer_text if ans and ans.answer_text else None
            required = "*" in q or inp.get_attribute("required") is not None or inp.get_attribute("aria-required") == "true"
            if text is None and not required:
                continue                                   # optional (e.g. "Google Scholar Profile URL"): leave blank
            if text is None:
                res.notes = f"unanswered: {q[:80]}"; ok = False; continue
            if inp.evaluate("e => e.tagName") == "TEXTAREA":
                text = with_unit(q, text)
            # date fields ("When are you available to start?" -> DD/MM/YYYY): the day after the last working day
            ph = (inp.get_attribute("placeholder") or "") + " " + (inp.get_attribute("aria-describedby") or "") + " " + (inp.get_attribute("type") or "")
            if re.search(r"dd/mm/yyyy|mm/dd/yyyy|\bdate\b", ph, re.I) or re.search(r"available to start|start date|joining date|date of joining", q, re.I):
                d = availability_date(self.profile)
                if d and not re.match(r"\d{1,2}/\d{1,2}/\d{4}", text):
                    text = d.strftime("%m/%d/%Y") if re.search(r"mm/dd", ph, re.I) else d.strftime("%d/%m/%Y")
            inp.click(); ap.keyboard.type(text, delay=random.randint(40, 90)); time.sleep(0.3)
            res.questions.append({"question": q, "kind": "text", "options": [], "answer": text, "source": "rules"})
        return ok

    def apply(self, job: JobPosting, mode: ApplyMode, answerer) -> ApplyResult:
        pg = self.page
        if mode == ApplyMode.COMPANY_SITE:
            return ApplyResult("saved_for_manual", external_url=job.url)
        if pg.url.split("&")[0] != job.url:
            pg.goto(job.url, wait_until="domcontentloaded", timeout=60000); time.sleep(random.uniform(4, 6))
        before = set(p for p in pg.context.pages)
        pg.locator("a:has-text('Apply now'), button:has-text('Apply now')").first.click(); time.sleep(random.uniform(7, 9))
        ap = next((p for p in pg.context.pages if p not in before), None) or pg
        try:
            ap.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        # logged out: Apply lands on Indeed's sign-in (where macOS may pop up the Apple Account sheet). Never touch it.
        if "secure.indeed.com" in ap.url or re.search(r"/auth\b|/account/login", ap.url) or \
                ap.locator("text=Continue with Google").count():
            if ap is not pg:
                ap.close()
            raise RuntimeError("Indeed is logged out: sign in (Continue with Google) in the indeed-profile browser, then rerun")
        res = ApplyResult("failed")
        for step in range(10):
            time.sleep(random.uniform(2, 3))
            body = ap.inner_text("body")
            if re.search(r"application (has been )?submitted|Your application was sent|applied successfully", body, re.I):
                res.status = "applied"; res.notes = "confirmed"; break
            submit = ap.locator("button:has-text('Submit your application'), button:has-text('Submit')").first
            if submit.count() and submit.is_visible():
                for box in ap.locator("input[type='checkbox']").all():
                    try:
                        label = box.evaluate("e => ((e.id && document.querySelector('label[for=\"'+e.id+'\"]')) || e.closest('label') || e.parentElement).innerText || ''")
                        if box.is_checked() and re.search(r"job alert|similar jobs|email me|notify me|marketing|updates about", label, re.I):
                            box.evaluate("e => e.click()")
                    except Exception:
                        pass
                submit.click(); time.sleep(random.uniform(5, 7))
                body = ap.inner_text("body")
                still_open = ap.locator("button:has-text('Submit your application')").count() and \
                    ap.locator("button:has-text('Submit your application')").first.is_visible()
                confirmed = (re.search(r"application (has been )?submitted|Your application was sent|applied successfully|"
                                       r"Application submitted|you.ve applied", body, re.I) or "post-apply" in ap.url)
                # Form still open = nothing was sent (validation): retryable. Form gone without a confirmation =
                # probably sent: final, so a retry can't apply twice.
                res.status = "applied" if confirmed else ("failed" if still_open else "submitted_unconfirmed")
                res.notes = "submitted" if confirmed else ("clicked Submit, form still open" if still_open else "clicked Submit, no confirmation")
                break
            if re.search(r"resume", body, re.I) and ap.locator("input[type='radio']").count() and not ap.locator("select, textarea").count():
                # resume choice page: prefer the Indeed/profile resume option if nothing is selected
                radios = ap.locator("input[type='radio']").all()
                if not any(r.is_checked() for r in radios):
                    radios[0].evaluate("e => e.click()")
            elif not self._fill_page(ap, answerer, res):
                self._bail(ap); return ApplyResult("deferred", res.notes, res.questions)
            # Indeed renders one hidden "Continue" per resume card; only one is visible. Never click a hidden one.
            conts = [c for c in ap.locator("button:has-text('Continue'), button:has-text('Next'), "
                                           "button:has-text('Review your application'), button:has-text('Apply anyway')").all()
                     if c.is_visible() and c.is_enabled()]
            if not conts:
                self._bail(ap); return ApplyResult("failed", "no visible continue button", res.questions)
            # "It looks like you don't meet these employer requirements": Indeed offers "Apply anyway".
            if any("apply anyway" in c.inner_text().lower() for c in conts):
                self._bail(ap); return ApplyResult("skipped", "Indeed: does not meet employer requirements", res.questions)
            conts[0].click(); time.sleep(random.uniform(4, 6))
            errs = ap.evaluate("() => Array.from(document.querySelectorAll('[class*=error], [role=alert]')).filter(e=>e.offsetParent!==null && e.innerText.trim()).map(e=>e.innerText.trim().slice(0,60)).slice(0,4)")
            if errs and step >= 1:
                self._bail(ap); return ApplyResult("deferred", "validation: " + "; ".join(errs[:2]), res.questions)
        if ap is not pg:
            try: ap.close()
            except Exception: pass
        return res

    def _bail(self, ap) -> None:
        try:
            if ap is not self.page:
                ap.close()
        except Exception:
            pass
