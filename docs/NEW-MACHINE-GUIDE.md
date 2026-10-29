# Running zangetsu on a new machine (state as of 29 Oct 2026)

This is the handover for moving the job bot to another machine. It covers what to copy, how to set it up, how the
daily protocol runs now, what changed between 21 and 29 October, and what still needs a human. For how the bot
worked up to 16 October, see `docs/BOT-STATUS-AND-FIXES.md`; the `README.md` has the full architecture.

The bot has been stopped on the old machine. Run it on one machine only: two copies share the same Naukri,
LinkedIn and Workday accounts and will trip rate limits (LinkedIn pauses the account after "unusual traffic").

---

## 1. Set up the new machine

```bash
git clone https://github.com/Ashu-Az/zangetsu.git
cd zangetsu
git checkout bot-fixes-oct-13-16          # current work; not merged into main yet
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/python -m pytest -q tests        # 213 tests should pass
```

### Private files git does not carry (copy them from the old machine)

| Path | What it holds |
|---|---|
| `.env` | Naukri login, LLM keys (Groq / Anthropic), Workday account, limits (see section 3) |
| `profile/profile.yaml` | Every personal fact the bot answers from: contact, CTC, notice, skills and years, filters, `background_summary`, `current_company` |
| `profile/confirmed_facts.md` | The facts list given to the LLM when no rule answers |
| `profile/resume.md`, `profile/master_resume.yaml`, `profile/resume_variants.yaml`, `profile/naukri_profile.yaml` | Resume sources for tailoring and the Naukri profile sections |
| `state/answer_cache.json` | Reused answers to screening questions |
| `state/seen_jobs.json` | Every job already handled, so nothing is applied to twice |
| `state/cooldowns.json` | Portal pauses (LinkedIn, Naukri 403) with their end times |
| `state/outreach_sent.json` | Recruiters already emailed, so nobody is emailed twice |
| `output/applications.csv` | The application log (the bot also reads it to skip repeat roles for 30 days) |
| `output/outreach/2026-10-27/` | 4 recruiter emails drafted but not sent yet (see section 6) |

Browser sessions in `state/*-profile/` do not move well between machines. Sign in again on the new machine:
Naukri (`python -m naukri_bot login`), LinkedIn, Wellfound, Hirist, and Gmail in the bot's Chrome (needed by
`tools/gmail_web_send.py`).

---

## 2. The daily protocol (order matters)

1. **Neo first** (Naukri Pro AI agent, `tools/neo_apply.py`): top priority; applies with Neo's custom resume.
2. **NVites** (recruiter invitations), then **Naukri Pro early access** (share interest).
3. **Naukri search** (capped per run, see section 3) and **Wellfound**, side by side.
4. **Hirist**: run it too (`python -m naukri_bot portal hirist --max 30`). It does not use Naukri's quota and
   brings 10–20 applications a day.
5. **Recruiter emails**: draft, proof-read, then **send** (from the candidate's Gmail, never Apple Mail).
6. **Top companies** (Naukri postings of priority companies), **Workday**, Greenhouse / Lever / Ashby boards.
7. **Company sites**: `scripts/hunt.sh company` fills each employer's own form with a tailored resume.
8. **LinkedIn**: Easy Apply (`portal linkedin`), then hiring posts (`leads`) and `tools/post_outreach.py` for
   tailored emails. Keep it light: about 5 post searches triggers LinkedIn's 48-hour pause.

Commands:

```bash
MAX_JOB_AGE_DAYS=7 scripts/cycle.sh morning      # steps 1-3, 5-6 (morning also refreshes the Naukri profile)
ATS_PROFILE=ats-profile-cycle scripts/hunt.sh company   # step 7
.venv/bin/python -m naukri_bot portal hirist --max 30   # step 4, in parallel with the cycle
# Neo again later in the day, when new early-apply jobs appear:
.venv/bin/python tools/neo_apply.py && .venv/bin/python -m naukri_bot nvites --max 10 && .venv/bin/python -m naukri_bot early-access --max 30
```

Suggested day: full cycle in the morning, Neo + NVites + early access again at about 13:00, 17:00 and 21:00,
LinkedIn once in the afternoon when its cooldown allows.

Keep the machine awake and online (plug in; System Settings > Battery > "Prevent automatic sleeping when the
display is off", or run `caffeinate -i`). Two morning runs on the old machine failed with
`ERR_INTERNET_DISCONNECTED` because the Mac slept.

---

## 3. Settings that changed (`.env` and `profile/profile.yaml`)

| Setting | Value now | Why |
|---|---|---|
| `DAILY_QUOTA` | 50 | Naukri's apply cap is a rolling 24 h; a 403 pauses Naukri until the next morning |
| `PRIORITY_RESERVE` | **20** (was 8) | Keeps Naukri applies for Neo / NVites, whose best early-apply jobs show up later in the day |
| `NAUKRI_RUN_MAX` (env, read by `cycle.sh`) | **15** per search run | Spreads search applies over the day instead of burning the quota in one morning run |
| `EXPERIENCE_SLACK` | **2** (was 1) | Jobs asking up to 4 years minimum are applied to; 5+ still skipped |
| `MAX_JOB_AGE_DAYS` | 3 (pass 7 for wider sweeps) | The 3-day pool empties fast; 7 days finds many more |
| `require_match` / `require_match_recommended` | `[]` | Naukri's "keyskills" tick is no longer required; `min_skill_overlap: 1` still checks relevance |
| `exclude_title_keywords` | no longer excludes `senior` / `sr.` | "Senior X" with a 2–5 year range is applied to; the experience check decides |
| `exclude_title_keywords` (added) | dot net, testing / QA / SDET, cost / financial / credit / risk analyst, Celonis, ServiceNow, Pega, SailPoint, AEO specialist | Roles that slipped in once matching became broad |
| `default_skill_years` and every `skill_years` entry | **3** | Every "years of experience in X" skill answer is 3; total / relevant experience stays 2 |
| `background_summary`, `current_company` | set in the profile | Answers for "your background in a few lines" and "current or previous employer" (kept out of code) |

---

## 4. What changed in the code (21–29 Oct 2026)

### Finding more jobs
- **Broad title matching** (`ranking.title_included`): any software / data / AI / developer title counts
  (Data Engineer, Node.js, Java, React, Computer Vision, Technical Analyst...); only clearly non-software roles are
  dropped. "AI / ML Developer" style titles (spaces around the slash) now match.
- Jobs skipped under the old exact-keyword filter were re-queued once (about 1,600 removed from `seen_jobs`).
- Test-looking company names (`test_`, `_sku`, `infotest`, `dummy`) are excluded.

### Answering questions correctly
- The LLM now gets "which / what technologies, cloud, platform" questions instead of a rule typing a bare
  number ("Which cloud platform?" now gets "Azure", not "3").
- "Have you worked with <company> Pvt Ltd before? Employee ID" now gets No; it was being read as a skill question.
- "Years of experience building / launching X" is a skill question (3), not total experience.
- Naukri chat: acknowledgement bubbles ("Got it!") are no longer answered as questions; inputs that the chat
  re-renders are looked up again instead of crashing (`'NoneType' object has no attribute 'click'`).
- New rules: securities licences and regulatory discipline (No), prior employment / employee ID at the hiring
  company (No), immediate family of an employee or partner (No), non-compete wording including "presently
  bound" (No), talent-pool preference (future roles in India), DSA rating "1 10" (8), desired remuneration
  "in INR" (full amount), current or previous employer, "Where are you located?" (Delhi/NCR, else India remote),
  background summary.
- Company forms (EEO): gender "Man, male or masculine"; when Male or No is not offered, "I don't wish to
  answer"; race South Asian / Indian else decline; veteran "not a veteran"; disability from the profile;
  text-message consent Yes; Town / Postcode map to city / postal code.

### Workday
- "How did you hear": Other > "Non <Company> Employee" (Citi), "Career Site" (Cigna).
- Country dropdown waits until its list loads; degree picks B.Tech / Engineering before "Bachelor of Science";
  "Prefix" gets Mr; privacy-policy acknowledgement boxes are ticked (not when they certify facts).
- The generic field pass skips experience-panel fields ("I currently work here" was reported as unanswered).

### Company career sites
- Job-alert sign-ups, talent-community boxes, newsletters and job-search bars are ignored (the bot had been
  filling Amgen's and Barclays' "Sign up for job alerts" instead of clicking Apply).
- When Apply leads to Workday, the Workday applier takes over from the job page.
- Cover-letter PDF attached where a form asks for one; the resume is re-attached before a tab is left for a human.

### Resumes and ATS
- `naukri_bot/ats_bimetric.py`: weighted ATS score (skills 45, title 20, experience 15, education 8, domain 8,
  certifications 4). Every tailored resume must reach **95** against the company's JD; missing JD skills are
  added under "Also worked with" and in the summary.
- Added terms are cleaned: no sentence fragments ("Years. be engineering", "Cd pipelines", "including
  microservices"); acronyms keep their case (RAG, LLM, SDKs). Anything added for ATS that the candidate has not
  used is a likely interview question; review before sending an important application.

### Recruiter emails
- `tools/gmail_web_send.py`: sends `.eml` drafts through Gmail in the bot's signed-in Chrome and logs them to
  `state/outreach_sent.json`.
- `tools/post_outreach.py`: LinkedIn hiring posts with an email address become tailored drafts.
- Drafts no longer use the poster's own name as the company, never claim skills that are not on the resume,
  skip college / student-placement addresses, skip posts asking for far more experience, and clean role titles
  (hashtags, camelCase, "2-10 years of Experience").
- Always proof-read drafts before sending: check the address, greeting, role and attachment.

---

## 5. Rules the candidate has set (keep these)

- Apply as widely as possible; never skip a job only because the title wording differs.
- Every skill "years of experience in X" answer is 3; never 0 or blank.
- Tailored resume for every application, ATS 95+ against that company's JD; always attach the resume (and a
  cover letter when asked).
- Order by fewest applicants first, then freshest.
- Send recruiter emails (do not leave drafts), from the candidate's Gmail only; no links in the body.
- Ignore unpaid roles and internships.
- In-person interviews only in Delhi NCR; elsewhere ask for a virtual round. Night / US shifts: No.
- Never enter passwords, OTPs, security codes or CAPTCHAs, and never create accounts on the candidate's behalf;
  leave those steps for the candidate.

---

## 6. Open items handed over

- **Recruiter emails ready to send** (`output/outreach/2026-10-27/`): HappLabs, Cyrix Health Care, DevTrust
  (Deepa), Daiviktech (Neha, as Software Engineer). Send with
  `.venv/bin/python tools/gmail_web_send.py output/outreach/2026-10-27` once Gmail is signed in.
- **Smartsheet** (Software Engineer – Full Stack, Greenhouse): retry after the EEO fixes.
- **Needs the candidate**: CAPTCHA forms (Delivery Centric, Reelo, Tangentia, Guru TechnoLabs, Open
  Destinations, DigiMantra), Workday sign-ins / verification emails (Equifax, Medtronic, Capital One, BMS, KLA),
  Citi questionnaires (Workday tasks), Cisco and Ema (bot check), One World Technologies and Roy Informatics
  (forms inside pop-ups).
- **Known gaps**: multi-step "Continue / Continue / Submit" career forms and forms inside pop-ups are not filled
  yet; Cisco's Workday education section does not fill the school.
