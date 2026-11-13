"""Step-by-step control of a long-lived browser, for sites without a bot flow (freelance platforms, 8 Nov 2026).

    .venv/bin/python tools/drive.py start freelance-profile      # opens the browser and keeps it running
    .venv/bin/python tools/drive.py run 'pg.goto("https://...")'  # runs Python with `pg` = active tab, `ctx` = context
    .venv/bin/python tools/drive.py look                          # screenshot + visible text + form fields of the tab

Each call attaches over the DevTools port and leaves the browser open.
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PORT = 9333
SHOT = ROOT / "output" / "debug" / "drive.png"

FIELDS_JS = """() => [...document.querySelectorAll('input,textarea,select,button,[role=button],[role=combobox],a[href]')]
  .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
  .slice(0, 120).map(e => {
    const lab = (e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.placeholder || '';
    const t = (e.innerText || e.value || '').trim().slice(0, 50);
    return `${e.tagName.toLowerCase()}${e.type ? '[' + e.type + ']' : ''} ${e.name ? 'name=' + e.name : ''} | ${lab.trim().slice(0, 60)} | ${t}`;
  })"""


def start(profile: str) -> None:
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / "state" / profile), headless=False,
            viewport={"width": 1366, "height": 860}, locale="en-IN", timezone_id="Asia/Kolkata",
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", f"--remote-debugging-port={PORT}"],
            ignore_default_args=["--enable-automation"])
        print("browser up", flush=True)
        while ctx.pages:
            time.sleep(2)


def attach(fn) -> None:
    with sync_playwright() as pw:
        br = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
        ctx = br.contexts[0]
        pg = ctx.pages[-1] if ctx.pages else ctx.new_page()   # every tab closed by hand: the browser is still up
        fn(pw, ctx, pg)


def look(_pw, _ctx, pg) -> None:
    pg.screenshot(path=str(SHOT))
    print("URL", pg.url)
    print(" ".join(pg.inner_text("body").split())[:2500])
    print("--- fields")
    for f in pg.evaluate(FIELDS_JS):
        print(f)
    print("screenshot:", SHOT)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "start":
        start(sys.argv[2])
    elif cmd == "look":
        attach(look)
    elif cmd == "run":
        code = sys.argv[2]

        def run(pw, ctx, pg):
            env = {"pw": pw, "ctx": ctx, "pg": pg, "json": json, "time": time}
            exec(code, env)
            if env.get("out") is not None:
                print(env["out"])
        attach(run)
