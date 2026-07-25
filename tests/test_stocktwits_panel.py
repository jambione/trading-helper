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

def test_the_52w_bounds_survive_having_no_quote_feed():
    """Regression: the bounds come from Stocktwits and need no Alpaca keys, but
    the track needs a current price to place its marker. Collapsing Hi/Lo into
    one column must not throw away data that was visible before it."""
    st = _st([_row(price=None, low_52w=19.51, high_52w=52.24)])
    cell = column_cells(_table(st), "52w lo→hi")[0]
    assert "19.51" in cell and "52.24" in cell


def test_no_bounds_and_no_price_is_still_a_dash():
    st = _st([_row(price=None, low_52w=None, high_52w=None)])
    assert "—" in column_cells(_table(st), "52w lo→hi")[0]


def test_a_known_price_still_draws_the_track_not_the_bounds():
    cell = column_cells(_table(_st([_row(price=5.0)])), "52w lo→hi")[0]
    assert "●" in cell


def test_missing_credentials_are_named_in_the_title():
    """Five blank columns with nothing explaining them is how a morning gets
    wasted. Without keys the panel is Stocktwits-only and says so."""
    st = _st([_row()])
    st.quotes_error = "no Alpaca keys (signal_engine.env)"
    assert "no Alpaca keys" in stocktwits_panel(st, {}, cfg=DEFAULTS).title


def test_the_credential_warning_outranks_the_staleness_warning():
    """A stale-quote age is meaningless when quotes never ran at all."""
    st = _st([_row()])
    st.quotes_error = "no Alpaca keys (signal_engine.env)"
    st.last_quote_ok = T0 - 9000
    title = stocktwits_panel(st, {}, cfg=DEFAULTS).title
    assert "no Alpaca keys" in title
    assert "quotes" not in title


def test_a_quote_attempt_without_keys_reports_rather_than_pretending():
    st = StocktwitsTrending(enrich_quotes=True)
    st.rows = [_row()]
    import stocktwits_trending as stt
    orig = stt._alpaca_client
    stt._alpaca_client = lambda: None
    try:
        assert st.refresh_quotes(T0) is False
    finally:
        stt._alpaca_client = orig
    assert "Alpaca keys" in st.quotes_error
    assert st.last_quote_ok == 0.0      # never claim a successful quote


def test_the_error_clears_once_keys_appear():
    st = StocktwitsTrending(enrich_quotes=True)
    st.rows = [_row()]
    st.quotes_error = "no Alpaca keys (signal_engine.env)"
    import stocktwits_trending as stt
    orig_c, orig_e = stt._alpaca_client, stt.enrich_with_alpaca
    stt._alpaca_client = lambda: object()
    stt.enrich_with_alpaca = lambda rows, **kw: rows
    try:
        assert st.refresh_quotes(T0) is True
    finally:
        stt._alpaca_client, stt.enrich_with_alpaca = orig_c, orig_e
    assert st.quotes_error == ""


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


# ── volume + RVOL ride their own, slower clock ──────────────────────────────

def test_volume_refreshes_less_often_than_quotes():
    """Today's minute bars for the whole panel are a far bigger payload than a
    snapshot, and volume does not move meaningfully in 15s."""
    st = StocktwitsTrending(quote_interval=15.0, volume_interval=60.0,
                            enrich_quotes=True)
    st.rows = [_row()]
    import stocktwits_trending as stt
    orig = stt._alpaca_client
    stt._alpaca_client = lambda: None          # stops before any fetch
    try:
        assert st.refresh_volume(T0) is False  # no client
        assert st.last_volume_attempt == T0
        assert st.refresh_volume(T0 + 30) is False
        assert st.last_volume_attempt == T0    # throttled, not re-attempted
        st.refresh_volume(T0 + 61)
        assert st.last_volume_attempt == T0 + 61
    finally:
        stt._alpaca_client = orig


def test_volume_and_rvol_come_off_minute_bars():
    """Same measurement as the momentum table's RVOL: minute-sum over an
    average of completed sessions, both from morning_funnel."""
    import pandas as pd
    import stocktwits_trending as stt

    st = StocktwitsTrending(enrich_quotes=True, rvol_time_adjusted=False)
    st.rows = [_row(vol_session=None, rvol=None)]
    idx = pd.date_range("2026-07-24 09:30", periods=4, freq="1min",
                        tz=stt.ET)
    df = pd.DataFrame({"volume": [500_000.0] * 4}, index=idx)

    import tools.morning_funnel as mf
    orig = (stt._alpaca_client, mf.fetch_minutes_today, mf.avg_session_volumes)
    stt._alpaca_client = lambda: object()
    mf.fetch_minutes_today = lambda c, syms, cfg, now_et: {"AAAA": df}
    mf.avg_session_volumes = lambda c, syms, cfg, now_et: {"AAAA": 1_000_000.0}
    try:
        assert st.refresh_volume(T0) is True
    finally:
        (stt._alpaca_client, mf.fetch_minutes_today,
         mf.avg_session_volumes) = orig

    row = st.by_symbol["AAAA"]
    assert row["vol_session"] == 2_000_000.0
    assert row["rvol"] == 2.0


def test_a_symbol_with_no_bars_yet_gets_none_not_zero():
    """A 0 would divide into a real RVOL and would read as "traded nothing"
    rather than "no bars yet"."""
    import stocktwits_trending as stt
    import tools.morning_funnel as mf

    st = StocktwitsTrending(enrich_quotes=True)
    st.rows = [_row(vol_session=123.0, rvol=9.9)]
    orig = (stt._alpaca_client, mf.fetch_minutes_today, mf.avg_session_volumes)
    stt._alpaca_client = lambda: object()
    mf.fetch_minutes_today = lambda c, syms, cfg, now_et: {}
    mf.avg_session_volumes = lambda c, syms, cfg, now_et: {}
    try:
        st.refresh_volume(T0)
    finally:
        (stt._alpaca_client, mf.fetch_minutes_today,
         mf.avg_session_volumes) = orig

    assert st.by_symbol["AAAA"]["vol_session"] is None
    assert st.by_symbol["AAAA"]["rvol"] is None
