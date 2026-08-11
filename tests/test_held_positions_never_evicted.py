"""Held positions must never lose their live market data.

The engine computes indicators only for names on the momentum ticker list, and
that list is bounded two ways: a 15-minute age purge and a hard cap, both keyed
on recency. Recency is a reasonable proxy for "still worth watching" while a
symbol is only a candidate. Once the desk owns it the question changes to "when
do I get out", and that cannot be answered without live CM RSI, %R and
sell_signal. A name bought at 14:20 was retired by 14:35 while still held, and
the sell_signal defence in ai_positions could never fire for it.

Operator rule: if we are in a position, that symbol's data takes precedence.
"""
import json
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _ROOT)

import dashboard as dash  # noqa: E402


def _write_log(tmp_path, entries):
    p = tmp_path / "wb_watchlist.json"
    p.write_text(json.dumps(entries), encoding="utf-8")
    return p


def _iso(minutes_ago):
    return (datetime.now(dash.ET) - timedelta(minutes=minutes_ago)).isoformat(
        timespec="seconds")


def _arm(monkeypatch, tmp_path, entries, held=frozenset()):
    monkeypatch.setattr(dash, "TICKER_LOG", _write_log(tmp_path, entries))
    monkeypatch.setattr(dash, "_committed_symbols", lambda: frozenset(held))
    dash._ticker_cache.update(mtime=-1.0, tickers=[], entries=[])


def test_a_held_position_survives_the_age_purge(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path, [
        {"ticker": "HELD", "added": _iso(90)},    # far past the 15-min cutoff
        {"ticker": "STALE", "added": _iso(90)},
    ], held={"HELD"})

    out = dash.load_tickers()

    assert "HELD" in out, "a position must not age out from under the desk"
    assert "STALE" not in out, "an ordinary stale candidate still goes"


def test_a_held_position_survives_the_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "TICKER_MAX_COUNT", 3)
    _arm(monkeypatch, tmp_path, [
        {"ticker": "HELD", "added": _iso(14)},    # oldest -> first to be cut
        {"ticker": "AAA", "added": _iso(4)},
        {"ticker": "BBB", "added": _iso(3)},
        {"ticker": "CCC", "added": _iso(2)},
        {"ticker": "DDD", "added": _iso(1)},
    ], held={"HELD"})

    out = dash.load_tickers()

    assert "HELD" in out
    assert "DDD" in out, "newest candidates still kept"
    assert len(out) <= 3 + 1, "cap still bounds the non-held tail"


def test_a_held_position_is_re_admitted_if_missing(monkeypatch, tmp_path):
    """The case that matters most: bought after the symbol already aged out."""
    _arm(monkeypatch, tmp_path, [
        {"ticker": "AAA", "added": _iso(1)},
    ], held={"BOUGHT"})

    out = dash.load_tickers()

    assert "BOUGHT" in out, "a held name absent from the list must come back"
    assert "AAA" in out


def test_nothing_changes_when_flat(monkeypatch, tmp_path):
    """No positions -> exactly the old behaviour, purge and cap intact."""
    monkeypatch.setattr(dash, "TICKER_MAX_COUNT", 2)
    _arm(monkeypatch, tmp_path, [
        {"ticker": "OLD", "added": _iso(90)},
        {"ticker": "AAA", "added": _iso(3)},
        {"ticker": "BBB", "added": _iso(2)},
        {"ticker": "CCC", "added": _iso(1)},
    ], held=frozenset())

    out = dash.load_tickers()

    assert "OLD" not in out
    assert len(out) == 2
    assert out == ["CCC", "BBB"]


def test_committed_symbols_includes_live_watch_book_rows(
        monkeypatch, tmp_path):
    """Live book rows (including watching) need tape; only done statuses drop."""
    monkeypatch.setattr(dash, "_held_cache", (0.0, frozenset()))
    rd = tmp_path / "reports"
    rd.mkdir()
    (rd / "positions_state.json").write_text(json.dumps({"POS": {}}))
    (rd / "entry_watch_state.json").write_text(json.dumps({
        "SUBM": {"status": "submitted"},
        "WATCH": {"status": "watching"},
        "DONE": {"status": "expired"},
    }))
    import ai_paths
    monkeypatch.setattr(ai_paths, "resolve_report_dir", lambda: rd)

    out = dash._committed_symbols()

    assert "POS" in out and "SUBM" in out and "WATCH" in out
    assert "DONE" not in out


def test_committed_symbols_fails_open_on_a_missing_file(monkeypatch, tmp_path):
    """A bad read must not take the whole watchlist down."""
    monkeypatch.setattr(dash, "_held_cache", (0.0, frozenset()))
    import ai_paths
    monkeypatch.setattr(ai_paths, "resolve_report_dir",
                        lambda: tmp_path / "does_not_exist")
    assert dash._committed_symbols() == frozenset()
