"""stale_age_read.py: SIP entry pricing, pair matching / timing, exclusions, canary and verdict ladder."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import late_gates_read as LR  # noqa: E402
import stale_age_read as A  # noqa: E402

T0 = 1_793_000_000.0


def pr():
    return LR.Pricer(lambda s, t: 2.0, lambda s, d, m: 100.0)


def sip(prints=None, fail=False):
    def fetch(sym, t0, t1):
        if fail:
            raise RuntimeError("429")
        return prints if prints is not None else [(t0 + 2.0, 100.0)]
    return A.SipEntry(fetch)


def test_sip_entry_is_first_trade_after_the_decision_within_60s():
    s = sip([(T0 - 1, 99.0), (T0 + 3, 101.0), (T0 + 5, 102.0)])
    assert s.price("D", "AAA", T0) == 101.0
    assert sip([]).price("D", "AAA", T0) is None
    f = sip(fail=True)
    assert f.price("D1", "AAA", T0) is None and "D1" in f.failed


def test_gross_uses_replay_exit_over_sip_entry():
    res = {"closed": [{"symbol": "AAA", "entry_ts": T0, "entry_px": 100.0, "ret": 0.01}]}
    out, n = A.price_trades(res, "2026-10-20", pr(), sip([(T0 + 1, 100.5)]))
    assert n == 0 and abs(out[0]["gross"] - (101.0 / 100.5 - 1) * 1e4) < 1e-9 and abs(out[0]["net"] - (out[0]["gross"] - 2.0)) < 1e-9


def test_match_pairs_is_one_to_one_nearest_same_symbol():
    ref = [{"symbol": "A", "entry_ts": 1000.0}, {"symbol": "A", "entry_ts": 1500.0}, {"symbol": "B", "entry_ts": 1000.0}]
    g = [{"symbol": "A", "entry_ts": 940.0}, {"symbol": "A", "entry_ts": 1490.0}, {"symbol": "B", "entry_ts": 5000.0}]
    pairs = sorted(A.match_pairs(ref, g))
    assert pairs == [(0, 0, -60.0), (1, 1, -10.0)]


def _write(d, day, var, rows):
    (d / f"{day}-{var}.json").write_text(json.dumps({"closed": rows, "open_at_end": []}))


def _sess(d, day, ref_rets, age_rets, shift=-120.0):
    base = [{"symbol": f"S{i}", "entry_ts": T0 + 600 * i, "entry_px": 100.0, "ret": r} for i, r in enumerate(ref_rets)]
    age = [{"symbol": f"S{i}", "entry_ts": T0 + 600 * i + shift, "entry_px": 100.0, "ret": r} for i, r in enumerate(age_rets)]
    _write(d, day, "REF", base)
    _write(d, day, "AGE30", age)
    _write(d, day, "MDE1", base)
    for k in A.SEEDS:
        _write(d, day, f"RREL_{k}", base)
    (d / f"{day}-RREL_share.json").write_text(json.dumps({"share": 0.3}))


def test_session_lift_timing_and_moved_share(tmp_path):
    _sess(tmp_path, "2026-10-20", [0.001] * 6, [0.002] * 6)
    r = A.session("2026-10-20", tmp_path, pr(), sip())
    assert abs(r["lift_ref"] - 10.0) < 1e-6 and r["moved_share"] == 1.0
    assert all(dt == -120.0 for dt in r["timing"])


def test_exclusions_ref_minimum_and_failed_fetch(tmp_path):
    _sess(tmp_path, "2026-10-20", [0.001] * 4, [0.001] * 4)
    assert "ref_trades" in A.session("2026-10-20", tmp_path, pr(), sip())["excluded"]
    _sess(tmp_path, "2026-10-21", [0.001] * 6, [0.001] * 6)
    assert A.session("2026-10-21", tmp_path, pr(), sip(fail=True))["excluded"] == "SIP fetch failed"


def test_canary_moves_lift_by_ten(tmp_path):
    for k, day in enumerate(["2026-10-20", "2026-10-21", "2026-10-29", "2026-10-30"]):
        _sess(tmp_path, day, [0.001 * (k + 1)] * 6, [0.0015 * (k + 1)] * 5 + [-0.001])
    base, _ = A.rows_for(tmp_path, pr(), sip())
    bump, _ = A.rows_for(tmp_path, pr(), sip(), canary_bp=10.0)
    for x, y in zip(base, bump):
        assert abs((y["lift_ref"] - x["lift_ref"]) - 10.0) < 1e-9


def test_lower95_and_t975():
    assert A.t975(21) == 2.086 and A.t975(500) == 1.96
    assert A.lower95([1.0, 2.0, 3.0, 4.0]) < 2.5
