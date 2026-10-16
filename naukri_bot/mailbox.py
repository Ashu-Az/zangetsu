"""Read verification links from the candidate's Gmail over IMAP (GMAIL_ADDRESS + GMAIL_APP_PASSWORD in .env).

Workday tenants (Thomson Reuters, Rockwell, Workday itself, 13 Oct 2026) only let a new account sign in after the
emailed verification link is opened; without it the Sign In page just stays put. Read-only: nothing is sent or
deleted, and only links from messages received after `since` are returned.
"""
from __future__ import annotations

import email
import html
import imaplib
import logging
import os
import re
import time
from email.utils import parsedate_to_datetime

log = logging.getLogger(__name__)
LINK = re.compile(r"https?://[^\s\"'<>]+")


def _creds() -> tuple[str, str] | None:
    addr, pw = os.environ.get("GMAIL_ADDRESS", "").strip(), os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    return (addr, pw) if addr and pw else None


def available() -> bool:
    return _creds() is not None


def _body(msg) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    out = []
    for p in parts:
        if p.get_content_type() in ("text/plain", "text/html"):
            try:
                out.append(p.get_payload(decode=True).decode(p.get_content_charset() or "utf-8", "ignore"))
            except Exception:
                pass
    return html.unescape("\n".join(out))


def find_link(host_hint: str, since: float, link_pattern: str = r"activat|verif|confirm",
              wait_s: int = 120, poll_s: int = 10) -> str | None:
    """First link whose URL contains `host_hint` (e.g. 'thomsonreuters.wd5.myworkdayjobs.com') and matches
    `link_pattern`, in a message received after `since` (epoch seconds). Polls for up to `wait_s`."""
    creds = _creds()
    if not creds:
        return None
    deadline = time.time() + wait_s
    while True:
        try:
            with imaplib.IMAP4_SSL("imap.gmail.com") as M:
                M.login(*creds)
                M.select('"[Gmail]/All Mail"', readonly=True)
                day = time.strftime("%d-%b-%Y", time.localtime(since - 86400))
                _, ids = M.search(None, f'(SINCE {day})')
                for mid in reversed(ids[0].split()[-40:]):
                    _, data = M.fetch(mid, "(RFC822)")
                    msg = email.message_from_bytes(data[0][1])
                    try:
                        if parsedate_to_datetime(msg["Date"]).timestamp() < since - 60:
                            continue
                    except Exception:
                        pass
                    for url in LINK.findall(_body(msg)):
                        url = url.rstrip(").,;")
                        if host_hint.lower() in url.lower() and re.search(link_pattern, url, re.I):
                            return url
        except (imaplib.IMAP4.error, OSError) as e:
            log.warning("mailbox: IMAP read failed (%s)", str(e)[:80])
            return None
        if time.time() >= deadline:
            return None
        time.sleep(poll_s)
