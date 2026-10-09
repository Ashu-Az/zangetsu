"""Per-site cooldown after a throttle warning, so no run touches that site until it expires."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from .config import STATE_DIR

PATH = STATE_DIR / "cooldowns.json"


def _load() -> dict:
    try:
        return json.loads(PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def pause(site: str, hours: float, reason: str) -> None:
    data = _load()
    data[site] = {"until": (datetime.now() + timedelta(hours=hours)).isoformat(timespec="minutes"), "reason": reason}
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(data, indent=2))


def paused_until(site: str) -> str | None:
    """ISO time the site is paused until, or None if it may be used now."""
    e = _load().get(site)
    if e and datetime.fromisoformat(e["until"]) > datetime.now():
        return f"{e['until']} ({e['reason']})"
    return None
