"""slope_now_read.py: session lifts against REF and CRAND, the presence exclusion, the fidelity-day skip and the
verdict ladder."""
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import late_gates_read as LR  # noqa: E402
import slope_now_read as Q  # noqa: E402

T0 = 1_793_000_000.0


def pr():
    return LR.Pricer(lambda s, t: 2.0, lambda s, d, m: 100.0)


def _w(d, day, var, ret, n=8):
    rows = [{"symbol": f"S{i}", "entry_ts": T0 + 900 * i, "entry_px": 100.0, "ret": ret} for i in range(n)]
    (d / f"{day}-{var}.json").write_text(json.dumps({"closed": rows, "open_at_end": []}))


def _arms(d, day, fresh_share):
    with gzip.open(d / f"{day}-REF-arms.jsonl.gz", "wt") as fh:
        for i in range(100):
            ok = i < fresh_share * 100
            fh.write(json.dumps({"ts": T0, "pctr_pitch_ts": T0 - (1 if ok else 60), "pctr_fast_pitch": 1.0,
                                 "pctr_slow_pitch": 1.0, "pctr_slow_pitch_prev": 1.0}) + "\n")


def _day(d, day, ref, slope, crand=None, n_crand=8):
    _w(d, day, "REF", ref)
    _w(d, day, "SLOPE", slope, n=6)
    _w(d, day, "MDE1", ref)
    for k in Q.SEEDS:
        _w(d, day, f"CRAND_{k}", ref if crand is None else crand, n=n_crand)
        _w(d, day, f"RAND_{k}", ref)


def test_session_lifts_and_crand_count_match(tmp_path):
    _day(tmp_path, "2026-10-20", 0.001, 0.0015, n_crand=6)
    r = Q.session("2026-10-20", tmp_path, pr())
    assert abs(r["lift_ref"] - 5.0) < 1e-6 and abs(r["lift_rand"] - 5.0) < 1e-6
    assert r["classes"]["removed"] == 2


def test_crand_count_mismatch_drops_lift_rand(tmp_path):
    _day(tmp_path, "2026-10-20", 0.001, 0.0015, n_crand=8)          # 8 vs 6 is outside +-15%
    assert Q.session("2026-10-20", tmp_path, pr())["lift_rand"] is None


def test_presence_exclusion_and_fidelity_day_not_scored(tmp_path):
    for d, share in (("2026-10-20", 0.6), ("2026-10-21", 0.2), ("2026-10-27", 0.6)):
        _day(tmp_path, d, 0.001, 0.001)
        _arms(tmp_path, d, share)
    rows, excl = Q.rows_for(tmp_path, pr())
    assert [r["day"] for r in rows] == ["2026-10-20"]
    assert [r["day"] for r in excl] == ["2026-10-21"] and "presence" in excl[0]["excluded"]


def _rows(lifts, ctl=0.5):
    return [{"day": f"d{i}", "lift_ref": x, "lift_ref_rp": x, "lift_rand": x, "m": {"SLOPE": 1.0},
             "ctl_rand": ctl * (i % 3), "ctl_mde1": ctl * (i % 2), "delays": [], "lift_EXISTS": 0.0,
             "lift_SLOPE_OPEN": 0.0, "n": {"REF": 20, "SLOPE": 12},
             "classes": {"same": 10, "delayed": 0, "removed": 8, "new": 0}} for i, x in enumerate(lifts)]


def test_verdict_pass_fail_and_underpowered():
    good = _rows([4.0, 5.0, 6.0, 4.5, 5.5] * 4)
    assert Q.verdict(good, Q.count(good, []), 0.0)["verdict"] == "PASS"
    half_bad = _rows([4.0, 5.0, 6.0, 4.5, 5.5] * 2 + [-1.0, -2.0, -0.5, -1.5, -1.0] * 2)
    assert Q.verdict(half_bad, Q.count(half_bad, []), 0.0)["verdict"] == "FAIL"
    few = _rows([4.0, 5.0, 6.0, 4.5])
    assert Q.verdict(few, Q.count(few, []), 0.0)["verdict"].startswith("UNDERPOWERED")


def test_exists_reading_when_history_explains_it():
    rows = _rows([4.0, 5.0, 6.0, 4.5, 5.5] * 4)
    for r in rows:
        r["lift_EXISTS"] = 9.0
    v = Q.verdict(rows, Q.count(rows, []), 0.0)
    assert v["info"]["pitch_history_reading"] == "the effect is the pitch-history requirement, not the slope"
