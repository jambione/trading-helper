"""momentum_premarket count mode: alert window, price band, usable-price rule, and that no return is computed."""
from __future__ import annotations

import gzip
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import momentum_premarket as mp  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(h, m, s=0):
    return datetime(2026, 11, 4, h, m, s, tzinfo=ET).timestamp()


def test_first_alert_in_window_only(tmp_path, monkeypatch):
    d = tmp_path / "2026-11-04"; d.mkdir()
    rows = [{"ts": _ts(3, 59), "symbol": "EARLY"}, {"ts": _ts(5, 10), "symbol": "AAA"}, {"ts": _ts(5, 30), "symbol": "AAA"},
            {"ts": _ts(9, 15, 30), "symbol": "LATE"}, {"ts": _ts(9, 15), "symbol": "EDGE"}]
    with gzip.open(d / "discord.jsonl.gz", "wt") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    monkeypatch.setattr(mp, "SESS", tmp_path)
    out = mp.first_alerts("2026-11-04")
    assert set(out) == {"AAA", "EDGE"} and out["AAA"] == _ts(5, 10)


def test_bands():
    assert [mp.band_of(x) for x in (1.5, 2.0, 4.99, 5, 19.9, 20, 100)] == ["<2", "2-5", "2-5", "5-10", "10-20", "20+", "20+"]


def test_classify_usable_price_and_liquidity_counts_only():
    ts = _ts(6, 30, 20)
    m = ts - (ts % 60)
    bars = [(m - 60 * k, 5.0, 5.1, 4.9, 5.05, 100.0 if k % 2 == 0 else 0.0) for k in range(10, -1, -1)] + \
           [(m + 60 * k, 5.0, 5.2, 4.8, 9.99, 50.0) for k in range(1, 6)]
    c = mp.classify(bars, ts)
    assert c["usable"] and c["px"] == 5.05 and c["after_prints"] == 5 and c["bars_5m"] >= 2
    assert set(c) == {"usable", "px", "bars_5m", "bars_total_pre", "after_prints", "after_minutes"}   # no return or forward price field
    stale = [(m - 900, 5.0, 5.1, 4.9, 5.05, 100.0)]
    assert mp.classify(stale, ts) == {"usable": False}
    assert mp.classify([], ts) is None


def test_report_renders():
    rows = [{"day": "2026-11-04", "sym": "A", "ts": 1.0, "usable": True, "px": 3.0, "bars_5m": 4, "bars_total_pre": 30, "after_prints": 40, "after_minutes": 150},
            {"day": "2026-11-04", "sym": "B", "ts": 1.0, "usable": False}]
    t = mp.report(rows, ["2026-10-01"])
    assert "2 name-days over 1 sessions" in t and "| 2-5 | 1 |" in t
