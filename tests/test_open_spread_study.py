"""open_spread_run / open_spread_read: window helpers, added-trade matching, session statistics, the verdict ladder."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import late_gates_read as LR  # noqa: E402
import open_spread_read as Q  # noqa: E402
import open_spread_run as R  # noqa: E402
import stale_age_read as SR  # noqa: E402

DAY = "2026-10-20"
T0, _ = Q.window(DAY)


def pr():
    return LR.Pricer(lambda s, t: 2.0, lambda s, d, m: 100.0)


def sip():
    return SR.SipEntry(lambda sym, t0, t1: [(t0 + 1.0, 100.0)])


def _w(d, var, trades):
    rows = [{"symbol": s, "entry_ts": ts, "entry_px": 100.0, "ret": r} for s, ts, r in trades]
    (d / f"{DAY}-{var}.json").write_text(json.dumps({"closed": rows, "open_at_end": []}))


def test_window_and_ratio_rules():
    t0, t1 = R.window(DAY)
    assert t1 - t0 == 16 * 60
    assert R.ratio_ok(0, 0) and not R.ratio_ok(1, 0) and R.ratio_ok(10, 12) and not R.ratio_ok(30, 12)


def test_added_trades_are_window_entries_without_a_ref_match():
    ref = [{"symbol": "A", "entry_ts": T0 + 500}]
    g = [{"symbol": "A", "entry_ts": T0 + 300},          # in the window but REF bought it within 600 s
         {"symbol": "B", "entry_ts": T0 + 60},           # in the window, no REF match: added
         {"symbol": "C", "entry_ts": T0 + 3600}]         # outside the window
    assert [t["symbol"] for t in Q.added(ref, g, DAY)] == ["B"]


def test_session_added_diff_against_rand(tmp_path):
    base = [("S%d" % i, T0 + 1800 + 900 * i, 0.001) for i in range(6)]
    _w(tmp_path, "REF", base)
    _w(tmp_path, "OPEN16", base + [("NEW", T0 + 120, 0.003)])
    for k in Q.SEEDS:
        _w(tmp_path, f"RAND_{k}", base + [("RND", T0 + 200, 0.0005)])
    (tmp_path / f"{DAY}-RAND_share.json").write_text(json.dumps({"release": 0.4}))
    r = Q.session(DAY, tmp_path, pr(), sip())
    assert r["added"] == {"OPEN16": 1, "RAND": 1.0}
    assert abs(r["added_diff"] - 25.0) < 1e-6 and r["session_lift"] > 0 and r["early"]["OPEN16"] == 1


def _rows(diffs, n=40, sl=5.0, lr=0.5, placebo=(1.0, -1.0, 0.5, -0.5)):
    return [{"day": f"d{i}", "added_diff": x, "session_lift": sl, "lift_ref": lr + 0.1 * (i % 3), "lift_rand": 0.5,
             "placebo": placebo[i % len(placebo)], "n": {"REF": n, "OPEN16": n},
             "added": {"OPEN16": 3, "RAND": 3}, "early": {"REF": 1, "OPEN16": 4}, "unmatched": False}
            for i, x in enumerate(diffs)]


def test_verdict_pass_refuted_and_underpowered():
    good = _rows([4.0, 6.0, 5.0, 3.0, 7.0, 5.5] * 4)
    assert Q.verdict(good)["verdict"] == "SCREEN PASS"
    bad = _rows([4.0, 6.0, 5.0, 3.0, 7.0, 5.5] * 2 + [-6.0, -4.0, -5.0, -7.0, -3.0, -6.5] * 2)
    assert Q.verdict(bad)["verdict"] == "FAIL (refuted)"
    few = _rows([4.0, 6.0, 5.0, 3.0, 7.0, 5.5])
    assert Q.verdict(few)["verdict"] == "UNDERPOWERED"


def test_control_unmatched_when_rand_adds_far_fewer():
    rows = _rows([4.0, 6.0, 5.0, 3.0, 7.0, 5.5] * 4)
    for r in rows:
        r["added"]["RAND"] = 1
    assert Q.verdict(rows)["verdict"] == "CONTROL UNMATCHED (UNPROVEN)"
