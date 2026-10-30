"""marketable_limit_hist: row filter, slip / block, re-entry model, verdict wiring."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import marketable_limit_hist as M  # noqa: E402

T0 = M.MH.ET  # noqa: F841


def ts(day, h, m, s=0):
    import datetime as dt
    return dt.datetime.fromisoformat(f"{day} {h:02d}:{m:02d}:{s:02d}").replace(tzinfo=M.MH.ET).timestamp()


def raw(sym="AAA", day="2026-10-05", m=30, **kw):
    t = ts(day, 10, m)
    r = {"symbol": sym, "entry_time": t, "exit_time": t + 300, "entry_price": 50.0, "exit_price": 50.1, "total_qty": 8,
         "paper": True, "entry_path": "watch", "edge_mode": "continuation", "scaled_out": False,
         "features": {"ask": 50.0, "bid": 49.98}}
    r.update(kw)
    return r


def test_rows_filter_keeps_window_and_drops_g2_arms():
    rs, drops = M.rows_from([raw(), raw(sym="BBB", entry_test={"arm": "bid"}), raw(sym="CCC", entry_test={"arm": "ask"}),
                             raw(sym="DDD", day="2026-09-26"), raw(sym="EEE", features={}), raw(sym="FFF", day="2026-09-10")])
    assert sorted(r["symbol"] for r in rs) == ["AAA", "CCC", "DDD"]       # 9/26-10/13 is kept here
    assert drops["g2_non_ask_arm"] == 1 and drops["no_features_ask"] == 1 and drops["outside_periods"] == 1
    assert rs[0]["ref"] == 50.0 and rs[0]["bid"] == 49.98


def test_slip_block_and_reentry():
    r = M.score_row({"day": "2026-10-05", "buy": 50.05, "sell": 50.10, "ref": 50.0, "bid": 49.98,
                     "send": ts("2026-10-05", 10, 30, 10), "exit_time": ts("2026-10-05", 10, 40)})
    assert abs(r["slip"] - 10.0) < 1e-6 and r["blocked"] and abs(r["net"] - (50.10 / 50.05 - 1) * 1e4) < 1e-9
    bars = [[630, 0, 0, 0, 50.0, 1], [631, 0, 0, 0, 50.02, 1]]          # 10:31 bar closes at 10:32:00 >= send + 60
    n = M.reentry_net(r, bars)
    px = 50.02 * (1 + (50.05 - 49.98) / (2 * 50.05))
    assert abs(n - (50.10 / px - 1) * 1e4) < 1e-9
    early = dict(r, exit_time=r["send"] + 30)
    assert M.reentry_net(early, bars) is None


def _rows(days=30, blocked_net=-15.0, kept_net=0.0, seed=2):
    rnd = random.Random(seed)
    rows = []
    for k in range(days):
        day = f"2026-{9 + (k >= 15):02d}-{(11 + k) if k < 15 else (1 + k - 15):02d}"
        for i in range(12):
            b = i < 3
            rows.append({"day": day, "net": (blocked_net if b else kept_net) + rnd.gauss(0, 3), "blocked": b,
                         "slip": 9.0 if b else 1.0, "selection": -6.0 if b else 1.0,
                         "spread_proxy": 0.01 * (i % 5)})
    return rows


def test_verdict_labels():
    v = M.verdict(_rows(), lambda r: 0.0)
    assert v["checks"]["1_session_sum"] and v["checks"]["3_beats_random"] and v["checks"]["5_n_gate"]
    assert v["label"] in ("PROMISING", "SPREAD-EQUIVALENT") and not v["decomposition"]["blocked_are_runners"]
    flat = M.verdict(_rows(blocked_net=0.0), lambda r: 0.0)
    assert flat["label"] == "NOT USEFUL"
    thin = M.verdict(_rows(days=5), lambda r: 0.0)
    assert thin["label"] == "UNDERPOWERED"
