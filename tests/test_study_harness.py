"""tools/studies/harness.py: canaries on a synthetic warehouse (null signal, planted edge, look-ahead guard, hold-out, one final read)."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import harness as H

N_SYM, N_DAY = 60, 34


def _write_day(path, rng):
    syms = np.array([f"S{i:03d}" for i in range(N_SYM)])
    idx, mins, o, h, l_, c, v = [], [], [], [], [], [], []
    for i in range(N_SYM):
        px = 50.0 * np.exp(np.cumsum(rng.normal(0, 0.0008, H.NBAR + 1)))
        for m in range(H.NBAR):
            idx.append(i); mins.append(H.M0 + m)
            o.append(px[m]); c.append(px[m + 1]); h.append(max(px[m], px[m + 1])); l_.append(min(px[m], px[m + 1]))
            v.append(1000.0 + 100 * i)
    np.savez(path, syms=syms, sym_idx=np.array(idx, np.int32), minute=np.array(mins, np.int16), o=np.array(o, np.float32),
             h=np.array(h, np.float32), l=np.array(l_, np.float32), c=np.array(c, np.float32), v=np.array(v, np.float32))


@pytest.fixture(scope="module")
def wh(tmp_path_factory):
    d = tmp_path_factory.mktemp("wh")
    rng = np.random.default_rng(3)
    for k in range(N_DAY):
        _write_day(d / f"2026-01-{k + 1:02d}.npz", rng)
    return d


CFG = {**H.DEFAULTS, "top_n": 50, "min_trades": 50, "min_days": 10}


def test_forward_return_is_next_open_to_open_after_hold():
    o = np.full((1, H.NBAR), 100.0)
    o[0, 14] = 101.0                                       # signal at 10, hold 3: buy open[11], sell open[14]
    fwd = H.forward_returns({"o": o}, 3)
    assert fwd[0, 10] == pytest.approx(100.0)
    assert np.isnan(fwd[0, H.NBAR - 4]) and not np.isnan(fwd[0, H.NBAR - 5])


def test_null_signal_has_no_edge(wh):
    r = H.run(H.random_signal(0.01, 5), 3, sorted(wh.glob("*.npz")), CFG, check=False)
    assert r["trades"] > 1000
    assert abs(r["lift"]["mean"]) < 1.0
    assert not r["edge"] and r["verdict"] == "FAIL"


def test_planted_edge_is_recovered(wh):
    fn, mut = H.planted(0.002, 10.0, 3, 11)
    r = H.run(fn, 3, sorted(wh.glob("*.npz")), CFG, check=False, mutate=mut)
    assert 9.0 <= r["lift"]["mean"] <= 11.0
    assert r["edge"] and r["verdict"] == "EDGE (component)"   # +10 gross clears the 6 bp cost but not the 10 bp stress cost
    assert 9.0 <= r["lift_later"]["mean"] <= 11.0


def test_planted_edge_above_stress_cost_is_profitable(wh):
    fn, mut = H.planted(0.002, 16.0, 3, 13)
    r = H.run(fn, 3, sorted(wh.glob("*.npz")), CFG, check=False, mutate=mut)
    assert r["verdict"] == "PROFITABLE"


def test_planted_edge_below_cost_is_a_component(wh):
    fn, mut = H.planted(0.004, 4.0, 3, 12)
    r = H.run(fn, 3, sorted(wh.glob("*.npz")), CFG, check=False, mutate=mut)
    assert r["edge"] and not r["profitable"] and r["verdict"] == "EDGE (component)"


def test_lookahead_signal_aborts(wh):
    def peek(day, prev):                                   # "buy before a bar that closes up": reads bar m+1
        c = day["c"]
        s = np.zeros(c.shape, bool)
        s[:, :-1] = c[:, 1:] > c[:, :-1] * 1.001
        return s
    with pytest.raises(SystemExit, match="LOOK-AHEAD"):
        H.run(peek, 3, sorted(wh.glob("*.npz")), CFG)


def test_causal_signal_passes_the_guard(wh):
    def causal(day, prev):
        c = day["c"]
        s = np.zeros(c.shape, bool)
        s[:, 1:] = c[:, 1:] > c[:, :-1] * 1.001
        return s
    r = H.run(causal, 3, sorted(wh.glob("*.npz")), CFG)
    assert r["lookahead_checks"] == 3 and r["trades"] > 0


def test_one_position_per_name_at_a_time():
    day = {"syms": ["A", "B"], "o": np.full((2, H.NBAR), 10.0)}
    sig = np.zeros((2, H.NBAR), bool)
    sig[0, 10:20] = True
    r = H.score_day(day, sig, 3)
    assert list(r["minute"]) == [10, 14, 18]


def test_holdout_hidden_then_one_final_read(wh, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(H, "OUT", tmp_path / "out")
    sig = tmp_path / "idea.py"
    sig.write_text("import numpy as np\nNAME='idea'\ndef signal(day, prev):\n    s=np.zeros(day['c'].shape,bool)\n"
                   "    s[:,1:]=day['c'][:,1:]>day['c'][:,:-1]*1.001\n    return s\n")
    base = [str(sig), "--warehouse", str(wh), "--top-n", "50", "--holdout", "10"]
    assert H.main(base) == 0
    assert "2026-01-24" in capsys.readouterr().out          # 34 days, last 10 hidden
    assert H.main(base + ["--final"]) == 0
    assert "2026-01-34" in capsys.readouterr().out
    assert H.main(base + ["--final"]) == 3
    assert "REFUSED" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["current_square", "early_leg_turn", "base_break", "early_square", "square_variable", "square_flat", "square_up3to6", "square_up6plus", "square_early_only", "square_late_only", "surge_unmoved", "flow_proxy", "full_green", "full_green_strong", "full_green_retest"])
def test_shipped_signals_are_causal_and_shaped(wh, name):
    fn, hold, nm = H.load_signal(Path(H.__file__).parent / "signals" / f"{name}.py")
    r = H.run(fn, hold, sorted(wh.glob("*.npz"))[:8], {**CFG, "min_trades": 0, "min_days": 0})   # raises on look-ahead
    assert nm == name and r["verdict"] != ""


def test_holds_list_reads_each_hold(wh, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(H, "OUT", tmp_path / "out")
    sig = Path(H.__file__).parent / "signals" / "current_square.py"
    assert H.main([str(sig), "--warehouse", str(wh), "--top-n", "50", "--holdout", "10", "--holds", "3,15"]) == 0
    out = capsys.readouterr().out
    assert "current_square@3m" in out and "current_square@15m" in out and "need 2.24" in out


def test_movers_filter_keeps_only_names_up_on_the_day():
    prev = {"c": np.full((2, H.NBAR), 100.0)}
    day = {"c": np.vstack([np.full(H.NBAR, 104.0), np.full(H.NBAR, 101.0)])}
    m = H.movers_mask(day, prev, 3.0)
    assert m[0].all() and not m[1].any()


def test_small_universe_pick_uses_only_the_prior_session():
    import warehouse_small as WS
    days = [f"2026-01-{k:02d}" for k in range(1, 24)]
    calm = {d: (5.05, 4.95, 5.0, 2_000_000) for d in days}                 # $5, $10M/day, 2% range
    wild = dict(calm)
    wild[days[-1]] = (6.0, 4.0, 5.0, 2_000_000)                            # 40% range on the LAST day only
    cheap = {d: (1.1, 0.9, 1.0, 9_000_000) for d in days}                  # under $2
    thin = {d: (5.5, 4.5, 5.0, 100_000) for d in days}                     # $0.5M/day
    uni = WS.pick({"CALM": calm, "WILD": wild, "CHEAP": cheap, "THIN": thin}, days)
    assert uni[days[-1]] == ["CALM", "WILD"]                               # both qualify; ranked on day -2, before the wild day
    assert "CHEAP" not in uni[days[-1]] and "THIN" not in uni[days[-1]]
    assert days[19] not in uni and days[20] in uni                         # needs 20 prior sessions


def test_model_features_do_not_read_the_future(wh):
    import ml_rank as M
    files = sorted(wh.glob("*.npz"))
    _d, day, prev = next(M.day_pairs(files[:2], CFG))
    F = M.features(day, prev)
    assert F.shape == (len(day["syms"]), H.NBAR, len(M.NAMES))
    cut = {"syms": day["syms"], **{k: day[k].copy() for k in H.KEYS}}
    for k in H.KEYS:
        cut[k][:, 201:] = np.nan
    assert np.allclose(M.features(cut, prev)[:, :201], F[:, :201], equal_nan=True)


def test_day_pick_return_is_next_open_to_the_1559_open():
    import day_pick as D
    o = np.full((1, H.NBAR), 100.0)
    o[0, D.EXIT] = 102.0
    assert D.to_close({"o": o}, D.DECIDE)[0] == pytest.approx(200.0)


def test_full_green_needs_no_wicks():
    sys.path.insert(0, str(Path(H.__file__).parent / "signals"))
    from full_green import full_green
    day = {"o": np.array([[100.0, 100.0, 100.0]]), "l": np.array([[100.0, 99.8, 100.0]]),
           "h": np.array([[100.3, 100.3, 100.5]]), "c": np.array([[100.3, 100.3, 100.3]])}
    assert list(full_green(day)[0]) == [True, False, False]        # clean; lower wick; upper wick


def test_pattern_battery_patterns_are_causal(wh):
    import pattern_battery as PB
    files = sorted(wh.glob("*.npz"))[:5]
    for name, fn in PB.PATTERNS.items():
        r = H.run(PB.wrap(fn), 3, files, {**CFG, "min_trades": 0, "min_days": 0})      # raises SystemExit on look-ahead
        assert r["verdict"], name


def test_entry_ideas_are_causal(wh):
    import entry_ideas as EI
    import pattern_battery as PB
    files = sorted(wh.glob("*.npz"))[:5]
    for name, fn in EI.IDEAS.items():
        r = H.run(PB.wrap(fn), 15, files, {**CFG, "min_trades": 0, "min_days": 0})      # raises SystemExit on look-ahead
        assert r["verdict"], name


def test_top_universe_ranks_on_the_prior_session_only():
    import warehouse_top as WT
    days = ["2026-01-05", "2026-01-06", "2026-01-07"]
    daily = {"BIG": {d: (100.0, 1_000_000) for d in days}, "MID": {d: (50.0, 1_000_000) for d in days},
             "SPIKE": {"2026-01-05": (20.0, 100), "2026-01-06": (20.0, 100), "2026-01-07": (20.0, 99_000_000)},
             "PENNY": {d: (5.0, 90_000_000) for d in days}}
    uni = WT.pick(daily, days, 2)
    assert uni["2026-01-07"] == ["BIG", "MID"]            # SPIKE's volume on 01-07 itself cannot put it in 01-07; PENNY is under $10


def test_profitable_needs_per_trade_net_not_only_day_means():
    rng = np.random.default_rng(5)

    def day(n, mu):
        g = mu + rng.normal(0, 1.0, n)
        return {"gross": g, "lift": g.copy(), "lift_later": g.copy(), "ctl_min": np.zeros(n), "ctl_later": np.zeros(n),
                "px": np.full(n, 100.0), "base_sum": 0.0, "base_n": 1}
    quiet = [day(2, 40.0) for _ in range(30)]                    # a few big winners on quiet days
    busy = [day(200, -3.0) for _ in range(6)]                    # many small losers on the busy days
    per_day = [x for i in range(6) for x in (quiet[5 * i:5 * i + 5] + [busy[i]])]
    r = H.verdict(per_day, {**H.DEFAULTS, "min_trades": 0, "min_days": 0})
    assert r["net"]["mean"] > 0 and r["net_trade"] < 0           # day means say yes, the account says no
    assert not r["profitable"]


def test_pair_trade_subtracts_the_hedge_and_respects_slots():
    import pair_trade as PT
    d = {"day": "2026-01-02", "minute": np.array([30, 31, 32, 33, 200]), "name": np.array([20.0, 20.0, 20.0, 20.0, -10.0]),
         "QQQ": np.array([5.0, 5.0, 5.0, 5.0, -30.0]), "SPY": np.zeros(5), "peers": np.zeros(5)}
    days = [d] * 4
    r = PT.simulate(days, "QQQ", 3, 3.0)
    assert r["n"] == 4 * 4                                   # the 4th signal at minute 33 finds all 3 slots busy; 200 is free again
    assert r["net"] == pytest.approx((3 * (15 - 3) + (20 - 3)) / 4)
    assert PT.simulate(days, None, None, 0.0)["net"] == pytest.approx(14.0)


def test_daily_horizon_window_return_and_delisting():
    import daily_horizon as DH
    O = np.array([[10.0, 10.0], [10.0, 20.0], [11.0, 20.0], [12.0, np.nan], [13.0, np.nan]])
    C = np.array([[10.0, 10.0], [10.5, 20.0], [11.5, 15.0], [12.5, np.nan], [13.5, np.nan]])
    r = DH.window_ret(O, DH.ffill0(C), 0, 2)                    # buy open of day 1, sell open of day 3
    assert r[0] == pytest.approx(2000.0)
    assert r[1] == pytest.approx(-2500.0)                       # gone by day 3: sold at its last close (15 vs a 20 buy)


def test_daily_horizon_alpha_recovers_a_planted_intercept():
    import daily_horizon as DH
    rng = np.random.default_rng(1)
    x = rng.normal(0, 300, 200)
    a, t, b = DH.alpha_t(50 + 1.2 * x + rng.normal(0, 20, 200), x)
    assert a == pytest.approx(50, abs=5) and b == pytest.approx(1.2, abs=0.02) and t > 10


def test_cluster_t_is_smaller_than_naive_t_when_trades_share_a_quarter():
    import daily_horizon as DH
    rng = np.random.default_rng(2)
    shock = rng.normal(0, 50, 20)                                # one shock per quarter, shared by its 100 trades
    g = np.repeat(np.arange(20), 100)
    x = 5 + shock[g] + rng.normal(0, 10, 2000)
    assert abs(DH.cluster_t(x, g)) < abs(DH.tstat(x)) / 3


def test_ride_rule_reads_no_edge_on_pure_drift():
    """Skeptic 11/11: stocks = index x martingale with upward drift. The trend exit holds winners longest, so an arithmetic
    trade-minus-control is positive with no edge. The ratio lift and the beta-adjusted residual must not read PASS."""
    import datetime as dt
    import daily_horizon as DH
    rng = np.random.default_rng(11)
    T, N = 1800, 160
    idx = np.cumprod(1 + 0.0005 + rng.normal(0, 0.008, T))
    C = idx[:, None] * np.cumprod(1 + rng.normal(0, 0.015, (T, N)), axis=0) * 50
    O = np.vstack([C[:1], C[:-1]]) * (1 + rng.normal(0, 0.002, (T, N)))
    days = [(dt.date(2016, 3, 1) + dt.timedelta(days=i)).isoformat() for i in range(T)]
    D = {"days": days, "O": O, "C": C, "Cf": C, "Of": O, "L": np.ones((T, N), bool), "spy_o": idx}
    r = DH.ride_stats(DH.ride_trades(D), days)
    assert r["trades"] > 500
    assert r["verdict"] == "FAIL"
    assert abs(r["lift"]) < 40                                   # bp per trade; the arithmetic version sat near +15 to +17 with t up to PASS


def test_nightly_append_pure_pieces(tmp_path):
    import warehouse_nightly as WN
    old = {"2026-11-05": (100.0, 101.0), "2026-11-06": (101.0, 102.0)}
    assert not WN.adj_moved(old, {"2026-11-06": (101.0, 102.0), "2026-11-09": (102.0, 103.0)})
    assert WN.adj_moved(old, {"2026-11-06": (50.5, 51.0)})                     # a 2-for-1 split since
    assert WN.adj_moved(old, {"2026-11-06": (100.5, 101.5)})                   # a dividend since (0.5%)
    days = ["2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"]
    assert WN.missing_sessions({"2026-11-06"}, days, "2026-11-05", 40) == ["2026-11-09", "2026-11-10"]
    assert WN.missing_sessions(set(), days, "2026-11-05", 1) == ["2026-11-06"]
    rows = [("d1", 12.0, 5e6), ("d2", 12.0, 5e6), ("d3", 12.0, 5e6)]
    assert WN.new_liquid_dates(rows, "d2", lambda r: {"d2", "d3"}) == ["d3"]
    pk = {"syms": np.array(["AAA", "BBB"]), "x": np.array([1])}
    assert WN.save_day(tmp_path, "2026-11-10", dict(pk), {"AAA"}) is True
    z = np.load(tmp_path / "2026-11-10.npz", allow_pickle=True)
    assert list(z["uni"]) == [True, False]
    assert WN.save_day(tmp_path, "2026-11-10", dict(pk), {"BBB"}) is False     # a day file is never overwritten
    assert list(np.load(tmp_path / "2026-11-10.npz", allow_pickle=True)["uni"]) == [True, False]
    assert WN.last_file_day(tmp_path) == "2026-11-10" and WN.last_file_day(tmp_path / "none") == ""


def test_tomorrows_universe_is_known_tonight():
    import warehouse_nightly as WN
    import warehouse_top as WT
    days = ["2026-11-09", "2026-11-10"]
    daily = {"BIG": {d: (100.0, 1e6) for d in days}, "NEW": {"2026-11-09": (20.0, 10.0), "2026-11-10": (20.0, 9e7)}}
    uni = WT.pick(daily, days + [WN.TOMORROW], 1)
    assert uni["2026-11-10"] == ["BIG"] and uni[WN.TOMORROW] == ["NEW"]        # NEW's volume today puts it in tomorrow's list


def test_tug_of_war_measures_read_only_the_last_five_bars():
    import tug_of_war as TW
    n = H.NBAR
    o = np.full((3, n), 100.0)
    c = o.copy()
    c[0, 10:15] = o[0, 10:15] + np.arange(1, 6) * 0.2            # row 0: five green bars stepping up, no wicks
    o[0, 11:15] = c[0, 10:14]
    c[1, 10:15] = 100.0                                          # row 1: five dojis in one place with long upper wicks
    h, l = np.maximum(o, c), np.minimum(o, c)
    h[1, 10:15] += 0.5
    l[1, 10:15] -= 0.1
    day = {"o": o, "h": h, "l": l, "c": c, "v": np.ones((3, n))}
    b = TW.buckets(TW.measures(day))
    assert b["rope: BULLS pulling (>= +1)"][0, 14] and b["greens: 5 of 5"][0, 14] and b["higher steps: 4 of 4"][0, 14]
    assert b["OPERATOR RULE: enter (bulls pulling, not bear wicks)"][0, 14]
    assert b["rope: SIDEWAYS (-1..+1)"][1, 14] and b["wicks: bear wicks (upper >= 65%)"][1, 14]
    assert b["OPERATOR RULE: prevent (sideways, bears, or bear wicks)"][1, 14]
    cut = {k: v.copy() for k, v in day.items()}
    for k in ("o", "h", "l", "c"):
        cut[k][:, 15:] = np.nan
    m1, m2 = TW.measures(day), TW.measures(cut)
    assert all(np.allclose(m1[k][:, :15], m2[k][:, :15], equal_nan=True) for k in m1)   # nothing after bar m is read


def test_five_minute_candles_use_only_finished_candles():
    import tug_of_war as TW
    n = H.NBAR
    c = np.tile(100.0 + np.arange(n) * 0.1, (1, 1))
    o = c - 0.1
    day = {"o": o, "h": c + 0.05, "l": o - 0.05, "c": c, "v": np.ones((1, n))}
    k5 = TW.candles(day, 5)
    assert k5["o"].shape == (1, 78) and k5["o"][0, 0] == pytest.approx(99.9) and k5["c"][0, 0] == pytest.approx(100.4)
    assert k5["h"][0, 1] == pytest.approx(100.95) and k5["v"][0, 0] == 5
    m = TW.measures_tf(day, 5)
    assert np.isnan(m["rope"][0, 23]) and not np.isnan(m["rope"][0, 24])          # five finished 5-minute candles exist at 09:54, not before
    assert m["rope"][0, 24] == m["rope"][0, 28] and m["rope"][0, 29] != m["rope"][0, 28] or m["greens"][0, 29] == 5
    cut = {q: v.copy() for q, v in day.items()}
    for q in ("o", "h", "l", "c"):
        cut[q][:, 27:] = np.nan                                                   # blank from 09:57: the 09:55 candle is unfinished
    m2 = TW.measures_tf(cut, 5)
    assert np.allclose(m["rope"][:, :27], m2["rope"][:, :27], equal_nan=True)


def test_tug_forward_counts_until_due(tmp_path):
    import tug_of_war as TW
    (tmp_path / "out.jsonl").write_text("")
    text = TW.forward(tmp_path, tmp_path / "out.jsonl")
    assert text.count("NOT DUE") == 2 and "0 of 40 flagged" in text and "PRIMARY" in text
