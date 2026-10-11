"""Push headline, profile summary and key skills from profile/naukri_profile.yaml to Naukri.

Selectors verified live on 2026-10-02 against https://www.naukri.com/mnjuser/profile.
"""
from __future__ import annotations

import logging
import time

import yaml

from .config import PROFILE_DIR, STATE_DIR
from .humanize import pause

log = logging.getLogger(__name__)

PROFILE_URL = "https://www.naukri.com/mnjuser/profile"
DRAWER = ".profileEditDrawer"
MAX_KEY_SKILLS = 20   # hard cap on Naukri (verified 2026-10-03)


def load_content() -> dict:
    with open(PROFILE_DIR / "naukri_profile.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _open_drawer(page, section_id: str):
    page.locator(f"#{section_id}").scroll_into_view_if_needed()
    pause(0.5, 1.0)
    page.locator(f"#{section_id} [class*='edit']").first.click()
    drawer = page.locator(DRAWER).filter(has=page.locator("button"))
    for i in range(drawer.count()):
        if drawer.nth(i).is_visible():
            pause(1.0, 1.5)
            return drawer.nth(i)
    raise RuntimeError(f"edit drawer for #{section_id} did not open")


def _wait_closed(page, drawer, timeout_s: float = 10) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if not drawer.is_visible():
                return True
        except Exception:
            return True
        time.sleep(0.4)
    return False


def _set_textarea_and_save(page, section_id: str, textarea_id: str, text: str, label: str) -> bool:
    drawer = _open_drawer(page, section_id)
    # Naukri renames these ids now and then (resumeHeadlineTxt -> resumeHeadline, Oct 2026): accept the old id,
    # the id without its "Txt" suffix, or the drawer's only textarea
    box = drawer.locator(f"#{textarea_id}, #{textarea_id.removesuffix('Txt')}, textarea").first
    box.click()
    page.keyboard.press("Meta+A")
    page.keyboard.press("Backspace")
    box.fill(text)
    pause(0.6, 1.2)
    drawer.locator("button[type='submit'], button:has-text('Save')").first.click()
    ok = _wait_closed(page, drawer)
    pause(1.5, 2.5)
    shown = page.locator(f"#{section_id}").inner_text()
    ok = ok and text[:40] in shown
    log.info("%s: %s", label, "updated" if ok else "NOT confirmed")
    return ok


def update_headline(page, text: str) -> bool:
    return _set_textarea_and_save(page, "lazyResumeHead", "resumeHeadlineTxt", text, "Resume headline")


def update_summary(page, text: str) -> bool:
    # the summary widget is lazy-loaded further down the page
    for _ in range(8):
        if page.locator("#lazyProfileSummary [class*='edit']").count():
            break
        page.mouse.wheel(0, 600)
        time.sleep(0.6)
    return _set_textarea_and_save(page, "lazyProfileSummary", "profileSummaryTxt", text, "Profile summary")


def _chip_titles(drawer) -> list[str]:
    return drawer.locator(".chipsContainer .chip").evaluate_all("els => els.map(e => e.getAttribute('title') || e.innerText.replace('Cross','').trim())")


def update_key_skills(page, wanted: list[str]) -> bool:
    drawer = _open_drawer(page, "lazyKeySkills")
    wanted_norm = {w.partition("<-")[0].strip().lower(): w for w in wanted}

    # 1. remove chips not wanted
    for title in _chip_titles(drawer):
        if title.lower() not in wanted_norm:
            chip = drawer.locator(f".chipsContainer .chip[title='{title}']")
            if chip.count():
                chip.first.locator("a.close").click()
                log.info("  - removed %s", title)
                pause(0.3, 0.6)

    # 2. add missing ones through the suggestor
    present = {t.lower() for t in _chip_titles(drawer)}
    inp = drawer.locator("#keySkillSugg")
    for entry in wanted:
        # "Label <- query" types `query` (the input is capped at 25 chars) and picks `Label`
        skill, _, query = (x.strip() for x in entry.partition("<-"))
        if skill.lower() in present:
            continue
        inp.click()
        inp.fill("")
        page.keyboard.type((query or skill)[:25], delay=70)
        pause(1.2, 1.8)
        if len(present) >= MAX_KEY_SKILLS:
            log.warning("  + %s -> skipped, Naukri allows at most %d key skills", skill, MAX_KEY_SKILLS)
            continue
        options = page.locator("#sugLst_keySkillSugg li")
        picked = False
        for i in range(options.count()):
            o = options.nth(i)
            try:
                label = o.inner_text().strip().lower()
                # exact match, or a long specific name that the suggestion merely extends,
                # e.g. "natural language processing" -> "Natural Language Processing (NLP)"
                if o.is_visible() and (label == skill.lower() or (len(skill) >= 12 and label.startswith(skill.lower()))):
                    o.click()
                    picked = True
                    break
            except Exception:
                continue
        if not picked:
            # Naukri only accepts names from its own taxonomy; never guess from a partial match
            inp.fill("")
            log.warning("  + %s -> not in Naukri's skill list, skipped", skill)
            continue
        pause(0.5, 0.9)
        now = {t.lower() for t in _chip_titles(drawer)}
        added = now - present
        present = now
        log.info("  + %s -> %s", skill, ", ".join(sorted(added)) if added else "NOT added")

    before = _chip_titles(drawer)
    drawer.locator("#saveKeySkills").click()
    ok = _wait_closed(page, drawer)
    pause(1.5, 2.5)
    shown = page.locator("#lazyKeySkills").inner_text()
    ok = ok and all(s.lower() in shown.lower() for s in before[:5])
    log.info("Key skills: %s (%d chips)", "updated" if ok else "NOT confirmed", len(before))
    return ok


def run(browser, sections: list[str], new_only: bool = False) -> dict[str, bool]:
    content = load_content()
    browser.goto(PROFILE_URL, settle=(4, 5))
    page = browser.page
    results = {}
    if "headline" in sections:
        results["headline"] = update_headline(page, " ".join(content["headline"].split()))
    if "skills" in sections:
        results["skills"] = update_key_skills(page, content["key_skills"])
    if "summary" in sections:
        results["summary"] = update_summary(page, " ".join(content["summary"].split()))
    if "employment" in sections:
        for k, v in update_employment(page, content.get("employment", [])).items():
            results[f"employment:{k}"] = v
    if "projects" in sections:
        for k, v in update_projects(page, content.get("projects", []), new_only=new_only).items():
            results[f"project:{k}"] = v
    if "resume" in sections:
        results["resume"] = upload_resume(page, content.get("resume_file", ""))
    if "itskills" in sections:
        for k, v in update_it_skills(page, content.get("it_skills", [])).items():
            results[f"itskill:{k}"] = v
    if "accomplishments" in sections:
        for k, v in update_online_profiles(page, content.get("online_profiles", [])).items():
            results[f"online:{k}"] = v
        for k, v in update_work_samples(page, content.get("work_samples", [])).items():
            results[f"sample:{k}"] = v
    if "career" in sections and content.get("career_profile"):
        results["career"] = update_career_profile(page, content["career_profile"])
    if "education" in sections:
        results["education_dedupe"] = dedupe_education(page) >= 0
    if "dedupe-projects" in sections:
        results["project_dedupe"] = delete_duplicate_projects(page) >= 0
    return results


def _open_entry_form(page, click_locator, form_marker: str):
    """Click an edit/add control and wait until the form with `form_marker` is visible (one retry)."""
    for attempt in range(2):
        try:
            click_locator.scroll_into_view_if_needed()
            pause(0.4, 0.8)
            click_locator.click()
            page.wait_for_selector(form_marker, state="visible", timeout=10000)
            pause(1.0, 1.5)
            return page.locator(DRAWER).filter(has=page.locator(form_marker)).first
        except Exception as e:
            log.warning("  form %s did not open (attempt %d): %s", form_marker, attempt + 1, e.__class__.__name__)
            page.keyboard.press("Escape")
            pause(1.0, 1.5)
    raise RuntimeError(f"could not open form {form_marker}")


# ---------------------------------------------------------------------------
# Employment
# ---------------------------------------------------------------------------
def _scroll_to_load(page, section_id: str) -> None:
    for _ in range(12):
        loc = page.locator(f"#{section_id}")
        if loc.count() and loc.inner_text().strip():
            break
        page.mouse.wheel(0, 700)
        time.sleep(0.5)
    page.locator(f"#{section_id}").scroll_into_view_if_needed()
    pause(0.5, 1.0)


def _entry_blocks(page, section_id: str):
    """Return (index, text) per editable entry: the text of the nearest ancestor of each edit icon
    that contains exactly one edit icon (i.e. the entry's own card/row)."""
    sec = page.locator(f"#{section_id}")
    try:
        texts = sec.evaluate("""el => Array.from(el.querySelectorAll('[class*=edit]')).map(e => {
            let n = e;
            for (let k = 0; k < 8 && n && n !== el; k++) {
                n = n.parentElement;
                if (n && n.querySelectorAll('[class*=edit]').length === 1 && (n.innerText || '').trim().length > 8) return n.innerText;
            }
            return '';
        })""")
    except Exception:
        texts = []
    return list(enumerate(texts))


def _set_suggestor(page, drawer, input_id: str, value: str) -> None:
    """Type into a suggestor input and pick the exact suggestion if one appears; else keep typed text."""
    inp = drawer.locator(f"#{input_id}")
    inp.click()
    page.keyboard.press("Meta+A")
    page.keyboard.press("Backspace")
    page.keyboard.type(value[:60], delay=50)
    pause(1.0, 1.5)
    lb = inp.get_attribute("aria-controls")
    if lb:
        opts = page.locator(f"#{lb} li")
        for i in range(opts.count()):
            try:
                if opts.nth(i).is_visible() and opts.nth(i).inner_text().strip().lower() == value.lower():
                    opts.nth(i).click()
                    return
            except Exception:
                continue
    page.keyboard.press("Tab")


def update_employment(page, entries: list[dict]) -> dict[str, bool]:
    results = {}
    for spec in entries:
        org = spec["organisation"].lower()
        _scroll_to_load(page, "lazyEmployment")
        blocks = _entry_blocks(page, "lazyEmployment")
        idx = next((i for i, t in blocks if org in t.lower()), None)
        if idx is None:
            log.warning("Employment '%s': no matching entry on the profile", spec["organisation"])
            results[spec["organisation"]] = False
            continue
        try:
            drawer = _open_entry_form(page, page.locator("#lazyEmployment [class*='edit']").nth(idx), "#jobDescription")
        except RuntimeError as e:
            log.error("Employment '%s': %s", spec["organisation"], e)
            results[spec["organisation"]] = False
            continue
        if spec.get("designation"):
            current = drawer.locator("#designationSugg").input_value()
            if current.strip().lower() != spec["designation"].lower():
                _set_suggestor(page, drawer, "designationSugg", spec["designation"])
        desc = " ".join(spec["description"].split())
        box = drawer.locator("#jobDescription")
        box.click()
        page.keyboard.press("Meta+A")
        page.keyboard.press("Backspace")
        box.fill(desc)
        pause(0.6, 1.0)
        drawer.locator("#submitEmployment").click()
        ok = _wait_closed(page, drawer)
        pause(1.5, 2.5)
        if not ok:
            err = drawer.inner_text().replace("\n", " | ")[:300]
            log.error("Employment '%s': drawer stayed open: %s", spec["organisation"], err)
            page.keyboard.press("Escape")
            page.reload()
            pause(3, 4)
        shown = page.locator("#lazyEmployment").inner_text()
        ok = ok and desc[:60] in shown
        log.info("Employment '%s': %s", spec["organisation"], "updated" if ok else "NOT confirmed")
        results[spec["organisation"]] = ok
    return results


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------
def _pick_listbox(page, drawer, input_id: str, needle: str) -> bool:
    """Open a Naukri custom dropdown (input.srchTxt) and click the option containing `needle`."""
    inp = drawer.locator(f"#{input_id}")
    inp.click()
    pause(0.6, 1.0)
    lb = inp.get_attribute("aria-controls")
    opts = page.locator(f"#{lb} li a")
    n = needle.lower()
    for i in range(opts.count()):
        try:
            if n in opts.nth(i).inner_text().strip().lower():
                opts.nth(i).click()
                pause(0.3, 0.6)
                return True
        except Exception:
            continue
    log.warning("  dropdown %s: no option containing %r", input_id, needle)
    return False


def _fill_textarea(page, drawer, textarea_id: str, text: str) -> None:
    box = drawer.locator(f"#{textarea_id}")
    box.click()
    page.keyboard.press("Meta+A")
    page.keyboard.press("Backspace")
    box.fill(text)


def _fill_project_form(page, drawer, spec: dict, is_new: bool) -> None:
    title_box = drawer.locator("#projectTitle")
    title_box.click()
    page.keyboard.press("Meta+A")
    page.keyboard.press("Backspace")
    title_box.fill(spec["title"][:100])
    if spec.get("link_to"):
        _pick_listbox(page, drawer, "eduExpIdFor", spec["link_to"])
    if spec.get("client"):
        _set_suggestor(page, drawer, "clientName", spec["client"])
    if is_new or spec.get("force_dates"):
        drawer.locator("label[for='finished']").click()
        pause(0.5, 0.8)
        sm, sy = spec["start"]
        em, ey = spec["end"]
        _pick_listbox(page, drawer, "projStartYearFor", str(sy))
        _pick_listbox(page, drawer, "projStartMonthFor", str(sm))
        _pick_listbox(page, drawer, "projEndYearFor", str(ey))
        _pick_listbox(page, drawer, "projEndMonthFor", str(em))
    _fill_textarea(page, drawer, "projectDetails", " ".join(spec["details"].split()))
    pause(0.5, 1.0)


def update_projects(page, specs: list[dict], new_only: bool = False) -> dict[str, bool]:
    results = {}
    for spec in specs:
        key = spec.get("match") or spec["title"]
        _scroll_to_load(page, "lazyProject")
        blocks = _entry_blocks(page, "lazyProject")
        idx = next((i for i, t in blocks if key.lower() in t.lower()), None)
        is_new = idx is None
        if new_only and not is_new:
            continue
        try:
            ctl = page.locator("#lazyProject [class*='add']").first if is_new else page.locator("#lazyProject [class*='edit']").nth(idx)
            drawer = _open_entry_form(page, ctl, "#projectTitle")
            _fill_project_form(page, drawer, spec, is_new)
            drawer.locator("#submitProject").click()
            ok = _wait_closed(page, drawer)
        except Exception as e:
            log.error("Project '%s': %s", key, e)
            ok = False
        pause(1.5, 2.5)
        if not ok:
            try:
                log.error("Project '%s': drawer stayed open: %s", key, drawer.inner_text().replace("\n", " | ")[:300])
            except Exception:
                pass
            page.reload()
            pause(3, 4)
        shown = page.locator("#lazyProject").inner_text()
        ok = ok and spec["title"][:30].lower() in shown.lower()
        log.info("Project '%s': %s", key, ("added" if is_new else "updated") if ok else "NOT confirmed")
        results[key] = ok
    return results


# ---------------------------------------------------------------------------
# Resume file
# ---------------------------------------------------------------------------
def upload_resume(page, path: str) -> bool:
    from pathlib import Path
    p = Path(path)
    if not p.is_absolute():
        p = PROFILE_DIR.parent / p
    if not p.exists():
        log.error("resume_file not found: %s", p)
        return False
    page.locator("#attachCV").set_input_files(str(p))
    time.sleep(8)
    shown = page.locator("#lazyAttachCV").inner_text()
    ok = p.name in shown
    log.info("Resume upload: %s (%s)", "done" if ok else "NOT confirmed", shown.replace("\n", " | ")[:120])
    return ok


# ---------------------------------------------------------------------------
# IT skills table
# ---------------------------------------------------------------------------
def _confirm_delete(page) -> bool:
    """After clicking a drawer's Delete, press the confirmation dialog's Delete button."""
    for _ in range(8):
        time.sleep(0.5)
        for sel in ["button.btn-dark-ot:has-text('Delete')", "button:has-text('Delete')", "button:has-text('Yes')", "button:has-text('Confirm')"]:
            loc = page.locator(sel)
            for i in range(loc.count()):
                try:
                    if loc.nth(i).is_visible():
                        loc.nth(i).click()
                        pause(1.5, 2.5)
                        return True
                except Exception:
                    continue
    log.warning("  no delete confirmation button appeared")
    return False


def _pick_listbox_verified(page, drawer, input_id: str, needle: str, tries: int = 3) -> bool:
    for _ in range(tries):
        if _pick_listbox(page, drawer, input_id, needle):
            pause(0.3, 0.5)
            if needle.lower() in drawer.locator(f"#{input_id}").input_value().lower():
                return True
        page.keyboard.press("Escape") if False else None
        pause(0.6, 1.0)
    return False


def update_it_skills(page, specs: list[dict]) -> dict[str, bool]:
    results = {}
    for spec in specs:
        name = spec["name"]
        page.reload()
        pause(4, 5)
        _scroll_to_load(page, "lazyITSkills")
        blocks = _entry_blocks(page, "lazyITSkills")
        idx = next((i for i, t in blocks if t.lower().split("\n")[0].strip() == name.lower()), None)
        if spec.get("delete"):
            if idx is None:
                results[name] = True
                continue
            try:
                drawer = _open_entry_form(page, page.locator("#lazyITSkills [class*='edit']").nth(idx), "#itSkillSugg")
                drawer.locator(".delete, span:has-text('Delete')").first.click()
                _confirm_delete(page)
                ok = _wait_closed(page, drawer)
            except Exception as e:
                log.error("IT skill '%s' delete: %s", name, e)
                ok = False
            pause(1.5, 2.0)
            names = [t.lower().split("\n")[0].strip() for _, t in _entry_blocks(page, "lazyITSkills")]
            ok = ok and name.lower() not in names
            log.info("IT skill '%s': %s", name, "deleted" if ok else "NOT deleted")
            results[name] = ok
            continue
        try:
            ctl = page.locator("#lazyITSkills [class*='add']").first if idx is None else page.locator("#lazyITSkills [class*='edit']").nth(idx)
            drawer = _open_entry_form(page, ctl, "#itSkillSugg")
            if idx is None:
                _set_suggestor(page, drawer, "itSkillSugg", name)
            # dropdowns first (typing into the version box afterwards does not disturb them)
            if spec.get("last_used"):
                if not _pick_listbox_verified(page, drawer, "lastUsedDroopeFor", str(spec["last_used"])):
                    log.warning("  %s: could not set last used = %s", name, spec["last_used"])
            if spec.get("years") is not None:
                _pick_listbox_verified(page, drawer, "expYearDroopeFor", f"{spec['years']} Year")
            if spec.get("months") is not None:
                _pick_listbox_verified(page, drawer, "expMonthDroopeFor", f"{spec['months']} Month")
            if spec.get("version") is not None:
                v = drawer.locator("#version")
                v.click(); page.keyboard.press("Meta+A"); page.keyboard.press("Backspace"); v.fill(str(spec["version"])[:10])
            pause(0.4, 0.8)
            drawer.locator("#saveITSkills").click()
            ok = _wait_closed(page, drawer)
            if not ok:
                log.error("IT skill '%s': %s", name, drawer.evaluate("el => Array.from(el.querySelectorAll('[class*=err]')).filter(e=>e.offsetParent!==null&&e.innerText.trim()).map(e=>e.innerText.trim().slice(0,100)).join(' / ')"))
                page.keyboard.press("Escape")
        except Exception as e:
            log.error("IT skill '%s': %s", name, str(e)[:160])
            ok = False
        pause(1.5, 2.0)
        row = next((t for _, t in _entry_blocks(page, "lazyITSkills") if t.lower().split("\n")[0].strip() == name.lower()), "")
        ok = ok and bool(row) and (not spec.get("last_used") or str(spec["last_used"]) in row)
        log.info("IT skill '%s': %s -> %s", name, ("added" if idx is None else "updated") if ok else "NOT confirmed", row.replace("\n", " | ")[:60])
        results[name] = ok
    return results


# ---------------------------------------------------------------------------
# Accomplishments: online profiles and work samples
# ---------------------------------------------------------------------------
def _acc_add_control(page, block_title: str):
    """The 'Add' link that belongs to the accomplishment block whose heading is block_title."""
    return page.locator("#lazyAccomplishment").locator(
        f"xpath=.//*[normalize-space(text())='{block_title}']/ancestor::*[.//a[contains(@class,'add')]][1]//a[contains(@class,'add')]").first


def update_online_profiles(page, specs: list[dict]) -> dict[str, bool]:
    results = {}
    for spec in specs:
        _scroll_to_load(page, "lazyAccomplishment")
        acc = page.locator("#lazyAccomplishment")
        existing = acc.locator(f"a[href='{spec['url']}']")
        is_new = existing.count() == 0
        try:
            if is_new:
                drawer = _open_entry_form(page, _acc_add_control(page, "Online profile"), "#online-profile-name_0")
            else:
                edit = existing.first.locator("xpath=./preceding::*[contains(@class,'edit')][1]")
                drawer = _open_entry_form(page, edit, "#online-profile-name_0")
            for fid, val in [("online-profile-name_0", spec["name"]), ("online-profile-url_0", spec["url"])]:
                f = drawer.locator(f"#{fid}"); f.click(); page.keyboard.press("Meta+A"); page.keyboard.press("Backspace"); f.fill(val)
            _fill_textarea(page, drawer, "online-profile-description_0", " ".join(spec.get("description", "").split()))
            drawer.locator("#saveOnlineProfilesBtn").click()
            ok = _wait_closed(page, drawer)
            if not ok:
                log.error("Online profile '%s': %s", spec["name"], drawer.inner_text().replace("\n", " | ")[:200])
                page.reload(); pause(3, 4)
        except Exception as e:
            log.error("Online profile '%s': %s", spec["name"], e)
            ok = False
        pause(1.5, 2.0)
        ok = ok and spec["url"] in page.locator("#lazyAccomplishment").inner_text()
        log.info("Online profile '%s': %s", spec["name"], ("added" if is_new else "updated") if ok else "NOT confirmed")
        results[spec["name"]] = ok
    return results


def update_work_samples(page, specs: list[dict]) -> dict[str, bool]:
    results = {}
    for spec in specs:
        _scroll_to_load(page, "lazyAccomplishment")
        acc = page.locator("#lazyAccomplishment")
        if spec["url"].rstrip("/") in acc.inner_text() or acc.locator(f"a[href^='{spec['url'].rstrip('/')}']").count():
            log.info("Work sample '%s': already present", spec["title"])
            results[spec["title"]] = True
            continue
        try:
            drawer = _open_entry_form(page, _acc_add_control(page, "Work sample"), "#workSample-title_0")
            for fid, val in [("workSample-title_0", spec["title"]), ("workSample-url_0", spec["url"])]:
                f = drawer.locator(f"#{fid}"); f.click(); f.fill(val)
            sm, sy = spec["start"]
            _pick_listbox(page, drawer, "workSample-startDate-year-0For", str(sy))
            _pick_listbox(page, drawer, "workSample-startDate-month-0For", str(sm))
            if spec.get("current"):
                drawer.locator("label[for='currentlyWorkingOn_0'], #currentlyWorkingOn_0").first.click()
            else:
                em, ey = spec["end"]
                _pick_listbox(page, drawer, "workSample-endDate-year-0For", str(ey))
                _pick_listbox(page, drawer, "workSample-endDate-month-0For", str(em))
            _fill_textarea(page, drawer, "workSample-description_0", " ".join(spec["description"].split())[:500])
            drawer.locator("#saveWorkSampleBtn").click()
            ok = _wait_closed(page, drawer)
            if not ok:
                log.error("Work sample '%s': %s", spec["title"], drawer.inner_text().replace("\n", " | ")[:200])
                page.reload(); pause(3, 4)
        except Exception as e:
            log.error("Work sample '%s': %s", spec["title"], e)
            ok = False
        pause(1.5, 2.0)
        ok = ok and spec["title"][:25].lower() in page.locator("#lazyAccomplishment").inner_text().lower()
        log.info("Work sample '%s': %s", spec["title"], "added" if ok else "NOT confirmed")
        results[spec["title"]] = ok
    return results


# ---------------------------------------------------------------------------
# Career profile
# ---------------------------------------------------------------------------
def update_career_profile(page, spec: dict) -> bool:
    _scroll_to_load(page, "lazyDesiredProfile")
    drawer = _open_entry_form(page, page.locator("#lazyDesiredProfile [class*='edit']").first, "#industryFor")
    try:
        if spec.get("industry"):
            _pick_listbox(page, drawer, "industryFor", spec["industry"])
        if spec.get("department") and spec["department"].lower() not in drawer.locator("#fareaDroopeFor").input_value().lower():
            _pick_listbox(page, drawer, "fareaDroopeFor", spec["department"])
            pause(1.5, 2.0)
        if spec.get("role_category") and spec["role_category"].lower() not in drawer.locator("#roleCategoryDroopeFor").input_value().lower():
            _pick_listbox(page, drawer, "roleCategoryDroopeFor", spec["role_category"])
            pause(1.5, 2.0)
        if spec.get("job_role"):
            _pick_listbox(page, drawer, "roleDroopeFor", spec["job_role"])
            pause(0.5, 0.8)
        for role in spec.get("preferred_roles_add", []):
            inp = drawer.locator("#roleSugg"); inp.click(); inp.fill(""); page.keyboard.type(role[:25], delay=60); pause(1.0, 1.5)
            lb = inp.get_attribute("aria-controls"); opts = page.locator(f"#{lb} li")
            hit = False
            for i in range(opts.count()):
                if opts.nth(i).is_visible() and opts.nth(i).inner_text().strip().lower() == role.lower():
                    opts.nth(i).click(); hit = True; break
            log.info("  preferred role %s: %s", role, "added" if hit else "not in list")
        for loc_name in spec.get("locations_add", []):
            inp = drawer.locator("#locationSugg"); inp.click(); inp.fill(""); page.keyboard.type(loc_name[:25], delay=60); pause(1.0, 1.5)
            lb = inp.get_attribute("aria-controls"); opts = page.locator(f"#{lb} li")
            for i in range(opts.count()):
                if opts.nth(i).is_visible() and loc_name.lower() in opts.nth(i).inner_text().strip().lower():
                    opts.nth(i).click(); break
        drawer.locator("#saveDesiredProfile").click()
        ok = _wait_closed(page, drawer)
        if not ok:
            log.error("Career profile: %s", drawer.inner_text().replace("\n", " | ")[:300])
            page.reload(); pause(3, 4)
    except Exception as e:
        log.error("Career profile: %s", e)
        ok = False
    pause(1.5, 2.0)
    shown = page.locator("#lazyDesiredProfile").inner_text()
    ok = ok and (not spec.get("job_role") or spec["job_role"].lower() in shown.lower())
    log.info("Career profile: %s", "updated" if ok else "NOT confirmed")
    return ok


# ---------------------------------------------------------------------------
# Education: remove duplicate entries
# ---------------------------------------------------------------------------
def dedupe_education(page) -> int:
    """Delete education entries whose (course, institute, years) text repeats an earlier one."""
    removed = 0
    for _ in range(5):
        _scroll_to_load(page, "lazyEducation")
        blocks = _entry_blocks(page, "lazyEducation")
        seen, dup_idx = set(), None
        for i, t in blocks:
            key = " ".join(t.replace("editOneTheme", "").replace("Primary", "").split()).lower()[:120]
            if key in seen:
                dup_idx = i
                break
            seen.add(key)
        if dup_idx is None:
            break
        try:
            drawer = _open_entry_form(page, page.locator("#lazyEducation [class*='edit']").nth(dup_idx), "#institute")
            drawer.locator(".delete, span:has-text('Delete')").first.click()
            _confirm_delete(page)
            _wait_closed(page, drawer)
            pause(2.0, 3.0)
            removed += 1
            log.info("Education: deleted duplicate entry #%d", dup_idx)
        except Exception as e:
            log.error("Education dedupe: %s", e)
            page.reload(); pause(3, 4)
            break
    return removed


def delete_duplicate_projects(page) -> int:
    """Delete later project entries whose title repeats an earlier one."""
    removed = 0
    for _ in range(6):
        _scroll_to_load(page, "lazyProject")
        titles = [l.replace("editOneTheme", "").strip() for l in page.locator("#lazyProject").inner_text().split("\n") if "editOneTheme" in l]
        seen, dup_idx = set(), None
        for i, t in enumerate(titles):
            k = t.lower()
            if k in seen:
                dup_idx = i
                break
            seen.add(k)
        if dup_idx is None:
            break
        try:
            drawer = _open_entry_form(page, page.locator("#lazyProject [class*='edit']").nth(dup_idx), "#projectTitle")
            drawer.locator(".delete, span:has-text('Delete')").first.click()
            _confirm_delete(page)
            _wait_closed(page, drawer)
            pause(2.0, 3.0)
            removed += 1
            log.info("Project: deleted duplicate '%s'", titles[dup_idx])
        except Exception as e:
            log.error("Project dedupe: %s", e)
            page.reload(); pause(3, 4)
            break
    return removed


# ------------------------------------------------------------------ freshness refresh
REFRESH_STATE = STATE_DIR / "profile_refresh.json"
MIN_REFRESH_GAP_H = 3


def refresh(browser, force: bool = False) -> tuple[bool, str]:
    """Re-save the resume headline so Naukri marks the profile as updated today.

    Recruiter search on Naukri favours recently updated profiles, and any saved edit counts. The headline alternates
    between `headline` and `headline_alt` from naukri_profile.yaml (or the same text with a trailing full stop), so
    the content never really changes. At most one refresh per MIN_REFRESH_GAP_H hours."""
    import json
    from datetime import datetime
    state = {}
    if REFRESH_STATE.exists():
        try:
            state = json.loads(REFRESH_STATE.read_text())
        except ValueError:
            state = {}
    last = state.get("last")
    if last and not force:
        age_h = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
        if age_h < MIN_REFRESH_GAP_H:
            return False, f"skipped: last refresh {age_h:.1f} h ago (minimum gap {MIN_REFRESH_GAP_H} h)"
    # Read the headline as it is on Naukri now (never overwrite it with stale text from a file), then toggle a
    # trailing full stop: the content stays the same, but Naukri records an update.
    browser.goto(PROFILE_URL, settle=(4, 5))
    page = browser.page
    try:
        drawer = _open_drawer(page, "lazyResumeHead")
        current = " ".join(drawer.locator("#resumeHeadlineTxt, #resumeHeadline").input_value(timeout=15000).split())
    except Exception as e:                       # drawer slow or layout changed: skip this refresh, try next time
        return False, f"could not open the headline editor ({type(e).__name__}); not touching it"
    page.keyboard.press("Escape"); pause(1.0, 1.5)
    if len(current) < 20:
        return False, "could not read the current headline; not touching it"
    text = current[:-1] if current.endswith(".") else current + "."
    use_alt = text.endswith(".")
    browser.goto(PROFILE_URL, settle=(3, 4))
    ok = update_headline(browser.page, text)
    if not ok:
        return False, "headline save failed"
    browser.goto(PROFILE_URL, settle=(3, 4))
    body = browser.page.inner_text("body")
    import re as _re
    m = _re.search(r"(Profile )?[Ll]ast updated\s*[-:]?\s*([^\n]{2,20})", body)
    seen = m.group(2).strip() if m else "?"
    state.update({"last": datetime.now().isoformat(timespec="minutes"), "used_alt": use_alt, "naukri_says": seen})
    REFRESH_STATE.parent.mkdir(parents=True, exist_ok=True)
    REFRESH_STATE.write_text(json.dumps(state, indent=2))
    return True, f"headline re-saved ({'alt' if use_alt else 'base'}); Naukri shows last updated: {seen}"
