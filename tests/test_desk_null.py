"""The turnaround kernel — no Alpaca, no live shadow log.

These tests pin the controls that replace the hindsight-loaded WITHIN dart
and the gate-1 verdict that every new thesis has to clear. If they break,
the next screen is grading against the wrong null again.
"""
from __future__ import annotations

import random
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import bars
import desk_null as N

ET = ZoneInfo("America/New_York")
DAY = "2026-08-14"


def _ts(hour: int, minute: int, day: str = DAY) -> float:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(
        tzinfo=ET).timestamp()


def _bars(start_h=9, start_m=35, n=180, price_at=None):
    """n one-minute bars. price_at(minutes_since_9_30) -> close."""
    stamps, closes = [], []
    h, m = start_h, start_m
    for _ in range(n):
        t = _ts(h, m)
        mins = (h * 60 + m) - (9 * 60 + 30)
        stamps.append(t)
        closes.append(float(price_at(mins) if price_at else 10.0))
        m += 1
        if m >= 60:
            h, m = h + 1, 0
    return stamps, closes


def _ctx(first_watch, pools=None, haircut=0.20):
    return N.NullContext(
        feed="sip", rng=random.Random(7), haircut=haircut, bench="IWM",
        first_watch=first_watch, pools=pools or {}, draws=8,
    )


# ── eligible vs legacy ───────────────────────────────────────────────────

def test_eligible_stamps_drop_the_pre_list_runup():
    stamps, _ = _bars(n=120)  # 9:35–11:34
    t0 = _ts(10, 30)
    first = t0
    elig = N.eligible_stamps(stamps, t0, first)
    assert elig, "should still have later bars"
    assert all(s >= first for s in elig)
    assert all(abs(s - t0) >= N.ELIGIBLE_EXCLUDE_SEC for s in elig)
    # The 9:40 bar is the hindsight dart. It must not be eligible.
    assert all(s >= t0 for s in elig)


def test_legacy_within_includes_the_pre_list_runup():
    stamps, _ = _bars(n=180)  # through 12:34, so |Δt|>30m exists on both sides
    t0 = _ts(10, 30)
    horizon = 30 * 60
    legacy = N.legacy_within_stamps(stamps, t0, horizon)
    assert any(s < t0 for s in legacy), "legacy dart buys the morning"


def test_hindsight_inflates_legacy_and_eligible_does_not(monkeypatch):
    """Name rips 9:35–10:30, then flats. Admission at 10:30.

    30m forward from admission is 0. The legacy dart can buy 9:40 and
    catch the rip, so admitted − legacy is negative. Eligible cannot.
    """
    def px(mins):
        # mins=5 is 9:35; mins=60 is 10:30. Linear 10 → 12, then flat 12.
        if mins <= 60:
            return 10.0 + 2.0 * (mins - 5) / 55.0
        return 12.0

    # Stop at 11:00 so the legacy dart cannot sample the post-rip flat
    # (legacy requires |Δt| > 30m, which is only the morning).
    stamps, closes = _bars(n=86, price_at=px)
    t0 = _ts(10, 30)

    def fake_fetch(sym, day, feed="sip"):
        if sym == "IWM":
            return stamps, [100.0] * len(stamps)
        return stamps, closes

    monkeypatch.setattr(bars, "fetch", fake_fetch)
    ctx = _ctx({("AAA", DAY): t0})
    s = N.score_one(t0, "AAA", DAY, 30 * 60, ctx)
    assert s is not None
    assert abs(s["fwd"]) < 0.05, "post-rip 30m is flat"
    assert s["within"] is not None and s["within"] > 1.0, "legacy bought the rip"
    assert s["eligible"] is not None and abs(s["eligible"]) < 0.05
    assert s["fwd"] - s["within"] < -1.0
    assert abs(s["fwd"] - s["eligible"]) < 0.2


# ── bars helpers ─────────────────────────────────────────────────────────

def test_realized_vol_of_a_flat_tape_is_zero():
    stamps, closes = _bars(n=30)
    t0 = stamps[-1]
    v = bars.realized_vol(stamps, closes, t0, lookback=900)
    assert v is not None
    assert v == 0.0


def test_realized_vol_none_when_too_short():
    stamps, closes = _bars(n=4)
    assert bars.realized_vol(stamps, closes, stamps[-1], lookback=900) is None


def test_move_since_open_measures_the_rip():
    def px(mins):
        return 10.0 + mins * 0.1
    stamps, closes = _bars(n=70, price_at=px)
    t0 = _ts(10, 30)
    mv = bars.move_since_open(stamps, closes, t0)
    assert mv is not None
    # Origin is the first RTH bar (9:35, mins=5 → $10.50), not 9:30.
    expect = (px(60) - px(5)) / px(5) * 100.0
    assert mv == pytest.approx(expect, abs=1e-9)
    assert mv > 0


def test_index_at_picks_the_bar_at_or_before_t0():
    stamps, _ = _bars(n=10)
    assert bars.index_at(stamps, stamps[3]) == 3
    assert bars.index_at(stamps, stamps[3] + 10) == 3
    assert bars.index_at([], 1.0) == -1


# ── residual, haircut, vol match ─────────────────────────────────────────

def test_residual_is_name_minus_iwm(monkeypatch):
    stamps, closes = _bars(n=80)  # name is flat
    bench = [100.0 * (1.001 ** i) for i in range(80)]  # IWM grinds up

    def fake_fetch(sym, day, feed="sip"):
        return (stamps, bench) if sym == "IWM" else (stamps, closes)

    monkeypatch.setattr(bars, "fetch", fake_fetch)
    s = N.score_one(_ts(10, 0), "AAA", DAY, 30 * 60, _ctx({("AAA", DAY): _ts(9, 35)}))
    assert s is not None and s["bench"] is not None and s["residual"] is not None
    assert s["residual"] == pytest.approx(s["fwd"] - s["bench"])
    assert s["fwd"] == pytest.approx(0.0, abs=1e-9)
    assert s["bench"] > 0
    assert s["residual"] < 0, "flat name lagged a rising tape"


def test_haircut_is_vs_cash_and_cancels_in_a_pair(monkeypatch):
    stamps, closes = _bars(n=80)
    monkeypatch.setattr(bars, "fetch", lambda *a, **k: (stamps, closes))
    s = N.score_one(_ts(10, 0), "AAA", DAY, 30 * 60,
                    _ctx({("AAA", DAY): _ts(9, 35)}, haircut=0.20))
    assert s["net"] == pytest.approx(s["fwd"] - 0.20)
    # Same-name control pays the same spread; the paired diff does not.
    if s["eligible"] is not None:
        assert (s["fwd"] - s["eligible"]) == pytest.approx(
            s["net"] - (s["eligible"] - 0.20)
        )


def test_vol_match_skips_when_admitted_vol_is_zero():
    stamps, closes = _bars(n=40)
    # A vol-0 name must not match the whole pool — 0.5–2× of zero is not a band.
    others = N._outside_forwards(
        {"BBB": (stamps, closes)}, stamps[20], 30 * 60,
        p0=10.0, vol0=0.0, want_vol=True)
    assert others == []


# ── clock, tags, research ────────────────────────────────────────────────

def test_tod_buckets_split_the_session():
    assert N.tod_bucket(_ts(9, 45)) == "open_drive"
    assert N.tod_bucket(_ts(10, 0)) == "morning"
    assert N.tod_bucket(_ts(12, 0)) == "midday"
    assert N.tod_bucket(_ts(14, 0)) == "late"
    assert N.tod_bucket(_ts(9, 34)) == "other"


def test_tag_admission_research_vs_scanner_and_chase():
    research = {DAY: {"symbols": {"APPS"}, "catalyst": {"APPS"},
                      "champion": "APPS", "reasons": {}}}
    score = {
        "day": DAY, "sym": "APPS", "tod": "open_drive",
        "open_move": 3.0, "minutes_since_open": 15,
    }
    t = N.tag_admission({"source": "trending", "exhaustion": 80.0, "rvol": 2.5},
                        score, research)
    assert t["research"] is True
    assert t["scanner"] is False
    assert t["champion"] is True
    assert t["catalyst"] is True
    assert t["chase"] is True
    assert t["fresh"] is False
    assert t["feature_ok"] is True

    score2 = dict(score, sym="WETO", open_move=0.2)
    t2 = N.tag_admission({"source": "momentum", "exhaustion": None, "rvol": None},
                         score2, research)
    assert t2["scanner"] is True and t2["research"] is False
    assert t2["feature_ok"] is False
    assert t2["fresh"] is True
    assert t2["chase"] is False


def test_source_research_counts_even_if_not_on_the_days_list():
    score = {"day": DAY, "sym": "ZZZ", "tod": "morning",
             "open_move": 0.0, "minutes_since_open": 40}
    t = N.tag_admission({"source": "research"}, score, {})
    assert t["research"] is True


def test_load_research_by_day_from_dated_markdown(tmp_path):
    md = tmp_path / "grok_research_20260814_080000.md"
    md.write_text(
        'preamble\n{"suggestions":[{"symbol":"APPS","score":8.8,'
        '"reason":"Q1 earnings beat+raise","summary":"live print"}]}',
        encoding="utf-8",
    )
    by = N.load_research_by_day(dirs=[tmp_path])
    assert "2026-08-14" in by
    rec = by["2026-08-14"]
    assert "APPS" in rec["symbols"]
    assert rec["champion"] == "APPS"
    assert "APPS" in rec["catalyst"]


# ── verdict ──────────────────────────────────────────────────────────────

def _scores(n, fwd, eligible, haircut=0.20):
    out = []
    for i in range(n):
        out.append({
            "fwd": fwd, "net": fwd - haircut, "eligible": eligible,
            "within": None, "outside": None, "outside_vol": None,
            "bench": 0.0, "residual": fwd,
        })
    return out


def test_verdict_empty_underpowered_fail_pass():
    assert N.verdict([]) == "EMPTY"
    assert N.verdict(_scores(10, 1.0, 0.0)) == "UNDERPOWERED"
    # Beats cash (0.5 > 0.20) but not timing — eligible is *better* than us.
    assert N.verdict(_scores(30, 0.5, 1.0)) == "FAIL"
    # Timing but does not clear the spread.
    assert N.verdict(_scores(30, 0.10, 0.0)) == "FAIL"
    # Both: +1% after 20bps, and we beat a 0% eligible dart every time.
    assert N.verdict(_scores(30, 1.0, 0.0)) == "PASS"


def test_paired_sigma_all_wins_is_not_noise():
    diffs = [1.0] * 30
    st = N.paired_stats(diffs)
    assert st["beat"] == 100.0
    assert st["sigma"] > 2.0


def test_capped_horizon_does_not_run_past_flatten():
    t0 = _ts(15, 10)
    # 60m from 15:10 would be 16:10; flatten at 15:50 leaves 40m.
    h = N.capped_horizon(t0, 60 * 60, 15 * 60 + 50)
    assert h == 40 * 60
    assert N.capped_horizon(t0, 10 * 60, 15 * 60 + 50) == 10 * 60
    assert N.capped_horizon(_ts(15, 48), 60 * 60, 15 * 60 + 50) is None
    assert N.capped_horizon(t0, 60 * 60, None) == 60 * 60


def test_collect_admissions_skips_preopen_and_keeps_rth():
    rows = [
        {"symbol": "AAA", "admit_ts": 1, "ts": _ts(8, 0), "price": 10},
        {"symbol": "BBB", "admit_ts": 2, "ts": _ts(10, 15), "price": 10},
        {"symbol": "BBB", "admit_ts": 2, "ts": _ts(10, 16), "price": 10.1},
    ]
    ads = N.collect_admissions(rows)
    assert len(ads) == 1
    assert ads[0][1] == "BBB"
    assert ads[0][2] == DAY
