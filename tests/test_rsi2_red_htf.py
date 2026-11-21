import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import rsi2_red_htf as H  # noqa: E402

ET = ZoneInfo("America/New_York")


def _t(day, hm):
    return datetime.fromisoformat(f"{day}T{hm}").replace(tzinfo=ET)


def test_buckets_last_close_and_missing_minutes():
    d = "2026-05-01"
    pts = [(_t(d, "10:00"), 1.0), (_t(d, "10:02"), 2.0), (_t(d, "10:06"), 3.0), (_t(d, "10:20"), 4.0)]
    b = H.buckets(pts, 5)
    assert [x[1] for x in b] == [2.0, 3.0, 4.0]                  # 10:00-04 closes at its last minute (10:02); 10:10/10:15 do not exist
    assert H.adjacent(b[0][0], b[1][0]) and not H.adjacent(b[1][0], b[2][0])


def test_rth_minute_and_clock_end():
    d = datetime.fromisoformat("2026-05-01").date()
    key = (d, (10 * 60) // 5)                                       # 10:00 bucket
    assert H.rth_minute(d, key, 5) == 30                            # 10:00 is RTH minute 30; known closed at minute 35


def test_c_enters_at_clock_end_even_if_last_minute_missing():
    day = "2026-05-01"
    rng = np.random.default_rng(3)
    hist = []
    t0 = _t("2026-04-30", "04:00")
    px = 30.0
    for i in range(16 * 60 * 2):                                    # two sessions of all-hours minutes, drifting down
        px += rng.normal(-0.002, 0.02)
        hist.append((t0 + timedelta(minutes=i), px))
    c = px + np.cumsum(rng.normal(0, 0.02, 390))
    c[35:40] = c[34] + np.arange(1, 6) * 0.08                      # a sharp 5-minute push 10:05-10:09
    c[39] = np.nan                                                  # its last minute is missing
    o, h, l = c - 0.01, c + 0.02, c - 0.02
    sigs = H.name_signals(day, hist, o, h, l, c, 30.0, 5) or []
    for s in sigs:
        if s["rule"] in ("C", "P2"):
            assert s["ent"] == s["b0"] + 5                          # never before the bucket's clock end
        if s["rule"] == "S":
            assert s["b0"] <= s["ent"] < s["b0"] + 5                # filled inside its own bucket
