"""Red-exit timing and control choice. No market data."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import rsi2_red_exit as R  # noqa: E402


def test_red_exits_next_minute_only_before_the_desk_exit():
    rsi = {600: 50.0, 601: 92.0, 602: 95.0}
    assert R.red_exit_minute(rsi, 600, 610) == 602        # bar 601 > 90 -> sell 602's open
    assert R.red_exit_minute(rsi, 600, 602) is None       # 602 would be the desk's own exit minute
    assert R.red_exit_minute({599: 99.0}, 600, 610) is None   # bars before the entry do not count


def test_strict_needs_the_panel_mirror():
    rsi = {601: 95.0}
    assert R.red_exit_minute(rsi, 600, 610, avg={601: (100.0, 99.0, 99.5)}, strict=True) == 602   # < SMA200, > SMA5
    assert R.red_exit_minute(rsi, 600, 610, avg={601: (99.0, 99.0, 99.5)}, strict=True) is None


def test_control_is_inside_the_hold_and_seeded():
    a = R.control_exit_minute("X", "2026-10-01", 600, 610)
    assert 601 <= a <= 609 and a == R.control_exit_minute("X", "2026-10-01", 600, 610)
    assert R.control_exit_minute("X", "2026-10-01", 600, 601) is None
