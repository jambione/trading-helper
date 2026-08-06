"""No protective exit, no position.

2026-08-06: desk_buy_bracket's bracket was rejected ("bracket orders do not
support extended hours trading"), the caller fell back to a bare limit, and
353 shares of CELH — $8.4k, 83% of account equity — opened with no stop. It
sat naked for 44 minutes until closed by hand. ALOY and XNDU took the same
path on 08-04.

Fixing the one caller was not enough: every bare buy helper in alpaca_trader
is reachable from somewhere (desk_buy, desk_buy_policy, ai_trading.buy_stock),
so the rule is enforced at the broker layer where nothing can route around it.

The counterweight these tests protect: risk-sized entries must still work.
Blocking them would trade one failure for another.
"""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import alpaca_trader as at  # noqa: E402


@pytest.fixture
def armed(monkeypatch):
    """Trader active, protection required, order submission observable."""
    submitted = []

    class _FakeOrder:
        id = "oid-test"
        status = "accepted"

    class _FakeClient:
        def submit_order(self, req):
            submitted.append(req)
            return _FakeOrder()

    monkeypatch.setattr(at, "_client", _FakeClient())
    monkeypatch.setattr(at, "_mode", "paper")
    monkeypatch.setattr(at, "_log_action", lambda *a, **k: None)
    monkeypatch.setattr(at, "_require_protective_exit", lambda: True)
    return submitted


def test_bare_limit_is_refused(armed):
    out = at.buy_limit_at_price("CELH", 23.76, 1000.0)
    assert out["ok"] is False
    assert out["status"] == "unprotected"
    assert armed == [], "submitted an order it should have refused"


def test_bare_market_is_refused(armed):
    out = at.buy_market_shares("CELH", 23.76, 1000.0)
    assert out["ok"] is False and out["status"] == "unprotected"
    assert armed == []


def test_limit_at_ask_is_refused_because_it_delegates(armed):
    """buy_limit_at_ask only computes a price and calls buy_limit_at_price —
    its docstring says "no brackets" outright. One guard must cover both."""
    out = at.buy_limit_at_ask("CELH", 23.76, 1000.0, pad_pct=0.1)
    assert out["ok"] is False and out["status"] == "unprotected"
    assert armed == []


def test_extended_hours_buy_is_refused(armed, monkeypatch):
    """The exact CELH shape: Alpaca will not accept a bracket outside RTH, so
    this branch cannot protect the position and must not open one."""
    monkeypatch.setattr(at, "_extended_hours", True)
    out = at.buy("CELH", 23.76, 0.0, 0.0)
    assert out["ok"] is False and out["status"] == "unprotected"
    assert "extended_hours" in out["note"]
    assert armed == []


def test_plain_market_buy_is_refused_when_brackets_unconfigured(armed, monkeypatch):
    monkeypatch.setattr(at, "_extended_hours", False)
    monkeypatch.setattr(at, "_use_brackets", False)
    out = at.buy("CELH", 23.76, 0.0, 0.0)
    assert out["ok"] is False and out["status"] == "unprotected"
    assert armed == []


def test_bracketed_buy_still_goes_through(armed, monkeypatch):
    """The counterweight. A buy that DOES carry stop and target must not be
    blocked — otherwise this fix simply stops the desk trading."""
    monkeypatch.setattr(at, "_extended_hours", False)
    monkeypatch.setattr(at, "_use_brackets", True)
    monkeypatch.setattr(at, "_stop_loss_pct", 4.0)
    monkeypatch.setattr(at, "_take_profit_pct", 15.0)
    monkeypatch.setattr(at, "_trade_amount", 1000.0)

    out = at.buy("CELH", 23.76, 0.0, 0.0)
    assert out["ok"] is True, "blocked a properly bracketed entry"
    assert len(armed) == 1
    req = armed[0]
    assert getattr(req, "take_profit", None) is not None
    assert getattr(req, "stop_loss", None) is not None


def test_guard_can_be_disabled_deliberately(armed, monkeypatch):
    """Escape hatch exists, but only by explicit config — never by accident."""
    monkeypatch.setattr(at, "_require_protective_exit", lambda: False)
    out = at.buy_limit_at_price("CELH", 23.76, 1000.0)
    assert out["ok"] is True and len(armed) == 1


def test_guard_defaults_to_on_when_config_is_unreadable(monkeypatch):
    """Fails safe: a missed trade is cheaper than an unhedged account."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "config":
            raise RuntimeError("config unreadable")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    assert at._require_protective_exit() is True
