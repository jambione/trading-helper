"""Trending panel rendering, and the quote clock that feeds it.

Covers what the desk actually reads on screen: that the volume column names its
feed instead of implying the consolidated tape, that a stale quote is visibly
stale, that the 52w pair became one track, and that Mkt Cap is gone.
"""
import os
import sys

from conftest import column_cells  # noqa: E402

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "momentum-monitor"))

from momentum_signal import DEFAULTS, stocktwits_panel  # noqa: E402
from stocktwits_trending import StocktwitsTrending  # noqa: E402

T0 = 1753449600.0


def _st(rows, **kw):
    st = StocktwitsTrending(enrich_quotes=False, max_price=None, **kw)
    st.rows = rows
    st.by_symbol = {r["symbol"]: r for r in rows}
    st.last_ok = T0
    st._seeded = True
    return st


def _row(sym="AAAA", **kw):
    row = {"symbol": sym, "rank": 1, "trending_score": 12.0,
           "price": 4.20, "pct_change": 5.0, "vol_session": 1_500_000,
           "high_52w": 10.0, "low_52w": 1.0}
    row.update(kw)
    return row


def _table(st, cfg=None, price_by_sym=None):
    panel = stocktwits_panel(st, price_by_sym or {}, limit=10,
                             hotkeys_on=True, cfg=cfg if cfg is not None
                             else DEFAULTS)
    return panel.renderable


def _headers(table):
    return [c.header for c in table.columns]


# ── the columns the user asked to change ─────────────────────────────────────

def test_market_cap_column_is_gone():
    assert not any("Cap" in h for h in _headers(_table(_st([_row()]))))


def test_the_52w_pair_became_one_track():
    heads = _headers(_table(_st([_row()])))
    assert "52w Hi" not in heads and "52w Lo" not in heads
    assert any(h.startswith("52w") for h in heads)


def test_the_track_places_the_current_price_between_the_bounds():
    near_low = _table(_st([_row(price=1.5)]))
    near_high = _table(_st([_row(price=9.5)]))
    lo_cell = column_cells(near_low, "52w lo→hi")[0]
    hi_cell = column_cells(near_high, "52w lo→hi")[0]
    assert lo_cell.index("●") < hi_cell.index("●")


def test_a_new_52w_high_is_marked_distinctly():
    cells = column_cells(_table(_st([_row(price=12.0)])), "52w lo→hi")
    assert "▶" in cells[0]


# ── volume names its feed ────────────────────────────────────────────────────

def test_the_volume_column_names_the_iex_feed():
    """Labelled plain "Volume" it read as the consolidated figure the
    Stocktwits site shows, which it is a few percent of."""
    assert "Vol·IEX" in _headers(_table(_st([_row()])))


def test_no_session_volume_renders_a_dash_not_a_30_day_average():
    row = _row(vol_session=None, avg_vol_consolidated=40_000_000)
    cells = column_cells(_table(_st([row])), "Vol·IEX")
    assert "—" in cells[0]
    assert "40" not in cells[0] and "M" not in cells[0]


# ── RVOL column ──────────────────────────────────────────────────────────────

def test_rvol_column_renders_the_value():
    cells = column_cells(_table(_st([_row(rvol=6.2)])), "RVOL")
    assert "6.2x" in cells[0]


def test_absent_rvol_is_a_dash_not_a_zero():
    """A blank RVOL must not read as "volume is unremarkable"."""
    cells = column_cells(_table(_st([_row()])), "RVOL")
    assert "—" in cells[0]
    assert "0.0x" not in cells[0]


def test_rvol_column_can_be_switched_off():
    cfg = {**DEFAULTS, "stocktwits_rvol_column": False}
    assert "RVOL" not in _headers(_table(_st([_row()]), cfg=cfg))


# ── price staleness ──────────────────────────────────────────────────────────

def test_a_current_price_renders_plainly():
    cells = column_cells(_table(_st([_row(price_age_sec=2.0)])), "Last")
    assert cells[0] == "$4.20"


def test_a_stale_price_is_dimmed_and_carries_its_age():
    cells = column_cells(_table(_st([_row(price_age_sec=47.0)])), "Last")
    assert "dim" in cells[0] and "$4.20" in cells[0] and "47s" in cells[0]


def test_an_hours_old_premarket_print_reads_in_hours_not_seconds():
    """A thin name's last print can be from yesterday's close. `32400s` does
    not read as a warning."""
    cells = column_cells(_table(_st([_row(price_age_sec=32400.0)])), "Last")
    assert "9h" in cells[0]


def test_a_price_borrowed_from_the_momentum_feed_carries_no_snapshot_age():
    """The age belonged to a print of a different number entirely."""
    st = _st([_row(price=None, price_age_sec=900.0)])
    cells = column_cells(_table(st, price_by_sym={"AAAA": 4.20}), "Last")
    assert cells[0] == "$4.20"


def test_the_age_keeps_advancing_when_a_quote_poll_stops_landing():
    """A stamped-once age freezes the moment enrichment fails to parse, so a
    price now minutes old keeps showing the age it had when it was seconds old.
    Deriving from the print's absolute timestamp is what makes it self-correct.
    """
    st = _st([_row(price_ts=T0 - 5, price_age_sec=5.0)])
    fresh = stocktwits_panel(st, {}, cfg=DEFAULTS, now=T0).renderable
    later = stocktwits_panel(st, {}, cfg=DEFAULTS, now=T0 + 600).renderable
    assert column_cells(fresh, "Last")[0] == "$4.20"
    assert "10m" in column_cells(later, "Last")[0]


def test_re_aging_reaches_the_row_the_journal_reads():
    """The journal records off `by_symbol`, not the rendered copy."""
    row = _row(price_ts=T0 - 5, price_age_sec=5.0)
    st = _st([row])
    st.display_rows(limit=10, now=T0 + 300)
    assert round(st.by_symbol["AAAA"]["price_age_sec"]) == 305


def test_staleness_marking_can_be_switched_off():
    cfg = {**DEFAULTS, "price_age_enabled": False}
    cells = column_cells(_table(_st([_row(price_age_sec=400.0)]), cfg=cfg),
                         "Last")
    assert cells[0] == "$4.20"


# ── the title carries the quote age, separately from the list poll ───────────

def test_the_title_warns_when_quotes_have_gone_stale():
    st = _st([_row()])
    st.last_quote_ok = T0 - 90
    title = stocktwits_panel(st, {}, cfg=DEFAULTS).title
    assert "quotes" in title


def test_the_title_stays_quiet_while_quotes_are_current():
    st = _st([_row()])
    st.last_quote_ok = __import__("time").time()
    assert "quotes" not in stocktwits_panel(st, {}, cfg=DEFAULTS).title


# ── quote clock is independent of the Stocktwits poll ────────────────────────

def test_quotes_refresh_between_list_polls():
    """The ranking barely moves in a minute; the prices hanging off it go stale
    in seconds. Re-quoting must not require re-polling Stocktwits."""
    st = StocktwitsTrending(poll_interval=60.0, quote_interval=15.0,
                            enrich_quotes=True)
    st.rows = [_row()]
    calls = []
    st._quote = lambda now: (calls.append(now), True)[1]

    assert st.refresh_quotes(T0) is True
    assert st.refresh_quotes(T0 + 5) is False      # inside the quote interval
    assert st.refresh_quotes(T0 + 20) is True      # past it, list untouched
    assert calls == [T0, T0 + 20]


def test_the_list_poll_stays_on_its_own_slower_clock():
    st = StocktwitsTrending(poll_interval=60.0, quote_interval=15.0,
                            enrich_quotes=False)
    st.last_attempt = T0
    assert st.refresh(T0 + 30) is False
    assert st.last_attempt == T0


def test_a_successful_list_poll_also_requotes():
    st = StocktwitsTrending(poll_interval=60.0, enrich_quotes=True)
    quoted = []
    st._quote = lambda now: (quoted.append(now), True)[1]
    import stocktwits_trending as stt
    orig = stt.fetch_trending
    stt.fetch_trending = lambda *a, **k: [_row()]
    try:
        assert st.refresh(T0) is True
    finally:
        stt.fetch_trending = orig
    assert quoted == [T0]


def test_quote_age_is_none_before_the_first_quote():
    assert StocktwitsTrending(enrich_quotes=False).quote_age(T0) is None


# ── the average-volume cache survives a churning trending list ──────────────

def test_symbols_with_no_history_are_negatively_cached():
    """The trending list turns over all day. Without the None entry a fresh
    listing would be re-requested on every quote poll and never resolve."""
    st = StocktwitsTrending(enrich_quotes=False)
    fetched = []

    import stocktwits_trending as stt
    orig_client, orig_daily = stt._alpaca_client, stt.fetch_daily_volumes
    stt._alpaca_client = lambda: object()
    stt.fetch_daily_volumes = lambda c, syms: (fetched.append(list(syms)), {})[1]
    try:
        st._avg_volumes(["AAAA"], T0)
        st._avg_volumes(["AAAA"], T0 + 30)
    finally:
        stt._alpaca_client, stt.fetch_daily_volumes = orig_client, orig_daily

    assert fetched == [["AAAA"]]
    assert st._avg_vol == {"AAAA": None}


def test_the_cache_is_dropped_on_a_new_session_date():
    st = StocktwitsTrending(enrich_quotes=False)
    st._avg_vol = {"AAAA": 1_000_000.0}
    st._avg_vol_date = "1999-01-01"

    import stocktwits_trending as stt
    orig = stt._alpaca_client
    stt._alpaca_client = lambda: None
    try:
        st._avg_volumes([], T0)
    finally:
        stt._alpaca_client = orig
    assert st._avg_vol == {}
