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
