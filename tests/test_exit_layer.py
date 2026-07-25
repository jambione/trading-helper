"""Position-aware exit layer: real-fill anchoring of PaperTrader exits
(flow_core), exit-mode master verdicts (tv-monitor), and the split
market-data / broker provider selection (trade_bridge).
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tv-monitor"))

import flow_core.core as l2_core  # noqa: E402
import tv_core  # noqa: E402
from trade_bridge.config import DEFAULTS  # noqa: E402
from trade_bridge.providers import build_providers  # noqa: E402
from trade_bridge.providers.mock import (MockBroker,  # noqa: E402
                                          MockMarketData)


def book(bid: float, ask: float | None = None, ts: float = 1000.0):
    ask = ask if ask is not None else bid + 0.01
    return l2_core.L2Book([(bid, 100.0)], [(ask, 100.0)], ts=ts)


def trader(**over):
    cfg = {"target_pct": 2.0, "stop_pct": 1.0, "trail_pct": 0.8}
    cfg.update(over)
    return l2_core.PaperTrader(cfg)


# ------------------------------------------------- PaperTrader.sync_real ----

def test_sync_real_anchors_to_broker_fill():
    tr = trader()
    note = tr.sync_real({"qty": 70, "avg_price": 171.58}, book(145.30))
    assert tr.in_position and tr.real
    assert tr.entry == 171.58
    assert "171.58" in note
    # same broker state again -> no churn
    assert tr.sync_real({"qty": 70, "avg_price": 171.58}, book(145.35)) is None


def test_sync_real_replaces_virtual_entry():
    tr = trader()
    tr.update(book(10.00), l2_core.Signal("BUY", "test", 10.00, 2.0))
    assert tr.in_position and not tr.real and tr.entry == 10.01  # paper @ ask
    tr.sync_real({"qty": 5, "avg_price": 10.05}, book(10.02))
    assert tr.real and tr.entry == 10.05


def test_take_profit_fires_off_real_entry():
    tr = trader()
    tr.sync_real({"qty": 10, "avg_price": 100.0}, book(100.0))
    sig, trade = tr.update(book(102.5), None)   # +2.5% > 2% target
    assert sig is not None and sig.action == "TAKE_PROFIT"
    assert trade.entry == 100.0
    assert not tr.in_position


def test_exit_muted_until_broker_state_changes():
    tr = trader()
    key = {"qty": 10, "avg_price": 100.0}
    tr.sync_real(key, book(100.0))
    sig, _ = tr.update(book(97.0), None)        # STOP
    assert sig.action == "STOP"
    # broker still shows the same open position -> no re-anchor, no re-alert
    assert tr.sync_real(key, book(97.0)) is None
    assert not tr.in_position
    # averaging down changes (qty, avg) -> re-arms the anchor
    note = tr.sync_real({"qty": 20, "avg_price": 98.5}, book(97.0))
    assert note is not None and tr.in_position and tr.entry == 98.5


def test_broker_flat_clears_real_anchor():
    tr = trader()
    tr.sync_real({"qty": 10, "avg_price": 100.0}, book(100.0))
    note = tr.sync_real(None, book(100.5))
    assert "flat" in note and not tr.in_position and not tr.real


def test_paper_entries_untouched_by_flat_broker():
    tr = trader()
    tr.update(book(10.00), l2_core.Signal("BUY", "test", 10.00, 2.0))
    assert tr.sync_real(None, book(10.02)) is None   # paper long survives
    assert tr.in_position and not tr.real


# ------------------------------------------- master_verdict in exit mode ----

def _l2(bias, play="STAND_ASIDE", pos=None):
    return {"bias": bias, "play": play, "pos": pos}


def test_exit_now_when_chart_and_tape_turn():
    pos = {"real": True, "entry": 100.0, "upnl": 1.2, "peak": 2.0}
    res = tv_core.master_verdict("SELL", _l2(-40, "BEAR_CRACK", pos))
    assert res["verdict"] == "EXIT NOW"


def test_take_profit_on_first_contrary_evidence_while_green():
    pos = {"real": True, "entry": 100.0, "upnl": 1.2, "peak": 2.0}
    res = tv_core.master_verdict("WAIT", _l2(-40, pos=pos))
    assert res["verdict"].startswith("TAKE PROFIT")
    res = tv_core.master_verdict("SELL", _l2(0, pos=pos))
    assert res["verdict"].startswith("TAKE PROFIT")


def test_tighten_when_red_position_under_pressure():
    pos = {"real": True, "entry": 100.0, "upnl": -0.8, "peak": 0.1}
    res = tv_core.master_verdict("WAIT", _l2(-40, pos=pos))
    assert res["verdict"].startswith("TIGHTEN")


def test_hold_while_trend_intact():
    pos = {"real": True, "entry": 100.0, "upnl": 3.0, "peak": 3.2}
    res = tv_core.master_verdict("STRONG BUY", _l2(50, "LONG_TRIGGER", pos))
    assert res["verdict"].startswith("HOLD")


def test_entry_mode_unchanged_without_position():
    res = tv_core.master_verdict("STRONG BUY", _l2(50, "LONG_TRIGGER"))
    assert res["verdict"] == "EXECUTE BUY"


# ------------------------------------------------- provider pair splitting --

def test_default_provider_pair_is_mock():
    md, br = build_providers(dict(DEFAULTS))
    assert isinstance(md, MockMarketData) and isinstance(br, MockBroker)


def test_broker_override_keeps_mock_market_data(monkeypatch):
    calls = {}

    class StubBroker:
        def __init__(self, cfg):
            calls["broker_cfg"] = cfg

    stub = types.ModuleType("trade_bridge.providers.alpaca")
    stub.AlpacaBroker = StubBroker
    stub.AlpacaMarketData = object
    monkeypatch.setitem(sys.modules, "trade_bridge.providers.alpaca", stub)

    cfg = dict(DEFAULTS)
    cfg["broker_provider"] = "alpaca"          # provider stays "mock"
    md, br = build_providers(cfg)
    assert isinstance(md, MockMarketData)
    assert isinstance(br, StubBroker)
    assert calls["broker_cfg"] is cfg
