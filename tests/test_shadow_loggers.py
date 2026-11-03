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


# ---- policy P1 v2 (midpoint_policy_prereg.json amended_1): IEX decides, SIP scores
T0 = None


def _t0():
    return _t("10:15")


def _p(iex, sip_q, sip_t, reason="local_trail", fill=10.00, **kw):
    t0 = _t0()
    sh = lambda rows: [(t0 + r[0],) + tuple(r[1:]) for r in rows]
    return S.simulate_policy_exit(t0, sh(sip_q), sh(sip_t), sh(iex), fill, reason=reason, **kw)


WIDE = [(-1.0, 10.00, 10.06)]


def test_policy_decides_on_iex_not_sip():
    # SIP is one cent wide, IEX is 6 cents wide: the desk only sees IEX, so it goes passive
    r = _p(WIDE, [(-1.0, 10.02, 10.03), (14.0, 10.02, 10.03)], [(3.0, 10.04)])
    assert r["route"] == "passive" and r["limit"] == 10.03


def test_policy_stale_iex_goes_market():
    r = _p([(-30.0, 10.00, 10.06)], [(-1.0, 10.00, 10.06)], [])
    assert r["route"] == "market:no_iex" and r["delta_bp"] == 0.0


def test_policy_one_tick_and_tight_go_market():
    assert _p([(-1.0, 10.00, 10.01)], WIDE, [])["route"] == "market:one_tick"
    assert _p([(-1.0, 100.00, 100.03)], [(-1.0, 100.0, 100.03)], [], fill=100.0)["route"] == "market:tight"


def test_policy_urgent_eod_unknown_go_market():
    assert _p(WIDE, WIDE, [], reason="dead_trade")["route"] == "market:urgent"
    assert _p(WIDE, WIDE, [], reason="eod_liquidate")["route"] == "market:eod"
    assert _p(WIDE, WIDE, [], reason=None)["route"] == "market:unknown_reason"


def test_limit_rounds_up():
    assert S.cent_up(5.065) == 5.07 and S.cent_up(10.015) == 10.02 and S.cent_up(10.03) == 10.03


def test_trade_through_fill():
    r = _p(WIDE, WIDE + [(20.0, 10.0, 10.06)], [(3.0, 10.04)])
    assert r["outcome"] == "filled" and r["sim_px"] == 10.03 and r["delta_bp"] == pytest.approx(30.0)


def test_bid_cross_needs_the_bid_at_the_limit():
    sip = WIDE + [(13.5, 10.00, 10.06)]
    r = _p(WIDE, sip, [(3.0, 10.04)], fill_rule="bid_cross")
    assert r["outcome"] == "timeout" and r["sim_px"] == 10.00
    r = _p(WIDE, sip + [(4.0, 10.03, 10.05)], [], fill_rule="bid_cross")
    assert r["outcome"] == "filled"


def test_bail_acts_one_tick_later_at_the_sip_bid():
    iex = WIDE + [(2.0, 9.99, 10.05)]
    sip = WIDE + [(2.0, 9.99, 10.05), (4.5, 9.97, 10.03)]
    r = _p(iex, sip, [(6.0, 10.04)])
    assert r["outcome"] == "bail" and r["sim_px"] == 9.97  # SIP bid at 2.0 + 3 s


def test_timeout_one_tick_after_collar():
    sip = WIDE + [(12.5, 9.98, 10.04)]
    r = _p(WIDE, sip, [], collar_sec=10.0)
    assert r["outcome"] == "timeout" and r["sim_px"] == 9.98


def test_wait_never_runs_past_the_close():
    t0 = _t("15:59") + 55
    q = [(t0 - 1, 10.0, 10.06), (t0 + 30, 9.0, 9.06)]
    r = S.simulate_policy_exit(t0, q, [(t0 + 8, 10.05)], q, 10.0, reason="local_trail")
    assert r["outcome"] == "timeout" and r["sim_px"] == 10.0  # capped at 16:00:00, not filled by the 16:00:03 print


def test_pair_sells_flags_and_t0():
    rows = [
        {"event": "submit", "action": "SELL_LIMIT", "symbol": "X", "order_id": "L", "ts": 100.0},
        {"event": "submit", "action": "SELL", "symbol": "X", "order_id": "a", "ts": 110.0},
        {"event": "fill", "side": "sell", "order_id": "a", "filled_avg_price": 9.9, "filled_qty": 3,
         "submitted_at": "2026-11-04 15:00:00+00:00", "filled_at": "2026-11-04 15:00:20+00:00"},
        {"event": "submit", "action": "SELL", "symbol": "TEST", "order_id": "j", "ts": 5.0},
        {"event": "fill", "side": "sell", "order_id": "j", "filled_avg_price": 1, "filled_qty": 1},
    ]
    p = S.pair_sells(rows)
    assert len(p) == 1 and p[0]["after_limit"] and p[0]["fill_lag"] == pytest.approx(20.0)
    assert p[0]["t0"] == datetime(2026, 11, 4, 15, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    assert not S.eligible(p[0])


def test_limit_sells_window_ends_at_fallback():
    rows = [
        {"event": "submit", "action": "SELL_LIMIT", "symbol": "X", "order_id": "L", "ts": 100.0, "note": "exit_test mid lmt=10.03"},
        {"event": "submit", "action": "SELL", "symbol": "X", "order_id": "m", "ts": 108.0},
    ]
    o = S.limit_sells(rows)[0]
    assert o["limit"] == 10.03 and o["t_end"] == 108.0 and o["filled"] is False


def test_predict_limit_fill_rules():
    q = [(1.0, 10.00, 10.06), (5.0, 10.03, 10.06)]
    assert S.predict_limit_fill(0, 10, 10.03, q, [(2.0, 10.04)], "trade_through")
    assert S.predict_limit_fill(0, 10, 10.03, q, [], "bid_cross")
    assert not S.predict_limit_fill(0, 4, 10.03, q, [], "bid_cross")


def test_reason_for_exit_nearest():
    oc = [{"symbol": "X", "exit_time": 100.0, "close_reason": "local_trail"},
          {"symbol": "X", "exit_time": 112.0, "close_reason": "dead_trade"},
          {"symbol": "Y", "exit_time": 101.0, "close_reason": "stale_data"}]
    assert S.reason_for_exit(oc, "X", 109.0) == "dead_trade"
    assert S.reason_for_exit(oc, "X", 200.0) is None


def test_verdict_rules():
    from tools.studies import midpoint_policy_verdict as V
    K = "p1_bid_cross_c10"

    def rows(day_vals):
        out = []
        for day, vals in day_vals.items():
            for i, v in enumerate(vals):
                out.append({"day": day, "after_limit": False, "fill_lag": 1.0,
                            K: {"delta_bp": v, "delta2_bp": v, "route": "passive" if i % 2 == 0 else "market:tight"}})
        return out
    days = [f"2026-09-{d:02d}" for d in (16, 17, 18, 21, 22, 23, 24, 25, 28, 29, 30)] + ["2026-10-01", "2026-10-02"]
    days += [f"2026-10-{d:02d}" for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19, 20, 21, 22, 23)]
    good = {d: [2.0 + (i % 3), 1.0, 0.0, 3.0 - (i % 2), 1.5] * 4 for i, d in enumerate(days)}
    assert V.verdict(rows(good), K)["verdict"] == "PASS"
    bad = {k: [-x for x in v] for k, v in good.items()}
    assert V.verdict(rows(bad), K)["verdict"] == "FAIL"
    assert V.verdict(rows({"2026-09-16": [1.0] * 10}), K)["verdict"] == "VOID"
    excluded = rows(good)
    for r in excluded:
        r["after_limit"] = True
    assert V.verdict(excluded, K)["verdict"] == "VOID"


def test_pick_rule_calibration():
    from tools.studies import midpoint_policy_verdict as V
    c = {"summary": {"actual_fill_rate": 0.34, "trade_through": {"pred_fill_rate": 0.80, "kappa": 0.5},
                     "bid_cross": {"pred_fill_rate": 0.40, "kappa": 0.5}}}
    assert V.pick_rule(c)[0] == "bid_cross"
    c["summary"]["bid_cross"]["pred_fill_rate"] = 0.60
    assert V.pick_rule(c)[0] is None


def test_marketable_limit_fills_at_the_sip_bid():
    # stale IEX 9.90/10.00 -> limit 9.95, real SIP bid 10.00: fills at the bid at once
    r = _p([(-1.0, 9.90, 10.00)], [(-1.0, 10.00, 10.04)], [(3.0, 10.02)], fill=10.0)
    assert r["outcome"] == "filled_marketable" and r["sim_px"] == 10.00 and r["delta_bp"] == 0.0


def test_trade_through_fill_never_below_the_bid():
    sip = WIDE + [(2.0, 10.05, 10.07)]
    r = _p(WIDE, sip, [(3.0, 10.06)])
    assert r["outcome"] == "filled" and r["sim_px"] == 10.05


def test_kappa_and_pick_rule_gate():
    from tools.studies import midpoint_policy_verdict as V
    assert S.cohen_kappa([True, False, True, False], [True, False, True, False]) == pytest.approx(1.0)
    c = {"summary": {"actual_fill_rate": 0.34, "trade_through": {"pred_fill_rate": 0.80, "kappa": 0.1},
                     "bid_cross": {"pred_fill_rate": 0.36, "kappa": 0.1}}}
    assert V.pick_rule(c)[0] is None  # right rate, no agreement
    c["summary"]["bid_cross"]["kappa"] = 0.45
    assert V.pick_rule(c)[0] == "bid_cross"
