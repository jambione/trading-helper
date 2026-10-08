"""Overnight book vs SPY / RSP at the same crosses (operator 10/8: 'it is not profitable')."""
import overnight_book as ob


def test_benchmark_bp_and_excess():
    close = {"SPY": (500.0, 1), "RSP": (180.0, 1)}
    opn = {"SPY": (498.5, 1), "RSP": (180.9, 1)}
    b = ob.benchmark_bp(close, opn)
    assert b == {"spy_bp": -30.0, "rsp_bp": 50.0}
    n = {"mean_bp_book": -194.9, **b}
    assert ob.night_excess(n) == {"vs_spy_bp": -164.9, "vs_rsp_bp": -244.9}


def test_missing_cross_gives_none_never_zero():
    assert ob.benchmark_bp({"SPY": (500.0, 1)}, {}) == {"spy_bp": None, "rsp_bp": None}
    assert ob.night_excess({"mean_bp_book": 10.0, "spy_bp": None, "rsp_bp": None}) == {"vs_spy_bp": None, "vs_rsp_bp": None}


def test_benchmarks_are_priced_with_the_book():
    src = open(ob.__file__).read()
    assert 'fetch(picks + list(BENCHMARKS), buy_day, "close")' in src and "night.update(benchmark_bp(close_cx, open_cx))" in src
