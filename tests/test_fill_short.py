"""fill_short: the mirror, the short through the desk's own exits, SSR, fill loading and the word. No market data."""
import json
import os
import random
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import call_exits as CE  # noqa: E402
import fill_short as F  # noqa: E402

ET = ZoneInfo("America/New_York")


def _t(hh, mm, ss=0, day=20):
    return datetime(2026, 10, day, hh, mm, ss, tzinfo=ET).timestamp()


def _cfg():
    import config
    return config.load_config_ro()


def _run(prices, monkeypatch, short=True):
    ts = _t(10, 0)
    tr = [(ts + 1 + 2 * i, p, 200) for i, p in enumerate(prices)]
    monkeypatch.setattr(CE, "cached_trades", lambda cl, sym, key, click, h: [t for t in tr if t[0] <= h])
    end = _t(16, 0)
    res, _ = F.run_side(CE, "X", "k", ts, None, [], _cfg(), end, 5.0, "asis", short=short)
    return res


def test_mirror_is_an_involution_and_swaps_high_low():
    e = 20.0
    tr = [(1.0, 21.0, 100), (2.0, 19.0, 300)]
    assert F.mirror_trades(F.mirror_trades(tr, e), e) == [(1.0, 21.0, 100), (2.0, 19.0, 300)]
    assert abs(F.mirror_trades(tr, e)[0][1] - 400 / 21) < 1e-9
    (t, hi, lo, c), = F.mirror_bars([(0.0, 22.0, 18.0, 20.0)], e)
    assert hi == 400 / 18 and lo == 400 / 22 and c == 20.0 and hi > lo


def test_short_return_sign():
    assert F.short_bp(20.0, 19.0) == 500.0
    assert F.short_bp(20.0, 21.0) == -500.0


def test_planted_fall_makes_the_short_money(monkeypatch):
    # The real price falls 20 -> 18 after the fill: the mirror rises, the desk's exits hold it, the short wins.
    prices = [20.0] * 5 + [20.0 - 0.01 * k for k in range(1, 201)]
    res = _run(prices, monkeypatch, short=True)
    assert res["status"] == "final" and res["gross_bp"] > 0
    long_ = _run(prices, monkeypatch, short=False)
    assert long_["status"] == "final" and long_["gross_bp"] < 0


def test_planted_rise_stops_the_short_out(monkeypatch):
    prices = [20.0] * 5 + [20.0 + 0.01 * k for k in range(1, 201)]
    res = _run(prices, monkeypatch, short=True)
    assert res["status"] == "final" and res["gross_bp"] < 0
    assert res["reason"] in ("local_trail", "dead_trade")
    assert res["exit"] > 20.0          # the cover is a real price above the entry


def test_no_print_within_the_wait_is_no_fill(monkeypatch):
    ts = _t(10, 0)
    tr = [(ts + 500, 20.0, 200)]
    monkeypatch.setattr(CE, "cached_trades", lambda cl, sym, key, click, h: [t for t in tr if t[0] <= h])
    res, _ = F.run_side(CE, "X", "k", ts, None, [], _cfg(), _t(16, 0), 5.0, "asis", short=True)
    assert res["status"] == "no_fill"


def test_ssr_today_and_carried_from_yesterday():
    assert F.ssr_on(8.9, 10.0, None, None)
    assert not F.ssr_on(9.1, 10.0, 9.5, 10.0)
    assert F.ssr_on(9.5, 10.0, 8.9, 10.0)
    daily = [("2026-10-14", 9.8, 10.0), ("2026-10-15", 8.5, 9.0)]
    assert F.ssr_for(daily, "2026-10-16", 9.0)          # yesterday fell 15% from 10.0
    assert not F.ssr_for(daily[:1], "2026-10-15", 9.5)


def test_tiers():
    assert [F.tier(x) for x in (25, 10, 7, 2.5, 1.5)] == ["10+", "10+", "5-10", "2-5", "<2"]


def test_load_fills_dedupes_orders_and_keeps_the_window(tmp_path):
    day = "2026-10-15"
    led = tmp_path / "fills"
    led.mkdir()
    snap = tmp_path / "snaps" / day
    snap.mkdir(parents=True)
    t0 = datetime(2026, 10, 15, 10, 0, tzinfo=ET).timestamp()
    rows = [{"event": "fill", "side": "buy", "symbol": "abc", "order_id": "o1", "ts": t0 + 5, "filled_avg_price": 12.0},
            {"event": "fill", "side": "buy", "symbol": "ABC", "order_id": "o1", "ts": t0, "filled_avg_price": 12.0},
            {"event": "fill", "side": "sell", "symbol": "ABC", "order_id": "o2", "ts": t0 + 60, "filled_avg_price": 12.1},
            {"event": "fill", "side": "buy", "symbol": "LATE", "order_id": "o3",
             "ts": datetime(2026, 10, 15, 15, 46, tzinfo=ET).timestamp(), "filled_avg_price": 15.0},
            {"event": "fill", "side": "buy", "symbol": "PRE", "order_id": "o4",
             "ts": datetime(2026, 10, 15, 9, 0, tzinfo=ET).timestamp(), "filled_avg_price": 15.0}]
    (led / f"{day}.jsonl").write_text("\n".join(json.dumps(r) for r in rows[:2]) + "\nnot json\n")
    (snap / "fills.jsonl").write_text("\n".join(json.dumps(r) for r in rows[1:]))
    out, n = F.load_fills([day], [led, tmp_path / "snaps"])
    assert [(f["symbol"], f["ts"]) for f in out] == [("ABC", t0)]
    assert n["outside_window"] == 2


def _rows(per_day, days=16, n=25, sd=30.0, seed=1):
    rng = random.Random(seed)
    out = []
    for i in range(days):
        d = f"2026-09-{23 + i}" if i < 8 else f"2026-10-{5 + i - 8:02d}"
        for _ in range(n):
            x = per_day(i) + rng.gauss(0, sd)
            out.append({"day": d, "net": x, "slip": x - 3.0})
    return out


def test_word_planted_edge_passes_and_noise_fails():
    assert F.word(_rows(lambda i: 8.0), 0.0)[0] == "PASS"
    assert F.word(_rows(lambda i: 0.0), 0.0)[0] == "FAIL"


def test_word_a_few_big_days_do_not_pass():
    w, info = F.word(_rows(lambda i: 300.0 if i in (2, 9, 12) else -10.0, sd=5.0), 0.0)
    assert w != "PASS"


def test_word_refusals():
    assert F.word(_rows(lambda i: 8.0), 0.30)[0].startswith("NOT READ")
    assert F.word(_rows(lambda i: 8.0, days=10), 0.0)[0] == "UNDERPOWERED"
    assert F.word(_rows(lambda i: 8.0, n=5), 0.0)[0] == "UNDERPOWERED"


def _hold_case(monkeypatch, answer):
    """Real price rises 0.75% in 20 s (through the short's first stop), then falls 2.5%. Minute bars cover it all."""
    import ai_positions as cp
    from tools import sell_double_check as sdc
    t0 = _t(10, 0)

    def real(t):
        if t < 0:
            return 20.0 - t / 3600 * 3.0
        if t < 20:
            return 20.0 + 0.15 * t / 20
        return 20.15 - 0.65 * min(1.0, (t - 20) / 1800)
    bars = []
    for m in range(-240, 60):
        ps = [real(60 * m + k) for k in range(0, 60, 5)]
        bars.append((t0 + 60 * m, max(ps) + 0.005, min(ps) - 0.005, ps[-1]))
    tr = [(t0 + 1 + 2 * i, round(real(1 + 2 * i), 2), 200) for i in range(1800)]
    monkeypatch.setattr(CE, "cached_trades", lambda cl, sym, key, click, h: [t for t in tr if t[0] <= h])
    seen = []

    def fake_eval(sym, b, px, **kw):
        seen.append((b, px))
        return {"sell": answer, "reason": "planted"}
    monkeypatch.setattr(sdc, "evaluate_symbol", fake_eval)
    monkeypatch.setattr(cp, "_dc_note", lambda *_: None)
    res, _ = F.run_side(CE, "X", "k", t0, None, bars, _cfg(), _t(16, 0), 5.0, "asis", short=True)
    return res, seen, bars


def test_the_trend_hold_reads_mirrored_bars_and_its_hold_keeps_the_short(monkeypatch):
    sold, seen, bars = _hold_case(monkeypatch, "yes")
    assert seen, "the trend hold was never asked"
    b, px = seen[0]
    e = sold["entry"]
    last = next(x for x in bars if x[0] == b[0][-1])
    assert abs(b[1][-1] - e * e / last[2]) < 1e-9                 # mirrored high = E^2 / real low
    assert abs(b[2][-1] - e * e / last[1]) < 1e-9                 # mirrored low = E^2 / real high
    assert max(b[0]) + 60 <= sold["exit_ts"]                     # completed bars only
    assert px < e                                                # a real rise is a fall in the mirror
    assert sold["gross_bp"] < 0 and sold["exit_ts"] - _t(10, 0) < 120
    held, _, _ = _hold_case(monkeypatch, "no")
    assert held["exit_ts"] > sold["exit_ts"] + 300 and held["gross_bp"] > sold["gross_bp"]


def test_double_check_bars_are_point_in_time(monkeypatch):
    """double_check_sell / no_progress read _symbol_bars_for_double_check; simulate patches it to completed bars."""
    import ai_positions as cp
    t0 = _t(10, 0)
    bars = [(t0 - 3600 + 60 * i, 20.1, 19.9, 20.0) for i in range(200)]
    got = []
    orig = CE.bars_upto
    monkeypatch.setattr(CE, "bars_upto", lambda b, now: got.append(now) or orig(b, now))
    tr = [(t0 + 1 + 2 * i, 20.0 - 0.01 * i, 200) for i in range(100)]
    CE.simulate("X", t0, tr, bars, _cfg(), t0 + 600)
    assert cp._symbol_bars_for_double_check.__module__ == "ai_positions"     # restored
    seen = {}

    def probe(*a, **k):
        seen["v"] = cp._symbol_bars_for_double_check("X", 0.0)
        return None
    monkeypatch.setattr(cp, "initial_local_stop", lambda *a, **k: (probe(), 19.9)[1])
    CE.simulate("X", t0, tr, bars, _cfg(), t0 + 600)
    v = seen["v"]
    assert v is not None and max(v[0]) + 60 <= t0 + 1 + 1e-6        # nothing after the entry print


def test_short_t1_blend():
    # Mirror target_1 at 21.0 for an entry at 20 = real 400/21 = 19.048: half covered there, half at 19.5.
    res = {"t1_hit": True, "t1_px": 21.0, "scale": 0.5}
    got = F.short_gross(20.0, 19.5, res)
    want = 0.5 * F.short_bp(20.0, 400 / 21) + 0.5 * F.short_bp(20.0, 19.5)
    assert abs(got - want) < 1e-9
    assert F.short_gross(20.0, 19.5, {"t1_hit": False}) == F.short_bp(20.0, 19.5)


def test_call_exits_session_end_keeps_the_t1_blend():
    res = {"status": "open", "entry": 10.0, "mark": 10.0, "t1_hit": True, "t1_px": 10.5, "scale": 0.5}
    assert CE.finalize_at_session_end(res, 0.0)["gross_bp"] == 250.0


def test_word_needs_days_in_each_half_and_reads_fetch_errors():
    rows = [r for r in _rows(lambda i: 8.0, days=16) if r["day"] > F.A_END or r["day"] <= "2026-09-25"]
    assert F.word(rows, 0.0)[0] == "UNDERPOWERED"           # 3 days in half A
    assert F.word(_rows(lambda i: 8.0), 0.0, fetch_error_share=0.06)[0].startswith("NOT READ")


def test_load_replay_reads_every_open_and_flags_missing_days(tmp_path):
    t = datetime(2026, 10, 6, 10, 0, tzinfo=ET).timestamp()
    (tmp_path / "2026-10-06_floor2.json").write_text(json.dumps({"closed": [
        {"symbol": "abc", "entry_ts": t, "entry_px": 3.1, "synthetic": True},
        {"symbol": "XYZ", "entry_ts": t + 60, "entry_px": 12.0}]}))
    fills, n, failed = F.load_replay(tmp_path, days=("2026-10-06",))
    assert [(f["symbol"], f["synthetic"]) for f in fills] == [("ABC", True), ("XYZ", False)]
    assert not failed and n["synthetic"] == 1
    assert F.load_replay(tmp_path, days=("2026-10-06", "2026-10-07"))[2]           # a missing day
    (tmp_path / "failures.txt").write_text("FAILED 2026-10-06 floor2\n")
    assert F.load_replay(tmp_path, days=("2026-10-06",))[2]


def _replay_rows(net, tc, days=9, n=15):
    out = []
    ds = list(F.REPLAY_DAYS)[:days]
    rng = random.Random(4)
    for d in ds:
        for _ in range(n):
            x = net + rng.gauss(0, 20)
            out.append({"day": d, "net": x, "slip": x - 2, "tiercost": x + tc - net})
    return out


def test_floor2_word_needs_the_tier_cost_line_too():
    kw = dict(a_end=F.REPLAY_A_END, extra_key="tiercost", **F.REPLAY_MIN)
    assert F.word(_replay_rows(15.0, 10.0), 0.0, **kw)[0] == "PASS"
    assert F.word(_replay_rows(15.0, -30.0), 0.0, **kw)[0] != "PASS"
    assert F.word(_replay_rows(15.0, 10.0), 0.0, replay_failed=True, **kw)[0].startswith("NOT READ")
    assert F.word(_replay_rows(15.0, 10.0, n=10), 0.0, **kw)[0] == "UNDERPOWERED"      # 90 trades
