"""Entry-cost A/B: arm limits, rotation, and rest-then-cross at the broker."""
import alpaca_trader
import ai_positions as cp


def test_limits_per_arm():
    assert cp.entry_test_limit("ask", 10.00, 10.01) is None
    assert cp.entry_test_limit("bid", 10.00, 10.01) == 10.00
    # 1c spread: mid rounded down is the bid
    assert cp.entry_test_limit("mid_down", 10.00, 10.01) == 10.00
    assert cp.entry_test_limit("mid_down", 10.00, 10.03) == 10.01
    assert cp.entry_test_limit("mid_down", 25.42, 25.47) == 25.44
    # no usable quote: passive arms fall back to market
    assert cp.entry_test_limit("bid", 0, 10.01) is None
    assert cp.entry_test_limit("mid_down", 10.02, 10.01) is None


def test_rotation_and_off(monkeypatch):
    monkeypatch.setattr(cp, "_ENTRY_TEST_N", [0])
    assert cp.entry_test_plan({"ai_entry_test_arms": ""}, 1, 2) is None
    cfg = {"ai_entry_test_arms": "ask,mid_down,bid"}
    arms = [cp.entry_test_plan(cfg, 10.0, 10.04)["arm"] for _ in range(6)]
    assert arms == ["ask", "mid_down", "bid"] * 2
    p = cp.entry_test_plan({"ai_entry_test_arms": "bid"}, 0, 10.0)
    assert p["arm"] == "bid" and p["limit"] is None and p["fallback"] is True


class FakeBroker:
    """Scripted get_order replies per order id."""

    def __init__(self, monkeypatch, limit_replies, market_reply=None):
        self.limit_replies = list(limit_replies)
        self.market_reply = market_reply
        self.cancelled = []
        self.market_qty = None
        monkeypatch.setattr(alpaca_trader, "buy_limit_at_price",
                            lambda t, px, qty=None, note="": {"ok": True, "order_id": "L", "qty": qty})
        monkeypatch.setattr(alpaca_trader, "buy_market_shares", self._mkt)
        monkeypatch.setattr(alpaca_trader, "get_order", self._get)
        monkeypatch.setattr(alpaca_trader, "cancel_order_id", lambda oid: self.cancelled.append(oid) or True)

    def _mkt(self, t, ref, qty=None):
        self.market_qty = qty
        return {"ok": True, "order_id": "M", "qty": qty}

    def _get(self, oid):
        if oid == "M":
            return self.market_reply
        return self.limit_replies.pop(0) if len(self.limit_replies) > 1 else self.limit_replies[0]


def _run(clock_step=1.0):
    t = [0.0]

    def clock():
        t[0] += clock_step
        return t[0]
    return alpaca_trader.buy_limit_then_market(
        "XYZ", 100, 10.00, ref_price=10.01, wait_sec=10, _sleep=lambda s: None, _clock=clock)


def test_passive_fill_never_crosses(monkeypatch):
    b = FakeBroker(monkeypatch, [{"status": "OrderStatus.NEW"},
                                 {"status": "OrderStatus.FILLED", "filled_qty": 100, "filled_avg_price": 10.0}])
    out = _run()
    assert out["passive_qty"] == 100 and out["crossed_qty"] == 0
    assert out["fill_px"] == 10.0 and b.cancelled == [] and b.market_qty is None


def test_unfilled_crosses_everything(monkeypatch):
    b = FakeBroker(monkeypatch, [{"status": "OrderStatus.NEW", "filled_qty": 0}],
                   market_reply={"status": "OrderStatus.FILLED", "filled_qty": 100, "filled_avg_price": 10.01})
    # cancel confirmation: once polled after the wait, report cancelled
    calls = {"n": 0}
    orig = b._get

    def get(oid):
        if oid == "L" and b.cancelled:
            return {"status": "OrderStatus.CANCELED", "filled_qty": 0}
        calls["n"] += 1
        return orig(oid)
    monkeypatch.setattr(alpaca_trader, "get_order", get)
    out = _run()
    assert b.cancelled == ["L"] and b.market_qty == 100
    assert out["passive_qty"] == 0 and out["crossed_qty"] == 100
    assert out["fill_px"] == 10.01 and out["order_id"] == "M"


def test_partial_fill_blends(monkeypatch):
    b = FakeBroker(monkeypatch, [{"status": "OrderStatus.PARTIALLY_FILLED", "filled_qty": 40,
                                  "filled_avg_price": 10.0}],
                   market_reply={"status": "OrderStatus.FILLED", "filled_qty": 60, "filled_avg_price": 10.02})

    def get(oid):
        if oid == "L" and b.cancelled:
            return {"status": "OrderStatus.CANCELED", "filled_qty": 40, "filled_avg_price": 10.0}
        return FakeBroker._get(b, oid)
    monkeypatch.setattr(alpaca_trader, "get_order", get)
    out = _run()
    assert b.market_qty == 60
    assert out["passive_qty"] == 40 and out["crossed_qty"] == 60 and out["qty"] == 100
    assert abs(out["fill_px"] - 10.012) < 1e-9
    assert out["order_id"] == "M" and out["limit_order_id"] == "L"


# ── the "wait" arm: buy at market once a wide spread tightens ─────────────

def test_wait_arm_has_no_limit_and_is_not_a_fallback(monkeypatch):
    monkeypatch.setattr(cp, "_ENTRY_TEST_N", [0])
    assert cp.entry_test_limit("wait", 10.00, 10.04) is None
    p = cp.entry_test_plan({"ai_entry_test_arms": "wait"}, 10.00, 10.04)
    assert p["arm"] == "wait" and p["limit"] is None and p["fallback"] is False
    monkeypatch.setattr(cp, "_ENTRY_TEST_N", [0])
    arms = [cp.entry_test_plan({"ai_entry_test_arms": "ask,mid_down,bid,wait"}, 10, 10.04)["arm"]
            for _ in range(4)]
    assert arms == ["ask", "mid_down", "bid", "wait"]


def _clock():
    t = [0.0]

    def clock():
        return t[0]

    def sleep(s):
        t[0] += s
    return clock, sleep


def test_wait_arm_buys_at_once_on_a_tight_spread():
    clock, sleep = _clock()
    p = cp.entry_wait_for_spread("XYZ", {"arm": "wait", "bid": 100.00, "ask": 100.02}, {},
                                 book=lambda s: (_ for _ in ()).throw(AssertionError("polled")),
                                 _sleep=sleep, _clock=clock)
    assert p["tight"] is True and p["waited_sec"] == 0.0     # 2 bp spread: no wait


def test_wait_arm_buys_when_the_spread_tightens():
    clock, sleep = _clock()
    quotes = iter([(10.00, 10.10), (10.02, 10.08), (10.03, 10.07)])
    p = cp.entry_wait_for_spread("XYZ", {"arm": "wait", "bid": 10.00, "ask": 10.10},
                                 {"ai_entry_test_cross_sec": 10, "ai_entry_wait_ratio": 0.5,
                                  "ai_entry_wait_poll_sec": 0.25},
                                 book=lambda s: next(quotes), _sleep=sleep, _clock=clock)
    assert p["tightened"] is True and p["buy_ask"] == 10.07 and p["waited_sec"] == 0.8


def test_wait_arm_buys_at_the_deadline_when_it_never_tightens():
    clock, sleep = _clock()
    p = cp.entry_wait_for_spread("XYZ", {"arm": "wait", "bid": 10.00, "ask": 10.10},
                                 {"ai_entry_test_cross_sec": 2},
                                 book=lambda s: (10.01, 10.12), _sleep=sleep, _clock=clock)
    assert p["tightened"] is False and p["buy_ask"] == 10.12 and p["waited_sec"] >= 2


def test_wait_arm_without_a_quote_buys_now():
    p = cp.entry_wait_for_spread("XYZ", {"arm": "wait", "bid": None, "ask": 10.1}, {})
    assert p["waited_sec"] == 0.0 and p["note"] == "no quote"


def test_plan_stamps_the_decision_time(monkeypatch):
    # tools/entry_arm_score.py prices every arm here, not at the wait arm's delayed submit.
    import time
    monkeypatch.setattr(cp, "_ENTRY_TEST_N", [0])
    t0 = time.time()
    p = cp.entry_test_plan({"ai_entry_test_arms": "wait"}, 10.00, 10.04)
    assert t0 - 1 <= p["t_decide"] <= time.time() + 1


class _FakeQuoteClient:
    """Latest-quote client double: counts calls, serves (bid, ask) in order."""

    def __init__(self, quotes):
        self.quotes, self.calls = list(quotes), 0

    def get_stock_latest_quote(self, req):
        import datetime as dt
        import types
        self.calls += 1
        bid, ask = self.quotes.pop(0) if len(self.quotes) > 1 else self.quotes[0]
        sym = req.symbol_or_symbols
        ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=0.4)
        return {sym: types.SimpleNamespace(bid_price=bid, ask_price=ask, timestamp=ts)}


def test_wait_arm_reads_bid_and_ask_from_one_quote(monkeypatch):
    # Default book: one IEX quote per poll on ONE reused client, never the split cache reads.
    fake = _FakeQuoteClient([(10.00, 10.10), (10.03, 10.07)])
    monkeypatch.setattr(cp, "_WAIT_QUOTE_CLIENT", [fake])
    monkeypatch.setattr(cp, "_premarket_book", lambda s: (_ for _ in ()).throw(AssertionError("split read")))
    clock, sleep = _clock()
    p = cp.entry_wait_for_spread("XYZ", {"arm": "wait", "bid": 10.00, "ask": 10.10},
                                 {"ai_entry_test_cross_sec": 10, "ai_entry_wait_ratio": 0.5},
                                 _sleep=sleep, _clock=clock)
    assert p["tightened"] is True and (p["buy_bid"], p["buy_ask"]) == (10.03, 10.07)
    assert p["quote_src"] == "iex_one_quote" and 0.3 <= p["buy_quote_age_sec"] <= 2.0
    assert fake.calls == 2 and cp._WAIT_QUOTE_CLIENT[0] is fake


def test_wait_arm_polls_no_faster_than_the_poll_knob(monkeypatch):
    # Round 2 review: 0.25 s polls made ~40 REST calls per 10 s wait on a shared data budget.
    calls = []
    clock, sleep = _clock()
    cp.entry_wait_for_spread("XYZ", {"arm": "wait", "bid": 10.00, "ask": 10.10},
                             {"ai_entry_test_cross_sec": 10},
                             book=lambda s: calls.append(1) or (10.01, 10.11),
                             _sleep=sleep, _clock=clock)
    assert len(calls) == 10                                   # default ai_entry_wait_poll_sec = 1.0


def test_wait_arm_backs_off_after_failed_reads(monkeypatch):
    clock, sleep = _clock()
    slept = []
    p = cp.entry_wait_for_spread("XYZ", {"arm": "wait", "bid": 10.00, "ask": 10.10},
                                 {"ai_entry_test_cross_sec": 10},
                                 book=lambda s: (None, None),
                                 _sleep=lambda x: slept.append(x) or sleep(x), _clock=clock)
    assert slept[:4] == [1.0, 2.0, 3.0, 3.0] and abs(sum(slept) - 10.0) < 1e-9
    assert p["tightened"] is False and p["buy_ask"] == 10.10 and p["buy_quote_age_sec"] is None