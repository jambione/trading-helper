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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import alpaca_trader  # noqa: E402
import ai_positions as cp  # noqa: E402


def _state_path(tmp_path):
    return tmp_path / "positions_state.json"


def _outcomes_path(tmp_path):
    return tmp_path / "outcomes.jsonl"


def _use_tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "POSITIONS_STATE_PATH", _state_path(tmp_path))
    monkeypatch.setattr(cp, "OUTCOMES_PATH", _outcomes_path(tmp_path))



# ── evaluate_entry CLI routing ────────────────────────────────────────────────

def test_evaluate_entry_routes_to_grok_cli(monkeypatch):
    """Grok book owner must not call Claude Code for entry checks."""
    calls = []

    def fake_grok(prompt, **kw):
        calls.append(("grok", kw.get("phase"), kw.get("model")))
        return '{"decision": "WAIT"}'

    def fake_claude(prompt, **kw):
        calls.append(("claude", kw.get("phase"), kw.get("model")))
        return '{"decision": "WAIT"}'

    monkeypatch.setattr(cp, "call_grok_cli", fake_grok)
    monkeypatch.setattr(cp, "call_claude_cli", fake_claude)
    cp.evaluate_entry(
        "NVDA", 100.0, 50_000.0, reason="test",
        backend="cli", model="grok-4.5", cli_bin="grok",
    )
    assert calls and calls[0][0] == "grok"
    assert calls[0][1] == "entry"


def test_evaluate_entry_routes_to_claude_cli(monkeypatch):
    calls = []

    def fake_grok(prompt, **kw):
        calls.append("grok")
        return '{"decision": "WAIT"}'

    def fake_claude(prompt, **kw):
        calls.append("claude")
        return '{"decision": "WAIT"}'

    monkeypatch.setattr(cp, "call_grok_cli", fake_grok)
    monkeypatch.setattr(cp, "call_claude_cli", fake_claude)
    cp.evaluate_entry(
        "NVDA", 100.0, 50_000.0, reason="test",
        backend="claude_cli", model="sonnet",
    )
    assert calls == ["claude"]


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


def test_normalize_wait_for_zone_infers_kind():
    d = cp.normalize_entry_decision({
        "decision": "WAIT",
        "entry_low": 27.0, "entry_high": 28.5,
        "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
        "summary": "wait for pullback to 27-28.5",
    })
    assert d["wait_kind"] == "wait_for_zone"
    assert d["entry_low"] == 27.0


def test_normalize_wait_setup_without_levels():
    d = cp.normalize_entry_decision({
        "decision": "WAIT", "entry_low": 0, "stop_price": 0, "target_1": 0,
        "summary": "no clean setup",
    })
    assert d["wait_kind"] == "wait_setup"


def test_normalize_hard_no():
    d = cp.normalize_entry_decision({
        "decision": "WAIT", "wait_kind": "hard_no", "summary": "thesis broken",
    })
    assert d["wait_kind"] == "hard_no"


def test_normalize_levels_beat_avoid_keyword():
    """Full levels → wait_for_zone even if summary says 'avoid' (chasing, etc.)."""
    d = cp.normalize_entry_decision({
        "decision": "WAIT",
        "entry_low": 27.0, "entry_high": 28.5,
        "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
        "summary": "avoid chasing here; wait for pullback into 27-28.5",
    })
    assert d["wait_kind"] == "wait_for_zone"


def test_normalize_hard_no_keyword_without_levels():
    d = cp.normalize_entry_decision({
        "decision": "WAIT",
        "entry_low": 0, "stop_price": 0, "target_1": 0,
        "summary": "thesis broken, stay away",
    })
    assert d["wait_kind"] == "hard_no"


def test_qualifies_as_entry_still_rejects_wait():
    d = cp.normalize_entry_decision({
        "decision": "WAIT", "entry_low": 27.0, "entry_high": 28.5,
        "stop_price": 25.0, "target_1": 35.0, "reward_risk": 3.5,
    })
    assert cp.qualifies_as_entry(d, min_reward_risk=3.0) is False


def test_log_entry_decision_writes_levels(tmp_path, monkeypatch):
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(cp, "EVENTS_PATH", events)
    cp.log_entry_decision(
        "SMCI",
        {"decision": "WAIT", "wait_kind": "wait_for_zone",
         "entry_low": 27.0, "entry_high": 28.5, "stop_price": 25.0,
         "target_1": 35.0, "summary": "pullback"},
        reason="structure",
    )
    row = json.loads(events.read_text().strip().splitlines()[-1])
    assert row["kind"] == "entry_decision"
    assert row["symbol"] == "SMCI"
    assert row["wait_kind"] == "wait_for_zone"
    assert row["entry_low"] == 27.0
    assert "pullback" in row["summary"]


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

    def __init__(self, market_open=True, fail_on_call=None):
        self.calls: list[dict] = []
        self._next_id = 1
        self._market_open = market_open
        # 1-based call index to fail (e.g. 2 = tranche B)
        self.fail_on_call = fail_on_call
        self.cancel_calls: list[str] = []
        self.close_calls: list[str] = []

    def market_is_open(self):
        return self._market_open

    def size_by_risk(self, equity, risk_pct, entry, stop):
        return alpaca_trader.size_by_risk(equity, risk_pct, entry, stop)

    def buy_bracket_exact(self, ticker, qty, stop_price, target_price=None):
        oid = f"order_{self._next_id}"
        self._next_id += 1
        self.calls.append({"ticker": ticker, "qty": qty,
                           "stop_price": stop_price, "target_price": target_price})
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            return {"ok": False, "error": "rejected", "status": "rejected"}
        return {"ok": True, "buy_order_id": oid,
                "stop_order_id": (None if target_price else f"stop_{oid}"),
                "status": "accepted"}

    def cancel_open_orders(self, ticker):
        self.cancel_calls.append(ticker)
        return {"ok": True, "canceled": 1}

    def close_out(self, ticker, price=0.0, rsi=0.0, hist=0.0):
        self.close_calls.append(ticker)
        return {"ok": True, "order_id": "close_1"}


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


def test_place_scaled_entry_rolls_back_when_tranche_b_fails(
        tmp_path, monkeypatch):
    """Half-armed books are forbidden — A success + B fail must cancel/close."""
    _use_tmp_state(tmp_path, monkeypatch)
    stub = _StubBroker(fail_on_call=2)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    result = cp.place_scaled_entry(
        "nvda", _buy_decision(scale_out_pct=40),
        account_equity=50_000.0, risk_pct=1.0, current_ask=40.5)
    assert result["ok"] is False
    assert result.get("rolled_back") is True
    assert stub.cancel_calls == ["NVDA"]
    assert stub.close_calls == ["NVDA"]
    # Must not persist managed state for a rolled-back entry.
    state_path = _state_path(tmp_path)
    if state_path.exists():
        state = json.loads(state_path.read_text() or "{}")
        assert "NVDA" not in state


def test_pre_entry_gate_blocks_daily_loss_limit(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    monkeypatch.setattr(cp, "OUTCOMES_PATH", tmp_path / "outcomes.jsonl")
    import time
    now = time.time()
    # Two losers totaling -3.5R today
    with (tmp_path / "outcomes.jsonl").open("w") as f:
        for r in (-2.0, -1.5):
            f.write(json.dumps({
                "exit_time": now, "realized_r_multiple": r,
            }) + "\n")
    ok, reason = cp.pre_entry_gate(
        "NVDA", 40.0, 50_000.0, daily_loss_limit_r=3.0, now=now,
        max_spread_pct=0)
    assert ok is False
    assert "daily_loss_limit" in reason


def test_pre_entry_gate_blocks_open_risk(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    # $50k equity, 1% risk/trade; seed a position already using ~4% open risk
    # entry 40 stop 38 → $2/sh; 1000 sh → $2000 risk = 4%
    _state_path(tmp_path).write_text(json.dumps({
        "AAA": {
            "entry_price": 40.0, "stop_price": 38.0, "total_qty": 1000,
            "entry_confirmed": True,
        }
    }))
    ok, reason = cp.pre_entry_gate(
        "NVDA", 40.0, 50_000.0, risk_pct=1.0, max_open_risk_pct=4.5,
        max_spread_pct=0)
    assert ok is False
    assert "open_risk" in reason


def test_pre_entry_gate_blocks_wide_spread(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    ok, reason = cp.pre_entry_gate(
        "NVDA", 40.4, 50_000.0, bid=40.0, max_spread_pct=0.5,
        daily_loss_limit_r=99.0, max_open_risk_pct=99.0)
    assert ok is False
    assert "spread_pct" in reason


def test_pre_entry_gate_allows_tight_spread(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    ok, reason = cp.pre_entry_gate(
        "NVDA", 40.1, 50_000.0, bid=40.0, max_spread_pct=1.0,
        daily_loss_limit_r=99.0, max_open_risk_pct=99.0)
    assert ok is True
    assert reason == ""


def test_pre_entry_gate_blocks_thin_dollar_volume(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    ok, reason = cp.pre_entry_gate(
        "NVDA", 40.0, 50_000.0, bid=39.95, max_spread_pct=1.0,
        min_dollar_volume=1_000_000.0, dollar_volume=50_000.0,
        daily_loss_limit_r=99.0, max_open_risk_pct=99.0)
    assert ok is False
    assert "dollar_vol" in reason


def test_unconfirmed_entry_expires_after_ttl(tmp_path, monkeypatch):
    _seed_state(
        tmp_path, monkeypatch,
        entry_confirmed=False, entry_time=1_000_000.0,
    )
    stub = _StubBrokerManage(position_open=False)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    # 1000 seconds later with 900s TTL
    events = cp.manage_open_positions(
        now=1_000_000.0 + 1000.0, unconfirmed_ttl_sec=900.0)
    kinds = [e["event"] for e in events]
    assert "entry_unconfirmed_expired" in kinds
    assert "NVDA" in stub.canceled
    assert "NVDA" in stub.closed
    state = json.loads(_state_path(tmp_path).read_text() or "{}")
    assert "NVDA" not in state


def test_resolve_trading_source_prefers_explicit():
    from ai_trader import resolve_trading_source, apply_trading_source
    assert resolve_trading_source({"ai_trading_source": "grok"}) == "grok"
    assert resolve_trading_source({
        "grok_trading_enabled": True,
        "ai_trading_enabled": True,
    }) == "grok"
    cfg = apply_trading_source({}, "grok")
    assert cfg["grok_trading_enabled"] is True
    assert cfg["ai_trading_enabled"] is False


def test_positions_payload_sticky_on_alpaca_fail(monkeypatch):
    """Failed broker poll must not publish empty positions (UI flash)."""
    import ai_trader as at

    at._last_good_positions.clear()
    at._last_good_account.clear()
    good = {
        "CMG": {
            "qty": 10.0, "avg_entry": 34.0, "current": 34.1,
            "pl": 1.0, "plpc": 0.3, "mkt_val": 341.0,
        },
    }
    at._last_good_positions.update(good)
    monkeypatch.setattr(alpaca_trader, "is_active", lambda: True)
    monkeypatch.setattr(alpaca_trader, "get_positions_detail", lambda: None)
    monkeypatch.setattr(alpaca_trader, "get_open_orders", lambda: [])
    monkeypatch.setattr(alpaca_trader, "get_account_day_pl", lambda: None)

    payload = at._positions_payload(
        "paper", 1.0, book_owner="grok", watch_poll_sec=20.0)
    assert "Alpaca" in (payload.get("error") or "")
    assert payload["positions"]["CMG"]["qty"] == 10.0

    # Successful empty book clears sticky (flat is authoritative).
    monkeypatch.setattr(alpaca_trader, "get_positions_detail", lambda: {})
    monkeypatch.setattr(
        alpaca_trader, "get_account_day_pl",
        lambda: {
            "equity": 100_000.0, "last_equity": 99_950.0,
            "day_pl": 50.0, "day_pl_pct": 0.05,
            "cash": 50_000.0, "buying_power": 100_000.0,
        },
    )
    payload2 = at._positions_payload(
        "paper", 2.0, book_owner="grok", watch_poll_sec=20.0)
    assert payload2["error"] == ""
    assert payload2["positions"] == {}
    assert at._last_good_positions == {}
    assert payload2["day_pl"] == 50.0
    assert payload2["account"]["equity"] == 100_000.0


def test_get_account_day_pl_maps_equity_delta(monkeypatch):
    """day_pl = equity − last_equity (Alpaca account day change)."""

    class _Acct:
        equity = 10_050.0
        last_equity = 10_000.0
        cash = 5_000.0
        buying_power = 20_000.0

    class _Client:
        def get_account(self):
            return _Acct()

    monkeypatch.setattr(alpaca_trader, "is_active", lambda: True)
    monkeypatch.setattr(alpaca_trader, "_client", _Client())
    snap = alpaca_trader.get_account_day_pl()
    assert snap is not None
    assert snap["day_pl"] == pytest.approx(50.0)
    assert snap["day_pl_pct"] == pytest.approx(0.5)
    assert snap["equity"] == 10_050.0


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
