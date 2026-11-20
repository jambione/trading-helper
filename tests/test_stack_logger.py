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


def test_parse_halts_and_ssr():
    xml = """<rss><channel><item><title>NTCL</title><ndaq:HaltDate>11/19/2026</ndaq:HaltDate>
    <ndaq:HaltTime>19:50:00.000</ndaq:HaltTime><ndaq:IssueSymbol>NTCL</ndaq:IssueSymbol><ndaq:ReasonCode>T1</ndaq:ReasonCode>
    <ndaq:ResumptionQuoteTime /><ndaq:ResumptionTradeTime /></item></channel></rss>"""
    h = sl.parse_halts(xml)
    assert h == [{"sym": "NTCL", "date": "11/19/2026", "time": "19:50:00.000", "reason": "T1", "resume_quote": "",
                  "resume_trade": ""}]
    ssr = sl.parse_ssr("Symbol,Security Name,Market Category,Trigger Time\nQTRX,Quanterix,Q,11/18/2026 9:30:00 AM\n")
    assert ssr == {"QTRX"}


def test_short_interest_table(tmp_path, monkeypatch):
    f = tmp_path / "shrt20261030.csv"
    f.write_text('"accountingYearMonthNumber"|"symbolCode"|"issueName"|"currentShortPositionQuantity"|"daysToCoverQuantity"|"settlementDate"\n'
                 '"20261030"|"A"|"Agilent"|"4892390"|"2.24"|"2026-10-30"\n')
    monkeypatch.setattr(sl, "SI_DIR", tmp_path)
    sl._slow["si_file"] = None
    t = sl.short_interest_table()
    assert t["A"] == {"short": 4892390, "dtc": 2.24, "settle": "2026-10-30"}


def test_parse_stream_sentiment():
    p = {"messages": [{"created_at": "2026-11-20T11:52:11Z", "entities": {"sentiment": None}},
                      {"created_at": "2026-11-20T11:42:11Z", "entities": {"sentiment": {"basic": "Bullish"}}},
                      {"created_at": "2026-11-20T11:22:11Z", "entities": {"sentiment": {"basic": "Bearish"}}},
                      {"created_at": "2026-11-20T10:52:11Z", "entities": {"sentiment": {"basic": "Bullish"}}}]}
    now = datetime_ts = 1795175531.0     # 2026-11-20T11:52:11Z
    r = sl.parse_stream(p, now)
    assert r["n"] == 4 and r["bull"] == 2 and r["bear"] == 1 and r["bull_share"] == round(2 / 3, 3)
    assert r["newest_age_min"] == 0.0 and r["msgs_per_hour"] == 3.0


def test_sentiment_caps_and_throttles(monkeypatch):
    calls = []
    monkeypatch.setattr(sl, "_get", lambda url, timeout=8.0: calls.append(url) or '{"messages": []}')
    sl._sent.update({"t": 0.0, "data": {}, "rr": 0})
    out = sl.sentiment([f"P{i}" for i in range(5)], [f"O{i}" for i in range(40)], 10_000.0)
    assert len(calls) == 20 and all(f"P{i}" in out for i in range(5))
    sl.sentiment(["P0"], [], 10_100.0)
    assert len(calls) == 20                  # within 10 minutes: no new fetches
