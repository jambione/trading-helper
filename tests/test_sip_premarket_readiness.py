"""Tests for SIP pre-market readiness:
1. phase_b stream_last_print multi-tier fallback (Finnhub -> live_print -> alpaca_price_poll -> Alpaca REST)
2. alpaca_api sip_is_realtime auto-link to alpaca_bar_feed="sip"
3. alpaca_api probe_feed_entitlement (200 OK vs 403 Forbidden)
4. trade_bridge feed_kw respect for SIP
5. Spread delay auto-zeroing in ai_entry_watch and tight_screener
"""
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import alpaca_api as A
from alpaca.data.enums import DataFeed
import phase_b as pb
import trade_bridge.providers.alpaca as bridge_alpaca
import tight_screener as ts
import ai_entry_watch as ew

ET = ZoneInfo("America/New_York")


def test_sip_is_realtime_auto_links_to_bar_feed():
    assert A.sip_is_realtime({}) is False
    assert A.sip_is_realtime({"alpaca_bar_feed": "sip"}) is True
    assert A.sip_is_realtime({"alpaca_data_feed": "sip"}) is True
    assert A.sip_is_realtime({"ai_sip_realtime": True}) is True
    assert A.sip_is_realtime({"alpaca_bar_feed": "iex", "ai_sip_realtime": False}) is False


def test_probe_feed_entitlement_success(monkeypatch):
    mock_client = MagicMock()
    monkeypatch.setattr(A, "connect_data_client", lambda cfg: mock_client)
    ok, msg = A.probe_feed_entitlement({"alpaca_bar_feed": "sip"})
    assert ok is True
    assert msg == "sip_ok"
    assert mock_client.get_stock_latest_quote.called


def test_probe_feed_entitlement_forbidden_403(monkeypatch):
    mock_client = MagicMock()
    mock_client.get_stock_latest_quote.side_effect = Exception("403 Forbidden: not entitled to SIP feed")
    monkeypatch.setattr(A, "connect_data_client", lambda cfg: mock_client)
    ok, msg = A.probe_feed_entitlement({"alpaca_bar_feed": "sip"})
    assert ok is False
    assert msg == "sip_forbidden_403"


def test_trade_bridge_feed_kw_respects_sip():
    assert bridge_alpaca.feed_kw({}) == {"feed": DataFeed.IEX}
    assert bridge_alpaca.feed_kw({"alpaca_bar_feed": "sip"}) == {"feed": DataFeed.SIP}
    assert bridge_alpaca.feed_kw({"alpaca_data_feed": "sip"}) == {"feed": DataFeed.SIP}


def test_tight_screener_auto_zeros_delay_on_sip():
    # 09:32 ET: with default 15m delay, 570+15+1 = 586 (09:46 ET) is required, so 09:32 returns False
    t_0932 = datetime(2026, 10, 20, 9, 32, tzinfo=ET)
    assert ts.in_scan_window(t_0932, {"alpaca_bar_feed": "iex"}) is False
    # With SIP, delay drops to 0, so 570+0+1 = 571 (09:31 ET) is required, so 09:32 returns True!
    assert ts.in_scan_window(t_0932, {"alpaca_bar_feed": "sip"}) is True


def test_admit_arm_gates_spread_delay_auto_zeros_on_sip(monkeypatch):
    # 09:32 ET (mins_open = 2)
    t_0932 = datetime(2026, 10, 20, 9, 32, tzinfo=ET).timestamp()
    row = {"symbol": "TEST", "source": "movers", "price": 10.0}
    cfg_iex = {
        "ai_watch_admit_arm_gates": True,
        "ai_watch_max_sip_spread_pct": 0.10,
        "alpaca_bar_feed": "iex",
    }
    cfg_sip = {
        "ai_watch_admit_arm_gates": True,
        "ai_watch_max_sip_spread_pct": 0.10,
        "alpaca_bar_feed": "sip",
    }
    # On IEX: mins_open (2) < delay+1 (16), so spread check sits out -> passes
    passed_iex, why_iex = ew.admit_arm_gates(row, cfg_iex, now=t_0932, spread_fn=lambda sym, now: 0.50)
    assert passed_iex is True

    # On SIP: delay is 0, so mins_open (2) >= delay+1 (1), spread check runs and catches the 0.50% wide spread!
    passed_sip, why_sip = ew.admit_arm_gates(row, cfg_sip, now=t_0932, spread_fn=lambda sym, now: 0.50)
    assert passed_sip is False
    assert why_sip == "spread_wide"


def test_phase_b_stream_last_print_falls_back_to_live_print(monkeypatch):
    # Simulate empty Finnhub stream
    import finnhub_stream as fh
    monkeypatch.setattr(fh, "get_latest_print", lambda sym: None)
    with fh.FINNHUB_STATE.lock:
        fh.FINNHUB_STATE.prices.clear()

    # Simulate dashboard live_print has fresh price
    monkeypatch.setattr(ew, "live_print", lambda sym: (25.50, 2.5))

    got = pb.stream_last_print("AAPL", now=1000.0)
    assert got is not None
    assert got["price"] == 25.50
    assert got["age_sec"] == 2.5
    assert got["src"] == "live_print"


def test_phase_b_stream_last_print_falls_back_to_alpaca_rest(monkeypatch):
    # Simulate empty Finnhub stream and empty live_print
    import finnhub_stream as fh
    monkeypatch.setattr(fh, "get_latest_print", lambda sym: None)
    with fh.FINNHUB_STATE.lock:
        fh.FINNHUB_STATE.prices.clear()
    monkeypatch.setattr(ew, "live_print", lambda sym: None)

    # Simulate Alpaca REST return
    mock_client = MagicMock()
    monkeypatch.setattr(A, "connect_data_client", lambda cfg: mock_client)
    monkeypatch.setattr(A, "get_latest_trade_quotes", lambda client, syms, cfg=None: {"AAPL": (26.00, 995.0)})

    got = pb.stream_last_print("AAPL", now=1000.0)
    assert got is not None
    assert got["price"] == 26.00
    assert got["age_sec"] == 5.0
    assert got["src"] == "alpaca_trade_rest"


def test_phase_b_position_working_sell_active_outside_rth(monkeypatch):
    import ai_positions as cp
    # Outside RTH
    monkeypatch.setattr(cp, "_rth_now", lambda _n: False)
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=False: False)  # ai_premarket_working_sell = False

    # Plain position: False
    plain_pos = {"entry_confirmed": True, "ticker": "AAA"}
    assert cp._premarket_working_sell_on(pos=plain_pos) is False

    # Phase B position: True!
    pb_pos = {"entry_confirmed": True, "ticker": "AAA", "phase_b": True}
    assert cp._premarket_working_sell_on(pos=pb_pos) is True

    # During RTH: always False (never regress RTH!)
    monkeypatch.setattr(cp, "_rth_now", lambda _n: True)
    assert cp._premarket_working_sell_on(pos=pb_pos) is False


def test_phase_b_position_in_apply_local_trail(monkeypatch):
    import ai_positions as cp
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_local_trail_enabled": True,
        "ai_premarket_working_sell": False,  # Off globally!
        "ai_local_trail_give_r": 0.10,
        "ai_stranded_close_sec": 0,
        "ai_shelf_trace_sec": 0,
    })
    monkeypatch.setattr(cp, "_cfg_flag", lambda k, d=True: {
        "ai_local_trail_enabled": True,
        "ai_premarket_working_sell": False,
    }.get(k, d))
    monkeypatch.setattr(cp, "_rth_now", lambda _n: False)
    monkeypatch.setattr(cp, "_premarket_bid", lambda _s: 9.85)

    pos = {
        "entry_confirmed": True, "entry_price": 10.0, "risk_per_share": 0.5,
        "last_seen_price": 10.0, "local_stop_price": 9.95,
        "entry_time": 1.0, "mfe_r": 0.0, "phase_b": True,
    }
    placed = {}
    monkeypatch.setattr("alpaca_trader.working_sell_replace", lambda sym, px, **k: placed.update({"sym": sym, "px": px}) or {"ok": True, "order_id": "ws-pb"})
    monkeypatch.setattr("alpaca_trader.close_out", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("close_out called")))

    events = []
    _ch, closed = cp.apply_local_trail("AAA", pos, 9.90, events, {})
    assert closed is False
    assert pos.get("working_sell_id") == "ws-pb"
    assert pos.get("working_sell_state") == "flatten"
    assert placed["px"] == 9.85


def test_phase_b_flatten_window_submits_working_sell(monkeypatch):
    # 09:26 ET (inside flatten window 09:25 - 09:28 ET)
    t_0926 = datetime(2026, 10, 20, 9, 26, tzinfo=ET).timestamp()
    pos = {
        "symbol": "AAA",
        "phase_b": True,
        "entry_price": 10.0,
        "last_seen_price": 10.5,
    }
    placed = {}
    import ai_positions as cp
    monkeypatch.setattr(cp, "_rest_working_sell", lambda sym, p, px, state: placed.update({"sym": sym, "px": px, "state": state}) or True)
    monkeypatch.setattr(pb, "fresh_stream_last", lambda sym, now=None, cfg=None: (10.5, "test"))
    cfg = {"ai_phase_b_enabled": True, "ai_phase_b_working_sell": True, "ai_phase_b_dry_run": False}
    pb.tick(
        cfg=cfg,
        now=t_0926,
        open_positions={"AAA": pos},
    )
    assert placed.get("sym") == "AAA"
    assert placed.get("px") == 10.5
    assert placed.get("state") == "flatten"


def test_alpaca_trader_close_out_off_hours_clamps_fractional_and_rejects_under_1(monkeypatch):
    import alpaca_trader as tr
    from types import SimpleNamespace
    fake = MagicMock()
    fake.get_orders.return_value = []
    monkeypatch.setattr(tr, "_host_allowed", lambda: True)
    monkeypatch.setattr(tr, "market_is_open", lambda: False)
    monkeypatch.setattr(tr, "_client", fake)
    monkeypatch.setattr(tr, "_mode", "paper")

    # 1. Fractional shares clamped to int (e.g. 10.8 -> 10)
    fake.get_open_position.return_value = SimpleNamespace(qty="10.8", current_price="5.0")
    fake.submit_order.return_value = SimpleNamespace(id="co-1", status="accepted")
    res = tr.close_out("AAA", price=5.0)
    assert res["ok"] is True
    req = fake.submit_order.call_args[0][0]
    assert int(req.qty) == 10
    assert float(req.limit_price) > 0
    assert bool(req.extended_hours) is True

    # 2. Under 1 share rejected
    fake.get_open_position.return_value = SimpleNamespace(qty="0.75", current_price="5.0")
    res_under = tr.close_out("BBB", price=5.0)
    assert res_under["ok"] is False
    assert res_under["status"] == "qty_under_1"
