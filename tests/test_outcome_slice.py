"""Tests for tools/outcome_slice.py — the feature/outcome slicer.

This tool exists to tell the operator whether a gate earned its place. Its
failure mode is not crashing, it is quietly reporting an edge that is not
there — so the tests here are mostly about what it must REFUSE to claim.
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT))

import outcome_slice as osl  # noqa: E402


def _row(r, *, ext=True, t=0, **feat):
    f = {"source": "trending", "look_reason": "EXT" if ext else "WASH",
         "criteria": ["score", "ext"] if ext else ["score"],
         "cm_rsi_rising": ext, "rvol": 2.5 if ext else 1.2,
         "entry_hour_et": 9.6}
    f.update(feat)
    return {"ts": t, "entry_time": t, "realized_r_multiple": r,
            "exit_price": 10.0, "features": f}


def _write(tmp_path, rows):
    p = tmp_path / "outcomes.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def test_unpriced_exits_are_excluded_not_zero_filled(tmp_path):
    """A trade whose exit could not be priced is missing data. Counting it as
    0.0R drags every mean toward zero and invents a flat trade that never
    happened — 3 of the 4 real records on 2026-08-06 were exactly this shape."""
    rows = [_row(1.0), _row(-1.0)]
    bad = _row(0.0)
    bad["realized_r_multiple"] = None
    rows.append(bad)
    p = _write(tmp_path, rows)

    usable, skipped = osl.load_outcomes(p)
    assert len(usable) == 2
    assert skipped["no_realized_r"] == 1


def test_rows_without_a_feature_vector_are_reported_separately(tmp_path):
    """Pre-instrumentation rows must not be pooled into whichever bucket a
    missing key happens to default to."""
    old = {"ts": 1, "realized_r_multiple": 0.5, "exit_price": 1.0}
    p = _write(tmp_path, [_row(1.0), old])

    usable, skipped = osl.load_outcomes(p)
    assert len(usable) == 1
    assert skipped["no_features"] == 1


def test_missing_feature_is_labelled_unrecorded_not_bucketed(tmp_path):
    """A feature the desk never observed gets its own visible bucket, so it
    cannot be mistaken for an observed False."""
    r = _row(1.0)
    r["features"].pop("cm_rsi_rising")
    p = _write(tmp_path, [r])
    usable, _ = osl.load_outcomes(p)

    table = osl.slice_by(usable, "cm_rsi_rising", min_n=1)
    assert "(unrecorded)" in table


def test_an_edge_present_in_only_one_half_is_flagged():
    """The signature that killed the indicator gate in benchmarks/ab_bench_*:
    +0.0977R first half, -0.0129R second. A pooled mean hides it; the slicer
    must say the sign flipped."""
    group = ([_row(1.0, t=i) for i in range(6)]        # early: winners
             + [_row(-0.9, t=100 + i) for i in range(6)])  # late: losers
    s = osl.summarize(group, min_n=1)
    assert s["half_a_mean"] > 0 and s["half_b_mean"] < 0
    assert s["holds_both_halves"] is False


def test_a_consistent_edge_is_not_flagged():
    group = [_row(0.4, t=i) for i in range(12)]
    s = osl.summarize(group, min_n=1)
    assert s["holds_both_halves"] is True


def test_small_groups_are_marked_underpowered():
    """A mean over a handful of trades is an anecdote and must be labelled."""
    s = osl.summarize([_row(2.0), _row(1.5), _row(1.8)], min_n=30)
    assert s["underpowered"] is True
    assert s["mean_r"] > 1.0, "still reports the number — just refuses to sell it"


def test_required_n_matches_the_desk_reality():
    """~780 trades per arm to resolve 0.1R at unit variance. This is the number
    that makes live A/B on a few-trades-a-day desk a non-starter, so it is
    pinned rather than left to drift."""
    assert 700 <= osl.required_n(0.10) <= 850
    assert osl.required_n(0.50) < osl.required_n(0.10)
    assert osl.required_n(0) == 0


def test_criteria_membership_slices_on_the_list(tmp_path):
    """crit:<name> asks whether a gate claimed credit for this admission."""
    p = _write(tmp_path, [_row(1.0, ext=True), _row(-1.0, ext=False)])
    usable, _ = osl.load_outcomes(p)

    table = osl.slice_by(usable, "crit:ext", min_n=1)
    assert table["True"]["n"] == 1 and table["False"]["n"] == 1
    assert table["True"]["mean_r"] == 1.0


def test_entry_hour_buckets_separate_the_open_from_the_flatten():
    """A 15:45 entry facing the 15:50 liquidate is not the same trade as the
    same signal at 09:35."""
    assert osl._bucket({"entry_hour_et": 9.6}, "entry_hour_et") == "open<10"
    assert osl._bucket({"entry_hour_et": 12.0}, "entry_hour_et") == "mid10-14"
    assert osl._bucket({"entry_hour_et": 15.8}, "entry_hour_et") == "late>=14"
    assert osl._bucket({}, "entry_hour_et") is None
