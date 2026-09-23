"""Capped stop sell: marketable limit with a floor, market for the remainder."""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import alpaca_trader as at  # noqa: E402


class _Client:
    def __init__(self, fill_on=("limit",), qty_after_cancel=10.0):
        self.fill_on = fill_on
        self.qty = 10.0
        self.qty_after_cancel = qty_after_cancel
        self.submitted, self.cancelled, self.closed = [], [], []

    def get_open_position(self, sym):
        if self.qty <= 0:
            raise RuntimeError("no position")
        return types.SimpleNamespace(qty=str(self.qty), current_price="41.90")

    def submit_order(self, req):
        self.submitted.append(req)
        st = "OrderStatus.FILLED" if "limit" in self.fill_on else "OrderStatus.NEW"
        if st.endswith("FILLED"):
            self.qty = 0
        return types.SimpleNamespace(id="L1", status=st)

    def get_order_by_id(self, oid):
        return types.SimpleNamespace(id=oid, status="OrderStatus.NEW")

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)
        self.qty = self.qty_after_cancel

    def close_position(self, sym):
        self.closed.append(sym)
        self.qty = 0
        return types.SimpleNamespace(id="M1", status="OrderStatus.PENDING_NEW")


@pytest.fixture
def client(monkeypatch):
    def make(**kw):
        c = _Client(**kw)
        monkeypatch.setattr(at, "_client", c)
        monkeypatch.setattr(at, "_can_mutate", lambda: True)
        monkeypatch.setattr(at, "market_is_open", lambda: True)
        monkeypatch.setattr(at, "cancel_open_orders", lambda t: {"canceled": 0})
        monkeypatch.setattr(at, "_log_action", lambda *a, **k: None)
        monkeypatch.setattr(at.time, "sleep", lambda s: None)
        return c
    return make


def test_limit_price_is_the_collar_under_the_print_rounded_down():
    assert at._collar_limit_px(41.475, 0.15) == 41.41     # 41.4127 -> 41.41
    assert at._collar_limit_px(0.5, 0.15) == 0.4992


def test_collared_sell_fills_on_the_limit(client):
    c = client(fill_on=("limit",))
    out = at.close_out("VKTX", price=41.475, collar_pct=0.15, collar_wait_sec=2)
    assert out["ok"] and out["order_id"] == "L1"
    assert float(c.submitted[0].limit_price) == 41.41
    assert c.closed == [] and c.cancelled == []
    assert "collar_limit" in out["note"]


def test_unfilled_remainder_goes_to_market(client, monkeypatch):
    c = client(fill_on=(), qty_after_cancel=4.0)
    t = [1000.0]
    monkeypatch.setattr(at.time, "time", lambda: t.__setitem__(0, t[0] + 1) or t[0])
    out = at.close_out("VKTX", price=41.475, collar_pct=0.15, collar_wait_sec=2)
    assert c.cancelled == ["L1"]
    assert c.closed == ["VKTX"]
    assert out["order_id"] == "M1"
    assert "-> market 4" in out["note"]


def test_zero_collar_is_plain_market(client):
    c = client()
    out = at.close_out("VKTX", price=41.475)
    assert c.submitted == [] and c.closed == ["VKTX"]
    assert out["order_id"] == "M1"


def test_no_price_falls_back_to_market(client):
    c = client()
    out = at.close_out("VKTX", collar_pct=0.15)
    assert c.submitted == [] and c.closed == ["VKTX"] and out["order_id"] == "M1"
