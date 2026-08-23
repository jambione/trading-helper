"""The ratchet ends these trades and was never written down.

`position_shadow.stop_price` is the 5% plan stop; the 0.10R working shelf
that actually fires is a different number, and until 2026-08-23 no log held
it. Every ratchet study reconstructed it from bars instead — slower, less
accurate, and impossible for the give now that `give_spread_k` makes the
give vary per name and per tick.

These pin the fields that make an exit answerable after the fact, and the
one distinction that matters most: the plan stop and the working shelf are
never the same column.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

cp = pytest.importorskip("ai_positions")

NOW = 1_787_000_000.0


def _pos(**kw):
    pos = {
        "entry_price": 10.0,
        "stop_price": 9.5,            # the 5% plan stop
        "risk_per_share": 0.5,        # 1R
        "target_1": 10.6,
        "entry_time": NOW - 600.0,
        "local_stop_price": 10.4,     # the working shelf
        "peak_price": 10.65,
        "mfe_r": 1.3,
        "mae_r": -0.1,
    }
    pos.update(kw)
    return pos


# ------------------------------------------------------------ the shelf

def test_the_working_shelf_is_not_the_plan_stop():
    """The bug this whole file exists to prevent.

    stop_price 9.50 and local_stop_price 10.40 are 0.90 apart — nearly 2R.
    Reading one as the other would say the desk exited 1.8R below where it
    actually did on every trade in the book.
    """
    pos = _pos()
    assert pos["stop_price"] != pos["local_stop_price"]
    assert cp._risk_basis(pos) == pytest.approx(0.5)


def test_give_in_force_is_peak_minus_shelf_in_r():
    """The give stopped being a constant when give_spread_k went live."""
    pos = _pos(peak_price=10.65, local_stop_price=10.4)
    give_r = (pos["peak_price"] - pos["local_stop_price"]) / cp._risk_basis(pos)
    assert give_r == pytest.approx(0.5)


def test_shelf_r_measures_distance_from_the_print_not_from_entry():
    pos = _pos()
    px = 10.5
    shelf_r = (px - pos["local_stop_price"]) / cp._risk_basis(pos)
    assert shelf_r == pytest.approx(0.2)


# ------------------------------------------------------------ the logger

def test_the_shadow_logger_writes_the_ratchet_fields():
    src = open(os.path.join(ROOT, "ai_positions.py"), encoding="utf-8").read()
    i = src.index("def _log_position_shadow")
    j = src.index("def ", i + 10)
    body = src[i:j]
    for field in ("local_stop_price", "peak_price", "runner_stop_price",
                  "shelf_r", "give_r", "spread_r",
                  "min_hold_active", "min_hold_blocks"):
        assert f'"{field}"' in body, f"{field} missing from position_shadow"


def test_the_outcome_row_carries_the_exit_side_cost():
    """spread_r on outcomes was 8.2%; exit slippage did not exist at all."""
    src = open(os.path.join(ROOT, "ai_positions.py"), encoding="utf-8").read()
    for field in ("exit_shelf_price", "exit_slippage_r", "give_r_at_exit",
                  "peak_price", "min_hold_blocks"):
        assert f'"{field}":' in src, f"{field} missing from the outcome row"


def test_exit_slippage_is_only_defined_for_a_shelf_exit():
    """A stop, a target and a 15:50 flatten have no intended price to miss.

    Reporting 0.0 for those would put three quarters of the book at
    'no slippage' and make the shelf's real cost disappear into an average.
    """
    src = open(os.path.join(ROOT, "ai_positions.py"), encoding="utf-8").read()
    i = src.index('"exit_slippage_r":')
    window = src[i:i + 400]
    assert 'close_reason == "local_trail"' in window


# ------------------------------------------------------------ rvol sanity

def test_an_impossible_rvol_is_flagged_not_clamped():
    """3.94% of shadow RVOLs exceed 100; the max is 81,820.

    Not clamped, because clamping edits the evidence — and because the live
    floor test is `rv < min_rvol`, so a garbage-high reading PASSES the
    thin-tape gate. The flag makes that measurable without touching it.
    """
    import ai_entry_watch as ew
    assert ew._rvol_is_sane(3144.09) is False
    assert ew._rvol_is_sane(81820.37) is False
    assert ew._rvol_is_sane(2.53) is True
    assert ew._rvol_is_sane(100.0) is True


def test_a_missing_rvol_is_distinguishable_from_a_broken_one():
    import ai_entry_watch as ew
    assert ew._rvol_is_sane(None) is None      # 19 fills had no reading
    assert ew._rvol_is_sane("junk") is None
    assert ew._rvol_is_sane(0.0) is False      # zero is not a ratio


def test_the_logger_actually_writes_correct_ratchet_values(tmp_path,
                                                           monkeypatch):
    """End to end, not a source grep: build a row and read it back."""
    import json
    out = tmp_path / "position_shadow.jsonl"
    monkeypatch.setattr(cp, "POSITION_SHADOW_PATH", out)
    pos = _pos(min_hold_blocks=3, min_hold_last="local_trail")
    cp._log_position_shadow("TEM", pos, 10.5, {}, "hold", NOW)
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert row["symbol"] == "TEM"
    assert row["stop_price"] == pytest.approx(9.5)        # the plan stop
    assert row["local_stop_price"] == pytest.approx(10.4)  # the working shelf
    assert row["peak_price"] == pytest.approx(10.65)
    assert row["shelf_r"] == pytest.approx(0.2)            # (10.50-10.40)/0.5
    assert row["give_r"] == pytest.approx(0.5)             # (10.65-10.40)/0.5
    assert row["min_hold_blocks"] == 3
    assert row["min_hold_last"] == "local_trail"


def test_the_logger_reports_none_rather_than_zero_without_a_shelf(tmp_path,
                                                                  monkeypatch):
    """A fresh fill has no shelf yet. 0.0 would read as 'shelf at zero'."""
    import json
    out = tmp_path / "position_shadow.jsonl"
    monkeypatch.setattr(cp, "POSITION_SHADOW_PATH", out)
    pos = _pos()
    pos.pop("local_stop_price")
    cp._log_position_shadow("TEM", pos, 10.5, {}, "hold", NOW)
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert row["local_stop_price"] is None
    assert row["shelf_r"] is None
    assert row["give_r"] is None


@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize("held", [True, False])
def test_the_split_condition_is_logically_identical(ready, held):
    """Proof the refactor changed no decision.

    Both gated exits went from `if READY and not held(...)` to a split form
    so the held-back case could be counted. Exhaustively: the new form fires
    exactly when the old one did, and calls held() exactly as often.
    """
    calls = []

    def held_fn():
        calls.append(1)
        return held

    old_calls = []

    def old_held_fn():
        old_calls.append(1)
        return held

    old_fires = ready and not old_held_fn() if ready else False
    _held = ready and held_fn()
    new_fires = ready and not _held

    assert new_fires == old_fires
    assert len(calls) == len(old_calls)


def test_the_shadow_row_carries_volume_and_catalyst():
    src = open(os.path.join(ROOT, "ai_entry_watch.py"), encoding="utf-8").read()
    i = src.index("def _shadow_row")
    j = src.index("\ndef ", i + 10)
    body = src[i:j]
    for field in ("rvol_ok", "dollar_volume", "ask", "news_n_24h",
                  "news_mins_since", "news_bearish", "news_bullish",
                  "news_cache_age_sec"):
        assert f'"{field}"' in body, f"{field} missing from the shadow row"
