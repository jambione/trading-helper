import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import eod_reversal as E  # noqa: E402


def bars(rows):
    return np.array(rows, dtype=float)


def day(px_1459, px_1530_open, px_1559_close, extra=()):
    rows = [[570, 100, 100, 100, 100, 1], [899, px_1459, px_1459, px_1459, px_1459, 1],
            [930, px_1530_open, px_1530_open, px_1530_open, px_1530_open, 1]]
    rows += [list(x) for x in extra]
    if px_1559_close is not None:
        rows.append([959, px_1559_close, px_1559_close, px_1559_close, px_1559_close, 1])
    return bars(sorted(rows))


PREV = bars([[959, 100, 100, 100, 100, 1]])


def test_signal_and_outcome():
    row, why = E.name_row("X", day(95, 96, 97), PREV)
    assert why is None
    assert abs(row["rod3"] - (-0.05)) < 1e-12
    assert abs(row["lh"] - (97 / 96 - 1)) < 1e-12


def test_missing_exit_bar_is_not_skipped():
    row, why = E.name_row("X", day(95, 96, None, extra=[[955, 98, 98, 98, 98, 1]]), PREV)
    assert why is None and abs(row["lh"] - (98 / 96 - 1)) < 1e-12


def test_skips_before_entry():
    assert E.name_row("X", day(95, 96, 97), None)[1] == "no_prior_session"
    assert E.name_row("X", day(95, 9, 9), PREV)[1] == "below_10"
    no_entry = bars([[570, 100, 100, 100, 100, 1], [899, 95, 95, 95, 95, 1], [959, 97, 97, 97, 97, 1]])
    assert E.name_row("X", no_entry, PREV)[1] == "no_1530"


def test_bottom_decile_and_placebo():
    names = [{"s": f"S{i:02d}", "rod3": i / 100, "lh": 0.0, "px": 50} for i in range(30)]
    los = E.bottom(names, 0.10)
    assert [r["s"] for r in los] == ["S00", "S01", "S02"]
    assert E.placebo(names, 3, "2026-06-01") == E.placebo(list(reversed(names)), 3, "2026-06-01")


def test_verdict_rules():
    good = [{"lift": 0.001 + 0.0001 * (i % 3), "los": 0.002 + 0.0001 * (i % 2), "k": 20, "spy_lh": 0.0} for i in range(25)]
    v, s = E.half_verdict(good)
    assert v == "PASS"
    assert E.final(["PASS", "PASS"], [s, s]).startswith("PASS (batch")
    bad = [{"lift": -0.001 - 0.0001 * (i % 3), "los": 0.0, "k": 20, "spy_lh": 0.0} for i in range(25)]
    assert E.half_verdict(bad)[0] == "FAIL"
