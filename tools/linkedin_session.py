"""Shared helper: a separate persistent Chromium profile for LinkedIn (state/linkedin-profile)."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # repo root, for naukri_bot

import time
from naukri_bot.config import Settings, STATE_DIR
from naukri_bot.browser import Browser

Settings.browser_profile_dir = property(lambda self: STATE_DIR / "linkedin-profile")


def open_linkedin(wait_login_s: int = 900) -> Browser:
    b = Browser(Settings()).__enter__()
    pg = b.page
    try:
        pg.bring_to_front()
        import subprocess
        # raise the newest Chromium window (the one just launched) above everything else
        subprocess.run(["osascript", "-e", 'tell application "System Events" to set frontmost of (last process whose name contains "Chrom") to true'], timeout=10, capture_output=True)
    except Exception:
        pass
    pg.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(4)
    deadline = time.time() + wait_login_s
    announced = False
    while time.time() < deadline:
        url = pg.url
        if "/feed" in url and "login" not in url and "authwall" not in url and pg.locator("input#username, input[name='session_key']").count() == 0:
            return b
        if not announced:
            print("WAITING_FOR_LOGIN: sign in to LinkedIn in the browser window", flush=True)
            announced = True
            # never navigate away from a verification screen (checkpoint / challenge)
            if "login" not in url and "checkpoint" not in url and "challenge" not in url:
                try: pg.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
                except Exception: pass
        if "checkpoint" in url or "challenge" in url:
            if not globals().get("_told_challenge"):
                print("VERIFICATION_PENDING: approve the sign-in in the LinkedIn phone app (tap Yes)", flush=True)
                globals()["_told_challenge"] = True
        time.sleep(3)
    b.__exit__(None, None, None)
    raise SystemExit("LOGIN_TIMEOUT")
