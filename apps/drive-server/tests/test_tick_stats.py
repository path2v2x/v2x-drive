"""Tick statistics behind the HUD's Server FPS line."""

from digital_twin_bridge.tick_stats import TickStats


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_fps_settles_on_the_tick_rate_and_drops_when_the_loop_stalls():
    clock = Clock()
    stats = TickStats(window_s=1.0, now_fn=clock)
    assert stats.fps() == 0.0
    for _ in range(60):  # three seconds at 20 Hz
        clock.t += 0.05
        stats.mark()
    assert 19.0 <= stats.fps() <= 21.0 and stats.total == 60
    for _ in range(20):  # a second at 10 Hz pulls the smoothed value down
        clock.t += 0.1
        stats.mark()
    assert 10.0 <= stats.fps() < 16.0
    clock.t += 3.0  # nothing ticked for a while: the HUD must not show a stale rate
    assert stats.fps() == 0.0
    assert stats.status() == {"fps": 0.0, "ticks": 80}
