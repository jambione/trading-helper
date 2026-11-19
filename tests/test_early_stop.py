import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import early_stop as S  # noqa: E402


def _rows(mean, days=10, per=20, spread=3.0):
    out = []
    for d in range(days):
        for k in range(per):
            out.append({"day": f"2026-05-{d + 1:02d}", "net": mean + (k % 5 - 2) * spread + (d % 3 - 1) * 0.5})
    return out


def test_screen_days_quarter_min_five():
    days = [f"d{i:03d}" for i in range(100)]
    assert S.screen_days(days) == days[:25]
    assert len(S.screen_days(days[:8])) == 5


def test_stops_only_clear_failures():
    assert S.screen(_rows(-6.0))[0] == "STOP"                 # the usual -6 bp desk result
    assert S.screen(_rows(1.0))[0] == "CONTINUE"              # above pass - 2
    assert S.screen(_rows(-6.0)[:20])[0] == "CONTINUE"        # too small to judge
    noisy = [{"day": f"2026-05-{d + 1:02d}", "net": (-30.0 if d % 2 else 25.0)} for d in range(10) for _ in range(10)]
    assert S.screen(noisy)[0] == "CONTINUE"                   # mean -2.5 but t not decisive


def test_gross_screen_skips_only_hopeless_cells():
    from early_stop import gross_screen
    days = [f"d{i:02d}" for i in range(20)]
    bad = [{"day": d, "gross": -5.0 + (i % 3)} for d in days for i in range(10)]
    assert gross_screen(bad, pass_mean=2.0)[0] == "SKIP_QUOTES"
    good = [{"day": d, "gross": 4.0 + (i % 3)} for d in days for i in range(10)]
    assert gross_screen(good, pass_mean=2.0)[0] == "QUOTE"
    noisy = [{"day": d, "gross": (60.0 if j % 2 else -60.0) + (-1 if j < 10 else 0)} for j, d in enumerate(days) for _ in range(10)]
    assert gross_screen(noisy, pass_mean=2.0)[0] == "QUOTE"       # wide uncertainty -> still quoted
    assert gross_screen(bad[:50], pass_mean=2.0)[0] == "QUOTE"    # too few rows
