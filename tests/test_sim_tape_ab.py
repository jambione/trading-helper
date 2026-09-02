"""Entry-filter A/B on a tiny synthetic tape (no Alpaca)."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import sim_fill_replay as sfr  # noqa: E402
import sim_tape_ab as ab  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(hour: int, minute: int, day: str = "2026-09-01") -> float:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(
        tzinfo=ET).timestamp()


def _arm(sym: str, minute: int, *, src: str, macd: str, rsi: str = "realtime") -> dict:
    return {
        "ts": _ts(10, minute),
        "symbol": sym,
        "arm_ok": True,
        "arm_why": "test",
        "price": 10.0,
        "last_ask_src": src,
        "macd_src": macd,
        "cm_rsi_src": rsi,
        "in_zone": True,
    }


def _fill_row(sym: str, minute: int, live_usd: float = -1.0) -> dict:
    return {
        "symbol": sym,
        "entry_price": 10.0,
        "stop_price": 9.5,
        "target_1": 10.3,
        "total_qty": 10,
        "exit_price": 9.9,
        "realized_r_multiple": -0.2,
        "realized_pl_usd": live_usd,
        "close_reason": "local_trail",
        "entry_time": _ts(10, minute),
        "exit_time": _ts(10, minute + 2),
        "features": {},
    }


def _bars(entry_minute: int = 1) -> list:
    # Flat then stomp the shelf so trail exits.
    out = []
    for i in range(5):
        m = entry_minute + i
        ts = _ts(10, m)
        c = 10.0
        low = 9.90 if i == 3 else 9.99
        out.append((ts, c, c + 0.01, low, c))
    return out


def test_parse_variant_rest_vs_stream():
    a = ab.parse_variant("rest+give=0.2")
    assert a.filters.require_stream_price is False
    assert a.overlay.get("give_r") == pytest.approx(0.2)
    b = ab.parse_variant("stream+rt_macd+rt_rsi+give=0.35")
    assert b.filters.require_stream_price is True
    assert b.filters.require_realtime_macd is True
    assert b.filters.require_realtime_rsi is True
    assert b.overlay.get("give_r") == pytest.approx(0.35)


def test_entry_filter_blocks_rest_and_alpaca_macd():
    f = ab.EntryFilters(require_stream_price=True, require_realtime_macd=True)
    assert ab.entry_filter_block(
        {"last_ask_src": "rest", "macd_src": "realtime"}, f) == "stream_required"
    assert ab.entry_filter_block(
        {"last_ask_src": "stream", "macd_src": "alpaca"}, f) == "macd_not_realtime"
    assert ab.entry_filter_block(
        {"last_ask_src": "stream", "macd_src": "realtime"}, f) is None


def test_filter_fills_drops_rest_arms():
    shadow = [
        _arm("AAA", 0, src="rest", macd="realtime"),
        _arm("BBB", 0, src="stream", macd="realtime"),
        _arm("CCC", 0, src="stream", macd="alpaca"),
    ]
    fills = [
        sfr.parse_fill(_fill_row("AAA", 1, -1.0))[0],
        sfr.parse_fill(_fill_row("BBB", 1, -2.0))[0],
        sfr.parse_fill(_fill_row("CCC", 1, -3.0))[0],
    ]
    arms = ab.shadow_arms_by_sym(shadow)
    filt = ab.EntryFilters(require_stream_price=True, require_realtime_macd=True)
    kept, counts, audit = ab.filter_fills(fills, arms, filt)
    assert [f["symbol"] for f in kept] == ["BBB"]
    assert counts["blocked_stream"] == 1
    assert counts["blocked_macd"] == 1
    assert counts["kept"] == 1
    assert sum(1 for a in audit if a["kept"]) == 1


def test_ab_run_keeps_fewer_fills_under_new_gates():
    shadow = [
        _arm("AAA", 0, src="rest", macd="realtime"),
        _arm("BBB", 0, src="stream", macd="realtime"),
    ]
    outcomes = [_fill_row("AAA", 1, -1.0), _fill_row("BBB", 1, -2.0)]
    fills = [sfr.parse_fill(r)[0] for r in outcomes]
    bars = {
        sfr.cache_key("AAA", "2026-09-01"): _bars(1),
        sfr.cache_key("BBB", "2026-09-01"): _bars(1),
    }
    cfg = {
        "desk_product": "scalp_legacy",
        "ai_watch_synth_stop_pct": 5.0,
        "ai_watch_synth_rr": 0.0,
        "ai_watch_synth_scale_out_pct": 50.0,
        "ai_local_trail_enabled": True,
        "ai_local_trail_give_r": 0.20,
        "ai_local_trail_give_open_r": 0.20,
        "ai_local_trail_min_give_px": 0.0,
        "ai_local_trail_give_max_pct": 0.0,
        "ai_local_trail_arm_r": 0.0,
        "ai_local_trail_be_at_r": 0.0,
        "ai_local_trail_print_ring": 2,
        "ai_dead_trade_min": 0,
        "ai_exit_left_overbought": False,
        "ai_watch_exhaustion_rules": False,
    }
    old = ab.parse_variant("rest+give=0.2", name="A")
    new = ab.parse_variant("stream+rt_macd+give=0.35", name="B")
    a = ab.run_variant(
        name="A", variant=old, days=["2026-09-01"], cfg=cfg,
        fills=fills, shadow_rows=shadow, bar_cache=bars,
        shadow_by_sym=sfr.shadow_index(shadow), allow_shadow=False,
    )
    b = ab.run_variant(
        name="B", variant=new, days=["2026-09-01"], cfg=cfg,
        fills=fills, shadow_rows=shadow, bar_cache=bars,
        shadow_by_sym=sfr.shadow_index(shadow), allow_shadow=False,
    )
    assert a["n_fills_kept"] == 2
    assert b["n_fills_kept"] == 1
    # A allows REST → admission does not count stream blocks
    assert a["admission"]["n_blocked_by_stream"] == 0
    assert a["admission"]["n_pass"] == 2
    # B requires stream → one REST arm blocked at admission
    assert b["admission"]["n_blocked_by_stream"] == 1
    assert b["admission"]["n_pass"] == 1
    assert b["n_walked"] == 1
    assert a["n_walked"] == 2
