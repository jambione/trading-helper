"""Operator 2026-10-15: ETFs, ETNs and funds never take a day-book seat."""
import ticker_filters as tf


def test_funds_are_not_stocks_by_issuer_name():
    for sym, name in [("XLP", "State Street Consumer Staples Select Sector SPDR ETF"),
                      ("TLT", "iShares 20 Year Treasury Bond ETF"), ("SLV", "iShares Silver Trust"),
                      ("SCHD", "Schwab US Dividend Equity ETF"), ("GLD", "SPDR Gold Trust"),
                      ("PDI", "PIMCO Dynamic Income Fund")]:
        assert tf.is_not_stock(sym, name), sym


def test_stocks_stay_including_trust_banks_and_lookalikes():
    for sym, name in [("WMT", "Walmart Inc. Common Stock"), ("NTRS", "Northern Trust Corporation"),
                      ("FG", "Fundamental Global Inc."), ("BULL", "Webull Corporation"), ("HD", "Home Depot, Inc.")]:
        assert not tf.is_not_stock(sym, name), sym


def test_unknown_name_never_blocks_and_levered_still_blocks(monkeypatch):
    monkeypatch.setattr(tf, "cached_asset_name", lambda s: "")
    assert not tf.is_fund("ZZZZ")
    assert tf.is_not_stock("TSLL")


def test_seat_paths_use_the_combined_check():
    import ai_entry_watch as ew
    import movers_screener as ms
    assert ew.is_levered_etp is tf.is_not_stock and ms.is_levered_etp is tf.is_not_stock
