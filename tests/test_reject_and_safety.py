"""Rejected-candidate logging and the unprotected-position invariant.

Both exist because of gaps 2026-08-06 exposed:
  • admission computed a reject list and discarded it, so a filter could only
    ever be observed on what it passed — which cannot tell a gate that removes
    losers from one that removes winners;
  • a naked 83%-of-equity position was caught by a human looking at the
    screen, while reconcile_unmanaged fired 384 times and aggregated to
    nothing.
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
import ai_positions as cp  # noqa: E402


# ── reject sampling ──────────────────────────────────────────────────────

def _rows(tmp_path, monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr(cp, "log_reject_sample", lambda r: written.append(r))
    ew._reject_last_logged.clear()
    return written


def test_rejects_are_logged_with_the_features_they_were_rejected_with(
        tmp_path, monkeypatch):
    written = _rows(tmp_path, monkeypatch)
    by_symbol = {"AAA": {"symbol": "AAA", "source": "trending", "price": 12.5,
                         "score": 14.0, "rvol": 0.9, "pct_change": 3.0,
                         "look_reason": "WASH", "criteria": ["score"]}}
    ew._log_rejects([{"symbol": "AAA", "reason": "thin_rvol",
                      "criteria": ["score"]}], by_symbol, {}, now=1000.0)

    assert len(written) == 1
    r = written[0]
    assert r["symbol"] == "AAA" and r["reason"] == "thin_rvol"
    # Price and features must ride along — a reject with no price cannot be
    # scored against what it did next.
    assert r["price"] == 12.5 and r["rvol"] == 0.9
    assert r["source"] == "trending" and r["look_reason"] == "WASH"


def test_dwell_is_not_recorded_as_a_rejection(tmp_path, monkeypatch):
    """A name mid-admission was not turned away. Logging it would pollute the
    reject arm with names that go on to be admitted seconds later."""
    written = _rows(tmp_path, monkeypatch)
    ew._log_rejects([{"symbol": "AAA", "reason": "dwell_1/2"}],
                    {"AAA": {"symbol": "AAA", "price": 1.0}}, {}, now=1000.0)
    assert written == []


def test_reject_logging_is_throttled_per_symbol(tmp_path, monkeypatch):
    """The book rebuilds every 2s. Unthrottled this writes ~15k rows an hour
    and says nothing a 60s series does not."""
    written = _rows(tmp_path, monkeypatch)
    rej = [{"symbol": "AAA", "reason": "not_uptrend"}]
    by = {"AAA": {"symbol": "AAA", "price": 5.0}}

    ew._log_rejects(rej, by, {}, now=1000.0)
    ew._log_rejects(rej, by, {}, now=1002.0)   # 2s later — same sync loop
    ew._log_rejects(rej, by, {}, now=1030.0)   # still inside the window
    assert len(written) == 1

    ew._log_rejects(rej, by, {}, now=1000.0 + 61)
    assert len(written) == 2, "throttle must expire so a series accrues"


def test_reject_logging_can_be_disabled(tmp_path, monkeypatch):
    written = _rows(tmp_path, monkeypatch)
    ew._log_rejects([{"symbol": "AAA", "reason": "not_uptrend"}],
                    {"AAA": {"symbol": "AAA", "price": 5.0}},
                    {"ai_reject_log_enabled": False}, now=1000.0)
    assert written == []


# ── unprotected-position invariant ───────────────────────────────────────

class _FakeAT:
    def __init__(self, positions, orders, equity=10_000.0):
        self._p, self._o, self._e = positions, orders, equity

    def get_positions_detail(self):
        return self._p

    def get_open_orders(self, limit=100):
        return self._o

    def get_equity(self):
        return self._e


def _reconcile(monkeypatch, tmp_path, positions, orders, state=None):
    monkeypatch.setattr(cp, "POSITIONS_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(cp, "EVENTS_PATH", tmp_path / "events.jsonl")
    (tmp_path / "state.json").write_text(json.dumps(state or {}))
    monkeypatch.setitem(sys.modules, "alpaca_trader",
                        _FakeAT(positions, orders))
    return cp.reconcile_broker(now=1000.0)


def test_position_with_no_resting_sell_is_flagged(tmp_path, monkeypatch):
    """The CELH shape: a live position, no protective order, huge relative to
    equity, and not in managed state."""
    rep = _reconcile(
        monkeypatch, tmp_path,
        positions={"CELH": {"qty": 353.0, "mkt_val": 8403.0}},
        orders=[],
    )
    assert len(rep["unprotected"]) == 1
    u = rep["unprotected"][0]
    assert u["symbol"] == "CELH"
    assert u["pct_equity"] == 84.0, "concentration is why this is urgent"
    assert u["managed"] is False


def test_position_with_a_resting_sell_is_not_flagged(tmp_path, monkeypatch):
    rep = _reconcile(
        monkeypatch, tmp_path,
        positions={"CELH": {"qty": 353.0, "mkt_val": 8403.0}},
        orders=[{"symbol": "CELH", "side": "sell", "type": "stop"}],
    )
    assert rep["unprotected"] == []


def test_take_profit_limit_alone_is_not_protection(tmp_path, monkeypatch):
    """RIOT shape: upper limit resting, no stop — still unprotected."""
    rep = _reconcile(
        monkeypatch, tmp_path,
        positions={"RIOT": {"qty": 100.0, "mkt_val": 1200.0}},
        orders=[{"symbol": "RIOT", "side": "sell", "type": "limit"}],
    )
    assert [u["symbol"] for u in rep["unprotected"]] == ["RIOT"]


def test_stop_limit_sell_counts_as_protection(tmp_path, monkeypatch):
    rep = _reconcile(
        monkeypatch, tmp_path,
        positions={"RIOT": {"qty": 100.0, "mkt_val": 1200.0}},
        orders=[{"symbol": "RIOT", "side": "sell", "type": "stop_limit"}],
    )
    assert rep["unprotected"] == []


def test_a_resting_buy_does_not_count_as_protection(tmp_path, monkeypatch):
    """Only a SELL exits a long. An open buy is the opposite of protection."""
    rep = _reconcile(
        monkeypatch, tmp_path,
        positions={"CELH": {"qty": 353.0, "mkt_val": 8403.0}},
        orders=[{"symbol": "CELH", "side": "buy", "type": "limit"}],
    )
    assert [u["symbol"] for u in rep["unprotected"]] == ["CELH"]


def test_flat_account_reports_nothing(tmp_path, monkeypatch):
    rep = _reconcile(monkeypatch, tmp_path, positions={}, orders=[])
    assert rep["unprotected"] == [] and rep["unmanaged"] == []
