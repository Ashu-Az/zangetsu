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


_since_break = 0
_next_break = random.randint(4, 7)


def between_applies(settings) -> float:
    """Wait like a person between two applications and return the seconds slept.

    Most gaps fall between MIN_DELAY and MAX_DELAY, skewed toward the short end (people skim fast, then
    sometimes linger on a listing); about one gap in eight is a longer read of up to 3x MAX_DELAY; and every
    4-7 applications there is a 2-6 minute break (BREAK_MIN/BREAK_MAX seconds), the way someone steps away."""
    global _since_break, _next_break
    lo, hi = settings.min_delay, settings.max_delay
    gap = lo + (hi - lo) * random.betavariate(1.6, 3.0)
    if random.random() < 0.12:
        gap = random.uniform(hi, hi * 3)
    _since_break += 1
    if _since_break >= _next_break:
        gap += random.uniform(settings.break_min, settings.break_max)
        _since_break, _next_break = 0, random.randint(4, 7)
    time.sleep(gap)
    return gap
