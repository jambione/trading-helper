"""Tests for the counterfactual (shadow) measurement path.

The desk can run a whole session without a fill — 2026-08-06: 530 zones, 31
symbols, 0 trades — which leaves every gate unmeasurable from outcomes.jsonl.
These cover the row the poller writes and the report that reads it back.
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT))

import shadow_report as sr  # noqa: E402


def _s(sym="AAA", *, ts, price, lo=9.6, hi=9.9, arm_ok=None, why="",
       admit=1000.0, **extra):
    row = {"ts": ts, "symbol": sym, "price": price, "entry_low": lo,
           "entry_high": hi, "stop_price": lo * 0.95, "target_1": hi * 1.075,
           "in_zone": lo <= price <= hi, "arm_ok": arm_ok, "arm_why": why,
           "source": "trending", "look_reason": "EXT", "rvol": 2.5,
           "criteria": ["score", "ext"], "admit_ts": admit,
           "cm_rsi_rising": True, "entry_hour_et": 10.0}
    row.update(extra)
    return row


# ── the row the poller writes ────────────────────────────────────────────

def test_shadow_row_marks_in_zone_from_the_live_price():
    import ai_entry_watch as ew

    rec = {"symbol": "AAA", "status": "watching", "source": "trending",
           "admit_rvol": 2.5, "admit_look_reason": "EXT",
           "admit_criteria": ["score", "ext"], "admit_ts": 1000.0,
           "structure": {"entry_low": 9.6, "entry_high": 9.9,
                         "stop_price": 9.12, "target_1": 10.6},
           "indicator": {"cm_ok": True, "pctr_ok": True,
                         "cm_rsi_rising": True, "sell_signal": False}}

    r = ew._shadow_row(rec, price=9.75, price_src="quote", arm_ok=False,
                       arm_why="sell_signal", now=1200.0)
    assert r["in_zone"] is True
    assert r["arm_ok"] is False and r["arm_why"] == "sell_signal"
    # Selection provenance must match the entry feature vector's fields so a
    # shadow slice and a filled-trade slice are comparable.
    assert r["look_reason"] == "EXT" and r["rvol"] == 2.5
    assert r["criteria"] == ["score", "ext"]

    out = ew._shadow_row(rec, price=12.0, price_src="tape", arm_ok=None,
                         arm_why="prefilter_far", now=1200.0)
    assert out["in_zone"] is False


def test_shadow_row_survives_a_record_with_no_zone_or_indicator():
    """Newly admitted names have neither for ~20s. Logging must not raise."""
    import ai_entry_watch as ew

    r = ew._shadow_row({"symbol": "AAA"}, price=None, price_src="quote",
                       arm_ok=None, arm_why="", now=1.0)
    assert r["in_zone"] is False and r["price"] is None
    assert r["cm_ok"] is None, "absent indicator must be None, not False"


# ── episode grouping ─────────────────────────────────────────────────────

def test_readmission_is_a_separate_episode():
    """A name dropped and re-admitted is two decisions. Pooling them would
    average an early refusal with a later entry as if one thing happened."""
    rows = [_s(ts=10, price=10.0, admit=1000.0),
            _s(ts=20, price=10.1, admit=1000.0),
            _s(ts=900, price=9.0, admit=5000.0)]
    eps = sr.by_episode(rows)
    assert len(eps) == 2


# ── forward return honesty ───────────────────────────────────────────────

def test_truncated_window_returns_none_not_zero():
    """An episode that ends before the horizon is missing data. Zero-filling
    biases every slice toward nothing-happened."""
    series = [_s(ts=0, price=10.0), _s(ts=30, price=10.5)]
    assert sr.forward_return(series, 0, horizon_sec=3600) is None


def test_forward_return_measures_to_the_last_sample_in_window():
    series = [_s(ts=0, price=10.0), _s(ts=300, price=10.5),
              _s(ts=600, price=11.0), _s(ts=99999, price=99.0)]
    got = sr.forward_return(series, 0, horizon_sec=600)
    assert got is not None and abs(got - 10.0) < 1e-6


# ── the three questions ──────────────────────────────────────────────────

def test_zone_reachability_counts_a_zone_price_never_touched():
    """A zone nothing reaches is not a filter, it is a refusal — and it is
    invisible in outcomes.jsonl because no trade ever happens."""
    never = [_s(ts=i * 20, price=12.0) for i in range(10)]
    e = sr.episode_summary(never, horizon_sec=600)
    assert e["zone_touched"] is False and e["minutes_to_touch"] is None


def test_gate_cost_is_measured_from_the_refusal_not_admission():
    """The question is what happened AFTER the desk declined. Measuring from
    admission understates a gate that blocks late in a move — price has
    already travelled by then."""
    series = (
        [_s(ts=0, price=10.0)]                                   # admitted high
        + [_s(ts=100, price=9.7, arm_ok=False, why="sell_signal")]  # refusal
        + [_s(ts=200 + i * 100, price=9.7 + 0.1 * i) for i in range(6)]  # rally
    )
    e = sr.episode_summary(series, horizon_sec=600)
    assert e["blocked_in_zone"] == 1
    assert e["block_reasons"] == ["sell_signal"]
    # From the refusal price (9.7) the move is up; from admission (10.0) it is
    # roughly flat. The first is the honest answer to "what did refusing cost".
    assert e["blocked_fwd_pct"] is not None
    assert e["blocked_fwd_pct"] > 0
    assert e["blocked_fwd_pct"] > (e["fwd_return_pct"] or 0)


def test_episode_with_no_block_has_no_blocked_forward():
    series = [_s(ts=i * 100, price=9.7, arm_ok=True) for i in range(8)]
    e = sr.episode_summary(series, horizon_sec=600)
    assert e["blocked_in_zone"] == 0 and e["blocked_fwd_pct"] is None
    assert e["armed"] is True


def test_loader_skips_rows_with_no_price(tmp_path):
    p = tmp_path / "shadow.jsonl"
    p.write_text("\n".join([
        json.dumps(_s(ts=1, price=9.7)),
        json.dumps({"symbol": "BBB", "ts": 2}),      # no price
        "not json",
    ]), encoding="utf-8")
    assert len(sr.load(p)) == 1
