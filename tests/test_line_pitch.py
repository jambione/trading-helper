"""One-minute pitch of the fast and slow %R lines. Display only; the buy does not read it.

The book polls about every 10s. Pitch is the change per minute against a reading
stored about a minute earlier, plus the minute before that.
"""
import inspect
from pathlib import Path

import ai_entry_watch as ew

_JS = (Path(__file__).resolve().parent.parent / "static" / "js" / "feeds.js").read_text(encoding="utf-8")


def _rec(sym, fast, slow, src="live", px="engine"):
    ind = {"pctr": fast, "pctr_slow": slow, "pctr_src": src}
    if px is not None:
        ind["pctr_px_src"] = px
    return {"symbol": sym, "indicator": ind}


def _feed(sym, points, t0=1_000_000.0, step=60.0, **kw):
    ew._PITCH_HIST.pop(sym, None)
    rec = None
    for i, (fast, slow) in enumerate(points):
        rec = _rec(sym, fast, slow, **kw)
        ew.pitch_record(rec, t0 + i * step)
    return rec


def test_no_history_yet_has_no_pitch():
    ew._PITCH_HIST.pop("PITCH0", None)
    rec = _rec("PITCH0", -40.0, -25.0)
    ew.pitch_record(rec, 1_000_000.0)
    assert ew._PITCH_HIST["PITCH0"]
    assert "pctr_fast_pitch" not in rec["indicator"]
    assert "pctr_pitch" not in rec["indicator"]


def test_a_ten_second_poll_is_not_a_pitch():
    ew._PITCH_HIST.pop("PITCH10", None)
    rec = _rec("PITCH10", -40.0, -25.0)
    ew.pitch_record(rec, 1_000_000.0)
    rec = _rec("PITCH10", -30.0, -20.0)
    ew.pitch_record(rec, 1_000_020.0)
    assert len(ew._PITCH_HIST["PITCH10"]) == 1
    assert "pctr_fast_pitch" not in rec["indicator"]


def test_one_minute_gives_the_latest_pitch_and_no_prior():
    rec = _feed("PITCH1", [(-40.0, -25.0), (-28.0, -23.0)])
    ind = rec["indicator"]
    assert ind["pctr_fast_pitch"] == 12.0
    assert ind["pctr_slow_pitch"] == 2.0
    assert ind["pctr_fast_pitch_prev"] is None
    assert ind["pctr_slow_pitch_prev"] is None
    assert ind["pctr_pitch"] == "meeting"


def test_two_minutes_keep_fast_under_slow_and_steeper_is_meeting():
    # Fast climbs 12 points a minute from underneath; slow climbs 2.
    rec = _feed("PITCHM", [(-50.0, -20.0), (-38.0, -18.0), (-26.0, -16.0)])
    ind = rec["indicator"]
    assert ind["pctr_fast_pitch"] == 12.0
    assert ind["pctr_slow_pitch"] == 2.0
    assert ind["pctr_fast_pitch_prev"] == 12.0
    assert ind["pctr_slow_pitch_prev"] == 2.0
    assert ind["pctr_pitch"] == "meeting"
    assert ind["pctr"] == -26.0 and ind["pctr"] < ind["pctr_slow"]


def test_equal_pitches_are_same():
    rec = _feed("PITCHEQ", [(-40.0, -30.0), (-30.0, -20.0), (-20.0, -10.0)])
    assert rec["indicator"]["pctr_fast_pitch"] == 10.0
    assert rec["indicator"]["pctr_slow_pitch"] == 10.0
    assert rec["indicator"]["pctr_pitch"] == "same"


def test_fast_already_above_and_steeper_is_opening():
    rec = _feed("PITCHO", [(-10.0, -30.0), (-2.0, -28.0)])
    ind = rec["indicator"]
    assert ind["pctr_fast_pitch"] == 8.0
    assert ind["pctr_slow_pitch"] == 2.0
    assert ind["pctr"] > ind["pctr_slow"]
    assert ind["pctr_pitch"] == "opening"


def test_a_falling_fast_line_is_not_both_up():
    rec = _feed("PITCHDN", [(-20.0, -40.0), (-30.0, -38.0)])
    assert rec["indicator"]["pctr_fast_pitch"] == -10.0
    assert rec["indicator"]["pctr_slow_pitch"] == 2.0
    assert rec["indicator"]["pctr_pitch"] == "not both up"


def test_history_restarts_after_a_gap_and_after_a_formula_change():
    ew._PITCH_HIST.pop("PITCHGAP", None)
    ew.pitch_record(_rec("PITCHGAP", -40.0, -25.0), 1_000_000.0)
    rec = _rec("PITCHGAP", -20.0, -15.0)
    ew.pitch_record(rec, 1_000_000.0 + 200.0)
    assert len(ew._PITCH_HIST["PITCHGAP"]) == 1
    assert ew._PITCH_HIST["PITCHGAP"][0][0] == 1_000_200.0
    assert "pctr_fast_pitch" not in rec["indicator"]

    ew._PITCH_HIST.pop("PITCHSRC", None)
    ew.pitch_record(_rec("PITCHSRC", -40.0, -25.0, px="engine"), 1_000_000.0)
    rec = _rec("PITCHSRC", -28.0, -23.0, px=None)
    ew.pitch_record(rec, 1_000_060.0)
    assert len(ew._PITCH_HIST["PITCHSRC"]) == 1
    assert ew._PITCH_HIST["PITCHSRC"][0][3] == "live"
    assert "pctr_fast_pitch" not in rec["indicator"]


def test_a_non_live_reading_is_not_stored():
    ew._PITCH_HIST.pop("PITCHNL", None)
    rec = _rec("PITCHNL", -40.0, -25.0, src="clock_range")
    ew.pitch_record(rec, 1_000_000.0)
    assert "PITCHNL" not in ew._PITCH_HIST
    rec = _rec("PITCHNL", -40.0, None)
    ew.pitch_record(rec, 1_000_000.0)
    assert "PITCHNL" not in ew._PITCH_HIST


def test_wire_and_empty_indicator_carry_the_pitch_fields():
    rec = _feed("PITCHW", [(-50.0, -20.0), (-38.0, -18.0)])
    wire = ew._exhaustion_wire_fields(rec)
    assert wire["pctr_fast_pitch"] == 12.0
    assert wire["pctr_slow_pitch"] == 2.0
    assert wire["pctr_pitch"] == "meeting"
    assert wire["pctr_fast_pitch_prev"] is None
    empty = ew._exhaustion_wire_fields({})
    for k in ew._PITCH_WIRE_KEYS:
        assert empty[k] is None


def test_pitch_fields_do_not_change_the_empty_square():
    cfg = {
        "ai_watch_exh_presquare_arm": True,
        "rte_threshold": 20,
        "ai_watch_exh_pre_thr": 35,
        "rte_confluence_max": 15,
        "ai_watch_square_min_count": 1,
    }
    ew._PRESQUARE_STREAK.pop("PITCHPRE", None)
    base = {
        "symbol": "PITCHPRE",
        "indicator": {
            "pctr": -30.0,
            "pctr_slow": -28.0,
            "pctr_rising": True,
            "pctr_falling": False,
            "pctr_src": "live",
        },
    }
    first = ew._presquare_exh_allows_buy(base, cfg, now=2_000_000.0)
    stamped = {
        "symbol": "PITCHPRE",
        "indicator": dict(base["indicator"], **{
            "pctr_fast_pitch": 12.0,
            "pctr_slow_pitch": 1.0,
            "pctr_fast_pitch_prev": 8.0,
            "pctr_slow_pitch_prev": 1.0,
            "pctr_pitch": "meeting",
        }),
    }
    second = ew._presquare_exh_allows_buy(stamped, cfg, now=2_000_000.0)
    assert first == (True, "presquare")
    assert second == first
    src = inspect.getsource(ew.should_arm_buy) + inspect.getsource(ew._presquare_exh_allows_buy)
    assert "pctr_fast_pitch" not in src and "pctr_pitch" not in src
    ew._PRESQUARE_STREAK.pop("PITCHPRE", None)


def test_the_book_hover_lists_the_pitch_fields():
    for key in (
        "pctr_fast_pitch", "pctr_slow_pitch",
        "pctr_fast_pitch_prev", "pctr_slow_pitch_prev", "pctr_pitch",
    ):
        assert f"{key}: w.{key}" in _JS, key
    title = _JS[_JS.index("function _fmtExhTitle("):_JS.index("function _bookMacdText(")]
    assert "pitch 1m fast" in _JS
    assert "prior 1m fast" in _JS
    assert "_fmtPitchLine(r)" in title
    assert "_fmtPitchPriorLine(r)" in title


def test_wr_slow_5m_refusal_blocks_falling_5m_slow_and_allows_rising():
    sym = "S5MVETO"
    ew._WR_SLOW_HIST.pop(sym, None)
    ew._PITCH_HIST.pop(sym, None)
    cfg_on = {"ai_watch_wr_slow_5m_veto": True, "ai_watch_wr_slow_5m_min_pitch": 0.0}
    cfg_off = {"ai_watch_wr_slow_5m_veto": False}

    t0 = 3_000_000.0
    r0 = _rec(sym, -32.0, -24.0)
    ew.wr_slow_5m_record(sym, r0, t0, cfg_on)

    # 5 minutes later, slow %R fell from -24.0 to -26.5 -> blocked with wr_slow_5m_falling
    r_fall = _rec(sym, -30.0, -26.5)
    assert ew.wr_slow_5m_refusal(sym, r_fall, t0 + 300.0, cfg_off) is None
    assert ew.wr_slow_5m_refusal(sym, r_fall, t0 + 300.0, cfg_on) == "wr_slow_5m_falling"

    # 5 minutes later, slow %R rose from -24.0 to -20.5 -> allowed (None)
    r_rise = _rec(sym, -25.0, -20.5)
    assert ew.wr_slow_5m_refusal(sym, r_rise, t0 + 300.0, cfg_on) is None
    ew._WR_SLOW_HIST.pop(sym, None)

