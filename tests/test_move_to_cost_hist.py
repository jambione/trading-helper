"""move_to_cost_hist: fire window, range window, the sequential draw, labels, power/void and the pass wiring."""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import move_to_cost_hist as M  # noqa: E402


def test_fire_window_keeps_591_on_but_refractory_sees_earlier(monkeypatch):
    # a fire at 588 starts the refractory, so 595 is not a fire; 610 is
    on = {588, 595, 610}
    monkeypatch.setattr(M.P, "empty_square", lambda f, s, m: m in on)
    assert M.fire_minutes({}, {}) == [610]


def test_range_window_and_min_bars():
    rows = [(m, 0, 50.0 + (1.0 if m == 600 else 0.0), 49.0, 50.0, 1) for m in range(580, 621)]
    r, n = M.range_bars(rows, 620)                         # [600, 620]: 21 bars, includes the 51.0 high
    assert n == 21 and abs(r - (51.0 / 49.0 - 1) * 100) < 1e-9
    r2, n2 = M.range_bars(rows[:5], 620)
    assert r2 is None and n2 == 0


class FakeData:
    def __init__(self, quotes):
        self.quotes, self.calls = quotes, 0

    def quote(self, sym, ts):
        self.calls += 1
        return self.quotes.get((sym, ts))


def _ev(sym, t, rng=0.5):
    return {"day": "2026-06-01", "symbol": sym, "t": t, "rng": rng, "bars": 21, "gross_bar": 1.0,
            "key": M.draw_key("2026-06-01", sym, t)}


def test_draw_skips_price_counts_missing_and_stops_at_quota():
    d = "2026-06-01"
    evs = [_ev("CHEAP", 600), _ev("GOOD", 601, rng=0.02), _ev("NOQ", 602), _ev("NORNG", 603, rng=None), _ev("GOOD2", 604, rng=2.0)]
    q = {("CHEAP", M.minute_ts(d, 601)): [5.0, 5.01],
         ("GOOD", M.minute_ts(d, 602)): [49.99, 50.01], ("GOOD", M.minute_ts(d, 612)): [50.04, 50.06],
         ("GOOD2", M.minute_ts(d, 605)): [30.0, 30.03], ("GOOD2", M.minute_ts(d, 615)): [29.97, 30.0]}
    kept, st = M.draw_session(evs, FakeData(q), per_session=60)
    by = {r["symbol"]: r for r in kept}
    assert set(by) == {"GOOD", "NOQ", "GOOD2"} and st["price_skip"] == 1 and st["range_missing"] == 1
    assert by["NOQ"]["status"] == "missing"                                      # unpriceable: kept as missing
    g = by["GOOD"]
    assert abs(g["gross_mid"] - (50.05 / 50.0 - 1) * 1e4) < 1e-6 and abs(g["spread"] - 4.0) < 1e-6
    assert abs(g["net"] - (g["gross_mid"] - g["spread"])) < 1e-9
    assert g["group"] == "LOW" and by["GOOD2"]["group"] == "REST" and M.label(0.5, 4.0) == "REST" and M.label(0.03, 4.0) == "LOW"
    kept2, _ = M.draw_session(evs, FakeData(q), per_session=1)
    assert len(kept2) == 1


def _rows(days=60, low_mid=-9.0, rest_mid=0.0, n_low=8, n_rest=40, seed=1):
    rnd = random.Random(seed)
    rows = []
    for k in range(days):
        day = f"2026-{5 + k // 12:02d}-{1 + 2 * (k % 12):02d}"                 # May..Sep: both halves
        for i in range(n_low + n_rest):
            low = i < n_low
            g = (low_mid if low else rest_mid) + rnd.gauss(0, 6)
            quiet = not low and i < n_low + 8      # REST names with the narrowest range but a tiny spread (>= 8 spreads)
            sp = 6.0 if low else (0.1 if quiet else 2.0 + (i % 5))
            rng = 0.04 if low else (0.01 if quiet else 0.2 + 0.05 * (i % 7))
            rows.append({"day": day, "symbol": f"S{i}", "status": "ok", "group": "LOW" if low else "REST",
                         "gross_mid": g, "spread": sp, "net": g - sp, "rng": rng,
                         "mid0": 50.0, "gross_bar": g})
    return rows


def test_power_void_and_verdict():
    """LOW is distinct from the lowest-range names, so a real LOW effect beats the range-only control."""
    rows = _rows()
    c = M.counts(rows)
    assert c["low_n"] == 480 and c["both_sessions"] == 60 and c["mde"] < 5 and M.powered(c) and not M.void(c)
    v = M.verdict(rows, c)
    assert v["verdict"] == "HOLDS", v["checks"]
    flat = _rows(low_mid=0.0)
    v2 = M.verdict(flat, M.counts(flat))
    assert v2["verdict"] == "NOT SUPPORTED" and not v2["checks"]["d"]


def test_void_on_missing_share():
    rows = _rows(days=5) + [{"day": "2026-05-01", "symbol": "X", "status": "missing", "group": "missing", "rng": 0.3}] * 40
    c = M.counts(rows)
    assert c["missing_share"] > M.MAX_MISSING and M.void(c)
