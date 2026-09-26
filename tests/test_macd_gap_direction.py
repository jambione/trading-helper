"""MACD on the wire, its provenance, and the pinned-%R exemption.

MACD no longer gates entry (the require_macd stack, the narrowing veto and
the EXH confluence override were retired). What remains: the book's MACD
column and the record's MACD fields, the live-tape provenance check that
``_macd_is_armed`` relies on, and the pinned-overbought exemption that lets
an armed MACD stand in for a %R turn.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402



def _rec(**ind):
    # macd_sep_ratio is part of the baseline: the separation test is the
    # strategy, and a record without one is now refused as macd_sep_unknown
    # rather than passed silently. 1.5 clears the 0.8 default multiple, so
    # these fixtures exercise the direction rule and not the size rule.
    base = {"macd_fast": 0.10, "macd_slow": 0.05, "macd_gap": 0.05,
            "macd_sep_ratio": 1.5}
    base.update(ind)
    return {"symbol": "AAA", "indicator": base}


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
    assert "macd_src" in got
    assert "macd_age_sec" in got


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
                      "macd_gap_rising": True, "macd_gap_falling": False,
                      "macd_src": "realtime", "macd_age_sec": 0.4},
        "structure": {"entry_low": 9.0, "entry_high": 11.0,
                      "stop_price": 8.5},
    }})
    row = ew.public_snapshot()[0]
    assert row["macd_gap"] == 0.05
    assert row["macd_gap_rising"] is True
    assert row["macd_src"] == "realtime"
    assert row["macd_age_sec"] == 0.4
    assert row.get("decision_max_age_sec") is not None


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


# ── provenance: which tape drew this reading ─────────────────────────────

RT = {"ai_watch_require_realtime_macd": True}


def _live(**ind):
    return ew.macd_reading_is_live(_rec(**ind)["indicator"], RT)


def test_a_rest_fallback_reading_is_refused():
    """bars_src flips per ticker mid-session, so an unchecked reading
    alternates between the Finnhub tape (0.3s median, measured 8/26) and the
    Alpaca REST fallback (up to 60s) without saying which."""
    assert _live(macd_src="alpaca") == (False, "macd_not_realtime_alpaca")


def test_a_realtime_reading_passes():
    assert _live(macd_src="realtime", macd_age_sec=0.3) == (True, "")


def test_unknown_provenance_is_refused():
    """Absence is not a pass — the rule everywhere else on this desk."""
    assert _live() == (False, "macd_src_unknown")


def test_an_age_without_a_source_is_not_provenance():
    """The hole: src empty + age set used to skip macd_src_unknown."""
    assert _live(macd_age_sec=0.3) == (False, "macd_src_unknown")


def test_an_age_ceiling_is_optional_and_off_by_default():
    from config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["ai_watch_macd_max_age_sec"] == 0.0
    # 0 = source check only: a realtime reading of any age still passes.
    assert _live(macd_src="realtime", macd_age_sec=999.0)[0] is True


def test_the_age_ceiling_bites_when_set():
    ok, why = ew.macd_reading_is_live(
        _rec(macd_src="realtime", macd_age_sec=42.0)["indicator"],
        dict(RT, ai_watch_macd_max_age_sec=10.0))
    assert ok is False and why == "macd_stale_bars"


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
    """Same record the poll builds: provenance present, no macd_src_unknown."""
    ok, why = _live(macd_gap_rising=True, macd_gap_falling=False,
                    macd_gap_prev=0.02, macd_src="realtime", macd_age_sec=0.4)
    assert ok, why


def test_the_same_record_without_provenance_is_still_refused():
    """The check must stay real — this is the half that must NOT regress."""
    assert _live(macd_gap_rising=True, macd_gap_falling=False,
                 macd_gap_prev=0.02) == (False, "macd_src_unknown")


# ── a %R pinned at the ceiling cannot be "rising" ───────────────────────────
#
# Williams %R is position-in-range. At 100% the name is at the top of its
# lookback window, so pctr_rising and pctr_falling are BOTH False and the
# exhaustion gate refuses it as not_rising_overbought forever. Measured
# 2026-08-27 on a momentum-hunting session: CRMG 100.0%, CSIQ 100.0%,
# FIG 98.9% — all flat, all refused. The strongest names on the book were the
# only ones structurally unreachable.
#
# The operator's rule: allow the LEVEL to stand in for the turn, but only
# while MACD is armed. A falling %R is still refused, so a top that has
# already rolled over cannot get in this way.

# ai_watch_exh_square_arm (e5339b9, default ON) intercepts exhaustion_allows_buy
# ahead of these rules and wants BOTH %R lines, so a fast-only fixture dies on
# no_exhaustion_data before reaching the pinned-OB rule under test. The square
# arm is a different arm story with its own cover in test_ai_entry_watch.py;
# these tests are about the legacy path, which is still live code and still
# reachable via the documented rollback (square_arm=false). Pin it the same way
# this file already pins ai_watch_tv_exh_rsi.
_LEGACY_ARM = {"ai_watch_exh_square_arm": False}

_OBFLAT = dict(RT, ai_watch_ob_allow_flat_when_macd_armed=True, **_LEGACY_ARM)


def _armed_ind(**over):
    ind = {"macd_src": "realtime", "macd_age_sec": 0.3,
           "macd_gap": 0.05, "macd_bull": True,
           "macd_gap_rising": True, "macd_gap_falling": False}
    ind.update(over)
    return ind


def test_macd_is_armed_needs_bullish_and_opening():
    assert ew._macd_is_armed({"indicator": _armed_ind()}) is True
    assert ew._macd_is_armed({"indicator": _armed_ind(macd_gap=-0.01)}) is False
    assert ew._macd_is_armed({"indicator": _armed_ind(macd_bull=False)}) is False


def test_macd_is_armed_refuses_a_closing_gap():
    """A wide gap that is closing is a move already over — exactly what a
    pinned %R must not be paired with."""
    assert ew._macd_is_armed({"indicator": _armed_ind(
        macd_gap_rising=False, macd_gap_falling=True)}) is False


def test_macd_is_armed_refuses_an_unproven_reading():
    """An opening gap drawn on the REST fallback is an opening gap in older
    bars. Absence is not a pass."""
    assert ew._macd_is_armed({"indicator": _armed_ind(macd_src="alpaca")}) is False
    assert ew._macd_is_armed({"indicator": _armed_ind(macd_src=None)}) is False


def test_macd_is_armed_is_narrower_than_the_entry_gate():
    """It stands in for a %R turn; it does not re-decide the entry. A gap far
    under macd_min_gap still counts as armed."""
    assert ew._macd_is_armed({"indicator": _armed_ind(macd_gap=0.0001)}) is True


def test_a_pinned_overbought_reading_passes_when_macd_is_armed():
    rec = {"symbol": "AAA", "indicator": dict(
        _armed_ind(), pctr=0.0, pctr_rising=False, pctr_falling=False)}
    ok, why = ew.exhaustion_allows_buy(rec, _OBFLAT)
    assert ok, why
    assert why == "overbought_macd_armed"


def test_a_pinned_overbought_reading_is_still_refused_without_the_flag():
    rec = {"symbol": "AAA", "indicator": dict(
        _armed_ind(), pctr=0.0, pctr_rising=False, pctr_falling=False,
        pctr_src="live")}
    cfg = dict(RT, ai_watch_exhaustion_rules=True, ai_watch_require_exh_rising=True,
               ai_watch_exhaustion_heat_max_pct=0.0,
               ai_watch_exhaustion_heat_min_pct=0.0, **_LEGACY_ARM)
    ok, why = ew.exhaustion_allows_buy(rec, cfg)
    assert ok is False
    assert why == "exh_not_rising"


def test_a_rolling_over_top_is_still_refused_with_the_flag():
    """The half that must not regress: falling is checked before this."""
    rec = {"symbol": "AAA", "indicator": dict(
        _armed_ind(), pctr=0.0, pctr_rising=False, pctr_falling=True,
        pctr_src="live")}
    cfg = dict(_OBFLAT, ai_watch_exhaustion_rules=True,
               ai_watch_require_exh_rising=True,
               ai_watch_exhaustion_heat_max_pct=0.0,
               ai_watch_exhaustion_heat_min_pct=0.0)
    ok, why = ew.exhaustion_allows_buy(rec, cfg)
    assert ok is False
    assert why == "exh_falling"


def test_a_pinned_reading_is_refused_when_macd_is_not_armed():
    """The level alone is never enough — MACD supplies the direction."""
    rec = {"symbol": "AAA", "indicator": dict(
        _armed_ind(macd_gap_rising=False, macd_gap_falling=True),
        pctr=0.0, pctr_rising=False, pctr_falling=False)}
    assert ew.exhaustion_allows_buy(rec, _OBFLAT)[0] is False


def test_the_knob_is_off_by_default_and_reaches_the_live_config():
    from config import DEFAULT_CONFIG, load_config
    assert DEFAULT_CONFIG["ai_watch_ob_allow_flat_when_macd_armed"] is False
    assert "ai_watch_ob_allow_flat_when_macd_armed" in load_config()


# ── MACD is not an entry gate ────────────────────────────────────────────
# With the require_macd stack and the narrowing veto retired, a tiny or
# missing MACD reading does not stop an EXH arm.

def _veto_arm_cfg(**over):
    cfg = {
        "ai_watch_arm_mode": "last",
        "ai_watch_tv_exh_rsi": False,
        "ai_watch_exhaustion_rules": True,
        "ai_watch_require_exhaustion_data": False,
        "ai_watch_exhaustion_heat_min_pct": 0.0,
        "ai_watch_exhaustion_heat_max_pct": 0.0,
        "ai_watch_ob_allow_hot": True,
        "ai_watch_arm_require_indicators": False,
        "ai_watch_arm_require_cm_rsi": False,
        "ai_watch_soft_ob_enabled": False,
        "ai_watch_mistimed_heat_enabled": False,
        "ai_min_reward_risk": 0.5,
        "ai_watch_min_stop_pct": 0,
        "ai_watch_synth_stop_pct": 5.0,
        "ai_watch_synth_rr": 0.6,
        # See _LEGACY_ARM: the square arm would refuse these fast-only %R
        # fixtures before the MACD veto under test ever runs.
        "ai_watch_exh_square_arm": False,
    }
    cfg.update(over)
    return cfg


def _veto_arm_rec(**ind):
    base = {
        "pctr": -20.0, "pctr_rising": True, "pctr_falling": False,
        "cm_rsi": 40.0, "cm_rsi_rising": True,
    }
    base.update(ind)
    return {
        "symbol": "AAA",
        "status": "watching",
        "structure": {
            "decision": "BUY",
            "zone_kind": "at_last",
            "entry_low": 9.0, "entry_high": 11.0,
            "stop_price": 8.5, "target_1": 12.0, "reward_risk": 0.6,
            "synthetic": True,
        },
        "indicator": base,
    }


def test_should_arm_allows_opening_gap_under_exh_rsi_without_min_gap():
    """A tiny opening gap used to die on macd_gap_too_close under full MACD."""
    rec = _veto_arm_rec(
        macd_fast=0.10, macd_slow=0.099, macd_gap=0.001,
        macd_gap_rising=True, macd_gap_falling=False, macd_gap_prev=0.0005)
    ok, why = ew.should_arm_buy(rec, ask=10.0, bid=9.99, cfg=_veto_arm_cfg())
    assert ok is True
    assert why.startswith("last_")


def test_should_arm_allows_missing_macd_under_veto_only():
    rec = _veto_arm_rec()  # no macd_* fields
    ok, why = ew.should_arm_buy(rec, ask=10.0, bid=9.99, cfg=_veto_arm_cfg())
    assert ok is True
    assert why.startswith("last_")
