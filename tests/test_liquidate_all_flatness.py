"""liquidate_all must mean the book is flat — nothing weaker.

On 2026-08-07 the EOD flatten cancelled USAR's bracket, was refused on the
close (the cancel was still pending, so the shares were still held_for_orders
and available was 0), and then reported ok:true because it had cancelled
something. The caller stamped the day done, which is what makes
_eod_liquidate_due return False for the rest of the day — so the one mechanism
that would have retried was retired. The position was left with no stop, no
target and no scheduled exit, heading into a weekend.
"""
import os
import sys

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _ROOT)

import alpaca_trader as tr  # noqa: E402


class _Broker:
    """Positions that only close once their cancels have settled."""

    def __init__(self, positions, refuse_first=0):
        self.positions = dict(positions)
        self.refuse_first = refuse_first
        self.close_calls = []

    def close_out_effect(self, sym):
        self.close_calls.append(sym)
        if len(self.close_calls) <= self.refuse_first:
            return {"ok": False, "status": "insufficient qty available"}
        self.positions.pop(sym, None)
        return {"ok": True, "order_id": "c1"}


def _arm(monkeypatch, broker, canceled=1):
    monkeypatch.setattr(tr, "_can_mutate", lambda: True)
    monkeypatch.setattr(tr, "cancel_open_orders",
                        lambda t: {"ok": True, "canceled": canceled, "errors": []})
    monkeypatch.setattr(tr, "get_positions_detail",
                        lambda: dict(broker.positions))
    monkeypatch.setattr(tr, "close_out",
                        lambda sym, **kw: broker.close_out_effect(sym))
    monkeypatch.setattr(tr, "_CANCEL_SETTLE_SEC", 0.0)


def test_cancelling_an_order_is_not_liquidating_a_position(monkeypatch):
    """The exact 2026-08-07 shape: canceled=1, closed=0, and ok must be False."""
    broker = _Broker({"USAR": {"qty": 105}}, refuse_first=99)  # never closes
    _arm(monkeypatch, broker)

    out = tr.liquidate_all()

    assert out["ok"] is False, "a cancelled order is not a flat book"
    assert out["closed"] == 0
    assert out["still_open"] == ["USAR"], "the leftover must be named"


def test_a_pending_cancel_refusal_is_retried_not_accepted(monkeypatch):
    """The close is refused while the cancel settles; one refusal is not final."""
    broker = _Broker({"USAR": {"qty": 105}}, refuse_first=1)
    _arm(monkeypatch, broker)

    out = tr.liquidate_all()

    assert out["ok"] is True
    assert out["closed"] == 1
    assert out["still_open"] == []
    assert len(broker.close_calls) >= 2, "must have retried after the refusal"


def test_a_clean_flatten_reports_ok(monkeypatch):
    broker = _Broker({"AAA": {"qty": 1}, "BBB": {"qty": 2}})
    _arm(monkeypatch, broker)

    out = tr.liquidate_all()

    assert out["ok"] is True
    assert out["closed"] == 2
    assert sorted(out["symbols"]) == ["AAA", "BBB"]
    assert out["still_open"] == []


def test_an_unverifiable_book_is_not_reported_flat(monkeypatch):
    """If the post-check cannot be read, do not claim success."""
    broker = _Broker({})
    _arm(monkeypatch, broker)
    monkeypatch.setattr(tr, "get_positions_detail", lambda: None)

    out = tr.liquidate_all()

    assert out["ok"] is False
    assert "unverified" in out["still_open"]


def test_flat_to_begin_with_is_ok(monkeypatch):
    broker = _Broker({})
    _arm(monkeypatch, broker, canceled=0)

    out = tr.liquidate_all()

    assert out["ok"] is True and out["closed"] == 0 and out["still_open"] == []


# ── 2026-11-17: closes already in flight are left alone ──────────────────────
from datetime import datetime, timedelta, timezone  # noqa: E402


class _Order:
    def __init__(self, oid, sym, side, typ, age_sec):
        self.id, self.symbol, self.side, self.type = oid, sym, side, typ
        self.submitted_at = datetime.now(timezone.utc) - timedelta(seconds=age_sec)


class _PieceBroker:
    """The paper broker on 11/17: a market close stays open and fills in pieces over several seconds.
    Cancelling it before it finishes means the position is never closed."""

    def __init__(self, positions):
        self.positions = dict(positions)
        self.orders = {}          # id -> _Order
        self.canceled = []
        self.n = 0

    def submit_close(self, sym):
        self.n += 1
        o = _Order(f"o{self.n}", sym, "sell", "market", 0)
        self.orders[o.id] = o
        return {"ok": True, "order_id": o.id}

    def cancel(self, oid):
        self.canceled.append(oid)
        self.orders.pop(oid, None)

    def fill_all(self):
        for o in list(self.orders.values()):
            self.positions.pop(o.symbol, None)
            self.orders.pop(o.id, None)


def _arm_pieces(monkeypatch, b):
    class _C:
        def get_orders(self, filter=None):
            return list(b.orders.values())
    monkeypatch.setattr(tr, "_client", _C())
    monkeypatch.setattr(tr, "_can_mutate", lambda: True)
    monkeypatch.setattr(tr, "_cancel_order_by_id", lambda oid: b.cancel(oid))
    monkeypatch.setattr(tr, "get_positions_detail", lambda: dict(b.positions))
    monkeypatch.setattr(tr, "close_out", lambda sym, **kw: b.submit_close(sym))
    monkeypatch.setattr(tr, "_CANCEL_SETTLE_SEC", 0.0)


def test_a_second_pass_does_not_cancel_the_first_pass_closes(monkeypatch):
    b = _PieceBroker({"COLD": {"qty": 39.2}, "GOOG": {"qty": 2.19}})
    _arm_pieces(monkeypatch, b)
    first = tr.liquidate_all()
    assert first["ok"] is False and sorted(first["still_open"]) == ["COLD", "GOOG"]   # not filled yet
    assert b.n == 2
    second = tr.liquidate_all()                                  # the next loop pass, seconds later
    assert b.canceled == [], "cancelled its own closes in flight - the 11/17 loop"
    assert b.n == 2, "re-sent closes that were already on their way"
    assert sorted(second["pending"]) == ["COLD", "GOOG"]
    b.fill_all()
    assert tr.liquidate_all()["ok"] is True


def test_a_stuck_close_is_cancelled_and_resent(monkeypatch):
    b = _PieceBroker({"COLD": {"qty": 39.2}})
    _arm_pieces(monkeypatch, b)
    b.orders["old"] = _Order("old", "COLD", "sell", "market", tr._CLOSE_INFLIGHT_MAX_SEC + 5)
    out = tr.liquidate_all()
    assert b.canceled == ["old"] and b.n == 1 and out["pending"] == []


def test_other_open_orders_are_still_cancelled(monkeypatch):
    b = _PieceBroker({"AAA": {"qty": 5}})
    _arm_pieces(monkeypatch, b)
    b.orders["stop"] = _Order("stop", "AAA", "sell", "stop", 5)      # a protective stop is not a close
    b.orders["buy"] = _Order("buy", "BBB", "buy", "limit", 5)
    tr.liquidate_all()
    assert sorted(b.canceled) == ["buy", "stop"] and b.n == 1


def test_kept_names_orders_survive(monkeypatch):
    b = _PieceBroker({"AAA": {"qty": 5}, "H4": {"qty": 3}})
    _arm_pieces(monkeypatch, b)
    b.orders["h4stop"] = _Order("h4stop", "H4", "sell", "stop", 5)
    out = tr.liquidate_all(except_symbols={"H4"})
    assert "h4stop" not in b.canceled and out["still_open"] == ["AAA"]
