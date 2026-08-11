"""Momentum candidates need RVOL ≥ floor once volume is known.

Unknown rvol is provisional (Discord can land a name before the first sample).
Held positions and src=book rows skip the floor — they need tape, not a heat
filter.
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


def _entry(sym, *, minutes_ago, src=""):
    ts = datetime.now(d.ET) - timedelta(minutes=minutes_ago)
    row = {"ticker": sym, "added": ts.isoformat(timespec="seconds")}
    if src:
        row["src"] = src
    return row


def _set_rvol(sym, rvol):
    with d.STATE.lock:
        d.STATE.tickers.setdefault(sym, {})["rvol"] = rvol


def _clear_rvol(sym):
    with d.STATE.lock:
        row = d.STATE.tickers.get(sym)
        if row:
            row.pop("rvol", None)


def test_purges_known_low_rvol_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "TICKER_MIN_RVOL", 2.0)
    monkeypatch.setattr(d, "TICKER_MAX_COUNT", 8)
    monkeypatch.setattr(d, "_committed_symbols", lambda: frozenset())
    _seed(tmp_path, monkeypatch, [
        _entry("HOT", minutes_ago=1),
        _entry("COLD", minutes_ago=1),
        _entry("UNKN", minutes_ago=1),
    ])
    _set_rvol("HOT", 3.5)
    _set_rvol("COLD", 1.2)
    _clear_rvol("UNKN")

    got = d.load_tickers()
    assert "HOT" in got
    assert "UNKN" in got, "unknown rvol stays provisionally"
    assert "COLD" not in got


def test_held_and_book_skip_rvol_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "TICKER_MIN_RVOL", 2.0)
    monkeypatch.setattr(d, "TICKER_MAX_COUNT", 8)
    monkeypatch.setattr(d, "_committed_symbols", lambda: frozenset({"HELD"}))
    _seed(tmp_path, monkeypatch, [
        _entry("HELD", minutes_ago=1),
        _entry("BOOK", minutes_ago=1, src="book"),
        _entry("COLD", minutes_ago=1),
    ])
    _set_rvol("HELD", 0.4)
    _set_rvol("BOOK", 0.5)
    _set_rvol("COLD", 0.6)

    got = d.load_tickers()
    assert "HELD" in got
    assert "BOOK" in got
    assert "COLD" not in got


def test_add_refuses_known_low_rvol(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "TICKER_MIN_RVOL", 2.0)
    monkeypatch.setattr(d, "_committed_symbols", lambda: frozenset())
    _seed(tmp_path, monkeypatch, [])
    _set_rvol("SLOW", 0.8)

    ok, is_new = d.add_ticker_to_log("SLOW")
    assert ok is True
    assert is_new is False
    assert d.load_tickers() == []


def test_add_allows_unknown_and_hot_rvol(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "TICKER_MIN_RVOL", 2.0)
    monkeypatch.setattr(d, "_committed_symbols", lambda: frozenset())
    _seed(tmp_path, monkeypatch, [])
    _clear_rvol("NEWB")
    _set_rvol("FAST", 4.0)

    ok1, new1 = d.add_ticker_to_log("NEWB")
    ok2, new2 = d.add_ticker_to_log("FAST")
    assert (ok1, new1) == (True, True)
    assert (ok2, new2) == (True, True)
    assert set(d.load_tickers()) == {"NEWB", "FAST"}


def test_add_book_ignores_low_rvol(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "TICKER_MIN_RVOL", 2.0)
    monkeypatch.setattr(d, "_committed_symbols", lambda: frozenset())
    _seed(tmp_path, monkeypatch, [])
    _set_rvol("SEED", 0.3)

    ok, is_new = d.add_ticker_to_log("SEED", src="book")
    assert (ok, is_new) == (True, True)
    assert d.load_tickers() == ["SEED"]


def test_floor_disabled_keeps_low_rvol(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "TICKER_MIN_RVOL", 0.0)
    monkeypatch.setattr(d, "TICKER_MAX_COUNT", 8)
    monkeypatch.setattr(d, "_committed_symbols", lambda: frozenset())
    _seed(tmp_path, monkeypatch, [_entry("COLD", minutes_ago=1)])
    _set_rvol("COLD", 0.2)

    assert d.load_tickers() == ["COLD"]
