"""Shadow loggers (operator 2026-11-03): pullback geometry, the observe-only
row writer, and the midpoint-exit / forward-return scorer's pure parts."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_entry_watch as aew
from tools.studies import shadow_loggers_score as S

ET = ZoneInfo("America/New_York")


def _t(hm: str, day: str = "2026-11-04") -> float:
    return datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=ET).timestamp()


def _leg_bars():
    """10:00-10:14 closed bars: a leg 100 -> 101, then a 2-bar dip to ~100.6."""
    bars = []
    for i in range(13):
        ts = _t("10:00") + 60 * i
        px = 100.0 + i * (1.0 / 12)
        bars.append((ts, px, px + 0.02, px - 0.02, px))
    bars.append((_t("10:13"), 100.65, 100.65, 100.55, 100.6))
    bars.append((_t("10:14"), 100.6, 100.70, 100.55, 100.65))
    return bars


def test_geometry_fires_on_reclaim():
    now = _t("10:15") + 5
    g = aew.pullback_geometry(_leg_bars(), 100.72, now, {})
    assert g is not None
    assert g["buy_stop"] == pytest.approx(100.71)
    assert g["struct_stop"] == pytest.approx(100.54)
    assert 0.10 <= g["dip_pct"] <= 1.50


def test_geometry_waits_below_buy_stop():
    assert aew.pullback_geometry(_leg_bars(), 100.66, _t("10:15") + 5, {}) is None


def test_geometry_needs_ten_closed_bars():
    assert aew.pullback_geometry(_leg_bars()[-5:], 100.72, _t("10:15") + 5, {}) is None


def test_geometry_ignores_open_bar():
    bars = _leg_bars() + [(_t("10:15"), 100.6, 103.0, 100.6, 103.0)]  # still forming
    g = aew.pullback_geometry(bars, 100.72, _t("10:15") + 5, {})
    assert g is not None and g["leg1_high"] < 102


def test_shadow_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(aew, "REPORT_DIR", tmp_path)
    assert aew.pullback_shadow_record({}, "ABC", 100.72, 100.70, {}, _t("10:15") + 5, {}, False, "x") is None
    assert not (tmp_path / "pullback_shadow").exists()


def test_shadow_row_and_dedupe(tmp_path, monkeypatch):
    import ob_observe
    monkeypatch.setattr(aew, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(ob_observe, "bars", lambda s: _leg_bars())
    aew._PULLBACK_SHADOW_LAST.clear()
    cfg = {"ai_watch_pullback_shadow": True}
    rec = {"indicator": {"cm_rsi": 61.0}, "source": "movers"}
    now = _t("10:15") + 5
    row = aew.pullback_shadow_record(rec, "abc", 100.72, 100.70, cfg, now, {"ob_room_pct": 0.8}, False, "wait_setup")
    assert row and row["symbol"] == "ABC" and row["main_why"] == "wait_setup"
    assert aew.pullback_shadow_record(rec, "ABC", 100.72, 100.70, cfg, now + 30, {}, False, "x") is None
    lines = (tmp_path / "pullback_shadow" / "2026-11-04.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["cm_rsi"] == 61.0
    assert rec == {"indicator": {"cm_rsi": 61.0}, "source": "movers"}  # record untouched


def test_shadow_never_raises(monkeypatch):
    import ob_observe
    monkeypatch.setattr(ob_observe, "bars", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    assert aew.pullback_shadow_record({}, "ABC", 1, 1, {"ai_watch_pullback_shadow": True}, _t("10:15"), {}, False, "") is None


def test_default_off():
    import config
    assert config.DEFAULT_CONFIG["ai_watch_pullback_shadow"] is False


# ---- scorer
def test_mid_exit_strict_fill():
    q = [(0.0, 10.00, 10.04)]
    tr = [(3.0, 10.03)]
    r = S.simulate_mid_exit(1.0, q, tr, actual_fill=10.00)
    assert r["limit"] == 10.02 and r["filled_strict"] and r["delta_bp"] == pytest.approx(20.0)


def test_mid_exit_print_at_limit_is_lenient_only():
    q = [(0.0, 10.00, 10.04), (10.5, 9.98, 10.02)]
    tr = [(3.0, 10.02)]
    r = S.simulate_mid_exit(1.0, q, tr, actual_fill=10.00)
    assert not r["filled_strict"] and r["filled_lenient"]
    assert r["sim_px"] == 9.98 and r["delta_bp"] == pytest.approx(-20.0)
    assert r["delta_lenient_bp"] == pytest.approx(20.0)


def test_mid_exit_no_nbbo():
    assert S.simulate_mid_exit(1.0, [(5.0, 10, 10.02)], [], 10.0) is None


def test_pair_sells():
    rows = [
        {"event": "submit", "action": "SELL", "symbol": "X", "order_id": "a", "ts": 5.0, "note": "close_out"},
        {"event": "fill", "side": "sell", "order_id": "a", "filled_avg_price": 9.9, "filled_qty": 3},
        {"event": "submit", "action": "BUY", "symbol": "X", "order_id": "b", "ts": 1.0},
        {"event": "fill", "side": "buy", "order_id": "b", "filled_avg_price": 9.8, "filled_qty": 3},
    ]
    p = S.pair_sells(rows)
    assert len(p) == 1 and p[0]["t0"] == 5.0 and p[0]["fill"] == 9.9


def test_forward_uses_next_bar_open():
    bars = [(_t("10:00") + 60 * i, 100 + i, 100 + i + 0.5, 100 + i - 0.5, 100 + i + 0.2) for i in range(40)]
    f = S.forward_from_bars(bars, _t("10:00") + 10)
    assert f["entry"] == 101  # 10:01 open, not the signal minute
    assert f["fwd5"] == pytest.approx((105.2 - 101) / 101 * 1e4)


def test_control_times_seeded():
    bars = [(_t("10:00") + 60 * i, 1, 1, 1, 1) for i in range(300)]
    assert S.control_times("X", "2026-11-04", bars) == S.control_times("X", "2026-11-04", bars)
