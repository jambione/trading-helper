"""The one kept cell, the pace formula, and the verdict. No market data."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import flat_pace_mid as F  # noqa: E402


def test_keep_is_the_published_band_and_nothing_wider():
    assert F.keep(1.64, 0.0, 0.5)
    assert F.keep(3.99, 0.01, 0.899)
    assert F.keep(1.64, -0.01, 0.0)
    assert not F.keep(1.639, 0.0, 0.5)     # below the published pace floor
    assert not F.keep(4.0, 0.0, 0.5)       # the 4x cap is exclusive
    assert not F.keep(2.0, 0.011, 0.5)     # past +1%
    assert not F.keep(2.0, -0.011, 0.5)
    assert not F.keep(2.0, 0.0, 0.90)      # at the live range cap
    assert not F.keep(None, 0.0, 0.5)
    assert not F.keep(2.0, None, 0.5)
    assert not F.keep(2.0, 0.0, None)


def test_pace_uses_only_the_prior_20_sessions():
    prior = [1000.0] * 20
    # 30 minutes after the open is a curve knot: expected fraction 0.20.
    assert abs(F.pace_of(200.0, prior, 30) - 1.0) < 1e-9
    assert F.pace_of(200.0, [1000.0] * 19, 30) is None
    # A 21st older session must not change the mean. The last 20 are the window.
    assert abs(F.pace_of(200.0, [99999.0] + prior, 30) - 1.0) < 1e-9


def test_range_includes_this_bar_and_rejects_a_flat_range():
    assert abs(F.range_pos(10.0, 8.0, 9.0) - 0.5) < 1e-9
    assert F.range_pos(10.0, 10.0, 10.0) is None


def _block(n, days, gross, t_gross, lift, t_lift, net3, net10):
    return {"n": n, "days": days, "gross": gross, "t_gross": t_gross,
            "lift": lift, "t_lift": t_lift, "net3": net3, "net10": net10, "lift_days": days}


def test_verdict_does_not_promote_a_one_sided_lift():
    # Both halves net-positive at 3 bp, but half B's lift vs the baseline is negative.
    kept = {
        "A": _block(200, 20, 0.0010, 2.0, 0.0008, 2.0, 0.0007, 0.0),
        "B": _block(200, 20, 0.0008, 2.0, -0.0001, -0.2, 0.0005, -0.0002),
        "ALL": _block(400, 40, 0.0009, 2.5, 0.0003, 1.0, 0.0006, -0.0001),
    }
    assert F.verdict(kept) == "FAIL"


def test_verdict_promising_needs_both_halves_and_the_combined_t():
    kept = {
        "A": _block(200, 20, 0.0010, 1.2, 0.0002, 0.8, 0.0007, 0.0),
        "B": _block(200, 20, 0.0008, 1.2, 0.0002, 0.8, 0.0005, -0.0002),
        "ALL": _block(400, 40, 0.0010, 3.0, 0.0002, 1.0, 0.0007, 0.0),
    }
    assert F.verdict(kept) == "PROMISING"
    thin = dict(kept)
    thin["B"] = _block(50, 4, 0.0008, 1.2, 0.0002, 0.8, 0.0005, -0.0002)
    # Half A already misses PASS (lift 2 bp, t 0.8, net at 10 bp is 0), so a thin B is a FAIL.
    assert F.verdict(thin) == "FAIL"


def test_verdict_stays_underpowered_when_the_powered_half_would_pass():
    kept = {
        "A": _block(200, 20, 0.0020, 3.0, 0.0008, 2.5, 0.0017, 0.0010),
        "B": _block(50, 4, 0.0020, 3.0, 0.0008, 2.5, 0.0017, 0.0010),
        "ALL": _block(250, 24, 0.0020, 3.0, 0.0008, 2.5, 0.0017, 0.0010),
    }
    assert F.verdict(kept) == "UNDERPOWERED"


def test_day_job_warms_the_lines_before_the_series():
    import early_entry_honest as E
    seen = {}

    def fake_lazy(day, prev):
        return {"iex": {"AAA": object()}, "sip": {}, "iex_prior": {}, "board": {"AAA": []}}

    def fake_series(frame, prev):
        seen["warm"] = E.WARM_LINES
        raise RuntimeError("stop after the flag check")

    old_lazy, old_series, old_flag = F.P.LazyDay, F.E.name_day_series, E.WARM_LINES
    F.P.LazyDay, F.E.name_day_series, E.WARM_LINES = fake_lazy, fake_series, False
    try:
        F.day_job("2026-05-01", "2026-04-30", {}, {})
    finally:
        F.P.LazyDay, F.E.name_day_series, E.WARM_LINES = old_lazy, old_series, old_flag
    assert seen["warm"] is True


def test_non_finite_features_are_not_measurable():
    assert not F._finite(float("nan"), 0.0, 0.5, 10.0)
    assert not F._finite(2.0, float("nan"), 0.5, 10.0)
    assert not F._finite(2.0, 0.0, 0.5, float("nan"))
    assert not F._finite(2.0, 0.0, 0.5, 0.0)
    assert F._finite(2.0, 0.0, 0.5, 10.0)


def test_history_is_one_warmup_session_plus_the_window():
    usable = ["2026-04-28", "2026-04-29"]
    warm = "2026-04-27"
    assert F.history_dates([warm] + usable, usable, warm) == [warm] + usable
    for bad in (
        ["2026-04-26", warm] + usable,
        [warm] + usable + ["2026-10-16"],
        [warm, "2026-04-28", "2026-05-01"],
        [warm, "2026-04-28"],
    ):
        try:
            F.history_dates(bad, usable, warm)
        except SystemExit:
            continue
        raise AssertionError(bad)
