# Zangetsu: how the bot works now, and what we fixed (13–16 Oct 2026)

This is the plain-English state of the job bot after four days of runs and fixes. The first half explains how
each step works today. The second half lists every fix, grouped by area, with the reason behind it. The last
sections cover what still needs you and how to run things.

Nothing here is committed to git yet. All 197 tests pass (`.venv/bin/python -m pytest -q tests`).

---

## 1. The daily run, step by step

Run it with `scripts/cycle.sh morning` (morning) or `scripts/cycle.sh` (later in the day). For company-site jobs,
follow it with `scripts/hunt.sh company`.

| # | Step | What it does now | Where |
|---|---|---|---|
| 1 | Profile refresh | Re-saves your Naukri headline so you show as "updated today". Waits up to 8 s for the edit drawer and retries once. | `naukri_profile.py` |
| 2 | **Neo (Naukri Pro AI agent)** | Asks Neo in its chat for jobs, opens each card, checks the job page, applies with Neo's **custom/tailored resume**, answers the recruiter's questions, and confirms "Applied". | `tools/neo_apply.py` |
| 3 | NVites | Applies to recruiter invitations that pass your filters; saves employer-site links. | `nvites.py` |
| 4 | Early access | Shares interest in Naukri Pro pre-launch roles that pass your filters. | `main.py` |
| 5 | Naukri search | Searches your keywords, ranks by fewest applicants, applies. Leaves **8 applications** of the 50/day cap for Neo and NVites. | `main.py`, `ranking.py` |
| 6 | Hirist | Searches and applies; stops paging a keyword after 2 pages of already-seen jobs. | `portals/hirist.py`, `portals/base.py` |
| 7 | Recruiter emails | Emails recruiters whose job descriptions include an email address, with a tailored resume (needs a strong enough match). | `outreach.py` |
| 8 | Top companies | Finds top-company Naukri postings that apply on the company's own site and saves the links. | `main.py` |
| 9 | Workday | Applies on Workday career sites with a resume tailored to each job description. | `ats/workday.py` |
| 10 | Startup boards | Greenhouse / Lever / Ashby boards: new fits only, at most 2 per company per month. | `ats/top_companies.py` |
| 11 | Company sites (`hunt.sh company`) | For every "apply on company site" job: saves the link, tailors a resume to the job description, fills the employer's form, submits when every field is known. | `ats/generic_form.py`, `main.py` |

### How every answer is chosen (in this order)

1. **Cache:** questions you answered by hand before.
2. **Rules:** `rules.py` plus the facts in `profile/profile.yaml` (CTC, notice, skills and years, education,
   addresses, yes/no policies, 31 written answers under `canned_answers`).
3. **Form-specific logic:** Workday dropdowns (`workday.py → _choice_for`), company forms
   (`generic_form.py → _pick / _question_value`).
4. **AI model** (Groq/Anthropic, from `.env`) for open questions, told to answer only from your profile and never to
   claim an action that didn't happen.
5. **Nothing fits:** the question goes to **`output/questions_for_ashu.csv`** (the inbox) and the form is left for you.

---

## 2. Neo: how it works now

Neo is a chat agent, so the bot now uses it like a person does:

```text
open Neo → type "Find jobs matching my profile" (then "Show me more jobs like these" ×2)
  └─ for each job card:
       open card → read job id → check the job page in a side tab
         (already applied? excluded title like Senior/Lead? technical title? experience fits? posted ≤ 30 days?)
       → click "Apply with custom Resume" on the job panel
       → wait for Neo's resume step:
            "Apply with tailored Resume"  (if Neo tailored it)  else  "Use original Resume"
       → "Add missing details" → answer the recruiter's questions in the drawer → Save
       → wait for "Successfully applied to <company>"
       → confirm "Applied" on the job page → record it
```

**Neo fixes (14–16 Oct):**

| Problem you saw | Cause | Fix |
|---|---|---|
| Neo applied to almost nothing | The old tool loaded Neo and grabbed only 0–4 leftover cards | It now **asks Neo for jobs** in the chat (3 rounds) |
| "It just reloads or skips" the tailored resume | The bot never clicked Neo's buttons; it applied through the plain job page | Drives **Apply with custom Resume → Apply with tailored Resume / Use original Resume → Add missing details** |
| Tailored resume ready but never applied (Tech Mahindra) | It clicked **old buttons from earlier jobs** still in the chat | Only clicks buttons that appear **after your latest "Apply with custom Resume" bubble** |
| Clicked its own chat bubble (Razorthink) | Neo echoes "Apply with custom Resume" as a user message | Only real buttons count; one job's error no longer stops the whole run |
| Answers "rejected 3x" | Typed into **Naukri's top search bar** and clicked the wrong Save | Drawer lookups ignore the site header, search bar and Neo's chat box; clicks the real **Save** |
| Open questions "deferred, needs the model" | The tool ran **without the AI model** | The model is switched on |
| Good jobs skipped as "too old" | Search rule of 1 day applied to Neo | Neo picks are allowed up to **30 days** old |
| "AI Security Engineer" skipped | Title wasn't in the search keyword list | Neo ignores the keyword list… |
| …then **Okta "Enterprise Account Executive"** got applied | …nothing stopped non-tech titles | Neo now requires a **technical title**; sales titles are excluded everywhere |
| Applications stuck when the daily cap was hit | Unapplied Neo jobs were forgotten | Kept in `state/neo_pending.json` and retried first next run (4 days) |

---

## 3. Questionnaire: answers that are now correct

### Wrong answers that were fixed

| Question | Was | Now |
|---|---|---|
| "Years of **work experience** with Django / PySpark / Azure?" | 2 (overclaim) | Model or you decide; never total tenure |
| "Years in Voice AI / AI Agents / Agentic AI Development" | 2 (bare "AI" matched) | Most specific skill wins (Agentic AI = 1); unknown stays unknown |
| "Are any of your **relatives** currently employed by X?" | **Yes** (S&P Global) | **No** |
| "Total Experience (**Months**)" | 0 | **24** |
| "Post-Graduation Percentage / CGPA" | 7.22 (your B.Tech CGPA) | **NA** |
| "Have you been enrolled in WorldQuant University?" | your college name | **No** |
| "Fill the form (mandatory): forms.gle/…" | "Yes – filled the form" (nobody had) | Link saved to `output/forms_to_fill.md` for you; model barred from claiming things were done |
| Race / ethnicity with "American Indian" in the list | picked "American Indian" | picks **Asian / Indian** |
| "Years in a tool you never used" (Robot Framework, Playwright) | application abandoned | **0**, application goes through, logged in the inbox |

### New answers added (from your decisions)

| Topic | Answer |
|---|---|
| Date of birth | from `profile.yaml` (`date_of_birth`), also used for the Workday date picker |
| Immediate joiner / notice options with "Immediately" | Yes / "Available immediately" (last day at Quess 20 Oct) |
| Date available / start date | 21/10/2026 |
| Relocation assistance | No (relocation itself: Yes) |
| Diversity questions | Truthful: heterosexual, not LGBTQ, not transgender, not Hispanic, Asian (Indian) |
| "How did you hear" with no Naukri/Other | Any generic job board; free-text box → "Naukri.com" |
| Current offers | No |
| Research output | Yes only for open-source AI/ML (S-SPPO); No for papers |
| API security testing / application security | 2 years |
| Online interview / DSA rounds | Yes (in-person outside Delhi NCR: No) |
| Valid work permit (India jobs) | Yes |
| Graduation year / completion month | 2024 / 10/2024 |
| University name (free text) | Netaji Subhas University of Technology (NSUT), Delhi; also matches the old name NSIT |
| SSC / HSC "Percentage / CGPA" | your 10th / 12th percentages from the profile |
| Desired pay / expected CTC | 14 (LPA, negotiable) |
| SQL and other resume skills rated 1–10 | 8 |
| BI tools (Tableau / Power BI) | No (not on your resume) |
| Employment type | Full-time |
| IT background, English, preferred language | Yes, "speak and write", English |
| UAN / Form 16, employment status, current company, phone + email | Yes, "employed, serving notice", your current company, phone + email from the profile |
| Statistics screening (97% confidence = true 97%? correlation = cause?) | No / No, plus written answers on the discount example and customer segments |
| Written answers in your voice | code philosophy, technical challenge, can technology solve problems, best skills, biggest project, passion, bio, anything else, securing AI/RAG apps |

**Rule of thumb now built in:** yes to willingness and to skills you really use; no or 0 only when the true answer
is no; never invent numbers, papers, offers or completed actions.

---

## 4. Workday and company-site forms

| Problem | Fix |
|---|---|
| New Workday accounts stuck at Sign In | Needs email verification; `mailbox.py` opens the link automatically **once `GMAIL_APP_PASSWORD` is in `.env`** |
| "How did you hear about us" (huge lists: S&P, Citi, Autodesk) | Searches Other → Naukri → company careers → LinkedIn → job board; scrolls the whole list if typing doesn't filter; works inside sub-lists too |
| Degree lists with ISCED labels / "B.Eng" | Picks Bachelor's (ISCED 6) / B.Eng |
| Field of Study long A–Z list | Searches Information Technology → Computer Science → Other |
| School not found (Citi) | Also searches "Netaji Subhas", NSIT and many "not listed" spellings |
| Previous worker / contractor of X | No |
| Schedule type, assessment accommodation, HiredScore consent (S&P) | Hybrid, No, I consent |
| "Enter today's date to confirm" | Today's date |
| Privacy acknowledgements ("how my information will be processed", demographic survey consent) | Ticked; legal claims (work authorisation, etc.) are never auto-ticked |
| Door number / building / company on Workday | From your address and current company |
| Fields that appear only after an earlier answer (iTechnosol) | Fills again and retries Submit once |
| Labels ending in "(required)" didn't match rules | Suffix stripped before matching |
| Client / sales enquiry forms (Xonier, Start Designs) | Detected and skipped; both companies blocked |
| **A form you were typing in got replaced** | **A tab left for you is never reused**; the next form opens in a new tab and the window stays open until you close it |

---

## 5. Other fixes

| Area | Fix |
|---|---|
| Outreach | No more emails to non-recruiter inboxes (accommodations@, compliance@, sales@…); summary counts fixed; emails say "immediate joiner" |
| Title filters | Teacher, tutor, mentor, account executive, business development, customer success, marketing… excluded |
| NVites | No crash when an invite disappears mid-list |
| Hirist | Stops paging stale keywords (was ~14 min for 11 new jobs) |
| Daily cap | Search keeps 8 of 50 for Neo/NVites (`PRIORITY_RESERVE` in `.env`) |

---

## 6. Files to check after a run

| File | What it is |
|---|---|
| `output/questions_for_ashu.csv` | Every question the bot couldn't answer, once each, with company and options. Status `new` = needs a rule or your answer |
| `output/forms_to_fill.md` | External forms (Google Forms etc.) recruiters asked you to fill |
| `output/applications.csv` | Every application and its status (`applied`, `needs_you`, `no_form`, …) |
| `output/company_site_jobs.csv` | All saved "apply on company site" links |
| `state/neo_pending.json` | Neo jobs to retry next run |
| `output/cycles/*.log` | Full logs of each run |

---

## 7. What still needs you

- **CAPTCHAs, passwords/account creation, video resumes**: the bot fills everything else and leaves the tab open.
- **Workday email verification**: add `GMAIL_APP_PASSWORD` to `.env` (myaccount.google.com/apppasswords).
- **Father's name**: one company form required it; not in your profile yet.
- **Questions marked `new` in the inbox**: answer them and they become rules.
- **Roles abroad** (GitLab's country list had no India): correctly left unsubmitted.

---

## 8. How to run

```bash
cd zangetsu
scripts/cycle.sh morning            # morning: refresh, Neo, NVites, early access, search, Hirist, emails, Workday, boards
scripts/hunt.sh company             # company-site stage for today's saved links
.venv/bin/python tools/neo_apply.py --max 15          # Neo only
.venv/bin/python tools/neo_apply.py --dry-run         # Neo, check only, no applying
.venv/bin/python -m naukri_bot company-apply --file <list> --submit --no-wait --browser-profile ats-profile-cycle
.venv/bin/python -m pytest -q tests                   # 197 tests
```

Naukri allows about 50 applications a day; after that the bot pauses Naukri until the cooldown in
`state/cooldowns.json` ends.
