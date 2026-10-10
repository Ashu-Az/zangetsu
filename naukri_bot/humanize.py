"""Small helpers that make the browser behave less like a script."""
from __future__ import annotations

import random
import time


def pause(lo: float = 0.8, hi: float = 2.2) -> None:
    time.sleep(random.uniform(lo, hi))


def human_type(page, selector_or_locator, text: str) -> None:
    """Type character by character with a jittered delay (40-140 ms)."""
    loc = page.locator(selector_or_locator) if isinstance(selector_or_locator, str) else selector_or_locator
    loc.click()
    for ch in text:
        page.keyboard.type(ch, delay=random.randint(40, 140))
        if ch in ".,;!?" and random.random() < 0.3:
            time.sleep(random.uniform(0.15, 0.5))


def scroll_like_reader(page, steps: int = 3) -> None:
    for _ in range(steps):
        page.mouse.wheel(0, random.randint(250, 700))
        time.sleep(random.uniform(0.4, 1.1))
    if random.random() < 0.5:
        page.mouse.wheel(0, -random.randint(100, 300))
        time.sleep(random.uniform(0.3, 0.8))



# Steady pacing: a person moves through job pages at a fairly even rhythm (~10 s per page, a few seconds either
# way), not in bursts followed by long breaks. Both page loads and applications share one clock.
_last_action = 0.0


def steady_gap(settings) -> float:
    """Seconds for the next gap: centred between MIN_DELAY and MAX_DELAY with mild jitter."""
    lo, hi = settings.min_delay, settings.max_delay
    mid, spread = (lo + hi) / 2, (hi - lo) / 4
    return min(hi, max(lo, random.gauss(mid, spread)))


def steady_wait(settings) -> float:
    """Wait until a steady gap has passed since the previous page load or application; return seconds slept."""
    global _last_action
    wait = max(0.0, _last_action + steady_gap(settings) - time.monotonic()) if _last_action else 0.0
    if wait:
        time.sleep(wait)
    _last_action = time.monotonic()
    return wait


def between_applies(settings) -> float:
    """The gap between two applications: the same steady rhythm as page loads (no bursts, no long breaks)."""
    return steady_wait(settings)


def pace_page(page, settings) -> None:
    """Make page.goto on this page wait for the steady rhythm first."""
    if getattr(page, "_paced", False):
        return
    raw = page.goto

    def goto(url, *a, **kw):
        steady_wait(settings)
        return raw(url, *a, **kw)

    page.goto = goto
    page._paced = True
