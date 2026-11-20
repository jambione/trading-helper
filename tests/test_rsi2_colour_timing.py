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


def test_green_cross_trigger_and_red_validity():
    import rsi2_colour_timing as r
    # c 100, U 0.1, D 1.8 -> P10 = 100 + max(0.2 - 0.1, .01) = 100.10; S5 = mean 100.50 -> P = 100.10 (> SMA200 99)
    assert r.green_cross_trigger(100.0, 0.1, 1.8, 100.5, 99.0) == 100.10
    assert np.isnan(r.green_cross_trigger(100.0, 0.1, 1.8, 100.5, 101.0))      # P below SMA200: not valid
    assert r.stop_valid_red(50.5, 50.2, 52.0, 50.1, 50.0)
    assert not r.stop_valid_red(50.5, 50.2, 50.4, 50.1, 50.0)                  # P above SMA200
    assert r.stop_valid_red(50.5, 49.9, 52.0, 50.1, 50.0)                      # amended_5: t-1 below SMA5 (ignition) counts


def test_pine_day_aligns_and_carries_over_missing_minutes():
    import rsi2_colour_timing as r
    prev = np.linspace(100, 110, 390)
    ext = np.array([110.0, 109.0])
    today = np.full(390, np.nan)
    today[0], today[1], today[3] = 108.0, 107.0, 112.0
    out = r.pine_day(prev, ext, today)
    full = np.concatenate([prev, ext, [108.0, 107.0, 112.0]])
    rsi, _, _ = r.pine_rsi(full)
    assert abs(out["rsi_pine"][1] - rsi[-2]) < 1e-12 and abs(out["rsi_pine"][3] - rsi[-1]) < 1e-12
    assert out["rsi_pine"][2] == out["rsi_pine"][1]                             # no bar at minute 2: state carries
    assert abs(out["s200_pine"][3] - full[-200:].mean()) < 1e-9
    assert abs(out["m4_pine"][3] - full[-4:].mean()) < 1e-9


def test_green_cross_treated_as_stop_rule():
    import rsi2_colour_timing as r
    row = {"t": 50}
    assert r.qbar("GREEN_CROSS", row) == r.qbar("RED_STOP", row) == 49


def test_green_cross_exit_after_fill_bar():
    import rsi2_colour_timing as r
    red = np.zeros(390, bool)
    red[53] = True
    o = np.arange(390, dtype=float)
    c = o + 0.5
    kind, bar, px = r.exit_for("GREEN_END", red, 50, 50, o, c)      # red at 53, ends at 54 -> sell open of 55
    assert (kind, bar, px) == ("open", 55, 55.0)
    kind, bar, px = r.exit_for("GREEN_END", np.zeros(390, bool), 50, 50, o, c)
    assert (kind, bar, px) == ("cap", 65, 65.5)


def test_wick_and_cross_vs_close():
    import rsi2_colour_timing as r
    o = np.full(10, 10.0); c = np.full(10, 10.0); h = np.full(10, 10.5); l = np.full(10, 9.0)
    assert abs(r.wick_pressure(o, h, l, c, 6) - (5 * 1.0 - 5 * 0.5) / (5 * 1.5)) < 1e-12
    stop = [{"day": "d", "sym": "A", "t": 5, "gross": 10.0}, {"day": "d", "sym": "B", "t": 7, "gross": -4.0}]
    close = [{"day": "d", "sym": "A", "t": 5, "gross": 6.0}]
    out = r.cross_vs_close(stop, close)
    assert out["matched"] == 1 and out["cross_minus_close_gross"] == 4.0 and out["unmatched_fills"] == 1


def test_red_trigger_exact_takes_the_binding_condition():
    import rsi2_colour_timing as r
    # RSI part binds: c 50, D 0.1, U 0.2 -> x = 50.7 -> lowest cent strictly above = 50.71; m4 49.0 -> 49.01
    assert r.red_trigger_exact(50.0, 0.2, 0.1, 49.0, 51.0)[:2] == (50.71, True)
    # off-cent threshold: x = 10.003 -> 10.01 (review fix 1)
    assert r.red_trigger_exact(10.0, 0.0, 0.003 / 9, 9.0, 20.0)[:2] == (10.01, True)
    # RSI already hot (U > 9D): threshold below c; SMA5 part binds at m4 + tick
    P, below, hot = r.red_trigger_exact(50.0, 1.0, 0.05, 50.30, 51.0)
    assert (P, below, hot) == (50.31, True, True)
    assert r.red_trigger_exact(50.0, 1.0, 0.05, 50.30, 50.2)[1] is False


def test_ratchet_exit_stop_breakeven_and_trail():
    import rsi2_colour_timing as r
    n = 20
    o = np.full(n, 100.0); h = np.full(n, 100.0); l = np.full(n, 99.9); c = np.full(n, 100.0)
    l[3] = 99.3                                        # touches entry - 0.6% = 99.4 -> exit at min(stop, open)=99.4
    assert r.ratchet_exit(o, h, l, c, 0, 100.0, False) == (99.4, 3)
    o2 = o.copy(); h2 = h.copy(); l2 = l.copy(); c2 = c.copy()
    l2[3] = 100.5; h2[2] = 101.0                       # high 101 -> trail stop 100.394; bar 4 low 100.3 exits
    l2[4] = 100.3; o2[4] = 100.5
    px, b = r.ratchet_exit(o2, h2, l2, c2, 0, 100.0, False)
    assert abs(px - 101.0 * 0.994) < 1e-9 and b == 4
    l3 = l.copy(); l3[3] = 99.9; l3[0] = 99.0          # an entry at the open watches its own bar (review fix 2)
    assert r.ratchet_exit(o, h, l3, c, 0, 100.0, True)[1] == 0
    assert r.ratchet_exit(o, h, l3, c, 0, 100.0, False)[1] != 0
