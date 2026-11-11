"""tools/studies/crypto_carry.py: 8-hour periods from hourly rows, carry = funding + basis change, costs, switching."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import crypto_carry as C  # noqa: E402

H = C.HOUR


def _rows(n_hours, rate=0.0000125, px=100.0, gap=0.0):
    f = {str((i + 1) * H): [rate, px] for i in range(n_hours)}
    p = {str(i * H): px + gap for i in range(n_hours + 1)}                # chart bars keyed by their start: bar i*H closes at (i+1)*H
    return f, p


def test_periods_sum_eight_hours_and_drop_incomplete_ones():
    f, p = _rows(24)
    del f[str(11 * H)]                                             # a missing hour spoils the second period only
    ps = C.periods(f, p)
    assert [x["t"] for x in ps] == [8 * H, 24 * H]
    assert ps[0]["funding"] == pytest.approx(0.0001)


def test_carry_is_funding_plus_basis_change_less_cost():
    f, p = _rows(24)
    p[str(15 * H)] = 100.5                                         # the bar closing at 16h: the future 0.5% above the coin, the short loses that
    s = C.carry_series(C.periods(f, p))
    assert s[0]["ret"] == pytest.approx(0.0001 - 0.005 - C.ROUND_TRIP / C.PER_YEAR)
    assert s[1]["ret"] == pytest.approx(0.0001 + 0.005 - C.ROUND_TRIP / C.PER_YEAR)     # and gets it back when the gap closes


def test_switched_waits_for_a_week_of_high_funding_and_pays_to_enter():
    f, p = _rows(8 * 60, rate=0.00002)                             # 0.016% per 8 h, above the 0.01% switch
    s = C.carry_series(C.periods(f, p), switched=True)
    assert not s[0]["on"] and s[0]["ret"] == 0.0
    first = next(i for i, x in enumerate(s) if x["on"])
    assert first == C.SWITCH_N - 1 and s[first]["ret"] == pytest.approx(0.00016 - C.ROUND_TRIP)
    assert s[first + 1]["ret"] == pytest.approx(0.00016)
    low = C.carry_series(C.periods(*_rows(8 * 60, rate=0.000005)), switched=True)
    assert not any(x["on"] for x in low)


def test_stats_and_verdict_on_a_steady_carry():
    f, p = _rows(8 * 3 * 365 * 2, rate=0.0000125)                  # 0.01% per 8 h = about 11% a year on notional
    st = C.stats(C.carry_series(C.periods(f, p)))
    assert st["annual"] == pytest.approx((0.1095 - 0.003) / 1.5, abs=0.004) and st["neg_share"] == 0 and st["maxdd"] == 0
    assert C.verdict({**st, "cash": 0.02})[0] == "PASS"
    assert "at least 2 points a year above cash" in C.verdict({**st, "cash": 0.07})[1]


def test_the_future_is_read_at_the_same_instant_as_the_coin():
    f, p = _rows(16)
    p[str(8 * H)] = 999.0                                           # the bar STARTING at 8h closes at 9h: must not be used for 8h
    ps = C.periods(f, p)
    assert ps[0]["perp"] == 100.0
