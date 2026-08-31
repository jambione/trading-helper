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


def test_invalidate_quotes_drops_cached_nbbo():
    gt._quote_cache.clear()
    gt._quote_cache["AAA"] = (0.0, 10.0, 9.9)
    gt._quote_cache["BBB"] = (0.0, 20.0, 19.9)
    assert gt.invalidate_quotes(["aaa"]) == 1
    assert "AAA" not in gt._quote_cache
    assert "BBB" in gt._quote_cache
    assert gt.invalidate_quotes() == 1
    assert gt._quote_cache == {}


def test_refresh_quotes_now_busts_cache_then_primes(monkeypatch):
    gt._quote_cache["SMCI"] = (0.0, 1.0, 0.9)
    primed = []

    def fake_prime(symbols):
        primed.append(list(symbols))
        for s in symbols:
            gt._quote_cache[s] = (1.0, 28.0, 27.9)
        return len(symbols)

    monkeypatch.setattr(gt, "prime_quotes", fake_prime)
    n = gt.refresh_quotes_now(["smci"])
    assert n == 1
    assert primed == [["SMCI"]]
    assert gt._quote_cache["SMCI"][1] == 28.0


def test_can_open_new_position_allows_under_cap_without_a_live_client():
    gt._max_positions = 5
    assert gt.can_open_new_position("NVDA") is True


def test_effective_max_positions_scales_with_equity():
    gt._max_positions = 8
    gt._slot_equity = 250.0
    gt._max_position_pct = 8.0
    assert gt.effective_max_positions(250.0) == 1
    assert gt.effective_max_positions(500.0) == 2
    assert gt.effective_max_positions(10_000.0) == 8
    # No live equity → configured ceiling (tests / trader-off).
    assert gt.effective_max_positions(None) == 8


def test_has_open_position_false_without_a_live_client():
    assert gt.has_open_position("NVDA") is False


def test_latest_ask_stamps_quote_ts_when_cache_misses(monkeypatch):
    """A REST-priced ask must carry its own quote age.

    Regression for 2026-08-31: _latest_ask consulted desk_actions._latest_ask
    before its own fallback. That path makes the same get_stock_latest_quote
    call but returns a bare float, so _quote_ts was never written and
    cached_quote_age_sec returned None. _row_tape_stale fails closed on an
    unknown age — correctly — so the row was refused for "no quote age" while
    holding a good ask. 4 of 13 rows were stuck that way at 10:35 ET.

    The test drives the real path: empty price cache, a stubbed Alpaca client
    returning a timestamped quote, and asserts the ask comes back WITH a
    provable age rather than None.
    """
    import datetime as _dt
    import sys
    import types

    import ai_trading as t

    qt = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=2)

    class _Q:
        ask_price = 4.25
        bid_price = 4.20
        timestamp = qt

    class _Client:
        def __init__(self, *a, **k):
            pass

        def get_stock_latest_quote(self, req):
            return {"ZZZT": _Q()}

    # desk_actions lives under momentum-monitor/ and is NOT importable here,
    # so the old tier-2 `try: import desk_actions` raised and fell through to
    # the stamping path regardless — the test would pass either way. Install a
    # working stub so tier 2 is genuinely reachable: if it ever returns, the
    # ask is 99.0 with no age and this test fails, which is the whole point.
    _stub = types.ModuleType("desk_actions")
    _stub._latest_ask = lambda sym: 99.0  # bare float, no timestamp
    monkeypatch.setitem(sys.modules, "desk_actions", _stub)

    monkeypatch.setattr(t, "_quote_cache", {}, raising=False)
    monkeypatch.setattr(t, "_quote_ts", {}, raising=False)
    monkeypatch.setattr(t, "_load_env", lambda: None, raising=False)
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    monkeypatch.setattr(
        "alpaca.data.historical.StockHistoricalDataClient", _Client)

    ask = t._latest_ask("ZZZT")
    age = t.cached_quote_age_sec("ZZZT")

    assert ask == 4.25
    assert age is not None, "REST ask came back without a provable quote age"
    assert 0.0 <= age < 60.0


def test_latest_ask_does_not_delegate_to_desk_actions():
    """Pin the leak shut: the desk_actions tier must not come back.

    It sits above the stamping fallback, so reintroducing it silently
    reinstates ask-without-age for every symbol past the 3s cache TTL.
    """
    import inspect

    import ai_trading as t

    src = inspect.getsource(t._latest_ask)
    body = "\n".join(
        ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert "desk_actions" not in body, (
        "_latest_ask delegates to desk_actions again — that path returns a "
        "bare float and drops the quote timestamp")
