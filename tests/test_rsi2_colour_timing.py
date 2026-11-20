import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

import rsi2_colour_timing as rc  # noqa: E402


def _walk(n, seed, px=50.0, sd=0.002):
    rng = np.random.default_rng(seed)
    return px * np.exp(np.cumsum(rng.normal(0, sd, n)))


def test_rsi_matches_live_series_and_skips_gaps():
    from ai_entry_watch import cm_rsi_series
    c = _walk(300, 1)
    rsi, _, _ = rc.rsi_state(c)
    live = cm_rsi_series(list(c), 2)
    assert np.allclose(rsi[1:], live, atol=1e-9)
    g = c.copy()
    g[[50, 51, 120]] = np.nan                       # live skips missing minutes
    rsi_g, _, _ = rc.rsi_state(g)
    live_g = cm_rsi_series([x for x in g if np.isfinite(x)], 2)
    assert np.allclose(rsi_g[np.isfinite(g)][1:], live_g, atol=1e-9) and np.isnan(rsi_g[50])


def test_red_trigger_matches_live_and_turns_the_bar_red():
    from ai_entry_watch import rsi2_trigger_prices
    for seed in range(5):
        c = _walk(80, seed + 10)
        _, U, D = rc.rsi_state(c)
        P = rc.red_trigger(c[-1], U[-1], D[-1])
        live_red, _ = rsi2_trigger_prices(list(c))
        assert live_red - 1e-4 <= P <= live_red + 0.01 + 1e-9          # rounded UP to the cent
        rsi, _, _ = rc.rsi_state(np.append(c, P))
        assert rsi[-1] >= 90


def test_stop_fill_needs_a_tick_through():
    assert np.isnan(rc.stop_fill(10.00, 9.95, 10.00))
    assert abs(rc.stop_fill(10.00, 9.95, 10.01) - 10.01) < 1e-9
    assert abs(rc.stop_fill(10.00, 10.05, 10.10) - 10.06) < 1e-9   # gap through: open + 1 tick


def _day(closes_list, pc_ratio=0.95, warm=None):
    """Synthetic day dict in cache shape; o[t] = c[t-1], h/l = max/min(o, c)."""
    U = len(closes_list)
    o, h, l, c = (np.zeros((U, 390)) for _ in range(4))
    cols = {k: [] for k in ("rsi", "U", "D", "red_plain", "green_plain", "red_strict", "green_strict")}
    for k, cl in enumerate(closes_list):
        c[k] = cl
        o[k] = np.concatenate(([cl[0]], cl[:-1]))
        h[k], l[k] = np.maximum(o[k], c[k]) * 1.0005, np.minimum(o[k], c[k]) * 0.9995
        w = warm[k] if warm is not None else np.full(rc.WARM, np.nan)
        col = rc.colours(np.concatenate([w, cl]), rc.WARM)
        for key in cols:
            cols[key].append(col[key])
    d = {"syms": [f"S{k}" for k in range(U)], "o": o, "h": h, "l": l, "c": c,
         "pc": np.array([cl[0] * pc_ratio for cl in closes_list]), "day": "2026-05-01",
         "spy_o": np.full(390, 100.0), "spy_c": np.full(390, 100.0)}
    d.update({k: np.array(v) for k, v in cols.items()})
    return d


def test_red_start_fires_on_cross_and_exits_next_open():
    cl = np.full(390, 50.0)
    cl[:20] = 50.0 - np.arange(20) * 0.01           # drift down (RSI low)
    cl[20:] = cl[19]
    cl[30], cl[31], cl[32] = cl[29] + 0.20, cl[29] + 0.30, cl[29] + 0.10   # up, up (red), down (exit)
    cl[33:] = cl[32]
    d = _day([cl])
    rsi = d["rsi"][0]
    t0 = [t for t in range(rc.T_LO, rc.T_HI + 1) if rsi[t] >= 90 and rsi[t - 1] < 90]
    tr, _ = rc.trades_for_name("RED_START", d, 0, "plain")
    assert tr and tr[0]["t"] == t0[0]
    r = tr[0]
    s = next(s for s in range(r["t"] + 1, r["t"] + 16) if not d["red_plain"][0][s])
    assert r["kind"] == "open" and r["ebar"] == s + 1 and abs(r["entry"] - d["o"][0][r["t"] + 1]) < 1e-12


def test_cap_sells_close_of_t_plus_15():
    cl = 50.0 + np.arange(390) * 0.01               # every bar up: red never ends
    d = _day([cl])
    tr, _ = rc.trades_for_name("RED_START", d, 0, "plain")
    # first red-start is before the window; with red continuous there is no new cross in the window -> no trade
    assert not tr
    kind, ebar, px = rc.exit_for("RED_START", d["red_plain"][0], 41, 40, d["o"][0], d["c"][0])
    assert kind == "cap" and ebar == 55 and px == d["c"][0][55]


def test_green_end_fires_on_first_bar_back_above_10():
    cl = np.full(390, 50.0)
    cl[25:29] = 50.0 - np.arange(1, 5) * 0.05       # four down bars -> RSI <= 10
    cl[29:] = cl[28] + 0.10                          # first up bar ends green
    d = _day([cl])
    tr, _ = rc.trades_for_name("GREEN_END", d, 0, "plain")
    g = d["green_plain"][0]
    first = next(t for t in range(rc.T_LO, rc.T_HI + 1) if g[t - 1] and not g[t])
    assert tr and tr[0]["t"] == first == 29


def _synthetic_days(n_days, n_names, plant_bp, seed):
    """Random-walk days; plant_bp > 0 adds that much per bar for 3 bars after every red start (built bar by bar)."""
    rng = np.random.default_rng(seed)
    days = []
    for di in range(n_days):
        cls = []
        for _ in range(n_names):
            c = np.empty(390)
            c[0] = 50.0
            up = down = None
            prev = c[0]
            boost, prev_rsi = 0, 50.0
            for t in range(1, 390):
                r = rng.normal(0, 0.002) + (plant_bp / 1e4 if boost > 0 else 0.0)
                boost = max(0, boost - 1)
                c[t] = prev * math.exp(r)
                dlt = c[t] - prev
                g, lo = max(dlt, 0.0), max(-dlt, 0.0)
                up, down = (g, lo) if up is None else (0.5 * g + 0.5 * up, 0.5 * lo + 0.5 * down)
                rsi = 100.0 if down == 0 else (0.0 if up == 0 else 100 - 100 / (1 + up / down))
                if rsi >= 90 and prev_rsi < 90:
                    boost = 3 if t + 1 < 390 else 0
                prev_rsi, prev = rsi, c[t]
            cls.append(c)
        d = _day(cls)
        d["day"] = f"2026-0{5 if di < n_days // 2 else 8}-{(di % 28) + 1:02d}"
        days.append(d)
    return days


def _edge(days, rule="RED_START"):
    rows = []
    for d in days:
        rr, _ = rc.day_rows(rule, d, "plain")
        rows += rr
    return rc.edge_verdict(rows)


def test_null_canary_no_edge_and_planted_edge_found():
    v0, st0 = _edge(_synthetic_days(16, 12, 0.0, 7))
    assert v0 == "NO EDGE", st0
    v1, st1 = _edge(_synthetic_days(16, 12, 15.0, 8))
    assert v1 == "EDGE", st1
