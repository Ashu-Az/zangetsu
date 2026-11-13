"""Apply to one Naukri job by URL with the bot's normal flow (rules, saved answers, model, assist mode).

Use it when a recruiter emails "please apply here: <naukri link>". It first checks the job page: if Naukri already
shows "Applied", it says so and does nothing.
    .venv/bin/python tools/naukri_apply_url.py "https://www.naukri.com/job-listings-...-291026031403"
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
for noisy in ("httpx", "httpcore"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from naukri_bot.answers_store import AnswerCache  # noqa: E402
from naukri_bot.browser import Browser  # noqa: E402
from naukri_bot.config import Settings  # noqa: E402
from naukri_bot.job_page import detect_apply_mode, scrape_job  # noqa: E402
from naukri_bot.main import handle_naukri_apply, make_llm  # noqa: E402
from naukri_bot.models import ApplyMode  # noqa: E402
from naukri_bot.tracker import Tracker  # noqa: E402


def main(url: str) -> int:
    settings, tracker, cache = Settings(), Tracker(), AnswerCache()
    try:
        llm = make_llm(settings)
    except Exception as e:                      # no model: rules + saved answers only, assist mode for the rest
        print("no model:", e)
        llm = None
    with Browser(settings) as b:
        b.ensure_logged_in()
        job = scrape_job(b, url)
        print(f"{job.title} | {job.company} | {job.experience} | {job.location}")
        mode = detect_apply_mode(b)
        if mode == ApplyMode.ALREADY_APPLIED:
            print("already applied")
            return 0
        if mode != ApplyMode.NAUKRI:
            print(f"not a Naukri apply ({mode}); open it by hand")
            return 1
        ok = handle_naukri_apply(b, llm, cache, job, tracker, dry_run=False, assist=True)
        print("applied" if ok else "not confirmed")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
