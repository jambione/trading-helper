"""live_mirror — arming, caps, request copying, and the paper->live order copy with fake clients."""
import json
from datetime import date
from types import SimpleNamespace as NS

import pytest
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    StopOrderRequest,
    TakeProfitRequest,
)

import live_mirror as lm


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "ARM_FILE", tmp_path / "live_mirror.armed")
    monkeypatch.setattr(lm, "OVERNIGHT_ARM_FILE", tmp_path / "overnight_live.armed")
    monkeypatch.setattr(lm, "OUT", tmp_path)
    monkeypatch.setattr(lm, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(lm, "LOG", tmp_path / "run.log")
    return tmp_path


def rows(files):
    p = files / "ledger.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_arming_needs_the_file_and_no_overnight_test(files):
    assert lm.armed()[0] is False
    (files / "live_mirror.armed").write_text("2026-10-12\n")
    assert lm.armed(date(2026, 10, 9)) == (False, "arms on 2026-10-12")
    assert lm.armed(date(2026, 10, 12))[0] is True
    (files / "overnight_live.armed").write_text("")
    assert lm.armed(date(2026, 10, 12))[0] is False         # the overnight test owns the account
    (files / "overnight_live.armed").unlink()
    (files / "live_mirror.armed").write_text("10/12")
    assert lm.armed(date(2026, 10, 12))[0] is False         # a typo stays disarmed


def test_buy_caps():
    ok = dict(cash_now=100.0, equity=100.0, holding=[], open_buys=[])
    assert lm.buy_allowed(20.0, **ok) == (True, "ok")
    assert lm.buy_allowed(lm.MAX_ORDER + 1, **ok)[0] is False
    assert lm.buy_allowed(None, **ok)[0] is False
    assert lm.buy_allowed(60.0, **dict(ok, cash_now=55.0))[1].startswith("cash")
    assert lm.buy_allowed(20.0, **dict(ok, holding=["BBB"]))[1].startswith("one_position")
    assert lm.buy_allowed(20.0, **dict(ok, open_buys=["BBB"]))[1].startswith("one_position")
    assert lm.buy_allowed(20.0, **dict(ok, equity=89.99))[1].startswith("equity_floor")


def test_defaults_fit_the_100_dollar_account():
    """Operator 10/8: $95 per buy (tight names up to ~$95), stop new buys under $90 equity."""
    assert lm.MAX_ORDER == 95 and lm.EQUITY_FLOOR == 90


def test_copy_request_keeps_type_prices_and_legs():
    req = MarketOrderRequest(symbol="AAA", notional=512.5, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                             order_class=OrderClass.BRACKET, take_profit=TakeProfitRequest(limit_price=21.0),
                             stop_loss=StopLossRequest(stop_price=19.0), client_order_id="desk-1")
    c = lm.copy_request(req, 1, "mir-desk-1")
    assert (c.qty, c.notional, c.client_order_id) == (1, None, "mir-desk-1")
    assert c.take_profit.limit_price == 21.0 and c.stop_loss.stop_price == 19.0
    assert lm.order_kind(c) == "market"
    assert lm.order_kind(StopOrderRequest(symbol="AAA", qty=3, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                                          stop_price=19.0)) == "stop"
    assert lm.order_kind(LimitOrderRequest(symbol="AAA", qty=3, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                           limit_price=20.0)) == "limit"


class FakePaper:
    def __init__(self):
        self.positions, self.submitted = [], []

    def submit_order(self, req):
        if getattr(req, "symbol", "") == "BAD":
            raise RuntimeError("paper rejected")
        self.submitted.append(req)
        return NS(id=f"p{len(self.submitted)}", client_order_id=req.client_order_id, status="accepted")

    def cancel_order_by_id(self, oid):
        return None

    def get_all_positions(self):
        return self.positions


class FakeLive:
    def __init__(self, cash=100.0, equity=100.0):
        self.cash, self.positions, self.open, self.submitted, self.cancelled, self.closed = cash, {}, [], [], [], []
        self.equity, self.history = equity, []

    def get_account(self):
        return NS(cash=str(self.cash), equity=str(self.equity), account_number="LIVE1", status="ACTIVE")

    def get_all_positions(self):
        return [NS(symbol=s, qty=str(q)) for s, q in self.positions.items()]

    def get_orders(self, req):
        if lm._v(getattr(req, "status", None)) == "all":
            return list(self.history)
        return [o for o in self.open if not req.symbols or o.symbol in req.symbols]

    def submit_order(self, req):
        self.submitted.append(req)
        o = NS(id=f"L{len(self.submitted)}", client_order_id=req.client_order_id, symbol=req.symbol,
               side=req.side, status="accepted")
        if lm.order_kind(req) in lm.PROTECTIVE or lm.order_kind(req) == "limit":
            self.open.append(o)                      # working until filled/cancelled
        elif lm._v(req.side) == "buy":
            self.positions[req.symbol] = 1           # a market buy fills at once
            self.history.append(NS(symbol=req.symbol, client_order_id=req.client_order_id, side=req.side, filled_qty="1"))
        elif lm._v(req.side) == "sell":
            self.positions.pop(req.symbol, None)
        return o

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)
        self.open = [o for o in self.open if o.id != oid]

    def close_position(self, sym):
        self.closed.append(sym)
        self.positions.pop(sym, None)
        return NS(id="Lclose", client_order_id=None)


def mirror(files, live):
    (files / "live_mirror.armed").write_text("")
    m = lm.Mirror(FakePaper(), desk_account="PAPER1", desk_key="pk")
    m.live = live
    m._price = lambda sym: 20.0
    return m


def drain(m):
    while not m.q.empty():
        m.handle(*m.q.get())


def test_proxy_mirrors_only_after_a_successful_paper_call(files):
    live = FakeLive()
    m = mirror(files, live)
    proxy = lm.MirroredClient(m.desk, m)
    proxy.submit_order(MarketOrderRequest(symbol="AAA", qty=37, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                          client_order_id="d1"))
    with pytest.raises(RuntimeError):
        proxy.submit_order(MarketOrderRequest(symbol="BAD", qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    drain(m)
    assert [(r.symbol, r.qty, r.client_order_id) for r in live.submitted] == [("AAA", 1, "mir-d1")]
    assert rows(files)[0]["event"] == "mirror_submit" and rows(files)[0]["qty_desk"] == 37
    assert m.id_map == {"p1": "L1"}


def test_second_name_waits_for_the_first_to_close(files):
    """One live position at a time; after the exit the next paper open is mirrored (proceeds reuse)."""
    live = FakeLive(cash=30.0)
    m = mirror(files, live)
    buy = lambda s, i: m.enqueue("submit", MarketOrderRequest(symbol=s, qty=5, side=OrderSide.BUY,  # noqa: E731
                                                              time_in_force=TimeInForce.DAY), NS(id=f"p{i}", client_order_id=None))
    buy("A0", 0)
    buy("A1", 1)
    drain(m)
    assert [r.symbol for r in live.submitted] == ["A0"]
    assert rows(files)[1]["event"] == "mirror_skip" and rows(files)[1]["reason"].startswith("one_position")
    m.enqueue("close", "A0")                         # the desk closes (e.g. no_progress / left_overbought)
    buy("A2", 2)
    drain(m)
    assert live.closed == ["A0"] and [r.symbol for r in live.submitted] == ["A0", "A2"]


def test_a_working_limit_buy_blocks_a_second_name(files):
    live = FakeLive()
    m = mirror(files, live)
    m.enqueue("submit", LimitOrderRequest(symbol="AAA", qty=5, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                          limit_price=20.0), NS(id="p1", client_order_id="e1"))
    m.enqueue("submit", MarketOrderRequest(symbol="BBB", qty=5, side=OrderSide.BUY, time_in_force=TimeInForce.DAY),
              NS(id="p2", client_order_id="e2"))
    drain(m)
    assert [r.symbol for r in live.submitted] == ["AAA"]


def test_desk_market_exit_is_copied(files):
    """The 10/8 exits (no_progress, left_overbought) flatten with market sells or close_position: both copied."""
    live = FakeLive()
    m = mirror(files, live)
    live.positions["AAA"] = 1
    m.bought.add("AAA")
    m.enqueue("submit", MarketOrderRequest(symbol="AAA", qty=12, side=OrderSide.SELL, time_in_force=TimeInForce.DAY),
              NS(id="p9", client_order_id="x9"))
    drain(m)
    assert live.submitted[-1].qty == 1 and "AAA" not in live.positions


def test_below_the_equity_floor_buys_stop_but_exits_continue(files):
    live = FakeLive(equity=89.0)
    m = mirror(files, live)
    m.enqueue("submit", MarketOrderRequest(symbol="AAA", qty=5, side=OrderSide.BUY, time_in_force=TimeInForce.DAY),
              NS(id="p1", client_order_id=None))
    drain(m)
    assert live.submitted == [] and rows(files)[0]["reason"].startswith("equity_floor")
    live.positions["BBB"] = 1
    m.bought.add("BBB")
    m.enqueue("close", "BBB")
    drain(m)
    assert live.closed == ["BBB"]


def test_stop_then_exit_cancels_the_live_stop_first(files):
    live = FakeLive()
    m = mirror(files, live)
    live.positions["AAA"] = 1
    stop = StopOrderRequest(symbol="AAA", qty=37, side=OrderSide.SELL, time_in_force=TimeInForce.DAY, stop_price=19.0)
    m.enqueue("submit", stop, NS(id="p1", client_order_id="s1"))
    m.enqueue("submit", stop, NS(id="p2", client_order_id="s2"))           # duplicate protective: skipped
    m.enqueue("submit", MarketOrderRequest(symbol="AAA", qty=37, side=OrderSide.SELL, time_in_force=TimeInForce.DAY),
              NS(id="p3", client_order_id="x1"))
    drain(m)
    assert [lm.order_kind(r) for r in live.submitted] == ["stop", "market"]
    assert live.submitted[1].qty == 1 and live.cancelled == ["L1"]
    assert [r["event"] for r in rows(files)] == ["mirror_submit", "mirror_skip", "mirror_submit"]


def test_sell_without_a_live_position_is_skipped(files):
    live = FakeLive()
    m = mirror(files, live)
    m.enqueue("submit", MarketOrderRequest(symbol="ZZZ", qty=5, side=OrderSide.SELL, time_in_force=TimeInForce.DAY),
              NS(id="p1", client_order_id=None))
    drain(m)
    assert live.submitted == [] and rows(files)[0]["reason"] == "no live position"


def test_sync_flattens_a_name_the_desk_no_longer_holds(files):
    live = FakeLive()
    m = mirror(files, live)
    m._roll_day(live)
    live.positions["AAA"] = 1
    m.bought.add("AAA")
    m.desk.positions = []
    m.sync()
    assert live.closed == ["AAA"] and rows(files)[-1]["reason"] in ("sync_exit", "eod_flatten")


def test_attach_is_a_no_op_off_paper():
    t = NS(_mode="live", _client=object())
    assert lm.attach(t) is None and not isinstance(t._client, lm.MirroredClient)


def test_desk_close_position_exit_is_paired_with_the_paper_close_order(files):
    """Skeptic 10/8 #1: the desk exits through close_position; the live exit row must carry the paper order id."""
    live = FakeLive()
    m = mirror(files, live)
    proxy = lm.MirroredClient(m.desk, m)
    m.desk.close_position = lambda sym: NS(id="pclose7", client_order_id="cl7")
    live.positions["AAA"] = 1
    m.bought.add("AAA")
    m.day = lm.datetime.now(lm.ET).date()
    proxy.close_position("AAA")
    drain(m)
    ex = [r for r in rows(files) if r["event"] == "mirror_exit"][0]
    assert ex["reason"] == "desk_close_position" and ex["desk_order_id"] == "pclose7" and live.closed == ["AAA"]


def test_disarmed_mirror_still_exits_what_it_holds(files):
    """Skeptic 10/8 #2: deleting the arm file stops new buys, never the exits."""
    live = FakeLive()
    m = mirror(files, live)
    (files / "live_mirror.armed").unlink()
    m.bought.add("AAA")
    live.positions["AAA"] = 1
    assert m.exit_side("close", ("AAA",)) and not m.exit_side(
        "submit", (MarketOrderRequest(symbol="B", qty=1, side=OrderSide.BUY, time_in_force=TimeInForce.DAY),))
    assert m.exit_side("submit", (MarketOrderRequest(symbol="AAA", qty=1, side=OrderSide.SELL,
                                                     time_in_force=TimeInForce.DAY),))


def test_restart_rebuilds_what_the_mirror_holds(files):
    """Skeptic 10/8 #3: after a restart the mirror's own live position is still covered by exits and sync."""
    live = FakeLive()
    m = mirror(files, live)
    m.enqueue("submit", MarketOrderRequest(symbol="AAA", qty=5, side=OrderSide.BUY, time_in_force=TimeInForce.DAY),
              NS(id="p1", client_order_id="e1"))
    drain(m)
    live.positions["ZZZ"] = 1                                    # not ours (no mir- buy today)
    m2 = mirror(files, live)                                     # a fresh process
    m2._roll_day(live)
    assert m2.bought == {"AAA"}


def test_same_name_second_leg_is_logged_as_live_leg_filled(files):
    """Skeptic 10/8 #7: the paper market fallback after a live-filled limit leg is not a one_position skip."""
    live = FakeLive()
    m = mirror(files, live)
    for i, req in enumerate((MarketOrderRequest(symbol="AAA", qty=5, side=OrderSide.BUY, time_in_force=TimeInForce.DAY),
                             MarketOrderRequest(symbol="AAA", qty=5, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))):
        m.enqueue("submit", req, NS(id=f"p{i}", client_order_id=f"e{i}"))
    drain(m)
    assert rows(files)[1]["reason"].startswith("live_leg_filled")


def test_startup_cancels_orphaned_working_orders(files, monkeypatch):
    """Skeptic 10/8 r2 #3: a working mir- limit at restart cannot be reached by the desk's cancel."""
    live = FakeLive()
    m = mirror(files, live)
    live.open.append(NS(id="Lold", client_order_id="mir-e9", symbol="AAA", side="buy", status="new"))
    monkeypatch.setattr(lm, "KEYS_FILE", files / "keys.json")
    (files / "keys.json").write_text("{}")
    m._startup_rebuild()
    assert live.cancelled == ["Lold"] and rows(files)[-1]["event"] == "mirror_startup_cancel"


def test_flattened_names_are_forgotten_and_sync_never_touches_an_overnight_account(files):
    """Skeptic 10/8 r2 #12: after its exit a name is not the mirror's, and no sync while overnight is armed."""
    live = FakeLive()
    m = mirror(files, live)
    m._roll_day(live)
    live.positions["AAA"] = 1
    m.bought.add("AAA")
    m.flatten(live, "AAA", "desk_close_position")
    assert "AAA" not in m.bought
    live.positions["AAA"] = 1                      # e.g. the overnight book's own buy of the same name
    m.bought.add("AAA")                             # even if something re-marked it
    (files / "overnight_live.armed").write_text("")
    m.desk.positions = []
    m.sync()
    assert live.closed == ["AAA"]                   # only the first, deliberate flatten
