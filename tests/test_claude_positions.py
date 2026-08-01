"""Risk-sized entries and mechanical position management.

Everything after entry (stop, scale-out, trailing stop, time stop) is meant
to be enforced by real broker orders and local state, never by asking Claude
again — these tests exercise that state machine against a stubbed
alpaca_trader, without any live Alpaca or Claude CLI call.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

import alpaca_trader  # noqa: E402
import claude_positions as cp  # noqa: E402


def _state_path(tmp_path):
    return tmp_path / "positions_state.json"


def _outcomes_path(tmp_path):
    return tmp_path / "outcomes.jsonl"


def _use_tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "POSITIONS_STATE_PATH", _state_path(tmp_path))
    monkeypatch.setattr(cp, "OUTCOMES_PATH", _outcomes_path(tmp_path))


# ── size_by_risk (alpaca_trader) ─────────────────────────────────────────────

def test_size_by_risk_caps_loss_to_the_stated_percent():
    # $50,000 equity, 1% risk = $500 max loss; $2 risk/share -> 250 shares.
    qty = alpaca_trader.size_by_risk(50_000, 1.0, entry=42.0, stop=40.0)
    assert qty == 250
    assert qty * (42.0 - 40.0) <= 500.0


def test_size_by_risk_rejects_undefined_or_backwards_risk():
    # No defined loss (stop >= entry) must not produce a share count.
    assert alpaca_trader.size_by_risk(50_000, 1.0, entry=40.0, stop=42.0) == 0
    assert alpaca_trader.size_by_risk(50_000, 1.0, entry=40.0, stop=40.0) == 0
    assert alpaca_trader.size_by_risk(0, 1.0, entry=40.0, stop=38.0) == 0
    assert alpaca_trader.size_by_risk(50_000, 1.0, entry=0.0, stop=0.0) == 0


# ── prompt template ───────────────────────────────────────────────────────────

def test_entry_prompt_fills_every_placeholder():
    prompt = cp.build_entry_prompt(
        "nvda", 121.50, 50_000.0, reason="AI infra momentum", risk_pct=1.0)
    assert "NVDA" in prompt
    assert "$121.50" in prompt
    assert "$50000.00" in prompt
    assert "1%" in prompt
    assert "AI infra momentum" in prompt
    # The mandatory rules from the user's prompt must survive verbatim intent.
    assert "Hard stop-loss" in prompt
    assert "Scale out" in prompt
    assert "Trailing stop" in prompt
    assert "Time stop" in prompt
    assert "Thesis break" in prompt
    assert "averaging down" in prompt


# ── parsing the entry decision ────────────────────────────────────────────────

def test_parse_entry_decision_extracts_the_json_object():
    text = """
    Here is my analysis of NVDA...

    {
      "decision": "BUY",
      "entry_low": 120.0,
      "entry_high": 122.0,
      "stop_price": 115.0,
      "target_1": 135.0,
      "target_2": 150.0,
      "scale_out_pct": 40,
      "trail_method": "20d_ma",
      "trail_pct": 8.0,
      "time_stop_days": 10,
      "reward_risk": 3.0,
      "summary": "Clean breakout with defined risk."
    }

    This setup offers a strong reward-to-risk profile.
    """
    decision = cp.parse_entry_decision(text)
    assert decision["decision"] == "BUY"
    assert decision["stop_price"] == 115.0
    assert decision["reward_risk"] == 3.0


def test_parse_entry_decision_returns_none_without_a_decision_key():
    assert cp.parse_entry_decision("just prose, no JSON at all") is None
    assert cp.parse_entry_decision('{"suggestions": []}') is None


# ── qualification gate ────────────────────────────────────────────────────────

def _buy_decision(**over):
    d = {
        "decision": "BUY", "entry_low": 40.0, "entry_high": 41.0,
        "stop_price": 38.0, "target_1": 46.0, "reward_risk": 3.0,
    }
    d.update(over)
    return d


def test_qualifies_rejects_wait():
    assert cp.qualifies_as_entry({"decision": "WAIT"}) is False
    assert cp.qualifies_as_entry(None) is False


def test_qualifies_rejects_undefined_risk():
    # Never a trade with undefined risk: stop must be below entry.
    assert cp.qualifies_as_entry(_buy_decision(stop_price=41.0)) is False
    assert cp.qualifies_as_entry(_buy_decision(stop_price=0)) is False
    assert cp.qualifies_as_entry(_buy_decision(target_1=0)) is False


def test_qualifies_enforces_minimum_reward_risk():
    assert cp.qualifies_as_entry(_buy_decision(reward_risk=2.0),
                                 min_reward_risk=3.0) is False
    assert cp.qualifies_as_entry(_buy_decision(reward_risk=3.0),
                                 min_reward_risk=3.0) is True


# ── placing the scaled entry (stubbed broker) ────────────────────────────────

class _StubBroker:
    """Records every bracket call instead of hitting a real Alpaca client."""

    def __init__(self, market_open=True):
        self.calls: list[dict] = []
        self._next_id = 1
        self._market_open = market_open

    def market_is_open(self):
        return self._market_open

    def size_by_risk(self, equity, risk_pct, entry, stop):
        return alpaca_trader.size_by_risk(equity, risk_pct, entry, stop)

    def buy_bracket_exact(self, ticker, qty, stop_price, target_price=None):
        oid = f"order_{self._next_id}"
        self._next_id += 1
        self.calls.append({"ticker": ticker, "qty": qty,
                           "stop_price": stop_price, "target_price": target_price})
        return {"ok": True, "buy_order_id": oid,
                "stop_order_id": (None if target_price else f"stop_{oid}"),
                "status": "accepted"}


def test_place_scaled_entry_splits_into_two_tranches_by_scale_out_pct(
        tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    stub = _StubBroker()
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    decision = _buy_decision(scale_out_pct=40)
    # Sized against the actual fill price (40.5), not the entry-zone bound:
    # $500 risk / $2.50 per-share risk = 200 shares total; 40% -> 80 / 120.
    result = cp.place_scaled_entry("nvda", decision, account_equity=50_000.0,
                                   risk_pct=1.0, current_ask=40.5)

    assert result["ok"] is True
    assert result["qty_a"] == 80
    assert result["qty_b"] == 120
    assert len(stub.calls) == 2
    tranche_a, tranche_b = stub.calls
    assert tranche_a["target_price"] == 46.0   # carries the first target
    assert tranche_b["target_price"] is None   # rides with a stop only

    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["qty_a"] == 80
    assert state["NVDA"]["qty_b"] == 120
    assert state["NVDA"]["breakeven_done"] is False


def test_place_scaled_entry_refuses_to_order_brackets_outside_market_hours(
        tmp_path, monkeypatch):
    """Alpaca rejects bracket (and plain market) orders outside RTH — two of
    the three scheduled research times are pre-market, so a BUY verdict then
    must be discarded, not attempted and left half-placed."""
    _use_tmp_state(tmp_path, monkeypatch)
    stub = _StubBroker(market_open=False)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    result = cp.place_scaled_entry("nvda", _buy_decision(),
                                   account_equity=50_000.0, current_ask=40.5)
    assert result["ok"] is False
    assert "market is closed" in result["error"]
    assert stub.calls == []


def test_place_scaled_entry_refuses_price_outside_the_entry_zone(
        tmp_path, monkeypatch):
    """Prefer missing a move over taking a low-quality setup — if price left
    the zone before the order could go in, don't chase it."""
    _use_tmp_state(tmp_path, monkeypatch)
    stub = _StubBroker()
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    decision = _buy_decision()  # entry_low=40, entry_high=41
    result = cp.place_scaled_entry("nvda", decision, account_equity=50_000.0,
                                   current_ask=44.0)
    assert result["ok"] is False
    assert "entry zone" in result["error"]
    assert stub.calls == []


# ── mechanical position management (no LLM) ─────────────────────────────────

class _StubBrokerManage:
    def __init__(self, order_status="new", position_open=True, current_price=44.0):
        self.order_status = order_status
        self.position_open = position_open
        self.current_price = current_price
        self.replace_calls: list[dict] = []
        self.closed: list[str] = []
        self.canceled: list[str] = []

    def get_positions_detail(self):
        return {"NVDA": {"current": self.current_price}} if self.position_open else {}

    def get_order(self, order_id):
        return {"status": self.order_status}

    def replace_stop(self, ticker, old_stop_order_id, *, trail_percent=None,
                     stop_price=None):
        self.replace_calls.append({
            "ticker": ticker, "old": old_stop_order_id,
            "trail_percent": trail_percent, "stop_price": stop_price,
        })
        return {"ok": True, "order_id": "new_stop_1", "status": "accepted"}

    def cancel_open_orders(self, ticker):
        self.canceled.append(ticker)
        return {"ok": True, "canceled": 1}

    def close_out(self, ticker):
        self.closed.append(ticker)
        return {"ok": True, "order_id": "close_1"}


def _seed_state(tmp_path, monkeypatch, **fields):
    _use_tmp_state(tmp_path, monkeypatch)
    base = {
        "qty_a": 100, "qty_b": 150, "total_qty": 250,
        "entry_price": 40.5,
        "tranche_a_order_id": "order_a", "tranche_b_stop_order_id": "stop_b",
        "stop_price": 38.0, "target_1": 46.0, "trail_pct": 8.0,
        "time_stop_days": 10, "entry_time": 1_000_000.0,
        "tranche_a_filled": False, "breakeven_done": False,
        "reward_risk": 3.0, "summary": "test thesis",
        "entry_confirmed": True, "last_seen_price": None,
        "closing_reason": None,
    }
    base.update(fields)
    _state_path(tmp_path).write_text(json.dumps({"NVDA": base}))


def test_tranche_a_fill_replaces_tranche_b_stop_with_trailing_stop(
        tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch)
    stub = _StubBrokerManage(order_status="filled")
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_100.0)

    assert len(events) == 1
    assert events[0]["event"] == "scaled_out"
    assert stub.replace_calls[0]["trail_percent"] == 8.0

    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["breakeven_done"] is True
    assert state["NVDA"]["tranche_a_filled"] is True


def test_no_action_while_tranche_a_order_is_still_open(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch)
    stub = _StubBrokerManage(order_status="new")
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_100.0)
    assert events == []
    assert stub.replace_calls == []

    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["breakeven_done"] is False


def test_time_stop_marks_closing_then_records_outcome_once_flat(
        tmp_path, monkeypatch):
    """close_out submits an exit order — it doesn't guarantee an instant
    fill, so the position stays tracked (marked closing) until a later tick
    observes it's actually gone, at which point the outcome is recorded."""
    _seed_state(tmp_path, monkeypatch, time_stop_days=10, entry_time=1_000_000.0)
    stub = _StubBrokerManage(order_status="new", position_open=True)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    # 11 days later — deadline passed with target 1 never hit.
    later = 1_000_000.0 + 11 * 86400
    events = cp.manage_open_positions(now=later)
    assert len(events) == 1
    assert events[0]["event"] == "time_stop"
    assert stub.closed == ["NVDA"]

    # Exit order submitted but not necessarily filled yet — still tracked.
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["closing_reason"] == "time_stop"

    # Next tick: the close actually took.
    stub.position_open = False
    events2 = cp.manage_open_positions(now=later + 5.0)
    assert len(events2) == 1
    assert events2[0]["event"] == "closed"
    assert events2[0]["close_reason"] == "time_stop"
    assert json.loads(_state_path(tmp_path).read_text()) == {}

    outcome = json.loads(_outcomes_path(tmp_path).read_text().strip())
    # Exit priced at the last observed mark (44.0, from the first tick).
    assert outcome["close_reason"] == "time_stop"
    assert outcome["exit_price_approx"] == 44.0
    assert outcome["realized_r_multiple"] == (44.0 - 40.5) / (40.5 - 38.0)
    assert outcome["realized_pl_usd"] == (44.0 - 40.5) * 250


def test_time_stop_does_not_fire_once_target_already_filled(tmp_path, monkeypatch):
    """A position that already scaled out is winning — the time stop is for
    trades still waiting to prove themselves, not ones that already did."""
    _seed_state(tmp_path, monkeypatch, time_stop_days=10,
                entry_time=1_000_000.0, tranche_a_filled=True,
                breakeven_done=True)
    stub = _StubBrokerManage(order_status="filled")
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    later = 1_000_000.0 + 30 * 86400
    events = cp.manage_open_positions(now=later)
    assert events == []
    assert stub.closed == []


def test_closure_detection_ignores_a_not_yet_confirmed_entry(tmp_path, monkeypatch):
    """An order that hasn't filled yet must not be mistaken for a closed
    position — that would record a bogus outcome the instant a trade opens."""
    _seed_state(tmp_path, monkeypatch, entry_confirmed=False)
    stub = _StubBrokerManage(position_open=False)  # entry order not filled yet
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_010.0)
    assert events == []
    state = json.loads(_state_path(tmp_path).read_text())
    assert "NVDA" in state
    assert not (_outcomes_path(tmp_path)).exists()


def test_hard_stop_closure_with_no_scale_out_is_labeled_stopped_out(
        tmp_path, monkeypatch):
    """No explicit closing_reason and tranche A never filled -> the only
    thing that can have closed both tranches together is the original stop."""
    _seed_state(tmp_path, monkeypatch, tranche_a_filled=False,
                last_seen_price=39.0)
    stub = _StubBrokerManage(position_open=False)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_010.0)
    assert events == [{"ticker": "NVDA", "event": "closed",
                       "close_reason": "stopped_out"}]
    outcome = json.loads(_outcomes_path(tmp_path).read_text().strip())
    assert outcome["realized_r_multiple"] < 0  # a loss, priced at the stop


# ── thesis-break review folded into the shared research call ────────────────

def test_holdings_review_snippet_is_empty_with_nothing_held(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    assert cp.build_holdings_review_snippet() == ""


def test_holdings_review_snippet_lists_held_positions(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch)

    class _StubDetail:
        def get_positions_detail(self):
            return {"NVDA": {"current": 44.0, "plpc": 5.2}}

    monkeypatch.setitem(sys.modules, "alpaca_trader", _StubDetail())
    snippet = cp.build_holdings_review_snippet()
    assert "NVDA" in snippet
    assert "test thesis" in snippet
    assert "position_reviews" in snippet


def test_apply_position_reviews_marks_only_flagged_symbols_for_closure(
        tmp_path, monkeypatch):
    """close_out submits an exit order but doesn't guarantee an instant fill,
    so this marks closing_reason rather than deleting outright — the outcome
    and final state cleanup happen once manage_open_positions sees it flat."""
    _use_tmp_state(tmp_path, monkeypatch)
    _state_path(tmp_path).write_text(json.dumps({
        "NVDA": {"summary": "held 1", "closing_reason": None},
        "AMD": {"summary": "held 2", "closing_reason": None},
    }))
    stub = _StubBrokerManage()
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.apply_position_reviews([
        {"symbol": "NVDA", "action": "exit", "reason": "guidance cut"},
        {"symbol": "AMD", "action": "hold", "reason": "thesis intact"},
    ])

    assert len(events) == 1
    assert events[0]["ticker"] == "NVDA"
    assert stub.closed == ["NVDA"]

    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["closing_reason"] == "thesis_break"
    assert state["AMD"]["closing_reason"] is None


def test_apply_position_reviews_does_not_re_exit_an_already_closing_position(
        tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    _state_path(tmp_path).write_text(json.dumps({
        "NVDA": {"summary": "held", "closing_reason": "time_stop"},
    }))
    stub = _StubBrokerManage()
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.apply_position_reviews(
        [{"symbol": "NVDA", "action": "exit", "reason": "also broken"}])
    assert events == []
    assert stub.closed == []


# ── performance_summary aggregation ──────────────────────────────────────────

def _write_outcome(tmp_path, **fields):
    base = {
        "ts": 1_000_000.0, "symbol": "TEST", "entry_price": 40.0,
        "stop_price": 38.0, "target_1": 46.0, "total_qty": 100,
        "exit_price_approx": 44.0, "realized_r_multiple": 2.0,
        "realized_pl_usd": 400.0, "close_reason": "trailed_out",
        "scaled_out": True, "entry_time": 900_000.0, "exit_time": 1_000_000.0,
        "hold_days": 1.16, "reward_risk_planned": 3.0, "summary": "thesis",
    }
    base.update(fields)
    path = _outcomes_path(tmp_path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(base) + "\n")


def test_performance_summary_with_no_outcomes_yet(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    assert cp.performance_summary() == {"count": 0}


def test_performance_summary_computes_win_rate_and_avg_r(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    _write_outcome(tmp_path, symbol="WIN1", realized_r_multiple=2.0,
                  realized_pl_usd=200.0, close_reason="trailed_out")
    _write_outcome(tmp_path, symbol="WIN2", realized_r_multiple=1.0,
                  realized_pl_usd=100.0, close_reason="trailed_out")
    _write_outcome(tmp_path, symbol="LOSS1", realized_r_multiple=-1.0,
                  realized_pl_usd=-100.0, close_reason="stopped_out")

    summary = cp.performance_summary()
    assert summary["count"] == 3
    assert summary["win_rate"] == pytest.approx(2 / 3)
    assert summary["avg_r_multiple"] == pytest.approx((2.0 + 1.0 - 1.0) / 3)
    assert summary["avg_r_multiple_wins"] == pytest.approx(1.5)
    assert summary["avg_r_multiple_losses"] == pytest.approx(-1.0)
    assert summary["total_realized_pl_usd"] == pytest.approx(200.0)
    assert summary["by_close_reason"] == {"trailed_out": 2, "stopped_out": 1}


def test_performance_summary_ignores_ungraded_outcomes(tmp_path, monkeypatch):
    """A trade with no computable risk basis (e.g. bad entry/stop data)
    shouldn't silently skew the win rate — exclude it, don't guess."""
    _use_tmp_state(tmp_path, monkeypatch)
    _write_outcome(tmp_path, symbol="UNGRADED", realized_r_multiple=None)
    _write_outcome(tmp_path, symbol="GRADED", realized_r_multiple=1.0)
    summary = cp.performance_summary()
    assert summary["count"] == 1


def test_performance_summary_filters_by_since(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    _write_outcome(tmp_path, symbol="OLD", exit_time=500_000.0,
                  realized_r_multiple=1.0)
    _write_outcome(tmp_path, symbol="NEW", exit_time=1_500_000.0,
                  realized_r_multiple=2.0)
    summary = cp.performance_summary(since=1_000_000.0)
    assert summary["count"] == 1
    assert summary["avg_r_multiple"] == 2.0
