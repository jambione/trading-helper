"""End-to-end desk auto path: constructive gate → risk plan → mocked bracket order."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

import alpaca_trader as tr  # noqa: E402
import desk_risk as dr  # noqa: E402
from auto_limit import AutoLimitState, constructive_setup, process_rows  # noqa: E402
import desk_actions as desk  # noqa: E402


def _good_row(sym="SOFI", **sp_extra):
    sp = {
        "status": "buy_zone",
        "bars_fetched": True,
        "proximity_pct": 100,
        "sell_signal": False,
        "buy_signal": True,
        "pctr_falling": False,
        "pctr_slow_falling": False,
        "cm_rsi": 20.0,
        "macd_ok": True,
    }
    sp.update(sp_extra)
    return {"ticker": sym, "rvol": 3.5, "signal_proximity": sp}


def test_constructive_then_plan_then_bracket_order_shape():
    """Full happy path with mocked Alpaca: correct qty, stop < limit < target."""
    # 1) Constructive gate
    ok, why = constructive_setup(_good_row())
    assert ok, why

    # 2) Risk plan from policy entry
    entry = 16.00
    plan = dr.plan_long(
        entry, equity=100_000, risk_pct=0.35, stop_pct=0.40, reward_r=2.0,
    )
    assert plan is not None
    assert plan.qty >= 1
    assert plan.stop < plan.entry < plan.target
    # risk at stop ≈ risk_pct of equity
    loss_if_stopped = plan.qty * (plan.entry - plan.stop)
    assert loss_if_stopped <= 100_000 * 0.0035 + plan.r_per_share  # floor slack

    # 3) Bracket submit
    fake = MagicMock()
    fake.submit_order.return_value = SimpleNamespace(id="br-desk-1", status="accepted")
    tr._mode = "paper"
    tr._client = fake
    tr._extended_hours = False
    with patch.object(tr, "_log_action"):
        out = tr.buy_limit_bracket(
            "SOFI", plan.qty, plan.entry, plan.stop, plan.target,
        )
    assert out["ok"] is True
    assert out["buy_order_id"] == "br-desk-1"
    assert out["limit_px"] == plan.entry
    assert out["stop_px"] == plan.stop
    assert out["target_px"] == plan.target

    req = fake.submit_order.call_args[0][0]
    assert str(req.order_class).lower().endswith("bracket")
    assert int(req.qty) == plan.qty
    assert float(req.limit_price) == plan.entry
    assert req.take_profit is not None
    assert float(req.take_profit.limit_price) == plan.target
    assert req.stop_loss is not None
    assert float(req.stop_loss.stop_price) == plan.stop
    assert float(req.stop_loss.limit_price) < plan.stop  # stop-limit slightly under


def test_process_rows_fires_bracket_fn_only_when_constructive():
    state = AutoLimitState()
    state.prev_status["SOFI"] = "aligning"
    calls = []

    def buy_fn(sym, row):
        calls.append(sym)
        plan = dr.plan_long(16.0, equity=50_000, risk_pct=0.35, stop_pct=0.4)
        return (f"BUY {sym} ok", plan.as_dict() if plan else None)

    cfg = {
        "auto_limit_enabled": True,
        "auto_limit_live": False,
        "auto_limit_require_constructive": True,
        "auto_limit_min_proximity_pct": 67,
    }
    # Bad: sell_signal
    bad = _good_row(sell_signal=True)
    process_rows(
        [bad], state, cfg=cfg, trader_mode="paper",
        position_symbols=set(), buy_fn=buy_fn, now=1_700_000_000.0,
    )
    assert calls == []

    # Reset edge: was buy_zone in prev from last walk? status was buy_zone so prev updated
    state.prev_status["SOFI"] = "watching"
    good = _good_row(sell_signal=False)
    ev = process_rows(
        [good], state, cfg=cfg, trader_mode="paper",
        position_symbols=set(), buy_fn=buy_fn, now=1_700_000_100.0,
    )
    assert calls == ["SOFI"]
    assert any(e.get("kind") == "auto_limit" and e.get("plan") for e in ev)


def test_desk_buy_bracket_uses_equity_and_submits(monkeypatch):
    """desk_buy_bracket with mocked quotes + trader."""
    desk._trader_ready = True
    desk._trader_mode = "paper"
    desk._limit_pad_pct = 0.1

    monkeypatch.setattr(desk, "_latest_bid", lambda s: 15.98)
    monkeypatch.setattr(desk, "_latest_ask", lambda s: 16.00)
    monkeypatch.setattr(desk, "_latest_price", lambda s: 15.99)

    fake_out = {
        "ok": True,
        "buy_order_id": "oid-1",
        "qty": 100,
        "limit_px": 16.0,
        "stop_px": 15.94,
        "target_px": 16.12,
        "status": "accepted",
    }

    class FakeAT:
        @staticmethod
        def is_active():
            return True

        @staticmethod
        def get_equity():
            return 100_000.0

        @staticmethod
        def buy_limit_bracket(sym, qty, lim, stop, target):
            assert qty >= 1
            assert stop < lim < target
            return {**fake_out, "qty": int(qty), "limit_px": lim,
                    "stop_px": stop, "target_px": target}

        @staticmethod
        def buy_limit_at_price(*a, **k):
            raise AssertionError("should use bracket path")

    monkeypatch.setitem(sys.modules, "alpaca_trader", FakeAT)
    # desk imports alpaca_trader inside function — patch module attribute
    import alpaca_trader as real_at
    monkeypatch.setattr(real_at, "is_active", FakeAT.is_active)
    monkeypatch.setattr(real_at, "get_equity", FakeAT.get_equity)
    monkeypatch.setattr(real_at, "buy_limit_bracket", FakeAT.buy_limit_bracket)

    msg, plan = desk.desk_buy_bracket(
        "SOFI",
        row=_good_row(),
        cfg={
            "risk_pct": 0.35,
            "stop_pct": 0.40,
            "reward_r": 2.0,
            "entry_max_spread_pct": 2.0,
            "entry_pad_max_pct": 0.15,
            "rvol_hot": 3.0,
        },
    )
    assert plan is not None
    assert "SOFI" in msg and "SL@" in msg and "TP@" in msg
    assert plan["qty"] >= 1
    assert plan["stop"] < plan["entry"] < plan["target"]
    assert plan.get("bracket") is True


def test_buy_limit_bracket_rejects_bad_geometry():
    tr._mode = "paper"
    tr._client = MagicMock()
    out = tr.buy_limit_bracket("X", 10, 10.0, 10.5, 11.0)  # stop above entry
    assert not out["ok"]
    tr._client.submit_order.assert_not_called()
