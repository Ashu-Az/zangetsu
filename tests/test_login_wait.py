"""The login wait loop must never navigate while the user is on the OTP / captcha step."""
from naukri_bot import browser as browser_mod
from naukri_bot.browser import Browser
from naukri_bot.config import Settings


class FakeBrowser(Browser):
    """Simulates: login page -> OTP step (form hidden, not logged in) -> logged in."""

    def __init__(self, states):
        super().__init__(Settings())
        self.settings.login_wait_seconds = 60
        self.states = list(states)
        self.gotos = []
        self.logged_in = False

    def goto(self, url, settle=(0, 0)):
        self.gotos.append(url)

    def is_logged_in(self):
        return self.logged_in

    def _looks_logged_in_here(self):
        state = self.states.pop(0) if self.states else "logged_in"
        self.logged_in = state == "logged_in"
        return self.logged_in


def test_otp_step_is_not_interrupted(monkeypatch):
    monkeypatch.setattr(browser_mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(browser_mod, "pause", lambda *a: None)
    b = FakeBrowser(["otp", "otp", "otp", "logged_in"])
    b.settings.naukri_email = b.settings.naukri_password = ""
    assert b.ensure_logged_in()
    # one navigation to the login page, none while the OTP step was showing
    assert b.gotos == [browser_mod.LOGIN_URL]
