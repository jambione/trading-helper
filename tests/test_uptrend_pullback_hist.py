"""uptrend_pullback_hist: the fire condition, controls (d, u), inverse exit, verdict labels."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import uptrend_pullback_hist as U  # noqa: E402


def test_condition_needs_uptrend_dip_and_turn_near_the_low():
    slow = {m: -10.0 for m in range(590, 610)}
    fast = {600: -70.0, 601: -85.0, 602: -90.0, 603: -88.0, 604: -80.0}
    assert U.cond(fast, slow, 604)                          # dip -90 in window, turning up from -88 (<= -60)
    flat = {**fast, 604: -88.0}
    assert not U.cond(flat, slow, 604)                      # not turning up
    assert not U.cond(fast, {m: -40.0 for m in range(590, 610)}, 604)        # slow not high
    gap = dict(fast)
    gap.pop(601)
    assert not U.cond(gap, slow, 604)                       # a missing minute in t-4..t
    assert U.cond(fast, {m: -60.0 for m in range(590, 610)}, 604, U.RULES["no_uptrend_slow_le_-50"])


def test_d_window_and_u_control():
    sip = {m: (10.0, 10.0) for m in range(570, 960)}
    for _ in range(20):
        c = U.d_minute(sip, "A", "2026-06-01", 600)
        assert 604 <= c <= 663
    slow = {m: -10.0 for m in range(570, 960)}
    fast = {m: -50.0 for m in range(570, 960)}
    u = U.u_minute(fast, slow, sip, "A", "2026-06-01", 600)
    assert u is not None and not (575 <= u <= 603) and (u + 1) // 60 == 601 // 60
    assert U.u_minute(fast, {m: -60.0 for m in range(570, 960)}, sip, "A", "2026-06-01", 600) is None


def test_inverse_exit_sells_on_the_turn_down_from_the_top():
    sip = {m: (100.0, 100.0 + (m - 600) * 0.1) for m in range(595, 640)}
    fast = {m: -50.0 for m in range(595, 640)}
    fast.update({605: -15.0, 606: -25.0})
    g = U.inverse_exit(fast, sip, 600)
    assert abs(g - ((100.0 + 0.6) / 100.0 - 1) * 1e4) < 1e-6   # exit at the close of minute 606


def _events(lift=8.0, days=84, n=12, seed=4):
    rnd = random.Random(seed)
    out = []
    for k in range(days):
        day = f"2026-{5 + k // 14:02d}-{1 + 2 * (k % 14):02d}"          # May..Oct: both halves powered
        for i in range(n):
            gr = 12.0 + rnd.gauss(0, 4)
            out.append({"rule": "primary", "day": day, "sym": f"S{i}", "t": 600, "gross3": gr,
                        "lift_d": lift + rnd.gauss(0, 4), "lift_x": 3.0, "lift_u": 2.0, "bounce": lift})
    return out


def test_verdict_labels():
    assert U.verdict(_events())["label"] == "PASS"
    assert U.verdict(_events(lift=-2.0))["label"] == "FAIL"
    assert U.verdict(_events(days=10))["label"] == "UNDERPOWERED"
    ev = _events()
    for e in ev:
        e["lift_u"] = None
    assert U.verdict(ev)["label"] != "PASS"                    # no_u > 20% blocks PASS
