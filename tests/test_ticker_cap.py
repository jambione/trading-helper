"""The momentum watchlist is bounded by count, not just by age.

Age alone did not hold it: the feeds re-add faster than the 15-minute purge
retires, so the panel drifted to 26+ entries on 2026-08-06. Each one costs a
quote on a desk already past Alpaca's rate limit, and the same list seeds AI
Watch's momentum candidates — so an unbounded panel silently widens the book's
intake too.
"""

import json
import sys
import threading
import time
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
    # These tests are about cap arithmetic. _committed_symbols() reads the
    # shared temp report dir (entry_watch_state.json / positions_state.json)
    # and protects whatever it finds from eviction, so any earlier test file
    # that seeded a watch book made this one keep cap+1 names — the failures
    # appeared only in a full run and never in isolation. Protection of held
    # and watched names has its own file: test_held_positions_never_evicted.
    monkeypatch.setattr(d, "_committed_symbols", frozenset)
    monkeypatch.setattr(d, "_held_cache", (0.0, frozenset()))
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


def test_snapshot_does_not_take_ticker_lock_while_holding_state(tmp_path, monkeypatch):
    """Lock inversion: STATE then _ticker_lock vs remove_ticker_from_log.

    That deadlock filled the default executor with hung /api/state work, so
    /auth/login timed out and the momentum desk printed login-failed.
    """
    _seed(tmp_path, monkeypatch, [_entry("AAA", minutes_ago=1)])
    d.load_tickers()
    monkeypatch.setattr(d, "load_news", lambda: [])
    monkeypatch.setattr(d, "load_swing", lambda: [])
    monkeypatch.setattr(d, "load_rs", lambda: {})
    monkeypatch.setattr(d, "load_claude_suggestions", lambda: {})
    monkeypatch.setattr(d, "load_grok_suggestions", lambda: {})
    monkeypatch.setattr(d, "build_ai_suggestions", lambda *a, **k: {})
    monkeypatch.setattr(d, "load_ai_positions", lambda: {})
    monkeypatch.setattr(d, "load_trending", lambda: [])
    monkeypatch.setattr(d, "_build_mention_rank", lambda *a, **k: {})
    monkeypatch.setattr(d, "_load_signal_state", lambda: {})
    monkeypatch.setattr(d, "_fh_subscribe", lambda *a, **k: None)
    monkeypatch.setattr(d, "overlay_ai_book_live_prices", lambda p, **k: p or {})

    started = threading.Event()
    done = threading.Event()

    def _run():
        started.set()
        d._snapshot()
        done.set()

    released = False
    d._ticker_lock.acquire()
    try:
        t = threading.Thread(target=_run, name="snap")
        t.start()
        assert started.wait(1.0)
        time.sleep(0.15)
        # Waiting on _ticker_lock is fine. Holding STATE.lock while doing
        # so is the inversion — remove_ticker_from_log takes them the
        # other way and the desk never comes back.
        assert not d.STATE.lock.locked(), (
            "_snapshot held STATE.lock while blocked on _ticker_lock")
        d._ticker_lock.release()
        released = True
        assert done.wait(3.0), "_snapshot hung after ticker lock was released"
        t.join(1.0)
    finally:
        if not released:
            d._ticker_lock.release()
