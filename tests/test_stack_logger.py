import gzip
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import stack_logger as sl  # noqa: E402


def _state():
    return {"trending": {"updated": 1.0, "error": "", "rows": [{"symbol": "CAPR", "rank": 1, "trending_score": 14.3,
                                                              "title": "x", "price": 7.5}]},
            "movers": {"ts": 2.0, "rows": [{"symbol": "INTC", "rvol": 0.9, "pct_change": 1.9, "reason": "skip"}]},
            "ai_suggestions": {"rows": [{"symbol": "AMBA", "p30": 0.48, "summary": "long text"}]},
            "discord": {"running": True, "count": 15, "alerts": [{"ticker": "SPY"}]},
            "tickers": [{"ticker": "IREN", "price": 14.45, "mention_count": 3, "mention_window": 2, "mention_burst": False,
                         "signal_proximity": {"status": "watching", "cm_rsi": 95.4, "pctr": -60.9, "buy_price": None},
                         "funnel": {"score": 0, "rejects": ["no data"]}}]}


def test_snapshot_keeps_layers_and_drops_bulk():
    s = sl.snapshot(_state(), 1795173120.0)
    assert s["trending"] == [{"symbol": "CAPR", "rank": 1, "trending_score": 14.3, "price": 7.5}]
    assert s["movers"][0] == {"symbol": "INTC", "rvol": 0.9, "pct_change": 1.9}
    assert s["ai"][0] == {"symbol": "AMBA", "p30": 0.48}
    t = s["tickers"][0]
    assert t["mention_count"] == 3 and t["mention_burst"] is False
    assert t["sp"] == {"status": "watching", "cm_rsi": 95.4, "pctr": -60.9}
    assert s["discord_n"] == 15 and s["health"]["discord_running"] is True
    json.dumps(s)


def test_snapshot_survives_missing_sections():
    s = sl.snapshot({}, 0.0)
    assert s["trending"] == [] and s["tickers"] == [] and s["discord_n"] is None


def test_window_weekdays_0400_to_2000_et():
    # 2026-11-20 is a Friday; 2026-11-21 a Saturday (UTC-5)
    assert sl.in_window(1795165200.0)          # Fri 04:00 ET
    assert not sl.in_window(1795165199.0)      # Fri 03:59:59
    assert not sl.in_window(1795222800.0)      # Fri 20:00
    assert not sl.in_window(1795251600.0)      # Sat 04:00


def test_append_is_readable_as_one_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(sl, "OUT", tmp_path)
    for i in range(3):
        sl.append({"i": i}, 1795173120.0 + i)
    p = next(tmp_path.glob("*.jsonl.gz"))
    rows = [json.loads(x) for x in gzip.open(p, "rt")]
    assert [r["i"] for r in rows] == [0, 1, 2]


def test_catalysts_from_local_caches(monkeypatch):
    import types
    nf = types.SimpleNamespace(cache_age_sec=lambda: 12.0,
                               features_for=lambda s, now: {"n_news_24h": 2, "mins_since": 5, "bearish": None} if s == "AAA" else {"n_news_24h": None})
    ff = types.SimpleNamespace(load_cache=lambda: {"AAA": {"shares_out": 10.5, "other": 1}})
    monkeypatch.setitem(sys.modules, "news_feed", nf)
    monkeypatch.setitem(sys.modules, "float_feed", ff)
    c = sl.catalysts(["AAA", "BBB"], 0.0)
    assert c["AAA"] == {"n_news_24h": 2, "mins_since": 5, "shares_out": 10.5}
    assert "BBB" not in c and c["_news_cache_age_sec"] == 12.0
    s = sl.snapshot(_state(), 0.0)
    assert set(s["catalysts"]) >= {"_news_cache_age_sec"}
