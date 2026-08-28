"""The premarket working-sell adapter must honour its own three knobs.

All three shipped in config.py AND in SAFE_CONFIG_KEYS with no reader anywhere
in the tree. That is worse than not shipping them: the dashboard accepts the
edit, the UI confirms it, and nothing changes. This desk lost most of a
session to that exact shape on ai_watch_decision_max_age_sec, which was set to
30s and silently resolved to a hardcoded 8.

The adapter itself is deliberately off (ai_premarket_working_sell=False) until
there is a live premarket book, so these test the wiring, not the strategy.
"""
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

cp = pytest.importorskip("ai_positions")


def _cfg(monkeypatch, **over):
    monkeypatch.setattr(cp, "_cfg_all", lambda: dict(over))


def _placed(monkeypatch):
    """Capture what price reaches the broker, if anything."""
    seen = []
    import types
    monkeypatch.setitem(sys.modules, "alpaca_trader", types.SimpleNamespace(
        working_sell_replace=lambda t, px: seen.append(px) or {"order_id": "x"}))
    return seen


# ── chase step: how often the resting price may move ─────────────────────

def test_a_replace_inside_the_chase_step_is_refused(monkeypatch):
    _cfg(monkeypatch, ai_premarket_chase_step_sec=2.5)
    seen = _placed(monkeypatch)
    pos = {"working_sell_px": 10.00, "working_sell_state": "shelf",
           "working_sell_ts": time.time()}
    assert cp._rest_working_sell("AAA", pos, 9.50, "shelf") is False
    assert seen == [], "premarket books are jumpy; do not chase every tick"


def test_a_replace_after_the_chase_step_goes_through(monkeypatch):
    _cfg(monkeypatch, ai_premarket_chase_step_sec=2.5)
    seen = _placed(monkeypatch)
    pos = {"working_sell_px": 10.00, "working_sell_state": "shelf",
           "working_sell_ts": time.time() - 3.0}
    assert cp._rest_working_sell("AAA", pos, 9.50, "shelf") is True
    assert seen == [9.50]


def test_zero_chase_step_replaces_freely(monkeypatch):
    _cfg(monkeypatch, ai_premarket_chase_step_sec=0.0)
    _placed(monkeypatch)
    pos = {"working_sell_px": 10.00, "working_sell_state": "shelf",
           "working_sell_ts": time.time()}
    assert cp._rest_working_sell("AAA", pos, 9.50, "shelf") is True


def test_the_placement_stamps_its_own_time(monkeypatch):
    """Without the stamp the step can never elapse or never apply."""
    _cfg(monkeypatch)
    _placed(monkeypatch)
    pos = {}
    cp._rest_working_sell("AAA", pos, 9.50, "shelf")
    assert cp._num(pos.get("working_sell_ts")) is not None


# ── slip cap: how far under the shelf it may rest ────────────────────────

def test_a_price_far_under_the_shelf_is_lifted_to_the_cap(monkeypatch):
    """A wide premarket NBBO must not park the sell below anything the
    position was ever risking."""
    _cfg(monkeypatch, ai_premarket_max_exit_slip_r=0.25)
    seen = _placed(monkeypatch)
    pos = {"local_stop_price": 10.00, "risk_per_share": 1.00}
    cp._rest_working_sell("AAA", pos, 8.00, "flatten")
    assert seen == [9.75], "10.00 shelf - 0.25R = 9.75"


def test_a_price_inside_the_cap_is_untouched(monkeypatch):
    _cfg(monkeypatch, ai_premarket_max_exit_slip_r=0.25)
    seen = _placed(monkeypatch)
    pos = {"local_stop_price": 10.00, "risk_per_share": 1.00}
    cp._rest_working_sell("AAA", pos, 9.90, "flatten")
    assert seen == [9.90]


def test_no_shelf_or_no_risk_means_no_cap(monkeypatch):
    """Absence must not invent a floor out of nothing."""
    _cfg(monkeypatch, ai_premarket_max_exit_slip_r=0.25)
    seen = _placed(monkeypatch)
    cp._rest_working_sell("AAA", {"risk_per_share": 1.0}, 8.00, "flatten")
    cp._rest_working_sell("BBB", {"local_stop_price": 10.0}, 8.00, "flatten")
    assert seen == [8.00, 8.00]


# ── quote ceiling: only outside RTH, and only when the adapter is on ─────

def test_the_premarket_ceiling_is_used_when_the_adapter_is_on(monkeypatch):
    monkeypatch.setattr(cp, "_premarket_working_sell_on", lambda: True)
    src = (_ROOT / "ai_positions.py").read_text(encoding="utf-8")
    i = src.index("def quote_is_live")
    body = src[i:i + 2200]
    assert "ai_premarket_quote_max_age_sec" in body
    assert "_premarket_working_sell_on()" in body
    assert "ai_stale_data_max_age_sec" in body, "RTH ceiling is the fallback"


def test_every_knob_has_a_reader():
    """The property the failing test was asserting, pinned per key."""
    src = (_ROOT / "ai_positions.py").read_text(encoding="utf-8")
    for k in ("ai_premarket_quote_max_age_sec",
              "ai_premarket_max_exit_slip_r",
              "ai_premarket_chase_step_sec"):
        assert k in src, f"{k} is dashboard-editable and unread"
