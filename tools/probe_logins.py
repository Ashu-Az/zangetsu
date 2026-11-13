"""Visit each platform in a saved browser profile and report whether it shows a signed-in page (8 Nov 2026)."""
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output" / "debug" / "logins"
SIGNED_OUT = re.compile(r"\b(sign ?in|log ?in|sign ?up|create (an )?account|get started|continue with google)\b", re.I)


def main() -> None:
    profile, urls = sys.argv[1], [l.split(" | ") for l in open(sys.argv[2]) if l.startswith("http")]
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / "state" / profile), headless=False, viewport={"width": 1366, "height": 860},
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            ignore_default_args=["--enable-automation"])
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        for url, name in ((u.strip(), n.strip()) for u, n in urls):
            try:
                pg.goto(url, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(6000)
                body = pg.inner_text("body")[:3000]
                slug = re.sub(r"\W+", "-", name.lower()).strip("-")
                pg.screenshot(path=str(OUT / f"{slug}.png"))
                hits = len(SIGNED_OUT.findall(body))
                print(f"{name} | {pg.url[:90]} | signed-out words: {hits} | {' '.join(body.split())[:160]}", flush=True)
            except Exception as e:
                print(f"{name} | ERROR {str(e)[:80]}", flush=True)
        ctx.close()


if __name__ == "__main__":
    main()
