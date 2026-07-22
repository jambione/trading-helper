"""Stocktwits trending parser / filter for the momentum monitor."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from stocktwits_trending import (  # noqa: E402
    parse_trending_payload,
    StocktwitsTrending,
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
            "fundamentals": {"MarketCapitalization": 1e11},
        },
        {
            "rank": 2,
            "symbol": "ONDS",
            "title": "Ondas",
            "trending_score": 15.0,
            "watchlist_count": 500,
            "instrument_class": "Stock",
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


def test_parse_ranks_and_crypto_flag():
    rows = parse_trending_payload(SAMPLE)
    assert len(rows) == 3
    assert rows[0]["symbol"] == "NOW" and rows[0]["rank"] == 1
    assert rows[2]["is_crypto"] is True


def test_stocks_only_and_max_price():
    st = StocktwitsTrending(poll_interval=60, stocks_only=True, max_price=30.0)
    st.rows = [r for r in parse_trending_payload(SAMPLE)
               if r["is_equity"] and not r["is_crypto"]]
    st.by_symbol = {r["symbol"]: r for r in st.rows}
    # NOW expensive, ONDS cheap
    shown = st.display_rows({"NOW": 96.0, "ONDS": 8.4}, limit=10)
    syms = [r["symbol"] for r in shown]
    assert "ONDS" in syms
    assert "NOW" not in syms
    assert "HBAR.X" not in syms


def test_unknown_price_kept():
    st = StocktwitsTrending(max_price=30.0, stocks_only=True)
    st.rows = [r for r in parse_trending_payload(SAMPLE)
               if r["is_equity"] and not r["is_crypto"]]
    shown = st.display_rows({}, limit=10)
    # no prices → still show equities (social heat)
    assert {r["symbol"] for r in shown} == {"NOW", "ONDS"}


def test_rank_of():
    st = StocktwitsTrending()
    st.by_symbol = {"ONDS": {"rank": 2, "symbol": "ONDS"}}
    assert st.rank_of("onds") == 2
    assert st.rank_of("ZZZZ") is None
