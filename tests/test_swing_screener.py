"""
test_swing_screener.py — offline tests for the swing-trade screener.

No network: Alpaca bars are monkeypatched via swing_screener.fetch_daily_bars /
fetch_daily_bars_batch, and Finnhub HTTP via swing_screener._finnhub_get.
Technicals run on synthetic daily bars. Covers the reversal gate, hard filters,
EPS-growth window, analyst consensus/trend, graceful degrade, run_screen output,
and the dashboard's load_swing + live-confluence overlay.

Run:
    venv/bin/python -m pytest tests/test_swing_screener.py -q
"""

import os
import sys
import time

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import swing_screener as fs   # noqa: E402
import dashboard as d         # noqa: E402


CFG = dict(
    swing_max_price=100.0, swing_rsi_oversold=30.0,
    swing_min_eps_growth=2.0, swing_max_eps_growth=3.0,
    swing_min_rating="Buy", swing_rel_vol_min=1.5, swing_min_rr=2.0,
    swing_earnings_window_days=14, swing_limit=12, swing_enrich_cap=40,
    ema_short=8, macd_fast=12, macd_slow=26, macd_signal=9,
)


def _bars(closes, volumes=None):
    """Build an oldest→newest OHLCV DataFrame from a close series."""
    n = len(closes)
    vols = volumes if volumes is not None else [1_000_000] * n
    return pd.DataFrame({
        "close": closes,
        "high":  [c * 1.01 for c in closes],
        "low":   [c * 0.99 for c in closes],
        "volume": vols,
    })


# ── Reversal confirmation ─────────────────────────────────────────────────────

def test_reversal_hook_up_confirms():
    closes = [100 - i for i in range(40)] + [64.0]   # decline then a real up bar
    t = fs._technicals(_bars(closes), CFG)
    assert t["oversold"] is True
    assert t["reversal_confirmed"] is True
    assert "rsi_hook" in t["reversal_signals"]


def test_reversal_still_falling_rejected():
    closes = [100 - i for i in range(45)]            # every bar down = falling knife
    t = fs._technicals(_bars(closes), CFG)
    assert t["oversold"] is True
    assert t["reversal_confirmed"] is False
    assert t["reversal_signals"] == []


def test_not_oversold_not_confirmed():
    closes = [50 + i for i in range(40)]             # uptrend → RSI high
    t = fs._technicals(_bars(closes), CFG)
    assert t["oversold"] is False
    assert t["reversal_confirmed"] is False


def test_rel_vol_and_support():
    closes = [100 - i for i in range(40)] + [61.0]
    vols = [1_000_000] * 40 + [3_000_000]
    t = fs._technicals(_bars(closes, vols), CFG)
    assert t["rel_vol"] > 2.0
    assert t["support"] == round(min(closes[-20:]) * 0.99, 2)


# ── Scoring & hard filters ────────────────────────────────────────────────────

def _candidate(**over):
    base = dict(
        ticker="XYZ", price=50.0, rsi=25.0, oversold=True,
        reversal_confirmed=True, reversal_signals=["rsi_hook"],
        rel_vol=2.0, support=45.0, target=95.0, rr=5.0,
        pe=10, pb=1.2, current_ratio=2.5, debt_to_equity=0.3,
        eps_growth=2.5, rating="Strong Buy",
        recent_upgrade=False, earnings_in_window=False, skipped=[],
    )
    base.update(over)
    return base


def test_score_accepts_clean_candidate():
    assert fs.score_candidate(_candidate(), CFG) is not None


def test_reject_over_price_ceiling():
    assert fs.score_candidate(_candidate(price=120.0), CFG) is None


def test_reject_no_reversal():
    assert fs.score_candidate(_candidate(reversal_confirmed=False), CFG) is None


def test_reject_low_rr():
    assert fs.score_candidate(_candidate(rr=1.0), CFG) is None


def test_reject_low_rel_vol():
    assert fs.score_candidate(_candidate(rel_vol=1.0), CFG) is None


def test_eps_growth_is_scoring_only_not_a_gate():
    # EPS-growth no longer hard-rejects (it's a weak free-tier proxy); values outside
    # the old 2–3× band still qualify and simply score lower.
    assert fs.score_candidate(_candidate(eps_growth=1.2), CFG) is not None
    assert fs.score_candidate(_candidate(eps_growth=5.0), CFG) is not None
    lo = fs.score_candidate(_candidate(eps_growth=1.0), CFG)
    hi = fs.score_candidate(_candidate(eps_growth=3.0), CFG)
    assert hi > lo


def test_reject_uncovered_instrument_when_finnhub_succeeded():
    # skipped=[] means Finnhub answered; no fundamentals at all → ETF/uncovered → drop.
    c = _candidate(pe=None, pb=None, rating=None, eps_growth=None, rr=None, skipped=[])
    assert fs.score_candidate(c, CFG) is None
    # …but if the fields are merely degraded (skipped populated), keep it.
    c2 = _candidate(pe=None, pb=None, rating=None, eps_growth=None, rr=None,
                    skipped=["metric", "rating"])
    assert fs.score_candidate(c2, CFG) is not None


def test_reject_weak_rating():
    assert fs.score_candidate(_candidate(rating="Hold"), CFG) is None
    assert fs.score_candidate(_candidate(rating="Buy"), CFG) is not None


def test_degraded_fields_do_not_reject():
    c = _candidate(rating=None, rr=None, eps_growth=None)
    assert fs.score_candidate(c, CFG) is not None


def test_upgrade_boost_and_earnings_penalty():
    up = fs.score_candidate(_candidate(recent_upgrade=True), CFG)
    base = fs.score_candidate(_candidate(), CFG)
    earn = fs.score_candidate(_candidate(earnings_in_window=True), CFG)
    assert up > base > earn


def test_technical_gate():
    assert fs._technical_gate(_candidate(), CFG) is True
    assert fs._technical_gate(_candidate(price=120.0), CFG) is False
    assert fs._technical_gate(_candidate(reversal_confirmed=False), CFG) is False
    assert fs._technical_gate(_candidate(rel_vol=1.0), CFG) is False


# ── Analyst consensus + trend (Finnhub recommendation) ────────────────────────

def test_consensus_and_trend():
    rec = [{"strongBuy": 20, "buy": 5, "hold": 1, "sell": 0, "strongSell": 0},
           {"strongBuy": 2,  "buy": 5, "hold": 10, "sell": 3, "strongSell": 0}]
    rating, improving = fs._consensus_and_trend(rec)
    assert rating == "Strong Buy"
    assert improving is True            # bullishness rose vs prior month

    rating2, improving2 = fs._consensus_and_trend([{"strongBuy": 0, "buy": 0, "hold": 8, "sell": 2, "strongSell": 0}])
    assert rating2 == "Hold"
    assert improving2 is False

    assert fs._consensus_and_trend([]) == (None, False)


# ── enrich + graceful degrade (Alpaca bars + Finnhub fundamentals) ────────────

def _install_fakes(monkeypatch, gated=()):
    """Fake Alpaca bars + Finnhub endpoints. `gated` = Finnhub path prefixes that
    raise _Degraded (simulating a paywalled/rate-limited endpoint)."""
    closes = [100 - i for i in range(40)] + [64.0]
    vols = [1_000_000] * 40 + [6_000_000]           # newest-bar volume surge
    df = _bars(closes, vols)
    monkeypatch.setattr(fs, "fetch_daily_bars", lambda sym, cfg: df.copy())

    soon = time.strftime("%Y-%m-%d", time.localtime(time.time() + 5 * 86400))

    def fake_finnhub(path, cfg, **params):
        for g in gated:
            if path.startswith(g):
                raise fs._Degraded(f"403 on {path}")
        if path == "stock/metric":
            return {"metric": {"peTTM": 25.0, "forwardPE": 10.0,   # 25/10 = 2.5×
                               "pbQuarterly": 1.3, "currentRatioQuarterly": 2.4,
                               "totalDebt/totalEquityQuarterly": 0.4, "52WeekHigh": 120.0}}
        if path == "stock/recommendation":
            return [{"strongBuy": 20, "buy": 5, "hold": 1, "sell": 0, "strongSell": 0},
                    {"strongBuy": 3,  "buy": 4, "hold": 9, "sell": 2, "strongSell": 0}]
        if path == "calendar/earnings":
            return {"earningsCalendar": [{"symbol": params.get("symbol"), "date": soon}]}
        return {}

    monkeypatch.setattr(fs, "_finnhub_get", fake_finnhub)


def test_enrich_full(monkeypatch):
    _install_fakes(monkeypatch)
    c = fs.enrich("XYZ", CFG)
    assert c["ticker"] == "XYZ"
    assert c["reversal_confirmed"] is True
    assert c["eps_growth"] == 2.5
    assert c["rating"] == "Strong Buy"
    assert c["recent_upgrade"] is True
    assert c["earnings_in_window"] is True
    assert c["current_ratio"] == 2.4
    assert c["skipped"] == []
    assert fs.score_candidate(c, CFG) is not None


def test_enrich_graceful_degrade(monkeypatch):
    _install_fakes(monkeypatch, gated=("stock/metric", "stock/recommendation"))
    c = fs.enrich("XYZ", CFG)
    assert c is not None                    # still produced, no exception
    assert c["rating"] is None
    assert c["eps_growth"] is None
    assert "metric" in c["skipped"] and "rating" in c["skipped"]
    assert fs.score_candidate(c, CFG) is not None   # degraded fields don't reject


def test_enrich_gated_out_before_fundamentals(monkeypatch):
    # A non-oversold uptrend fails the technical gate → enrich returns None and
    # never spends Finnhub calls.
    up = _bars([50 + i for i in range(40)])
    monkeypatch.setattr(fs, "fetch_daily_bars", lambda sym, cfg: up.copy())
    called = {"finnhub": False}
    def boom(*a, **k):
        called["finnhub"] = True
        return {}
    monkeypatch.setattr(fs, "_finnhub_get", boom)
    assert fs.enrich("XYZ", CFG) is None
    assert called["finnhub"] is False


# ── run_screen output ─────────────────────────────────────────────────────────

def test_run_screen_writes_sorted_capped(monkeypatch, tmp_path):
    monkeypatch.setattr(fs, "SWING_FILE", tmp_path / "swing_candidates.json")
    monkeypatch.setattr(fs, "screen_universe", lambda cfg: ["AAA", "BBB", "CCC"])

    # All three pass the technical gate; scores decide ranking.
    passing = _bars([100 - i for i in range(40)] + [64.0], [1_000_000] * 40 + [6_000_000])
    monkeypatch.setattr(fs, "fetch_daily_bars_batch",
                        lambda syms, cfg: {s: passing.copy() for s in syms})
    monkeypatch.setattr(fs, "add_fundamentals", lambda sym, c, cfg: c)
    scores = {"AAA": 1.0, "BBB": 3.0, "CCC": 2.0}
    monkeypatch.setattr(fs, "score_candidate", lambda c, cfg: scores[c["ticker"]])

    cfg = dict(CFG, swing_limit=2)
    out = fs.run_screen(cfg)
    assert [c["ticker"] for c in out] == ["BBB", "CCC"]     # sorted desc, capped at 2

    import json
    data = json.loads((tmp_path / "swing_candidates.json").read_text())
    assert "updated" in data
    assert [c["ticker"] for c in data["candidates"]] == ["BBB", "CCC"]


# ── Scheduler ─────────────────────────────────────────────────────────────────

def test_seconds_until_next_run():
    from datetime import datetime
    cfg = {"swing_run_times": ["08:00", "12:30", "20:00"]}
    now = datetime(2026, 7, 4, 9, 0, tzinfo=fs.ET)      # 09:00 → next is 12:30
    secs = fs._seconds_until_next_run(cfg, now)
    assert abs(secs - 3.5 * 3600) < 2

    late = datetime(2026, 7, 4, 22, 0, tzinfo=fs.ET)    # after last → tomorrow 08:00
    secs2 = fs._seconds_until_next_run(cfg, late)
    assert abs(secs2 - 10 * 3600) < 2


# ── Dashboard: load_swing + live confluence overlay ───────────────────────────

@pytest.fixture(autouse=True)
def _clean_state():
    with d.STATE.lock:
        d.STATE.discord_alerts.clear()
        d.STATE.sentiment_events.clear()
    d._swing_cache["mtime"] = -1.0
    yield


def test_load_swing_parses_file(monkeypatch, tmp_path):
    import json
    f = tmp_path / "swing_candidates.json"
    f.write_text(json.dumps({"updated": 1.0, "candidates": [{"ticker": "ABC"}]}))
    monkeypatch.setattr(d, "SWING_FILE", f)
    d._swing_cache["mtime"] = -1.0
    assert d.load_swing() == [{"ticker": "ABC"}]


def test_snapshot_overlays_confluence(monkeypatch):
    monkeypatch.setattr(d, "load_swing", lambda: [{"ticker": "ABC", "score": 9.0}])
    with d.STATE.lock:
        d.STATE.sentiment_events["ABC"] = [
            {"ticker": "ABC", "score": 1.0, "source": "chat", "raw": "x", "ts": time.time()}
        ]
        d.STATE.discord_alerts.append({"ticker": "ABC", "burst": False})

    snap = d._snapshot()
    abc = next(c for c in snap["swing"] if c["ticker"] == "ABC")
    assert abc["confluence"]["count"] >= 2
    assert "chat" in abc["confluence"]["sources"]
