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


@pytest.mark.parametrize("name", ["current_square", "early_leg_turn", "base_break", "early_square", "square_variable", "square_flat", "square_up3to6", "square_up6plus", "square_early_only", "square_late_only", "surge_unmoved", "flow_proxy"])
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
