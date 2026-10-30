"""iex_order_spread_calibration stage 0: delayed timestamp, primary population, power stop."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import iex_order_spread_calibration as C  # noqa: E402


def _r(day, t=600, spread=5.0, status="ok", group="REST"):
    return {"day": day, "symbol": "AAA", "t": t, "spread": spread, "status": status, "group": group}


def test_moments_and_delayed_ts():
    rows = [_r("2026-06-01"), _r("2026-06-01", status="missing", group="missing")]
    assert len(C.moments(rows)) == 1
    assert C.delayed_ts(rows[0]) == C.MH.minute_ts("2026-06-01", 601) - 16 * 60


class FakeData:
    def __init__(self, q):
        self.q, self.calls = q, 0

    def quote(self, sym, ts):
        self.calls += 1
        v = self.q.get(ts, "none")
        if v == "err":
            raise C.MH.MH.FetchError("x")
        return None if v == "none" else v


def test_primary_wide_and_underpowered():
    ms = []
    for k in range(40):
        day = f"2026-08-{1 + k % 28:02d}"                  # half B
        ms += [_r(day, t=600, spread=20.0), _r(day, t=610, spread=4.0), _r(day, t=620, spread=30.0)]
    q = {}
    for r in ms:
        ts = C.delayed_ts(r)
        q[ts] = [9.999, 10.001] if r["t"] != 620 else [9.99, 10.01]   # 2 bp vs 20 bp delayed for t=620: not in primary
    st = C.attach_delayed(ms, FakeData(q))
    s = C.summary(ms)
    assert st["ok"] == 120 and s["B"]["primary_n"] == 80 and s["B"]["primary_wide"] == 40
    assert abs(s["B"]["base_rate"] - 0.5) < 1e-9 and s["B"]["all_wide_in_half"] == 80
    assert not s["powered"] and s["verdict"].startswith("UNDERPOWERED")


def test_missing_delayed_quote_is_excluded_and_errors_void():
    ms = [_r("2026-08-03") for _ in range(10)]
    st = C.attach_delayed(ms, FakeData({}))
    s = C.summary(ms)
    assert st["missing"] == 10 and s["B"]["primary_n"] == 0 and s["B"]["excluded_no_delayed_quote"] == 10
    ms2 = [_r("2026-08-03")]
    C.attach_delayed(ms2, FakeData({C.delayed_ts(ms2[0]): "err"}))
    assert C.summary(ms2)["void"]
