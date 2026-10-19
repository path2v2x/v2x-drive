"""How fast the world really ticks, for the driving HUD's "Server FPS" line.

``tick_loop`` marks every successful ``world.tick()``; ``fps()`` is a smoothed
ticks-per-second that drops to 0 when the loop has stalled for a while.
"""

from __future__ import annotations

import time
from collections import deque


class TickStats:
    def __init__(self, window_s: float = 1.0, now_fn=time.monotonic):
        self._now = now_fn
        self._window = float(window_s)
        self._marks: deque = deque()
        self._ema = 0.0
        self.total = 0

    def mark(self) -> None:
        now = self._now()
        self._marks.append(now)
        self.total += 1
        cutoff = now - self._window
        while self._marks and self._marks[0] <= cutoff:
            self._marks.popleft()
        instant = len(self._marks) / self._window
        self._ema = instant if self._ema == 0.0 else 0.8 * self._ema + 0.2 * instant

    def fps(self) -> float:
        if not self._marks or self._now() - self._marks[-1] > 2.0 * self._window:
            return 0.0
        return round(self._ema, 1)

    def status(self) -> dict:
        return {"fps": self.fps(), "ticks": self.total}


TICK_STATS = TickStats()
