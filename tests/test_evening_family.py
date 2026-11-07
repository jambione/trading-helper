import sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import evening_family as ef


def test_vol_ratio_uses_mean_of_prior_15_and_needs_10_bars():
    v = np.full(40, 100.0); v[30] = 500.0
    assert abs(ef.vol_ratio(v, 30) - 5.0) < 1e-9
    w = v.copy(); w[16:28] = np.nan                        # only 3 prior bars present
    assert np.isnan(ef.vol_ratio(w, 30))


def test_first_true_restarts_only_at_kept_trigger():
    assert ef.first_true([10, 20, 39, 40, 70]) == [10, 40, 70]


def test_trade_and_missing_bar_skip():
    o = np.full(390, 100.0); c = np.full(390, 100.0); c[110] = 100.5
    assert abs(ef.trade(o, c, 100, 10, 6.0) - (50.0 - 6.0)) < 1e-9
    c2 = c.copy(); c2[110] = np.nan
    assert ef.trade(o, c2, 100, 10, 6.0) is None


def test_later_minutes_ignores_future_triggers():
    # a trigger at 175 (after the candidate minute 150) must NOT remove 150; a trigger at 140 (before) removes 140..150 (H=10)
    assert 150 in ef.later_minutes(100, 10, [100, 175])
    assert 150 not in ef.later_minutes(100, 10, [100, 140])
    assert min(ef.later_minutes(100, 10, [100])) == 112


def test_primary_pool_excludes_only_triggers_up_to_t_and_duplicates():
    trig = {"B": [90], "C": [110], "D": [60]}
    pool = ef.primary_pool(["A", "B", "C", "D", "E"], trig, 100, "A", exclude=("E",))
    assert "B" not in pool                      # triggered in [85, 100]
    assert "C" in pool                          # triggers at 110 is future information at t=100
    assert "D" in pool and "E" not in pool and "A" not in pool


def _rows(n_days=70, per_day=8, edge=0.0, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(n_days):
        d = f"2026-03-{1 + i // 3:02d}-{i:03d}"
        for k in range(per_day):
            ca = float(rng.normal(-6, 20))
            rows.append({"d": d, "net": ca + edge + float(rng.normal(0, 5)), "net_s": None, "ca": ca, "cb": ca + float(rng.normal(0, 3))})
    return rows


def test_planted_canary_recovers_the_planted_edge():
    rows = _rows(edge=0.0)
    assert abs(ef.canary_planted(rows, 8.0) - 8.0) < 1e-6


def test_null_rows_give_no_edge_and_a_real_edge_is_detected():
    null = ef.evaluate(_rows(edge=0.0), 2.28)
    assert null["status"] in ("FAIL", "UNDERPOWERED") and null["status"] != "EDGE"
    real = ef.evaluate(_rows(edge=6.0), 2.28)
    assert real["status"] in ("EDGE", "PROFITABLE")


def test_minute_grid_fills_flat_and_returns_offset():
    pc = np.linspace(10, 11, 200); ph = pc + 0.01; pl = pc - 0.01
    c = np.full(390, np.nan); h = c.copy(); l = c.copy()
    c[:5] = 11.0; h[:5] = 11.01; l[:5] = 10.99
    cc, hh, ll, off = ef.minute_grid(pc, ph, pl, c, h, l)
    assert off == 112 and len(cc) == 112 + 390 and not np.isnan(cc).any()
    assert cc[112 + 10] == 11.0 and hh[112 + 10] == 11.0       # missing minute carried flat


def test_presquare_requires_two_consecutive_bars_and_trigger_is_the_second():
    n = 112 + 390
    cc = np.concatenate([np.linspace(20, 30, 112 + 200), np.linspace(30, 29.5, 190)])
    hh, ll = cc + 0.02, cc - 0.02
    bars = ef.presquare_bars(cc, hh, ll, 112, 21)
    assert all(15 <= b <= 360 for b in bars)
    assert len(bars) == len(set(bars))


# ---------------- through the real scoring path ----------------
def _day(names, seed, surge=None, spy_vol=0.0003):
    """Synthetic session in the warehouse layout: SPY + names, random-walk closes, flat volume. surge = {(name): (t, jump, drift5)}."""
    rng = np.random.default_rng(seed)
    syms = ["SPY"] + list(names)
    n = len(syms)
    spy_r = rng.normal(0, spy_vol, 390)
    c = np.zeros((n, 390)); v = np.full((n, 390), 1000.0)
    for k, s in enumerate(syms):
        r = rng.normal(0, 0.0004, 390) + (spy_r if k == 0 else 0.0)
        if surge and s in surge:
            t, jump, drift = surge[s]
            r[t] += jump; v[k, t] = 8000.0
            r[t + 1:t + 6] += drift / 5
        c[k] = 50.0 * np.exp(np.cumsum(r)) if k else 400.0 * np.exp(np.cumsum(r))
    o = np.concatenate([c[:, :1], c[:, :-1]], axis=1)
    return {"syms": syms, "idx": {s: i for i, s in enumerate(syms)}, "o": o, "h": c * 1.0002, "l": c * 0.9998, "c": c, "v": v}


def test_score_day_finds_a_planted_surge_and_draws_controls_without_future_information():
    names = [f"N{i}" for i in range(25)]
    hist = [_day(names, 100 + i) for i in range(20)]
    day = _day(names, 7, surge={"N0": (100, 0.006, 0.0040), "N1": (105, 0.006, 0.0040)})
    out, st = ef.score_day(day, hist[-1], hist, names, "2026-02-02", which=("S",))
    rows = [r for r in out["S"] if r["sym"] == "N0" and r["t"] == 100]
    assert rows, "planted surge on N0 at bar 100 not found"
    r = rows[0]
    assert r["net"] > 20.0                                    # +40 bp drift planted over 5 bars, less 6 bp cost, plus noise
    assert r["ca"] is not None and r["cb"] is not None
    # N1 surges at 105 (after t=100): it is NOT excluded from N0's pool (future information), so it can serve as a control
    trig = {"N1": [105], "N0": [100]}
    assert "N1" in ef.primary_pool(names, trig, 100, "N0")


def test_null_day_has_no_planted_net_and_counts_match_the_trigger_rule():
    names = [f"N{i}" for i in range(25)]
    hist = [_day(names, 200 + i) for i in range(20)]
    day = _day(names, 9)
    out, _ = ef.score_day(day, hist[-1], hist, names, "2026-02-03", which=("S",))
    big = [r for r in out["S"] if r["net"] > 20.0]
    assert len(big) <= max(1, len(out["S"]) // 4)             # no planted drift: large nets are rare


def test_planted_edge_through_score_day_moves_the_lift_by_the_planted_amount():
    names = [f"N{i}" for i in range(25)]
    hist = [_day(names, 300 + i) for i in range(20)]
    base = _day(names, 11, surge={"N0": (100, 0.006, 0.0)})
    plant = _day(names, 11, surge={"N0": (100, 0.006, 0.0040)})      # same noise, +40 bp drift over the next 5 bars
    ob, _ = ef.score_day(base, hist[-1], hist, names, "2026-02-04", which=("S",))
    op, _ = ef.score_day(plant, hist[-1], hist, names, "2026-02-04", which=("S",))
    rb = [r for r in ob["S"] if r["sym"] == "N0" and r["t"] == 100][0]
    rp = [r for r in op["S"] if r["sym"] == "N0" and r["t"] == 100][0]
    assert abs((rp["net"] - rb["net"]) - 40.0) < 4.0
