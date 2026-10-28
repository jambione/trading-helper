"""Slope-now gate: refuse a buy unless the live %R pitch is rising now and the slow line is not slowing."""
import ai_entry_watch as ew

ON = {"ai_watch_slope_now_gate": True, "ai_watch_slope_now_missing_refuses": False}
STRICT = {"ai_watch_slope_now_gate": True, "ai_watch_slope_now_missing_refuses": True}
T = 1_793_110_000.0


def rec(fp=None, sp=None, spp=None, age=1.0):
    ind = {}
    if fp is not None:
        ind["pctr_fast_pitch"] = fp
    if sp is not None:
        ind["pctr_slow_pitch"] = sp
    if spp is not None:
        ind["pctr_slow_pitch_prev"] = spp
    if age is not None:
        ind["pctr_pitch_ts"] = T - age
    return {"symbol": "AAA", "indicator": ind}


def test_off_by_default_and_when_knob_false():
    r = rec(-5.0, -2.0, 1.0)
    assert ew.slope_now_refusal(r, T, {}) is None
    assert ew.slope_now_refusal(r, T, {"ai_watch_slope_now_gate": False}) is None


def test_rising_and_not_slowing_passes():
    assert ew.slope_now_refusal(rec(6.0, 2.5, 2.5), T, ON) is None
    assert ew.slope_now_refusal(rec(6.0, 3.0, 2.0), T, ON) is None


def test_hood_and_enph_shapes_refuse():
    # HOOD 10/27 13:00: slow pitch fell from 7.3 to 2.0 a minute later -> slowing
    assert ew.slope_now_refusal(rec(7.9, 2.0, 7.3), T, ON) == "slope_not_rising"
    # flat or falling lines
    assert ew.slope_now_refusal(rec(0.0, 1.0, 0.5), T, ON) == "slope_not_rising"
    assert ew.slope_now_refusal(rec(3.0, -0.2, 0.4), T, ON) == "slope_not_rising"


def test_missing_values_pass_unless_strict_but_present_values_still_tested():
    assert ew.slope_now_refusal(rec(), T, ON) is None
    assert ew.slope_now_refusal(rec(), T, STRICT) == "slope_unknown"
    assert ew.slope_now_refusal(rec(fp=-1.0), T, ON) == "slope_not_rising"
    assert ew.slope_now_refusal(rec(fp=4.0, sp=2.0), T, ON) is None
    assert ew.slope_now_refusal(rec(fp=4.0, sp=2.0), T, STRICT) == "slope_unknown"


def test_stale_pitch_counts_as_missing():
    old = rec(-5.0, -2.0, 1.0, age=30.0)
    assert ew.slope_now_refusal(old, T, ON) is None
    assert ew.slope_now_refusal(old, T, STRICT) == "slope_unknown"
    assert ew.slope_now_refusal(rec(-5.0, -2.0, 1.0, age=None), T, ON) is None


def test_pitch_record_stamps_the_timestamp_and_clears_it():
    ew._PITCH_HIST.pop("PTS", None)
    r = {"symbol": "PTS", "indicator": {"pctr": -60.0, "pctr_slow": -40.0, "pctr_src": "live", "pctr_px_src": "engine"}}
    for k in range(4):
        r["indicator"].update({"pctr": -60.0 + 8 * k, "pctr_slow": -40.0 + 3 * k})
        ew.pitch_record(r, T + 60.0 * k)
    assert r["indicator"].get("pctr_pitch_ts") == T + 180.0
    r["indicator"]["pctr_src"] = "stale"
    ew.pitch_record(r, T + 240.0)
    assert "pctr_pitch_ts" not in r["indicator"]


def test_blocker_labels_exist():
    assert ew.format_blocker("slope_not_rising") == "%R not rising now"
    assert ew.format_blocker("slope_unknown") == "no %R pitch"
