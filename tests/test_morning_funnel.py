"""Tests for tools/morning_funnel.py — pure functions only, no network."""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import morning_funnel as mf  # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 7, 10, 9, 50, tzinfo=ET)   # Friday, 20 min after open

KNOBS = {"min_price": 1.5, "max_price": 60.0, "min_rvol": 1.5,
         "max_spread_pct": 0.35, "pm_max_spread_pct": 1.0,
         "min_dollar_min": 25000.0,
         "max_candidates": 25, "refresh_sec": 150.0, "avg_days": 10}


# The minute-based average session volume production now supplies to evaluate().
# A daily-bar average is not interchangeable — see mf.avg_session_volume.
AVG_VOL = 1_000_000.0


# ── synthetic data builders ─────────────────────────────────────────────────
def daily_df(prev_close=10.0, avg_vol=1_000_000, days=12):
    idx = pd.bdate_range(end="2026-07-09", periods=days, tz=ET)
    return pd.DataFrame({"open": prev_close, "high": prev_close * 1.02,
                         "low": prev_close * 0.98, "close": prev_close,
                         "volume": float(avg_vol)}, index=idx)


def minute_df(start="2026-07-10 09:30", bars=20, base=10.5, drift=0.01,
              vol_per_bar=23_000):
    idx = pd.date_range(start=start, periods=bars, freq="1min", tz=ET)
    closes = [base + drift * i for i in range(bars)]
    return pd.DataFrame({
        "open": [base] + closes[:-1],
        "high": [c + 0.02 for c in closes],
        "low": [c - 0.02 for c in closes],
        "close": closes,
        "volume": float(vol_per_bar)}, index=idx)


# ── session clock ───────────────────────────────────────────────────────────
def test_session_windows():
    def label(h, m, day=10):     # 2026-07-10 is a Friday
        return mf.session_window(datetime(2026, 7, day, h, m, tzinfo=ET))[0]
    assert label(5, 0) == "PRE-MARKET SCAN"
    assert label(6, 45) == "T1 FUNNEL"
    assert label(7, 30) == "TRANCHE 1"
    assert label(8, 25) == "T2 FUNNEL"
    assert label(8, 45) == "TRANCHE 2"
    assert label(9, 27) == "T3 FUNNEL"
    assert label(9, 50) == "TRANCHE 3"
    assert label(11, 0) == "WIND-DOWN"
    assert label(13, 30) == "EXITS ONLY"
    assert label(20, 0) == "CLOSED"
    assert label(10, 0, day=11) == "CLOSED"      # Saturday


def test_next_shot_countdown():
    def shot(h, m, day=10):
        return mf.next_shot(datetime(2026, 7, day, h, m, tzinfo=ET))
    assert shot(6, 0) == ("TRANCHE 1", 60)
    assert shot(7, 30) == ("TRANCHE 2", 60)
    assert shot(9, 0) == ("TRANCHE 3", 30)
    assert shot(9, 30) is None                   # last shot fired
    assert shot(8, 0, day=11) is None            # Saturday


# ── volume curve ────────────────────────────────────────────────────────────
def test_expected_fraction_monotonic_and_bounded():
    vals = [mf.expected_fraction(m) for m in range(-330, 391, 15)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))
    assert mf.expected_fraction(390) == 1.0
    assert mf.expected_fraction(500) == 1.0
    assert 0 < mf.expected_fraction(-300) < 0.05
    assert 0.04 < mf.expected_fraction(0) <= 0.06


# ── candidate gathering ─────────────────────────────────────────────────────
def test_gather_candidates_sources_and_dedupe(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "transcription").mkdir()
    (tmp_path / "config" / "funnel_watchlist.txt").write_text(
        "# comment\nAAPL, tsla  # inline\n\nHOLO\n", encoding="utf-8")
    (tmp_path / "transcription" / "wb_watchlist.json").write_text(
        json.dumps([{"ticker": "SPRC"}, {"ticker": "AAPL"}]), encoding="utf-8")
    (tmp_path / "swing_candidates.json").write_text(
        json.dumps({"candidates": [{"symbol": "ZCMD"}]}), encoding="utf-8")

    out = mf.gather_candidates(["holo", "NVDA"], tmp_path)
    # CLI first, then watchlist file, then mentions, then swing; deduped
    assert out == ["HOLO", "NVDA", "AAPL", "TSLA", "SPRC", "ZCMD"]


def test_gather_candidates_rejects_junk_and_caps(tmp_path):
    out = mf.gather_candidates(["TOOLONGG", "123", "", "OK"], tmp_path, max_n=1)
    assert out == ["OK"]


# ── evaluate: scoring and filters ───────────────────────────────────────────
def test_strong_candidate_scores_high():
    row = mf.evaluate("HOLO", daily_df(), minute_df(),
                      quote=(10.60, 10.62), now_et=NOW, knobs=KNOBS,
                      avg_vol=AVG_VOL)
    assert row["rejects"] == []
    assert 2.5 < row["rvol"] < 3.5          # 460k vs 1M-avg day at 9:50 pace
    assert 4.0 < row["chg_pct"] < 9.0       # opened ~+5% and drifting up
    assert row["score"] > 60
    assert row["spread_pct"] == pytest.approx(0.188, abs=0.02)


def test_low_rvol_rejected_after_open():
    row = mf.evaluate("DEAD", daily_df(), minute_df(vol_per_bar=5_000),
                      quote=None, now_et=NOW, knobs=KNOBS,
                      avg_vol=AVG_VOL)
    assert "low rvol" in row["rejects"]
    assert row["score"] == 0.0


def test_price_band_reject():
    row = mf.evaluate("PENNY", daily_df(prev_close=0.50),
                      minute_df(base=0.52, drift=0.001),
                      quote=None, now_et=NOW, knobs=KNOBS)
    assert "price" in row["rejects"]


def test_wide_spread_reject():
    row = mf.evaluate("WIDE", daily_df(), minute_df(),
                      quote=(10.00, 10.10), now_et=NOW, knobs=KNOBS)
    assert "spread" in row["rejects"]


def test_premarket_no_pace_rejects():
    """Before the open, thin volume must not disqualify a gapper."""
    pre_now = datetime(2026, 7, 10, 9, 0, tzinfo=ET)
    row = mf.evaluate("GAP", daily_df(),
                      minute_df(start="2026-07-10 08:30", bars=25,
                                vol_per_bar=2_000),
                      quote=None, now_et=pre_now, knobs=KNOBS,
                      avg_vol=AVG_VOL)
    assert "low rvol" not in row["rejects"]
    assert "illiquid" not in row["rejects"]
    # premarket gap read from last trade vs prior close
    assert row["gap_pct"] == pytest.approx(row["chg_pct"])


def test_premarket_spread_uses_looser_ceiling():
    """0.6% is fatal in-session but normal at 7:00 — only >1% rejects."""
    pre_now = datetime(2026, 7, 10, 7, 5, tzinfo=ET)
    pm = minute_df(start="2026-07-10 06:30", bars=30, vol_per_bar=2_000)
    ok = mf.evaluate("GAP", daily_df(), pm,
                     quote=(10.60, 10.665), now_et=pre_now, knobs=KNOBS)
    assert "spread" not in ok["rejects"]
    wide = mf.evaluate("GAP", daily_df(), pm,
                       quote=(10.00, 10.15), now_et=pre_now, knobs=KNOBS)
    assert "spread" in wide["rejects"]


def test_dollar_per_min_is_wall_clock():
    """Sparse premarket bars: 6 bars in the last 15 min is 15 wall-clock
    minutes of tape, not 6 — pace must not be inflated by missing bars."""
    pre_now = datetime(2026, 7, 10, 7, 0, tzinfo=ET)
    idx = pd.date_range("2026-07-10 06:48", periods=6, freq="2min", tz=ET)
    m = pd.DataFrame({"open": 10.0, "high": 10.02, "low": 9.98,
                      "close": 10.0, "volume": 3_000.0}, index=idx)
    row = mf.evaluate("THIN", daily_df(), m, quote=None,
                      now_et=pre_now, knobs=KNOBS)
    assert row["dollar_min"] == pytest.approx(6 * 3_000 * 10.0 / 15.0)


def test_missing_data_rejected():
    assert "no data" in mf.evaluate("X", None, None, None, NOW, KNOBS)["rejects"]
    assert "no data" in mf.evaluate(
        "X", daily_df(days=3), minute_df(), None, NOW, KNOBS)["rejects"]
    assert "no data" in mf.evaluate(
        "X", daily_df(), minute_df(start="2026-07-09 09:30"),  # stale day
        None, NOW, KNOBS)["rejects"]


def test_rank_orders_by_score_rejects_last():
    strong = mf.evaluate("STRG", daily_df(), minute_df(),
                         quote=None, now_et=NOW, knobs=KNOBS,
                         avg_vol=AVG_VOL)
    mild = mf.evaluate("MILD", daily_df(),
                       minute_df(base=10.05, drift=0.002, vol_per_bar=12_000),
                       quote=None, now_et=NOW, knobs=KNOBS,
                       avg_vol=AVG_VOL)
    dead = mf.evaluate("DEAD", daily_df(), minute_df(vol_per_bar=4_000),
                       quote=None, now_et=NOW, knobs=KNOBS,
                       avg_vol=AVG_VOL)
    ranked = mf.rank([dead, mild, strong])
    assert [r["sym"] for r in ranked] == ["STRG", "MILD", "DEAD"]


def test_build_view_renders():
    """Smoke test: the rich view builds for both good and rejected rows."""
    rows = mf.rank([
        mf.evaluate("STRG", daily_df(), minute_df(), (10.60, 10.62), NOW,
                    KNOBS, avg_vol=AVG_VOL),
        mf.evaluate("DEAD", daily_df(), minute_df(vol_per_bar=4_000),
                    None, NOW, KNOBS, avg_vol=AVG_VOL),
    ])
    import io
    from rich.console import Console
    Console(file=io.StringIO(), width=100).print(mf.build_view(rows, NOW))


def history_df(sessions=8, bars=20, vol_per_bar=23_000):
    """Minute bars across several COMPLETED sessions before NOW.

    avg_session_volume needs MIN_AVG_SESSIONS closed sessions and excludes
    today, so a single-session frame resolves to None no matter how healthy
    the fetch was.
    """
    frames = []
    for back in range(1, sessions + 1):
        day = NOW.date() - timedelta(days=back)
        frames.append(minute_df(
            start=f"{day.isoformat()} 09:30", bars=bars,
            vol_per_bar=vol_per_bar))
    return pd.concat(frames).sort_index()


def _reset_avg_cache():
    mf._AVG_VOL_CACHE.clear()
    mf._AVG_VOL_DATE = ""
    mf._AVG_VOL_RETRY_AT = 0.0
    # The day cache is also on disk (so a restart does not rebuild it with the
    # desk's heaviest request), and conftest points it at one tmpdir for the
    # whole session — so without this a previous test's baselines warm straight
    # back into the next one.
    mf._AVG_VOL_DISK.unlink(missing_ok=True)


def test_empty_history_batch_is_not_cached_as_a_verdict(monkeypatch):
    """An all-empty history response must not become a day-long "no rvol".

    The cache is keyed by the ET day and `wanted` skips anything already in it,
    so writing None for every symbol here is never retried until tomorrow.
    _split_batch returns {} both when the feed genuinely had nothing and when
    the batch degraded under load, and a live trending list — names trading
    heavily enough to trend — lacking 18 days of bars is not a real scenario.
    On 2026-08-07 this left all 26 trending rows with rvol=None from the open,
    which silently reduced admission to Stocktwits score alone.
    """
    _reset_avg_cache()
    monkeypatch.setattr(mf, "fetch_minutes_history", lambda *a, **k: {})
    out = mf.avg_session_volumes(object(), ["AAA", "BBB"], {}, NOW)
    assert out == {}, "an empty batch must cache nothing"

    # And once the feed recovers the symbols must actually resolve.
    _reset_avg_cache()
    monkeypatch.setattr(
        mf, "fetch_minutes_history",
        lambda *a, **k: {"AAA": history_df(), "BBB": history_df()})
    out = mf.avg_session_volumes(object(), ["AAA", "BBB"], {}, NOW)
    assert set(out) == {"AAA", "BBB"}
    assert all(v is not None for v in out.values())


def test_partial_history_still_caches_the_real_negatives(monkeypatch):
    """A non-empty response is trustworthy per symbol.

    Only the all-or-nothing empty is indistinguishable from a failure — if the
    feed answered for one name and not another, the absent one genuinely has no
    usable history and re-requesting it every cycle never resolves.
    """
    _reset_avg_cache()
    monkeypatch.setattr(
        mf, "fetch_minutes_history", lambda *a, **k: {"AAA": history_df()})
    out = mf.avg_session_volumes(object(), ["AAA", "BBB"], {}, NOW)
    assert out["AAA"] is not None
    assert "BBB" in out and out["BBB"] is None


def test_baselines_survive_a_process_restart_without_refetching(monkeypatch):
    """The day cache must outlive the process that built it.

    It is an average of COMPLETED sessions, so it is valid for the whole ET day
    — but it lived only in memory, so every restart rebuilt it with an 18-day
    batch for every symbol, against a rate limit four processes share. Losing it
    mid-session is not a slow start: the refetch 429s, the backoff holds for
    minutes, and rvol is None for all of it. On 2026-08-07 restarting the
    trending screener at 13:17 did exactly that.
    """
    _reset_avg_cache()
    calls = []

    def _fetch(*a, **k):
        calls.append(1)
        return {"AAA": history_df(), "BBB": history_df()}

    monkeypatch.setattr(mf, "fetch_minutes_history", _fetch)
    first = dict(mf.avg_session_volumes(object(), ["AAA", "BBB"], {}, NOW))
    assert len(calls) == 1
    assert all(v is not None for v in first.values())

    # Simulate a restart: in-memory state gone, disk file left alone.
    mf._AVG_VOL_CACHE.clear()
    mf._AVG_VOL_DATE = ""
    mf._AVG_VOL_RETRY_AT = 0.0

    after = mf.avg_session_volumes(object(), ["AAA", "BBB"], {}, NOW)
    assert after == first, "restart must reuse the persisted baselines"
    assert len(calls) == 1, "and must not spend another history request"


def test_stale_day_cache_on_disk_is_ignored(monkeypatch):
    """Yesterday's baselines are the wrong denominator for today."""
    _reset_avg_cache()
    monkeypatch.setattr(
        mf, "fetch_minutes_history",
        lambda *a, **k: {"AAA": history_df(), "BBB": history_df()})
    mf.avg_session_volumes(object(), ["AAA", "BBB"], {}, NOW)

    import json as _json
    raw = _json.loads(mf._AVG_VOL_DISK.read_text())
    raw["date"] = "1999-01-01"
    mf._AVG_VOL_DISK.write_text(_json.dumps(raw))

    mf._AVG_VOL_CACHE.clear()
    mf._AVG_VOL_DATE = ""
    assert mf._load_avg_disk(NOW.date().isoformat()) == {}


def test_corrupt_day_cache_never_raises():
    """A torn or hand-edited file must degrade to a refetch, not a crash."""
    _reset_avg_cache()
    mf._AVG_VOL_DISK.parent.mkdir(parents=True, exist_ok=True)
    mf._AVG_VOL_DISK.write_text("{not json at all")
    assert mf._load_avg_disk(NOW.date().isoformat()) == {}
