"""Fixed-$ limit-at-ask buy path in alpaca_trader + desk_actions routing.

Mirrors tests/test_alpaca_brackets.py: mock the TradingClient's submit_order
and assert on the request object.
"""
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import alpaca_trader as tr

# momentum-monitor/ isn't an importable package (hyphen) — add it to the path.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "momentum-monitor"))
import desk_actions as desk  # noqa: E402
import mac_agent as ma  # noqa: E402  (repo root is on sys.path under `python -m pytest`)


@pytest.fixture(autouse=True)
def _restore_protection_guard():
    """_arm_trader rebinds module-level globals, which would otherwise leak
    into every later test file and silently disable the no-naked-buy policy.

    market_is_open and the session cache are here for the same reason: a stub
    left behind by one test decides what session a later one thinks it is in,
    and the session now controls whether an order carries extended_hours.
    """
    orig = tr._require_protective_exit
    orig_clock = tr.market_is_open
    orig_cache = tr._clock_cache
    yield
    tr._require_protective_exit = orig
    tr.market_is_open = orig_clock
    tr._clock_cache = orig_cache


def _arm_trader(fake, *, extended=False, amount=1000.0, protected=False,
                market_open=False):
    tr._mode = "paper"
    tr._client = fake
    tr._trade_amount = amount
    # `_extended_hours` is desk POLICY; what lands on the order is
    # ext_hours_now(), which is that policy AND the session. Both have to be
    # stated or the fake MagicMock clock decides — its get_clock().is_open is
    # a truthy Mock, so every test would silently read as "market open".
    tr._extended_hours = extended
    tr._clock_cache = (0.0, False)          # never inherit a cached session
    tr.market_is_open = lambda: bool(market_open)
    # These tests assert order MECHANICS — qty rounding, limit price, TIF — on
    # the bare buy helpers, which the broker layer now refuses by default (no
    # protective exit, no position; see test_protective_exit_required.py).
    # Disable the policy here so the mechanics stay covered; the policy has its
    # own tests and is not what these are checking.
    tr._require_protective_exit = lambda: bool(protected)


# ── alpaca_trader.buy_limit_at_ask ────────────────────────────────────────────

def test_limit_at_ask_qty_price_and_tif():
    fake = MagicMock()
    fake.submit_order.return_value = SimpleNamespace(id="l-1", status="accepted")
    _arm_trader(fake, extended=True, amount=1000.0)

    with patch.object(tr, "_log_action"):
        out = tr.buy_limit_at_ask("AAPL", ask=50.0, dollar_amount=1000.0, pad_pct=0.1)

    assert out["ok"] is True
    assert out["order_id"] == "l-1"
    assert out["qty"] == 19            # 1000 // round(50*1.001, 2) == 1000 // 50.05
    assert isinstance(out["qty"], int) and out["qty"] == int(out["qty"])  # whole shares
    assert out["limit_px"] == 50.05

    req = fake.submit_order.call_args[0][0]
    assert float(req.qty) == 19.0
    assert float(req.qty).is_integer()             # never fractional
    assert float(req.limit_price) == 50.05
    assert str(req.time_in_force).lower().endswith("day")
    # Policy on AND outside RTH — the one combination that carries the flag.
    assert bool(req.extended_hours) is True


def test_limit_at_ask_extended_flag_off():
    fake = MagicMock()
    fake.submit_order.return_value = SimpleNamespace(id="l-2", status="accepted")
    _arm_trader(fake, extended=False, amount=1000.0)

    with patch.object(tr, "_log_action"):
        out = tr.buy_limit_at_ask("MSFT", ask=100.0, dollar_amount=1000.0, pad_pct=0.0)

    assert out["ok"] and out["qty"] == 10 and out["limit_px"] == 100.0
    req = fake.submit_order.call_args[0][0]
    assert bool(req.extended_hours) is False


def test_limit_at_ask_under_budget_skips():
    fake = MagicMock()
    _arm_trader(fake, amount=1000.0)

    with patch.object(tr, "_log_action"):
        out = tr.buy_limit_at_ask("BRK.A", ask=1500.0, dollar_amount=1000.0, pad_pct=0.1)

    assert out["ok"] is False
    assert out["status"] == "under_budget"
    fake.submit_order.assert_not_called()


def test_limit_at_ask_no_ask_skips():
    fake = MagicMock()
    _arm_trader(fake, amount=1000.0)

    with patch.object(tr, "_log_action"):
        out = tr.buy_limit_at_ask("NVDA", ask=0.0, dollar_amount=1000.0)

    assert out["ok"] is False
    assert out["status"] == "no_ask"
    fake.submit_order.assert_not_called()


def test_limit_at_ask_defaults_to_module_trade_amount():
    fake = MagicMock()
    fake.submit_order.return_value = SimpleNamespace(id="l-3", status="accepted")
    _arm_trader(fake, amount=500.0)

    with patch.object(tr, "_log_action"):
        out = tr.buy_limit_at_ask("F", ask=10.0)   # no dollar_amount → uses _trade_amount

    assert out["ok"] and out["qty"] == 50          # 500 // 10.0


# ── alpaca_trader.buy_market_shares (whole-share market, no fractional) ────────

def test_market_shares_whole_qty():
    fake = MagicMock()
    fake.submit_order.return_value = SimpleNamespace(id="m-1", status="accepted")
    _arm_trader(fake, amount=1000.0)

    with patch.object(tr, "_log_action"):
        out = tr.buy_market_shares("AAPL", price=33.0, dollar_amount=1000.0)

    assert out["ok"] and out["qty"] == 30          # int(1000 // 33) == 30
    assert isinstance(out["qty"], int)
    req = fake.submit_order.call_args[0][0]
    assert float(req.qty) == 30.0 and float(req.qty).is_integer()
    assert not hasattr(req, "notional") or req.notional is None   # never notional


def test_market_shares_under_budget_skips():
    fake = MagicMock()
    _arm_trader(fake, amount=100.0)

    with patch.object(tr, "_log_action"):
        out = tr.buy_market_shares("BRK.A", price=1500.0, dollar_amount=100.0)

    assert out["ok"] is False and out["status"] == "under_budget"
    fake.submit_order.assert_not_called()


# ── TradingView chart-title → symbol parsing ──────────────────────────────────

def test_tv_symbol_leading_token():
    assert ma._parse_tv_symbol("ENHA 3.16 ▲ +1.20% — TradingView") == "ENHA"


def test_tv_symbol_exchange_prefix():
    assert ma._parse_tv_symbol("AAPL stock price — NASDAQ:AAPL — TradingView") == "AAPL"


def test_tv_symbol_dotted_ticker():
    assert ma._parse_tv_symbol("BRK.B 400.00 — TradingView") == "BRK.B"


def test_tv_symbol_url_fallback_when_title_unhelpful():
    got = ma._parse_tv_symbol(
        "TradingView",
        "https://www.tradingview.com/chart/x04Gfcu8/?symbol=NASDAQ%3ATSLA")
    assert got == "TSLA"


def test_tv_symbol_none_when_empty():
    assert ma._parse_tv_symbol("") is None
    assert ma._parse_tv_symbol("   ", "") is None


def test_desk_tv_focus_symbol_dispatches_to_agent():
    with patch.object(desk, "_agent", create=True) as agent:
        agent.read_tv_symbol.return_value = "gme"
        assert desk.tv_focus_symbol() == "GME"


# ── desk_actions routing ──────────────────────────────────────────────────────

def test_desk_buy_routes_to_limit_ask():
    desk._buy_style = "limit_ask"
    desk._trade_amount = 1000.0
    desk._limit_pad_pct = 0.1
    desk._extended = True

    with patch("alpaca_trader.is_active", return_value=True), \
         patch("alpaca_trader.buy_limit_at_ask",
               return_value={"ok": True, "order_id": "z", "qty": 19, "limit_px": 50.05}) as blim, \
         patch("alpaca_trader.buy") as bnot, \
         patch.object(desk, "_latest_ask", return_value=50.0):
        msg = desk.desk_buy("aapl")

    blim.assert_called_once_with("AAPL", 50.0, 1000.0, 0.1)
    bnot.assert_not_called()
    assert "AAPL" in msg and "19sh" in msg


def test_desk_buy_routes_to_market_whole_shares():
    desk._buy_style = "market"
    desk._trade_amount = 1000.0

    with patch("alpaca_trader.is_active", return_value=True), \
         patch("alpaca_trader.buy_market_shares",
               return_value={"ok": True, "order_id": "m", "qty": 20}) as bmkt, \
         patch("alpaca_trader.buy_limit_at_ask") as blim, \
         patch("alpaca_trader.buy") as bnotional, \
         patch.object(desk, "_latest_ask", return_value=50.0):
        msg = desk.desk_buy("msft")

    bmkt.assert_called_once_with("MSFT", 50.0, 1000.0)
    blim.assert_not_called()
    bnotional.assert_not_called()      # fractional notional path never used
    assert "MSFT" in msg and "20sh" in msg


def test_desk_buy_unknown_style_falls_back_to_whole_limit():
    # A stale "notional_market" config must NOT reach the fractional path.
    desk._buy_style = "notional_market"
    desk._trade_amount = 1000.0
    desk._limit_pad_pct = 0.1
    desk._extended = False

    with patch("alpaca_trader.is_active", return_value=True), \
         patch("alpaca_trader.buy_limit_at_ask",
               return_value={"ok": True, "order_id": "z", "qty": 19, "limit_px": 50.05}) as blim, \
         patch("alpaca_trader.buy") as bnotional, \
         patch.object(desk, "_latest_ask", return_value=50.0):
        msg = desk.desk_buy("aapl")

    # desk_buy only branches to `market`; everything else is the whole-share limit path
    blim.assert_called_once()
    bnotional.assert_not_called()
    assert "19sh" in msg


def test_desk_buy_auto_uses_market_when_open():
    desk._buy_style = "auto"
    desk._trade_amount = 1000.0

    with patch("alpaca_trader.is_active", return_value=True), \
         patch("alpaca_trader.market_is_open", return_value=True), \
         patch("alpaca_trader.buy_market_shares",
               return_value={"ok": True, "order_id": "m", "qty": 20}) as bmkt, \
         patch("alpaca_trader.buy_limit_at_ask") as blim, \
         patch.object(desk, "_latest_ask", return_value=50.0):
        msg = desk.desk_buy("aapl")

    bmkt.assert_called_once()
    blim.assert_not_called()
    assert "20sh mkt" in msg


def test_desk_buy_auto_uses_limit_when_closed():
    desk._buy_style = "auto"
    desk._trade_amount = 1000.0
    desk._limit_pad_pct = 0.1
    desk._extended = True

    with patch("alpaca_trader.is_active", return_value=True), \
         patch("alpaca_trader.market_is_open", return_value=False), \
         patch("alpaca_trader.buy_limit_at_ask",
               return_value={"ok": True, "order_id": "z", "qty": 19, "limit_px": 50.05}) as blim, \
         patch("alpaca_trader.buy_market_shares") as bmkt, \
         patch.object(desk, "_latest_ask", return_value=50.0):
        msg = desk.desk_buy("aapl")

    blim.assert_called_once()
    bmkt.assert_not_called()
    assert "19sh" in msg


def test_desk_sell_routes_to_close_out_off_the_bid():
    desk._trader_mode = "paper"
    with patch("alpaca_trader.is_active", return_value=True), \
         patch("alpaca_trader.close_out",
               return_value={"ok": True, "order_id": "s", "canceled": 1}) as close, \
         patch.object(desk, "_latest_bid", return_value=42.0) as bid:
        msg = desk.desk_sell("tsla")

    bid.assert_called_once()
    assert close.call_args.kwargs.get("price") == 42.0
    assert "TSLA" in msg and "canceled 1" in msg


def test_desk_sell_canceled_no_position_message():
    desk._trader_mode = "paper"
    with patch("alpaca_trader.is_active", return_value=True), \
         patch("alpaca_trader.close_out",
               return_value={"ok": True, "order_id": None, "canceled": 1,
                             "note": "no position (canceled 1 open order)"}), \
         patch.object(desk, "_latest_bid", return_value=2.0):
        msg = desk.desk_sell("baos")
    assert "no position" in msg


# ── alpaca_trader.close_out (cancel resting orders, then flatten) ──────────────

def test_close_out_cancels_then_closes_when_open():
    fake = MagicMock()
    fake.get_orders.return_value = [SimpleNamespace(id="o1"), SimpleNamespace(id="o2")]
    fake.get_open_position.return_value = SimpleNamespace(qty="92", current_price="10.0")
    fake.get_clock.return_value = SimpleNamespace(is_open=True)
    fake.close_position.return_value = SimpleNamespace(id="c-1", status="accepted")
    tr._mode = "paper"
    tr._client = fake
    tr._limit_offset = 0.1

    with patch.object(tr, "_log_action"):
        out = tr.close_out("BAOS", price=10.0)

    assert out["ok"] is True and out["canceled"] == 2
    assert fake.cancel_order_by_id.call_count == 2
    fake.close_position.assert_called_once_with("BAOS")


def test_get_positions_detail_maps_pnl():
    fake = MagicMock()
    fake.get_all_positions.return_value = [
        SimpleNamespace(symbol="tsla", qty="120", avg_entry_price="10.82",
                        current_price="11.40", unrealized_pl="69.60",
                        unrealized_plpc="0.0536", market_value="1368.0"),
    ]
    tr._mode = "paper"
    tr._client = fake

    d = tr.get_positions_detail()
    assert set(d) == {"TSLA"}
    p = d["TSLA"]
    assert p["qty"] == 120.0
    assert p["avg_entry"] == 10.82
    assert p["current"] == 11.40
    assert p["pl"] == 69.60
    assert round(p["plpc"], 2) == 5.36        # fraction -> percent
    assert p["mkt_val"] == 1368.0


def test_init_retries_transient_unauthorized():
    acct = SimpleNamespace(cash="1000", buying_power="1000")
    client = MagicMock()
    client.get_account.side_effect = [Exception("unauthorized"),
                                      Exception("unauthorized"), acct]
    with patch("alpaca.trading.client.TradingClient", return_value=client), \
         patch("alpaca_trader.time.sleep"):
        tr.init(mode="paper", api_key="k", secret_key="s")
    assert tr.is_active() and tr._mode == "paper"
    assert client.get_account.call_count == 3


def test_init_gives_up_after_retries():
    client = MagicMock()
    client.get_account.side_effect = Exception("unauthorized")
    with patch("alpaca.trading.client.TradingClient", return_value=client), \
         patch("alpaca_trader.time.sleep"):
        tr.init(mode="paper", api_key="k", secret_key="s")
    assert not tr.is_active() and tr._mode == "off"


def test_close_out_no_position_reports_canceled():
    fake = MagicMock()
    fake.get_orders.return_value = [SimpleNamespace(id="o1")]
    fake.get_open_position.side_effect = Exception("no position")
    tr._mode = "paper"
    tr._client = fake

    with patch.object(tr, "_log_action"):
        out = tr.close_out("BAOS", price=2.0)

    assert out["status"] == "canceled" and out["canceled"] == 1
    fake.close_position.assert_not_called()
