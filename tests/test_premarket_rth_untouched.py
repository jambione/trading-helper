"""Premarket readiness merge (11/04): the Phase B exit and sizing changes must never act in the regular session."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_positions as cp  # noqa: E402


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("pos", [None, {}, {"phase_b": True}, {"phase_b": True, "entry_price": 10.0}])
def test_working_sell_is_off_during_the_regular_session(monkeypatch, flag, pos):
    monkeypatch.setattr(cp, "_rth_now", lambda ts: True)
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=False: flag if k == "ai_premarket_working_sell" else d)
    assert cp._premarket_working_sell_on(pos=pos) is False
    assert cp._premarket_working_sell_on(pos) is False
    assert cp._premarket_working_sell_on() is False


def test_outside_rth_a_plain_position_follows_the_old_flag(monkeypatch):
    monkeypatch.setattr(cp, "_rth_now", lambda ts: False)
    for flag in (False, True):
        monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=False, f=flag: f if k == "ai_premarket_working_sell" else d)
        assert cp._premarket_working_sell_on(pos={}) is flag
        assert cp._premarket_working_sell_on() is flag


def test_phase_b_rails_are_fingerprinted():
    import learn_stamps
    for k in ("ai_phase_b_max_position_pct", "ai_phase_b_max_position_dollars", "ai_phase_b_risk_pct",
              "ai_phase_b_max_spread_pct", "ai_phase_b_lockout_on_loss", "ai_phase_b_max_entries_per_symbol",
              "ai_phase_b_min_prints_in_window"):
        assert k in learn_stamps._FINGERPRINT_KEYS
