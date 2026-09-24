"""Volume pace vs the stock's own normal: observe first, enforce later (2026-09-23)."""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_entry_watch as ew  # noqa: E402
from tests.test_ai_entry_watch import _armable_rec, _last_cfg  # noqa: E402

ET = ZoneInfo("America/New_York")


def _t(h, m):
    return datetime(2026, 9, 24, h, m, tzinfo=ET).timestamp()


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    ew._RVOL_PACE_CACHE.clear()
    ew._RVOL_OBS_LOGGED.clear()
    import ai_positions as cp
    logged = []
    monkeypatch.setattr(cp, "log_event", lambda kind, **f: logged.append((kind, f)) or {})
    yield logged
    ew._RVOL_PACE_CACHE.clear()
    ew._RVOL_OBS_LOGGED.clear()


def test_pace_is_volume_over_expected_share_of_normal():
    import tools.morning_funnel as mf
    now = _t(11, 16)                                   # data through 11:00 (16 min back)
    frac = mf.expected_fraction(90)
    got = ew.rvol_pace_sip("AAA", now=now, fetch=lambda s, te: (2_000_000 * frac, 1_000_000))
    assert got == pytest.approx(2.0)


def test_pace_is_none_before_sip_is_served():
    assert ew.rvol_pace_sip("AAA", now=_t(9, 40), fetch=lambda s, te: (1, 1)) is None


def _arm(monkeypatch, pace, **cfg):
    monkeypatch.setattr(ew, "rvol_pace_sip", lambda s, now=None: pace)
    rec = _armable_rec()
    return ew.should_arm_buy(rec, ask=10.0, bid=9.99, cfg=_last_cfg(**cfg)), rec


def test_observe_never_refuses_but_stamps_and_logs(monkeypatch, _clear):
    (ok, why), rec = _arm(monkeypatch, 0.5, ai_watch_rvol_pace_observe=True)
    assert why not in ("rvol_pace_low", "rvol_pace_unknown")
    if ok:   # only a real arm pass reaches the gate
        assert rec["rvol_pace_sip"] == 0.5
        kinds = [k for k, f in _clear]
        assert "rvol_pace_observe" in kinds
        f = dict(_clear)["rvol_pace_observe"]
        assert f["would_block"] is True and f["enforced"] is False


def test_observe_with_unknown_pace_still_arms(monkeypatch):
    (_ok, why), _rec = _arm(monkeypatch, None, ai_watch_rvol_pace_observe=True)
    assert why not in ("rvol_pace_low", "rvol_pace_unknown")


def test_enforce_refuses_low_and_unknown(monkeypatch):
    (ok, why), _ = _arm(monkeypatch, 1.2, ai_watch_min_rvol_pace=1.64)
    base_ok, base_why = ew.should_arm_buy(_armable_rec(), ask=10.0, bid=9.99, cfg=_last_cfg())
    if base_ok:
        assert (ok, why) == (False, "rvol_pace_low")
        assert _arm(monkeypatch, None, ai_watch_min_rvol_pace=1.64)[0] == (False, "rvol_pace_unknown")
        assert _arm(monkeypatch, 2.0, ai_watch_min_rvol_pace=1.64)[0][0] is True


def test_off_by_default_never_computes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("gate is off; must not fetch")
    monkeypatch.setattr(ew, "rvol_pace_sip", boom)
    ew.should_arm_buy(_armable_rec(), ask=10.0, bid=9.99, cfg=_last_cfg())


def test_entry_features_carry_the_pace():
    f = ew._entry_features({"symbol": "AAA", "rvol_pace_sip": 1.9})
    assert f["rvol_pace_sip"] == 1.9
