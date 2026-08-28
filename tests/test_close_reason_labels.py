"""Every exit must say why, including the ones the broker performs.

resolve_exit prefers pos["closing_reason"] and otherwise infers from the order
type, where a market fill becomes "flattened". The 15:50 EOD sweep calls
alpaca_trader.liquidate_all directly and never touched desk state, so every
EOD exit landed in that bucket.

"flattened" is therefore not a rule and not homogeneous. On 2026-08-28 it held
BIVI at +1.912R — the best trade in the dataset — beside QS at -0.455R after
being up +0.597R. Grouped by close_reason, analysis can say nothing true about
either while they wear the same word.
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

cp = pytest.importorskip("ai_positions")


def _state(monkeypatch, tmp_path, positions):
    monkeypatch.setattr(cp, "POSITIONS_STATE_PATH", tmp_path / "pos.json")
    cp._save_state(positions)


def test_it_stamps_every_confirmed_position(monkeypatch, tmp_path):
    _state(monkeypatch, tmp_path, {
        "AAA": {"entry_confirmed": True},
        "BBB": {"entry_confirmed": True},
    })
    assert cp.mark_closing_reason("eod_liquidate") == 2
    st = cp._load_state()
    assert st["AAA"]["closing_reason"] == "eod_liquidate"
    assert st["BBB"]["closing_reason"] == "eod_liquidate"


def test_it_does_not_overwrite_a_reason_already_given(monkeypatch, tmp_path):
    """A position already closing for a real reason keeps it — the desk's own
    verdict outranks the sweep that happens to catch it."""
    _state(monkeypatch, tmp_path, {
        "AAA": {"entry_confirmed": True, "closing_reason": "macd_negative"},
    })
    assert cp.mark_closing_reason("eod_liquidate") == 0
    assert cp._load_state()["AAA"]["closing_reason"] == "macd_negative"


def test_it_skips_unconfirmed_entries(monkeypatch, tmp_path):
    """A resting order that never filled has no exit to explain."""
    _state(monkeypatch, tmp_path, {"AAA": {"entry_confirmed": False}})
    assert cp.mark_closing_reason("eod_liquidate") == 0


def test_except_symbols_are_left_alone(monkeypatch, tmp_path):
    """H4 swings survive 15:50 — overnight is the trade."""
    _state(monkeypatch, tmp_path, {
        "AAA": {"entry_confirmed": True},
        "H4X": {"entry_confirmed": True},
    })
    assert cp.mark_closing_reason("eod_liquidate", except_symbols={"H4X"}) == 1
    assert "closing_reason" not in cp._load_state()["H4X"]


def test_the_eod_path_labels_before_it_liquidates():
    """Order matters: liquidate_all goes to the broker, and a label written
    afterwards would race the reconcile that records the outcome."""
    src = (_ROOT / "ai_trader.py").read_text(encoding="utf-8")
    i = src.index('mark_closing_reason("eod_liquidate"')
    j = src.index("liquidate_all(except_symbols=keep)")
    assert i < j, "the label must be stamped before the broker closes anything"


def test_the_label_failing_cannot_stop_the_liquidation():
    """Flattening the book at 15:50 is protection. A bookkeeping call must
    never be able to prevent it."""
    src = (_ROOT / "ai_trader.py").read_text(encoding="utf-8")
    i = src.index('mark_closing_reason("eod_liquidate"')
    assert "try:" in src[i - 120:i]
    assert "except Exception" in src[i:i + 240]
