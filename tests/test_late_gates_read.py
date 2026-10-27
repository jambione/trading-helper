"""late_gates_read.py: pricing, classes, exclusions, canary, halves and the verdict ladder on synthetic replay files."""
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import late_gates_read as L  # noqa: E402

ET = ZoneInfo("America/New_York")


def ts(day, hh, mm, ss=0):
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hh, minute=mm, second=ss, tzinfo=ET).timestamp()


def pricer(spread=2.0, close=None):
    return L.Pricer(lambda s, t: spread, close or (lambda s, d, m: 100.0))


def trade(sym, t, ret, px=100.0):
    return {"symbol": sym, "entry_ts": t, "entry_px": px, "ret": ret}


def write(d, day, var, closed, open_=()):
    (d / f"{day}-{var}.json").write_text(json.dumps({"closed": closed, "open_at_end": list(open_)}))


def test_open_at_end_marked_at_1549_close_and_repriced_from_ret():
    day = "2026-10-20"
    closes = {L.close_1549(day): 101.0, L.minute_of(ts(day, 10, 0, 30)): 100.5}
    pr = pricer(close=lambda s, d, m: closes.get(int(m)))
    res = {"closed": [trade("AAA", ts(day, 10, 0, 30), 0.01)], "open_at_end": [trade("BBB", ts(day, 15, 0), None)]}
    res["open_at_end"][0].pop("ret")
    out = L.trades(res, day, pr)
    assert len(out) == 2
    a = [t for t in out if t["symbol"] == "AAA"][0]
    assert abs(a["gross"] - 100.0) < 1e-9
    assert abs(a["gross_rp"] - ((101.0 / 100.5) - 1) * 1e4) < 1e-6      # exit = entry*(1+ret), not exit_px
    b = [t for t in out if t["symbol"] == "BBB"][0]
    assert b["open"] and abs(b["gross"] - 100.0) < 1e-9


def test_no_nbbo_over_ten_percent_excludes_session():
    day = "2026-10-20"
    pr = L.Pricer(lambda s, t: None if s == "X" else 2.0, lambda s, d, m: 100.0)
    tr = {"REF": [dict(t, gross=0.0, gross_rp=0.0) for t in
                  [trade("X", 1.0, 0), trade("Y", 2.0, 0), trade("Z", 3.0, 0), trade("W", 4.0, 0)]]}
    assert L.charge(tr, pr)["excluded"].startswith("no_nbbo")


def test_classes_same_delayed_removed_new():
    ref = [{"symbol": "A", "entry_ts": 1000.0}, {"symbol": "B", "entry_ts": 1000.0}, {"symbol": "C", "entry_ts": 1000.0}]
    g = [{"symbol": "A", "entry_ts": 1060.0}, {"symbol": "B", "entry_ts": 1000.0 + 600}, {"symbol": "D", "entry_ts": 5000.0}]
    c = L.classify(ref, g)
    assert (c["same"], c["delayed"], c["removed"], c["new"]) == (1, 1, 1, 1)
    assert c["delays"] == [10.0]


def _session_files(d, day, ref_rets, g_rets, rand_rets=None, n=6):
    t0 = ts(day, 10, 0)
    ref = [trade(f"S{i}", t0 + 600 * i, r) for i, r in enumerate(ref_rets)]
    g = [trade(f"S{i}", t0 + 600 * i, r) for i, r in enumerate(g_rets)]
    write(d, day, "REF", ref)
    write(d, day, "S5", g)
    write(d, day, "MDE1", ref)
    for k in (1, 2, 3):
        rr = rand_rets if rand_rets is not None else ref_rets
        write(d, day, f"S5_RAND_{k}", [trade(f"S{i}", t0 + 600 * i, r) for i, r in enumerate(rr[:len(g_rets)])])


def test_identical_session_scores_lift_zero_and_ref_keyed_minimum(tmp_path):
    day = "2026-10-20"
    _session_files(tmp_path, day, [0.001] * 6, [0.001] * 6)
    r = L.session(day, "S5", tmp_path, pricer())
    assert r["identical"] and abs(r["lift_ref"]) < 1e-9 and "excluded" not in r
    _session_files(tmp_path, "2026-10-21", [0.001] * 4, [0.001] * 4)
    assert "ref_trades" in L.session("2026-10-21", "S5", tmp_path, pricer())["excluded"]


def test_canary_moves_lift_by_ten(tmp_path):
    days = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
    for i, d in enumerate(days):
        _session_files(tmp_path, d, [0.001 * (i + 1)] * 6, [0.0005 * (i + 2)] * 5 + [-0.001])
    res = L.canary(tmp_path, pricer())
    assert res["S5"]["ok"] and abs(res["S5"]["delta"] - 10.0) < 1e-6


def test_halves_by_count():
    a, b = L.halves([{"d": i} for i in range(11)])
    assert len(a) == 6 and len(b) == 5


def _rows(lifts, rand=None):
    return [{"day": f"d{i}", "lift_ref": x, "lift_ref_rp": x, "lift_rand": (rand if rand is not None else x),
             "m": {"REF": -20.0, "S5": -20.0 + x}} for i, x in enumerate(lifts)]


def _cnt(power=True, share=0.2, removed=0.2):
    return {"power_ok": power, "share_net_pooled": share, "share_removed_pooled": removed,
            "trades": {"REF": 400, "S5": 200}}


def test_verdict_pass_needs_every_check_and_reports_loss_reducer():
    rows = _rows([6.0, 5.5, 6.5, 5.0] * 6)
    v = L.verdict("S5", rows, _cnt(), 0.0)
    assert v["verdict"] == "PASS" and v["reading"] == "loss reducer only"


def test_verdict_one_negative_half_fails_regardless_of_t():
    rows = _rows([9.0] * 12 + [-0.5] * 12)
    assert L.verdict("S5", rows, _cnt(), 0.0)["verdict"] == "FAIL"


def test_verdict_underpowered_is_not_scored_before_the_cap():
    assert L.verdict("S5", _rows([6.0] * 24), _cnt(power=False), 0.0)["verdict"].startswith("UNDERPOWERED")


def test_rand_leg_dropped_only_when_both_shares_small():
    rows = _rows([6.0, 5.5, 6.5, 5.0] * 6, rand=-1.0)
    assert L.verdict("S5", rows, _cnt(share=0.01, removed=0.2), 0.0)["verdict"] == "FAIL"
    assert L.verdict("S5", rows, _cnt(share=0.01, removed=0.01), 0.0)["verdict"] == "PASS"


def test_dup_penalty_raises_the_bar():
    rows = _rows([3.4, 3.6] * 12)
    assert L.verdict("S5", rows, _cnt(), 0.0)["checks"]["lift_ref"]
    assert not L.verdict("S5", rows, _cnt(), 1.5)["checks"]["lift_ref"]


def test_mde_is_max_of_both_proxies():
    rows = [{"ctl_rand": x, "ctl_mde1": 2 * x} for x in (1.0, -1.0, 2.0, -2.0)]
    m = L.mde(rows)
    import statistics
    assert math.isclose(m, 2.8 * statistics.stdev([2.0, -2.0, 4.0, -4.0]) / 2)
