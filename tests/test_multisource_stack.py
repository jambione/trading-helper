import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "studies"))
sys.path.insert(0, str(ROOT / "tools"))

import multisource_stack as ms  # noqa: E402


def test_s_layer_coverage_not_off():
    assert ms.s_layer(None) is None
    assert ms.s_layer({"valid": False, "tagged": 10, "bull_share": 0.9}) is None
    assert ms.s_layer({"valid": True, "tagged": 4, "bull_share": 0.9}) is None
    assert ms.s_layer({"valid": True, "tagged": 10, "bull_share": 0.7}) is True
    assert ms.s_layer({"valid": True, "tagged": 10, "bull_share": 0.69}) is False


def test_n_layer_window_recaps_and_words():
    ts = 100000.0
    h = [{"t": ts - 100, "headline": "Company wins contract", "n_symbols": 1}]
    assert ms.n_layer(h, ts) is True
    assert ms.n_layer([{"t": ts + 1, "headline": "x", "n_symbols": 1}], ts) is False          # after T: ignored
    assert ms.n_layer([{"t": ts - 100, "headline": "Movers", "n_symbols": 8}], ts) is False   # recap skipped
    assert ms.n_layer(h + [{"t": ts - 50, "headline": "Prices $10M registered direct offering", "n_symbols": 1}], ts) is False
    assert ms.n_layer([{"t": ts - 50, "headline": "Trading halt lifted; shares resume", "n_symbols": 1}], ts) is True
    assert ms.n_layer([{"t": ts - 50, "headline": "atm the open, shares rise", "n_symbols": 1}], ts) is True


def test_ret15_and_pick_etf_and_finra_lag():
    c = {i: 100.0 + i for i in range(0, 60)}
    assert abs(ms.ret15(c, 30) - (129 / 114 - 1)) < 1e-12
    days = [f"2026-0{5 + i // 28}-{i % 28 + 1:02d}" for i in range(70)]
    rng = np.random.default_rng(0)
    x = np.cumsum(rng.normal(0, 0.01, 70))
    daily = {"AAA": {d: float(np.exp(v)) for d, v in zip(days, x)},
             "XLK": {d: float(np.exp(v + rng.normal(0, 0.001))) for d, v in zip(days, x)}}
    for e in ms.ETFS:
        daily.setdefault(e, {d: float(np.exp(v)) for d, v in zip(days, np.cumsum(rng.normal(0, 0.01, 70)))})
    assert ms.pick_etf(daily, "AAA", days[-1]) == "XLK"
    assert ms.pick_etf(daily, "AAA", days[20]) == "SPY"          # < 40 prior sessions
    assert ms.finra_published("2026-11-13") == "2026-11-25"      # 8 business days


def test_matched_diffs_tiers():
    rows = [{"day": "d", "m": 100, "gross": 50.0, "S": True, "N": None, "R": None},
            {"day": "d", "m": 110, "gross": 10.0, "S": False, "N": None, "R": None},
            {"day": "d", "m": 120, "gross": 20.0, "S": False, "N": None, "R": None},
            {"day": "d", "m": 300, "gross": 0.0, "S": True, "N": None, "R": None}]
    out = ms.matched_diffs(rows, ("S",))
    assert out[0] == ("d", 35.0, "30") and out[1] == ("d", None, "unmatched")


def test_hold_ret_same_shape_as_event():
    r = {"opens": {10: 100.0}, "closes": {k: 100.0 + (k - 10) for k in range(10, 40)}}
    assert abs(ms.hold_ret(r, 10) - (129 / 100 - 1) * 1e4) < 1e-9
    r2 = {"opens": {10: 100.0}, "closes": {10: 100.0, 15: 101.0}}
    assert ms.hold_ret(r2, 10) is None          # < 10 minutes of window


def test_market_bucket_and_gaps():
    assert ms.bucket(-0.012) == "down<=-1%" and ms.bucket(0.0) == "flat" and ms.bucket(0.02) == "up>=+1%"
    assert ms.bucket(None) is None
    inp = {"daily": {"SPY": {"2026-05-01": 100.0, "2026-05-04": 101.0}}, "bars": {"2026-05-04": {"SPY": {0: (98.0, 98.5)}}}}
    assert abs(ms.market_gaps(inp)["2026-05-04"] - (98 / 100 - 1)) < 1e-12
