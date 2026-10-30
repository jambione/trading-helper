"""first_leg_delay_hist: per-delay gross/net, draw rules, verdict wiring."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import first_leg_delay_hist as F  # noqa: E402


class FakeData:
    def __init__(self, q):
        self.q, self.calls = q, 0

    def quote(self, sym, ts):
        self.calls += 1
        return self.q.get((sym, ts))


def test_measure_delays_and_draw():
    d, t = "2026-06-01", 600
    ts = lambda m: F.M.minute_ts(d, m)   # noqa: E731
    q = {("A", ts(601)): [49.99, 50.01], ("A", ts(604)): [50.04, 50.06],     # d0: 50 -> 50.05
         ("A", ts(603)): [50.09, 50.11], ("A", ts(606)): [50.09, 50.11],     # d2: 50.10 -> 50.10
         ("CHEAP", ts(601)): [5.0, 5.01]}
    evs = [{"day": d, "symbol": "A", "t": t, "key": "1"}, {"day": d, "symbol": "CHEAP", "t": t, "key": "0"},
           {"day": d, "symbol": "NOQ", "t": t, "key": "2"}]
    kept, st = F.draw_session(evs, FakeData(q))
    by = {r["symbol"]: r for r in kept}
    assert st["price_skip"] == 1 and by["NOQ"]["status"] == "missing"
    a = by["A"]
    # exit spread 0.02/50.05 = ~4 bp: half each side = ~4 bp; full-at-entry secondary = 6
    assert abs(a["gross0"] - 10.0) < 1e-6 and abs(a["spread0"] - 4.0) < 1e-6
    assert abs(a["net0"] - (10.0 - (4.0 + 0.02 / 50.05 * 1e4) / 2)) < 1e-6 and abs(a["net_full0"] - 6.0) < 1e-6
    assert abs(a["gross2"]) < 1e-9 and "gross5" not in a
    assert F.cost(2)(a) == a["gross0"] - a["gross2"] and F.cost(5)(a) is None


def _rows(net0=-4.0, g_drop=3.0, days=60):
    rnd = random.Random(3)
    rows = []
    for k in range(days):
        day = f"2026-{5 + k // 12:02d}-{1 + 2 * (k % 12):02d}"
        for i in range(30):
            g0 = net0 + 3.0 + rnd.gauss(0, 20)
            rows.append({"day": day, "symbol": f"S{i}", "t": 600 + i, "status": "ok", "gross0": g0, "spread0": 3.0,
                         "net0": g0 - 3.0, "gross2": g0 - g_drop, "net2": g0 - g_drop - 3, "gross5": g0 - g_drop,
                         "net5": g0 - g_drop - 3})
    return rows


def test_verdicts():
    v = F.verdicts(_rows())
    assert v["signal"] == "NO_EDGE_EVEN_AT_SIGNAL" and v["speed"].startswith("SPEED_MATTERS: waiting is worse")
    v2 = F.verdicts(_rows(net0=12.0, g_drop=0.0))
    assert v2["signal"] == "CONTROL_NEEDED" and v2["speed"] == "not shown"
    pos = {h: {"mean": 3.0} for h in ("A", "B")}
    assert F.verdicts(_rows(net0=12.0, g_drop=0.0), pos)["signal"] == "EDGE_AT_SIGNAL"
    neg = {"A": {"mean": 3.0}, "B": {"mean": -1.0}}
    assert F.verdicts(_rows(net0=12.0, g_drop=0.0), neg)["signal"] == "NOT_ABOVE_CONTROL"
    c = F.counts(_rows())
    assert not c["void"] and not c["underpowered"] and c["sessions"] == 60


def test_control_minute_same_hour_and_away_from_fires():
    ev = {"day": "2026-06-01", "symbol": "A", "t": 610}
    c = F.control_minute(ev, [610, 640])
    assert 600 <= c < 660 and abs(c - 610) >= 15 and abs(c - 640) >= 15
    assert F.control_minute(ev, list(range(585, 921, 10))) is None


def test_control_stage_stops_at_the_stop_time(monkeypatch):
    monkeypatch.setattr(F, "past", lambda stop_at: True)
    rows = [{"day": "2026-06-01", "symbol": "A", "t": 610, "status": "ok", "net0": 1.0}]
    assert F.control_rows(rows, {}, None, "09:15") is None
