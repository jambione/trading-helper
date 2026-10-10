"""Known-answer tests for tools/studies/std_scorer.py: planted edges come back, no edge comes back as ~0, and the
room-HOD style path-dependent membership does NOT create a fake edge (it does under name-day-first averaging)."""
import os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import std_scorer as S


def synth(edge_bp, days=60, names=40, per_name=12, seed=1, path_dependent=False):
    rng = random.Random(seed)
    out = []
    for di in range(days):
        day = f"2026-01-{di:03d}"
        mkt = rng.gauss(0, 20)
        for ni in range(names):
            level = 0.0
            for k in range(per_name):
                ret = mkt + rng.gauss(0, 60)
                if path_dependent:
                    # in the cell while the name's running path is below -40 (membership depends on the path so far);
                    # true future return is independent of membership: there is NO edge.
                    in_cell = level < -40
                    level += ret
                else:
                    in_cell = rng.random() < 0.3
                    if in_cell:
                        ret += edge_bp
                out.append({"day": day, "name": f"N{ni}", "ts": k, "hour": 10 + k // 4, "ret_bp": ret, "cell": in_cell})
    return out


def test_planted_edge_comes_back():
    r = S.cell_vs_control(synth(15.0), lambda d: d["cell"])
    assert 10 < r["stat"]["mean"] < 20 and r["stat"]["t"] > 4


def test_no_edge_reads_zero():
    r = S.cell_vs_control(synth(0.0, seed=7), lambda d: d["cell"])
    assert abs(r["stat"]["mean"]) < 5 and abs(r["stat"]["t"]) < 2.5


def test_path_dependent_membership_does_not_fake_an_edge():
    rows = synth(0.0, seed=3, path_dependent=True)
    r = S.cell_vs_control(rows, lambda d: d["cell"])
    assert abs(r["stat"]["mean"]) < 6, r["stat"]


def test_paired_variants_matches_entries_and_keeps_empty_days():
    ref = [{"day": "D1", "symbol": "A", "entry_ts": 100.0, "net_bp": -5.0},
           {"day": "D1", "symbol": "B", "entry_ts": 200.0, "net_bp": 3.0}]
    var = [{"day": "D1", "symbol": "A", "entry_ts": 102.0, "net_bp": 1.0},
           {"day": "D1", "symbol": "C", "entry_ts": 300.0, "net_bp": 9.0}]
    r = S.paired_variants(ref, var, ["D1", "D2"])
    assert r["matched"] == 1 and r["paired_days"]["D1"] == 6.0
    assert r["unmatched_var"] == 1 and r["unmatched_ref"] == 1
    assert r["dollars_days"]["D2"] == 0.0 and "D2" in r["days_without_matches"]


def test_preflight_flags_split_adjusted_pennies_and_429s():
    rows = [{"day": "D1", "raw": 0.30, "adj": 135.0}, {"day": "D1", "raw": 50.0, "adj": 50.0}]
    rep = S.preflight(rows, raw_key="raw", adj_key="adj", floor=10, rate_limited=2, expected_days=["D1", "D2"])
    assert rep["adj_raw_ratio_off"] == 1 and rep["raw_below_floor_adj_above"] == 1 and rep["missing_days"] == ["D2"]
    assert len(rep["problems"]) == 4


def test_name_day_first_averaging_fakes_an_edge_on_the_same_data():
    """The room-HOD artifact reproduced: needle_cells_oos.score_cell (name-day first) on path-dependent membership."""
    import importlib.util
    p = os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "needle_cells_oos.py")
    spec = importlib.util.spec_from_file_location("needle_cells_oos", p)
    N = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(N)
    rows = synth(0.0, seed=3, path_dependent=True)
    moments = [{"day": r["day"], "nd": (r["name"], r["day"]), "hour": r["hour"], "gross": r["ret_bp"], "c": r["cell"]} for r in rows]
    biased = N.score_cell(moments, "c", 0.0)
    m_biased = sum(biased["day_diff"].values()) / len(biased["day_diff"])
    honest = S.cell_vs_control(rows, lambda d: d["cell"])["stat"]["mean"]
    assert m_biased > honest + 5, (m_biased, honest)
