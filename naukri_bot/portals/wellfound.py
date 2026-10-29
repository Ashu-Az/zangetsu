"""Wellfound adapter: role listing pages in India, fresh and junior-friendly cards, Apply dialog with a short note."""
from __future__ import annotations

import logging
import random
import re
import time

from ..models import ApplyMode, JobCard, JobPosting
from .base import ApplyResult, Portal

log = logging.getLogger(__name__)

ROLE_SLUGS = {
    "software engineer": "software-engineer", "full stack developer": "full-stack-engineer", "data scientist": "data-scientist",
    "ai engineer": "artificial-intelligence-engineer", "machine learning engineer": "machine-learning-engineer",
    "backend developer": "backend-engineer", "backend engineer": "backend-engineer", "python developer": "python-developer", "software developer": "software-engineer", "generative ai engineer": "artificial-intelligence-engineer",
}


def cover_note(job: JobPosting, profile: dict) -> str:
    """profile.cover_note is a template with {title} and {company}; otherwise build a short generic note."""
    template = profile.get("cover_note")
    if template:
        return template.strip().format(title=job.title, company=job.company or "your team")
    skills = ", ".join((profile.get("skills") or [])[:8])
    role = profile.get("current_title", "software engineer")
    article = "an" if role[:1].lower() in "aeiou" else "a"
    return (f"Hi, I'm applying for the {job.title} role. I'm {article} {role} with "
            f"{profile.get('total_experience_years', '')} years of experience. Stack: {skills}. "
            f"Notice period: {profile.get('notice_period', 'short')}. Code: {(profile.get('links') or {}).get('github', '')}")


class WellfoundPortal(Portal):
    name = "wellfound"
    profile_dir = "wellfound-profile"
    daily_cap = 30

    @staticmethod
    def _question_for(dlg, box) -> str:
        """Question text for a screening textarea: its <label for=id>, else the nearest block's text."""
        box_id = box.get_attribute("id") or ""
        if box_id:
            lab = dlg.locator(f"label[for='{box_id}']")
            if lab.count() and lab.first.inner_text().strip():
                return " ".join(lab.first.inner_text().split())
        # the question can sit in a sibling block above the box with an empty <label> (Steps AI, 21 Oct 2026:
        # "What interests you about working for this company?" came back as ""): walk up until there is text,
        # and keep the last sentence that ends in "?" when the block holds more than the question
        txt = box.evaluate("""e => { let n = e;
            for (let i = 0; i < 6 && n; i++) {
                let p = n.previousElementSibling;
                while (p) { const t = (p.innerText || '').trim(); if (t) return t; p = p.previousElementSibling; }
                n = n.parentElement;
                const t = n && (n.innerText || '').trim(); if (t) return t;
            } return ''; }""")
        txt = " ".join((txt or "").split())
        qs = re.findall(r"[^?.!]*\?", txt)
        return (qs[-1].strip() if qs else txt)[:300]

    def search_urls(self) -> list[str]:
        slugs = {ROLE_SLUGS.get(k.lower(), k.lower().replace(" ", "-")) for k in self.settings.keywords}
        return [f"https://wellfound.com/role/l/{s}/india" for s in sorted(slugs)]

    def collect_cards(self, url: str) -> list[JobCard]:
        pg = self.page
        pg.goto(url, wait_until="domcontentloaded", timeout=60000); time.sleep(random.uniform(5, 7))
        for _ in range(6):
            pg.mouse.wheel(0, 1500); time.sleep(0.8)
        raw = pg.evaluate("""() => Array.from(document.querySelectorAll('a[href*="/jobs/"]')).filter(a => /\\/jobs\\/\\d+/.test(a.href)).map(a => {
            let row = a; for (let k = 0; k < 8; k++) { row = row.parentElement; if (row && /years of exp/.test(row.innerText || '') && /ago|today|yesterday/i.test(row.innerText || '')) break; }
            let comp = row; for (let k = 0; k < 6 && comp; k++) { comp = comp.parentElement; const h = comp && comp.querySelector('h2, a[href^="/company/"], a[href*="wellfound.com/company/"]'); if (h) { return {href: a.href.split('?')[0], row: row ? row.innerText : '', company: h.innerText.trim()}; } }
            return {href: a.href.split('?')[0], row: row ? row.innerText : '', company: ''};
        })""")
        out, seen = [], set()
        for r in raw:
            jid = re.search(r"/jobs/(\d+)", r["href"]).group(1)
            if jid in seen:
                continue
            seen.add(jid)
            lines = [l.strip() for l in r["row"].split("\n") if l.strip()]
            title = lines[0] if lines else ""
            exp = next((l for l in lines if "years of exp" in l), "")
            posted = next((l for l in lines if re.search(r"\bago\b|today|yesterday", l, re.I)), "")
            sal = next((l for l in lines if "₹" in l or "$" in l), "")
            loc = next((l for l in lines if "•" in l and ("office" in l.lower() or "remote" in l.lower() or "hybrid" in l.lower())), "")
            out.append(JobCard(job_id=jid, title=title, company=r["company"].split("\n")[0][:60], url=r["href"], experience=exp, location=loc, posted=posted, snippet=sal))
        return out

    def scrape(self, card: JobCard) -> tuple[JobPosting, ApplyMode]:
        pg = self.page
        pg.goto(card.url, wait_until="domcontentloaded", timeout=60000); time.sleep(random.uniform(5, 7))
        t = pg.locator("main").first.inner_text() if pg.locator("main").count() else pg.inner_text("body")
        job = JobPosting(job_id=card.job_id, url=card.url, title=card.title, company=card.company, location=card.location, posted=card.posted, salary=card.snippet)
        m = re.search(r"POSTED\s+([^\n|]+)", t, re.I)
        if m:
            job.posted = m.group(1).strip().lower()
        m = re.search(r"(\d+)\s*\+?\s*years of exp", card.experience)
        job.experience = f"{m.group(1)} - {int(m.group(1)) + 3} years" if m else ""
        m = re.search(r"₹\s?(\d+(?:\.\d+)?)L\s*[–-]\s*₹\s?(\d+(?:\.\d+)?)L", job.salary or t)
        if m:
            job.salary = f"{m.group(1)}-{m.group(2)} Lacs P.A."
        m = re.search(r"([^\n]+) at ([^\n]+)\n", t)
        if m and not job.company:
            job.company = m.group(2).strip()
        i = t.find("Role Overview"); i = i if i >= 0 else t.find("About the job"); job.description = t[i:i + 8000] if i >= 0 else t[:6000]
        # button text may carry an icon glyph ("✓ Applied"): compare on the letters only
        apply_btns = [re.sub(r"^[^a-z]+", "", b.inner_text().strip().lower()) for b in pg.locator("button").all() if b.is_visible()]
        apply_btns = [b for b in apply_btns if b.startswith("appl")]
        if any(b.startswith("applied") for b in apply_btns):
            return job, ApplyMode.ALREADY_APPLIED
        if any(b in ("apply", "apply now") for b in apply_btns):
            return job, ApplyMode.NAUKRI
        return job, ApplyMode.UNKNOWN

    def apply(self, job: JobPosting, mode: ApplyMode, answerer) -> ApplyResult:
        pg = self.page
        if pg.url.split("?")[0] != job.url:
            pg.goto(job.url, wait_until="domcontentloaded", timeout=60000); time.sleep(random.uniform(4, 6))
        btns = [b for b in pg.locator("button").all() if b.is_visible() and re.match(r"^apply( now)?$", b.inner_text().strip(), re.I)]
        if not btns:
            return ApplyResult("failed", "no visible Apply button")
        btns[0].scroll_into_view_if_needed(); btns[0].click(); time.sleep(random.uniform(3, 5))
        dlgs = [d for d in pg.locator("[role='dialog'], dialog[open]").all() if d.is_visible()]
        res = ApplyResult("failed")
        if not dlgs:
            return ApplyResult("failed", "no apply dialog")
        dlg = dlgs[-1]
        text = dlg.inner_text()
        if re.search(r"not accepting applications|cannot apply|no longer accepting", text, re.I):
            pg.keyboard.press("Escape")
            return ApplyResult("skipped", "employer not accepting applications from this location")
        # job outside the profile's preferred locations: tick "I can relocate to <city>" (profile says willing to relocate)
        if re.search(r"does not support the locations|Update your location preferences", text, re.I):
            if self.profile.get("willing_to_relocate", True):
                reloc = dlg.locator("label, [role='checkbox'], input[type='checkbox']").filter(has_text=re.compile(r"relocate", re.I)).first
                if not reloc.count():
                    reloc = dlg.get_by_text(re.compile(r"I can relocate", re.I)).first
                if reloc.count():
                    reloc.click(); time.sleep(0.8)
                    res.questions.append({"question": "I can relocate to the job location", "kind": "checkbox", "options": ["yes"], "answer": "yes", "source": "profile"})
            else:
                pg.keyboard.press("Escape"); return ApplyResult("skipped", "job location not in preferences and not willing to relocate")
        # Text boxes are either the application note or employer screening questions
        # ("customQuestionAnswers[...]"). Disabled boxes are never typed into.
        note_done = False
        for box in dlg.locator("textarea").all():
            if not box.is_visible() or not box.is_enabled():
                continue
            name = box.get_attribute("name") or ""
            q = self._question_for(dlg, box) if "customQuestion" in name else ""
            if "customQuestion" in name and not q.strip() and not note_done:
                # an unlabelled custom box is the company's "What interests you about working for this company?" note
                # (Ind-US Target, 19 Oct 2026): write the cover note there
                name = ""
            if "customQuestion" in name:
                ans = answerer(q, [], "text") if q else None
                if not ans or not (ans.answer_text or "").strip():
                    pg.keyboard.press("Escape")
                    return ApplyResult("deferred", f"unanswered: {q[:120]}", res.questions)
                box.click(); pg.keyboard.insert_text(ans.answer_text); time.sleep(0.6)
                res.questions.append({"question": q, "kind": "text", "options": [], "answer": ans.answer_text[:200], "source": "rules"})
            elif not note_done:
                note = cover_note(job, self.profile)
                box.click(); pg.keyboard.insert_text(note); time.sleep(0.8)
                res.questions.append({"question": "Application note", "kind": "text", "options": [], "answer": note[:120], "source": "template"})
                note_done = True
        # any extra radio/select questions
        for sel in dlg.locator("select").all():
            opts = [o.strip() for o in sel.locator("option").all_inner_texts() if o.strip() and not o.strip().lower().startswith("select")]
            q = (sel.get_attribute("aria-label") or "").strip()
            ans = answerer(q, opts, "dropdown") if q else None
            if ans and ans.selected_option_indices:
                sel.select_option(label=opts[ans.selected_option_indices[0]]); res.questions.append({"question": q, "kind": "dropdown", "options": opts, "answer": opts[ans.selected_option_indices[0]], "source": "rules"})
        sub = dlg.locator("button:has-text('Send application'), button:has-text('Apply now'), button:has-text('Submit'), button:has-text('Apply')").last
        if not sub.count():
            self.page.keyboard.press("Escape"); return ApplyResult("failed", "no submit button in dialog", res.questions)
        if not sub.is_enabled():
            # a required choice is still missing (e.g. location); try any unchecked checkbox in the dialog once
            for cb in dlg.locator("input[type='checkbox']").all():
                if not cb.is_checked():
                    cb.evaluate("e => e.click()"); time.sleep(0.6)
                    if sub.is_enabled(): break
        if not sub.is_enabled():
            pg.keyboard.press("Escape"); return ApplyResult("deferred", "submit disabled: a required field in the dialog is unfilled", res.questions)
        sub.click(); time.sleep(random.uniform(4, 6))
        after = (pg.locator("[role='dialog'], dialog[open]").last.inner_text() if pg.locator("[role='dialog'], dialog[open]").count() else "") + "\n" + (pg.locator("main").first.inner_text() if pg.locator("main").count() else "")
        if re.search(r"application (sent|submitted)|you applied|applied\b", after, re.I) or pg.locator("button:has-text('Applied')").count():
            res.status = "applied"; res.notes = "confirmed"
        else:
            res.notes = "no confirmation after submit"
        try:
            pg.keyboard.press("Escape")
        except Exception:
            pass
        return res
