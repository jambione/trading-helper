from __future__ import annotations
"""Shared trading-session clock — one definition of the day's windows.

Used by tools/morning_funnel.py (banner + guidance) and both monitors
(tv-monitor/tv_signal.py) so the windows mean
the same thing everywhere. Tuned for a cash account split into thirds:
three round trips per day, entered at 7:00, 8:30, and 9:30 ET, with a
funnel re-rank right before each shot.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# (start-min, label) of each tranche entry in ET minutes-of-day
SHOTS = [
    (7 * 60, "TRANCHE 1"),
    (8 * 60 + 30, "TRANCHE 2"),
    (9 * 60 + 30, "TRANCHE 3"),
]

# (start-min, end-min, label, guidance, rich color) in ET minutes-of-day
WINDOWS = [
    (4 * 60, 400, "PRE-MARKET SCAN",
     "gappers declaring themselves — build the list", "yellow"),
    (400, 420, "T1 FUNNEL",
     "final re-rank — name tranche 1's battlefield", "dark_orange"),
    (420, 500, "TRANCHE 1",
     "first third in play — thin tape, limit orders only", "green"),
    (500, 510, "T2 FUNNEL",
     "re-rank — 8:30 news and data can flip the list", "dark_orange"),
    (510, 565, "TRANCHE 2",
     "second third — volume building into the open", "green"),
    (565, 570, "T3 FUNNEL",
     "last re-rank before the bell", "dark_orange"),
    (570, 630, "TRANCHE 3",
     "final third — fastest tape, widest spreads in the first minutes", "green"),
    (630, 720, "WIND-DOWN",
     "no new entries — work the exits", "yellow"),
    (720, 960, "EXITS ONLY",
     "chop hours — be flat or managing, never entering", "red"),
]


def session_window(now_et: datetime | None = None) -> tuple[str, str, str]:
    """Return (label, guidance, color) for the current ET session window."""
    now_et = now_et or datetime.now(ET)
    mins = now_et.hour * 60 + now_et.minute
    if now_et.weekday() < 5:
        for start, end, label, guide, color in WINDOWS:
            if start <= mins < end:
                return label, guide, color
    return "CLOSED", "market closed", "dim"


def next_shot(now_et: datetime | None = None) -> tuple[str, int] | None:
    """Return (label, minutes-until) for the next tranche entry today,
    or None once the last shot has passed (or on weekends)."""
    now_et = now_et or datetime.now(ET)
    if now_et.weekday() >= 5:
        return None
    mins = now_et.hour * 60 + now_et.minute
    for start, label in SHOTS:
        if mins < start:
            return label, start - mins
    return None


def session_line(now_et: datetime | None = None) -> str:
    """One-line rich markup for monitor headers."""
    now_et = now_et or datetime.now(ET)
    label, guide, color = session_window(now_et)
    return (f"[{color}]{now_et.strftime('%H:%M')} ET  "
            f"[bold]{label}[/bold] — {guide}[/{color}]")
