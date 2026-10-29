"""move_to_cost_live: row cleaning, fill matching, the range window, power and verdict wiring."""
import datetime
import sys
import zoneinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import move_to_cost_live as L  # noqa: E402

ET = zoneinfo.ZoneInfo("America/New_York")


def ts(day, h, m, s=0):
    return datetime.datetime.fromisoformat(f"{day} {h:02d}:{m:02d}:{s:02d}").replace(tzinfo=ET).timestamp()


def row(sym="AAA", day="2026-09-15", h=10, m=30, hold=120, **kw):
    t = ts(day, h, m)
    base = {"symbol": sym, "entry_time": t, "exit_time": t + hold, "entry_price": 50.0, "exit_price": 50.1,
            "total_qty": 8, "paper": True, "entry_path": "watch", "edge_mode": "continuation", "scaled_out": False}
    base.update(kw)
    return base


def test_clean_rows_applies_every_drop():
    raw = [row(), row(),                                   # exact duplicate
           row(sym="BBB", paper=None), row(sym="CCC", entry_path="adopted"), row(sym="DDD", scaled_out=True),
           row(sym="EEE", edge_mode="exhaustion_scalp"), row(sym="FFF", exit_price=None),
           row(sym="GGG", day="2026-10-01"),               # exploration window
           row(sym="HHH", m=30), row(sym="HHH", m=31)]     # overlapping same-symbol pair: both out
    keep, drops = L.clean_rows(raw)
    assert [r["symbol"] for r in keep] == ["AAA"]
    assert drops == {"duplicate": 1, "not_watch_paper": 2, "scaled_out": 1, "exhaustion_scalp": 1,
                     "no_exit_price_or_qty": 1, "outside_periods": 1, "overlap_pair": 2}


def test_match_fills_quantity_weighted_and_strict():
    r = {"symbol": "AAA", "entry_time": 1000.0, "exit_time": 1300.0, "qty": 8}
    fills = [{"symbol": "AAA", "side": "buy", "qty": 5, "price": 50.0, "t": 1001.0},
             {"symbol": "AAA", "side": "buy", "qty": 3, "price": 50.08, "t": 1002.0},
             {"symbol": "AAA", "side": "sell", "qty": 8, "price": 50.2, "t": 1250.0},
             {"symbol": "BBB", "side": "buy", "qty": 8, "price": 9.0, "t": 1001.0}]
    buy, sell = L.match_fills(r, fills)
    assert abs(buy - (5 * 50 + 3 * 50.08) / 8) < 1e-9 and sell == 50.2
    assert L.match_fills({**r, "qty": 9}, fills) is None                         # qty mismatch
    assert L.match_fills({**r, "entry_time": 500.0}, fills) is None               # no buy in the window


def test_range_window_and_early_entries():
    bars = [(m, 0, 50.0 + (0.5 if m == 620 else 0.0), 49.9, 50.0, 100) for m in range(570, 700)]
    assert L.range_pct(bars, ts("2026-09-10", 9, 45)) is None                      # before 09:51
    r = L.range_pct(bars, ts("2026-09-10", 10, 30))                                 # m = 630: [609, 629]
    assert abs(r - (50.5 / 49.9 - 1) * 100) < 1e-9


def test_period_and_power():
    assert L.period("2026-09-11") == "P1" and L.period("2026-09-10") is None and L.period("2026-10-01") is None and L.period("2026-10-29") == "P2"
    assert L.powered({"low_n": 60, "both_sessions": 15, "mde": 8.0})
    assert not L.powered({"low_n": 59, "both_sessions": 15, "mde": 8.0})
    assert not L.powered({"low_n": 80, "both_sessions": 20, "mde": 8.5})


def _scored(days=20, n_low=4, n_rest=10, low=-12.0, rest=-2.0, period=lambda i: "P1" if i < 12 else "P2"):
    rows = []
    for d in range(days):
        for i in range(n_low):
            rows.append({"day": f"d{d:02d}", "period": period(d), "status": "ok", "group": "LOW",
                         "net": low + (i - 1.5) * 2 + 0.3 * d, "spread": 5.0, "rng": 0.3})
        for i in range(n_rest):
            # REST includes narrow-range names with tiny spreads (still >= 8 spreads of movement), as in real data
            rows.append({"day": f"d{d:02d}", "period": period(d), "status": "ok", "group": "REST",
                         "net": rest + (i % 3) - 1, "spread": 1.0 if i < 4 else 5.0 + (i % 4),
                         "rng": 0.1 if i < 4 else 0.8 + 0.1 * i})
    return rows


def test_verdict_holds_and_fails():
    """A pure low-range artifact (LOW == the lowest-range trades) cannot pass the range-only control; here LOW is
    distinct from the lowest-range trades, so a real LOW effect holds."""
    v = L.verdict(_scored())
    assert v["holds"], v["checks"]
    v2 = L.verdict(_scored(low=-2.0))
    assert not v2["holds"] and not v2["checks"]["d"]
