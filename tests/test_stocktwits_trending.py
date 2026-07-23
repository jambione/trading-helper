"""Stocktwits trending parser / filter / formatters for the momentum monitor."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from stocktwits_trending import (  # noqa: E402
    parse_trending_payload,
    StocktwitsTrending,
    fmt_mcap,
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
    # millions → dollars
    assert now["market_cap"] == 105260.0 * 1_000_000.0


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
    assert fmt_mcap(6.24e9).startswith("$6.24B")
    assert "M" in fmt_vol(57.57e6)


def test_rank_of():
    st = StocktwitsTrending(enrich_quotes=False)
    st.by_symbol = {"ONDS": {"rank": 2, "symbol": "ONDS"}}
    assert st.rank_of("onds") == 2
    assert st.rank_of("ZZZZ") is None


def test_nok_depository_receipt_is_equity():
    """NOK is instrument_class=depositoryreceipt on ST — must pass stocks_only."""
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
    assert rows[0]["symbol"] == "NOK"
    assert rows[0]["is_equity"] is True
    assert rows[0]["is_crypto"] is False
    st = StocktwitsTrending(stocks_only=True, enrich_quotes=False, max_price=30.0)
    st.rows = [r for r in rows if r["is_equity"] and not r["is_crypto"]]
    st.by_symbol = {r["symbol"]: r for r in st.rows}
    st.rows[0]["price"] = 10.67
    shown = st.display_rows(limit=10)
    assert any(r["symbol"] == "NOK" for r in shown)
