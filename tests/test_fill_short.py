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
