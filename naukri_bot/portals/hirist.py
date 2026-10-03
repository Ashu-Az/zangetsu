"""Hirist adapter: /search/<keyword>?exp=N pages, card freshness, one-click Apply with optional screening dialog."""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import date
from urllib.parse import quote

from ..models import ApplyMode, JobCard, JobPosting
from .base import ApplyResult, Portal

log = logging.getLogger(__name__)


class HiristPortal(Portal):
    name = "hirist"
    profile_dir = "hirist-profile"
    daily_cap = 50

    def search_urls(self) -> list[str]:
        exp = self.settings.experience_years or ""
        return [f"https://www.hirist.tech/search/{quote(kw)}?page={p}&loc=&exp={exp}" for kw in self.settings.keywords for p in (1, 2, 3, 4, 5)]

    def collect_cards(self, url: str) -> list[JobCard]:
        pg = self.page
        pg.goto(url, wait_until="domcontentloaded", timeout=60000); time.sleep(random.uniform(4, 6))
        raw = pg.evaluate("""() => Array.from(document.querySelectorAll('a[href*="/j/"]')).map(a => {
            let n = a; for (let k = 0; k < 6; k++) { n = n.parentElement; if (n && n.innerText && n.innerText.length > 80) break; }
            return {href: a.href.split('?')[0], text: n ? n.innerText : a.innerText};
        })""")
        out, seen = [], set()
        for r in raw:
            m = re.search(r"-(\d{5,})$", r["href"])
            if not m or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            lines = [l.strip() for l in r["text"].split("\n") if l.strip()]
            title = lines[0] if lines else ""
            company = title.split(" - ")[0] if " - " in title else ""
            exp = next((l for l in lines if re.search(r"\d+\s*-\s*\d+\s*yrs", l, re.I)), "")
            loc = lines[lines.index(exp) + 1] if exp in lines and lines.index(exp) + 1 < len(lines) else ""
            posted = next((l for l in lines if l.lower().startswith("posted")), "")
            out.append(JobCard(job_id=m.group(1), title=title, company=company, url=r["href"], experience=exp, location=loc, posted=posted,
                               skills=[l for l in lines if "/" in l][:1]))
        return out

    def scrape(self, card: JobCard) -> tuple[JobPosting, ApplyMode]:
        pg = self.page
        pg.goto(card.url, wait_until="domcontentloaded", timeout=60000); time.sleep(random.uniform(4, 6))
        t = pg.inner_text("body")
        job = JobPosting(job_id=card.job_id, url=card.url, title=card.title, company=card.company, location=card.location, posted=card.posted)
        m = re.search(r"(\d+)\s*-\s*(\d+)\s*Years", t)
        job.experience = f"{m.group(1)} - {m.group(2)} years" if m else card.experience.replace("yrs", "years")
        m = re.search(r"Posted on:\s*(\d{2})/(\d{2})/(\d{4})", t)
        if m:
            d, mo, y = map(int, m.groups())
            age = (date.today() - date(y, mo, d)).days
            job.posted = "today" if age <= 0 else f"{age} days ago"
        i = t.find("Job Description"); job.description = t[i:i + 8000] if i >= 0 else t[:6000]
        m = re.search(r"\n([A-Za-z][^\n]{3,200})\nPosted on:", t)
        if m:
            job.skills = [s.strip() for s in re.split(r"(?<=[a-z])(?=[A-Z])", m.group(1)) if s.strip()]
        if re.search(r"\n(Applied|Already Applied|Application Sent)\b", t, re.I):
            return job, ApplyMode.ALREADY_APPLIED
        if pg.locator("button:has-text('Apply')").count():
            return job, ApplyMode.NAUKRI
        return job, ApplyMode.UNKNOWN

    def _dialog(self):
        return self.page.locator("[role='dialog'], .MuiDialog-paper, dialog[open]").last

    def apply(self, job: JobPosting, mode: ApplyMode, answerer) -> ApplyResult:
        pg = self.page
        if pg.url.split("?")[0] != job.url:
            pg.goto(job.url, wait_until="domcontentloaded", timeout=60000); time.sleep(random.uniform(3, 5))
        btn = pg.locator("button:has-text('Apply')").first
        if not btn.count():
            return ApplyResult("failed", "no Apply button")
        btn.click(); time.sleep(random.uniform(3, 5))
        res = ApplyResult("failed")
        for _ in range(6):
            dlg = self._dialog()
            if not dlg.count() or not dlg.is_visible():
                break
            text = dlg.inner_text()
            if re.search(r"applied successfully|application (has been )?(sent|submitted)|successfully applied", text, re.I):
                res.status = "applied"; res.notes = "dialog confirmed"
                for sel in ["button:has-text('Ok')", "button:has-text('Close')", "button:has-text('Done')"]:
                    if dlg.locator(sel).count(): dlg.locator(sel).first.click(); break
                return res
            # screening questions
            answered_any = False
            for grp in dlg.locator("[role='radiogroup']").all():
                q = (grp.get_attribute("aria-label") or grp.locator("xpath=preceding::*[self::p or self::label or self::legend][1]").first.inner_text()).strip()
                labels = grp.locator("label")
                opts = [labels.nth(i).inner_text().strip() for i in range(labels.count())]
                if any(r.is_checked() for r in grp.locator("input[type='radio']").all()):
                    continue
                ans = answerer(q, opts, "radio")
                idx = ans.selected_option_indices[0] if ans and ans.selected_option_indices else next((i for i, o in enumerate(opts) if o.lower() == "yes"), None)
                if idx is None:
                    self._close(); return ApplyResult("deferred", f"unanswered: {q[:80]}", res.questions)
                labels.nth(idx).click(); answered_any = True
                res.questions.append({"question": q, "kind": "radio", "options": opts, "answer": opts[idx], "source": "rules" if ans else "default-yes"})
            for inp in dlg.locator("input[type='text'], textarea, input:not([type])").all():
                if inp.input_value().strip():
                    continue
                q = (inp.get_attribute("aria-label") or inp.get_attribute("placeholder") or inp.locator("xpath=preceding::*[self::p or self::label][1]").first.inner_text()).strip()
                ans = answerer(q, [], "text")
                text_ans = ans.answer_text if ans and ans.answer_text else (str(self.profile.get("default_skill_years", 1)) if re.search(r"years|yrs", q, re.I) else None)
                if text_ans is None:
                    self._close(); return ApplyResult("deferred", f"unanswered: {q[:80]}", res.questions)
                inp.click(); pg.keyboard.type(text_ans, delay=50); answered_any = True
                res.questions.append({"question": q, "kind": "text", "options": [], "answer": text_ans, "source": "rules" if ans else "default"})
            sub = dlg.locator("button:has-text('Submit'), button:has-text('Apply'), button:has-text('Continue'), button:has-text('Next')").last
            if sub.count() and sub.is_visible():
                sub.click(); time.sleep(random.uniform(3, 4)); continue
            if not answered_any:
                break
        # verify on the page
        time.sleep(2)
        t = pg.inner_text("body")
        if re.search(r"applied successfully|application (sent|submitted)|\bApplied\b", t, re.I) or not pg.locator("button:has-text('Apply')").count():
            res.status = "applied" if res.status != "applied" else res.status
            res.notes = res.notes or "page shows applied"
        else:
            res.notes = "no applied marker after click"
        return res

    def _close(self) -> None:
        try:
            self.page.keyboard.press("Escape"); time.sleep(1)
        except Exception:
            pass
