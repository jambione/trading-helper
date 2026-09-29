"""Day's position history for the strip under the book.

Fills are the Alpaca account. Reasons are the desk's. The list is one ET day.
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import day_history as dh  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = "2026-09-29"


def _ts(hour, minute, second=0, day=29):
    return datetime(2026, 9, day, hour, minute, second, tzinfo=ET).timestamp()


def _buy(sym, qty, price, hour, minute, second=0, **extra):
    row = {
        "id": extra.pop("id", f"{sym}-b-{hour}{minute}{second}"),
        "symbol": sym,
        "side": "buy",
        "filled_qty": qty,
        "filled_avg_price": price,
        "filled_at": datetime.fromtimestamp(
            _ts(hour, minute, second), tz=ET).isoformat(),
        "type": extra.pop("type", "market"),
        "status": "filled",
    }
    row.update(extra)
    return row


def _sell(sym, qty, price, hour, minute, second=0, **extra):
    extra.setdefault("id", f"{sym}-s-{hour}{minute}{second}")
    row = _buy(sym, qty, price, hour, minute, second, **extra)
    row["side"] = "sell"
    return row


def _outcome(**kw):
    base = {
        "symbol": "ASTS",
        "entry_price": 60.78,
        "entry_time": _ts(10, 15, 50),
        "exit_price": 60.57,
        "exit_time": _ts(10, 16, 55),
        "close_reason": "local_trail",
        "arm_why": "oversold_leave",
        "total_qty": 7,
        "realized_pl_usd": -1.47,
    }
    base.update(kw)
    return base


def test_round_trip_carries_price_time_reason_and_pl():
    hist = dh.build_day_history(
        [_buy("ASTS", 7, 60.78, 10, 16, 12),
         _sell("ASTS", 7, 60.57, 10, 16, 52, type="market")],
        outcomes=[_outcome()],
        now=_ts(11, 0),
        day=DAY,
    )
    assert hist["day"] == DAY
    assert len(hist["rows"]) == 1
    row = hist["rows"][0]
    assert row["symbol"] == "ASTS"
    assert row["status"] == "closed"
    assert row["qty"] == 7
    assert row["entry_price"] == pytest.approx(60.78)
    assert row["entry_time"] == pytest.approx(_ts(10, 16, 12))
    assert row["entry_reason"] == "leave oversold"
    assert len(row["exits"]) == 1
    assert row["exits"][0]["price"] == pytest.approx(60.57)
    assert row["exits"][0]["time"] == pytest.approx(_ts(10, 16, 52))
    assert row["exits"][0]["reason"] == "local trail"
    assert row["pl"] == pytest.approx(-1.47)


def test_scale_out_keeps_each_exit_and_does_not_use_outcome_pl():
    """Outcome realized_pl prices every share at the final exit."""
    fills = [
        _buy("NVDA", 10, 10.0, 10, 0, id="b1"),
        _sell("NVDA", 4, 10.20, 10, 5, id="s1", type="limit"),
        _sell("NVDA", 6, 10.05, 10, 9, id="s2", type="market"),
    ]
    outcome = {
        "symbol": "NVDA",
        "entry_price": 10.0,
        "entry_time": _ts(10, 0, 1),
        "exit_price": 10.05,
        "exit_time": _ts(10, 9, 2),
        "close_reason": "local_trail",
        "arm_why": "presquare",
        "total_qty": 10,
        "realized_pl_usd": 0.50,  # (10.05-10)*10, missing the scale-out
    }
    row = dh.build_day_history(
        fills, outcomes=[outcome], now=_ts(11, 0), day=DAY,
    )["rows"][0]
    assert row["entry_reason"] == "empty square"
    assert len(row["exits"]) == 2
    assert row["exits"][0]["reason"] == "scale out"
    assert row["exits"][0]["qty"] == 4
    assert row["exits"][1]["reason"] == "local trail"
    assert row["exits"][1]["qty"] == 6
    assert row["pl"] == pytest.approx(1.10)
    assert row["status"] == "closed"


def test_two_round_trips_stay_separate_newest_first():
    fills = [
        _buy("A", 1, 5, 10, 0, id="a1"),
        _sell("A", 1, 5.1, 10, 2, id="a2", type="market"),
        _buy("A", 1, 6, 11, 0, id="a3"),
        _sell("A", 1, 5.9, 11, 4, id="a4", type="market"),
    ]
    rows = dh.build_day_history(fills, now=_ts(12, 0), day=DAY)["rows"]
    assert len(rows) == 2
    assert rows[0]["entry_price"] == pytest.approx(6)
    assert rows[1]["entry_price"] == pytest.approx(5)
    assert rows[0]["pl"] == pytest.approx(-0.10)
    assert rows[1]["pl"] == pytest.approx(0.10)


def test_adds_average_into_one_entry():
    fills = [
        _buy("B", 4, 10, 10, 0, id="b1"),
        _buy("B", 6, 12, 10, 1, id="b2"),
        _sell("B", 10, 11, 10, 5, id="b3", type="market"),
    ]
    row = dh.build_day_history(fills, now=_ts(11, 0), day=DAY)["rows"][0]
    assert row["qty"] == 10
    assert row["entry_price"] == pytest.approx(11.2)
    assert row["entry_time"] == pytest.approx(_ts(10, 0))
    assert len(row["exits"]) == 1


def test_yesterday_is_gone():
    y_buy = _ts(15, 0, day=28)
    y_sell = _ts(15, 10, day=28)
    fills = [
        {"id": "y1", "symbol": "OLD", "side": "buy", "filled_qty": 3,
         "filled_avg_price": 8, "filled_at": y_buy, "type": "market"},
        {"id": "y2", "symbol": "OLD", "side": "sell", "filled_qty": 3,
         "filled_avg_price": 8.2, "filled_at": y_sell, "type": "market"},
        _buy("NEW", 1, 4, 10, 0, id="n1"),
    ]
    rows = dh.build_day_history(fills, now=_ts(11, 0), day=DAY)["rows"]
    assert [r["symbol"] for r in rows] == ["NEW"]


def test_open_position_uses_account_pl_and_managed_reason():
    rows = dh.build_day_history(
        [_buy("CVNA", 5, 40.0, 10, 30, id="c1")],
        open_positions={"CVNA": {"qty": 5, "avg_entry": 40.0, "pl": -2.5}},
        managed={"CVNA": {"arm_why": "overbought", "entry_time": _ts(10, 30, 1)}},
        now=_ts(11, 0),
        day=DAY,
    )["rows"]
    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["exits"] == []
    assert rows[0]["entry_reason"] == "full square"
    assert rows[0]["pl"] == pytest.approx(-2.5)


def test_partial_exit_adds_unrealized_on_what_is_left():
    fills = [
        _buy("Q", 10, 10, 10, 0, id="q1"),
        _sell("Q", 4, 10.2, 10, 5, id="q2", type="limit"),
    ]
    row = dh.build_day_history(
        fills,
        open_positions={"Q": {"qty": 6, "avg_entry": 10, "pl": 0.30}},
        now=_ts(11, 0),
        day=DAY,
    )["rows"][0]
    assert row["status"] == "open"
    assert row["exits"][0]["reason"] == "scale out"
    assert row["pl"] == pytest.approx(1.10)


def test_outcome_exit_shows_before_the_sell_fill_and_does_not_double():
    buy = [_buy("ASTS", 7, 60.78, 10, 16, 12)]
    early = dh.build_day_history(
        buy, outcomes=[_outcome()], now=_ts(10, 17), day=DAY,
    )["rows"][0]
    assert early["status"] == "closed"
    assert len(early["exits"]) == 1
    assert early["exits"][0]["reason"] == "local trail"
    assert early["pl"] == pytest.approx((60.57 - 60.78) * 7)

    both = dh.build_day_history(
        buy + [_sell("ASTS", 7, 60.57, 10, 16, 52, type="market")],
        outcomes=[_outcome()],
        now=_ts(10, 17),
        day=DAY,
    )["rows"]
    assert len(both) == 1
    assert len(both[0]["exits"]) == 1
    assert both[0]["exits"][0]["time"] == pytest.approx(_ts(10, 16, 52))


def test_overnight_flatten_keeps_yesterdays_entry_because_the_exit_is_today():
    sell_at = _ts(9, 31, 2)
    outcome = _outcome(
        symbol="AIG",
        entry_price=70.0,
        entry_time=_ts(15, 40, day=28),
        exit_price=69.5,
        exit_time=sell_at,
        close_reason="eod_liquidate",
        arm_why="mid_rise",
        total_qty=2,
        realized_pl_usd=-1.0,
    )
    # Entry was yesterday, so the only fill today is the sell. The outcome
    # still has to supply the entry, and the sell must not become a second row.
    fills = [{
        "id": "aig-sell",
        "symbol": "AIG",
        "side": "sell",
        "filled_qty": 2,
        "filled_avg_price": 69.5,
        "filled_at": sell_at,
        "type": "market",
    }]
    rows = dh.build_day_history(
        fills, outcomes=[outcome], now=_ts(9, 40), day=DAY,
    )["rows"]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "AIG"
    assert rows[0]["entry_price"] == pytest.approx(70.0)
    assert rows[0]["entry_reason"] == "mid rise"
    assert rows[0]["exits"][0]["reason"] == "end of day"
    assert rows[0]["pl"] == pytest.approx(-1.0)


def test_partial_then_complete_fill_is_one_order():
    partial = _buy("Z", 2, 3, 10, 0, id="same")
    partial["filled_qty"] = 2
    full = _buy("Z", 5, 3, 10, 0, id="same")
    rows = dh.build_day_history(
        [partial, full], now=_ts(11, 0), day=DAY,
    )["rows"]
    assert len(rows) == 1
    assert rows[0]["qty"] == 5


def test_still_open_from_account_when_the_buy_was_not_in_todays_fills():
    rows = dh.build_day_history(
        [],
        open_positions={"HELD": {"qty": 3, "avg_entry": 12.5, "pl": 1.25}},
        managed={"HELD": {"entry_time": _ts(14, 0, day=28), "arm_why": "oversold_leave"}},
        now=_ts(10, 0),
        day=DAY,
    )["rows"]
    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["entry_price"] == pytest.approx(12.5)
    assert rows[0]["entry_reason"] == "leave oversold"
    assert rows[0]["pl"] == pytest.approx(1.25)


def test_positions_payload_publishes_day_history(monkeypatch):
    import ai_trader as at
    import alpaca_trader

    dh.clear_caches()
    at._last_good_positions.clear()
    at._last_good_account.clear()
    monkeypatch.setattr(alpaca_trader, "is_active", lambda: True)
    monkeypatch.setattr(alpaca_trader, "get_positions_detail", lambda: {})
    monkeypatch.setattr(alpaca_trader, "get_open_orders", lambda: [])
    monkeypatch.setattr(alpaca_trader, "get_filled_orders", lambda **kw: [])
    monkeypatch.setattr(
        alpaca_trader, "get_account_day_pl",
        lambda: {"equity": 1.0, "last_equity": 1.0, "day_pl": 0.0, "day_pl_pct": 0.0},
    )
    monkeypatch.setattr(dh, "load_outcomes", lambda: [])
    monkeypatch.setattr(dh, "load_today_fills", lambda now: [])
    payload = at._positions_payload("paper", _ts(11, 0), book_owner="grok")
    assert payload["day_history"]["day"] == DAY
    assert payload["day_history"]["rows"] == []


def test_broker_fills_are_paced(monkeypatch):
    calls = {"n": 0}

    def _filled(**kw):
        calls["n"] += 1
        return [_buy("ASTS", 1, 10, 10, 0, id="p1")]

    import alpaca_trader
    dh.clear_caches()
    monkeypatch.setattr(alpaca_trader, "is_active", lambda: True)
    monkeypatch.setattr(alpaca_trader, "get_filled_orders", _filled)
    import fill_ledger
    monkeypatch.setattr(fill_ledger, "read_day", lambda day: [])
    now = _ts(11, 0)
    first = dh.load_today_fills(now)
    second = dh.load_today_fills(now + 5)
    assert calls["n"] == 1
    assert len(first) == 1 and len(second) == 1
    third = dh.load_today_fills(now + 31)
    assert calls["n"] == 2
    assert third[0]["symbol"] == "ASTS"


# ── the strip sits where the legend did ─────────────────────────────────────


def test_history_strip_is_on_the_book_and_the_legend_stays_gone():
    html = (_ROOT / "dashboard.html").read_text(encoding="utf-8")
    js = (_ROOT / "static" / "js" / "feeds.js").read_text(encoding="utf-8")
    css = (_ROOT / "static" / "css" / "styles.css").read_text(encoding="utf-8")
    assert "data-ai-book-legend" not in html
    assert "function _paintBookLegend" not in js
    assert ".ai-book-legend" not in css
    assert 'data-ai-book-day-hist' in html
    # Under the rows, which is where the legend div used to be.
    assert html.index("data-ai-book-rows") < html.index("data-ai-book-day-hist")
    assert "function _paintDayHistory" in js
    assert "day_history" in js
    assert "No positions today" in js
    assert ".ai-book-day-hist" in css
