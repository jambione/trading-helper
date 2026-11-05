"""_wr_slope_fields (operator 11/04): observe-only %R slope at entry. Matches tools/sell_double_check, empty when it
cannot be measured, never raises, and nothing in the arm path reads it."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_entry_watch as ew  # noqa: E402
import ob_observe  # noqa: E402
from tools import sell_double_check as sdc  # noqa: E402

T0 = 1_793_800_000.0


def _bars(prices, end=T0 - 90):
    """(ts, open, high, low, close, vol) 1-minute bars, last closing just before T0."""
    n = len(prices)
    return [(end - 60 * (n - 1 - i), p, p + 0.02, p - 0.02, p, 1000.0) for i, p in enumerate(prices)]


def _put(monkeypatch, prices):
    monkeypatch.setattr(ob_observe, "bars", lambda s: _bars(prices))


def test_flat_tape_has_flat_lines(monkeypatch):
    _put(monkeypatch, [50.0 + 0.001 * (i % 2) for i in range(300)])
    out = ew._wr_slope_fields("ABC", 50.0, T0, {})
    assert out and abs(out["wr_d5_fast"]) < 6 and abs(out["wr_d5_slow"]) < 6
    assert set(out) >= {"wr_fast_now", "wr_slow_now", "wr_d2_fast", "wr_d10_slow", "wr_bar_age_sec"}


def test_a_surge_reads_as_a_rising_fast_line(monkeypatch):
    # a dip pulls the fast line toward -100, then a sharp rise sends it back up (SE's shape on 11/04)
    prices = [50.0] * 270 + [50.0 - 0.05 * i for i in range(1, 21)] + [49.0 + 0.12 * i for i in range(1, 11)]
    _put(monkeypatch, prices)
    out = ew._wr_slope_fields("ABC", prices[-1] + 0.05, T0, {})
    assert out["wr_d5_fast"] > 10 and out["wr_d10_fast"] > 10 and out["wr_d2_fast"] > 0


def test_matches_the_double_check_windows(monkeypatch):
    prices = [50.0 + 0.01 * ((i * 7) % 13) for i in range(300)]
    _put(monkeypatch, prices)
    b = _bars(prices)
    rows = sdc.closed_rows([x[0] for x in b], [x[2] for x in b], [x[3] for x in b], [x[4] for x in b], T0)
    fw = sdc.line_window(rows, 50.05, sdc.FAST_LENGTH, sdc.FAST_SPAN, 11)
    out = ew._wr_slope_fields("ABC", 50.05, T0, {})
    assert out["wr_d5_fast"] == pytest.approx(round(fw[-1] - fw[-6], 2), abs=0.011)
    assert out["wr_fast_now"] == pytest.approx(round(fw[-1], 2), abs=0.011)


def test_empty_when_short_off_or_broken(monkeypatch):
    _put(monkeypatch, [50.0] * 50)
    assert ew._wr_slope_fields("ABC", 50.0, T0, {}) == {}
    _put(monkeypatch, [50.0] * 300)
    assert ew._wr_slope_fields("ABC", 50.0, T0, {"ai_watch_wr_slope_log": False}) == {}
    assert ew._wr_slope_fields("", 50.0, T0, {}) == {} and ew._wr_slope_fields("ABC", None, T0, {}) == {}
    monkeypatch.setattr(ob_observe, "bars", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
    assert ew._wr_slope_fields("ABC", 50.0, T0, {}) == {}


def test_nothing_reads_the_slope_keys():
    import re
    src = Path(ew.__file__).read_text() + Path(ew.__file__).with_name("ai_positions.py").read_text()
    # observe-only: no gate looks one up (they are only written, into the entry features)
    assert not re.search(r'(\.get\(|\[)\s*["\']wr_(d\d+_(fast|slow)|fast_now|slow_now|bar_age_sec)', src)


def test_defaults_on_in_config_and_off_switch_exists():
    import config
    assert config.DEFAULT_CONFIG["ai_watch_wr_slope_log"] is True


def test_wr_steep_refusal_is_off_by_default_and_fails_open():
    import ai_entry_watch as ew
    f = {"wr_d5_fast": 42.0}
    assert ew.wr_steep_refusal(f, {}) is None                                   # default 0 = off
    assert ew.wr_steep_refusal(f, {"ai_watch_wr_steep_block_pts": 30.0}) == "d5 42>=30"
    assert ew.wr_steep_refusal({"wr_d5_fast": 29.9}, {"ai_watch_wr_steep_block_pts": 30.0}) is None
    assert ew.wr_steep_refusal({}, {"ai_watch_wr_steep_block_pts": 30.0}) is None   # no reading never refuses
    assert ew.wr_steep_refusal(None, {"ai_watch_wr_steep_block_pts": 30.0}) is None
    assert ew.wr_steep_refusal({"wr_d5_fast": "x"}, {"ai_watch_wr_steep_block_pts": 30.0}) is None
