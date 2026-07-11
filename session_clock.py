"""Shared trading-session clock — one definition of the day's windows.

Used by tools/morning_funnel.py (banner + guidance) and both monitors
(webull-l2/l2_signal.py, tv-monitor/tv_signal.py) so "prime time" means
the same thing everywhere. Tuned for one full-account round trip per
day on a cash account: find the trade early, be done by lunch.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# (start-min, end-min, label, guidance, rich color) in ET minutes-of-day
WINDOWS = [
    (4 * 60, 570, "PRE-MARKET",
     "build the list — gaps and premarket volume are forming", "yellow"),
    (570, 585, "OPENING RANGE",
     "watch, don't chase — spreads widest, most fakeouts", "dark_orange"),
    (585, 630, "PRIME WINDOW",
     "the day's entry window — take the A setup here", "green"),
    (630, 690, "LATE MORNING",
     "volume fading — A+ setups only", "yellow"),
    (690, 840, "MIDDAY CHOP",
     "stand aside — the worst hours of the day", "red"),
    (840, 900, "AFTERNOON",
     "second-tier window — only if still flat on the day", "yellow"),
    (900, 960, "POWER HOUR",
     "volume returns, but a 3pm entry rushes the exit", "dark_orange"),
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


def session_line(now_et: datetime | None = None) -> str:
    """One-line rich markup for monitor headers."""
    now_et = now_et or datetime.now(ET)
    label, guide, color = session_window(now_et)
    return (f"[{color}]{now_et.strftime('%H:%M')} ET  "
            f"[bold]{label}[/bold] — {guide}[/{color}]")
