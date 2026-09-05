"""Unit tests for Package 1 arm_why → veto bucket mapper."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from desk_arm_buckets import BUCKETS, arm_bucket, bucket_label


def test_buckets_stable_set():
    assert BUCKETS == (
        "readiness", "exh", "rsi", "macd_dir", "heat", "spread", "zone", "other",
    )


def test_common_live_reasons_from_shadow():
    """Top live arm_why values from Sep tapes map to the intended buckets."""
    cases = {
        # readiness
        "tape_only": "readiness",
        "stale_quote": "readiness",
        "need_stream": "readiness",
        "stream_required": "readiness",
        "no_macd_data": "readiness",
        "macd_not_realtime_alpaca": "readiness",
        "rsi_not_realtime_alpaca": "readiness",
        "macd_stale_bars": "readiness",
        "macd_src_unknown": "readiness",
        # exh
        "exh_falling": "exh",
        "exh_not_rising": "exh",
        "last_exhaustion_off": "exh",
        "no_exhaustion_data": "exh",
        # rsi
        "rsi_extended": "rsi",
        "rsi_not_rising": "rsi",
        "rsi_below_band": "rsi",
        # macd_dir
        "macd_bearish": "macd_dir",
        "macd_gap_narrowing": "macd_dir",
        "macd_gap_too_close": "macd_dir",
        "macd_gap_insufficient": "macd_dir",
        # heat
        "mistimed_heat": "heat",
        "soft_ob": "heat",
        "late_heat": "heat",
        "cheap_ob_band": "heat",
        "last_heating": "heat",
        "last_overbought": "heat",
        "heating_too_low": "heat",
        "extended_cheap": "heat",
        # spread
        "spread": "spread",
        "wide_spread": "spread",
        # zone
        "above_zone": "zone",
        "below_zone": "zone",
        "no_structure": "zone",
        "wait_setup": "zone",
        "reward_risk": "zone",
        "prefilter_far": "zone",
    }
    for why, want in cases.items():
        assert arm_bucket(why) == want, (why, arm_bucket(why), want)


def test_empty_and_unknown_go_other():
    assert arm_bucket(None) == "other"
    assert arm_bucket("") == "other"
    assert arm_bucket("brand_new_veto_xyz") == "other"


def test_case_insensitive():
    assert arm_bucket("TAPE_ONLY") == "readiness"
    assert arm_bucket(" Macd_Bearish ") == "macd_dir"


def test_bucket_label_covers_all():
    for b in BUCKETS:
        assert isinstance(bucket_label(b), str)
        assert len(bucket_label(b)) > 0
