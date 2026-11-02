"""Unit tests for the pre-square CM RSI filter (ai_watch_presquare_max_cm_rsi)."""
import ai_entry_watch as ew


def test_presquare_cm_rsi_off_by_default():
    rec = {"indicator": {"cm_rsi": 95.0}}
    assert ew.presquare_cm_rsi_refusal(rec, {}) is None
    assert ew.presquare_cm_rsi_refusal(rec, {"ai_watch_presquare_max_cm_rsi": 0.0}) is None


def test_presquare_cm_rsi_blocks_pegged():
    cfg = {"ai_watch_presquare_max_cm_rsi": 75.0}
    # Pegged in OB (like DAL today at 89.5 or 96.3)
    rec_pegged = {"indicator": {"cm_rsi": 89.5}}
    assert ew.presquare_cm_rsi_refusal(rec_pegged, cfg) == "presquare_cm_rsi_pegged"

    # Calm pullback RSI passes
    rec_pullback = {"indicator": {"cm_rsi": 62.0}}
    assert ew.presquare_cm_rsi_refusal(rec_pullback, cfg) is None

    # Threshold boundary
    rec_boundary = {"indicator": {"cm_rsi": 75.0}}
    assert ew.presquare_cm_rsi_refusal(rec_boundary, cfg) is None


def test_presquare_cm_rsi_record_fallback():
    cfg = {"ai_watch_presquare_max_cm_rsi": 75.0}
    # When indicator map is not nested but in top-level record
    rec_top = {"cm_rsi": 92.0}
    assert ew.presquare_cm_rsi_refusal(rec_top, cfg) == "presquare_cm_rsi_pegged"


def test_presquare_cm_rsi_fails_open_on_missing():
    cfg = {"ai_watch_presquare_max_cm_rsi": 75.0}
    assert ew.presquare_cm_rsi_refusal({}, cfg) is None
    assert ew.presquare_cm_rsi_refusal({"indicator": {}}, cfg) is None
