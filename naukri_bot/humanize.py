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
