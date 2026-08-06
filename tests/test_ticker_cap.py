"""The momentum watchlist is bounded by count, not just by age.

Age alone did not hold it: the feeds re-add faster than the 15-minute purge
retires, so the panel drifted to 26+ entries on 2026-08-06. Each one costs a
quote on a desk already past Alpaca's rate limit, and the same list seeds AI
Watch's momentum candidates — so an unbounded panel silently widens the book's
intake too.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dashboard as d  # noqa: E402


def _seed(tmp_path, monkeypatch, entries):
    p = tmp_path / "ticker_log.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(d, "TICKER_LOG", p)
    monkeypatch.setattr(d, "_ticker_cache",
                        {"mtime": -1.0, "tickers": [], "entries": []})
    return p


def _entry(sym, *, minutes_ago):
    ts = datetime.now(d.ET) - timedelta(minutes=minutes_ago)
    return {"ticker": sym, "added": ts.isoformat(timespec="seconds")}


def test_keeps_only_the_newest_up_to_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "TICKER_MAX_COUNT", 8)
    # 12 fresh entries, staggered: AAA newest ... LLL oldest.
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF",
            "GGG", "HHH", "III", "JJJ", "KKK", "LLL"]
    _seed(tmp_path, monkeypatch,
          [_entry(s, minutes_ago=i) for i, s in enumerate(syms)])

    got = d.load_tickers()
    assert len(got) == 8
    assert set(got) == set(syms[:8]), "kept the wrong 8 — must be the newest"
    assert "LLL" not in got and "III" not in got


def test_cap_is_written_back_so_the_eviction_sticks(tmp_path, monkeypatch):
    """The file is the source of truth for quoting, the trade_bridge auto-watch
    and the Finnhub subscription set. If eviction only happened in memory the
    dropped names would keep costing quotes."""
    monkeypatch.setattr(d, "TICKER_MAX_COUNT", 3)
    p = _seed(tmp_path, monkeypatch,
              [_entry(s, minutes_ago=i)
               for i, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"])])

    d.load_tickers()
    on_disk = json.loads(p.read_text(encoding="utf-8"))
    assert [e["ticker"] for e in on_disk] == ["AAA", "BBB", "CCC"]
    # No internal bookkeeping leaks into the persisted file.
    assert all(set(e.keys()) == {"ticker", "added"} for e in on_disk)


def test_stale_entries_are_purged_before_the_cap_applies(tmp_path, monkeypatch):
    """Order matters: capping first could retire a fresh name in favour of a
    stale one that was about to be purged anyway."""
    monkeypatch.setattr(d, "TICKER_MAX_COUNT", 3)
    _seed(tmp_path, monkeypatch, [
        _entry("OLDA", minutes_ago=60),   # stale, beyond TICKER_MAX_AGE
        _entry("OLDB", minutes_ago=45),   # stale
        _entry("NEWA", minutes_ago=1),
        _entry("NEWB", minutes_ago=2),
        _entry("NEWC", minutes_ago=3),
    ])

    got = d.load_tickers()
    assert set(got) == {"NEWA", "NEWB", "NEWC"}


def test_under_the_cap_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "TICKER_MAX_COUNT", 8)
    _seed(tmp_path, monkeypatch,
          [_entry(s, minutes_ago=i) for i, s in enumerate(["AAA", "BBB"])])

    got = d.load_tickers()
    assert set(got) == {"AAA", "BBB"}


def test_cap_bounds_what_ai_watch_can_seed(tmp_path, monkeypatch):
    """ai_entry_watch._momentum_flagged_from_dashboard reads this same list, so
    the cap is not display-only — it bounds the book's momentum intake."""
    monkeypatch.setattr(d, "TICKER_MAX_COUNT", 4)
    syms = [f"Z{chr(65 + i // 26)}{chr(65 + i % 26)}" for i in range(20)]
    _seed(tmp_path, monkeypatch,
          [_entry(s, minutes_ago=i) for i, s in enumerate(syms)])

    assert len(d.load_tickers()) == 4
