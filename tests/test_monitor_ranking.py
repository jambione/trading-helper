"""T0.1 — row_rank() ordering + the momentum_table() column seam.

These are regression locks, not feature tests: every later roadmap ticket
claims "ordering/columns unchanged", and this file is what that claim is
measured against.

Assertions are on cell *content* and column metadata, never on rendered box
art — a rich upgrade changes borders and would otherwise fail spuriously.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from momentum_signal import (  # noqa: E402
    MOMENTUM_COLUMNS,
    Feed,
    momentum_table,
    row_rank,
)

# Fixed clock; every fixture timestamp is relative to this.
T0 = 1753449600.0


class _NullAlerter:
    """Alerter stand-in — the ordering tests must not beep or notify."""

    def __init__(self):
        self.fired = []

    def fire(self, kind, sym, detail=""):
        self.fired.append((kind, sym, detail))


NO_ALERTS = {"alert_new": False, "alert_burst": False, "alert_buy": False}


def _feed(ages: dict, rows: list, *, new_ttl=120.0, seeded=True) -> Feed:
    f = Feed({"new_ttl": new_ttl})
    f.rows = list(rows)
    f.seeded = seeded
    for sym, age in ages.items():
        f.first_seen[sym] = T0 - age
    return f


# ── row_rank ─────────────────────────────────────────────────────────────────

def test_newer_first_seen_sorts_above_older():
    """A symbol first seen 10s ago outranks one first seen 300s ago."""
    cfg = {"new_ttl": 120.0}
    fresh = row_rank({"ticker": "FRESH"}, T0 - 10.0, T0, 5, cfg)
    stale = row_rank({"ticker": "STALE"}, T0 - 300.0, T0, 0, cfg)
    assert fresh < stale


def test_freshness_does_not_expire_at_new_ttl():
    """Past new_ttl, first_seen STILL wins — it is not a decaying term.

    The roadmap's T0.1 text says server order takes over once new_ttl
    elapses. It does not, and never has: `new_ttl` gates only the NEW
    badge via Feed.is_fresh(). Locking real behavior here so a later
    ticket cannot quietly "fix" the ordering into a regression.
    """
    cfg = {"new_ttl": 120.0}
    # Both far past the 120s TTL. Server order is deliberately inverted:
    # the newer row arrived LAST from the server (idx 9 vs 0).
    newer = row_rank({"ticker": "NEWER"}, T0 - 300.0, T0, 9, cfg)
    older = row_rank({"ticker": "OLDER"}, T0 - 900.0, T0, 0, cfg)
    assert newer < older, "first_seen must dominate regardless of TTL"


def test_server_index_breaks_first_seen_ties():
    cfg = {"new_ttl": 120.0}
    a = row_rank({"ticker": "AAAA"}, T0 - 600.0, T0, 3, cfg)
    b = row_rank({"ticker": "BBBB"}, T0 - 600.0, T0, 7, cfg)
    assert a < b


def test_unseen_symbol_sorts_last():
    """first_seen defaulting to 0.0 puts never-seen rows at the bottom."""
    cfg = {"new_ttl": 120.0}
    seen = row_rank({"ticker": "SEEN"}, T0 - 9999.0, T0, 0, cfg)
    unseen = row_rank({"ticker": "GHOST"}, 0.0, T0, 0, cfg)
    assert seen < unseen


def test_rank_is_deterministic_and_stable():
    """Same inputs -> same key. Guards the T5.2 stability requirement."""
    cfg = {"new_ttl": 120.0}
    row = {"ticker": "AAAA", "pct_change": 5.0}
    keys = {row_rank(row, T0 - 30.0, T0 + i, 2, cfg) for i in range(5)}
    assert len(keys) == 1, "rank must not drift with wall-clock alone"


# ── ordering through the real ingest path ────────────────────────────────────

SERVER_ROWS = [
    {"ticker": "AAAA"}, {"ticker": "BBBB"}, {"ticker": "CCCC"},
    {"ticker": "DDDD"}, {"ticker": "EEEE"},
]


def test_ingest_ordering_matches_recorded_behavior():
    """Golden order captured from the pre-refactor implementation.

    DDDD is absent from first_seen, so ingest stamps it at `now` -> newest
    -> top. Then EEEE (10s), AAAA (300s, past TTL but still ahead),
    then the BBBB/CCCC tie resolved by server order.
    """
    ages = {"EEEE": 10.0, "AAAA": 300.0, "BBBB": 600.0, "CCCC": 600.0}
    f = _feed(ages, [], new_ttl=120.0)
    f.ingest({"tickers": [dict(r) for r in SERVER_ROWS]}, T0,
             _NullAlerter(), NO_ALERTS)
    assert [r["ticker"] for r in f.rows] == \
        ["DDDD", "EEEE", "AAAA", "BBBB", "CCCC"]


def test_ingest_is_stable_across_repeated_polls():
    """Re-ingesting an unchanged snapshot must not reshuffle rows.

    Jitter on a 2s refresh is worse than a wrong order.
    """
    ages = {"AAAA": 40.0, "BBBB": 40.0, "CCCC": 41.0}
    f = _feed(ages, [], new_ttl=120.0)
    seen = []
    for i in range(4):
        f.ingest({"tickers": [dict(r) for r in SERVER_ROWS]}, T0 + i * 2.0,
                 _NullAlerter(), NO_ALERTS)
        seen.append([r["ticker"] for r in f.rows])
    assert seen[0] == seen[1] == seen[2] == seen[3]


def test_ingest_survives_null_ticker_row():
    """A row with ticker=None must not crash the sort (and so the loop)."""
    f = _feed({}, [], new_ttl=120.0)
    f.ingest({"tickers": [{"ticker": "AAAA"}, {"ticker": None}]}, T0,
             _NullAlerter(), NO_ALERTS)
    assert "AAAA" in [(r.get("ticker") or "") for r in f.rows]


# ── column seam ──────────────────────────────────────────────────────────────

HISTORICAL_HEADERS = ["#", "Symbol", "Added", "Price", "Chg%", "Mentions",
                      "Setup", ""]


def test_column_headers_and_order_unchanged():
    assert [h for h, _, _ in MOMENTUM_COLUMNS] == HISTORICAL_HEADERS


def test_rendered_columns_match_spec():
    f = _feed({"AAAA": 10.0}, [{"ticker": "AAAA", "price": 1.0}])
    t = momentum_table(f, T0, 0.5, True)
    assert [c.header for c in t.columns] == HISTORICAL_HEADERS
    assert [c.justify for c in t.columns] == \
        ["right", "left", "right", "right", "right", "right", "right", "left"]


def _cells(table):
    """{header: [cell, ...]} keyed positionally so the blank header works."""
    return [list(c.cells) for c in table.columns]


def test_cell_content_golden():
    """Exact cell strings for a fixture row set — this is where the
    "byte-identical render" acceptance actually lives."""
    rows = [
        {"ticker": "AAAA", "price": 3.41, "pct_change": 12.4,
         "mention_window": 9, "mention_count": 31, "mention_burst": True,
         "find_it_first": True,
         "confluence": {"sources": ["alert", "squeeze"], "count": 2},
         "signal_proximity": {"cm_rsi": 22.0, "pctr": -91.0,
                              "pctr_slow": -88.0, "pctr_deep_os": True}},
        {"ticker": "BBBB", "price": 12.005, "pct_change": -3.2,
         "mention_window": 0, "mention_count": 4,
         "signal_proximity": {"cm_rsi": 44.0, "pctr": -40.0,
                              "pctr_slow": -30.0}},
        {"ticker": "CCCC", "price": None, "pct_change": None,
         "mention_window": 0, "mention_count": 0},
        # tracked but no CM RSI yet -> the "pending" (…) path. An empty
        # signal_proximity dict is falsy and reads as "untracked" (—).
        {"ticker": "DDDD", "price": 0.87, "pct_change": 0.0,
         "mention_window": 2, "mention_count": 2,
         "signal_proximity": {"status": "watching"}},
    ]
    f = _feed({"AAAA": 300.0, "BBBB": 600.0, "CCCC": 600.0}, rows)
    t = momentum_table(f, T0, 0.5, True, 35.0, -100.0, -75.0, {"AAAA": 4})
    cols = _cells(t)

    assert cols[0] == ["1", "2", "3", "4"]                        # hotkey
    assert cols[1] == [f"[bold cyan]{s}[/bold cyan]"
                       for s in ("AAAA", "BBBB", "CCCC", "DDDD")]
    assert cols[3] == ["3.41", "12.01", "—", "0.87"]              # price
    assert cols[4] == ["[green]+12.4[/green]", "[red]-3.2[/red]",
                       "—", "[white]+0.0[/white]"]                # chg
    assert cols[5] == ["9/31", "0/4", "—", "2/2"]                 # mentions
    # Setup: FOCUS via engine flag, partial readout, untracked, pending
    assert cols[6][0] == ("[bold black on green] FOCUS [/] "
                          "[bold green]22·-91/-88[/bold green]")
    assert cols[6][1] == "[dim]44·-40/-30[/dim]"
    assert cols[6][2] == "[dim]—[/dim]"
    assert cols[6][3] == "[dim]…[/dim]"
    # Flags: FIRST + BURST + confluence + ST rank on AAAA only
    assert cols[7][0] == ("[bold black on green]🥇FIRST[/] "
                          "[bold black on yellow]🔥BURST[/] "
                          "[magenta]⚡2[/magenta] "
                          "[bold black on magenta] ST#4 [/]")
    assert cols[7][1] == ""


def test_added_column_uses_first_seen():
    """'—' when the symbol has no first_seen stamp; TZ-independent check."""
    f = _feed({"AAAA": 0.0}, [{"ticker": "AAAA"}, {"ticker": "ZZZZ"}])
    cols = _cells(momentum_table(f, T0, 0.5, True))
    assert cols[2][1] == "[dim]—[/dim]"
    assert cols[2][0] != "[dim]—[/dim]"


def test_hotkeys_off_blanks_the_key_column():
    f = _feed({"AAAA": 10.0}, [{"ticker": "AAAA"}, {"ticker": "BBBB"}])
    cols = _cells(momentum_table(f, T0, 0.5, False))
    assert cols[0] == ["", ""]


def test_hotkey_numbers_stop_at_nine():
    rows = [{"ticker": f"S{i:03d}"} for i in range(11)]
    f = _feed({}, rows)
    cols = _cells(momentum_table(f, T0, 0.5, True))
    assert cols[0] == ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", ""]


def test_new_badge_respects_new_ttl():
    rows = [{"ticker": "FRESH"}, {"ticker": "STALE"}]
    f = _feed({"FRESH": 10.0, "STALE": 300.0}, rows, new_ttl=120.0)
    cols = _cells(momentum_table(f, T0, 0.5, True))
    assert "NEW" in cols[7][0]
    assert "NEW" not in cols[7][1]


def test_empty_feed_fallback_row_fills_every_column():
    f = _feed({}, [])
    t = momentum_table(f, T0, 0.5, True)
    cols = _cells(t)
    assert len(cols) == len(MOMENTUM_COLUMNS)
    assert cols[1] == ["[dim]no momentum tickers in the feed[/dim]"]
    for c in cols[2:]:
        assert c == [""]


def test_a_raising_cell_builder_degrades_to_a_dash():
    """Ground rule 4: a broken cell loses one cell, not the desk."""
    def _boom(row, ctx):
        raise ValueError("simulated cell failure")

    spec = list(MOMENTUM_COLUMNS)
    spec[3] = ("Price", {"justify": "right"}, _boom)
    f = _feed({"AAAA": 10.0}, [{"ticker": "AAAA", "price": 1.0}])
    cols = _cells(momentum_table(f, T0, 0.5, True, columns=spec))
    assert cols[3] == ["[dim]—[/dim]"]
    assert cols[1] == ["[bold cyan]AAAA[/bold cyan]"]   # neighbours intact


def test_appending_a_column_does_not_disturb_existing_ones():
    """The whole point of the seam: T2.2/T3.1 can append safely."""
    spec = list(MOMENTUM_COLUMNS) + [
        ("RVOL", {"justify": "right"}, lambda row, ctx: "8.2x"),
    ]
    rows = [{"ticker": "AAAA", "price": 3.41, "pct_change": 12.4}]
    f = _feed({"AAAA": 10.0}, rows)
    base = _cells(momentum_table(f, T0, 0.5, True))
    ext = _cells(momentum_table(f, T0, 0.5, True, columns=spec))
    assert ext[:len(base)] == base
    assert ext[-1] == ["8.2x"]
