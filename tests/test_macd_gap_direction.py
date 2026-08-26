"""Is the MACD gap opening or closing? Size alone cannot say.

Every test in macd_allows_buy measures how far APART the fast and slow
lines are — bullish sign, an absolute floor, a multiple of the histogram's
own rolling std. None of them says which way the lines are moving, so a
+0.03 gap that was +0.08 two bars ago passes all of them while the momentum
the entry is meant to ride is already over. Entering that buys the fade.

Same distinction cm_rsi_rising draws for RSI, on the same trend_lookback.

A FLAT gap is deliberately allowed: the operator's rule is "if it is
trending towards closing we don't want to open", and flat is not closing.
Tightening that to "must be actively widening" would be a second, stricter
knob rather than a reinterpretation of this one.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402

ON = {"ai_watch_arm_require_macd": True, "ai_watch_macd_block_narrowing": True}
OFF = {"ai_watch_arm_require_macd": True, "ai_watch_macd_block_narrowing": False}


def _rec(**ind):
    base = {"macd_fast": 0.10, "macd_slow": 0.05, "macd_gap": 0.05}
    base.update(ind)
    return {"symbol": "AAA", "indicator": base}


# ── the rule ─────────────────────────────────────────────────────────────

def test_a_closing_gap_is_refused():
    rec = _rec(macd_gap_rising=False, macd_gap_falling=True,
               macd_gap_prev=0.08)
    ok, why = ew.macd_allows_buy(rec, ON)
    assert ok is False
    assert why == "macd_gap_narrowing"
    assert "0.08" in str(rec.get("block_detail"))
    assert "0.05" in str(rec.get("block_detail"))


def test_an_opening_gap_passes():
    ok, why = ew.macd_allows_buy(
        _rec(macd_gap_rising=True, macd_gap_falling=False, macd_gap_prev=0.02),
        ON)
    assert ok is True
    assert why == "macd_bullish_gap"


def test_a_flat_gap_passes_because_flat_is_not_closing():
    ok, why = ew.macd_allows_buy(
        _rec(macd_gap_rising=False, macd_gap_falling=False), ON)
    assert ok is True
    assert why == "macd_bullish_gap"


def test_unknown_direction_is_refused_not_waved_through():
    """Too few bars for the lookback. Absence is not a pass — the same rule
    the rest of this desk runs on."""
    rec = _rec(macd_gap_rising=None, macd_gap_falling=None)
    ok, why = ew.macd_allows_buy(rec, ON)
    assert ok is False
    assert why == "macd_gap_dir_unknown"


def test_a_missing_direction_field_is_also_unknown():
    """An engine that has not published the field yet must not read as flat."""
    ok, why = ew.macd_allows_buy(_rec(), ON)
    assert ok is False
    assert why == "macd_gap_dir_unknown"


# ── it is opt-in and does not disturb the size tests ─────────────────────

def test_off_by_default_keeps_the_size_only_behaviour():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_macd_block_narrowing"] is False
    ok, why = ew.macd_allows_buy(
        _rec(macd_gap_rising=False, macd_gap_falling=True, macd_gap_prev=0.08),
        OFF)
    assert ok is True, "a closing gap still passes when the knob is off"
    assert why == "macd_bullish_gap"


def test_direction_is_checked_last_so_size_refusals_keep_their_reason():
    """A bearish name must report macd_bearish, not macd_gap_narrowing —
    the State column has to name the first thing that is wrong."""
    rec = {"symbol": "AAA", "indicator": {
        "macd_fast": 0.01, "macd_slow": 0.05, "macd_gap": -0.04,
        "macd_gap_rising": False, "macd_gap_falling": True}}
    ok, why = ew.macd_allows_buy(rec, ON)
    assert ok is False
    assert why == "macd_bearish"


def test_a_gap_under_the_floor_still_reports_the_floor():
    rec = {"symbol": "AAA", "indicator": {
        "macd_fast": 0.10, "macd_slow": 0.099, "macd_gap": 0.001,
        "macd_gap_rising": False, "macd_gap_falling": True}}
    ok, why = ew.macd_allows_buy(rec, ON)
    assert ok is False
    assert why == "macd_gap_too_close"


# ── the wire carries it ──────────────────────────────────────────────────

def test_wire_fields_carry_direction_and_the_previous_gap():
    got = ew._macd_wire_fields(_rec(
        macd_gap_rising=True, macd_gap_falling=False, macd_gap_prev=0.02,
        macd_sep_ratio=1.4))
    assert got["macd_gap"] == 0.05
    assert got["macd_gap_rising"] is True
    assert got["macd_gap_falling"] is False
    assert got["macd_gap_prev"] == 0.02
    assert got["macd_sep_ratio"] == 1.4


def test_wire_fields_keep_unknown_direction_as_none_not_false():
    """False means "not widening"; None means "cannot say". Collapsing them
    would let the arm gate treat a too-short series as a held gap."""
    got = ew._macd_wire_fields(_rec())
    assert got["macd_gap_rising"] is None
    assert got["macd_gap_falling"] is None


def test_wire_fields_survive_a_record_with_no_indicator():
    got = ew._macd_wire_fields({"symbol": "AAA"})
    assert got["macd_gap"] is None
    assert got["macd_gap_rising"] is None
    assert got["macd_bull"] is False


def test_snapshot_actually_ships_the_macd_column(tmp_path, monkeypatch):
    """The redesign added the column, the renderer, the CSS and the gate but
    never put the numbers on the wire, so every row rendered "—" while the
    engine held real values."""
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew.save_watch({"AAA": {
        "symbol": "AAA", "status": "watching", "last_ask": 10.0,
        "last_ask_src": "rest", "last_ask_age_sec": 1.0,
        "indicator": {"macd_fast": 0.10, "macd_slow": 0.05, "macd_gap": 0.05,
                      "macd_gap_rising": True, "macd_gap_falling": False},
        "structure": {"entry_low": 9.0, "entry_high": 11.0,
                      "stop_price": 8.5},
    }})
    row = ew.public_snapshot()[0]
    assert row["macd_gap"] == 0.05
    assert row["macd_gap_rising"] is True


# ── the record has to actually receive the numbers ───────────────────────

def test_refresh_stamps_the_full_macd_set_onto_the_record():
    """The 8/26 redesign made MACD the entry lever and the poll's indicator
    whitelist copied only macd_ok — and that dict REPLACES the previous map,
    so macd_allows_buy looked for macd_gap on a record that had just had it
    wiped. Every name read "no_macd_data" while the engine held +0.0425.
    """
    rec = {"symbol": "AAA", "indicator": {"cm_rsi": 40.0}}
    wrote = ew.refresh_engine_macd(rec, {
        "macd_gap": 0.0425, "macd_fast": 0.11, "macd_slow": 0.0675,
        "macd_sep_ratio": 2.15, "macd_bull": True, "macd_cross": False,
        "macd_gap_rising": True, "macd_gap_falling": False,
        "macd_gap_prev": 0.03,
    })
    assert wrote is True
    ind = rec["indicator"]
    assert ind["macd_gap"] == 0.0425
    assert ind["macd_fast"] == 0.11 and ind["macd_slow"] == 0.0675
    assert ind["macd_sep_ratio"] == 2.15
    assert ind["macd_gap_rising"] is True
    assert ind["cm_rsi"] == 40.0, "must not clobber the rest of the map"


def test_refresh_leaves_the_record_alone_when_the_engine_has_no_gap():
    """"Not computed yet" is not "the gap is gone" — blanking a good reading
    on a cold engine would flip a live name to no_macd_data."""
    rec = {"symbol": "AAA", "indicator": {"macd_gap": 0.05}}
    assert ew.refresh_engine_macd(rec, {"macd_bull": True}) is False
    assert rec["indicator"]["macd_gap"] == 0.05


def test_refresh_accepts_macd_hist_as_the_gap():
    rec = {"symbol": "AAA"}
    assert ew.refresh_engine_macd(rec, {"macd_hist": -0.02}) is True
    assert rec["indicator"]["macd_gap"] == -0.02


def test_refresh_is_a_no_op_on_junk():
    assert ew.refresh_engine_macd(None, {"macd_gap": 1.0}) is False
    assert ew.refresh_engine_macd({"symbol": "AAA"}, None) is False


def test_the_poll_whitelist_carries_the_levels_not_just_the_verdict():
    """Pinned as source text: that dict replaces the indicator map wholesale,
    so a field missing from it is a field the gate will never see."""
    import pathlib
    src = pathlib.Path(ew.__file__).read_text(encoding="utf-8")
    i = src.index('"macd_ok": sig.get("macd_ok"),')
    body = src[i:i + 1400]
    for k in ("macd_gap", "macd_fast", "macd_slow", "macd_sep_ratio",
              "macd_gap_rising", "macd_gap_falling"):
        assert f'"{k}": sig.get("{k}")' in body, f"{k} missing from the whitelist"
