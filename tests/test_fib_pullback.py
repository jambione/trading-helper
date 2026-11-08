"""Swing / zone / reaction / bracket / control rules of fib_pullback on hand-built bars. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import fib_pullback as F  # noqa: E402


def b(m, o, h, lo, c, v=1000.0):
    return (m, o, h, lo, c, v)


def path(pull_low=101.4, confirm_close=101.9, after=103.2):
    """L = 100 at 09:35, H = 103 at 10:10 (R 3, GP zone 101.146..101.5, break 100.642), pullback, reaction, run."""
    bars = [b(570 + k, 100.5, 100.8, 100.2, 100.6) for k in range(5)]
    bars.append(b(575, 100.6, 100.7, 100.0, 100.4))                          # L = 100.0
    px = 100.4
    for k in range(576, 610):                                                 # climb to 103 by 10:10
        nx = 100.4 + (k - 575) * 2.6 / 34
        bars.append(b(k, px, min(nx + 0.02, 102.99), px - 0.02, nx))
        px = nx
    bars.append(b(610, px, 103.0, px - 0.02, 102.9))                          # H = 103.0 (10:10)
    bars.append(b(611, 102.9, 102.95, 102.2, 102.3))
    bars.append(b(612, 102.3, 102.35, 101.7, 101.8))
    bars.append(b(613, 101.8, 101.85, pull_low, 101.6))                       # touch (low into the zone)
    bars.append(b(614, 101.6, confirm_close + 0.05, 101.55, confirm_close))   # reaction bar
    for k in range(615, 650):
        if after <= confirm_close:                                            # flat, never bullish
            bars.append(b(k, confirm_close, confirm_close + 0.01, confirm_close - 0.05, confirm_close))
            continue
        nx = min(after, confirm_close + (k - 614) * 0.1)
        bars.append(b(k, nx - 0.05, nx + 0.02, nx - 0.08, nx))
    return bars


def test_gp_signal_and_target_exit():
    bars = path()
    sig = F.detect(bars, "GP")
    assert len(sig) == 1 and sig[0]["dec_min"] == 614 and abs(sig[0]["H"] - 103.0) < 1e-9 and sig[0]["L"] == 100.0
    tr = F.trade(bars, sig[0])
    assert tr["why"] == "target" and abs(tr["gross"] - (103.0 / bars[sig[0]["entry_i"]][1] - 1)) < 1e-12


def test_no_reaction_no_trade_and_break():
    assert F.detect(path(confirm_close=101.58, after=101.58), "GP") == []    # no close above the previous high
    bars = path(pull_low=100.5)                                               # pierces 0.786 by low only
    i = next(k for k, x in enumerate(bars) if x[0] == 613)
    bars[i] = b(613, 101.8, 101.85, 100.5, 100.6)                             # closes below 100.642 -> broken
    assert F.detect(bars, "GP") == []


def test_swing_must_be_two_percent_and_shallow_zone_is_different():
    flat = [b(570 + k, 100.0, 100.5, 99.9, 100.2) for k in range(80)]
    assert F.detect(flat, "GP") == []
    assert F.zone_bounds("SHALLOW") == (0.236, 0.382) and F.zone_bounds("RANDOM_DEPTH", 0.62) == (0.62, 0.738)


def test_simulate_rules():
    bars = [b(600, 100, 100.2, 99.9, 100.1), b(601, 100.1, 101.0, 100.0, 100.9), b(602, 100.9, 101.6, 100.8, 101.5)]
    r = F.simulate(bars, 0, 99.0, 101.5)
    assert r["why"] == "target" and r["exit"] == 101.5                       # needs high >= 101.51; bar 602 has 101.6
    eq = [b(600, 100, 100.2, 99.9, 100.1), b(601, 100.1, 101.5, 100.0, 100.9)] + [b(602 + k, 100.9, 101.0, 100.8, 100.9) for k in range(40)]
    assert F.simulate(eq, 0, 99.0, 101.5)["why"] == "time"                   # an equal high is not a fill
    st = [b(600, 100, 100.2, 99.9, 100.1), b(601, 100.1, 100.2, 98.0, 98.5), b(602, 98.4, 98.6, 98.0, 98.2)]
    s = F.simulate(st, 0, 99.0, 101.5)
    assert s["why"] == "stop" and s["exit"] == 98.4                           # next bar's open
    both = [b(600, 100, 100.2, 99.9, 100.1), b(601, 100.1, 101.6, 98.0, 98.5)]
    assert F.simulate(both, 0, 99.0, 101.5)["why"] == "target"                # target wins a bar that does both
    gap = [b(600, 100, 100.2, 99.9, 100.1), b(610, 100.3, 100.4, 100.2, 100.3)]
    assert F.simulate(gap, 0, 99.0, 101.5)["why"] == "gap"


def test_control_same_hour_and_separation():
    bars = path()
    sig = F.detect(bars, "GP")[0]
    tr = F.trade(bars, sig)
    c = F.control(bars, sig, tr, "X", "2026-06-01", "fib-gp-ctrl")
    assert c is None or isinstance(c["net"], float)
    assert c == F.control(bars, sig, tr, "X", "2026-06-01", "fib-gp-ctrl")   # seeded


def test_overlap_and_stats():
    rows = [{"day": "d", "sym": "A", "entry_min": 600, "exit_min": 610, "net": 0.01, "ctrl": 0.0},
            {"day": "d", "sym": "A", "entry_min": 605, "exit_min": 610, "net": 0.01, "ctrl": 0.0},
            {"day": "d", "sym": "A", "entry_min": 611, "exit_min": 616, "net": 0.01, "ctrl": 0.0}]
    assert [r["entry_min"] for r in F.no_overlap(rows)] == [600, 611]
    assert F.final(["PASS", "PASS"], [0.01, 0.01], [30, 30]) == "PASS"
    assert F.final(["NOT_PASSED", "NOT_PASSED"], [0.0002, 0.0001], [30, 30]) == "FAIL"



def test_break_before_touch_kills_the_swing():
    bars = path()
    i = next(k for k, x in enumerate(bars) if x[0] == 613)
    bars[i] = b(613, 101.8, 101.85, 100.55, 100.6)       # closes below the break (100.642) without being a touch first
    for j, mm in enumerate(range(614, 618)):              # recovery back into the zone, then bullish bars
        bars[i + 1 + j] = b(mm, 100.7, 101.8, 100.7, 101.4 + 0.1 * j)
    assert F.detect(bars, "GP") == []


def test_first_bullish_bar_outside_vwap_means_no_trade():
    bars = path(confirm_close=101.9)
    sig = F.detect(bars, "GP")
    vw = F.vwaps(bars)
    i = next(k for k, x in enumerate(bars) if x[0] == 614)
    if bars[i][4] <= vw[i]:                               # if the first bullish bar is below VWAP, no later bar may confirm
        assert sig == []
    else:
        assert sig and sig[0]["dec_min"] == 614


def test_skips():
    bars = path()
    sig = dict(F.detect(bars, "GP")[0])
    sig["brk"] = 105.0
    assert F.trade(bars, sig) == {"skip": "open_at_or_below_stop"}
    sig = dict(F.detect(bars, "GP")[0])
    e = sig["entry_i"]
    bars2 = list(bars)
    bars2[e] = (bars[e][0] + 9,) + bars[e][1:]
    assert F.trade(bars2, sig) == {"skip": "entry_gap"}


# ---------------- amended_3 controls (11/07): windows proven by making ONLY the minutes under test eligible ----------------
def _sel(good, start=600, n=330, base=20.0):
    """Bars where only the minutes in `good` form a bullish, above-VWAP setup (isolated, so close > previous high holds); every other bar is flat-down."""
    out = []
    for i in range(n):
        m = start + i
        if m in good:
            out.append((m, base, base + 0.22, base - 0.01, base + 0.20, 1000.0))
        else:
            out.append((m, base, base + 0.001, base - 0.012, base - 0.01, 1000.0))
    return out


SIG = {"dec_min": 640, "H": 30.0, "L": 19.0, "brk": 19.0}
TR = {"exit_min": 670, "entry_min": 641, "stop_pct": 0.005, "target_pct": 0.01}


def _ctl(good):
    import fib_pullback as fp
    return fp.control(_sel(good), SIG, TR, "X", "2026-06-01", "t")


def test_secondary_window_only_after_exit_plus_2_through_exit_plus_62():
    assert _ctl({620}) is None            # before the signal
    assert _ctl({650}) is None            # inside the trade
    assert _ctl({671}) is None            # exit + 1: too close
    assert _ctl({733}) is None            # exit + 63: past the window
    assert _ctl({672}) is not None        # exit + 2
    assert _ctl({732}) is not None        # exit + 62
    assert _ctl({620, 650, 733, 680}) is not None     # one eligible minute among ineligible ones is enough


def _xs_bars(good_at_dec):
    return _sel({640, 700} if good_at_dec else {700})


def test_primary_control_needs_a_bullish_above_vwap_bar_AT_the_decision_minute_on_another_name():
    import fib_pullback as fp
    sig, tr = {"dec_min": 640}, {"stop_pct": 0.005, "target_pct": 0.01, "entry_min": 641}
    allb = {"X": _sel({640}), "Y": _sel(set()), "Z": _sel({640})}                  # Y not bullish at 640, Z is
    c = fp.control_xs(allb, "X", sig, tr, "2026-06-01")
    assert c is not None
    assert fp.control_xs({"X": _sel({640}), "Y": _sel(set())}, "X", sig, tr, "2026-06-01") is None          # only a non-setup name -> no control
    assert fp.control_xs({"X": _sel({640})}, "X", sig, tr, "2026-06-01") is None                           # the name itself is never its control
    # eligibility uses the decision bar only: changing Z AFTER the decision minute does not remove it from the pool
    z2 = _sel({640}); z2[-30:] = [(b[0], 20.0, 20.001, 19.99, 19.995, 1000.0) for b in z2[-30:]]
    assert fp.control_xs({"X": _sel({640}), "Z": z2}, "X", sig, tr, "2026-06-01") is not None
    # deterministic, and without replacement (3 eligible names -> the 5-draw average uses each at most once)
    assert fp.control_xs(allb, "X", sig, tr, "2026-06-01") == c


def test_baseline_control_has_no_setup_filter_but_the_same_window():
    import fib_pullback as fp
    bars = _sel(set())                                                             # no bullish setup anywhere
    assert fp.control(bars, SIG, TR, "X", "2026-06-01", "t") is None               # the setup-matched control finds nothing
    assert fp.control_uncond(bars, TR, "X", "2026-06-01") is not None              # the baseline does not need a setup
    assert fp.control_uncond(_sel(set(), n=60), TR, "X", "2026-06-01") is None     # but still only after exit + 2


def test_standard_reading_flags_void_when_the_control_is_far_from_the_baseline():
    import fib_pullback as fp
    rows = lambda ctrl, base: [{"day": f"2026-06-{1 + i // 4:02d}", "sym": f"S{i % 9}", "net": 0.004 + 0.0004 * (i % 5), "gross": 0.0045 + 0.0004 * (i % 5),
                               "ctrl": ctrl, "ctrl2": ctrl, "ctrl_u": base} for i in range(200)]
    near = fp.standard_reading({"A": rows(-0.0006, -0.0006), "B": rows(-0.0006, -0.0006)})
    far = fp.standard_reading({"A": rows(0.0100, -0.0006), "B": rows(0.0100, -0.0006)})
    assert near["VOID"] is False and far["VOID"] is True and far["EDGE"] is False
    assert near["halves"]["A"]["no_control_share"] == 0
