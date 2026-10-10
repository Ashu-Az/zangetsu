"""Headed Chromium with a persistent profile so the Naukri login survives between runs.

Naukri sits behind Akamai, which returns "Access Denied" to headless Chromium, so the
bot always runs with a visible window.
"""
from __future__ import annotations

import logging
import random
import time

from playwright.sync_api import Page, sync_playwright, TimeoutError as PWTimeout

from . import selectors as S
from .config import Settings
from .humanize import human_type, pause

log = logging.getLogger(__name__)

HOME = "https://www.naukri.com/"
MNJ_HOME = "https://www.naukri.com/mnjuser/homepage"
LOGIN_URL = "https://www.naukri.com/nlogin/login"


class Browser:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pw = None
        self.context = None
        self.page: Page | None = None

    # ---- lifecycle ----
    def __enter__(self) -> "Browser":
        self.settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        self.pw = sync_playwright().start()
        kwargs = dict(
            user_data_dir=str(self.settings.browser_profile_dir),
            headless=False,
            viewport={"width": 1366, "height": 860},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            ignore_default_args=["--enable-automation"],
        )
        if self.settings.browser_executable:
            kwargs["executable_path"] = self.settings.browser_executable
        self.context = self.pw.chromium.launch_persistent_context(**kwargs)
        self.context.set_default_timeout(15000)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        from .humanize import pace_page                       # steady ~10 s rhythm on every page load
        for pg in self.context.pages:
            pace_page(pg, self.settings)
        self.context.on("page", lambda pg: pace_page(pg, self.settings))
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.context:
                self.context.close()
        finally:
            if self.pw:
                self.pw.stop()

    # ---- navigation ----
    def goto(self, url: str, settle: tuple[float, float] = (2.5, 4.5)) -> None:
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        pause(*settle)

    def body_text(self) -> str:
        try:
            return self.page.inner_text("body", timeout=5000)
        except PWTimeout:
            return ""

    def text_present(self, needles: list[str]) -> bool:
        body = self.body_text().lower()
        return any(n.lower() in body for n in needles)

    # ---- login ----
    def _login_form_shown(self) -> bool:
        """Naukri keeps the /mnjuser/ URL when logged out and renders the login form in place."""
        for sel in (S.LOGIN_LINK, S.LOGIN_USERNAME, S.LOGIN_PASSWORD):
            if self.first_visible(sel):
                return True
        return False

    def is_logged_in(self) -> bool:
        """True only on positive evidence (a logged-in header element). A missing login form is not
        evidence: Akamai's 'Access Denied' page and half-rendered pages have no form either."""
        self.goto(MNJ_HOME, settle=(2, 3))
        if "access denied" in self.page.title().lower():
            log.error("Naukri returned 'Access Denied' (bot protection).")
            return False
        if "/mnjuser/" not in self.page.url or self._login_form_shown():
            return False
        return any(self.page.locator(m).count() for m in S.LOGGED_IN_MARKERS)

    def save_debug(self, name: str) -> None:
        """Screenshot + HTML of the current page into output/debug/, for fixing selectors offline."""
        from .config import OUTPUT_DIR
        d = OUTPUT_DIR / "debug"
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60]
        try:
            self.page.screenshot(path=str(d / f"{stamp}-{safe}.png"), full_page=True)
            (d / f"{stamp}-{safe}.html").write_text(self.page.content(), encoding="utf-8")
            log.info("  debug snapshot: output/debug/%s-%s.*", stamp, safe)
        except Exception as e:
            log.warning("  could not save debug snapshot: %s", e)

    def _looks_logged_in_here(self) -> bool:
        """Logged-in check on the current page, without navigating (navigating would wipe an OTP/captcha step)."""
        try:
            if self._login_form_shown():
                return False
            return any(self.page.locator(m).count() for m in S.LOGGED_IN_MARKERS)
        except Exception:      # page mid-navigation
            return False

    def ensure_logged_in(self) -> bool:
        if self.is_logged_in():
            log.info("Already logged in (persistent profile).")
            return True

        self.goto(LOGIN_URL)
        if self.settings.naukri_email and self.settings.naukri_password:
            log.info("Filling login form…")
            try:
                human_type(self.page, S.LOGIN_USERNAME, self.settings.naukri_email)
                pause(0.5, 1.2)
                human_type(self.page, S.LOGIN_PASSWORD, self.settings.naukri_password)
                pause(0.5, 1.0)
                self.page.locator(S.LOGIN_SUBMIT).first.click()
            except PWTimeout:
                log.warning("Login form not found; waiting for you to log in manually.")
        else:
            log.info("No credentials in .env. Log in manually in the browser window.")

        deadline = time.time() + self.settings.login_wait_seconds
        log.info("Waiting up to %ss for login (OTP / captcha are fine)…", self.settings.login_wait_seconds)
        # Only watch the page here. The OTP / captcha step hides the login form too, so a hidden
        # form does not mean "logged in", and any navigation would destroy that step.
        while time.time() < deadline:
            time.sleep(3)
            if self._looks_logged_in_here():
                pause(2, 3)    # let the post-login redirect settle, then confirm once
                if self.is_logged_in():
                    log.info("Login detected.")
                    return True
        log.error("Login was not completed in time.")
        self.save_debug("login-timeout")
        return False

    # ---- small conveniences ----
    def first_visible(self, selector: str):
        """Return the first visible element matching selector or None."""
        loc = self.page.locator(selector)
        for i in range(min(loc.count(), 10)):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
        return None

    def first_present(self, selector: str):
        """First visible match, else the first attached match, else None."""
        el = self.first_visible(selector)
        if el:
            return el
        loc = self.page.locator(selector)
        return loc.first if loc.count() else None

    def click_if_visible(self, selector: str) -> bool:
        el = self.first_present(selector)
        if not el:
            return False
        try:
            el.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        pause(0.3, 0.9)
        try:
            el.click(timeout=5000)
        except Exception:
            el.evaluate("e => e.click()")  # hidden by an overlay or off-screen: fire the click directly
        return True

    def random_idle(self) -> None:
        from .humanize import between_applies
        between_applies(self.settings)
