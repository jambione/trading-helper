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
    # macd_sep_ratio is part of the baseline: the separation test is the
    # strategy, and a record without one is now refused as macd_sep_unknown
    # rather than passed silently. 1.5 clears the 0.8 default multiple, so
    # these fixtures exercise the direction rule and not the size rule.
    base = {"macd_fast": 0.10, "macd_slow": 0.05, "macd_gap": 0.05,
            "macd_sep_ratio": 1.5}
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


# ── the separation rule the strategy is named for ────────────────────────

def test_a_narrow_separation_is_refused():
    """gap >= sep_mult * std  <=>  ratio >= sep_mult. The gate used to read
    macd_hist_std, which the engine does not publish — it puts the finished
    quotient on the wire as macd_sep_ratio — so `std` was None on every
    symbol and this check silently never ran."""
    rec = _rec(macd_sep_ratio=0.4, macd_gap_rising=True, macd_gap_falling=False)
    ok, why = ew.macd_allows_buy(rec, ON)
    assert ok is False
    assert why == "macd_gap_insufficient"
    assert "0.40" in str(rec.get("block_detail"))


def test_a_wide_separation_passes():
    ok, why = ew.macd_allows_buy(
        _rec(macd_sep_ratio=4.25, macd_gap_rising=True, macd_gap_falling=False),
        ON)
    assert ok is True and why == "macd_bullish_gap"


def test_the_ratio_is_read_at_the_configured_multiple():
    cfg = dict(ON, macd_sep_mult=2.0)
    base = dict(macd_gap_rising=True, macd_gap_falling=False)
    assert ew.macd_allows_buy(_rec(macd_sep_ratio=1.5, **base), cfg)[1] == (
        "macd_gap_insufficient")
    assert ew.macd_allows_buy(_rec(macd_sep_ratio=2.5, **base), cfg)[0] is True


def test_a_raw_std_is_still_honoured_if_a_producer_publishes_one():
    rec = {"symbol": "AAA", "indicator": {
        "macd_fast": 0.10, "macd_slow": 0.05, "macd_gap": 0.05,
        "macd_hist_std": 0.20,          # 0.8 * 0.20 = 0.16 > 0.05
        "macd_gap_rising": True, "macd_gap_falling": False}}
    ok, why = ew.macd_allows_buy(rec, ON)
    assert ok is False and why == "macd_gap_insufficient"


def test_no_separation_reading_at_all_is_refused_not_passed():
    """This is the bug: with neither field the whole check short-circuited
    and the live rule collapsed to a bare gap >= macd_min_gap."""
    rec = {"symbol": "AAA", "indicator": {
        "macd_fast": 0.10, "macd_slow": 0.05, "macd_gap": 0.05,
        "macd_gap_rising": True, "macd_gap_falling": False}}
    ok, why = ew.macd_allows_buy(rec, ON)
    assert ok is False
    assert why == "macd_sep_unknown"


def test_zeroing_the_multiple_does_NOT_disable_the_separation_test():
    """Footgun, pinned rather than "fixed" without being asked for.

    The knob is read as `float(cfg.get("macd_sep_mult", 0.8) or 0.8)`, and
    0 is falsy — so setting macd_sep_mult to 0 in bot_config.json silently
    restores 0.8 instead of turning the check off. Several knobs on this
    desk document "0 disables"; this one does the opposite, quietly. Set it
    to a tiny positive number if you actually want it out of the way.
    """
    cfg = dict(ON, macd_sep_mult=0)
    ok, why = ew.macd_allows_buy(
        _rec(macd_sep_ratio=0.01, macd_gap_rising=True,
             macd_gap_falling=False), cfg)
    assert ok is False, "0 does not disable — it re-reads as the 0.8 default"
    assert why == "macd_gap_insufficient"

    ok2, _ = ew.macd_allows_buy(
        _rec(macd_sep_ratio=0.01, macd_gap_rising=True,
             macd_gap_falling=False), dict(ON, macd_sep_mult=1e-9))
    assert ok2 is True, "a tiny positive multiple is how you stand it down"


# ── the EXH confluence override ──────────────────────────────────────────

OVR = dict(ON, ai_watch_macd_exh_override=True,
           ai_watch_macd_exh_override_min_pct=70.0)


def _ovr_rec(pctr, *, rising=True, exh_rising=True, gap=0.0004, sep=0.05):
    """A gap far too small for either size test, so only the override can
    pass it. pctr is the raw %R; exhaustion_pct is 100 + pctr."""
    return {"symbol": "AAA", "indicator": {
        "macd_fast": 0.10, "macd_slow": 0.10 - gap, "macd_gap": gap,
        "macd_sep_ratio": sep,
        "macd_gap_rising": rising, "macd_gap_falling": not rising,
        "pctr": pctr, "pctr_rising": exh_rising,
        "pctr_falling": not exh_rising,
    }}


def test_confluence_opens_at_any_gap():
    """The operator's rule: MACD open and trending at ANY gap, with EXH
    rising past 70, is an automatic yes. This gap is 0.0004 — an order of
    magnitude under macd_min_gap, and 0.05x separation."""
    rec = _ovr_rec(-25.0)                       # EXH 75%
    ok, why = ew.macd_allows_buy(rec, OVR)
    assert ok is True
    assert why == "macd_exh_confluence"
    assert "75.0%" in str(rec.get("block_detail"))


def test_the_same_row_is_refused_without_the_override():
    ok, why = ew.macd_allows_buy(_ovr_rec(-25.0), ON)
    assert ok is False
    assert why == "macd_gap_too_close"


def test_exh_below_the_threshold_does_not_override():
    ok, why = ew.macd_allows_buy(_ovr_rec(-35.0), OVR)   # EXH 65%
    assert ok is False and why == "macd_gap_too_close"


def test_both_lines_must_be_turning_up_macd_side():
    """A closing MACD gap cannot be overridden into a buy."""
    ok, why = ew.macd_allows_buy(_ovr_rec(-25.0, rising=False), OVR)
    assert ok is False
    assert why in ("macd_gap_too_close", "macd_gap_narrowing")


def test_both_lines_must_be_turning_up_exh_side():
    """A %R at 85 that is ROLLING OVER is a top, not a confirmation — the
    operator's own setup calls that where the profit gain stops."""
    ok, why = ew.macd_allows_buy(_ovr_rec(-15.0, exh_rising=False), OVR)
    assert ok is False and why == "macd_gap_too_close"


def test_the_override_cannot_rescue_a_bearish_macd():
    """"Open" means the lines are apart. A negative gap is not a narrow one,
    and no amount of EXH makes it bullish."""
    rec = _ovr_rec(-10.0)
    rec["indicator"].update({"macd_fast": 0.01, "macd_slow": 0.05,
                             "macd_gap": -0.04})
    ok, why = ew.macd_allows_buy(rec, OVR)
    assert ok is False and why == "macd_bearish"


def test_missing_exh_does_not_override():
    rec = _ovr_rec(-25.0)
    rec["indicator"]["pctr"] = None
    ok, why = ew.macd_allows_buy(rec, OVR)
    assert ok is False and why == "macd_gap_too_close"


def test_the_threshold_is_configurable():
    cfg = dict(OVR, ai_watch_macd_exh_override_min_pct=90.0)
    assert ew.macd_allows_buy(_ovr_rec(-25.0), cfg)[0] is False   # 75 < 90
    assert ew.macd_allows_buy(_ovr_rec(-5.0), cfg)[0] is True     # 95 >= 90


def test_override_is_off_by_default():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_macd_exh_override"] is False
    assert DEFAULT_CONFIG["ai_watch_macd_exh_override_min_pct"] == 70.0


def test_a_wide_healthy_gap_still_passes_on_its_own_merits():
    """The override adds a path; it must not become the only one."""
    ok, why = ew.macd_allows_buy(
        _rec(macd_sep_ratio=2.0, macd_gap_rising=True, macd_gap_falling=False),
        OVR)
    assert ok is True and why == "macd_bullish_gap"


# ── provenance: which tape drew this reading ─────────────────────────────

RT = dict(ON, ai_watch_require_realtime_macd=True)


def test_a_rest_fallback_reading_is_refused():
    """MACD became the entry lever with no provenance check while the levers
    it replaced both had one. bars_src flips per ticker mid-session, so an
    ungated gate alternates between the Finnhub tape (0.3s median, measured
    8/26) and the Alpaca REST fallback (up to 60s) without saying which."""
    rec = _rec(macd_gap_rising=True, macd_gap_falling=False,
               macd_src="alpaca")
    ok, why = ew.macd_allows_buy(rec, RT)
    assert ok is False
    assert why == "macd_not_realtime_alpaca"
    assert "alpaca" in str(rec.get("block_detail"))


def test_a_realtime_reading_passes():
    ok, why = ew.macd_allows_buy(
        _rec(macd_gap_rising=True, macd_gap_falling=False,
             macd_src="realtime", macd_age_sec=0.3), RT)
    assert ok is True and why == "macd_bullish_gap"


def test_unknown_provenance_is_refused():
    """Absence is not a pass — the rule everywhere else on this desk."""
    ok, why = ew.macd_allows_buy(
        _rec(macd_gap_rising=True, macd_gap_falling=False), RT)
    assert ok is False and why == "macd_src_unknown"


def test_an_age_ceiling_is_optional_and_off_by_default():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_macd_max_age_sec"] == 0.0
    # 0 = source check only: a realtime reading of any age still passes.
    ok, _ = ew.macd_allows_buy(
        _rec(macd_gap_rising=True, macd_gap_falling=False,
             macd_src="realtime", macd_age_sec=999.0), RT)
    assert ok is True


def test_the_age_ceiling_bites_when_set():
    cfg = dict(RT, ai_watch_macd_max_age_sec=10.0)
    ok, why = ew.macd_allows_buy(
        _rec(macd_gap_rising=True, macd_gap_falling=False,
             macd_src="realtime", macd_age_sec=42.0), cfg)
    assert ok is False and why == "macd_stale_bars"


def test_provenance_is_checked_before_the_size_tests():
    """A fallback reading must not be reported as a narrow gap — the State
    column has to name the real problem, which is the feed."""
    rec = _rec(macd_gap=0.0001, macd_sep_ratio=0.01,
               macd_gap_rising=True, macd_gap_falling=False,
               macd_src="alpaca")
    ok, why = ew.macd_allows_buy(rec, RT)
    assert ok is False and why == "macd_not_realtime_alpaca"


def test_the_guard_is_off_by_default():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_require_realtime_macd"] is False
    ok, _ = ew.macd_allows_buy(
        _rec(macd_gap_rising=True, macd_gap_falling=False,
             macd_src="alpaca"), ON)
    assert ok is True, "a fallback reading still passes when the guard is off"


def test_refresh_stamps_provenance_onto_the_record():
    rec = {"symbol": "AAA"}
    ew.refresh_engine_macd(rec, {"macd_gap": 0.02, "bars_src": "realtime",
                                 "bars_age_sec": 0.4})
    assert rec["indicator"]["macd_src"] == "realtime"
    assert rec["indicator"]["macd_age_sec"] == 0.4


# ── provenance survives the whitelist that REPLACES the indicator map ───────
#
# 2026-08-27. ai_watch_require_realtime_macd refuses a reading with no
# provenance. The poll's indicator dict is a wholesale replacement, and it
# carried cm_rsi_src/cm_rsi_age_sec but not the MACD pair — so the guard read
# an empty map on every row, answered macd_src_unknown, and no name could
# open. The book showed "MACD src?" against rows whose engine state said
# bars_src="realtime". Third field lost to this whitelist (macd_gap, pctr_src).

def test_the_poll_whitelist_carries_macd_provenance():
    """Source-pinned: the failure is invisible in unit tests that build
    records by hand, because the dropped keys are dropped in transport."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "ai_entry_watch.py"
    body = src.read_text(encoding="utf-8")
    i = body.index('"cm_rsi_src": sig.get("bars_src")')
    block = body[i:body.index('"ts": t0,', i)]
    assert '"macd_src": sig.get("bars_src")' in block
    assert '"macd_age_sec": sig.get("bars_age_sec")' in block


def test_a_record_with_provenance_is_not_refused_for_lacking_it():
    """The end the operator sees: same record, guard on, no macd_src_unknown."""
    rec = _rec(macd_gap_rising=True, macd_gap_falling=False,
               macd_gap_prev=0.02, macd_src="realtime", macd_age_sec=0.4)
    ok, why = ew.macd_allows_buy(rec, RT)
    assert why != "macd_src_unknown"
    assert ok, why


def test_the_same_record_without_provenance_is_still_refused():
    """The guard must stay real — this is the half that must NOT regress."""
    rec = _rec(macd_gap_rising=True, macd_gap_falling=False,
               macd_gap_prev=0.02)
    ok, why = ew.macd_allows_buy(rec, RT)
    assert ok is False
    assert why == "macd_src_unknown"
