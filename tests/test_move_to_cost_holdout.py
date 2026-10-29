"""move_to_cost_holdout: labels, session d, the three removal controls, placebo MDE, power, VOID, verdict wiring."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import move_to_cost_holdout as M  # noqa: E402


def test_labels_and_cut():
    assert M.label(None) == "missing" and M.label(7.99) == "LOW" and M.label(8.0) == "REST"


def _rows(days=8, low_net=-12.0, rest_net=-3.0, n_low=3, n_rest=10, wide_low=False):
    rows = []
    for d in range(days):
        for i in range(n_low):
            rows.append({"day": f"d{d}", "status": "ok", "group": "LOW", "net": low_net + (i - 1) * 2 + d * 0.3,
                         "gross": low_net + 5, "spread": 9.0 if wide_low else 5.0, "rng": 0.3})
        for i in range(n_rest):
            rows.append({"day": f"d{d}", "status": "ok", "group": "REST", "net": rest_net + (i % 3) - 1 + d * 0.2,
                         "gross": rest_net + 5, "spread": 6.0 if wide_low else 5.0 + (i % 5), "rng": 1.0 + 0.1 * i})
    return rows


def test_session_d_needs_both_groups():
    rows = _rows(days=3)
    rows.append({"day": "solo", "status": "ok", "group": "LOW", "net": -50.0, "gross": 0.0, "spread": 5.0, "rng": 0.1})
    assert len(M.session_d(rows)) == 3


def test_holds_when_low_is_really_worse():
    v = M.verdict_set(_rows())
    assert v["checks"]["d"] and v["checks"]["low_negative"] and v["checks"]["beats_random"], v
    assert v["checks"]["beats_range_only"] is not None


def test_spread_only_control_catches_a_pure_cost_artifact():
    """If LOW is just the widest-spread trades, removing by spread does as well: beats_spread_only must fail."""
    rows = _rows(wide_low=True)
    for r in rows:                      # identical gross for everyone: any LOW gap is the spread alone
        r["gross"] = 0.0
        r["net"] = -r["spread"]
    v = M.verdict_set(rows)
    assert v["checks"]["beats_spread_only"] is False


def test_power_and_void():
    c = {"low_n": 25, "low_sessions": 7, "mde": 8.0}
    assert M.powered(c) and not M.powered({**c, "mde": 12.0}) and not M.powered({**c, "low_n": 10})
    rows = [{"status": "error"}] * 6 + [{"status": "ok"}] * 94
    assert M.void(rows) and not M.void([{"status": "error"}] * 5 + [{"status": "ok"}] * 95)


def test_placebo_mde_is_finite_and_positive():
    m = M.placebo_mde(M.scored(_rows()))
    assert m is not None and m > 0


def test_data_errors_are_not_no_data(tmp_path, monkeypatch):
    calls = []

    def bad(path, params):
        calls.append(path)
        raise RuntimeError("429")
    d = M.Data(bad, tmp_path)
    monkeypatch.setattr(M.time, "sleep", lambda s: None)
    try:
        d.spread_bp("AAA", 1_793_000_000.0)
        assert False, "should raise"
    except M.FetchError:
        pass
    assert len(calls) == 3 and d.errors == 1 and not list(tmp_path.glob("*.json"))     # error is never cached


def test_t_quantiles_small_samples():
    assert M.t_crit(6, 0.975) == 2.447 and M.t_crit(7, 0.80) == 0.896 and M.t_crit(100, 0.975) == 1.96
