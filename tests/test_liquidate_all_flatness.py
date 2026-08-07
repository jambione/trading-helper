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
