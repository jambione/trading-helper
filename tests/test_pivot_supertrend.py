import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pivot_supertrend as PS


def zig(n, base=100.0, step=0.05, amp=0.3, drift=0.0):
    """A wavy series with pivots every few bars; drift per bar."""
    out = []
    for i in range(n):
        c = base + drift * i + (amp if (i // 3) % 2 == 0 else -amp) + step * (i % 3)
        out.append((i * 60.0, c, c + 0.05, c - 0.05, c))
    return out


def test_point_in_time_no_lookahead():
    b = zig(80, drift=0.02)
    full = PS.supertrend(b)
    for cut in (30, 45, 60):
        part = PS.supertrend(b[:cut])
        assert part[-1] == full[cut - 1]


def test_uptrend_stays_long_and_line_trails_below_price():
    b = zig(80, drift=0.05)
    st = PS.supertrend(b)
    tail = [s for s in st[-20:] if s["trend"] is not None]
    assert tail and all(s["trend"] == 1 for s in tail)
    assert all(s["tup"] < b[-20 + k][4] for k, s in enumerate(st[-20:]))


def test_sharp_drop_flips_to_minus_one_and_exit_due():
    b = zig(60, drift=0.05)
    last = b[-1][4]
    for k in range(1, 8):
        c = last - 0.6 * k
        b.append((b[-1][0] + 60, c + 0.1, c + 0.15, c - 0.05, c))
    st = PS.supertrend(b)
    assert st[-1]["trend"] == -1
    due, info = PS.exit_due(b, now=b[-1][0] + 60)
    assert due is True


def test_exit_due_uses_completed_bars_only_and_needs_30():
    b = zig(25)
    assert PS.exit_due(b, now=b[-1][0] + 60)[0] is None
    b = zig(60, drift=0.05)
    due, info = PS.exit_due(b, now=b[-1][0] + 30)       # last bar still forming
    assert info["bars"] == 59


def test_atr_is_wilder_seeded_with_simple_mean():
    b = [(i * 60.0, 100, 101, 99, 100) for i in range(12)]   # TR = 2 every bar
    st = PS.supertrend(b)
    assert st[9]["atr"] == 2.0 and abs(st[11]["atr"] - 2.0) < 1e-12
