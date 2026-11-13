"""Open a list of URLs in tabs of a saved browser profile and keep the window open until Ashu closes it.

Used for one-time sign-ups (freelance / AI-training platforms, 8 Nov 2026): Ashu creates the accounts himself, and the
logins stay in the profile so later bot runs can apply from those sessions.

    .venv/bin/python tools/open_tabs.py --profile freelance-profile urls.txt
"""
import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="one URL per line; text after ' | ' is ignored")
    ap.add_argument("--profile", default="freelance-profile", help="folder under state/")
    a = ap.parse_args()
    urls = [l.split(" | ")[0].strip() for l in open(a.file) if l.strip().startswith("http")]
    if not urls:
        sys.exit("no URLs")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / "state" / a.profile), headless=False,
            viewport={"width": 1366, "height": 860}, locale="en-IN", timezone_id="Asia/Kolkata",
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            ignore_default_args=["--enable-automation"])
        first = ctx.pages[0] if ctx.pages else ctx.new_page()
        for i, u in enumerate(urls):
            pg = first if i == 0 else ctx.new_page()
            try:
                pg.goto(u, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"could not open {u}: {str(e)[:80]}", flush=True)
        print(f"{len(urls)} tab(s) open; close the window when done", flush=True)
        while ctx.pages:
            time.sleep(2)
        ctx.close()


if __name__ == "__main__":
    main()
