"""Fill a Workday application up to the Review step (never submits).

Flow (verified on mastercard.wd1 / expedia.wd108, Oct 2026): job page -> "Apply" (adventureButton) ->
"Autofill with Resume" | "Apply Manually" | "Use My Last Application" -> steps shown in the progress bar:
Create Account/Sign In, Autofill with Resume, My Information, My Experience, Application Questions (1..n),
Voluntary Disclosures, Self Identify, Review. Fields carry stable data-automation-id attributes; questions
are answered by label through the same rules as Naukri; anything unknown is handed to the person.

    python -m naukri_bot workday <job-url>
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field

from ..rules import rule_answer, with_unit
from .generic_form import LEGAL_CLAIM, WORK_AUTH_Q, consent_only            # noqa: F401  (LEGAL_CLAIM: shared rule)

log = logging.getLogger(__name__)

A = lambda aid: f"[data-automation-id='{aid}']"            # noqa: E731
# older tenants: bottom-navigation-next-button; newer (mastercard.wd1, Oct 2026): pageFooterNextButton
NEXT = A("pageFooterNextButton") + ", " + A("bottom-navigation-next-button")
ACTIVE_STEP = A("progressBarActiveStep")
OPTION = "[role='option'], [data-automation-id='promptOption'], [data-automation-id='menuItem']"


@dataclass
class WorkdayResult:
    status: str                      # ready_for_review | needs_verification | closed | needs_you | failed | applied | submitted_unconfirmed
    notes: str = ""
    filled: list[dict] = field(default_factory=list)


class WorkdayApplier:
    def __init__(self, page, profile: dict, email: str, password: str, resume_path: str, assist: bool = True,
                 submit: bool = False, jd: str = "", job_location: str = ""):
        self.pg, self.profile, self.email, self.password = page, profile, email, password
        self.jd = jd or ""                         # the job description, when the caller has it (minimum years etc.)
        self.job_location = job_location or ""     # e.g. "IND.Pune": India jobs make work-authorization answers confirmed
        self.submit = submit                       # press Submit at Review (only when every answer is from confirmed facts)
        self.resume_path, self.assist = os.path.abspath(resume_path), assist
        self.result = WorkdayResult("failed")

    # ------------------------------------------------------------------ helpers
    def _visible(self, sel: str, timeout: int = 0) -> bool:
        loc = self.pg.locator(f"{sel} >> visible=true")
        if not timeout:
            return loc.count() > 0
        try:
            loc.first.wait_for(timeout=timeout)
            return True
        except Exception:
            return False

    def _click(self, sel: str) -> None:
        """Workday lays a transparent 'click_filter' div over many buttons; clicking the button itself can miss."""
        el = self.pg.locator(f"{sel} >> visible=true").first
        el.scroll_into_view_if_needed()
        try:
            el.click(timeout=5000)
        except Exception:
            el.evaluate("e => e.click()")

    def snapshot(self, name: str) -> None:
        from ..config import OUTPUT_DIR
        d = OUTPUT_DIR / "debug"; d.mkdir(parents=True, exist_ok=True)
        path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-workday-{name}"
        try:
            self.pg.screenshot(path=f"{path}.png", full_page=True)
            path.with_suffix(".html").write_text(self.pg.content(), encoding="utf-8")
            log.info("  debug snapshot: %s.png", path)
        except Exception:
            pass

    def step(self) -> str:
        if not self._visible(ACTIVE_STEP):
            return ""
        return self.pg.locator(ACTIVE_STEP).first.inner_text().strip().split("\n")[-1].strip()

    # answers the bot may submit on its own: straight from the profile, the resume data or the confirmed rules.
    # Anything else (fallbacks such as a school list's "Other", right-to-work answers whose job country the bot
    # cannot see) keeps the application at Review for the person.
    TRUSTED = frozenset({"profile", "rules", "profile/rules", "resume", "consent", "profile (replaced autofill)"})

    def _note(self, label: str, value: str, source: str) -> None:
        log.info("    %-45s -> %s  [%s]", label[:45], str(value)[:60], source)
        # a field that failed once (list still loading) and was then filled is no longer unanswered
        self.result.filled = [f for f in self.result.filled if not (f["source"] == "unknown" and f["label"] == label)]
        self.result.filled.append({"label": label, "value": value, "source": source})

    def _rule_source(self, label: str, default: str = "profile/rules") -> str:
        """Right-to-work / sponsorship answers depend on the job's country, which Workday's form doesn't show. When the
        caller passed the job's location and it is in India (the candidate is an Indian citizen), the answer is a
        confirmed fact; otherwise it stays for the person to check."""
        if not WORK_AUTH_Q.search(label):
            return default
        if re.search(r"\bindia\b|\bIND\b|bengaluru|bangalore|hyderabad|pune|chennai|mumbai|gurugram|gurgaon|noida|delhi|kolkata|ahmedabad",
                     self.job_location or "", re.I) and not re.search(r"remote\s*-?\s*(us|usa|uk|eu)|united states|singapore|dubai", self.job_location or "", re.I):
            return default
        return "rules (work authorization: check job country)"

    def ask_human(self, what: str, timeout_s: int = 600) -> bool:
        """Alert the person and wait until the step changes (they fixed/answered it and pressed Continue)."""
        if not self.assist:
            return False
        log.warning("  >>> YOUR TURN in the Chromium window: %s", what[:150])
        try:
            subprocess.run(["osascript", "-e", f'display notification "{what[:110]}" with title "Workday needs you" sound name "Glass"'],
                           timeout=5, capture_output=True)
            subprocess.Popen(["say", "Workday needs your input"])
        except Exception:
            pass
        before = self.step()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(2)
            if self.step() != before:
                return True
        return False

    # ------------------------------------------------------------------ entry
    def run(self, url: str) -> WorkdayResult:
        pg = self.pg
        pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        cont = pg.locator("a:has-text('Continue Application'), button:has-text('Continue Application')")
        if not self._visible(A("adventureButton") + ", a:has-text('Continue Application'), button:has-text('Continue Application')",
                             timeout=20000):
            closed = self._visible(A("errorMessage")) or "doesn't exist" in pg.inner_text("body")
            self.result.status, self.result.notes = ("closed", "posting no longer exists") if closed else ("failed", "no Apply button")
            return self.result
        if re.search(r"you('ve| have) (already )?applied|application submitted", pg.inner_text("body"), re.I):
            self.result.status, self.result.notes = "applied", "already applied (Workday says so)"
            return self.result
        if cont.count() and cont.first.is_visible() and not pg.locator(A("adventureButton")).first.is_visible():
            # an earlier run saved a partial application on this tenant: pick it up where it stopped
            log.info("  continuing the saved application")
            cont.first.click(); time.sleep(4)
            if not self._auth() and not self._past_auth():
                return self.result
            return self._continue_steps()
        self._click(A("adventureButton"))
        time.sleep(3)
        # "Use My Last Application" is offered even to new accounts and then stalls at sign-in: prefer a fresh autofill
        for choice in ("Autofill with Resume", "Apply Manually", "Use My Last Application"):
            loc = pg.get_by_text(choice, exact=True)
            if loc.count() and loc.first.is_visible():
                log.info("  start: %s", choice)
                loc.first.click()
                break
        if not self._auth():
            return self.result
        return self._continue_steps()

    def _continue_steps(self) -> WorkdayResult:
        """Walk the application steps after sign-in (fresh, or a saved application being continued)."""
        pg = self.pg
        blank = recoveries = 0
        for _ in range(16):
            if re.search(r"something went wrong|please refresh the page", self.pg.inner_text("body"), re.I):
                if recoveries >= 2:
                    self.snapshot("workday-error")
                    self.result.status, self.result.notes = "needs_you", "Workday kept showing 'Something went wrong'"
                    return self.result
                recoveries += 1
                log.warning("  Workday error page: refreshing (%d/2)", recoveries)
                self.pg.reload(wait_until="domcontentloaded"); time.sleep(8)
                continue
            name = self.step()
            log.info("  step: %s", name or "(none)")
            if not name:
                blank += 1
                if blank >= 4:                      # no progress bar: an error page, a popup, or signed out
                    self.snapshot("no-step")
                    msg = " | ".join(l.strip() for l in self.pg.inner_text("body").split("\n") if l.strip())[:300]
                    self.result.status, self.result.notes = "needs_you", f"unexpected page: {msg}"
                    return self.result
                time.sleep(2)
                continue
            blank = 0
            low = name.lower()
            if low.startswith("review"):
                if self.submit:
                    return self._submit()
                self.result.status, self.result.notes = "ready_for_review", "filled; waiting for you to review and submit"
                return self.result
            if "autofill" in low or "quick apply" in low:
                self._upload_resume()
            elif "my information" in low:
                self._fill_form()
            elif "my experience" in low:
                self._upload_resume()                # always this job's resume, also over "Use My Last Application"
                self._fill_experience()
                self._fill_form()
            else:                                   # application questions, disclosures, self identify
                self._fill_form()
            if not self._next(name):
                return self.result
        self.snapshot("too-many-steps")
        self.result.status, self.result.notes = "needs_you", "did not reach Review within 16 steps"
        return self.result

    # ------------------------------------------------------------------ steps
    def _past_auth(self) -> bool:
        st = self.step()
        return bool(st) and "sign in" not in st.lower()

    def _wait_past_auth(self, seconds: float = 20) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._past_auth():
                return True
            time.sleep(1.5)
        return False

    def _email_route(self) -> None:
        """Some tenants put an Apple / Google / 'Sign in with email' gate in front of the email form - at the start
        (KLA) or again after Create Account (Thomson Reuters). Take the email route whenever it shows."""
        pg = self.pg
        for sel in (A("SignInWithEmailButton"), "button:has-text('Sign in with email')", "button:has-text('Sign In with Email')",
                    "button:has-text('Continue with email')"):
            b = pg.locator(sel)
            try:
                if b.count() and b.first.is_visible() and not pg.locator("input[type='password']").first.is_visible():
                    b.first.click(); time.sleep(2); return
            except Exception:
                continue

    def _auth(self) -> bool:
        pg = self.pg
        # tenant cookie banners (Thomson Reuters) can sit over the Sign In button: dismiss them first
        for sel in ("button:has-text('Accept Cookies')", "button:has-text('Accept All')", "button:has-text('Accept all cookies')",
                    "#onetrust-accept-btn-handler", "button:has-text('Decline')"):
            try:
                b = pg.locator(sel).first
                if b.count() and b.is_visible():
                    b.click(); time.sleep(1); break
            except Exception:
                pass
        form = "input[type='password'], " + A("createAccountLink") + ", " + A("signInLink")
        # some tenants (KLA) first offer Apple/Google/LinkedIn/email buttons: take the email route
        email_btn = A("SignInWithEmailButton")
        if self._visible(form + ", " + email_btn, timeout=30000) and not pg.locator(form).first.is_visible():
            btn = pg.locator(email_btn)
            if btn.count() and btn.first.is_visible():
                btn.first.click(); time.sleep(2)
        if not self._visible(form, timeout=15000):
            if self._past_auth():
                return True                          # still signed in from an earlier application
            self.result.notes = "sign-in form did not appear"
            return False
        if self._visible(A("createAccountSubmitButton")):
            self._create_account()
            if self._wait_past_auth(18):
                log.info("  account created; now on: %s", self.step())
                return True
        # account exists on this company's site (Create Account lands on a plain Sign In page), or sign-in was shown first
        self._email_route()
        if self._visible(A("signInSubmitButton")) or self._visible(A("signInLink")):
            if not self._visible(A("signInSubmitButton")):
                self._click(A("signInLink")); time.sleep(2)
                self._email_route()
            log.info("  signing in as %s", self.email)
            self._sign_in()
            if self._wait_past_auth(20):
                log.info("  signed in; now on: %s", self.step())
                return True
            # no account here yet: most tenants say "wrong email or password", some (Thomson Reuters) say nothing
            # and just stay on Sign In - in both cases try creating the account (an existing one is refused harmlessly)
            if self._visible(A("createAccountLink")) and (re.search(r"wrong email|might be locked|invalid", pg.inner_text("body"), re.I)
                                                          or self._visible(A("signInSubmitButton"), timeout=1000)):
                log.info("  no account on this site yet: creating one")
                self._click(A("createAccountLink")); time.sleep(3)
                self._email_route()
                if not self._visible(A("createAccountSubmitButton"), timeout=5000) and self._visible(A("createAccountLink"), timeout=2000):
                    self._click(A("createAccountLink")); time.sleep(3)    # the gate led to Sign In: ask for Create Account again
                if self._visible(A("createAccountSubmitButton"), timeout=10000):
                    self._create_account()
                    if self._wait_past_auth(20):
                        log.info("  account created; now on: %s", self.step())
                        return True
                    created_at = time.time()
                    # some tenants (Thomson Reuters) send a new account back to their sign-in gate: sign in now
                    self._email_route()
                    if self._visible(A("signInSubmitButton"), timeout=8000):
                        log.info("  account created; signing in to it")
                        self._sign_in()
                        if self._wait_past_auth(25):
                            log.info("  signed in; now on: %s", self.step())
                            return True
                    # still on Sign In: the new account must be verified from the email first (TR, Rockwell,
                    # Workday, 13 Oct 2026); open the link from Gmail when an app password is set, then sign in
                    if self._verify_from_mail(created_at):
                        return True
        if getattr(self, "_created_at", 0) and self._verify_from_mail(self._created_at):
            return True
        body = pg.inner_text("body").lower()
        if re.search(r"verify your (email|account)|verification (email|link)|check your email", body):
            self.result.status, self.result.notes = "needs_verification", "Workday sent a verification email; click it, then rerun"
            if self.ask_human("Click the verification link Workday emailed you, then sign in here"):
                return self._past_auth()
            return False
        # this site's password differs from the saved one: the person signs in here, then filling carries on
        if self.ask_human("Sign in to this Workday site yourself; filling continues once you're past Sign In", timeout_s=900) \
                and self._wait_past_auth(30):
            log.info("  signed in by you; now on: %s", self.step())
            return True
        self.snapshot("auth")
        if re.search(r"wrong email|invalid (email|password)|incorrect|might be locked", body):
            self.result.status, self.result.notes = "failed", "sign-in rejected (wrong password for this site, or account locked)"
        else:
            self.result.status, self.result.notes = "needs_you", "could not get past Create Account/Sign In"
        return False

    def _verify_from_mail(self, since: float) -> bool:
        from .. import mailbox
        if not mailbox.available() or getattr(self, "_mail_tried", False):
            return False
        self._mail_tried = True
        host = re.sub(r"^https?://", "", self.pg.url).split("/")[0]
        log.info("  waiting for the verification email from %s", host)
        link = mailbox.find_link(host, since)
        if not link:
            log.info("  no verification email arrived")
            return False
        back = self.pg.url
        self.pg.goto(link, wait_until="domcontentloaded"); time.sleep(4)
        log.info("  opened the verification link")
        if self._past_auth():
            return True
        if not self._visible(A("signInSubmitButton"), timeout=5000):
            self.pg.goto(back, wait_until="domcontentloaded"); time.sleep(3)
            self._email_route()
            if self._visible(A("signInLink"), timeout=5000) and not self._visible(A("signInSubmitButton")):
                self._click(A("signInLink")); time.sleep(2)
        if self._visible(A("signInSubmitButton"), timeout=8000):
            self._sign_in()
            if self._wait_past_auth(25):
                log.info("  verified and signed in; now on: %s", self.step())
                return True
        return False

    def _create_account(self) -> None:
        pg = self.pg
        log.info("  creating a Workday account for %s", self.email)
        self._created_at = time.time()
        pg.locator(A("email")).first.fill(self.email)
        pg.locator(A("password")).first.fill(self.password)
        if pg.locator(A("verifyPassword")).count():
            pg.locator(A("verifyPassword")).first.fill(self.password)
        box = pg.locator(A("createAccountCheckbox"))
        if box.count() and not box.first.is_checked():
            box.first.check(force=True)
        # never touch the honeypot (data-automation-id='beecatcher')
        sub = pg.locator(f"{A('createAccountSubmitButton')} >> visible=true").first
        filt = pg.locator(f"{A('click_filter')}[aria-label='Create Account'] >> visible=true")
        (filt.first if filt.count() else sub).click()

    def _sign_in(self) -> None:
        pg = self.pg
        self._visible(A("email"), timeout=15000)
        # Type like a person: some tenants (Thomson Reuters, 10 Oct 2026) ignore values set with fill(), so the
        # Sign In click silently does nothing with no error shown.
        for aid, value in (("email", self.email), ("password", self.password)):
            box = pg.locator(A(aid)).first
            box.click(); box.fill("")
            pg.keyboard.type(value, delay=35)
        pg.keyboard.press("Tab"); time.sleep(0.5)
        filt = pg.locator(f"{A('click_filter')}[aria-label='Sign In'] >> visible=true")
        if filt.count():
            filt.first.click()
        else:
            self._click(A("signInSubmitButton"))
        time.sleep(4)
        if self._visible(A("signInSubmitButton"), timeout=1000) and not re.search(
                r"wrong email|invalid|incorrect|might be locked", pg.inner_text("body"), re.I):
            pg.locator(A("password")).first.click()       # click did not register: submit from the password field
            pg.keyboard.press("Enter")

    def _upload_resume(self) -> None:
        """Attach this job's tailored resume, removing any file already attached (a previous application's
        resume carried over by "Use My Last Application" must not be sent to this employer)."""
        pg = self.pg
        try:
            pg.wait_for_selector("input[type='file']", state="attached", timeout=15000)
        except Exception:
            log.info("  (no resume upload field on this step)")
            return
        inp = pg.locator("input[type='file']")
        mine = os.path.basename(self.resume_path)
        try:
            attached = [t.strip() for t in pg.locator(A("file-upload-item")).all_inner_texts() if t.strip()]
        except Exception:
            attached = []
        if attached and all(mine in t for t in attached):
            log.info("  resume already attached: %s", mine)
            return
        old = pg.locator(f"{A('delete-file')}, {A('file-upload-item')} button[aria-label^='Delete' i], "
                         f"{A('file-upload-item')} button:has-text('Delete')")
        for _ in range(6):                            # remove earlier attachments one by one (the list re-renders)
            btns = [b for b in old.all() if b.is_visible()]
            if not btns:
                break
            try:
                btns[0].click(); time.sleep(1.2)
            except Exception as e:
                log.warning("  could not remove an attached file: %s", str(e)[:80])
                break
        if any(b.is_visible() for b in old.all()):
            self._unknown("Resume (an earlier file is still attached: remove it)", [])
        inp.first.set_input_files(self.resume_path)
        log.info("  resume uploaded: %s", os.path.basename(self.resume_path))
        self._note("Resume", os.path.basename(self.resume_path), "resume")
        time.sleep(8)                                 # Workday parses the resume to prefill My Experience

    # ------------------------------------------------------------------ generic form filling
    def _profile_value(self, label: str) -> tuple[str | None, str]:
        """Contact / identity fields straight from the profile, matched on the field label."""
        p, l = self.profile, label.lower()
        if re.search(r"extension|\bext\b", l):
            return None, ""                          # never put the phone number into "Phone Extension"
        table = [
            (r"\b(given|first) name", p.get("first_name")), (r"\b(family|last) name|surname", p.get("last_name")),
            (r"address line 1|street", p.get("address_line1")), (r"address line 2", p.get("address_line2")), (r"\bcity\b|town", p.get("city")),
            (r"postal|pin ?code|zip", p.get("postal_code")), (r"^(phone|mobile)( number)?$|phone number|mobile number", p.get("phone_national")),
            (r"country phone code|phone code|country code", p.get("phone_country_code")),
            (r"^email", p.get("email")), (r"linkedin", (p.get("links") or {}).get("linkedin")),
            # Workday address split into door / building (16 Oct 2026); "Company" on My Information = current employer
            (r"^door( number| no\.?)?$|^house (number|no\.?)$|^flat( number| no\.?)?$", str(p.get("address_line1") or "").split(",")[0].strip() or None),
            (r"^building( name)?$", str(p.get("address_line1") or "").split(",")[0].strip() or None),
            (r"^(current )?company( name)?$|^current employer$", p.get("current_company")),
        ]
        for pat, val in table:
            if val and re.search(pat, l):
                return str(val), "profile"
        return None, ""

    def _choice_for(self, label: str, options: list[str]) -> int | None:
        p, l = self.profile, label.lower()
        def pick(*pats):
            for pat in pats:
                for i, o in enumerate(options):
                    if re.search(pat, o, re.I):
                        return i
            return None
        if re.search(r"^(how did you hear|source\b)", l):
            i = pick(*[re.escape(h) for h in p.get("how_heard") or []])
            # employee-status style source lists (Citi: AMS / Citi Employee on Leave / Non Citi Employee / Redeployment)
            return i if i is not None else pick(r"^non[- ].{0,30}employee", r"^external( candidate| applicant)?$", r"^(job ?board|job portal|online)",
                                                  r"\b(job ?boards?|job (portal|posting|site|search (site|engine))s?|online (job|posting|advert)|internet)\b")
        # data-retention choice: keep the profile so the employer can suggest future roles (Ashu: yes to future roles)
        keep = pick(r"(kept|keep|retain).{0,60}(future|suitable|other) (roles|opportunit|positions|vacanc)|notify me of any suitable roles")
        if keep is not None and re.search(r"select one of the following|personal (information|data)|retention|how long", l + " " + " ".join(options), re.I):
            return keep
        if p.get("no_conflicts_of_interest") and re.search(r"(engaged|involved) (or involved )?in any other business|any other business or employment|outside (business|employment) (activit|interest)|outside employment or activit"
                                                       r"|(continue|keep).{0,40}if (you are )?hired", l):
            return pick(r"^no\b")
        if re.search(r"answer .?yes.? if you have (ever )?been employed by|have (ever )?been employed by .{0,60}(subsidiar|group)", l):
            return pick(r"^no\b")
        if re.search(r"industry (do )?you (specialize|specialise|work) in|industry (of )?(specialization|expertise)|which industry", l):
            # Ashu works in IT / software services
            return pick(r"information technology|^it\b|\bsoftware\b|\btechnology\b|\btech\b|\binternet\b|\bsaas\b", r"^others?\b")
        if re.search(r"^citizenship|^nationality|country of citizenship", l) and len(l) < 40:
            return pick(r"^citizen\b.{0,5}\(?india", r"^india$", r"^indian$", r"^india\b", r"^(indian )?citizen$", r"\bindian\b(?!.*non)")   # Indian citizen
        if re.search(r"^(country|country/territory)\b", l) and len(l) < 40:
            return pick(rf"^{re.escape(p.get('country', 'India'))}$")
        if re.search(r"^(state|province|region)\b", l) and len(l) < 40:
            st = re.escape(p.get("state", ""))
            # Delhi is listed as "Delhi", "NCT of Delhi", "National Capital Territory of Delhi", "Delhi (DL)" ...
            return pick(rf"^{st}$", rf"^{st}\b", rf"(nct|national capital territory) of {st}", rf"\b{st}\b") if p.get("state") else None
        if re.search(r"^(phone device|device type)", l):
            return pick(r"^mobile", r"cell", r"^main$")
        # short labels ("Gender") or a long preamble ending in the ask ("... Please indicate your gender below.": Salesforce)
        if re.search(r"\b(gender|sex)\b", l) and (len(l) < 60 or re.search(r"(indicate|select|state|provide|what is) your (gender|sex)\b", l)) \
                and not re.search(r"pronoun|transgender|orientation", l):
            return pick(rf"^{re.escape(p.get('gender', ''))}$") if p.get("gender") else None
        if re.search(r"veteran", l):
            return pick(r"not a (protected )?veteran", r"^no\b", r"i am not") if p.get("veteran") is False else None
        if re.search(r"disabilit", l):
            return pick(r"^no,? i (do not|don.t) have", r"^no\b") if p.get("disability") is False else None
        if re.search(r"^language\b", l) and len(l) < 30:
            return pick(r"^english")
        if re.search(r"^(reading|speaking|writing|comprehension|overall|read|speak|write|spoken|written)\b|proficiency", l) and len(l) < 40:
            return pick(r"fluent", r"native", r"advanced", r"expert", r"proficient", r"\bhigh\b", r"excellent", r"very good")
        if re.search(r"previously (been )?(worked|employed)|former employee|worked (for|at) .* before|current or former|"
                     r"have you ever (worked|been employed)|provided services as a (contingent|contractor)|"
                     r"(been|are you) (currently,? )?(or have you been )?an? (contractor|consultant|contingent worker) (with|for|at)|"
                     r"(currently|ever),? or have you (ever )?been an? (contractor|employee)|"
                     r"have you worked at .{0,40} as an? (employee|consultant|contractor)|did you (work|previously work) (previously )?(at|for)|"
                     r"within the past \d+ years,? have you been employed by|are you an? (existing|current) .{0,20}employee|"
                     r"have you ever been an employee (for|of|at)", l):
            return pick(r"^no\b", r"(have not|haven.t|never) (worked|been employed)", r"^none\b", r"not (a )?(current|former)")
        if re.search(r"(united states|u\.s\.|us) government or public institution|government (or public sector )?employ", l):
            return pick(r"^no\b")                          # only employers: Quess, HeadsUp, ROVA, Verzeo (all private, India)
        if p.get("no_conflicts_of_interest") and re.search(r"conflicts?(\(s\))? of interest", l):
            return pick(r"^no\b")
        if (re.search(r"export control", l) and re.search(r"citizen or permanent resident of one or more of the countries", l)) or \
                re.search(r"for any of the countries listed below.{0,80}(citizen|passport|dual citizenship)", l):
            return pick(r"^no\b")                          # Indian citizen only, no passport; India is not on the US-embargoed list
        if re.search(r"willing to travel", l) and p.get("willing_to_travel"):
            want = re.escape(str(p["willing_to_travel"]))            # e.g. "25% to 50%" (Ashu, 12 Oct 2026)
            return pick(want, r"25%? to 50%", r"up to 25%")
        if re.search(r"ever accepted an offer (to work )?(at|with|from)", l) and \
                not re.search(r"quess|headsup|rova|botter|verzeo", l):
            return pick(r"^no\b")                          # only offers ever accepted: his four employers
        if re.search(r"(basic|essential|minimum) (/essential |or essential )?qualifications", l):
            # only numbers tied to experience: "3-7 years of experience", "experience: 2+ years", "minimum 2 years' experience"
            exp = r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*\d{0,2}\s*\+?\s*(?:years|yrs)['’]?\s*(?:of\s+)?(?:\w+\s+){0,3}?experience"
            mins = [int(x) for x in re.findall(exp, self.jd, re.I)] + \
                   [int(x) for x in re.findall(r"experience[^.\n]{0,30}?(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*\d{0,2}\s*(?:years|yrs)", self.jd, re.I)]
            yrs = float(p.get("total_experience_years") or 2)
            # Ashu (12 Oct 2026): Yes only when the role's minimum experience is within his ~2 years
            if mins and min(mins) <= yrs:
                return pick(r"^yes\b")
            return None
        if re.search(r"valid residency permit in the country", l):
            return pick(r"not applicable", r"^yes\b")      # Indian citizen applying to India roles: no permit needed
        if re.search(r"(future (positions|openings|opportunities|roles)).{0,60}(select|option|communicat)|regarding future positions", l):
            return pick(r"^yes\b.*(receive|contact|communicat)|^yes\b")
        if re.search(r"preference to receive notification", l):
            return pick(r"whatsapp and email", r"^email only")   # Ashu opted in to SMS/WhatsApp contact
        if re.search(r"name exactly as (shown )?on my aadhaar|aadhaar card", l + " " + " ".join(options), re.I) and p.get("name_matches_id_documents"):
            return pick(r"provided my name exactly as shown on my aadhaar")
        if re.search(r"legal age to work|at least 18|18 years (of age|or older)", l):
            return pick(r"^yes\b")
        # education level lists with ISCED labels (13 Oct 2026): B.Tech = bachelor's, ISCED 6
        if re.fullmatch(r"(highest )?(degree|level of education|education level|highest (level of )?education)( attained| completed)?\??", l):
            return pick(r"^bachelor", r"isced 6\b", r"\bbachelor", r"b\.?\s?tech|b\.?\s?eng\b|b\.?e\b|undergraduate degree")   # "B.Eng" (Citi, 16 Oct 2026)
        # "previous worker or contractor of Equifax or its subsidiaries" (13 Oct 2026)
        if re.search(r"previous (worker|employee|contractor)|former (worker|contractor)", l):
            return pick(r"^no\b")
        # work arrangement: Ashu is open to all of them; hybrid first
        if re.search(r"(types? of )?(schedules?|work arrangements?|work (mode|model|setup)s?).{0,30}(willing|open|prefer|comfortable)", l):
            return pick(*[rf"\b{re.escape(m)}\b" for m in (p.get("work_mode") or ["Hybrid"])])
        # timed assessments: "do you require an accommodation / adjustment?" -> No (no disability)
        if p.get("disability") is False and \
                re.search(r"(timed |online )?assessments?.{0,300}(accommodat|adjustment|accessib)|(require|need).{0,40}(accommodat|adjustment)", l):
            return pick(r"^no\b")
        # AI-screening (HiredScore) and keep-my-application consents: Ashu consents (wants future roles too)
        if re.search(r"consent", l) and re.search(r"hiredscore|ai[- ]assisted|screening tool|evaluate your application|future (job )?opportunit", l):
            return pick(r"^i consent", r"^yes\b", r"\bconsent\b(?!.*not)")
        ans = rule_answer(label, options, "dropdown", p)
        if ans and ans.selected_option_indices:
            return ans.selected_option_indices[0]
        return None

    def _popup_options(self) -> list:
        """Options of the open dropdown/prompt only. Workday renders the popup outside the form fields; the same
        option also appears as menuItem and promptOption, and other fields' selected pills (e.g. "India (+91)") are
        visible too, so filter to visible options outside any form field and keep one per text."""
        out, seen = [], set()
        for o in self.pg.locator("[data-automation-id='promptOption'], [role='option']").all():
            try:
                if not o.is_visible() or o.evaluate("e => !!e.closest(\"[data-automation-id^='formField-']\")"):
                    continue
                t = o.inner_text().strip()
            except Exception:
                continue
            if t and t not in seen:
                seen.add(t); out.append(o)
        return out

    def _scroll_find(self, patterns: list[str], exclude: str = "", max_steps: int = 60):
        """Scroll a long, virtualized popup list (S&P's source list runs '10,000 Able Interns'..., and typing does not
        filter it, 14 Oct 2026) and return (option, text) for the first pattern found, in pattern order."""
        seen: dict[str, object] = {}
        stale = 0
        for _ in range(max_steps):
            opts = self._popup_options()
            new = 0
            for o in opts:
                try:
                    t = o.inner_text().strip()
                except Exception:
                    continue
                if t not in seen:
                    seen[t] = o; new += 1
            for pat in patterns:
                hit = next((t for t in seen if re.search(pat, t, re.I) and not (exclude and re.search(exclude, t, re.I))), None)
                if hit:
                    try:
                        seen[hit].scroll_into_view_if_needed(timeout=3000)
                        return seen[hit], hit
                    except Exception:
                        break                        # scrolled away: keep going, it will be seen again
            stale = stale + 1 if not new else 0
            if stale >= 3 or not opts:
                break
            try:
                opts[-1].scroll_into_view_if_needed(timeout=3000)
                self.pg.mouse.wheel(0, 400)
            except Exception:
                break
            time.sleep(0.4)
        return None, None

    def _open_options(self, ctl) -> list:
        ctl.click()
        time.sleep(1.2)
        return self._popup_options()

    def _fill_form(self) -> None:
        pg = self.pg
        self._visible("[data-automation-id^='formField-'], " + NEXT, timeout=15000)
        time.sleep(1.5)
        fields = pg.locator("[data-automation-id^='formField-'] >> visible=true").all()
        for f in fields:
            label = ""
            try:
                label = " ".join((f.locator("label, legend").first.inner_text() if f.locator("label, legend").count() else "").split())
                label = label.rstrip("*").strip()
                if not label:
                    continue
                # the Skills section is filled by _fill_skills; its leftover search box ("Type to Add Skills") is not
                # an unanswered question once skills are selected
                skills_done = any(x.get("label") == "Skills" and x.get("value") and x.get("value") != "(none matched)"
                                  for x in self.result.filled)
                if (f.get_attribute("data-automation-id") == "formField-skills" or re.search(r"type to add skills", label, re.I)) \
                        and (skills_done or pg.locator(f"{A('formField-skills')} {A('selectedItem')}").count()):
                    continue
                # "Enter today's date below to confirm that you have read ..." (S&P Global HiredScore notice, 14 Oct
                # 2026): an acknowledgement date, filled with today's date in the MM/DD/YYYY widget
                if re.search(r"(enter|provide|type) today.?s date|today.?s date (below|to confirm)|date of (signature|acknowledg)", label, re.I) \
                        and f.locator(A("dateSectionMonth-input")).count():
                    if f.locator(A("dateSectionYear-input")).first.input_value().strip():
                        continue
                    today = time.localtime()
                    for aid, val in (("dateSectionMonth-input", f"{today.tm_mon:02d}"), ("dateSectionDay-input", f"{today.tm_mday:02d}"),
                                     ("dateSectionYear-input", str(today.tm_year))):
                        box = f.locator(A(aid))
                        if box.count():
                            box.first.evaluate("e => e.focus()"); pg.keyboard.type(val, delay=80)
                    pg.keyboard.press("Tab"); time.sleep(0.4)
                    self._note("Acknowledgement date", time.strftime("%m/%d/%Y"), "today")
                    continue
                # date of birth (Workday MM/DD/YYYY widget): only from profile.date_of_birth, never guessed
                if re.search(r"date of birth|\bdob\b|birth ?date", label, re.I) and f.locator(A("dateSectionMonth-input")).count():
                    if f.locator(A("dateSectionYear-input")).first.input_value().strip():
                        continue
                    dob = str(self.profile.get("date_of_birth") or "")
                    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", dob)
                    if not m:
                        self._unknown(label, []); continue
                    for aid, val in (("dateSectionMonth-input", m.group(2)), ("dateSectionDay-input", m.group(3)),
                                     ("dateSectionYear-input", m.group(1))):
                        box = f.locator(A(aid))
                        if box.count():
                            box.first.evaluate("e => e.focus()"); pg.keyboard.type(val, delay=80)
                    pg.keyboard.press("Tab"); time.sleep(0.4)
                    self._note(label, "(from profile)", "profile")
                    continue
                # text inputs
                txt = f.locator("input[type='text'], input:not([type]), input[type='tel'], input[type='email'], textarea")
                btn = f.locator("button[aria-haspopup='listbox']")
                radios = f.locator("input[type='radio']")
                checks = f.locator("input[type='checkbox']")
                prompt = f.locator(A("multiselectInputContainer") + ", " + A("searchBox"))
                if btn.count():
                    cur = btn.first.inner_text().strip()
                    if cur and not re.match(r"select one|^$", cur, re.I):
                        # a value left by an earlier attempt or by autofill is re-checked against the rules: a saved
                        # draft once submitted "No" to Salesforce's truthfulness acknowledgement (15 Oct 2026)
                        opts = self._open_options(btn.first)
                        names = [o.inner_text().strip() for o in opts]
                        idx = self._choice_for(label, names) if names else None
                        if idx is not None and names[idx] != cur:
                            opts[idx].click(); time.sleep(0.6)
                            self._note(label, f"{names[idx]} (was '{cur}')", self._rule_source(label))
                        else:
                            pg.keyboard.press("Escape"); time.sleep(0.3)
                        continue
                    opts = self._open_options(btn.first)
                    names = [o.inner_text().strip() for o in opts]
                    if not names:                            # State loads its list only after Country settles: reopen once
                        pg.keyboard.press("Escape"); time.sleep(2.5)
                        opts = self._open_options(btn.first)
                        names = [o.inner_text().strip() for o in opts]
                    idx = self._choice_for(label, names)
                    if idx is None:
                        pg.keyboard.press("Escape")
                        self._unknown(label, names)
                        continue
                    opts[idx].click(); time.sleep(0.6)
                    self._note(label, names[idx], self._rule_source(label))
                elif prompt.count():
                    if f.locator(A("selectedItem")).count() and "0 items selected" not in f.inner_text():
                        sel = " ".join(x.inner_text() for x in f.locator(A("selectedItem")).all())
                        # a source kept from an earlier application or the resume parse ("AllJobs", 12 Oct 2026):
                        # keep it only if it is one Ashu would give (Other, then Naukri), else clear it and pick again
                        if not re.search(r"how did you hear|^source", label, re.I) or \
                                re.search(r"naukri|\bother\b|company|career|linkedin|website", sel, re.I):
                            continue
                        for x in f.locator(A("selectedItem")).all():
                            d = x.locator(A("DELETE_charm") + ", [aria-label^='Delete'], [data-automation-id*='delete' i]")
                            if d.count():
                                d.first.click(); time.sleep(0.6)
                        if f.locator(A("selectedItem")).count() and "0 items selected" not in f.inner_text():
                            self._unknown(f"{label} (pre-selected '{sel.strip()}', not a source Ashu uses)", []); continue
                    inp = prompt.first.locator("input").first
                    inp.click(); time.sleep(0.3)
                    if inp.input_value().strip():             # text left by an earlier search hides the full list
                        inp.fill(""); time.sleep(0.5)
                    time.sleep(0.9)
                    picked = []
                    for _level in range(3):              # categories open a sub-list ("Job Board" -> "Naukri")
                        opts = self._popup_options()
                        for _wait in range(8):           # the list often loads a moment after the click (Mastercard)
                            if opts:
                                break
                            time.sleep(0.5); opts = self._popup_options()
                        names = [o.inner_text().strip() for o in opts]
                        if not opts and (picked or not re.search(r"how did you hear|^source|citizenship|nationality|country|field of study|major|discipline", label, re.I)):
                            break                        # nothing to choose (searchable fields fall through to typing)
                        idx = None if picked and re.search(r"how did you hear|^source", label, re.I) else self._choice_for(label, names)
                        if idx is None and not picked and re.search(r"citizenship|nationality|country", label, re.I):
                            # long country lists are virtualized: type to search, then choose from the filtered list
                            inp.fill(""); pg.keyboard.type(str(self.profile.get("country") or "India"), delay=40)
                            pg.keyboard.press("Enter"); time.sleep(1.5)
                            opts = self._popup_options(); names = [o.inner_text().strip() for o in opts]
                            idx = self._choice_for(label, names)
                        if idx is None and not picked and re.search(r"field of study|^major\b|discipline|speciali[sz]ation", label, re.I):
                            # long A-Z lists load only what matches the typed text: IT, else CS, else Other (Ashu)
                            for q, pat in (("Information Technology", r"\binformation technology\b"),
                                           ("Computer Science", r"^computer science\b"),
                                           ("Computer Engineering", r"^computer (engineering|applications)\b"),
                                           ("Other", r"^others?$")):
                                inp.fill(""); pg.keyboard.type(q, delay=40); pg.keyboard.press("Enter"); time.sleep(1.5)
                                opts = self._popup_options(); names = [o.inner_text().strip() for o in opts]
                                idx = next((k for k, n in enumerate(names) if re.search(pat, n, re.I)), None)
                                if idx is not None:
                                    break
                        if idx is None and not picked and re.search(r"how did you hear|^source", label, re.I):
                            # long flat source lists are virtualized: search for a truthful source instead of scrolling
                            # "Career" finds "<Company> Careers" (Autodesk, 13 Oct 2026); a hit must not name another
                            # job board ("Glassdoor Website") - the jobs come from company sites, Naukri or LinkedIn
                            other_board = re.compile(r"indeed|glassdoor|monster|dice|shine|foundit|instahire|ziprecruiter|"
                                                     r"careerbuilder|51job|104\.com|alljobs|drushim|simplyhired", re.I)
                            for q in ("Other", "Naukri", "Career", "Company Website", "Website", "LinkedIn", "Internet",
                                      "Job Board", "Job Portal", "Job Posting", "Job Site", "Online"):  # Ashu: Other, then Naukri; else any job board (14 Oct)
                                inp.fill(""); pg.keyboard.type(q, delay=40); pg.keyboard.press("Enter"); time.sleep(1.5)
                                opts = self._popup_options(); names = [o.inner_text().strip() for o in opts]
                                idx = next((k for k, n in enumerate(names) if q.lower() in n.lower() and not other_board.search(n)
                                            and not re.search(r"employee|former|referr|agency|recruiter|event|fair|university", n, re.I)), None)
                                if idx is not None:
                                    break
                            if idx is None:
                                inp.fill(""); time.sleep(0.5)   # don't leave a failed search in the box ("No Items." on the retry)
                                # typing does not filter some tenants' lists (S&P): scroll the whole list instead
                                inp.click(); time.sleep(1.2)
                                o, t = self._scroll_find(
                                    [r"naukri", r"^other\b|^others$", r"company (career|web)|career(s)? (site|page|website)|corporate (site|website)",
                                     r"linkedin", r"\bjob ?boards?\b|job (portal|posting|site)s?|online (job|posting)|^internet\b"],
                                    exclude=other_board.pattern + r"|employee|former|referr|agency|recruiter|event|fair|university|\binterns?\b")
                                if o is not None:
                                    opts, names, idx = [o], [t], 0
                        if idx is None and picked:      # inside a category: a real source we used, else its "Other" (never a random first leaf)
                            # the jobs come from company career sites, Naukri or LinkedIn - never claim Glassdoor/Indeed
                            idx = next((k for k, n in enumerate(names) if re.search(r"naukri", n, re.I) and not re.search(r"\(", n)), None)
                            if idx is None:
                                idx = next((k for k, n in enumerate(names) if re.search(r"company (career )?(site|website)|career site|corporate (site|website)|linkedin", n, re.I)), None)
                            if idx is None:
                                idx = next((k for k, n in enumerate(names) if re.search(r"^other\b|other (job board|website)s?|general", n, re.I)), None)
                            if idx is None:   # employee-status sub-lists (Citi: "Other" -> AMS / Citi Employee on Leave / Non Citi Employee)
                                idx = next((k for k, n in enumerate(names) if re.search(r"^non[- ].{0,30}employee|^external( candidate| applicant)?$", n, re.I)), None)
                            if idx is None:   # any generic job board (Ashu, 14 Oct 2026), never a named rival board
                                idx = next((k for k, n in enumerate(names) if re.search(
                                    r"\bjob ?boards?\b|job (portal|posting|site|search)s?|online (job|posting|advert)|^internet\b", n, re.I)
                                    and not re.search(r"indeed|glassdoor|monster|dice|shine|foundit|ziprecruiter|careerbuilder", n, re.I)), None)
                            if idx is None and len(names) > 6:   # long sub-list: scroll it for the same choices
                                o, t = self._scroll_find([r"naukri", r"company (career )?(site|website)|career site|corporate", r"linkedin",
                                                          r"^other\b", r"\bjob ?boards?\b|job (portal|posting|site)s?|^internet\b"],
                                                         exclude=r"indeed|glassdoor|monster|dice|shine|foundit|employee|referr|agency")
                                if o is not None:
                                    opts, names, idx = [o], [t], 0
                            if idx is None and len(names) == 1:
                                idx = 0
                        if idx is None:
                            if picked:                   # say what the sub-list offered, so the next fix is obvious
                                log.info("    %s: after %s the sub-list had %s", label[:40], picked, names[:15])
                            break
                        opts[idx].click(); time.sleep(1.2)
                        picked.append(names[idx])
                        if f.locator(A("selectedItem")).count() and "0 items selected" not in f.inner_text():
                            break
                    pg.keyboard.press("Escape"); time.sleep(0.4)
                    if not f.locator(A("selectedItem")).count() or "0 items selected" in f.inner_text():
                        self._unknown(label, names if 'names' in dir() else [])
                        continue
                    self._note(label, " > ".join(picked), "profile")
                elif radios.count():
                    if any(r.is_checked() for r in radios.all()):
                        continue
                    def radio_names():
                        return [f.locator(f"label[for='{r.get_attribute('id')}']").inner_text().strip()
                                if r.get_attribute("id") and f.locator(f"label[for='{r.get_attribute('id')}']").count() else ""
                                for r in radios.all()]
                    names = radio_names()
                    for _wait in range(6):               # labels can render after the inputs (Mastercard, 10 Oct 2026)
                        if all(names):
                            break
                        time.sleep(0.5); names = radio_names()
                    if not all(names):                   # still blank: Yes/No groups carry value="true"/"false"
                        vals = [(r.get_attribute("value") or "").lower() for r in radios.all()]
                        if set(vals) <= {"true", "false"}:
                            names = ["Yes" if v == "true" else "No" for v in vals]
                    idx = self._choice_for(label, names)
                    if idx is None:
                        self._unknown(label, names); continue
                    try:
                        radios.nth(idx).check(force=True); time.sleep(0.4)
                    except Exception:
                        pass                              # Autodesk (13 Oct 2026): "did not change its state" -> click the label below
                    if not radios.nth(idx).is_checked():  # Applied Materials (12 Oct 2026): the forced check did not register
                        rid = radios.nth(idx).get_attribute("id")
                        lab = f.locator(f"label[for='{rid}']") if rid else None
                        (lab.first if lab is not None and lab.count() else radios.nth(idx)).click(force=True); time.sleep(0.4)
                    self._note(label, names[idx], self._rule_source(label))
                elif txt.count():
                    el = txt.first
                    cur = el.input_value().strip()
                    owned, _src = self._profile_value(label)
                    if owned is not None and cur and cur != owned:
                        # Workday's resume parser guesses contact fields (it put the resume headline into Address
                        # Line 1 and "+91 ..." into Phone Number): the profile is the source of truth for these
                        el.fill(""); el.fill(owned)
                        self._note(label, owned, "profile (replaced autofill)")
                        continue
                    if cur:
                        continue
                    val, src = self._profile_value(label)
                    if val is None:
                        ans = rule_answer(label, [], "text", self.profile)
                        val, src = (ans.answer_text, self._rule_source(label, "rules")) if ans and ans.answer_text else (None, "")
                        if val and re.search(r"salary|ctc|compensation", label, re.I) and el.get_attribute("type") != "number":
                            val = with_unit(label, val)            # "14 LPA (negotiable)", not a bare "14"
                    if val is None:
                        if f.locator("[aria-required='true'], [required]").count() or "*" in (f.inner_text() or "")[:120]:
                            self._unknown(label, [])
                        continue
                    el.fill(val); time.sleep(0.3)
                    self._note(label, val, src)
                elif checks.count() > 1:                 # checkbox group ("worked at X as: Employee / Intern / ... / I have not")
                    if any(c.is_checked() for c in checks.all()):
                        continue
                    names = [(f.locator(f"label[for='{c.get_attribute('id')}']").inner_text().strip()
                              if c.get_attribute("id") else "") for c in checks.all()]
                    idx = self._choice_for(label, names)
                    if idx is None:
                        self._unknown(label, names); continue
                    lab = f.locator(f"label[for='{checks.nth(idx).get_attribute('id')}']")
                    (lab.first.click() if lab.count() else checks.nth(idx).check(force=True)); time.sleep(0.5)
                    self._note(label, names[idx], self._rule_source(label))
                elif checks.count() == 1 and re.search(r"i am fluent in this language", label, re.I):
                    if not checks.first.is_checked():
                        checks.first.check(force=True); self._note(label, "checked", "profile")
                elif checks.count() == 1 and re.search(r"i (agree|acknowledge|consent|confirm|have read|have reviewed|understand|certify|declare|attest)|terms|privacy (policy|notice|statement)|confirm the statement|check this box to (confirm|agree|acknowledge)", label, re.I):
                    if not checks.first.is_checked():
                        # "Yes, I have read and consent to the above" names nothing itself: the "above" is the
                        # section's heading (Citi: "Terms and Conditions / Terms of Use"); only a terms or privacy
                        # section counts, and the label must still not be a legal claim
                        above = ""
                        if re.search(r"(consent|agree) to the above|read and (consent|agree)", label, re.I) and not LEGAL_CLAIM.search(label):
                            body = self.pg.inner_text("body")
                            k = body.find(label[:40])
                            above = body[max(0, k - 4000):k] if k > 0 else ""
                        if consent_only(label) or re.search(r"terms and conditions|terms of use|privacy (policy|notice|statement)", above[-4000:], re.I):
                            checks.first.check(force=True)
                            self._note(label, "checked", "consent")
                        else:                                # may be a legal claim (LEGAL_CLAIM): the person decides
                            self._unknown(label, [])
            except Exception as e:
                log.warning("    field error (%s): %s", type(e).__name__, str(e)[:100])
                if label:
                    self._unknown(label, [])             # not filled: keep it from being submitted unseen

    # ------------------------------------------------------------------ My Experience (from the resume data)
    MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

    def _dates(self, text: str) -> tuple[tuple[int, int] | None, tuple[int, int] | None, bool]:
        """'Feb 2026 – Present' -> ((2, 2026), None, True)."""
        found = [(self.MONTHS[m.group(1)[:3].lower()], int(m.group(2))) for m in re.finditer(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{4})", text)]
        current = bool(re.search(r"present|current|now", text, re.I))
        return (found[0] if found else None), (found[1] if len(found) > 1 else None), current

    def _set_date(self, field, month_year: tuple[int, int] | None) -> None:
        if not month_year:
            return
        # the month/year inputs are spinbuttons kept off-screen under a display layer: focus them directly and type
        mon = field.locator(A("dateSectionMonth-input"))
        yr = field.locator(A("dateSectionYear-input"))
        try:
            if mon.count():
                mon.first.evaluate("e => e.focus()"); self.pg.keyboard.type(f"{month_year[0]:02d}", delay=80)
            if yr.count():
                yr.first.evaluate("e => e.focus()"); self.pg.keyboard.type(str(month_year[1]), delay=80)
            self.pg.keyboard.press("Tab")
        except Exception as e:
            log.warning("    date not set (%s)", type(e).__name__)
        time.sleep(0.3)

    def _set_text(self, panel, aid: str, value: str) -> None:
        el = panel.locator(f"{A(aid)} input, {A(aid)} textarea").first
        if el.count() and el.input_value().strip() != value:
            el.fill(""); el.fill(value)

    def _panels(self, section: str):
        return self.pg.locator(f"[role='group'][aria-labelledby^='{section}-'][aria-labelledby$='-panel']")

    def _ensure_panels(self, section: str, n: int) -> None:
        for _ in range(8):
            have = self._panels(section).count()
            if have >= n:
                break
            btn = self.pg.locator(f"[aria-labelledby='{section}-section'] {A('add-button')}")
            if not btn.count():
                heading = section.replace("-", " ")
                btn = self.pg.locator("[role='group']").filter(has=self.pg.locator("h3, h4", has_text=heading)).locator(
                    f"{A('add-button')}, button:has-text('Add')")
            if not btn.count():
                log.info("    no '%s' section on this site", section.replace("-", " "))
                return
            btn.first.click(timeout=8000)
            time.sleep(1.2)
        while self._panels(section).count() > n:       # drop junk entries the resume parser invented
            last = self._panels(section).nth(self._panels(section).count() - 1)
            btn = last.locator("button:has-text('Delete'), " + A("DELETE_charm") + ", [aria-label^='Delete']").first
            if not btn.count():
                break
            btn.click(); time.sleep(1.2)

    @staticmethod
    def _norm_school(t: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower()).split())

    def school_names(self, institution: str = "") -> set[str]:
        """Exact names this candidate's school may be listed under: the full name (with and without the
        bracketed acronym / trailing city) and each alias in profile school_aliases. Normalized."""
        out = set()
        for n in [institution, self.profile.get("school")]:
            if not n:
                continue
            for v in (n, re.sub(r"\s*\(.*?\)", "", n), n.split(",")[0], re.sub(r"\s*\(.*?\)", "", n.split(",")[0])):
                out.add(self._norm_school(v))
        out |= {self._norm_school(a) for a in self.profile.get("school_aliases") or [] if a}
        return {n for n in out if n}

    def school_option_matches(self, option: str, institution: str = "") -> bool:
        """An option is the candidate's school only if it IS the full name or an exact alias (a shared prefix
        of a long name can also name other colleges)."""
        names = self.school_names(institution)
        return self._norm_school(option) in names or self._norm_school(re.sub(r"\s*\(.*?\)", "", option)) in names

    def _pick_prompt(self, field, searches: list[str], institution: str = "") -> bool:
        """Searchable school list: type each search in turn and take an option that is exactly the school (full
        name or alias); "Other"/"Not Listed" only as a last resort, and then the person reviews it."""
        inp = field.locator("input").first
        for q in searches:
            fallback = re.fullmatch(r"others?|not listed|school not listed|not in list|other university|other institution|unlisted", q.strip(), re.I)
            inp.click(); inp.fill(""); self.pg.keyboard.type(q, delay=40); self.pg.keyboard.press("Enter"); time.sleep(2)
            if fallback:
                # "Other", "Not Listed", "School Not Listed" - but never a country-specific one ("School Not Listed - UK")
                opt = next((o for o in self._popup_options()
                            if re.fullmatch(rf"(school |institution |university )?{re.escape(q.strip().lower())}( school| university| institution)?",
                                            self._norm_school(o.inner_text()))), None)
            else:
                opt = next((o for o in self._popup_options() if self.school_option_matches(o.inner_text(), institution)), None)
            if opt:
                text = opt.inner_text().strip()
                opt.click(); time.sleep(0.8); self.pg.keyboard.press("Escape")
                self._note("School or University", text or q, "fallback (school not listed)" if fallback else "resume")
                return True
            self.pg.keyboard.press("Escape")
        self._unknown("School or University", [])
        return False

    def _fill_experience(self) -> None:
        try:
            self._fill_experience_inner()
        except Exception as e:
            log.warning("  My Experience: %s", str(e)[:150])
            self._unknown("My Experience (not filled: check every entry)", [])

    def _fill_experience_inner(self) -> None:
        """Workday's resume parser garbles entries (dropped the current job, company "ai", summary as a role
        description), so overwrite every entry from the structured resume data instead of trusting it."""
        try:
            from ..resume_variants import load_variants
            core = load_variants()["core"]
        except Exception as e:
            log.warning("  no resume data for My Experience: %s", e)
            self._unknown("My Experience (no resume data to fill it from)", [])
            return
        pg = self.pg
        self._visible(f"{A('formField-jobTitle')}, {A('add-button')}", timeout=15000)
        jobs = core.get("experience", [])
        # Salesforce's tenant calls the section "Work History (Optional)" (15 Oct 2026): find whichever name is used
        sec = next((n for n in ("Work-Experience", "Work-History") if self._panels(n).count()), None) or \
              next((n for n in ("Work-Experience", "Work-History")
                    if pg.locator("h2, h3, h4", has_text=n.replace("-", " ")).count()), "Work-Experience")
        if self._panels(sec).count() or jobs:
            self._ensure_panels(sec, len(jobs))
            for i, job in enumerate(jobs):
              try:
                panel = self._panels(sec).nth(i)
                panel.scroll_into_view_if_needed()
                self._set_text(panel, "formField-jobTitle", job["title"])
                self._set_text(panel, "formField-companyName", job["company"])
                self._set_text(panel, "formField-location", job.get("location", ""))
                start, end, current = self._dates(job["dates"])
                box = panel.locator(f"{A('formField-currentlyWorkHere')} input[type='checkbox']")
                if box.count() and box.first.is_checked() != current:
                    box.first.click(force=True); time.sleep(0.5)
                self._set_date(panel.locator(A("formField-startDate")), start)
                if not current:
                    self._set_date(panel.locator(A("formField-endDate")), end)
                self._set_text(panel, "formField-roleDescription", "\n".join(f"- {b}" for b in job.get("bullets", [])))
                self._note(f"Work Experience {i + 1}", f"{job['title']} @ {job['company']} ({job['dates']})", "resume")
              except Exception as e:
                log.warning("    work experience %d not filled: %s", i + 1, str(e)[:120])
                self._unknown(f"Work Experience {i + 1}", [])
        try:
            self._fill_education(core.get("education", []))
        except Exception as e:
            log.warning("    education not filled: %s", str(e)[:120])
            self._unknown("Education", [])
        try:
            self._fill_websites()
        except Exception as e:
            log.warning("    websites not filled: %s", str(e)[:100])
            self._unknown("Websites", [])
        try:
            self._fill_skills()
        except Exception as e:
            log.warning("    skills not filled: %s", str(e)[:100])
            self._unknown("Skills", [])

    def _fill_education(self, edus: list) -> None:
        pg = self.pg
        if edus:
            self._ensure_panels("Education", len(edus))
            for i, ed in enumerate(edus):
              try:
                panel = self._panels("Education").nth(i)
                school = ed["institution"].split(",")[0].strip()
                if panel.locator(f"{A('formField-schoolName')} input").count():
                    self._set_text(panel, "formField-schoolName", school)
                elif panel.locator(f"{A('formField-school')} input").count():
                    aliases = sorted((a for a in self.profile.get("school_aliases") or [] if a), key=len, reverse=True)
                    searches = [re.sub(r"\s*\(.*?\)", "", school)] + [a for a in aliases if a != school]
                    # partial / former names first (NSUT was "Netaji Subhas Institute of Technology", NSIT, until 2018),
                    # then the many spellings of "not listed" (Citi found none of "Other"/"Not Listed", 16 Oct 2026)
                    searches = searches + [x for x in ("Netaji Subhas", "NSUT", "Netaji Subhas Institute of Technology", "NSIT")
                                           if x not in searches]
                    self._pick_prompt(panel.locator(A("formField-school")), searches + ["Other", "Not Listed", "Others",
                                      "School Not Listed", "Not in list", "Other University", "Other Institution", "Unlisted"],
                                      institution=ed["institution"])
                deg = panel.locator(f"{A('formField-degree')} button[aria-haspopup='listbox']")
                if deg.count():
                    opts = self._open_options(deg.first)
                    names = [o.inner_text().strip() for o in opts]
                    idx = next((k for k, n in enumerate(names) if re.search(r"bachelor|b\.?\s?tech|b\.?\s?eng|^b\.?e\.?$|undergrad", n, re.I)), None)
                    if idx is None:     # US-style short lists (GED/HS/AA/BA/BS): a B.Tech is a Bachelor of Science
                        idx = next((k for k, n in enumerate(names) if re.fullmatch(r"b\.?\s?s\.?c?\.?", n.strip(), re.I)), None)
                    if idx is not None:
                        opts[idx].click(); time.sleep(0.6)
                    else:
                        pg.keyboard.press("Escape")
                fos = panel.locator(f"{A('formField-fieldOfStudy')} input").first
                major = str(self.profile.get("major") or "").strip()
                if major and fos.count() and not panel.locator(f"{A('formField-fieldOfStudy')} {A('selectedItem')}").count():
                    # long A-Z lists only load what matches the typed text (14 Oct 2026: stuck at "Accounting..."):
                    # search each discipline in Ashu's order - Information Technology, else Computer Science, else Other
                    tries = [(major, rf"^{re.escape(major)}$"), ("Information Technology", r"\binformation technology\b"),
                             ("Computer Science", r"^computer science( (and|&) engineering)?$|^computer science\b"),
                             ("Computer Engineering", r"^computer (engineering|applications)\b"),
                             ("Computer", r"\bcomputer (science|engineering)\b"), ("Other", r"^other$|^others$")]
                    picked = None
                    for q, pat in tries:
                        fos.click(); fos.fill(""); pg.keyboard.type(q, delay=40); pg.keyboard.press("Enter"); time.sleep(1.5)
                        opt = next((o for o in self._popup_options() if re.search(pat, o.inner_text().strip(), re.I)), None)
                        if opt:
                            picked = opt.inner_text().strip(); opt.click(); time.sleep(0.6)
                            break
                        pg.keyboard.press("Escape")
                    pg.keyboard.press("Escape")
                    if picked:
                        self._note("Field of Study", picked, "profile")
                if self.profile.get("gpa"):
                    self._set_text(panel, "formField-gradeAverage", str(self.profile["gpa"]))
                years = [int(y) for y in re.findall(r"\d{4}", ed.get("dates", ""))]
                if years:
                    self._set_date(panel.locator(A("formField-firstYearAttended")), (1, years[0]))
                    self._set_date(panel.locator(A("formField-lastYearAttended")), (1, years[-1]))
                self._note(f"Education {i + 1}", f"{ed['degree']} @ {ed['institution']}", "resume")
              except Exception as e:
                log.warning("    education %d not filled: %s", i + 1, str(e)[:120])
                self._unknown(f"Education {i + 1}", [])

    def _fill_websites(self) -> None:
        """Websites: portfolio, GitHub, LinkedIn."""
        pg = self.pg
        links = self.profile.get("links") or {}
        urls = [u for u in (links.get("portfolio"), links.get("github"), links.get("linkedin")) if u]
        if urls and pg.locator("[aria-labelledby='Websites-section'], [role='group'][aria-labelledby='Websites-section']").count():
            self._ensure_panels("Websites", len(urls))
            for i, u in enumerate(urls):
                panel = self._panels("Websites").nth(i)
                el = panel.locator("input").first
                if el.count() and el.input_value().strip() != u:
                    el.fill(u)
            self._note("Websites", ", ".join(urls), "profile")

    # Workday's skill catalogue spells many names differently from the resume
    SKILL_ALIASES = {"llms": "Large Language Models", "rag": "Retrieval Augmented Generation", "nlp": "Natural Language Processing",
                     "genai": "Generative AI", "ci/cd": "Continuous Integration", "express.js": "Express.js", "sql": "SQL"}

    @staticmethod
    def _skill_norm(t: str) -> str:
        t = re.sub(r"\s*\(.*?\)", "", (t or "").lower())          # "Python (Programming Language)" -> "python"
        return re.sub(r"[^a-z0-9+#]", "", t.replace(".js", "js"))   # "React.js" / "ReactJS" -> "reactjs"

    def _fill_skills(self) -> None:
        """Skills: type each and pick the matching catalogue entry. Accepts the catalogue's own spelling of the same
        skill ("Python (Programming Language)", "React.js", "Large Language Models (LLM)"), never a different skill."""
        pg = self.pg
        sk = pg.locator(f"{A('formField-skills')} input, input[id*='skills' i][id$='skills' i]").first
        if not sk.count() or pg.locator(f"{A('formField-skills')} {A('selectedItem')}").count():
            return
        wanted = list(dict.fromkeys((self.profile.get("skills_primary") or []) + (self.profile.get("skills_secondary") or [])))
        added = []
        for skill in wanted:
            if len(added) >= 15:
                break
            query = self.SKILL_ALIASES.get(skill.lower(), skill)
            target = {self._skill_norm(skill), self._skill_norm(query), self._skill_norm(skill).replace("js", "")}
            sk.click(); sk.fill(query); pg.keyboard.press("Enter"); time.sleep(1.5)
            opts = self._popup_options()
            opt = next((o for o in opts if o.inner_text().strip().lower() == query.lower()), None) or \
                  next((o for o in opts if self._skill_norm(o.inner_text()) in target
                         or self._skill_norm(o.inner_text()).removesuffix("js") in target), None)
            if opt:
                name = opt.inner_text().strip()
                if name not in added:
                    opt.click(); added.append(name); time.sleep(0.5)
            pg.keyboard.press("Escape"); time.sleep(0.3)
        self._note("Skills", ", ".join(added) or "(none matched)", "profile")

    def submit_blockers(self) -> tuple[list[str], list[str]]:
        """(unanswered, doubtful): fields left for the person (value None / source 'unknown') and answers whose
        source is not in TRUSTED. Either one keeps the application from being submitted automatically."""
        unanswered = [f["label"] for f in self.result.filled if f.get("value") is None or f.get("source") == "unknown"]
        doubtful = [f["label"] for f in self.result.filled
                    if f.get("value") is not None and f.get("source") != "unknown" and f.get("source") not in self.TRUSTED]
        return unanswered, doubtful

    def _submit(self) -> WorkdayResult:
        """Press Submit only if every field was answered and every answer came from confirmed facts; confirm
        from Workday's own page."""
        unanswered, doubtful = self.submit_blockers()
        if unanswered or doubtful:
            why = (f"unanswered {unanswered[:3]}" if unanswered else "") + ("; " if unanswered and doubtful else "") + \
                  (f"unconfirmed answers {doubtful[:3]}" if doubtful else "")
            self.result.status, self.result.notes = "ready_for_review", f"not submitted: {why}"
            return self.result
        self.snapshot("review-before-submit")
        btn = self.pg.locator(f"{NEXT} >> visible=true").first
        if not btn.count() or "submit" not in btn.inner_text().lower():
            self.result.status, self.result.notes = "ready_for_review", "no Submit button on Review"
            return self.result
        self._click(NEXT)
        for _ in range(15):
            time.sleep(2)
            body = self.pg.inner_text("body")
            if re.search(r"application (has been )?submitted|successfully submitted|thank you for (applying|your application)|congratulations", body, re.I):
                self.snapshot("submitted")
                self.result.status, self.result.notes = "applied", "submitted; Workday confirmed"
                return self.result
            if re.search(r"something went wrong|errors? found", body, re.I):
                break
        self.snapshot("submit-unconfirmed")
        # Submit WAS pressed: terminal, never retried automatically (a retry could send a second application)
        self.result.status, self.result.notes = "submitted_unconfirmed", \
            "pressed Submit but no confirmation seen: check Candidate Home (do NOT resubmit blindly)"
        return self.result

    def _unknown(self, label: str, options: list[str]) -> None:
        log.info("    %-45s -> (no answer) options=%s", label[:45], options[:6])
        self.result.filled.append({"label": label, "value": None, "source": "unknown", "options": options})
        from ..ask_ashu import record
        record(label, options, "workday", getattr(self, 'company', ''))

    def _next(self, step_name: str) -> bool:
        pg = self.pg
        if not self._visible(NEXT, timeout=15000):
            self.snapshot("no-continue")
            self.result.status, self.result.notes = "needs_you", f"no Continue button on '{step_name}'"
            return False
        self._click(NEXT)
        time.sleep(5)
        if re.search(r"something went wrong|please refresh the page", pg.inner_text("body"), re.I):
            log.warning("  Workday error after Continue: refreshing")
            pg.reload(wait_until="domcontentloaded"); time.sleep(8)
            return True                                  # the main loop re-reads the step and refills it
        if self.step() == step_name and not getattr(self, "_retried", None) == step_name:
            # fields that rendered late (below the fold, after a refresh) were missed: one more pass, then Continue
            self._retried = step_name
            log.info("  still on '%s': filling again before giving up", step_name)
            self._fill_form()
            self._click(NEXT); time.sleep(5)
        if self.step() == step_name and re.search(r"duplicate website", pg.inner_text("body"), re.I):
            # the account already holds these links from an earlier application: websites are optional, drop the rows
            log.info("  duplicate website URLs: removing the Websites rows and continuing")
            for _ in range(6):
                dels = [b for b in self._panels("Websites").locator("button:has-text('Delete'), [data-automation-id='panelSetDeleteButton']").all() if b.is_visible()]
                if not dels:
                    break
                dels[0].click(); time.sleep(1)
            self._click(NEXT); time.sleep(5)
        if self.step() == step_name:
            self.snapshot("stuck-" + re.sub(r"\W+", "-", step_name.lower())[:30])
            errors = [e.strip() for e in pg.locator(f"{A('errorMessage')}, [role='alert'], [data-automation-id*='error' i] >> visible=true").all_inner_texts() if e.strip()]
            what = f"'{step_name}' has fields the bot could not fill: " + "; ".join(errors[:3] or ["(see the page)"])
            log.warning("  %s", what)
            if self.ask_human(what):
                return True
            self.result.status, self.result.notes = "needs_you", what
            return False
        return True
