"""Human-like pacing.

The official client is a phone app that records one answer at a time. Firing
every request back to back looks nothing like that and is the fastest way to
get throttled by the server. FastETS therefore paces itself:

* every network call is separated by a small jittered delay
* every answer waits a realistic reading time before it is uploaded
  (based on the word count of the text that has to be spoken)
* every 大题 gets an extra pause in between

All waits are interruptible so the user can stop a run immediately.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Optional

from .score import words_of


class Pacer:
    """Interruptible delay scheduler."""

    def __init__(
        self,
        enabled: bool = True,
        request_delay_ms: int = 400,
        jitter_ratio: float = 0.4,
        reading_wpm: int = 150,
        min_reading_delay_ms: int = 800,
        max_reading_delay_ms: int = 9000,
        section_pause_ms: int = 1500,
    ) -> None:
        self.enabled = enabled
        self.request_delay_ms = max(0, int(request_delay_ms))
        self.jitter_ratio = max(0.0, min(float(jitter_ratio), 2.0))
        self.reading_wpm = max(0, int(reading_wpm))
        self.min_reading_delay_ms = max(0, int(min_reading_delay_ms))
        self.max_reading_delay_ms = max(
            self.min_reading_delay_ms, int(max_reading_delay_ms)
        )
        self.section_pause_ms = max(0, int(section_pause_ms))
        self._stop = threading.Event()

    # -- control -------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def reset(self) -> None:
        self._stop.clear()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def _sleep(self, seconds: float) -> bool:
        """Sleep, returning False when the run was interrupted."""
        if seconds <= 0:
            return not self.stopped
        return not self._stop.wait(seconds)

    # -- delays --------------------------------------------------------------

    def _jitter(self, base_ms: int) -> float:
        if base_ms <= 0:
            return 0.0
        spread = base_ms * self.jitter_ratio
        offset = random.uniform(-spread, spread)
        return max(0.0, base_ms + offset) / 1000.0

    def request(self) -> bool:
        """Short jittered pause before a plain API call."""
        if not self.enabled:
            return not self.stopped
        return self._sleep(self._jitter(self.request_delay_ms))

    def reading(self, text: str) -> bool:
        """Wait roughly as long as a human would need to read ``text``."""
        if not self.enabled:
            return not self.stopped
        if self.reading_wpm <= 0:
            return self.request()
        words = max(len(words_of(text or "")), 1)
        # Reading time plus the time it takes to open the mouth and start.
        seconds = words / (self.reading_wpm / 60.0)
        delay_ms = int(seconds * 1000) + random.randint(250, 900)
        delay_ms = max(self.min_reading_delay_ms, min(delay_ms, self.max_reading_delay_ms))
        return self._sleep(delay_ms / 1000.0)

    def upload(self) -> bool:
        """Pause between the three OSS uploads of one answer."""
        if not self.enabled:
            return not self.stopped
        return self._sleep(self._jitter(max(350, self.request_delay_ms)))

    def section(self) -> bool:
        """Longer pause between two 大题."""
        if not self.enabled:
            return not self.stopped
        return self._sleep(self._jitter(self.section_pause_ms))


def pacer_from_settings(settings: dict) -> Pacer:
    return Pacer(
        enabled=bool(settings.get("pace_enabled", True)),
        request_delay_ms=int(settings.get("request_delay_ms", 400)),
        jitter_ratio=float(settings.get("request_jitter_ratio", 0.4)),
        reading_wpm=int(settings.get("reading_wpm", 150)),
        min_reading_delay_ms=int(settings.get("min_reading_delay_ms", 800)),
        max_reading_delay_ms=int(settings.get("max_reading_delay_ms", 9000)),
        section_pause_ms=int(settings.get("section_pause_ms", 1500)),
    )
