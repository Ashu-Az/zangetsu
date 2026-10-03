"""Log in (manually, once) and dump the current LinkedIn profile to output/linkedin_profile.txt."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # repo root, for naukri_bot

import time, re
from pathlib import Path
from linkedin_session import open_linkedin

b = open_linkedin()
pg = b.page
print("LOGGED_IN", flush=True)
pg.goto("https://www.linkedin.com/in/me/", wait_until="domcontentloaded", timeout=60000); time.sleep(6)
print("PROFILE_URL", pg.url, flush=True)
for _ in range(16):
    pg.mouse.wheel(0, 900); time.sleep(0.7)
time.sleep(2)
main = pg.locator("main").first
text = main.inner_text() if main.count() else pg.inner_text("body")
out = Path("output"); out.mkdir(exist_ok=True)
(out / "linkedin_profile.txt").write_text(text)
(out / "linkedin_profile.html").write_text(pg.content())
base = pg.url.split("?")[0].rstrip("/")
for sub in ["details/experience", "details/skills", "details/projects", "details/education", "details/certifications"]:
    try:
        pg.goto(f"{base}/{sub}/", wait_until="domcontentloaded", timeout=60000); time.sleep(4)
        for _ in range(6): pg.mouse.wheel(0, 900); time.sleep(0.5)
        t = pg.locator("main").first.inner_text()
        (out / f"linkedin_{sub.split('/')[-1]}.txt").write_text(t)
        print("SAVED", sub, len(t), flush=True)
    except Exception as e:
        print("ERR", sub, e.__class__.__name__, flush=True)
    time.sleep(2)
b.__exit__(None, None, None)
print("DONE", flush=True)
