"""Send outreach emails through Gmail's web UI in the bot's signed-in Chrome profile (Ashu's Gmail), for when the
Gmail connector is down and GMAIL_APP_PASSWORD is not set (22 Oct 2026: "use my gmail, never Apple Mail").

    .venv/bin/python tools/gmail_web_send.py output/outreach/2026-10-22            # every .eml in the folder
    .venv/bin/python tools/gmail_web_send.py path/to/one.eml --dry-run             # compose only, don't press Send

Each .eml's To / Subject / plain-text body and its PDF attachment are used as they are. A sent message is recorded in
the folder's sent_web.json so a rerun never sends it twice.
"""
from __future__ import annotations

import email
import json
import sys
import tempfile
import time
import urllib.parse
from email import policy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from naukri_bot.browser import Browser  # noqa: E402
from naukri_bot.config import Settings  # noqa: E402


def load(eml: Path) -> dict:
    m = email.message_from_file(open(eml), policy=policy.default)
    body, att = "", None
    for part in m.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            body = part.get_content()
        elif part.get_filename():
            att = (part.get_filename(), part.get_payload(decode=True))
    return {"to": str(m["To"]), "subject": str(m["Subject"]), "body": body.strip(), "att": att}


def send(pg, msg: dict, dry: bool) -> bool:
    url = "https://mail.google.com/mail/u/0/?view=cm&fs=1&tf=1&" + urllib.parse.urlencode(
        {"to": msg["to"], "su": msg["subject"], "body": msg["body"]})
    pg.goto(url, wait_until="domcontentloaded", timeout=90000)
    pg.locator("div[aria-label='Message Body'], div[role='textbox'][aria-label*='Body']").first.wait_for(timeout=30000)
    time.sleep(2)
    if msg["att"]:
        name, data = msg["att"]
        tmp = Path(tempfile.mkdtemp()) / name
        tmp.write_bytes(data)
        pg.locator("input[type=file][name='Filedata']").first.set_input_files(str(tmp))
        for _ in range(30):                                  # wait for the upload chip to finish
            time.sleep(1)
            if pg.get_by_text(name, exact=False).count() and not pg.locator("[aria-label*='Uploading']").count():
                break
    if dry:
        print("  composed (dry run, not sent)")
        return False
    pg.locator("div[role='button'][aria-label^='Send'], div[role='button'][data-tooltip^='Send']").first.click()
    for _ in range(20):
        time.sleep(1)
        if pg.get_by_text("Message sent", exact=False).count():
            return True
    return False


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    target = Path(args[0])
    emls = sorted(target.glob("*.eml")) if target.is_dir() else [target]
    log_path = (target if target.is_dir() else target.parent) / "sent_web.json"
    sent = json.loads(log_path.read_text()) if log_path.exists() else {}
    todo = [e for e in emls if e.name not in sent]
    print(f"{len(todo)} to send ({len(emls) - len(todo)} already sent)")
    with Browser(Settings()) as b:
        for e in todo:
            msg = load(e)
            print(f"-> {msg['to']} | {msg['subject']}")
            ok = send(b.page, msg, dry)
            print("   sent" if ok else "   NOT confirmed")
            if ok:
                sent[e.name] = {"to": msg["to"], "subject": msg["subject"], "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                log_path.write_text(json.dumps(sent, indent=1))
                # the outreach step skips anyone in this log, so the same recruiter is never emailed twice
                gp = Path(__file__).resolve().parent.parent / "state" / "outreach_sent.json"
                g = json.loads(gp.read_text()) if gp.exists() else {}
                g[msg["to"].lower()] = {"at": sent[e.name]["at"], "title": msg["subject"], "company": "", "source": "gmail web"}
                gp.write_text(json.dumps(g, indent=1))
            time.sleep(8)                                    # human pace between sends
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
