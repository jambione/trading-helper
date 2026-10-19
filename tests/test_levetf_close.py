"""levetf_close: the leveraged-ETF close-flow test. Synthetic bars only, no market data."""
import os
import random
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import levetf_close as L  # noqa: E402


def test_big_flags_read_only_earlier_sessions():
    xs = [float(i % 7) for i in range(80)]
    flags = L.big_flags(xs)
    assert flags[:60] == [None] * 60
    changed = xs[:70] + [1e9] * 10
    assert L.big_flags(changed)[:70] == flags[:70]


def test_big_flag_is_the_80th_percentile_of_the_last_60():
    xs = list(range(1, 61)) + [48.2, 48.0]
    flags = L.big_flags(xs)
    # 80th percentile of 1..60 (linear) is 48.2; session 61 sees 2..61's window.
    assert flags[60] is True
    assert flags[61] is False


def test_trade_uses_1530_open_and_1549_close():
    bars = {L.SIG_MIN: (0, 0, 0, 99.0), L.ENTRY_MIN: (100.0, 0, 0, 100.0), L.EXIT_MIN: (0, 0, 0, 101.0),
            L.INFO_EXIT_MIN: (0, 0, 0, 102.0)}
    assert abs(L.trade_bp(bars, 1) - 100.0) < 1e-9
    assert abs(L.trade_bp(bars, -1) + 100.0) < 1e-9
    assert abs(L.trade_bp(bars, 1, L.INFO_EXIT_MIN) - 200.0) < 1e-9
    assert L.trade_bp({L.ENTRY_MIN: (100.0, 0, 0, 100.0)}, 1) is None


def test_halves_and_the_round2_window():
    assert L.half_of("2016-03-01") == "A" and L.half_of("2020-12-31") == "A"
    assert L.half_of("2021-01-04") == "B" and L.half_of("2025-09-26") == "B"
    assert L.half_of("2025-09-29") == "R2" and L.half_of("2026-09-25") == "R2"
    assert L.half_of("2026-10-16") == "B"
    assert L.half_of("2016-02-29") is None


def _h(**over):
    base = {"big_sessions": 200, "net": 2.0, "net_t": 3.0, "excess": 1.0, "lev_minus_placebo": 1.0,
            "boot_lo": 0.5, "net_wo_top": 1.0, "net_wo_crash": 1.0}
    a, b = dict(base), dict(base)
    for k, v in over.items():
        half, key = k.split("_", 1)
        (a if half == "a" else b)[key] = v
    return {"A": a, "B": b, "comb_t": 3.0, "lmp_t": 3.0}


def test_the_words():
    assert L.word(_h()) == "PASS"
    assert L.word(_h(b_lev_minus_placebo=-0.1)) == "MOMENTUM ONLY"
    weak = _h()
    weak["lmp_t"] = 1.9
    assert L.word(weak) == "MOMENTUM ONLY"
    assert L.word(_h(a_net_t=1.0)) == "PROMISING"
    assert L.word(_h(a_net_t=1.0, b_excess=-0.1)) == "FAIL"
    assert L.word(_h(b_net_wo_top=-0.1)) == "FAIL"
    assert L.word(_h(b_boot_lo=-0.1)) == "PROMISING"
    assert L.word(_h(a_big_sessions=149)) == "UNDERPOWERED"
    assert L.word(_h(b_net=-1.0)) == "FAIL"


def test_a_missing_spread_month_costs_flat_5_bp_and_is_counted():
    minutes, daily = _synthetic(plant_lev=0.0, plant_placebo=0.0, n=90)
    spr = {}
    rows, cnt = L.build_rows(minutes, daily, spr)
    assert rows and all(abs((r["gross"] - r["net"]) - L.FLAT_BP) < 1e-9 for r in rows)
    assert cnt["missing_spread_months"] > 0
    spr = {(s, m): 0.5 for s in L.SYMS for m in {d[:7] for d in minutes}}
    rows, cnt = L.build_rows(minutes, daily, spr)
    assert all(abs((r["gross"] - r["net"]) - 1.5) < 1e-9 for r in rows) and cnt["missing_spread_months"] == 0


def test_the_prior_close_is_the_previous_session_not_today():
    minutes, daily = _synthetic(plant_lev=0.0, plant_placebo=0.0, n=70)
    d = sorted(minutes)[65]
    daily["SPY"][d] = 1e6           # today's daily close must not be read
    rows, _ = L.build_rows(minutes, daily, {})
    r = [x for x in rows if x["sym"] == "SPY" and x["day"] == d][0]
    assert abs(r["r"]) < 0.5


def test_week_boot_brackets_a_constant():
    vals = {(date(2021, 1, 4) + timedelta(days=i)).isoformat(): 3.0 for i in range(0, 200)}
    lo, hi = L.week_boot(vals, n=200)
    assert abs(lo - 3.0) < 1e-9 and abs(hi - 3.0) < 1e-9


def _synthetic(plant_lev: float, plant_placebo: float, n: int, seed: int = 0):
    """Random-walk sessions. A planted edge adds `plant` bp in the day's direction to the 15:49 close on big days."""
    rng = random.Random(seed)
    day0 = date(2015, 10, 1)
    days, d = [], day0
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    minutes = {x: {} for x in days}
    daily = {s: {} for s in L.SYMS}
    for s in L.SYMS:
        prev = 100.0
        hist = []
        for x in days:
            r = rng.gauss(0, 0.01)
            sig = prev * (1 + r)
            hist.append(abs(r))
            big = len(hist) > L.WARM and abs(r) >= L._quantile(sorted(hist[-L.WARM - 1:-1]), L.PCT)
            plant = plant_placebo if s in L.PLACEBO else plant_lev
            ent = sig * (1 + rng.gauss(0, 0.0002))
            move = rng.gauss(0, 0.0010) + (plant / 1e4 * (1 if r > 0 else -1) if big else 0.0)
            ex = ent * (1 + move)
            minutes[x][s] = {L.SIG_MIN: (sig, sig, sig, sig), L.ENTRY_MIN: (ent, ent, ent, ent),
                             L.EXIT_MIN: (ex, ex, ex, ex), L.INFO_EXIT_MIN: (ex, ex, ex, ex)}
            close = ex
            daily[s][x] = close
            prev = close
    return minutes, daily


def _word_for(plant_lev, plant_placebo):
    minutes, daily = _synthetic(plant_lev, plant_placebo, n=2850, seed=7)
    old = (L.A_FROM, L.A_TO, L.B_FROM, L.B_TO)
    days = sorted(minutes)
    L.A_FROM, L.A_TO, L.B_FROM, L.B_TO = days[L.WARM], days[1400], days[1401], days[-1]
    try:
        rows, cnt = L.build_rows(minutes, daily, {(s, m): 0.5 for s in L.SYMS for m in {d[:7] for d in minutes}})
        md = L.report(rows, cnt)
    finally:
        L.A_FROM, L.A_TO, L.B_FROM, L.B_TO = old
    return md.split("**")[1], md


def test_a_planted_flow_reads_pass_and_noise_does_not():
    word, md = _word_for(plant_lev=8.0, plant_placebo=0.0)
    assert word == "PASS", md[:600]
    word, _ = _word_for(plant_lev=8.0, plant_placebo=8.0)
    assert word == "MOMENTUM ONLY"
    word, _ = _word_for(plant_lev=0.0, plant_placebo=0.0)
    assert word in ("FAIL", "UNDERPOWERED")
