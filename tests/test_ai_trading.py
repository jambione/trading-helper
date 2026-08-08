"""Claude paper-trading tools: safety rails and dispatch."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import ai_trading as gt  # noqa: E402


def test_tool_definitions_include_buy_and_sell():
    names = {t["name"] for t in gt.tool_definitions()}
    assert names == {"buy_stock", "sell_stock", "list_positions", "get_account"}


def test_buy_blocked_when_not_ready():
    # Fresh module state without init → off
    gt._ready = False
    gt._mode = "off"
    out = gt.buy_stock("NVDA", reason="test")
    assert out["ok"] is False
    assert "off" in (out.get("error") or "").lower() or "need" in (out.get("error") or "").lower()


def test_invalid_symbol_rejected_even_when_ready():
    gt._ready = True
    gt._mode = "paper"
    gt._buys_this_poll = 0
    gt._max_buys_per_poll = 3
    out = gt.buy_stock("TOOLONGSYMBOL")
    assert out["ok"] is False
    assert "invalid" in (out.get("error") or "").lower()


def test_buy_cap_per_poll():
    gt._ready = True
    gt._mode = "paper"
    gt._buys_this_poll = 3
    gt._max_buys_per_poll = 3
    out = gt.buy_stock("AAPL")
    assert out["ok"] is False
    assert "cap" in (out.get("error") or "").lower()


def test_execute_tool_unknown():
    out = gt.execute_tool("explode", {})
    assert out["ok"] is False
    assert "unknown" in (out.get("error") or "").lower()


def test_trading_system_addon_mentions_paper():
    text = gt.trading_system_addon()
    assert "PAPER" in text.upper()
    assert "buy_stock" in text


# ── helpers used by ai_positions.py's risk-sized entry path ─────────────

def test_buys_left_this_poll_reflects_the_cap():
    gt._max_buys_per_poll = 3
    gt._buys_this_poll = 1
    assert gt.buys_left_this_poll() == 2
    gt._buys_this_poll = 3
    assert gt.buys_left_this_poll() == 0
    gt._buys_this_poll = 5  # never negative even if something over-bought
    assert gt.buys_left_this_poll() == 0


def test_record_external_buy_counts_against_the_same_cap_as_buy_stock():
    """claude_positions places orders directly against alpaca_trader, bypassing
    buy_stock() — this is what keeps that path counted against the same
    per-poll cap rather than an unlimited side door."""
    gt._max_buys_per_poll = 3
    gt._buys_this_poll = 0
    gt.record_external_buy("nvda", {"stop_price": 38.0, "target_1": 46.0})
    assert gt.buys_left_this_poll() == 2

    logged = gt.recent_trades(5)
    assert logged[-1]["symbol"] == "NVDA"
    assert logged[-1]["stop_price"] == 38.0


def test_can_open_new_position_allows_under_cap_without_a_live_client():
    gt._max_positions = 5
    assert gt.can_open_new_position("NVDA") is True


def test_has_open_position_false_without_a_live_client():
    assert gt.has_open_position("NVDA") is False
