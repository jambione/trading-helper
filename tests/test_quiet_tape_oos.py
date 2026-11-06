"""quiet_tape_oos: ratio math, held-out pass needs all four conditions, planted effect passes, null fails."""
from __future__ import annotations

import random
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import quiet_tape_oos as q  # noqa: E402

ET = ZoneInfo("America/New_York")


def _fill(h, m):
    return datetime(2026, 10, 20, h, m, 20, tzinfo=ET).timestamp()


def test_ratio_uses_last_five_closed_minutes_over_session_pace():
    ft = _fill(10, 30)
    cut = ft - (ft % 60)
    open_ts = datetime(2026, 10, 20, 9, 30, tzinfo=ET).timestamp()
    bars = [(open_ts + 60 * i, 1, 1, 1, 1, 1000.0) for i in range(60) if open_ts + 60 * i < cut]       # steady 1000/min
    assert abs(q.ratio(bars, ft) - 1.0) < 1e-9
    quiet = [b if b[0] < cut - 300 else (b[0], 1, 1, 1, 1, 100.0) for b in bars]                          # last 5 minutes at 100/min
    assert 0.10 < q.ratio(quiet, ft) < 0.12
    assert q.ratio(bars, _fill(9, 35)) is None and q.ratio([], ft) is None                               # < 10 session minutes / no bars


def _rows(n, planted, seed):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        d = f"2026-10-{1 + i * 28 // n:02d}"
        quiet = rng.random() < 0.2
        bp = rng.gauss(-14 if (quiet and planted) else -5, 12)
        rows.append({"d": d, "bp": bp, "ng": bp < -10, "ratio": 0.1 if quiet else 0.8})
    return rows


def test_planted_effect_passes_and_null_fails():
    assert q.evaluate(_rows(900, True, 1))["pass"]
    assert not q.evaluate(_rows(900, False, 2))["pass"]


def test_too_few_quiet_fills_never_pass():
    rows = _rows(900, True, 3)
    for r in rows:
        r["ratio"] = 0.8
    for r in rows[:10]:
        r["ratio"], r["bp"] = 0.1, -40.0
    assert not q.evaluate(rows)["pass"]
