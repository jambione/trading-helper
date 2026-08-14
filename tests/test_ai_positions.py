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


def _pos_shadow_path(tmp_path):
    return tmp_path / "position_shadow.jsonl"


def _use_tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "POSITIONS_STATE_PATH", _state_path(tmp_path))
    monkeypatch.setattr(cp, "OUTCOMES_PATH", _outcomes_path(tmp_path))
    monkeypatch.setattr(cp, "POSITION_SHADOW_PATH", _pos_shadow_path(tmp_path))



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
        self.limit_calls: list[dict] = []

    def is_active(self):
        return True

    def market_is_open(self):
        return self._market_open

    def size_by_risk(self, equity, risk_pct, entry, stop):
        return alpaca_trader.size_by_risk(equity, risk_pct, entry, stop)

    def buy_limit_at_price(self, ticker, price, dollar_amount, **kwargs):
        oid = f"naked_{self._next_id}"
        self._next_id += 1
        qty = int(float(dollar_amount) / float(price)) if price else 0
        self.limit_calls.append({
            "ticker": ticker, "price": price, "dollar_amount": dollar_amount,
            "kind": "naked_limit",
        })
        self.calls.append({"ticker": ticker, "qty": qty, "naked": True})
        return {"ok": True, "order_id": oid, "status": "accepted"}

    def buy_limit_bracket(self, ticker, qty, limit_price, stop_price,
                          target_price=None, **kwargs):
        self.limit_calls.append({
            "ticker": ticker, "qty": qty, "limit_price": limit_price,
            "stop_price": stop_price, "target_price": target_price,
        })
        # Mirror buy_bracket_exact's bookkeeping so tranche logic is identical.
        return self.buy_bracket_exact(ticker, qty, stop_price, target_price)

    def buy_bracket_exact(self, ticker, qty, stop_price, target_price=None):
        oid = f"order_{self._next_id}"
        self._next_id += 1
        self.calls.append({"ticker": ticker, "qty": qty,
                           "stop_price": stop_price, "target_price": target_price})
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            return {"ok": False, "error": "rejected", "status": "rejected"}
        # Day scalp: every successful arm must expose stop + (when given) TP ids.
        return {
            "ok": True, "buy_order_id": oid,
            "stop_order_id": f"stop_{oid}",
            "target_order_id": (f"tp_{oid}" if target_price else None),
            "status": "accepted",
        }

    def cancel_open_orders(self, ticker):
        self.cancel_calls.append(ticker)
        return {"ok": True, "canceled": 1}

    def close_out(self, ticker, price=0.0, rsi=0.0, hist=0.0):
        self.close_calls.append(ticker)
        return {"ok": True, "order_id": "close_1"}

    def cancel_order_id(self, order_id):
        self.cancel_calls.append(str(order_id or ""))
        return True

    def place_limit_sell(self, ticker, qty, limit_price, **kwargs):
        oid = f"tp_{self._next_id}"
        self._next_id += 1
        self.limit_calls.append({
            "ticker": ticker, "qty": qty, "limit_price": limit_price,
            "kind": "partial_t1",
        })
        return {"ok": True, "order_id": oid, "status": "accepted", "qty": qty}

    def sell_qty_market(self, ticker, qty):
        oid = f"mkt_{self._next_id}"
        self._next_id += 1
        self.calls.append({
            "ticker": ticker, "qty": qty, "stop_price": None,
            "target_price": None, "kind": "partial_mkt",
        })
        return {"ok": True, "order_id": oid, "status": "accepted", "qty": qty}

    def free_sell_capacity(self, ticker, settle_sec=0.0):
        self.cancel_calls.append(f"free:{ticker}")
        return {"ok": True, "canceled": 1}

    def place_stop_sell(self, ticker, stop_price, qty=None):
        oid = f"stop_{self._next_id}"
        self._next_id += 1
        self.calls.append({
            "ticker": ticker, "qty": qty, "stop_price": stop_price,
            "target_price": None, "kind": "stop_sell",
        })
        return {"ok": True, "order_id": oid, "status": "accepted", "qty": qty}


def test_place_scaled_entry_splits_into_two_tranches_by_scale_out_pct(
        tmp_path, monkeypatch):
    """Option A dual: one parent buy for full size; qty_a/qty_b are bookkeeping.
    Partial T1 attaches after fill (not a second protected buy)."""
    _use_tmp_state(tmp_path, monkeypatch)
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_day_scalp_dual_tranche": True,
        "ai_entry_broker_target": True,
        "ai_watch_synth_scale_out_pct": 40.0,
        "ai_max_position_pct": 25.0,
        "ai_watch_arm_below_zone": True,
        "ai_watch_arm_below_zone_max_r": 1.0,
    })
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
    # Single parent order for full size, stop only (T1 after fill).
    assert len(stub.calls) == 1
    parent = stub.calls[0]
    assert parent["qty"] == 200
    assert parent["target_price"] is None
    assert parent["stop_price"] == 38.0

    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["qty_a"] == 80
    assert state["NVDA"]["qty_b"] == 120
    assert state["NVDA"]["total_qty"] == 200
    assert state["NVDA"]["t1_attach_pending"] is True
    assert state["NVDA"]["logical_dual"] is True
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


def test_place_scaled_entry_rolls_back_when_parent_buy_fails(
        tmp_path, monkeypatch):
    """Single-parent dual: if the only buy fails, no managed state is kept."""
    _use_tmp_state(tmp_path, monkeypatch)
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_day_scalp_dual_tranche": True,
        "ai_entry_broker_target": True,
        "ai_watch_synth_scale_out_pct": 40.0,
        "ai_max_position_pct": 25.0,
        "ai_watch_arm_below_zone": True,
        "ai_watch_arm_below_zone_max_r": 1.0,
    })
    stub = _StubBroker(fail_on_call=1)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    result = cp.place_scaled_entry(
        "nvda", _buy_decision(scale_out_pct=40),
        account_equity=50_000.0, risk_pct=1.0, current_ask=40.5)
    assert result["ok"] is False
    assert "NVDA" not in (result.get("ticker") and {})
    state_path = _state_path(tmp_path)
    if state_path.exists():
        state = json.loads(state_path.read_text() or "{}")
        assert "NVDA" not in state
    assert stub.close_calls == []  # never opened


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


def test_pre_entry_gate_blocks_pdt_when_broker_count_high(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    ok, reason = cp.pre_entry_gate(
        "NVDA", 40.0, 10_000.0,
        daily_loss_limit_r=99.0, max_open_risk_pct=99.0, max_spread_pct=0,
        pdt_protect="block", broker_daytrade_count=3)
    assert ok is False
    assert reason.startswith("pdt_")


def test_pre_entry_gate_skips_pdt_above_25k(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    ok, reason = cp.pre_entry_gate(
        "NVDA", 40.0, 50_000.0,
        daily_loss_limit_r=99.0, max_open_risk_pct=99.0, max_spread_pct=0,
        pdt_protect="block", broker_daytrade_count=4)
    assert ok is True
    assert reason == ""


def test_outcomes_coverage_flags_missing_close(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    ev = tmp_path / "events.jsonl"
    monkeypatch.setattr(cp, "EVENTS_PATH", ev)
    now = 1_800_000_000.0
    ev.write_text(json.dumps({
        "ts": now - 100, "kind": "entry_ok", "symbol": "GLXY",
        "entry_time": now - 100,
    }) + "\n")
    cov = cp.outcomes_coverage(now=now, lookback_sec=1000)
    assert cov["n_entries"] == 1
    assert cov["n_uncovered"] == 1
    assert cov["uncovered"][0]["symbol"] == "GLXY"
    (tmp_path / "outcomes.jsonl").write_text(json.dumps({
        "symbol": "GLXY", "entry_time": now - 100, "exit_time": now - 10,
        "realized_r_multiple": -0.1,
    }) + "\n")
    cov2 = cp.outcomes_coverage(now=now, lookback_sec=1000)
    assert cov2["n_uncovered"] == 0


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


def test_eod_liquidate_due_once_per_day(tmp_path, monkeypatch):
    """EOD liquidate arms at 15:50 ET and only once per calendar day."""
    import ai_trader as at
    import ai_positions as cp
    from datetime import datetime
    from zoneinfo import ZoneInfo

    path = tmp_path / "eod.json"
    monkeypatch.setattr(cp, "EOD_LIQUIDATE_STATE_PATH", path)
    et = ZoneInfo("America/New_York")
    # Monday 15:49 → not due
    mon_early = datetime(2026, 8, 3, 15, 49, tzinfo=et).timestamp()
    assert at._eod_liquidate_due(
        {"ai_eod_liquidate_enabled": True, "ai_eod_liquidate_time": "15:50"},
        mon_early,
    ) is False
    # Monday 15:50 → due
    mon = datetime(2026, 8, 3, 15, 50, tzinfo=et).timestamp()
    assert at._eod_liquidate_due(
        {"ai_eod_liquidate_enabled": True, "ai_eod_liquidate_time": "15:50"},
        mon,
    ) is True
    at._mark_eod_liquidate_done(mon, {"ok": True, "closed": 1})
    assert at._eod_liquidate_due(
        {"ai_eod_liquidate_enabled": True, "ai_eod_liquidate_time": "15:50"},
        mon + 60,
    ) is False


def test_liquidate_all_cancels_and_closes(monkeypatch):
    """liquidate_all cancels open orders then close_out each position."""
    calls = {"cancel": 0, "close": []}

    monkeypatch.setattr(alpaca_trader, "is_active", lambda: True)
    # Host guard blocks mutations off the trading host; force allow in unit test.
    monkeypatch.setattr(alpaca_trader, "_can_mutate", lambda: True)
    monkeypatch.setattr(
        alpaca_trader, "cancel_open_orders",
        lambda ticker=None: calls.__setitem__("cancel", calls["cancel"] + 1)
        or {"ok": True, "canceled": 2, "errors": []},
    )
    monkeypatch.setattr(
        alpaca_trader, "get_positions_detail",
        lambda: {"CMG": {"qty": 10}, "AAA": {"qty": 5}},
    )

    def _close(sym, **kw):
        calls["close"].append(sym)
        return {"ok": True, "order_id": "x", "status": "accepted"}

    monkeypatch.setattr(alpaca_trader, "close_out", _close)
    out = alpaca_trader.liquidate_all()
    assert out["canceled"] == 2
    assert out["closed"] == 2
    assert set(out["symbols"]) == {"AAA", "CMG"}
    assert calls["cancel"] == 1


def test_past_eod_liquidate_time():
    import ai_entry_watch as ew
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    cfg = {"ai_eod_liquidate_enabled": True, "ai_eod_liquidate_time": "15:50"}
    before = datetime(2026, 8, 3, 15, 49, tzinfo=et).timestamp()
    after = datetime(2026, 8, 3, 15, 50, tzinfo=et).timestamp()
    assert ew.past_eod_liquidate_time(cfg, before) is False
    assert ew.past_eod_liquidate_time(cfg, after) is True
    assert ew.past_eod_liquidate_time(
        {"ai_eod_liquidate_enabled": False}, after) is False


def test_watch_session_and_trading_hours(tmp_path, monkeypatch):
    """Watch from 09:00 ET; paper entries only when market_open and before EOD."""
    import ai_entry_watch as ew
    import ai_positions as cp
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    cfg = {
        "ai_watch_enabled": True,
        "ai_watch_start_time": "09:00",
        "ai_eod_liquidate_enabled": True,
        "ai_eod_liquidate_time": "15:50",
        "ai_sod_liquidate_enabled": True,
    }
    pre = datetime(2026, 8, 3, 8, 59, tzinfo=et).timestamp()
    start = datetime(2026, 8, 3, 9, 0, tzinfo=et).timestamp()
    rth = datetime(2026, 8, 3, 10, 0, tzinfo=et).timestamp()
    eod = datetime(2026, 8, 3, 15, 50, tzinfo=et).timestamp()

    assert ew.watch_session_active(cfg, pre) is False
    assert ew.watch_session_active(cfg, start) is True
    assert ew.watch_session_active(cfg, rth) is True
    assert ew.watch_session_active(cfg, eod) is False

    # Pre-open: can watch from 9:00 but not trade.
    assert ew.trading_hours_active(cfg, start, market_open=False) is False
    # RTH but SOD not done yet → no entries.
    monkeypatch.setattr(cp, "SOD_LIQUIDATE_STATE_PATH", tmp_path / "sod.json")
    assert ew.sod_liquidate_done(cfg, rth) is False
    assert ew.trading_hours_active(cfg, rth, market_open=True) is False
    # After SOD latch, trading allowed.
    (tmp_path / "sod.json").write_text(
        json.dumps({"last_day": "2026-08-03"}), encoding="utf-8")
    assert ew.sod_liquidate_done(cfg, rth) is True
    assert ew.trading_hours_active(cfg, rth, market_open=True) is True
    assert ew.trading_hours_active(cfg, eod, market_open=True) is False


def test_sod_liquidate_due_once_per_day(tmp_path, monkeypatch):
    """SOD arms only at first RTH open and only once per ET day."""
    import ai_trader as at
    import ai_positions as cp
    from datetime import datetime
    from zoneinfo import ZoneInfo

    path = tmp_path / "sod.json"
    monkeypatch.setattr(cp, "SOD_LIQUIDATE_STATE_PATH", path)
    et = ZoneInfo("America/New_York")
    rth = datetime(2026, 8, 3, 9, 35, tzinfo=et).timestamp()
    cfg = {
        "ai_sod_liquidate_enabled": True,
        "ai_eod_liquidate_enabled": True,
        "ai_eod_liquidate_time": "15:50",
    }
    assert at._sod_liquidate_due(cfg, rth, market_open=False) is False
    assert at._sod_liquidate_due(cfg, rth, market_open=True) is True
    at._mark_sod_liquidate_done(rth, {"ok": True})
    assert at._sod_liquidate_due(cfg, rth + 60, market_open=True) is False


def test_clear_watch_book_empties_state(tmp_path, monkeypatch):
    """EOD clear must wipe entry_watch_state so AI Watch UI goes empty."""
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew.save_watch({
        "CMG": {"symbol": "CMG", "status": "watching", "score": 8},
        "AAA": {"symbol": "AAA", "status": "submitted", "score": 7},
    })
    out = ew.clear_watch_book(now=1.0)
    assert out == {}
    assert ew.load_watch() == {}


def test_save_state_does_not_clobber_dashboard_wire(tmp_path, monkeypatch):
    """Managed book must not overwrite ai_positions_state.json (live/stale flash)."""
    import ai_positions as cp

    report = tmp_path / "claude_reports"
    report.mkdir()
    wire = tmp_path / "ai_positions_state.json"
    wire.write_text(
        json.dumps({
            "updated": 1.0,
            "mode": "paper",
            "positions": {"CMG": {"qty": 1}},
            "entry_book": [],
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(cp, "POSITIONS_STATE_PATH", report / "positions_state.json")
    monkeypatch.setattr(cp, "ROOT", tmp_path)
    monkeypatch.setattr(
        cp, "resolve_report_dir", lambda: report,
    )

    cp._save_state({"CMG": {"qty": 1, "entry_price": 34.0}})

    assert (report / "positions_state.json").exists()
    after = json.loads(wire.read_text(encoding="utf-8"))
    assert after.get("updated") == 1.0
    assert "positions" in after
    assert "CMG" not in after or "updated" in after


def test_is_ai_positions_wire_rejects_managed_map():
    import dashboard as d

    assert d._is_ai_positions_wire({
        "updated": 1.0, "positions": {"CMG": {"qty": 1}},
    })
    assert not d._is_ai_positions_wire({"CMG": {"qty": 1, "entry_price": 34.0}})
    assert not d._is_ai_positions_wire({})


# ── mechanical position management (no LLM) ─────────────────────────────────

class _StubBrokerManage:
    def __init__(self, order_status="new", position_open=True, current_price=44.0,
                 fills=None, live_qty=None, open_orders=None):
        self.order_status = order_status
        self.position_open = position_open
        self.current_price = current_price
        self.live_qty = live_qty
        self._open_orders = open_orders
        self.replace_calls: list[dict] = []
        self.closed: list[str] = []
        self.canceled: list[str] = []
        # order_id -> filled_avg_price. Outcomes are priced off real fills, so
        # the stub has to be able to answer "what did this leg fill at?".
        self.fills = dict(fills or {})

    def get_positions_detail(self):
        if not self.position_open:
            return {}
        row = {"current": self.current_price}
        if self.live_qty is not None:
            row["qty"] = self.live_qty
        return {"NVDA": row}

    def get_order(self, order_id):
        out = {"status": self.order_status}
        if order_id in self.fills:
            out["filled_avg_price"] = self.fills[order_id]
        return out

    def get_open_orders(self, limit=100):
        if self._open_orders is not None:
            return list(self._open_orders)
        # Protected book: open long has a resting stop sell so heal does not fire.
        if not self.position_open:
            return []
        return [{
            "id": "stop_b", "symbol": "NVDA", "side": "sell",
            "type": "stop", "status": "new", "stop": 38.0,
        }]

    def get_equity(self):
        return 50_000.0

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

    def cancel_order_id(self, order_id):
        self.canceled.append(str(order_id or ""))
        return True

    def place_limit_sell(self, ticker, qty, limit_price, **kwargs):
        self.canceled.append(f"limit_sell:{ticker}:{qty}:{limit_price}")
        return {
            "ok": True, "order_id": "tp_partial_1", "status": "accepted",
            "qty": qty,
        }

    def sell_qty_market(self, ticker, qty):
        self.closed.append(f"partial:{ticker}:{qty}")
        return {
            "ok": True, "order_id": "mkt_partial_1", "status": "accepted",
            "qty": qty,
        }

    def free_sell_capacity(self, ticker, settle_sec=0.0):
        self.canceled.append(f"free:{ticker}")
        return {"ok": True, "canceled": 1}

    def place_stop_sell(self, ticker, stop_price, qty=None):
        self.replace_calls.append({
            "ticker": ticker, "old": None,
            "trail_percent": None, "stop_price": stop_price, "qty": qty,
        })
        return {
            "ok": True, "order_id": "stop_rearm_1", "status": "accepted",
            "qty": qty, "stop": stop_price,
        }


def _seed_state(tmp_path, monkeypatch, **fields):
    _use_tmp_state(tmp_path, monkeypatch)
    base = {
        "qty_a": 100, "qty_b": 150, "total_qty": 250,
        "entry_price": 40.5,
        "risk_per_share": 2.5,  # frozen entry−stop so raised stops keep R
        "runner_trail_r": 1.0,
        "tranche_a_order_id": "order_a",
        "tranche_a_target_order_id": "target_a",
        "tranche_b_stop_order_id": "stop_b",
        "stop_price": 38.0, "target_1": 46.0, "trail_pct": 8.0,
        "time_stop_days": 10, "entry_time": 1_000_000.0,
        "tranche_a_filled": False, "breakeven_done": False,
        "reward_risk": 3.0, "summary": "test thesis",
        "entry_confirmed": True, "last_seen_price": None,
        "closing_reason": None,
    }
    base.update(fields)
    _state_path(tmp_path).write_text(json.dumps({"NVDA": base}))


def test_tranche_a_fill_puts_the_runner_stop_one_r_behind_the_peak(
        tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch)
    # "filled" here must mean the TAKE-PROFIT leg filled, not the parent buy.
    stub = _StubBrokerManage(order_status="filled")
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_100.0)

    assert any(e.get("event") == "scaled_out" for e in events)
    # entry 40.5, stop 38.0 -> 1R = 2.50. Peak 44.0 -> 44.0 - 1R = 41.50,
    # which is above breakeven, so the trail (not the floor) sets the level.
    # A trailing PERCENT is never sent: it would be a different distance on
    # every name.
    assert stub.replace_calls[0]["stop_price"] == 41.5
    assert stub.replace_calls[0]["trail_percent"] is None

    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["breakeven_done"] is True
    assert state["NVDA"]["tranche_a_filled"] is True
    assert state["NVDA"]["runner_stop_price"] == 41.5


def test_runner_stop_floors_at_breakeven_so_a_target_hit_cannot_finish_red(
        tmp_path, monkeypatch):
    """The whole point of P0: tranche A banked 0.6R, so the runner must not be
    allowed to give back more than that. peak-1R is below entry here."""
    _seed_state(tmp_path, monkeypatch)
    # Peak 41.0: peak - 1R = 38.5, well under the 40.5 entry.
    stub = _StubBrokerManage(order_status="filled", current_price=41.0)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_100.0)

    assert stub.replace_calls[0]["stop_price"] == 40.5     # breakeven, not 38.5
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["runner_stop_price"] == 40.5


def test_runner_stop_ratchets_up_with_the_peak_and_never_back_down(
        tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch)
    stub = _StubBrokerManage(order_status="filled")
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_100.0)
    assert stub.replace_calls[-1]["stop_price"] == 41.5

    stub.current_price = 48.0                    # runs further
    events = cp.manage_open_positions(now=1_000_105.0)
    assert stub.replace_calls[-1]["stop_price"] == 45.5
    assert any(e.get("event") == "runner_stop_raised" for e in events)

    calls_before = len(stub.replace_calls)
    stub.current_price = 46.0                    # pulls back; peak unchanged
    cp.manage_open_positions(now=1_000_110.0)
    assert len(stub.replace_calls) == calls_before, "a pullback must not move it"

    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["runner_stop_price"] == 45.5
    assert state["NVDA"]["peak_price"] == 48.0


def test_runner_stop_is_not_placed_above_the_market(tmp_path, monkeypatch):
    """Price fell back through entry between the target fill and this tick —
    a breakeven stop would sit above the last print and trigger on receipt."""
    _seed_state(tmp_path, monkeypatch)
    stub = _StubBrokerManage(order_status="filled", current_price=39.0)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_100.0)

    assert stub.replace_calls == []
    assert any(e.get("event") == "runner_stop_skipped" for e in events)
    state = json.loads(_state_path(tmp_path).read_text())
    # Nothing was placed, so nothing was recorded — the original entry stop is
    # still the only protection, which is the correct outcome here.
    assert state["NVDA"].get("runner_stop_price") is None


def test_realized_r_is_measured_against_the_entry_stop_not_the_moved_one(
        tmp_path, monkeypatch):
    """A managed trade must not fall out of the daily-loss gate. stop_price is
    rewritten by every stop move; risk_per_share is frozen at entry."""
    _use_tmp_state(tmp_path, monkeypatch)
    pos = {
        "entry_price": 40.5, "stop_price": 40.5,   # already lifted to breakeven
        "risk_per_share": 2.5, "total_qty": 100,
        "entry_time": 1_000_000.0, "tranche_a_filled": True,
    }

    out = cp._record_outcome("NVDA", pos, 43.0, "trailed_out", 1_000_100.0)

    assert out["realized_r_multiple"] == pytest.approx(1.0)   # not None
    assert out["realized_pl_usd"] == pytest.approx(250.0)


def test_a_deliberate_zero_survives_config_read():
    """float(x or default) cannot express 0 — which is how a configured
    'no trail' came back as 2.5%."""
    assert cp._opt_float(0, 2.5) == 0.0
    assert cp._opt_float(0.0, 2.5) == 0.0
    assert cp._opt_float(None, 2.5) == 2.5
    assert cp._opt_float("", 2.5) == 2.5
    assert cp._opt_float("junk", 2.5) == 2.5
    assert cp._opt_float(1.25, 2.5) == 1.25


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
    # close_out returns order_id "close_1"; that leg's fill is what prices the
    # outcome, so the exit is traceable even though no protective leg fired.
    stub = _StubBrokerManage(order_status="new", position_open=True,
                             fills={"close_1": 43.75})
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    # 11 days later — deadline passed with target 1 never hit.
    later = 1_000_000.0 + 11 * 86400
    events = cp.manage_open_positions(now=later)
    assert any(e.get("event") == "time_stop" for e in events)
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
    # Exit priced at the closing order's actual fill, not the last polled mark.
    assert outcome["close_reason"] == "time_stop"
    assert outcome["exit_price"] == 43.75
    assert outcome["realized_r_multiple"] == (43.75 - 40.5) / (40.5 - 38.0)
    assert outcome["realized_pl_usd"] == (43.75 - 40.5) * 250


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
    # The stop leg filled at 37.90 — slightly through the 38.00 trigger, which
    # is exactly the slippage the old last_seen_price estimate papered over.
    stub = _StubBrokerManage(position_open=False, fills={"stop_b": 37.90})
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_010.0)
    assert events == [{"ticker": "NVDA", "event": "closed",
                       "close_reason": "stopped_out"}]
    outcome = json.loads(_outcomes_path(tmp_path).read_text().strip())
    assert outcome["exit_price"] == 37.90
    assert outcome["realized_r_multiple"] < 0  # a loss, priced at the real fill


def test_outcome_carries_the_decision_time_feature_vector(tmp_path, monkeypatch):
    """The whole point of the feature vector is that it reaches the outcome.

    Slicing joins nothing: outcomes.jsonl has to land denormalized, features
    and result on one row, or "did EXT-tagged entries outperform?" is
    unanswerable. The two halves must stay distinguishable — selection
    (source/rvol/look_reason/criteria) is a different question from timing
    (cm_ok/pctr_ok/cm_rsi_rising).
    """
    features = {
        "source": "trending", "score": 19.3, "rvol": 2.1, "pct_change": 18.0,
        "look_reason": "EXT", "criteria": ["score", "rvol", "uptrend", "ext"],
        "cm_ok": True, "pctr_ok": True, "cm_rsi_rising": True,
        "macd_ok": False, "cm_rsi": 18.3, "pctr": -82.1,
        "proximity_pct": 100.0, "entry_hour_et": 10.25, "dwell_sec": 240.0,
        "ask": 40.5,
    }
    _seed_state(tmp_path, monkeypatch, tranche_a_filled=False,
                last_seen_price=39.0, features=features)
    stub = _StubBrokerManage(position_open=False, fills={"stop_b": 37.90})
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_010.0)

    outcome = json.loads(_outcomes_path(tmp_path).read_text().strip())
    # Result side still correct — features must not disturb pricing.
    assert outcome["exit_price"] == 37.90
    assert outcome["realized_r_multiple"] < 0

    f = outcome["features"]
    assert f is not None, "outcome lost the feature vector — nothing is sliceable"
    assert f["source"] == "trending" and f["look_reason"] == "EXT"
    assert f["rvol"] == 2.1 and "ext" in f["criteria"]
    assert f["cm_rsi_rising"] is True and f["entry_hour_et"] == 10.25


def test_outcome_without_features_still_records(tmp_path, monkeypatch):
    """Positions opened before the feature vector existed, or by a path that
    does not set one, must still produce a priced outcome — instrumentation
    must never be able to block the record that gates the daily loss limit."""
    _seed_state(tmp_path, monkeypatch, tranche_a_filled=False,
                last_seen_price=39.0)
    stub = _StubBrokerManage(position_open=False, fills={"stop_b": 37.90})
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_010.0)

    outcome = json.loads(_outcomes_path(tmp_path).read_text().strip())
    assert outcome["exit_price"] == 37.90
    assert outcome["features"] is None


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
    # The data-quality block is always present, so a caller can never read the
    # graded numbers without seeing what they were computed from.
    assert cp.performance_summary() == {
        "count": 0, "ungraded": 0, "ungraded_symbols": [],
        "unknown_close_reason": 0,
    }


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


def test_parent_buy_fill_does_not_count_as_a_scale_out(tmp_path, monkeypatch):
    """tranche_a_order_id is the PARENT buy — it fills seconds after entry.

    Keying "did tranche A scale out?" off it made tranche_a_filled True on
    every position immediately, which labelled every close trailed_out, killed
    the time stop, and moved a runner's stop to breakeven at entry.
    """
    _seed_state(tmp_path, monkeypatch)
    stub = _StubBrokerManage(order_status="new", position_open=True)

    # Parent buy is filled; the take-profit leg is not.
    def get_order(order_id):
        return {"status": "filled" if order_id == "order_a" else "new"}

    stub.get_order = get_order
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_100.0)

    assert events == [], "a filled entry order is not a scale-out"
    assert stub.replace_calls == [], "runner's stop must not move at entry"
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["tranche_a_filled"] is False


# ── entry order shape: marketable limit capped at the zone top ───────────────

def _lim_cfg(monkeypatch, **over):
    cfg = {"ai_entry_order_style": "limit", "ai_entry_limit_pad_pct": 0.15,
           "ai_entry_limit_ttl_sec": 30.0}
    cfg.update(over)
    monkeypatch.setattr(cp, "_entry_cfg", lambda: cfg)
    return cfg


def test_entry_limit_is_marketable_but_capped_at_the_zone_top(monkeypatch):
    """A market entry fills at whatever the ask is at execution. size_by_risk
    sizes off current_ask and the stop is derived from it, so a fill above the
    quote makes real risk exceed the configured 5% and notional exceed the 20%
    cap. Capping at the zone top makes "we only fill inside the zone" true by
    construction rather than by hope.
    """
    _lim_cfg(monkeypatch)

    # Pad keeps it marketable when there is room under the zone top.
    px = cp._entry_limit_price(current_ask=19.00, entry_high=19.44, entry_low=18.66)
    assert px == 19.03                      # 19.00 * 1.0015

    # Ask sitting at the top: the cap binds, we never bid above the zone.
    px = cp._entry_limit_price(current_ask=19.44, entry_high=19.44, entry_low=18.66)
    assert px == 19.44

    # Zone bounds passed in either order still cap correctly.
    px = cp._entry_limit_price(current_ask=19.44, entry_high=18.66, entry_low=19.44)
    assert px == 19.44

    # Desk click: bid last + pad even if that is above the zone.
    px = cp._entry_limit_price(
        current_ask=1.90, entry_high=1.82, entry_low=1.79, cap_at_zone=False)
    assert px == 1.90


def test_market_style_and_missing_quote_fall_back_to_market(monkeypatch):
    _lim_cfg(monkeypatch, ai_entry_order_style="market")
    assert cp._entry_limit_price(19.0, 19.44, 18.66) is None

    _lim_cfg(monkeypatch)
    # No quote to anchor a limit on — a market order is the honest fallback.
    assert cp._entry_limit_price(None, 19.44, 18.66) is None
    assert cp._entry_limit_price(0.0, 19.44, 18.66) is None


def test_place_scaled_entry_uses_a_limit_and_never_bids_above_the_zone(
        tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    # Dual off so this test only asserts the limit shape of one bracket.
    _lim_cfg(monkeypatch, ai_day_scalp_dual_tranche=False)
    stub = _StubBroker(market_open=True)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    decision = _buy_decision(synthetic=True)      # entry_low 40.0, high 41.0
    out = cp.place_scaled_entry("nvda", decision, account_equity=50_000.0,
                                risk_pct=1.0, current_ask=41.0)
    assert out["ok"] is True
    assert stub.limit_calls, "expected a LIMIT entry, not a market order"
    lim = stub.limit_calls[0]["limit_price"]
    assert lim == 41.0, "pad must be capped at the zone top"
    assert lim <= max(decision["entry_high"], decision["entry_low"])


def test_unfilled_entry_limit_is_cancelled_after_the_short_ttl(
        tmp_path, monkeypatch):
    """A resting entry limit gets a much shorter leash than a filled-but-
    unconfirmed position: if price left the zone the setup is gone, and a
    15-minute rest lets it fill long after the zone re-anchored away."""
    _lim_cfg(monkeypatch, ai_entry_limit_ttl_sec=30.0)
    _seed_state(tmp_path, monkeypatch, entry_confirmed=False,
                entry_limit_price=41.0, entry_time=1_000_000.0)
    stub = _StubBrokerManage(position_open=False)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    # 45s later: past the 30s limit TTL but far inside the 900s unconfirmed TTL.
    events = cp.manage_open_positions(now=1_000_045.0)
    assert any(e["event"] == "entry_unconfirmed_expired" for e in events)
    assert stub.canceled == ["NVDA"]
    assert json.loads(_state_path(tmp_path).read_text()) == {}


def test_a_filled_but_unconfirmed_position_keeps_the_long_ttl(
        tmp_path, monkeypatch):
    """No resting limit -> this is a fill we simply have not seen yet, which
    must not be cancelled after 30 seconds."""
    _lim_cfg(monkeypatch, ai_entry_limit_ttl_sec=30.0)
    _seed_state(tmp_path, monkeypatch, entry_confirmed=False,
                entry_limit_price=None, entry_time=1_000_000.0)
    stub = _StubBrokerManage(position_open=False)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_045.0)
    assert not any(e["event"] == "entry_unconfirmed_expired" for e in events)
    assert "NVDA" in json.loads(_state_path(tmp_path).read_text())


# ── sell_signal defends an open position ─────────────────────────────────────

def _sell_sig(monkeypatch, *, sell=True, enabled=True):
    """Point the exit path at a fixed engine reading."""
    monkeypatch.setattr(cp, "_engine_indicators",
                        lambda: {"NVDA": {"sell_signal": sell}})
    monkeypatch.setattr(cp, "_sell_signal_defends", lambda state: bool(enabled))
    monkeypatch.setattr(cp, "_resting_stop_order_id", lambda t: "stop_leg_1")


def test_sell_signal_underwater_never_places_a_stop_above_the_market(
        tmp_path, monkeypatch):
    """The hazard this whole feature turns on.

    A stop is only a stop while it sits BELOW the market. Underwater, moving it
    "to breakeven" puts it above the last print and Alpaca triggers it on
    receipt — a market exit wearing a stop order's name. That is the opposite
    of tightening, and both open positions were below entry when this was
    written. Leave the original stop and record that the flag was seen.
    """
    _seed_state(tmp_path, monkeypatch, entry_price=48.676,
                last_seen_price=48.585, stop_price=46.217)
    _sell_sig(monkeypatch)
    stub = _StubBrokerManage(current_price=48.585)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_100.0)

    assert stub.replace_calls == [], "must not touch the stop while underwater"
    assert any(e["event"] == "sell_signal_underwater" for e in events)
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["stop_price"] == 46.217, "original stop left working"


def test_sell_signal_in_profit_tightens_the_stop_to_entry(tmp_path, monkeypatch):
    """In profit the move is safe and is the point: cap the trade at a scratch
    while leaving the target live in case the signal is wrong."""
    _seed_state(tmp_path, monkeypatch, entry_price=19.94,
                last_seen_price=20.105, stop_price=18.943)
    _sell_sig(monkeypatch)
    stub = _StubBrokerManage(current_price=20.105)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_100.0)

    assert len(stub.replace_calls) == 1
    call = stub.replace_calls[0]
    assert call["stop_price"] == 19.94
    assert call["old"] == "stop_leg_1", "must cancel the resting leg, not stack"
    assert any(e["event"] == "sell_signal_breakeven" for e in events)
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["stop_price"] == 19.94


def test_sell_signal_never_loosens_a_stop_already_past_entry(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch, entry_price=19.94,
                last_seen_price=22.0, stop_price=21.0)
    _sell_sig(monkeypatch)
    stub = _StubBrokerManage(current_price=22.0)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_100.0)

    assert stub.replace_calls == [], "21.0 already beats breakeven"
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["stop_price"] == 21.0


def test_sell_signal_acts_once_not_every_tick(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch, entry_price=19.94,
                last_seen_price=20.105, stop_price=18.943)
    _sell_sig(monkeypatch)
    stub = _StubBrokerManage(current_price=20.105)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_100.0)
    cp.manage_open_positions(now=1_000_105.0)
    cp.manage_open_positions(now=1_000_110.0)

    assert len(stub.replace_calls) == 1, "one replacement, not one per tick"


def test_no_sell_signal_leaves_the_position_alone(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch, entry_price=19.94,
                last_seen_price=20.105, stop_price=18.943)
    _sell_sig(monkeypatch, sell=False)
    stub = _StubBrokerManage(current_price=20.105)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_100.0)

    assert stub.replace_calls == []
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["stop_price"] == 18.943


# ── exits the desk did not place ─────────────────────────────────────────────

class _StubBrokerFills(_StubBrokerManage):
    """Broker that remembers fills the desk never placed (manual close, EOD)."""

    def __init__(self, filled=None, **kw):
        super().__init__(**kw)
        self._filled = list(filled or [])

    def get_filled_orders(self, limit=200, days=None):
        return self._filled


def _sell(sym="NVDA", qty=250, px=41.5, otype="market",
          at="2026-08-07T18:43:51+00:00"):
    return {"symbol": sym, "side": "sell", "qty": qty, "type": otype,
            "filled_avg_price": px, "filled_at": at, "status": "filled"}


def test_a_hand_liquidated_exit_is_priced_and_labelled_from_the_broker(
        tmp_path, monkeypatch):
    """The case that produced four null outcomes on 2026-08-07.

    The desk can only recognise exits it placed itself. A hand-liquidated
    position, an EOD flatten, or a leg whose id was lost to a restart all
    resolved to exit_price=None and were then labelled "stopped_out" by
    default — none of that day's four exits was within 2% of its stop. The
    fills were in the broker's history the whole time.
    """
    _seed_state(tmp_path, monkeypatch, entry_time=1_786_126_000.0,
                tranche_a_order_id=None, tranche_a_target_order_id=None,
                tranche_b_stop_order_id=None, last_seen_price=41.0)
    stub = _StubBrokerFills(position_open=False, filled=[_sell(px=41.5)])
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_786_128_300.0)

    assert events == [{"ticker": "NVDA", "event": "closed",
                       "close_reason": "flattened"}]
    outcome = json.loads(_outcomes_path(tmp_path).read_text().strip())
    assert outcome["exit_price"] == 41.5, "priced off the real fill"
    assert outcome["realized_r_multiple"] is not None, "R is computable now"
    assert outcome["close_reason"] == "flattened", "a market sell is not a stop"


def test_an_unresolvable_exit_says_unknown_rather_than_stopped_out(
        tmp_path, monkeypatch):
    """No label beats a wrong one — the scorecard reads it as observed fact."""
    _seed_state(tmp_path, monkeypatch, tranche_a_order_id=None,
                tranche_a_target_order_id=None, tranche_b_stop_order_id=None)
    stub = _StubBrokerFills(position_open=False, filled=[])
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_010.0)

    assert events[0]["close_reason"] == "unknown"
    outcome = json.loads(_outcomes_path(tmp_path).read_text().strip())
    assert outcome["exit_price"] is None
    assert outcome["realized_r_multiple"] is None


def test_a_broker_stop_fill_is_still_called_a_stop_out(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch, entry_time=1_786_126_000.0,
                tranche_a_order_id=None, tranche_a_target_order_id=None,
                tranche_b_stop_order_id=None)
    stub = _StubBrokerFills(position_open=False,
                            filled=[_sell(px=37.9, otype="stop")])
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_786_128_300.0)
    assert events[0]["close_reason"] == "stopped_out"


def test_a_broker_limit_fill_is_the_target(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch, entry_time=1_786_126_000.0,
                tranche_a_order_id=None, tranche_a_target_order_id=None,
                tranche_b_stop_order_id=None)
    stub = _StubBrokerFills(position_open=False,
                            filled=[_sell(px=46.0, otype="limit")])
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_786_128_300.0)
    assert events[0]["close_reason"] == "target_hit"


def test_an_earlier_round_trip_does_not_price_this_one(tmp_path, monkeypatch):
    """Same symbol, traded twice — the older fill must not be used."""
    _seed_state(tmp_path, monkeypatch, entry_time=1_786_126_000.0,
                tranche_a_order_id=None, tranche_a_target_order_id=None,
                tranche_b_stop_order_id=None)
    stub = _StubBrokerFills(position_open=False, filled=[
        _sell(px=99.0, at="2026-08-07T10:00:00+00:00"),   # before entry
    ])
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_786_128_300.0)
    assert events[0]["close_reason"] == "unknown"
    outcome = json.loads(_outcomes_path(tmp_path).read_text().strip())
    assert outcome["exit_price"] is None


def test_an_explicit_closing_reason_outranks_forensics(tmp_path, monkeypatch):
    """time_stop/thesis_break are the desk saying why IT closed the trade."""
    _seed_state(tmp_path, monkeypatch, entry_time=1_786_126_000.0,
                closing_reason="time_stop", tranche_a_order_id=None,
                tranche_a_target_order_id=None, tranche_b_stop_order_id=None)
    stub = _StubBrokerFills(position_open=False, filled=[_sell(px=41.5)])
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_786_128_300.0)
    assert events[0]["close_reason"] == "time_stop"
    outcome = json.loads(_outcomes_path(tmp_path).read_text().strip())
    assert outcome["exit_price"] == 41.5, "still priced off the real fill"


# ── exit-side shadow log ─────────────────────────────────────────────────────

def _pos_shadow_rows(tmp_path):
    p = _pos_shadow_path(tmp_path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_an_open_position_is_logged_every_tick_including_quiet_ones(
        tmp_path, monkeypatch):
    """"hold" is the row that makes refusals priceable.

    The buy side samples every candidate every poll and records why it did not
    arm; that is what lets shadow_report say what a gate cost. A position left
    the telemetry the instant it opened — 4099 rows on 2026-08-07, all
    status=watching — so the exit side had terminal outcomes and nothing about
    the ticks where it chose to keep holding.
    """
    _seed_state(tmp_path, monkeypatch, last_seen_price=44.0)
    monkeypatch.setattr(cp, "_engine_indicators", lambda: {})
    stub = _StubBrokerManage(current_price=44.0)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_100.0)
    cp.manage_open_positions(now=1_000_105.0)

    rows = _pos_shadow_rows(tmp_path)
    assert len(rows) == 2, "one row per tick, even when nothing happens"
    assert all(r["exit_why"] == "hold" for r in rows)
    assert rows[0]["symbol"] == "NVDA"


def test_the_row_carries_what_an_exit_decision_needs(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch, last_seen_price=44.0)
    monkeypatch.setattr(cp, "_engine_indicators", lambda: {
        "NVDA": {"cm_ok": True, "pctr_ok": False, "cm_rsi_rising": True,
                 "sell_signal": False, "proximity_pct": 67, "cm_rsi": 30.1,
                 "pctr": -80.0}})
    stub = _StubBrokerManage(current_price=44.0)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_100.0)
    r = _pos_shadow_rows(tmp_path)[0]

    # entry 40.5, stop 38.0 -> risk 2.5; price 44.0 -> +1.4R
    assert round(r["unrealized_r"], 2) == 1.40
    assert r["pct_to_stop"] is not None and r["pct_to_target"] is not None
    assert r["cm_ok"] is True and r["sell_signal"] is False
    assert r["has_indicators"] is True
    assert r["hold_sec"] > 0


def test_excursions_track_the_worst_and_best_reached(tmp_path, monkeypatch):
    """MAE/MFE cannot be reconstructed from an outcome row.

    Entry and exit say nothing about the worst and best prices in between,
    which is exactly what tells you a stop was too tight or a target left
    money on the table.
    """
    _seed_state(tmp_path, monkeypatch, last_seen_price=44.0)
    monkeypatch.setattr(cp, "_engine_indicators", lambda: {})
    stub = _StubBrokerManage(current_price=44.0)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    cp.manage_open_positions(now=1_000_100.0)          # +1.4R

    stub.current_price = 39.0                          # dipped below entry
    cp.manage_open_positions(now=1_000_105.0)
    stub.current_price = 46.0                          # then ran
    cp.manage_open_positions(now=1_000_110.0)

    last = _pos_shadow_rows(tmp_path)[-1]
    assert last["mae_r"] < 0, "worst excursion went negative"
    assert last["mfe_r"] > last["unrealized_r"] - 0.01
    assert round(last["mfe_r"], 2) == 2.20             # (46-40.5)/2.5


def test_exit_why_records_what_the_machinery_decided(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch, entry_price=19.94,
                last_seen_price=20.105, stop_price=18.943)
    _sell_sig(monkeypatch)
    stub = _StubBrokerManage(current_price=20.105)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_100.0)

    assert _pos_shadow_rows(tmp_path)[0]["exit_why"] == "sell_signal_breakeven"


def test_the_log_can_be_switched_off(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch, last_seen_price=44.0)
    monkeypatch.setattr(cp, "_engine_indicators", lambda: {})
    monkeypatch.setattr(cp, "_cfg_flag",
                        lambda k, d=True: False if "position_shadow" in k else d)
    stub = _StubBrokerManage(current_price=44.0)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_100.0)
    assert _pos_shadow_rows(tmp_path) == []


def test_telemetry_failure_never_breaks_the_position_manager(
        tmp_path, monkeypatch):
    """A logging bug must not stop stops being managed."""
    _seed_state(tmp_path, monkeypatch, last_seen_price=44.0)
    monkeypatch.setattr(cp, "_engine_indicators", lambda: {})
    monkeypatch.setattr(cp, "POSITION_SHADOW_PATH",
                        tmp_path / "nope" / "\0bad" / "x.jsonl")
    stub = _StubBrokerManage(current_price=44.0)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_100.0)   # must not raise

# ── Day Scalp v0: capital protection + sell strategy ─────────────────────────

def test_place_scaled_entry_refuses_without_stop_or_target(
        tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    stub = _StubBroker()
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    d = _buy_decision(stop_price=0)
    out = cp.place_scaled_entry("nvda", d, account_equity=50_000.0,
                                current_ask=40.5)
    assert out["ok"] is False
    assert "stop and target" in out["error"]
    assert stub.calls == []

    d = _buy_decision(target_1=0)
    out = cp.place_scaled_entry("nvda", d, account_equity=50_000.0,
                                current_ask=40.5)
    assert out["ok"] is False
    assert stub.calls == []


def test_synthetic_dual_tranche_when_day_scalp_enabled(tmp_path, monkeypatch):
    """Day scalp dual: one parent buy, half/half bookkeeping, T1 after fill."""
    _use_tmp_state(tmp_path, monkeypatch)
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_day_scalp_dual_tranche": True,
        "ai_entry_broker_target": True,
        "ai_watch_synth_scale_out_pct": 50.0,
        "ai_watch_synth_trail_pct": 2.5,
        "ai_entry_order_style": "limit",
        "ai_entry_limit_pad_pct": 0.15,
        "ai_max_position_pct": 25.0,
        "ai_watch_arm_below_zone": True,
        "ai_watch_arm_below_zone_max_r": 1.0,
    })
    stub = _StubBroker()
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    decision = _buy_decision(
        synthetic=True, scale_out_pct=50, trail_pct=2.5, target_1=42.0)
    # 200 shares at 40.5 with $2.5 risk → dual 100/100 bookkeeping
    out = cp.place_scaled_entry(
        "nvda", decision, account_equity=50_000.0, risk_pct=1.0,
        current_ask=40.5)
    assert out["ok"] is True
    assert out["qty_a"] == 100
    assert out["qty_b"] == 100
    assert len(stub.calls) == 1
    assert stub.calls[0]["qty"] == 200
    assert stub.calls[0]["target_price"] is None  # partial T1 after fill
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["qty_b"] == 100
    assert state["NVDA"]["t1_attach_pending"] is True
    assert state["NVDA"]["strategy"] == "day_scalp_v0"


def test_manage_attaches_partial_t1_after_fill(tmp_path, monkeypatch):
    """After parent fill, dual book rests a partial limit sell for qty_a."""
    _seed_state(
        tmp_path, monkeypatch,
        qty_a=100, qty_b=100, total_qty=200,
        t1_attach_pending=True,
        tranche_a_target_order_id=None,
        tranche_a_filled=False,
        last_seen_price=41.0,
        target_1=42.0,
        entry_confirmed=True,
    )
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_position_shadow_enabled": False,
        "ai_sell_signal_breakeven": False,
        "ai_heal_unprotected": False,
        "ai_dead_trade_min": 0,
    })
    monkeypatch.setattr(cp, "_engine_indicators", lambda: {})
    stub = _StubBrokerManage(current_price=41.0)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_100.0)
    assert any(e.get("event") == "t1_limit_attached" for e in events)
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["t1_attach_pending"] is False
    assert state["NVDA"]["tranche_a_target_order_id"] == "tp_partial_1"


def test_place_scaled_entry_allows_armable_below_zone(tmp_path, monkeypatch):
    """Price under the band but above the stop still places (Option A geometry)."""
    _use_tmp_state(tmp_path, monkeypatch)
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_day_scalp_dual_tranche": True,
        "ai_entry_broker_target": True,
        "ai_watch_synth_scale_out_pct": 50.0,
        "ai_max_position_pct": 25.0,
        "ai_watch_arm_below_zone": True,
        "ai_watch_arm_below_zone_max_r": 1.0,
    })
    stub = _StubBroker()
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    # zone 40-41, stop 38; ask 39.5 is below floor but armable
    decision = _buy_decision(entry_low=40.0, entry_high=41.0, stop_price=38.0,
                             target_1=42.0)
    out = cp.place_scaled_entry(
        "nvda", decision, account_equity=50_000.0, risk_pct=1.0,
        current_ask=39.5)
    assert out["ok"] is True
    assert stub.calls and stub.calls[0]["qty"] > 0


def test_place_scaled_entry_rebases_stop_when_fill_is_through_plan(tmp_path, monkeypatch):
    """Stop is not live until open — a fill under the plan stop still places."""
    _use_tmp_state(tmp_path, monkeypatch)
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_day_scalp_dual_tranche": True,
        "ai_entry_broker_target": True,
        "ai_watch_synth_scale_out_pct": 50.0,
        "ai_max_position_pct": 25.0,
        "ai_watch_arm_below_zone": True,
        "ai_watch_min_stop_pct": 1.5,
        "ai_broker_stop_enabled": False,
    })
    stub = _StubBroker()
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    decision = _buy_decision(entry_low=5.31, entry_high=5.67, stop_price=5.22,
                             target_1=6.00)
    out = cp.place_scaled_entry(
        "ipwr", decision, account_equity=50_000.0, risk_pct=1.0,
        current_ask=5.10)
    assert out["ok"] is True
    state = json.loads(_state_path(tmp_path).read_text())
    stop = float(state["IPWR"]["stop_price"])
    assert stop < 5.10
    assert stop == pytest.approx(5.10 * 0.985, rel=1e-4)


def test_place_scaled_entry_naked_limit_when_broker_stop_off(tmp_path, monkeypatch):
    """Local ratchet owns the stop — parent buy is a bare limit."""
    _use_tmp_state(tmp_path, monkeypatch)
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_day_scalp_dual_tranche": True,
        "ai_entry_broker_target": True,
        "ai_watch_synth_scale_out_pct": 50.0,
        "ai_max_position_pct": 25.0,
        "ai_broker_stop_enabled": False,
    })
    stub = _StubBroker()
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    decision = _buy_decision(entry_low=40.0, entry_high=41.0, stop_price=38.0,
                             target_1=42.0)
    out = cp.place_scaled_entry(
        "nvda", decision, account_equity=50_000.0, risk_pct=1.0,
        current_ask=40.5)
    assert out["ok"] is True
    assert stub.calls and stub.calls[0].get("naked") is True
    assert not stub.limit_calls or stub.limit_calls[0].get("kind") == "naked_limit"


def test_dead_trade_exits_flat_trade_after_timeout(tmp_path, monkeypatch):
    _seed_state(
        tmp_path, monkeypatch,
        time_stop_days=None, entry_time=1_000_000.0,
        last_seen_price=40.4, mfe_r=0.05, mae_r=-0.1,
        entry_confirmed=True, tranche_a_filled=False,
    )
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_dead_trade_min": 90.0,
        "ai_dead_trade_mfe_r": 0.25,
        "ai_position_shadow_enabled": False,
        "ai_sell_signal_breakeven": False,
        "ai_heal_unprotected": False,
        "ai_entry_limit_ttl_sec": 30.0,
    })
    stub = _StubBrokerManage(order_status="new", position_open=True,
                             current_price=40.4)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    later = 1_000_000.0 + 91 * 60
    events = cp.manage_open_positions(now=later)
    assert any(e["event"] == "dead_trade" for e in events)
    assert stub.closed == ["NVDA"]
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["closing_reason"] == "dead_trade"


def test_left_overbought_does_not_flatten_a_dual_book(tmp_path, monkeypatch):
    """13/19 closes on 2026-08-12 were this path; T1/ratchet own the exit."""
    _seed_state(
        tmp_path, monkeypatch,
        qty_a=21, qty_b=21, total_qty=42,
        entry_confirmed=True, tranche_a_filled=False,
        last_seen_price=58.3, target_1=59.08, stop_price=56.44,
        entry_price=58.09,
        tranche_a_target_order_id="t1",
        t1_attach_pending=False,
    )
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_position_shadow_enabled": False,
        "ai_sell_signal_breakeven": False,
        "ai_heal_unprotected": False,
        "ai_dead_trade_min": 0,
        "ai_watch_exhaustion_rules": True,
        "ai_exit_left_overbought": True,
    })
    monkeypatch.setattr(cp, "_engine_indicators", lambda: {
        "NVDA": {"pctr": -25.0, "pctr_falling": True},
    })
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_watch_exhaustion_rules": True,
        "ai_exit_left_overbought": True,
        "ai_edge_mode": "exhaustion_scalp",
    })

    class _EW:
        @staticmethod
        def live_exhaustion(*a, **k):
            return (-25.0, 20.0, False, True)

        @staticmethod
        def exhaustion_exit_now(probe, cfg):
            return True, "left_overbought"

    import sys
    monkeypatch.setitem(sys.modules, "ai_entry_watch", _EW)
    stub = _StubBrokerManage(current_price=58.3, live_qty=42)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_100.0)
    assert stub.closed == []
    assert any(e.get("event") == "left_overbought_deferred" for e in events)
    # close_out never ran; position still tracked
    state = json.loads(_state_path(tmp_path).read_text())
    assert "NVDA" in state
    assert state["NVDA"].get("closing_reason") is None


def test_adopt_restores_dual_qty_from_entry_ok(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    monkeypatch.setattr(cp, "_latest_entry_ok_event", lambda s: {
        "qty_a": 121, "qty_b": 121, "stop_price": 9.71,
        "target_1": 10.22, "entry_price": 10.03, "ts": 1.0,
        "strategy": "day_scalp_v0",
    })
    monkeypatch.setattr(cp, "_resting_stop_price", lambda s: 9.71)
    monkeypatch.setattr(cp, "log_event", lambda *a, **k: {})
    state: dict = {}
    cp._adopt_unmanaged(
        ["ABCL"],
        {"ABCL": {"qty": 121, "avg_entry": 10.03, "current": 10.20}},
        state,
        2.0,
    )
    pos = state["ABCL"]
    assert pos["qty_a"] == 121 and pos["qty_b"] == 121
    assert pos["tranche_a_filled"] is True
    assert pos["t1_attach_pending"] is False


def test_t1_attach_failure_cools_down(tmp_path, monkeypatch):
    _seed_state(
        tmp_path, monkeypatch,
        qty_a=100, qty_b=100, total_qty=200,
        t1_attach_pending=True,
        tranche_a_target_order_id=None,
        tranche_a_filled=False,
        last_seen_price=41.0,
        target_1=42.0,
        entry_confirmed=True,
    )
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_position_shadow_enabled": False,
        "ai_sell_signal_breakeven": False,
        "ai_heal_unprotected": False,
        "ai_dead_trade_min": 0,
    })
    monkeypatch.setattr(cp, "_engine_indicators", lambda: {})

    class _FailT1(_StubBrokerManage):
        def place_limit_sell(self, ticker, qty, limit_price, **kwargs):
            self.canceled.append(f"limit_fail:{ticker}")
            return {"ok": False, "error": "held_for_orders"}

    stub = _FailT1(current_price=41.0, live_qty=200)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    cp.manage_open_positions(now=1_000_100.0)
    n_free = stub.canceled.count("free:NVDA")
    assert n_free >= 1
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["t1_attach_cooldown_until"] == 1_000_190.0
    # Second tick inside the window must not free+retry.
    stub.canceled.clear()
    cp.manage_open_positions(now=1_000_120.0)
    assert "free:NVDA" not in stub.canceled


def test_heal_dual_tranche_keeps_t1_and_places_runner_stop(tmp_path, monkeypatch):
    """Heal must not prefer_stop_over_target on a dual book (ABCL 2026-08-12)."""
    _seed_state(
        tmp_path, monkeypatch,
        qty_a=121, qty_b=121, total_qty=242,
        t1_attach_pending=False,
        tranche_a_target_order_id="t1_abcl",
        tranche_a_filled=False,
        last_seen_price=9.97,
        entry_price=10.03,
        stop_price=9.71,
        target_1=10.22,
        entry_confirmed=True,
    )
    state = json.loads(_state_path(tmp_path).read_text())

    class _HealStub(_StubBrokerManage):
        def place_stop_sell(self, ticker, stop_price, qty=None):
            self.replace_calls.append({
                "ticker": ticker, "stop_price": stop_price, "qty": qty,
            })
            return {"ok": True, "order_id": "runner_stop_heal", "qty": qty}

    stub = _HealStub(current_price=9.97)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    def _flag(key, default=True):
        if key in ("ai_heal_unprotected", "ai_broker_stop_enabled"):
            return True
        return default

    monkeypatch.setattr(cp, "_cfg_flag", _flag)
    ev = cp._heal_unprotected(
        [{"symbol": "NVDA", "managed": True}], state)
    assert ev and ev[0]["event"] == "unprotected_healed"
    assert ev[0].get("kept_t1") is True
    assert ev[0]["qty"] == 121
    assert stub.canceled == []
    assert state["NVDA"]["tranche_a_target_order_id"] == "t1_abcl"
    assert any(c.get("qty") == 121 for c in stub.replace_calls)


def test_qty_drop_infers_t1_and_raises_runner_to_breakeven(tmp_path, monkeypatch):
    """Broker half-size with lost T1 order id still ratchets (IONQ/ABCL)."""
    _seed_state(
        tmp_path, monkeypatch,
        qty_a=121, qty_b=121, total_qty=242,
        entry_price=10.03, stop_price=9.71, target_1=10.22,
        risk_per_share=0.32, runner_trail_r=1.0,
        tranche_a_target_order_id=None,
        t1_attach_pending=False,
        tranche_a_filled=False,
        breakeven_done=False,
        last_seen_price=10.03,
        entry_confirmed=True,
        peak_price=10.23,
    )
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_position_shadow_enabled": False,
        "ai_sell_signal_breakeven": False,
        "ai_heal_unprotected": False,
        "ai_dead_trade_min": 0,
    })
    monkeypatch.setattr(cp, "_engine_indicators", lambda: {})
    stub = _StubBrokerManage(
        order_status="new", current_price=10.23, live_qty=121,
        open_orders=[{
            "id": "stop_old", "symbol": "NVDA", "side": "sell",
            "type": "stop", "status": "new", "stop": 9.71,
        }],
    )
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    events = cp.manage_open_positions(now=1_000_100.0)
    assert any(e.get("event") == "t1_fill_inferred" for e in events)
    assert any(e.get("event") == "scaled_out" for e in events)
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["tranche_a_filled"] is True
    assert state["NVDA"]["runner_stop_price"] == 10.03
    assert stub.replace_calls[-1]["stop_price"] == 10.03


def test_ratchet_invariant_fails_when_scaled_stop_still_original():
    state = {
        "ABCL": {
            "entry_confirmed": True, "qty_a": 121, "qty_b": 121,
            "entry_price": 10.03, "stop_price": 9.71,
            "tranche_a_filled": True,
        },
    }
    detail = {"ABCL": {"qty": 121, "current": 10.22}}
    orders = [{
        "symbol": "ABCL", "side": "sell", "type": "stop_limit",
        "stop": 9.71, "limit": 9.61,
    }]
    rows = cp.evaluate_ratchet_invariants(state, detail, orders)
    assert len(rows) == 1
    assert rows[0]["ok"] is False
    assert rows[0]["event"] == "ratchet_stop_below_entry"


def test_ratchet_invariant_passes_when_runner_locked_at_entry():
    state = {
        "ABCL": {
            "entry_confirmed": True, "qty_a": 121, "qty_b": 121,
            "entry_price": 10.03, "stop_price": 10.03,
            "runner_stop_price": 10.03,
            "tranche_a_filled": True,
        },
    }
    detail = {"ABCL": {"qty": 121, "current": 10.22}}
    orders = [{
        "symbol": "ABCL", "side": "sell", "type": "stop",
        "stop": 10.03,
    }]
    rows = cp.evaluate_ratchet_invariants(state, detail, orders)
    assert rows[0]["ok"] is True
    assert rows[0]["event"] == "ratchet_ok"


def test_dead_trade_skips_when_mfe_proves_the_trade(tmp_path, monkeypatch):
    _seed_state(
        tmp_path, monkeypatch,
        time_stop_days=None, entry_time=1_000_000.0,
        last_seen_price=41.5, mfe_r=0.5, entry_confirmed=True, peak_price=41.8,
    )
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_dead_trade_min": 90.0,
        "ai_dead_trade_mfe_r": 0.25,
        "ai_position_shadow_enabled": False,
        "ai_sell_signal_breakeven": False,
        "ai_heal_unprotected": False,
    })
    stub = _StubBrokerManage(position_open=True, current_price=41.5)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    later = 1_000_000.0 + 91 * 60
    events = cp.manage_open_positions(now=later)
    assert not any(e["event"] == "dead_trade" for e in events)
    assert stub.closed == []


# ── outcome data quality is reported, not swallowed ─────────────────────────

def _write_outcomes(tmp_path, *rows):
    _outcomes_path(tmp_path).write_text(
        "".join(json.dumps(r) + "\n" for r in rows))


def test_performance_summary_counts_trades_it_cannot_grade(
        tmp_path, monkeypatch):
    """An unresolved exit fill must show up as a number, not vanish. It is
    excluded from realized R *and* from the daily-loss gate, so a silent skip
    reports a clean scorecard computed off an unknown fraction of the day."""
    _use_tmp_state(tmp_path, monkeypatch)
    _write_outcomes(
        tmp_path,
        {"symbol": "AAA", "realized_r_multiple": 0.6, "realized_pl_usd": 60.0,
         "close_reason": "target_hit", "exit_time": 10.0, "hold_days": 0.0},
        {"symbol": "BBB", "realized_r_multiple": None, "exit_price": None,
         "close_reason": "unknown", "exit_time": 20.0},
        {"symbol": "CCC", "realized_r_multiple": None, "exit_price": None,
         "close_reason": "unknown", "exit_time": 30.0},
    )

    s = cp.performance_summary()

    assert s["count"] == 1                      # graded
    assert s["ungraded"] == 2
    assert s["ungraded_symbols"] == ["BBB", "CCC"]
    assert s["unknown_close_reason"] == 2
    assert s["win_rate"] == 1.0                 # the graded row only


def test_performance_summary_reports_quality_even_with_nothing_gradable(
        tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    _write_outcomes(
        tmp_path,
        {"symbol": "AAA", "realized_r_multiple": None, "close_reason": "unknown",
         "exit_time": 10.0},
    )

    s = cp.performance_summary()

    assert s["count"] == 0
    assert s["ungraded"] == 1        # not reported as "no trading happened"


# ── spread measured in R, not in percent of price ───────────────────────────

def test_spread_gate_in_r_rejects_a_cheap_looking_spread_on_a_tight_stop():
    """A 0.20% spread reads as negligible against price, but against a 0.50%
    stop the round trip is 80% of 1R."""
    ok, why = cp.pre_entry_gate(
        "AAA", ask=10.0, bid=9.98, account_equity=50_000.0,
        stop_price=9.95, max_spread_r=0.25, max_spread_pct=0.0,
    )
    assert ok is False
    assert why.startswith("spread_r_0.80>")


def test_spread_gate_in_r_passes_when_the_risk_unit_is_wide_enough():
    # Same 0.02 spread, but a 5% stop: round trip is 0.008R.
    ok, why = cp.pre_entry_gate(
        "AAA", ask=10.0, bid=9.98, account_equity=50_000.0,
        stop_price=9.50, max_spread_r=0.25, max_spread_pct=0.0,
    )
    assert (ok, why) == (True, "")


def test_spread_gate_in_r_is_off_by_default():
    """Shipped off: the live path reads IEX quotes, which always look wide."""
    ok, why = cp.pre_entry_gate(
        "AAA", ask=10.0, bid=9.98, account_equity=50_000.0,
        stop_price=9.95, max_spread_pct=0.0,
    )
    assert (ok, why) == (True, "")


def test_entry_slippage_is_measured_against_the_limit_in_r(
        tmp_path, monkeypatch):
    """The honest answer to 'what does crossing cost' — a real fill against the
    limit we asked for, never a quote."""
    _seed_state(tmp_path, monkeypatch, entry_confirmed=False,
                entry_limit_price=40.50, risk_per_share=2.5,
                tranche_a_order_id="order_a")
    stub = _StubBrokerManage(order_status="new", fills={"order_a": 40.75})
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)

    cp.manage_open_positions(now=1_000_100.0)

    state = json.loads(_state_path(tmp_path).read_text())
    # paid 40.75 against a 40.50 limit, on 2.50 of risk = 0.10R
    assert state["NVDA"]["entry_slippage_r"] == pytest.approx(0.10)
    assert state["NVDA"]["entry_price"] == 40.75


def test_never_lower_rstop_keeps_the_high_water():
    assert cp.never_lower_rstop(3.18, 3.01, 2.95) == pytest.approx(3.18)
    assert cp.never_lower_rstop(None, 3.01) == pytest.approx(3.01)
    assert cp.never_lower_rstop(None, 0, None) is None


def test_local_trail_give_is_give_r_times_risk():
    cfg = {"ai_local_trail_give_r": 0.08, "ai_local_trail_give_px": 0}
    # $1 of R → 8 cents. $0.54 of R → 4.32 cents (floored at a penny).
    assert cp.local_trail_give(17.44, 1.00, cfg) == pytest.approx(0.08)
    assert cp.local_trail_give(17.44, 0.54, cfg) == pytest.approx(0.0432)
    # No R: 0.08% of last.
    assert cp.local_trail_give(44.69, 0, cfg) == pytest.approx(0.035752)


def test_local_trail_give_r_stays_wide_until_025r_then_snaps():
    cfg = {
        "ai_local_trail_give_r": 0.10,
        "ai_local_trail_give_open_r": 0.20,
        "ai_local_trail_tighten_mfe_r": 0.25,
    }
    assert cp.local_trail_give_r(0.0, cfg) == pytest.approx(0.20)
    assert cp.local_trail_give_r(0.08, cfg) == pytest.approx(0.20)
    assert cp.local_trail_give_r(0.25, cfg) == pytest.approx(0.20)
    assert cp.local_trail_give_r(0.26, cfg) == pytest.approx(0.10)
    assert cp.local_trail_give_r(1.20, cfg) == pytest.approx(0.10)


def test_local_profit_stop_uses_wide_give_at_open():
    pos = {
        "entry_price": 10.00, "entry_stop_price": 9.80,
        "risk_per_share": 0.20, "last_seen_price": 10.00, "mfe_r": 0.0,
    }
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_local_trail_give_r": 0.10,
        "ai_local_trail_give_open_r": 0.20,
        "ai_local_trail_tighten_mfe_r": 0.25,
    }
    # Open RSTOP is the fill, not last − 0.20R (9.96).
    assert cp.local_profit_stop(pos, cfg) == pytest.approx(10.00)
    pos["last_seen_price"] = 10.02
    pos["mfe_r"] = 0.10
    # first green still 0.20R under last, floored at entry
    assert cp.local_profit_stop(pos, cfg) == pytest.approx(10.00)
    pos["last_seen_price"] = 10.06
    pos["mfe_r"] = 0.30
    # past +0.25R: last − 0.10R = 10.04
    assert cp.local_profit_stop(pos, cfg) == pytest.approx(10.04)


def test_local_profit_stop_trails_last_immediately_when_arm_r_zero():
    pos = {
        "entry_price": 19.41, "entry_stop_price": 18.88,
        "risk_per_share": 0.53, "last_seen_price": 19.43, "mfe_r": 0.038,
        "local_stop_price": 18.88,
    }
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_local_trail_arm_r": 0.0,
        "ai_local_trail_give_r": 0.10,
        "ai_local_trail_give_open_r": 0.20,
        "ai_local_trail_tighten_mfe_r": 0.25,
    }
    # last − 0.20R is 19.324; open floor is entry 19.41
    want = cp.local_profit_stop(pos, cfg)
    assert want == pytest.approx(19.41)


def test_local_profit_stop_holds_plan_stop_until_arm_r():
    pos = {
        "entry_price": 10.00, "entry_stop_price": 9.80,
        "risk_per_share": 0.20, "last_seen_price": 10.02, "mfe_r": 0.10,
        "local_stop_price": 9.80,
    }
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_local_trail_arm_r": 0.20,
        "ai_local_trail_give_r": 0.10,
        "ai_local_trail_give_open_r": 0.20,
    }
    # Unarmed: hold current shelf or entry, not a drop to plan.
    assert cp.local_profit_stop(pos, cfg) == pytest.approx(9.80)
    pos["last_seen_price"] = 10.06
    pos["mfe_r"] = 0.30
    # Armed, mfe 0.30 > 0.25: last − 0.10R = 10.04
    want = cp.local_profit_stop(pos, cfg)
    assert want == pytest.approx(10.04)


def test_local_profit_stop_uses_dollar_give_when_set():
    pos = {
        "entry_price": 17.00, "entry_stop_price": 16.46,
        "risk_per_share": 0.54, "last_seen_price": 17.12,
        "local_stop_price": 16.46,
    }
    cfg = {"ai_local_trail_enabled": True, "ai_local_trail_give_px": 0.05}
    assert cp.local_profit_stop(pos, cfg) == pytest.approx(17.07)


def test_local_profit_stop_tracks_last_minus_give():
    pos = {
        "entry_price": 8.64, "entry_stop_price": 8.38,
        "risk_per_share": 0.26, "last_seen_price": 8.50,
    }
    cfg = {"ai_local_trail_enabled": True, "ai_local_trail_give_r": 0.20}
    # Underwater: RSTOP is entry, not last − give.
    assert cp.local_profit_stop(pos, cfg) == pytest.approx(8.64)
    pos["last_seen_price"] = 8.80
    assert cp.local_profit_stop(pos, cfg) == pytest.approx(8.748)


def test_local_profit_stop_never_lowers():
    pos = {
        "entry_price": 8.64, "entry_stop_price": 8.38,
        "risk_per_share": 0.26, "last_seen_price": 8.70,
        "local_stop_price": 8.71,
    }
    cfg = {"ai_local_trail_enabled": True, "ai_local_trail_give_r": 0.20}
    pos["last_seen_price"] = 8.60
    assert cp.local_profit_stop(pos, cfg) == pytest.approx(8.71)


def test_local_trail_flattens_when_tape_prints_through_even_if_broker_is_above(
        tmp_path, monkeypatch):
    """Board stop is the liquidation line: any this-tick print at/under sells."""
    _seed_state(
        tmp_path, monkeypatch,
        entry_price=8.64, entry_stop_price=8.38, stop_price=8.38,
        risk_per_share=0.26, target_1=8.79,
        last_seen_price=8.90, peak_price=8.90, mfe_r=1.0,
        local_stop_price=8.71,
        tranche_a_filled=False, entry_confirmed=True,
    )
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_local_trail_give_r": 0.20,
        "ai_stale_data_max_age_sec": 15.0,
        "ai_position_shadow_enabled": False,
        "ai_dead_trade_min": 0,
        "ai_sell_signal_breakeven": False,
        "ai_watch_exhaustion_rules": False,
    }
    monkeypatch.setattr(cp, "_cfg_all", lambda: cfg)
    monkeypatch.setattr(cp, "_entry_cfg", lambda: cfg)
    monkeypatch.setattr(cp, "_cfg_flag", lambda key, default=True: {
        "ai_local_trail_enabled": True,
        "ai_watch_exhaustion_rules": False,
        "ai_sell_signal_breakeven": False,
    }.get(key, default))
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "live_print", lambda _sym: (8.70, 0.2))
    stub = _StubBrokerManage(order_status="new", current_price=8.95)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    events = cp.manage_open_positions(now=1_000_100.0)
    assert any(e.get("event") == "local_trail" for e in events)
    assert "NVDA" in stub.closed
    trail = next(e for e in events if e.get("event") == "local_trail")
    assert trail["last"] == pytest.approx(8.70)
    assert trail["stop"] == pytest.approx(8.71)


def test_local_trail_flattens_when_last_breaks_the_shelf(tmp_path, monkeypatch):
    _seed_state(
        tmp_path, monkeypatch,
        entry_price=8.64, entry_stop_price=8.38, stop_price=8.38,
        risk_per_share=0.26, target_1=8.79,
        last_seen_price=8.76, peak_price=8.76, mfe_r=0.46,
        local_stop_price=8.71,
        tranche_a_filled=False, entry_confirmed=True,
    )
    cfg = {
        "ai_local_trail_enabled": True,
        "ai_local_trail_arm_r": 0.20,
        "ai_local_trail_give_r": 0.20,
        "ai_position_shadow_enabled": False,
        "ai_dead_trade_min": 0,
        "ai_sell_signal_breakeven": False,
        "ai_watch_exhaustion_rules": False,
    }
    monkeypatch.setattr(cp, "_cfg_all", lambda: cfg)
    monkeypatch.setattr(cp, "_entry_cfg", lambda: cfg)
    monkeypatch.setattr(cp, "_cfg_flag", lambda key, default=True: {
        "ai_local_trail_enabled": True,
        "ai_watch_exhaustion_rules": False,
        "ai_sell_signal_breakeven": False,
    }.get(key, default))
    stub = _StubBrokerManage(order_status="new", current_price=8.70)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    events = cp.manage_open_positions(now=1_000_100.0)
    assert any(e.get("event") == "local_trail" for e in events)
    assert "NVDA" in stub.closed
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["closing_reason"] == "local_trail"


def test_stale_data_flattens_after_max_age(tmp_path, monkeypatch):
    _seed_state(
        tmp_path, monkeypatch,
        entry_confirmed=True, last_seen_price=40.5,
        stale_since=1_000_000.0,
    )
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=True: True)
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_stale_data_flatten": True, "ai_stale_data_max_age_sec": 15.0,
        "ai_local_trail_enabled": False, "ai_dead_trade_min": 0,
        "ai_watch_exhaustion_rules": False,
    })
    monkeypatch.setattr(cp, "_rth_now", lambda now: True)
    monkeypatch.setattr(cp, "quote_is_live", lambda *a, **k: (False, "none"))
    stub = _StubBrokerManage(current_price=40.5)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    events = cp.manage_open_positions(now=1_000_020.0)
    assert any(e.get("event") == "stale_data" for e in events)
    assert stub.closed == ["NVDA"]
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"]["closing_reason"] == "stale_data"


def test_stale_data_does_not_flatten_on_first_miss(tmp_path, monkeypatch):
    _seed_state(tmp_path, monkeypatch, entry_confirmed=True)
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=True: True)
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_stale_data_flatten": True, "ai_stale_data_max_age_sec": 15.0,
        "ai_local_trail_enabled": False, "ai_dead_trade_min": 0,
        "ai_watch_exhaustion_rules": False,
    })
    monkeypatch.setattr(cp, "_rth_now", lambda now: True)
    monkeypatch.setattr(cp, "quote_is_live", lambda *a, **k: (False, "none"))
    stub = _StubBrokerManage(current_price=40.5)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    events = cp.manage_open_positions(now=1_000_100.0)
    assert not any(e.get("event") == "stale_data" for e in events)
    assert stub.closed == []
    state = json.loads(_state_path(tmp_path).read_text())
    assert state["NVDA"].get("stale_since") == 1_000_100.0


def test_desk_click_flattens_when_already_long(monkeypatch):
    stub = _StubBroker()
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    import ai_trading as gt
    monkeypatch.setattr(gt, "has_open_position", lambda _s: True)
    monkeypatch.setattr(gt, "is_ready", lambda: True)
    out = cp.desk_click("umac")
    assert out["ok"] is True
    assert out["action"] == "flatten"
    assert stub.close_calls == ["UMAC"]


def test_desk_click_buys_when_not_open(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    stub = _StubBroker()
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    import ai_trading as gt
    monkeypatch.setattr(gt, "has_open_position", lambda _s: False)
    monkeypatch.setattr(gt, "is_ready", lambda: True)
    monkeypatch.setattr(gt, "get_account", lambda: {"equity": 10_000.0})
    monkeypatch.setattr(gt, "record_external_buy", lambda *a, **k: None)
    import ai_entry_watch as ew
    structure = _buy_decision()
    monkeypatch.setattr(ew, "load_watch", lambda: {
        "LUNR": {"symbol": "LUNR", "structure": structure, "source": "desk"},
    })
    monkeypatch.setattr(ew, "decision_price", lambda *a, **k: (40.5, "stream", 0.2))
    monkeypatch.setattr(ew, "_structure_usable", lambda s: True)
    monkeypatch.setattr(
        ew, "_decision_for_place",
        lambda s, **k: dict(s, desk_force=True, entry_path="desk_click"))
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_day_scalp_dual_tranche": True,
        "ai_max_position_pct": 25.0,
        "ai_risk_pct": 1.0,
        "ai_watch_arm_below_zone": True,
    })
    out = cp.desk_click("LUNR")
    assert out["ok"] is True
    assert out["action"] == "buy"
    assert stub.calls


def test_desk_click_still_buys_when_clock_says_closed(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    stub = _StubBroker(market_open=False)
    monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
    import ai_trading as gt
    monkeypatch.setattr(gt, "has_open_position", lambda _s: False)
    monkeypatch.setattr(gt, "is_ready", lambda: True)
    monkeypatch.setattr(gt, "get_account", lambda: {"equity": 10_000.0})
    monkeypatch.setattr(gt, "record_external_buy", lambda *a, **k: None)
    import ai_entry_watch as ew
    structure = _buy_decision()
    monkeypatch.setattr(ew, "load_watch", lambda: {
        "VWAV": {"symbol": "VWAV", "structure": structure, "source": "desk"},
    })
    monkeypatch.setattr(ew, "decision_price", lambda *a, **k: (40.5, "stream", 0.2))
    monkeypatch.setattr(ew, "_structure_usable", lambda s: True)
    monkeypatch.setattr(
        ew, "_decision_for_place",
        lambda s, **k: dict(s, desk_force=True, entry_path="desk_click"))
    monkeypatch.setattr(cp, "_entry_cfg", lambda: {
        "ai_day_scalp_dual_tranche": True,
        "ai_max_position_pct": 25.0,
        "ai_risk_pct": 1.0,
        "ai_broker_stop_enabled": False,
    })
    out = cp.desk_click("VWAV")
    assert out["ok"] is True, out
    assert out["action"] == "buy"
    assert stub.calls


def test_infer_t1_refuses_qty_drop_unless_price_reached_t1():
    pos = {
        "qty_a": 143, "qty_b": 143, "target_1": 8.499,
        "peak_price": 8.40, "last_seen_price": 8.08,
    }
    assert cp._infer_t1_fill(pos, 143.0) is False
    pos["peak_price"] = 8.50
    assert cp._infer_t1_fill(pos, 143.0) is True
