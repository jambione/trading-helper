"""square_relax_read.py: session lifts and moved pairs, the screen verdict ladder, the per-session stop check."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import late_gates_read as LR  # noqa: E402
import square_relax_read as Q  # noqa: E402
import stale_age_read as SR  # noqa: E402

T0 = 1_793_000_000.0


def pr():
    return LR.Pricer(lambda s, t: 2.0, lambda s, d, m: 100.0)


def sip():
    return SR.SipEntry(lambda sym, t0, t1: [(t0 + 1.0, 100.0)])


def _w(d, day, var, rets, shift=0.0, n=8):
    rows = [{"symbol": f"S{i}", "entry_ts": T0 + 900 * i + shift, "entry_px": 100.0, "ret": rets[i % len(rets)]} for i in range(n)]
    (d / f"{day}-{var}.json").write_text(json.dumps({"closed": rows, "open_at_end": []}))


def _day(d, day, ref_ret, v_ret, shift=-120.0):
    _w(d, day, "REF", [ref_ret])
    _w(d, day, "TIGHT20", [v_ret], shift)
    _w(d, day, "MDE1", [ref_ret])
    for k in Q.SEEDS:
        _w(d, day, f"RAND_TIGHT20_{k}", [ref_ret])
    (d / f"{day}-RAND_TIGHT20_share.json").write_text(json.dumps({"share": 0.4}))


def test_session_moved_pairs_timing_and_lifts(tmp_path):
    _day(tmp_path, "2026-10-20", 0.001, 0.0015)
    r = Q.session("2026-10-20", "TIGHT20", tmp_path, pr(), sip())
    assert abs(r["lift_ref"] - 5.0) < 1e-6 and r["moved_share"] == 1.0
    assert r["timing"] == [-120.0] * 8 and abs(r["lift_rand"] - 5.0) < 1e-6


def test_unmoved_trades_do_not_count_as_moved(tmp_path):
    _day(tmp_path, "2026-10-20", 0.001, 0.001, shift=10.0)
    r = Q.session("2026-10-20", "TIGHT20", tmp_path, pr(), sip())
    assert r["moved_share"] == 0.0 and r["timing"] == []


def _rows(lifts, timing=-120.0, n=12, moved=1.0):
    return [{"day": f"d{i}", "lift_ref": x, "timing": [timing] * 4, "moved_share": moved,
             "moved_diffs": [x] * 4, "n": {"REF": n, "TIGHT20": n}, "lift_rand": x, "ctl_rand": 0.1 * (i % 3),
             "ctl_mde1": 0.2 * (i % 2)} for i, x in enumerate(lifts)]


def test_verdict_screen_pass_needs_faster_and_value_kept():
    e, l = _rows([0.5, 1.5, 1.0, 0.8, 1.2] * 3), _rows([0.4, 1.1, 0.9, 1.3, 0.7] * 3)
    v = Q.verdict("TIGHT20", e, l, [0.0] * 15, [0.0] * 15, 2.0)
    assert v["verdict"] == "SCREEN PASS", v


def test_verdict_fails_when_not_faster_or_neighbour_costs():
    e, l = _rows([0.5, 1.5, 1.0, 0.8, 1.2] * 3, timing=30.0), _rows([0.4, 1.1, 0.9, 1.3, 0.7] * 3, timing=30.0)
    assert Q.verdict("TIGHT20", e, l, [0.0] * 15, [0.0] * 15, 2.0)["verdict"] == "SCREEN FAIL"
    e, l = _rows([0.5, 1.5, 1.0, 0.8, 1.2] * 3), _rows([0.4, 1.1, 0.9, 1.3, 0.7] * 3)
    assert Q.verdict("TIGHT20", e, l, [-9.0, -8.5, -9.5] * 5, [0.0] * 15, 2.0)["checks"]["neighbour_ok"] is False


def test_negative_point_estimate_fails_even_inside_the_margin():
    e, l = _rows([-0.3, -0.1, -0.2, -0.4, 0.0] * 3), _rows([0.4, 1.1, 0.9, 1.3, 0.7] * 3)
    assert Q.verdict("TIGHT20", e, l, [0.0] * 15, [0.0] * 15, 2.0)["verdict"] != "SCREEN PASS"


def test_underpowered_when_trades_below_floor():
    e, l = _rows([1.0, 2.0] * 3, n=6), _rows([1.0, 2.0] * 3, n=6)
    assert Q.verdict("TIGHT20", e, l, [0.0] * 6, [0.0] * 6, 2.0)["verdict"] == "UNDERPOWERED"


def test_stage2_harm_stop_when_not_faster(tmp_path):
    _day(tmp_path, "2026-10-29", 0.001, 0.001, shift=60.0)
    res = Q.stage2_check("2026-10-29", "TIGHT20", tmp_path, pr(), sip(), [])
    assert res["harm"] is True and res["stop"] is True
