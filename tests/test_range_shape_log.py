"""_range_shape_fields: observe-only entry range position / rise from the in-memory bar store."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_entry_watch as ew  # noqa: E402
import ob_observe  # noqa: E402

T = 1_793_900_000.0 - (1_793_900_000.0 % 60)


def _bars(closes, end=T - 60):
    n = len(closes)
    return [(end - 60 * (n - 1 - i), c, c + 0.02, c - 0.02, c, 1000.0) for i, c in enumerate(closes)]


def test_top_of_a_run_up(monkeypatch):
    closes = [10.0 + 0.01 * i for i in range(60)]          # steady climb
    monkeypatch.setattr(ob_observe, "bars", lambda s: _bars(closes))
    out = ew._range_shape_fields("ABC", closes[-1] + 0.01, T + 20, {"ai_watch_range_shape_log": True})
    assert out["rs_pos15"] >= 0.9 and out["rs_pos30"] >= 0.9
    assert out["rs_rise15_bp"] > 0 and out["rs_er15"] > 0.9 and out["rs_bars"] == 60


def test_bottom_of_a_range(monkeypatch):
    closes = [10.3 - 0.01 * i for i in range(60)]          # steady fall, entry at the low
    monkeypatch.setattr(ob_observe, "bars", lambda s: _bars(closes))
    out = ew._range_shape_fields("ABC", closes[-1] - 0.01, T + 20, {})
    assert out["rs_pos15"] <= 0.1 and out["rs_rise15_bp"] < 0


def test_open_bar_and_gaps_are_ignored(monkeypatch):
    closes = [10.0] * 60
    b = _bars(closes) + [(T + 5, 99.0, 99.0, 99.0, 99.0, 1.0)]   # a bar starting AFTER the entry minute began
    monkeypatch.setattr(ob_observe, "bars", lambda s: b)
    out = ew._range_shape_fields("ABC", 10.0, T + 20, {})
    assert out["rs_rng15_bp"] < 50                          # the 99.0 bar must not widen the range
    stale = _bars(closes, end=T - 600)
    monkeypatch.setattr(ob_observe, "bars", lambda s: stale)
    assert ew._range_shape_fields("ABC", 10.0, T + 20, {}) == {}


def test_off_short_and_error_return_empty(monkeypatch):
    monkeypatch.setattr(ob_observe, "bars", lambda s: _bars([10.0] * 60))
    assert ew._range_shape_fields("ABC", 10.0, T + 20, {"ai_watch_range_shape_log": False}) == {}
    monkeypatch.setattr(ob_observe, "bars", lambda s: _bars([10.0] * 10))
    assert ew._range_shape_fields("ABC", 10.0, T + 20, {}) == {}
    monkeypatch.setattr(ob_observe, "bars", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
    assert ew._range_shape_fields("ABC", 10.0, T + 20, {}) == {}
    assert ew._range_shape_fields("", 10.0, T, {}) == {}
