"""Unit tests for the Buy-Stop Pullback arm (ai_watch_pullback_buystop_arm)."""
from datetime import datetime
from zoneinfo import ZoneInfo
import ai_entry_watch as ew
import ob_observe

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 2, 10, 30, tzinfo=ET).timestamp()
OPEN = datetime(2026, 11, 2, 9, 30, tzinfo=ET).timestamp()


def _make_rec(sym: str = "XYZ", status: str = "watching", ask: float = 100.0, cm_rsi: float = 60.0):
    return {
        "symbol": sym,
        "status": status,
        "source": "tight",
        "last_ask": ask,
        "price": ask,
        "last_ask_ts": NOW,
        "structure": {"decision": "BUY", "entry_low": 99.0, "entry_high": 101.0, "stop_price": 95.0, "target_1": 105.0, "reward_risk": 1.25},
        "indicator": {"cm_rsi": cm_rsi},
    }


def _load_bars(sym: str, bars):
    ob_observe.reset()
    rows = [(OPEN + 60 * k, o, h, l, c) for k, (o, h, l, c) in enumerate(bars)]
    ob_observe.absorb_rows(sym, rows)


def test_buystop_pullback_off_by_default():
    rec = _make_rec()
    ok, why = ew.pullback_buystop_decide(rec, "XYZ", 100.0, 99.98, {}, now=NOW, sig={})
    assert not ok
    assert why == "pullback_arm_off"


def test_buystop_pullback_hard_gates():
    cfg = {"ai_watch_pullback_buystop_arm": True}
    rec = _make_rec(status="paused")
    ok, why = ew.pullback_buystop_decide(rec, "XYZ", 100.0, 99.98, cfg, now=NOW, sig={})
    assert not ok
    assert why == "not_watching"


def test_buystop_pullback_blocks_pegged_cm_rsi():
    cfg = {"ai_watch_pullback_buystop_arm": True, "ai_watch_pullback_max_cm_rsi": 75.0}
    rec = _make_rec(cm_rsi=88.5)
    ok, why = ew.pullback_buystop_decide(rec, "XYZ", 100.0, 99.98, cfg, now=NOW, sig={})
    assert not ok
    assert why == "cm_rsi_pegged"


def test_buystop_pullback_requires_bars_and_uptrend():
    cfg = {"ai_watch_pullback_buystop_arm": True}
    rec = _make_rec()
    # Too few bars
    _load_bars("XYZ", [(100.0, 100.1, 99.9, 100.0)] * 5)
    ok, why = ew.pullback_buystop_decide(rec, "XYZ", 100.0, 99.98, cfg, now=OPEN + 6 * 60, sig={})
    assert not ok
    assert why == "insufficient_bars"

    # Flat bars (no leg1 gain >= 0.35%)
    _load_bars("XYZ", [(100.0, 100.05, 99.95, 100.0)] * 20)
    ok, why = ew.pullback_buystop_decide(rec, "XYZ", 100.0, 99.98, cfg, now=OPEN + 21 * 60, sig={})
    assert not ok
    assert why == "no_leg1_uptrend"


def test_buystop_pullback_waiting_vs_trigger():
    cfg = {
        "ai_watch_pullback_buystop_arm": True,
        "ai_watch_pullback_min_leg1_gain_pct": 0.35,
        "ai_watch_pullback_min_dip_pct": 0.10,
        "ai_watch_pullback_max_dip_pct": 1.50,
    }
    # Create an uptrend: 100.0 -> 101.0 (+1.0%), then a pullback bar high=100.80, low=100.50, close=100.60
    # 15 bars: start at 100.0 and climb to 101.0
    bars = []
    for k in range(12):
        px = 100.0 + 0.08 * k
        bars.append((px, px + 0.05, px - 0.02, px + 0.03))
    # Bar 12 reaches high 101.0
    bars.append((100.90, 101.00, 100.85, 100.95))
    # Bar 13 pulls back: high 100.80, low 100.50, close 100.60
    bars.append((100.90, 100.80, 100.50, 100.60))
    # Bar 14 consolidates: high 100.75, low 100.55, close 100.65
    bars.append((100.60, 100.75, 100.55, 100.65))

    _load_bars("XYZ", bars)
    t_test = OPEN + 16 * 60

    # Pullback high = max(100.80, 100.75) = 100.80
    # Pullback low = min(100.50, 100.55) = 100.50
    # buy_stop = 100.80 + 0.01 = 100.81
    # stop_loss = 100.50 - 0.01 = 100.49
    # risk = 100.81 - 100.49 = 0.32

    # Case A: Price is at 100.70 (below buy_stop 100.81) -> waits for trigger!
    rec = _make_rec(ask=100.70)
    ok, why = ew.pullback_buystop_decide(rec, "XYZ", 100.70, 100.68, cfg, now=t_test, sig={})
    assert not ok
    assert why == "wait_buystop_trigger"

    # Case B: Price ticks to 100.82 (reaches / crosses buy_stop 100.81) -> triggers entry!
    rec = _make_rec(ask=100.82)
    ok, why = ew.pullback_buystop_decide(rec, "XYZ", 100.82, 100.80, cfg, now=t_test, sig={})
    assert ok
    assert why == "pullback_buystop"
    pb = rec.get("pullback_buystop")
    assert isinstance(pb, dict)
    assert pb["stop_price"] == 100.49
    assert pb["target_1"] == round(100.82 + 0.32, 2)
    assert pb["zone_kind"] == "pullback_buystop"
