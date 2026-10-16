"""Rule flags, onset/refractory, markout, control. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import early_entry_honest as E  # noqa: E402

NAN = float("nan")


def test_pre_flag_is_the_approach_band_not_the_full_square():
    fast = [-40.0, -38.0, -30.0]
    slow = [-33.0, -33.0, -33.0]
    assert E.pre_flag(fast, slow, 2)            # both >= -35, tight, fast rising
    full = [-30.0, -25.0, -15.0]
    assert not E.pre_flag(full, [-18.0] * 3, 2)  # full square is SQ, not PRE
    assert E.sq_flag(full, [-18.0] * 3, 2)


def test_pre_flag_needs_rising_tight_and_finite():
    assert not E.pre_flag([-30.0, -30.0, -31.0], [-30.0] * 3, 2)    # fast falling
    assert not E.pre_flag([-40.0, -38.0, -10.0], [-34.0] * 3, 2)   # gap 24 > 15
    assert not E.pre_flag([-40.0, -38.0, NAN], [-30.0] * 3, 2)
    assert not E.pre_flag([-30.0], [-30.0], 0)                     # no previous minute
    assert not E.pre_flag([-30.0, -29.97], [-30.0, -30.0], 1)      # +0.03 is inside eps 0.05
    assert E.pre_flag([-30.0, -29.9], [-30.0, -30.0], 1)           # live: one minute back + eps


def test_grid_fill_matches_the_engine_minute_grid():
    import pandas as pd
    t0 = pd.Timestamp("2026-10-01 14:00", tz="UTC")
    f = pd.DataFrame({"time": [t0, t0 + pd.Timedelta(minutes=3), t0 + pd.Timedelta(minutes=40)],
                      "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0], "low": [1.0, 2.0, 3.0],
                      "close": [1.5, 2.5, 3.5], "volume": [10.0, 20.0, 30.0]})
    g = E.grid_fill(f)
    # 2 filled minutes at the previous close, 0 volume; a 37-minute gap stays a gap
    assert len(g) == 5
    assert list(g["close"][:4]) == [1.5, 1.5, 1.5, 2.5] and list(g["volume"][1:3]) == [0.0, 0.0]


def test_underpowered_is_not_a_fail():
    assert E.rule_verdict(["PASS", "PASS"]) == "PASS"
    assert E.rule_verdict(["PASS", "UNDERPOWERED"]) == "UNDERPOWERED"
    assert E.rule_verdict(["FAIL", "UNDERPOWERED"]) == "FAIL"


def test_markout_never_exits_on_the_closing_bar():
    sip = {m: (10.0, 10.0) for m in range(930, 961)}
    assert E.markout(sip, 951) is None        # exit bar 16:00
    assert E.markout(sip, 950) is not None    # exit bar 15:59


def _rth(n, start=E.RTH_START):
    return list(range(start, start + n))


def test_ign_needs_price_and_volume_and_counts_missing_minutes_as_zero():
    minutes = _rth(25)
    closes = [10.0] * 25
    vols = [100.0] * 25
    closes[24] = 10.03                      # +0.30% over 3 bars
    vols[24] = 250.0                        # 2.5x
    assert E.ign_flag(minutes, closes, vols, 24)
    vols[24] = 150.0
    assert not E.ign_flag(minutes, closes, vols, 24)
    # drop half the window bars: mean falls to 50, 150 is now 3x
    keep = [i for i in range(25) if i % 2 == 0 or i == 24]
    m2 = [minutes[i] for i in keep]
    c2 = [closes[i] for i in keep]
    v2 = [vols[i] for i in keep]
    assert E.ign_flag(m2, c2, v2, len(m2) - 1)


def test_ign_never_reads_premarket_or_the_future():
    minutes = _rth(10, start=E.RTH_START - 5)  # 09:25..09:34: window would reach premarket
    assert not E.ign_flag(minutes, [10.0] * 10, [100.0] * 10, 9)
    minutes = _rth(30)
    closes = [10.0] * 30
    vols = [100.0] * 30
    closes[24], vols[24] = 10.03, 250.0
    before = E.ign_flag(minutes, closes, vols, 24, dict(zip(minutes, vols)))
    vols[25:] = [10_000.0] * 5               # later bars must not change bar 24
    after = E.ign_flag(minutes, closes, vols, 24, dict(zip(minutes, vols)))
    assert before == after is True


def test_fires_onset_refractory_window_and_board():
    minutes = list(range(E.WIN_START - 2, E.WIN_START + 40))
    flags = [False] * len(minutes)
    for m in (E.WIN_START - 1, E.WIN_START + 1, E.WIN_START + 2, E.WIN_START + 10,
              E.WIN_START + 20, E.WIN_START + 36):
        flags[minutes.index(m)] = True
    flags[minutes.index(E.WIN_START + 2)] = True     # continuation, not an onset
    board = set(minutes) - {E.WIN_START + 36}
    sigs, off = E.fires(minutes, flags, board)
    # WIN_START-1 outside window; +1 fires; +2 continuation; +10 refractory; +20 fires; +36 off board
    assert sigs == [E.WIN_START + 1, E.WIN_START + 20]
    assert off == 1


def test_markout_is_entry_open_to_close_of_hold_bar():
    sip = {600: (10.0, 10.1), 609: (10.2, 10.5)}
    assert abs(E.markout(sip, 600) - 0.05) < 1e-12
    assert E.markout(sip, 601) is None


def test_control_is_a_later_minute_with_an_exit_bar():
    sip = {m: (10.0, 10.0) for m in range(600, 700)}
    k = E.control_minute("X", "2026-10-01", "PRE", sip, set(), 610)
    assert k is not None and 611 <= k <= 610 + E.CTRL_SPAN
    assert (k + E.HOLD - 1) in sip
    assert k == E.control_minute("X", "2026-10-01", "PRE", sip, set(), 610)   # seeded
    late = {m: (10.0, 10.0) for m in range(940, 960)}
    assert E.control_minute("X", "d", "PRE", late, set(), 950) is None       # no room before 16:00


def test_control_is_unbiased_after_a_selected_rise():
    """Null calibration: on a driftless walk, fire after a 3-bar rise; the
    rule's lift over its control must be ~0 (same-hour controls gave ~-20 bp)."""
    import random
    import statistics
    lifts = []
    for seed in range(300):
        r = random.Random(seed)
        p, sip = 20.0, {}
        for m in range(E.RTH_START, E.RTH_END):
            o = p
            p *= 1 + r.gauss(0, 0.002)
            sip[m] = (o, p)
        for m in range(600, 880):
            if sip[m][1] / sip[m - 3][1] - 1 > 0.004:
                g = E.markout(sip, m + 1)
                k = E.control_minute("S", str(seed), "R", sip, set(), m + 1)
                if g is not None and k is not None:
                    lifts.append(g - E.markout(sip, k))
    assert len(lifts) > 300
    se = statistics.stdev(lifts) / len(lifts) ** 0.5
    assert abs(statistics.mean(lifts)) < 3 * se


def test_half_verdict_requires_power():
    assert E.half_verdict(0.001, 2.5, 0.0001, 99, 9) == "UNDERPOWERED"
    assert E.half_verdict(0.001, 2.5, 0.0001, 200, 5) == "UNDERPOWERED"
    assert E.half_verdict(0.001, 2.5, -0.0001, 200, 9) == "FAIL"
    assert E.half_verdict(0.0006, 2.1, 0.0001, 200, 9) == "PASS"


def test_lead_report_counts_only_prior_fires():
    fills = {("X", "d"): [0.0]}
    # _et_min(0.0) is 19:00 ET on 1969-12-31; build fires relative to it
    fm = E._et_min(0.0)
    log = {("X", "d"): [("PRE", fm - 6), ("PRE", fm + 2), ("IGN", fm - 30)]}
    out = E.lead_report(log, fills)
    assert out["PRE"]["with_prior_fire"] == 1
    assert out["PRE"]["median_lead_min"] == 5      # bar fm-6 closes at fm-5
    assert out["IGN"]["with_prior_fire"] == 0      # 29 min > 15
    assert out["PRE"]["fires"] == 2 and abs(out["PRE"]["hit_rate_10m"] - 0.5) < 1e-12


def test_mid_flag_is_the_live_minus_50_cross():
    fast = [-55.0, -48.0]
    slow = [-60.0, -59.0]
    assert E.mid_flag(fast, slow, [50.0, 50.0], 1)
    assert E.mid_flag(fast, slow, [50.0, 70.0], 1)                # live has no RSI term
    assert not E.mid_flag(fast, slow, [50.0, 70.0], 1, cap=True)  # MIDCAP info variant
    assert not E.mid_flag(fast, [-60.0, -60.0], [50.0] * 2, 1)    # slow not rising
    assert not E.mid_flag([-45.0, -40.0], slow, [50.0] * 2, 1)    # no cross: already above
    assert not E.mid_flag([-50.0, -50.0], slow, [50.0] * 2, 1)    # -50 is not above -50


def test_rsi2_turn_needs_oversold_then_up_with_fast_rising():
    fast = [-80.0, -79.0]
    assert E.rsi2_turn_flag(fast, [30.0, 40.0], 1)
    assert not E.rsi2_turn_flag(fast, [36.0, 40.0], 1)             # was not oversold
    assert not E.rsi2_turn_flag(fast, [30.0, 29.0], 1)             # did not turn up
    assert not E.rsi2_turn_flag([-80.0, -80.0], [30.0, 40.0], 1)   # fast flat


def test_connors_green_needs_uptrend_pullback_and_oversold():
    import early_entry_honest as E
    closes = [100.0 + 0.01 * i for i in range(205)]          # steady uptrend
    closes[-1] = closes[-2] - 0.05                           # last bar pulls back under SMA5, still over SMA200
    rsi = [50.0] * 204 + [5.0]
    f = E.con_flags(closes, rsi)
    assert f[-1] and not any(f[:-1])
    assert not E.con_flags(closes, [50.0] * 204 + [12.0])[-1]          # RSI not under 10
    down = [100.0 - 0.01 * i for i in range(205)]
    assert not E.con_flags(down, [5.0] * 205)[-1]                     # under SMA200: no uptrend
    assert not any(E.con_flags(closes[:150], [5.0] * 150))             # fewer than 200 rows


def test_con_live_series_uses_prior_bars_for_warmup():
    import pandas as pd
    import early_entry_honest as E
    t0 = pd.Timestamp("2026-10-14 13:30", tz="UTC")          # 09:30 ET
    prior = pd.DataFrame({"close": [100.0 + 0.01 * i for i in range(220)]})
    today = pd.DataFrame({"time": [t0 + pd.Timedelta(minutes=k) for k in range(3)],
                          "close": [102.30, 102.35, 101.60]})   # up, up, sharp pullback (still over SMA200 ~101.2)
    m, f = E.con_live_series(prior, today)
    assert m == [570, 571, 572] and f[2] and not f[0]         # fires at 09:32 thanks to the warm-up
    assert not any(E.con_live_series(None, today)[1])         # no prior bars: cannot warm up
    import math
    closes = [100.0 + 0.01 * i for i in range(205)]
    closes[100] = float("nan")
    assert not any(E.con_flags(closes, [5.0] * 205))           # a missing close inside the window


def test_os_leave_needs_triangle_then_leave_rising():
    import tools.studies.early_entry_honest as E
    fast = [-95, -92, -85, -70, -60]
    slow = [-90, -90, -88, -84, -82]
    assert E.os_tri(fast, slow, 0) and E.os_tri(fast, slow, 1)
    assert not E.os_leave_flag(fast, slow, 2)            # still in the triangle
    assert E.os_leave_flag(fast, slow, 3)                # fast back above -80, rising, triangle 1 bar ago
    late = [-95, -92, -90, -89, -88, -87, -70]
    lslow = [-90, -90, -90, -60, -60, -60, -60]
    assert not E.os_leave_flag(late, lslow, 6)           # triangle more than 3 bars back
    assert not E.os_leave_flag([-95, -90, -80], [-60, -60, -60], 2)   # lines never together: no triangle
    assert not E.os_leave_flag([-95, -90, -85], [-90, -90, -84], 2)   # both still deep: not a leave


def test_warm_lines_give_a_slow_line_from_the_first_minute(monkeypatch):
    import math
    import pandas as pd
    import tools.studies.early_entry_honest as E

    def frame(start, n, base):
        t = pd.date_range(start, periods=n, freq="1min", tz="UTC")
        px = [base + 0.01 * ((i * 7) % 13) for i in range(n)]
        return pd.DataFrame({"time": t, "open": px, "high": [p + 0.02 for p in px],
                             "low": [p - 0.02 for p in px], "close": px, "volume": [1000.0] * n})
    prior = frame("2026-10-14 17:40", 220, 10.0)
    day = frame("2026-10-15 13:30", 60, 10.0)
    monkeypatch.setattr(E, "WARM_LINES", False)
    cold = E.name_day_series(day, prior)
    assert not math.isfinite(cold["slow"][0])
    monkeypatch.setattr(E, "WARM_LINES", True)
    warm = E.name_day_series(day, prior)
    assert len(warm["minutes"]) == len(cold["minutes"]) and warm["minutes"][0] == cold["minutes"][0]
    assert math.isfinite(warm["slow"][0]) and math.isfinite(warm["fast"][0])


def test_steep_turn_needs_slope_dip_and_rising_slow():
    import tools.studies.early_entry_honest as E
    fast = [-70, -65, -60, -58, -56, -55, -54, -60, -62, -61, -60, -50, -40]
    slow = [-50] * 11 + [-49, -48]
    assert E.steep_flag(fast, slow, 12)                    # +20 in 3 bars, slow rising, dip <= -50 earlier
    assert not E.steep_flag(fast, [-50] * 13, 12)          # slow flat
    lazy = fast[:11] + [-57, -52]
    assert not E.steep_flag(lazy, slow, 12)                # only +8 in 3 bars: sideways-ish
    high = [-30] * 10 + [-25, -15, -10]
    assert not E.steep_flag(high, slow, 12)                # no dip in the last 10 minutes


def test_confirm_sigs_buy_only_if_the_price_held():
    minutes = [600, 601, 602, 603, 604]
    closes = [10.0, 10.0, 9.9, 10.1, 10.2]
    assert E.confirm_sigs(minutes, closes, [600], 1, True) == [601]           # equal counts as held
    assert E.confirm_sigs(minutes, closes, [600], 2, True) == []              # dipped by bar 602
    assert E.confirm_sigs(minutes, closes, [600], 2, False) == [602]          # the skipped (dipped) signal
    assert E.confirm_sigs(minutes, closes, [603], 2, True) == []              # no bar 605: no decision
    assert E.confirm_sigs(minutes, [10.0, NAN, 1, 1, 1], [600], 1, True) == []
    vols = [5, 0, 5, 5, 5]
    assert E.confirm_sigs(minutes, closes, [600], 1, True, vols) == []         # no-print filler bar: no decision
    assert E.confirm_sigs(minutes, closes, [602], 1, True, vols, {602}) == []  # left the board before the decision
    assert E.confirm_sigs(minutes, closes, [602], 1, True, vols, {602, 603}) == [603]


def test_confirm_rules_derive_from_pre_onsets(monkeypatch):
    monkeypatch.setattr(E, "RULES", ("CONF1", "DIP1", "PRE"))
    monkeypatch.setattr(E, "pre_flag", lambda f, s, i: i == 2)
    for name in ("sq_flag", "os_leave_flag", "steep_flag"):
        monkeypatch.setattr(E, name, lambda f, s, i: False)
    monkeypatch.setattr(E, "mid_flag", lambda *a, **k: False)
    monkeypatch.setattr(E, "rsi2_turn_flag", lambda *a, **k: False)
    monkeypatch.setattr(E, "ign_flag", lambda *a, **k: False)
    m = list(range(600, 610))
    ser = {"minutes": m, "fast": [0.0] * 10, "slow": [0.0] * 10, "rsi": [0.0] * 10, "vol": [1.0] * 10,
           "close": [10, 10, 10, 10.1, 10, 10, 10, 10, 10, 10]}
    out = E.rule_fires(ser, set(m), ("X", "2026-10-01"))
    assert out["PRE"][0] == [602] and out["CONF1"][0] == [603] and out["DIP1"][0] == []
    monkeypatch.setattr(E, "RULES", ("HELD1", "PRE"))
    a = E.rule_fires(ser, set(m), ("X", "2026-10-01"))["HELD1"][0]
    assert a == E.rule_fires(ser, set(m), ("X", "2026-10-01"))["HELD1"][0]     # seeded: reproducible
    assert len(a) <= 1
