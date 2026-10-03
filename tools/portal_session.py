"""Separate persistent Chromium profile per job portal, with a manual-login wait."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # repo root, for naukri_bot

import sys, time, subprocess
from pathlib import Path
from naukri_bot.config import Settings, STATE_DIR
from naukri_bot.browser import Browser

SITES = {
    "indeed":    dict(login="https://secure.indeed.com/auth?hl=en_IN", home="https://profile.indeed.com/",
                      logged_in=lambda pg: "secure.indeed.com" not in pg.url and "/auth" not in pg.url and pg.locator("text=Sign in").count() == 0),
    "hirist":    dict(login="https://www.hirist.tech/login", home="https://www.hirist.tech/",
                      logged_in=lambda pg: pg.locator("a[href*='logout'], a:has-text('Logout'), [class*='profile-pic'], img[alt*='profile' i]").count() > 0 and "/login" not in pg.url),
    "wellfound": dict(login="https://wellfound.com/login", home="https://wellfound.com/profile/edit",
                      logged_in=lambda pg: "/login" not in pg.url and pg.locator("a[href*='/logout'], button:has-text('Log out'), a:has-text('Log out'), nav img[alt*='avatar' i], [data-test*='avatar' i]").count() > 0),
}

def open_portal(site: str, wait_login_s: int = 900) -> Browser:
    cfg = SITES[site]
    Settings.browser_profile_dir = property(lambda self, s=site: STATE_DIR / f"{s}-profile")
    b = Browser(Settings()).__enter__()
    pg = b.page
    try:
        pg.bring_to_front()
        subprocess.run(["osascript", "-e", 'tell application "System Events" to set frontmost of (last process whose name contains "Chrom") to true'], timeout=10, capture_output=True)
    except Exception:
        pass
    pg.goto(cfg["home"], wait_until="domcontentloaded", timeout=60000); time.sleep(5)
    if cfg["logged_in"](pg):
        print(f"[{site}] LOGGED_IN", flush=True); return b
    try: pg.goto(cfg["login"], wait_until="domcontentloaded", timeout=60000)
    except Exception: pass
    print(f"[{site}] WAITING_FOR_LOGIN", flush=True)
    deadline = time.time() + wait_login_s
    while time.time() < deadline:
        time.sleep(4)
        try:
            if cfg["logged_in"](pg):
                time.sleep(2)
                pg.goto(cfg["home"], wait_until="domcontentloaded", timeout=60000); time.sleep(5)
                if cfg["logged_in"](pg):
                    print(f"[{site}] LOGGED_IN", flush=True); return b
        except Exception:
            pass
    b.__exit__(None, None, None)
    raise SystemExit(f"[{site}] LOGIN_TIMEOUT")

if __name__ == "__main__":
    site = sys.argv[1]
    b = open_portal(site)
    pg = b.page
    out = Path("output"); out.mkdir(exist_ok=True)
    try:
        time.sleep(3)
        for _ in range(10): pg.mouse.wheel(0, 900); time.sleep(0.5)
        text = pg.inner_text("body")
        (out / f"{site}_profile.txt").write_text(text); (out / f"{site}_profile.html").write_text(pg.content())
        pg.screenshot(path=str(out / f"{site}_profile.png"), full_page=True)
        links = pg.evaluate("() => Array.from(document.querySelectorAll('a')).map(a => a.href).filter(h => /profile|resume|skills|experience|edit|settings|preferences/i.test(h))")
        (out / f"{site}_links.txt").write_text("\n".join(dict.fromkeys(links)))
        print(f"[{site}] SAVED url={pg.url} chars={len(text)} links={len(set(links))}", flush=True)
    finally:
        b.__exit__(None, None, None)
