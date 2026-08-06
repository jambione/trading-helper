"""One box owns the account.

Both machines ran against the same Alpaca paper key. Risk caps
(ai_max_positions, ai_max_open_risk_pct) are enforced per-instance, so real
exposure could be double what either box believed; liquidate_all closes EVERY
position in the account, so whichever box reached 15:50 first flattened the
other's book; and 3 of the 4 rows in outcomes.jsonl were the other machine's
trades, which is why no aggregate could be trusted (roadmap P0-1).

The operator decided on 2026-08-06 that only the Mac mini trades. Enforcing it
in the repo means the other box self-disables on pull, instead of depending on
nobody starting it.
"""

import socket
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import alpaca_trader as at  # noqa: E402


@pytest.fixture(autouse=True)
def _restore():
    orig_host = at._host_allowed
    orig_prot = at._require_protective_exit
    yield
    at._host_allowed = orig_host
    at._require_protective_exit = orig_prot


@pytest.fixture
def armed(monkeypatch):
    submitted = []

    class _Order:
        id = "oid"
        status = "accepted"

    class _Client:
        def submit_order(self, req):
            submitted.append(req)
            return _Order()

        def get_all_positions(self):
            return []

        def close_all_positions(self, cancel_orders=True):
            submitted.append("close_all")
            return []

    monkeypatch.setattr(at, "_client", _Client())
    monkeypatch.setattr(at, "_mode", "paper")
    monkeypatch.setattr(at, "_log_action", lambda *a, **k: None)
    monkeypatch.setattr(at, "_require_protective_exit", lambda: False)
    return submitted


def _set_host(monkeypatch, value):
    monkeypatch.setattr(at, "_host_allowed",
                        at._host_allowed.__wrapped__
                        if hasattr(at._host_allowed, "__wrapped__")
                        else at._host_allowed)
    import config
    real = config.load_config

    def fake():
        cfg = dict(real())
        cfg["ai_trading_host"] = value
        return cfg

    monkeypatch.setattr(config, "load_config", fake)


# ── the guard itself ─────────────────────────────────────────────────────

def test_unset_means_unrestricted(monkeypatch):
    """Single-box setups must be unaffected — this cannot become a change that
    quietly stops an existing desk trading."""
    _set_host(monkeypatch, "")
    assert at._host_allowed() is True


def test_matching_host_is_allowed(monkeypatch):
    _set_host(monkeypatch, socket.gethostname())
    assert at._host_allowed() is True


def test_local_suffix_is_normalised(monkeypatch):
    """socket.gethostname() returns "x.local" while scutil --get LocalHostName
    returns "x". Either spelling must be accepted or the owner box silently
    stops trading."""
    base = socket.gethostname().removesuffix(".local")
    _set_host(monkeypatch, base)
    assert at._host_allowed() is True
    _set_host(monkeypatch, base + ".local")
    assert at._host_allowed() is True


def test_other_host_is_refused(monkeypatch):
    _set_host(monkeypatch, "some-other-machine")
    assert at._host_allowed() is False


def test_unreadable_config_refuses(monkeypatch):
    """Fails toward refusing: a desk that stops is visible and recoverable, one
    trading from two places silently corrupts both books."""
    import config
    monkeypatch.setattr(config, "load_config",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert at._host_allowed() is False


# ── what the guard actually blocks ───────────────────────────────────────

def test_a_foreign_host_cannot_buy(armed, monkeypatch):
    monkeypatch.setattr(at, "_host_allowed", lambda: False)
    out = at.buy_limit_bracket("CELH", 100, 23.76, 22.50, 25.00)
    assert out["ok"] is False
    assert armed == []


def test_a_foreign_host_cannot_liquidate_the_account(armed, monkeypatch):
    """The sharpest edge of P0-1: liquidate_all closes EVERY position in the
    account, so an unowned box hitting its own 15:50 flattens the owner's book."""
    monkeypatch.setattr(at, "_host_allowed", lambda: False)
    out = at.liquidate_all()
    assert out.get("ok") is False
    assert armed == [], "flattened an account this machine does not own"


def test_a_foreign_host_cannot_cancel_orders(armed, monkeypatch):
    monkeypatch.setattr(at, "_host_allowed", lambda: False)
    out = at.cancel_open_orders("CELH")
    assert out.get("ok") is False
    assert armed == []


def test_reads_still_work_on_a_foreign_host(armed, monkeypatch):
    """A dev box must still SEE the account — it just must not act. Gating
    is_active() itself would blind its dashboard entirely."""
    monkeypatch.setattr(at, "_host_allowed", lambda: False)
    assert at.is_active() is True
    # get_open_positions reads through the client; it must not be refused.
    assert at.get_open_positions() is not None


def test_the_owner_host_still_trades(armed, monkeypatch):
    monkeypatch.setattr(at, "_host_allowed", lambda: True)
    out = at.buy_limit_bracket("CELH", 100, 23.76, 22.50, 25.00)
    assert out["ok"] is True and len(armed) == 1
