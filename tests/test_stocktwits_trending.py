"""Stocktwits trending parser / filter / formatters for the momentum monitor."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from stocktwits_trending import (  # noqa: E402
    parse_trending_payload,
    StocktwitsTrending,
    fmt_vol,
)


SAMPLE = {
    "symbols": [
        {
            "rank": 1,
            "symbol": "NOW",
            "title": "ServiceNow",
            "trending_score": 19.8,
            "watchlist_count": 1000,
            "instrument_class": "Stock",
            "fundamentals": {
                "MarketCapitalization": 105260.0,
                "HighPriceLast52Weeks": 210.20,
                "LowPriceLast52Weeks": 81.24,
                "AverageDailyVolumeLastMonth": 16.79e6,
            },
        },
        {
            "rank": 2,
            "symbol": "ONDS",
            "title": "Ondas",
            "trending_score": 15.0,
            "watchlist_count": 500,
            "instrument_class": "Stock",
            "fundamentals": {
                "MarketCapitalization": 4360.0,
                "HighPriceLast52Weeks": 15.28,
                "LowPriceLast52Weeks": 1.78,
            },
        },
        {
            "rank": 3,
            "symbol": "HBAR.X",
            "title": "Hedera",
            "trending_score": 8.0,
            "watchlist_count": 200,
            "instrument_class": "Cryptocurrency",
        },
    ],
    "response": {"status": 200},
}


def test_parse_fundamentals_columns():
    rows = parse_trending_payload(SAMPLE)
    now = next(r for r in rows if r["symbol"] == "NOW")
    assert now["rank"] == 1
    assert now["high_52w"] == 210.20
    assert now["low_52w"] == 81.24
    # Consolidated — named so it cannot be mistaken for an IEX-comparable
    # figure and divided into the IEX session volume.
    assert now["avg_vol_consolidated"] == 16.79e6
    assert "market_cap" not in now


def test_stocks_only_and_max_price():
    st = StocktwitsTrending(poll_interval=60, stocks_only=True, max_price=30.0,
                            enrich_quotes=False)
    st.rows = [r for r in parse_trending_payload(SAMPLE)
               if r["is_equity"] and not r["is_crypto"]]
    st.by_symbol = {r["symbol"]: r for r in st.rows}
    # inject prices as Alpaca would
    for r in st.rows:
        if r["symbol"] == "NOW":
            r["price"] = 96.0
        if r["symbol"] == "ONDS":
            r["price"] = 8.4
    shown = st.display_rows(limit=10)
    syms = [r["symbol"] for r in shown]
    assert "ONDS" in syms
    assert "NOW" not in syms


def test_fmt_helpers():
    assert "M" in fmt_vol(57.57e6)


def test_rank_of():
    st = StocktwitsTrending(enrich_quotes=False)
    st.by_symbol = {"ONDS": {"rank": 2, "symbol": "ONDS"}}
    assert st.rank_of("onds") == 2
    assert st.rank_of("ZZZZ") is None


def test_nok_depository_receipt_excluded():
    """NOK is depositoryreceipt — stocks_only drops it (stocks only)."""
    payload = {
        "symbols": [{
            "rank": 2,
            "symbol": "NOK",
            "title": "Nokia Corp",
            "trending_score": 10.0,
            "watchlist_count": 1,
            "instrument_class": "depositoryreceipt",
            "fundamentals": {},
        }],
    }
    rows = parse_trending_payload(payload)
    assert rows[0]["is_equity"] is False
    st = StocktwitsTrending(stocks_only=True, enrich_quotes=False, max_price=30.0)
    st.rows = [r for r in rows if r["is_equity"] and not r["is_crypto"]]
    assert st.rows == []


def test_look_extension_near_highs():
    from stocktwits_trending import apply_look_highlights
    rows = [
        {
            "symbol": "AAA", "trending_score": 20.0, "pct_change": 8.0,
            "vol_session": 2e6, "price": 9.0, "high_52w": 10.0, "low_52w": 1.0,
        },
        {
            "symbol": "BBB", "trending_score": 5.0, "pct_change": 0.5,
            "vol_session": 1e5, "price": 5.0, "high_52w": 10.0, "low_52w": 1.0,
        },
    ]
    apply_look_highlights(rows, min_abs_chg=3.0, max_looks=2)
    by = {r["symbol"]: r for r in rows}
    assert by["AAA"]["look"] is True
    assert by["AAA"]["look_reason"] == "EXT"
    assert by["BBB"]["look"] is False


def test_look_washout_near_lows():
    from stocktwits_trending import apply_look_highlights
    rows = [
        {
            "symbol": "CCC", "trending_score": 15.0, "pct_change": -6.0,
            "vol_session": 3e6, "price": 2.0, "high_52w": 20.0, "low_52w": 1.0,
        },
    ]
    apply_look_highlights(rows, min_abs_chg=3.0, max_looks=2)
    assert rows[0]["look"] is True
    assert rows[0]["look_reason"] == "WASH"


def test_look_max_caps_highlights():
    from stocktwits_trending import apply_look_highlights
    rows = [
        {"symbol": f"S{i}", "trending_score": 20 - i, "pct_change": 10.0,
         "vol_session": 5e6, "price": 9.5, "high_52w": 10.0, "low_52w": 1.0}
        for i in range(5)
    ]
    apply_look_highlights(rows, min_abs_chg=3.0, max_looks=2)
    assert sum(1 for r in rows if r["look"]) == 2
