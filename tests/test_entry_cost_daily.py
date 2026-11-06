"""entry_cost_daily: cost sign convention and quote guards."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import entry_cost_daily as ec  # noqa: E402


def test_buy_at_ask_costs_half_spread_and_sell_at_bid_too():
    b = ec.cost_bp("buy", 100.10, 100.00, 100.10)
    s = ec.cost_bp("sell", 100.00, 100.00, 100.10)
    assert abs(b["cost_bp"] - 5.0) < 0.1 and abs(s["cost_bp"] - 5.0) < 0.1 and abs(b["half_spread_bp"] - 5.0) < 0.1


def test_price_improvement_is_negative_cost():
    assert ec.cost_bp("buy", 100.00, 100.00, 100.10)["cost_bp"] < 0


def test_unusable_quotes_are_none():
    assert ec.cost_bp("buy", 100.0, 0.0, 100.1) is None
    assert ec.cost_bp("buy", 100.0, 100.2, 100.1) is None
    assert ec.cost_bp("buy", 0.0, 100.0, 100.1) is None


def test_summary_medians_and_never_green():
    rec = {"day": "2026-11-05", "rows": [
        {"side": "buy", "cost_bp": 4.0, "half_spread_bp": 3.0}, {"side": "buy", "cost_bp": 6.0, "half_spread_bp": 5.0},
        {"side": "sell", "cost_bp": 2.0, "half_spread_bp": 3.0}, {"side": "sell", "err": "no_usable_quote"}]}
    s = ec.summarize(rec, {"2026-11-05": (0.25, 20)})
    assert (s["buys"], s["sells"], s["missing"], s["buy_cost"], s["sell_cost"], s["ng_share"]) == (2, 1, 1, 5.0, 2.0, 0.25)
    assert ec.ngtxt(s) == "25% of 20"


def test_fetch_window_is_blocked_in_market_hours(monkeypatch):
    class D(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 6, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(ec, "datetime", D)
    assert ec.blocked_now()
