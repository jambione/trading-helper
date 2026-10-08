"""Tests for tools/studies/structure_pullback.py (setups S2-S7, exits, controls, verdicts). Synthetic, no network."""
from __future__ import annotations

import collections
import random

import pytest

from _hist_synth import (DAY, T, FakeData, FakeStudyMarket, bar, bars60, flat, from5, from15, price_at, weekdays)
import bars_structure as BS
import name_history as NH
import structure_pullback as SP

D1, D2 = "2026-09-11", "2026-09-14"
FLAT_DAY = [(100.0, 100.0, 100.0, 100.0)] * 7


def ctx_for(b1, prior60, prior_low=None, prior_close=100.0, sessions5=(D1, D2)):
    series = BS.concat_sessions(prior60 + [BS.aggregate(b1, DAY, 60)])
    return SP.Ctx("XYZ", DAY, b1, series, list(sessions5), prior_low, prior_close,
                  lambda t: (price_at(b1, t), 0.0002))


def fill15(spec, px):
    return spec + [(px, px, px, px)] * (26 - len(spec))


# ------------------------------------------------------------------ S2
def _hl60(hl):
    return [((h + l) / 2, h, l, (h + l) / 2) for h, l in hl]


S2_60 = [bars60(D1, _hl60([(103, 101), (102.5, 100.5), (102, 100), (103, 100.8), (104, 101), (105, 101.5),
                           (104.5, 102.5)])),
         bars60(D2, _hl60([(104.2, 102.4), (103.5, 102.0), (105, 102.6), (106, 103), (107, 104), (106.5, 104.2),
                           (106, 104.5)]))]
S2_15 = [(104, 104.5, 103.5, 104), (104, 104.2, 103, 103.2), (103.2, 103.4, 102.05, 103.0),
         (103.0, 103.2, 102.3, 103.1), (103.1, 104.1, 103.05, 104.0)]


def test_S2_fires():
    c = collections.Counter()
    sig = SP.scan_S2(ctx_for(from15(fill15(S2_15, 104.0)), S2_60), c)
    assert sig and sig["t"] == T(10, 45) and sig["L"] == 102.0
    assert abs(sig["stop"] - 102.04) < 1e-9 and sig["n_rej"] == 2 and sig["depth"] == "within_0.1pct"


def test_S2_needs_approach_from_above():
    s = list(S2_15)
    s[1] = (104, 104.2, 102.05, 103.2)                 # previous bar already at the level
    assert SP.scan_S2(ctx_for(from15(fill15(s, 104.0)), S2_60), collections.Counter()) is None


def test_S2_cancel_below_L():
    s = list(S2_15)
    s[3] = (103.0, 103.2, 100.5, 100.9)                # closes < L x 0.99
    c = collections.Counter()
    assert SP.scan_S2(ctx_for(from15(fill15(s, 104.0)), S2_60), c) is None and c["S2_cancel"] == 1


def test_S2_needs_two_rejections():
    s = list(S2_15)
    s[3] = (103.0, 103.5, 102.9, 103.4)                # not a rejection bar
    assert SP.scan_S2(ctx_for(from15(fill15(s, 104.0)), S2_60), collections.Counter()) is None


def test_S2_R_bound_skip_ends_candidate():
    s = list(S2_15)
    s[3] = (103.0, 103.2, 101.5, 103.1)                # rejection, but the stop is now > 2% away
    c = collections.Counter()
    assert SP.scan_S2(ctx_for(from15(fill15(s, 104.0)), S2_60), c) is None and c["S2_skip_R"] == 1


def test_S2_no_uptrend_no_touch():
    down = [bars60(D1, _hl60([(107, 104)] * 7)), bars60(D2, _hl60([(106, 103)] * 7))]
    assert SP.scan_S2(ctx_for(from15(fill15(S2_15, 104.0)), down), collections.Counter()) is None


# ------------------------------------------------------------------ S3
S3_15 = [(100.2, 100.5, 100.0, 100.3), (100.3, 100.4, 100.1, 100.2), (100.2, 100.45, 100.05, 100.3),
         (100.3, 100.5, 100.1, 100.3), (100.4, 101.3, 100.35, 101.2), (101.0, 101.1, 100.55, 101.0)]
FLAT60 = [bars60(D1, FLAT_DAY), bars60(D2, FLAT_DAY)]


def test_S3_fires():
    sig = SP.scan_S3(ctx_for(from15(fill15(S3_15, 101.0)), FLAT60), collections.Counter())
    assert sig and sig["t"] == T(11, 0) and sig["M"] == 100.5 and abs(sig["stop"] - 100.54) < 1e-9


def test_S3_wick_only_break_is_not_a_break():
    s = list(S3_15)
    s[4] = (100.4, 101.3, 100.35, 100.55)
    c = collections.Counter()
    assert SP.scan_S3(ctx_for(from15(fill15(s, 101.0)), FLAT60), c) is None and c["S3_break"] == 0


def test_S3_close_below_M_cancels():
    s = list(S3_15)
    s[5] = (101.2, 101.25, 100.3, 100.4)
    c = collections.Counter()
    assert SP.scan_S3(ctx_for(from15(fill15(s, 100.4)), FLAT60), c) is None and c["S3_cancel"] == 1


def test_S3_retest_after_six_bars_expires():
    s = S3_15[:5] + [(101.2, 101.4, 101.1, 101.3)] * 6 + [(101.0, 101.1, 100.55, 101.0)]
    c = collections.Counter()
    assert SP.scan_S3(ctx_for(from15(fill15(s, 101.0)), FLAT60), c) is None and c["S3_expire"] >= 1


# ------------------------------------------------------------------ S4
def s4_bars(reds=((103.9, 103.9, 103.65, 103.7), (103.7, 103.75, 103.55, 103.6)),
            entry=(103.6, 103.95, 103.6, 103.95), start=(10, 40)):
    t0 = T(*start)
    b = flat(103.0, T(9, 30), t0)
    pole = [(103.0, 103.3, 103.0, 103.3), (103.3, 103.6, 103.3, 103.6), (103.6, 103.9, 103.6, 103.9)]
    seq = pole + list(reds) + [entry]
    b += [bar(t0 + 60 * i, *x) for i, x in enumerate(seq)]
    b += flat(entry[3], t0 + 60 * len(seq), T(16, 0))
    return b


def test_S4_fires():
    sig = SP.scan_S4(ctx_for(s4_bars(), FLAT60, prior_close=100.0), collections.Counter())
    assert sig and sig["t"] == T(10, 46) and abs(sig["stop"] - 103.54) < 1e-9


def test_S4_one_red_is_not_a_pullback():
    b = s4_bars(reds=((103.9, 103.9, 103.65, 103.7),), entry=(103.7, 103.95, 103.7, 103.95))
    assert SP.scan_S4(ctx_for(b, FLAT60), collections.Counter()) is None


def test_S4_six_reds_cancel():
    reds = tuple((103.9 - 0.02 * i, 103.9 - 0.02 * i, 103.6 - 0.01 * i, 103.88 - 0.02 * i) for i in range(6))
    c = collections.Counter()
    assert SP.scan_S4(ctx_for(s4_bars(reds=reds), FLAT60), c) is None and c["S4_cancel"] == 1


def test_S4_close_below_pole_open_cancels():
    reds = ((103.9, 103.9, 103.65, 103.7), (103.7, 103.7, 102.9, 102.95))
    c = collections.Counter()
    assert SP.scan_S4(ctx_for(s4_bars(reds=reds), FLAT60), c) is None and c["S4_cancel"] == 1


def test_S4_needs_plus_3pct_and_window_and_break():
    c = collections.Counter()
    assert SP.scan_S4(ctx_for(s4_bars(), FLAT60, prior_close=101.5), c) is None and c["S4_below_3pct"] == 1
    c = collections.Counter()
    assert SP.scan_S4(ctx_for(s4_bars(start=(10, 20)), FLAT60), c) is None and c["S4_outside_window"] == 1
    c = collections.Counter()
    b = s4_bars(entry=(103.6, 103.7, 103.6, 103.7))   # close not above the previous high
    assert SP.scan_S4(ctx_for(b, FLAT60), c) is None and c["S4_no_break"] == 1


# ------------------------------------------------------------------ S5
S5_HEAD = [(102, 102.5, 101.5, 102), (102, 102.2, 101.0, 101.2), (101.2, 101.4, 100.9, 101.3)]


def test_S5_pin_fires():
    s = S5_HEAD + [(100.6, 100.8, 99.95, 100.75)]
    sig = SP.scan_S5(ctx_for(from15(fill15(s, 100.75)), FLAT60, prior_low=100.0), collections.Counter())
    assert sig and sig["kind"] == "pin" and sig["t"] == T(10, 30) and abs(sig["stop"] - 99.94) < 1e-9


def test_S5_double_bottom_fires():
    s = S5_HEAD + [(100.5, 100.6, 99.98, 100.2), (100.2, 100.5, 100.2, 100.4), (100.4, 100.6, 100.3, 100.5),
                   (100.4, 100.5, 100.0, 100.45)]
    sig = SP.scan_S5(ctx_for(from15(fill15(s, 100.45)), FLAT60, prior_low=100.0), collections.Counter())
    assert sig and sig["kind"] == "double_bottom" and sig["t"] == T(11, 15) and abs(sig["stop"] - 99.97) < 1e-9


def test_S5_second_low_too_early_and_cancel():
    s = S5_HEAD + [(100.5, 100.6, 99.98, 100.2), (100.2, 100.5, 100.2, 100.4), (100.3, 100.5, 100.0, 100.45),
                   (100.45, 100.6, 100.3, 100.5)]
    assert SP.scan_S5(ctx_for(from15(fill15(s, 100.45)), FLAT60, prior_low=100.0), collections.Counter()) is None
    s = S5_HEAD + [(100.5, 100.6, 99.98, 100.2), (100.2, 100.3, 99.7, 99.8)]
    c = collections.Counter()
    assert SP.scan_S5(ctx_for(from15(fill15(s, 99.8)), FLAT60, prior_low=100.0), c) is None and c["S5_cancel"] == 1


def test_S5_first_touch_only_from_above():
    s = [(102, 102.5, 101.5, 102), (102, 102.2, 100.05, 101.2), (101.2, 101.4, 100.9, 101.3),
         (100.6, 100.8, 99.95, 100.75)]
    sig = SP.scan_S5(ctx_for(from15(fill15(s, 100.75)), FLAT60, prior_low=100.0), collections.Counter())
    assert sig and sig["t"] == T(10, 30)    # the 09:45 bar (start < 10:00) is not a touch; 10:15 approaches from above
    s2 = [(102, 102.5, 101.5, 102), (102, 102.2, 101.0, 101.2), (101.2, 101.4, 100.1, 101.3),
          (100.6, 100.8, 99.95, 100.75)]
    assert SP.scan_S5(ctx_for(from15(fill15(s2, 100.75)), FLAT60, prior_low=100.0),
                      collections.Counter()) is None     # previous low 100.1 is not > level x 1.0015


# ------------------------------------------------------------------ S6
S6_5 = ([(100.7, 101, 100.5, 100.8), (100.8, 100.8, 100.3, 100.5), (100.5, 100.6, 100.0, 100.4),
         (100.4, 100.9, 100.4, 100.8), (100.8, 101.2, 100.6, 101.0)] + [(100.9, 100.9, 100.5, 100.7)] * 7
        + [(100.5, 100.7, 99.8, 100.3), (100.3, 101.5, 100.25, 101.4), (101.4, 101.8, 100.9, 101.6),
           (101.6, 101.7, 100.85, 101.0)])


def s6_bars(spec):
    b = from5(spec)
    return b + flat(spec[-1][3], b[-1].end, T(16, 0))


def test_S6_fires_only_after_fvg_known():
    c = collections.Counter()
    sig = SP.scan_S6(ctx_for(s6_bars(S6_5), FLAT60), c)
    assert sig and sig["fvg"] == [100.7, 100.9]
    assert sig["t"] == T(10, 49) and sig["t"] > T(10, 45)      # the touch inside bar k+1 (10:40-10:45) is ignored
    assert abs(sig["stop"] - 99.79) < 1e-9


def test_S6_no_fvg_no_entry():
    s = list(S6_5)
    s[14] = (101.4, 101.8, 100.6, 101.6)                      # low(k+1) <= high(k-1): no gap
    assert SP.scan_S6(ctx_for(s6_bars(s), FLAT60), collections.Counter()) is None


def test_S6_close_below_swing_low_is_not_a_sweep():
    s = list(S6_5)
    s[12] = (100.5, 100.7, 99.8, 99.9)
    c = collections.Counter()
    SP.scan_S6(ctx_for(s6_bars(s), FLAT60), c)
    assert c["S6_sweep"] == 0


# ------------------------------------------------------------------ S7
S7_PRIOR = [bars60(D2, [(102, 103.0, 101.0, 102), (102, 103.5, 101.5, 103), (103, 104.0, 102.0, 103.5),
                        (103.5, 103.6, 101.8, 103), (103, 103.2, 101.5, 102.5), (102.5, 106.0, 102.5, 103.5),
                        (103.5, 104.5, 102.8, 103.8)])]
S7_15 = [(103.8, 104.0, 103.6, 103.9), (103.9, 104.3, 103.8, 104.2), (104.2, 104.6, 104.1, 104.4),
         (104.4, 104.9, 104.3, 104.5), (104.5, 104.6, 103.7, 103.9), (103.9, 104.5, 103.8, 104.4)]


def test_S7_fires_with_swing_known_at_indication_start():
    sig = SP.scan_S7(ctx_for(from15(fill15(S7_15, 104.4)), S7_PRIOR, sessions5=(D2,)), collections.Counter())
    assert sig and sig["H"] == 104.0          # 106 (yesterday 14:30 bar) is only known at 10:30 today: not used
    assert sig["t"] == T(11, 0) and abs(sig["stop"] - 103.69) < 1e-9 and sig["ind_high"] == 104.9


def test_S7_needs_a_correction():
    s = S7_15[:4] + [(104.5, 104.6, 104.1, 104.2), (104.2, 104.5, 104.1, 104.4)]
    assert SP.scan_S7(ctx_for(from15(fill15(s, 104.4)), S7_PRIOR), collections.Counter()) is None


def test_S7_cancel_below_pre_indication_swing_low():
    s = S7_15[:4] + [(104.5, 104.6, 101.0, 101.2), (101.2, 104.5, 101.0, 104.4)]
    c = collections.Counter()
    assert SP.scan_S7(ctx_for(from15(fill15(s, 104.4)), S7_PRIOR), c) is None and c["S7_cancel_swing_low"] == 1


# ------------------------------------------------------------------ common rules
def test_stale_entry_uses_the_trade_and_drops_pair():
    b1 = from15(fill15(S3_15, 101.0))
    ctx = ctx_for(b1, FLAT60)
    ctx.entry = lambda t: None
    c = collections.Counter()
    sig = SP.scan_S3(ctx, c)
    assert sig["stale"] and c["S3_stale_entry"] == 1


def test_R_bounds():
    ctx = ctx_for(flat(100, T(9, 30), T(16, 0)), FLAT60)
    c = collections.Counter()
    assert SP.try_entry(ctx, "S2", T(11, 0), 99.86, c)[0] == "skip"         # R 0.14% < 0.15%
    assert SP.try_entry(ctx, "S2", T(11, 0), 99.85, c)[0] == "entry"        # R 0.15% (bound included)
    assert SP.try_entry(ctx, "S2", T(11, 0), 98.0, c)[0] == "entry"         # R 2.0% (bound included)
    assert SP.try_entry(ctx, "S2", T(11, 0), 97.99, c)[0] == "skip"         # R > 2.0%
    assert SP.try_entry(ctx, "S2", T(11, 0), 100.5, c)[0] == "skip"         # R <= 0


# ------------------------------------------------------------------ exits
def qd(stale_at=None):
    def q(sym, t):
        if stale_at and any(abs(t - x) < 1e-6 for x in stale_at):
            return [t - 30, 99.99, 100.01]
        return [t, 99.99, 100.01]
    return FakeData([DAY], quote_fn=q)


def _sim(bars, t=None, stop=99.0, target=103.0, stale_at=None, counts=None):
    t = t or T(11, 0)
    b1 = flat(100, T(9, 30), T(16, 0))
    rep = {b.start: b for b in bars}
    b1 = [rep.get(b.start, b) for b in b1]
    return SP.simulate(qd(stale_at), "XYZ", b1, t, 100.0, 0.001, stop, target, 1.0, counts)


def test_stop_wins_when_bar_hits_both():
    o = _sim([bar(T(11, 5), 100, 103.5, 98.5, 100)])
    assert o["exit"] == "stop" and abs(o["net"] - (1e4 * (-0.01) - 5 - 1)) < 1e-6


def test_gap_through_fills_at_open_and_slippage_cap():
    o = _sim([bar(T(11, 5), 98, 98.2, 97.5, 98)])
    assert abs(o["net"] - (1e4 * (-0.02) - 6)) < 1e-6 and abs(o["net_slip"] - o["net"]) < 1e-9   # capped at open
    o = _sim([bar(T(11, 5), 99.5, 99.6, 98, 98.5)])
    assert abs(o["net"] - (-100 - 6)) < 1e-6 and abs(o["net_slip"] - (-150 - 6)) < 1e-6        # (stop+low)/2


def test_target_on_ge():
    o = _sim([bar(T(11, 5), 100, 103.0, 99.5, 102)])
    assert o["exit"] == "target" and abs(o["R_mult"] - 3.0) < 1e-9


def test_exit_scan_starts_at_t_plus_60():
    o = _sim([bar(T(11, 0), 100, 100, 98, 100)])                  # the bar containing the entry is skipped
    assert o["exit"] == "time"


def test_time_exit_at_plus_80_and_stale_fallback():
    o = _sim([])
    assert o["exit"] == "time" and abs(o["hold_min"] - 80.0) < 1e-9
    c = collections.Counter()
    o = _sim([bar(T(12, 19), 100, 100.2, 99.9, 100.2)], stale_at=[T(11, 0) + 5 + 4800], counts=c)
    assert c["time_exit_bar_close_fallback"] == 1 and abs(o["net"] - (1e4 * 0.002 - 10)) < 1e-6   # entry spread


def test_time_exit_capped_at_1550():
    o = _sim([], t=T(14, 40))
    assert abs(o["hold_min"] - (80 * 60 - 600 - 5) / 60) < 1e-9


def test_intrabar_exit_quote_stale_uses_entry_spread():
    c = collections.Counter()
    o = _sim([bar(T(11, 5), 100, 103.5, 98.5, 100)], stale_at=[T(11, 6)], counts=c)
    assert c["exit_quote_stale_entry_spread"] == 1 and abs(o["net"] - (-100 - 10)) < 1e-6


# ------------------------------------------------------------------ controls
def test_control_time_rules():
    t = T(11, 0)
    for s in SP.SETUPS:
        c = SP.control_time(s, "XYZ", DAY, t)
        assert c == SP.control_time(s, "XYZ", DAY, t)                # deterministic
        assert abs(c - t) > 3600 and T(10, 30) <= c <= T(14, 30)
        if s not in SP.MINUTE_SETUPS:
            assert (c - T(10, 30)) % 900 == 0
    ms = {SP.control_time("S4", f"N{k}", DAY, T(13, 0)) for k in range(40)}
    assert any((x - T(10, 30)) % 900 for x in ms)                    # minute grid for S4


def test_control_same_geometry(monkeypatch):
    calls = []
    real = SP.simulate

    def rec(data, sym, b1, t, entry, spread, stop, target, beta, counts=None, tag=""):
        calls.append((t, entry, stop, target))
        return real(data, sym, b1, t, entry, spread, stop, target, beta, counts, tag)
    monkeypatch.setattr(SP, "simulate", rec)
    b1 = flat(100, T(9, 30), T(16, 0))
    ctx = ctx_for(b1, FLAT60)
    sig = {"setup": "S3", "t": T(11, 0), "entry": 100.0, "spread": 0.001, "stop": 99.0, "R": 1.0, "r_pct": 0.01}
    p = SP.run_pair(qd(), ctx, sig, 1.0, collections.Counter())
    ct = p["ctrl_t"]
    ctrl_calls = [x for x in calls if x[0] == ct]
    t_, e, st, tg = ctrl_calls[0]
    assert abs(st - e * 0.99) < 1e-9 and abs(tg - e * 1.03) < 1e-9
    assert all(x[0] == ct for x in calls if x[0] != sig["t"])


def test_control_stale_entry_drops_pair():
    b1 = flat(100, T(9, 30), T(16, 0))
    ctx = ctx_for(b1, FLAT60)
    sig = {"setup": "S3", "t": T(11, 0), "entry": 100.0, "spread": 0.001, "stop": 99.0, "R": 1.0, "r_pct": 0.01}
    c = SP.control_time("S3", "XYZ", DAY, sig["t"])
    p = SP.run_pair(qd(stale_at=[c + 5]), ctx, sig, 1.0, collections.Counter())
    assert p["ctrl_drop"] == "stale" and "ctrl" not in p


def test_control_reads_nothing_before_its_own_entry():
    b1 = flat(100, T(9, 30), T(16, 0))
    ctx = ctx_for(b1, FLAT60)
    sig = {"setup": "S5", "t": T(13, 30), "entry": 100.0, "spread": 0.001, "stop": 99.0, "R": 1.0, "r_pct": 0.01}
    d = qd()
    p = SP.run_pair(d, ctx, sig, 1.0, collections.Counter(), event_arm=False)
    c = p["ctrl_t"]
    assert d.quote_log and all(t >= c + SP.ENTRY_LAG for _, t in d.quote_log)


# ------------------------------------------------------------------ verdicts
def _pairs(n_days=40, n_names=30, diff=0.0, noise=10.0, ev_mean=20.0, slip_shift=0.0, seed=3):
    rng = random.Random(seed)
    days = weekdays("2026-06-01", n_days)
    hv = NH.halves_of(days)
    out = []
    for d in days:
        for k in range(n_names):
            c = rng.gauss(ev_mean - diff, noise)
            e = c + diff + rng.gauss(0, noise)
            arm = lambda x: {"net": x, "hedged": x, "net_slip": x + slip_shift, "hedged_slip": x + slip_shift}  # noqa
            out.append({"setup": "S2", "sym": f"N{k}", "day": d, "half": hv[d], "t": T(11, 0), "ctrl_t": T(13, 0),
                        "ev": arm(e), "ctrl": arm(c)})
    return out


def test_setup_verdicts():
    assert SP.setup_verdict(_pairs(diff=20.0), "S2")["verdict"] == "PASS"
    assert SP.setup_verdict(_pairs(diff=-20.0), "S2")["verdict"] == "FAIL"
    v = SP.setup_verdict(_pairs(diff=0.0, noise=3.0, n_days=60, n_names=40), "S2")
    assert v["verdict"] == "FAIL" and v["powered"]
    assert SP.setup_verdict(_pairs(diff=0.0, noise=500.0, n_days=8, n_names=5), "S2")["verdict"] == "UNDERPOWERED"


def test_setup_verdict_event_net_must_be_positive_and_slip_cell():
    v = SP.setup_verdict(_pairs(diff=20.0, ev_mean=-50.0), "S2")
    assert v["verdict"] != "PASS" and any("event net <= 0" in w for w in v["why"])
    ps = _pairs(diff=20.0)
    for p in ps:
        p["ev"]["net_slip"] = p["ev"]["net"] - 40          # stop slippage hurts only the event arm
        p["ev"]["hedged_slip"] = p["ev"]["hedged"] - 40
    v = SP.setup_verdict(ps, "S2")
    assert v["verdict"] != "PASS" and "stop-slippage cell < 0" in v["why"]


def test_setup_verdict_drop_top_names():
    ps = _pairs(diff=-2.0, noise=2.0)
    for p in ps:
        if p["sym"] in ("N0", "N1", "N2", "N3", "N4"):
            for k in ("net", "hedged", "net_slip", "hedged_slip"):
                p["ev"][k] += 300
    v = SP.setup_verdict(ps, "S2")
    assert sorted(v["raw"]["drops"]["sym"]["dropped"]) == ["N0", "N1", "N2", "N3", "N4"]
    assert v["verdict"] != "PASS" and any("drop-top-sym" in w for w in v["why"])


def test_projection_is_outcome_blind_and_decides_extension():
    ps = _pairs(diff=0.0, noise=200.0, n_days=10, n_names=5)
    for p in ps:
        del p["ev"]                                        # no event outcome exists in the count step
    sigs = [{"setup": p["setup"], "sym": p["sym"], "day": p["day"], "half": p["half"], "stale": False} for p in ps]
    pj = SP.projection(ps, sigs)
    assert pj["S2"]["A"]["events"] > 0 and pj["S2"]["projected_mde"] > SP.UNDER_BP
    assert pj["extend_to_2025_11_03"]
    for h in "AB":
        assert abs(pj["S2"][h]["projected_se"] - 2 ** 0.5 * pj["S2"][h]["se_ctrl"]) < 1e-12


def test_drop_rates_failed_data():
    sigs = [{"setup": "S2", "stale": i < 5} for i in range(100)]
    pairs = [{"setup": "S2"} for _ in range(95)]
    assert SP.drop_rates(pairs, sigs)["failed_data"]                  # 5% vs 0%
    pairs[0]["ctrl_drop"] = "stale"
    pairs[1]["ctrl_drop"] = "stale"
    pairs[2]["ctrl_drop"] = "stale"
    assert not SP.drop_rates(pairs, sigs)["failed_data"]              # 5% vs 3.2%


# ------------------------------------------------------------------ end to end: count reads controls only
def test_count_simulates_controls_only(tmp_path, monkeypatch):
    days = weekdays("2026-05-04", 34)
    mkt = FakeStudyMarket(days, ["AAA", "BBB", "CCC"], seed=5)
    monkeypatch.setattr(SP, "DATA_LO", days[10])
    monkeypatch.setattr(SP, "TEST_HI", days[-1])
    monkeypatch.setattr(NH, "BETA_MIN_PAIRS", 50)
    monkeypatch.setattr(NH.BRO, "script_rev", lambda: "test")
    sim_t, ctrl_t = [], []
    real_sim, real_ct = SP.simulate, SP.control_time

    def rec_sim(data, sym, b1, t, *a, **k):
        sim_t.append(t)
        return real_sim(data, sym, b1, t, *a, **k)

    def rec_ct(*a):
        c = real_ct(*a)
        ctrl_t.append(c)
        return c
    monkeypatch.setattr(SP, "simulate", rec_sim)
    monkeypatch.setattr(SP, "control_time", rec_ct)
    # force signals: every name-day gets an S2 signal at 11:00
    monkeypatch.setitem(SP.SCANNERS, "S2", lambda ctx, c: SP.try_entry(ctx, "S2", T(11, 0, day=ctx.day),
                                                                       ctx.entry(T(11, 0, day=ctx.day))[0] * 0.995,
                                                                       c)[1])
    res = SP.run("count", work=str(tmp_path), mkt=mkt)
    assert res["projection"]["S2"]["A"]["events"] + res["projection"]["S2"]["B"]["events"] > 0
    assert sim_t and set(sim_t) <= set(ctrl_t)                        # no event exit was simulated
    out = SP.run("score", work=str(tmp_path), mkt=mkt) if not res["projection"]["extend_to_2025_11_03"] else None
    if out is None:
        with pytest.raises(SystemExit):
            SP.run("score", work=str(tmp_path), mkt=mkt)


# ------------------------------------------------------------------ review round 1 (reviewer B)
def test_time_exit_without_quote_or_bar_drops_the_pair():
    """FF1: no time-exit quote AND no fallback bar -> no outcome (not px = entry booked as -cost); the event arm
    drop enters the FAILED-DATA event drop rate."""
    c = collections.Counter()
    t = T(11, 0)
    d = qd(stale_at=[t + 5 + 4800])
    assert SP.simulate(d, "XYZ", [], t, 100.0, 0.001, 99.0, 103.0, 1.0, c, "ev_") is None
    assert c["ev_time_exit_no_price"] == 1
    ctx = ctx_for([], FLAT60)
    sig = {"setup": "S3", "t": t, "entry": 100.0, "spread": 0.001, "stop": 99.0, "R": 1.0, "r_pct": 0.01}
    p = SP.run_pair(d, ctx, sig, 1.0, c, control_arm=False)
    assert p["ev_drop"] == "no_exit_price" and "ev" not in p
    sigs = [{"setup": "S3", "stale": False} for _ in range(20)]
    pairs = [{"setup": "S3"} for _ in range(19)] + [p]
    assert SP.drop_rates(pairs, sigs)["event"] == 0.05


def test_powered_is_judged_on_the_raw_series_only():
    """N1 (operator): 'powered' = every RAW half MDE <= 15 bp (the count step projects from raw control net). A
    raw-powered, not-passed setup is FAIL even when the hedged MDE > 15 bp."""
    ps = _pairs(diff=0.0, noise=3.0, n_days=60, n_names=40)
    rng = random.Random(5)
    for p in ps:
        p["ev"]["hedged"] = p["ev"]["hedged_slip"] = p["ev"]["net"] + rng.gauss(0, 2000)
    v = SP.setup_verdict(ps, "S2")
    assert all(h["mde"] <= SP.UNDER_BP for h in v["raw"]["halves"].values())
    assert any(h["mde"] > SP.UNDER_BP for h in v["hedged"]["halves"].values())
    assert v["hedged"]["pooled"]["t"] > SP.FAIL_T and v["raw"]["pooled"]["t"] > SP.FAIL_T   # not the t <= -2 rule
    assert v["powered"] and v["verdict"] == "FAIL"


def test_spy_stale_and_beta_missing_are_counted_apart():
    """N4: a missing beta is not a stale SPY quote."""
    c = collections.Counter()
    b1 = flat(100, T(9, 30), T(16, 0))
    SP.simulate(qd(), "XYZ", b1, T(11, 0), 100.0, 0.001, 99.0, 103.0, None, c, "ev_")
    assert c["ev_beta_missing"] == 1 and c["ev_spy_stale"] == 0
    d = FakeData([DAY], quote_fn=lambda s, t: [t - 30, 99.99, 100.01] if s == NH.SPY else [t, 99.99, 100.01])
    SP.simulate(d, "XYZ", b1, T(11, 0), 100.0, 0.001, 99.0, 103.0, 1.0, c, "ev_")
    assert c["ev_spy_stale"] == 1 and c["ev_beta_missing"] == 1


def test_score_signal_mismatch_says_rerun_fetch(tmp_path, monkeypatch):
    """N3: score's signals differ from count's when a quote only score needs is missing -> 're-run fetch'."""
    import json as _json
    days = weekdays("2026-05-04", 34)
    mkt = FakeStudyMarket(days, ["AAA", "BBB", "CCC"], seed=5)
    monkeypatch.setattr(SP, "DATA_LO", days[10])
    monkeypatch.setattr(SP, "TEST_HI", days[-1])
    monkeypatch.setattr(NH, "BETA_MIN_PAIRS", 50)
    monkeypatch.setattr(NH.BRO, "script_rev", lambda: "test")
    res = SP.run("count", work=str(tmp_path), mkt=mkt)
    res["signals_sha256"] = "0" * 64
    res["projection"]["extend_to_2025_11_03"] = False
    _json.dump(res, open(tmp_path / "count.json", "w"))
    with pytest.raises(SystemExit, match="re-run fetch"):
        SP.run("score", work=str(tmp_path), mkt=mkt)


def test_pass_action_blocked_and_report_lists_uncomputed_cells():
    """N5: a PASS carries 'ACTION BLOCKED: earlier-period and IEX reruns not wired' in why and report.md;
    ambiguity 5: report.md lists the two uncomputed information cells as NOT COMPUTED with the reason."""
    v = SP.setup_verdict(_pairs(diff=20.0), "S2")
    assert v["verdict"] == "PASS" and NH.ACTION_BLOCKED in v["why"]
    h = {"events": 1, "names": 1, "projected_mde": 1.0}
    res = {"prereg": SP.PREREG, "script_rev": "test", "failed_data": False,
           "count": {"projection": {s: {"A": h, "B": h} for s in SP.SETUPS}},
           "setups": {s: v for s in SP.SETUPS}}
    md = SP.render_report(res)
    assert "ACTION BLOCKED: earlier-period and IEX reruns not wired" in md
    assert md.count(": NOT COMPUTED - ") == 2 and "short mirror" in md and "%R square" in md
    r = SP.RESOLUTIONS
    assert "'S2'" in r["R14_control_draw"] and "never fire" in r["R10_S6_resume"]
    assert "impossible" in r["R9_S5_resume"] and "before the R-bound" in r["R3_stale_entry"]
