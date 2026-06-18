#!/usr/bin/env python3
"""
Tests for ticker extraction — company-name expansion, stopword filtering,
and plain ALL-CAPS symbol recognition.

Run with:
  venv/bin/python -m pytest transcription/test_ticker_recognition.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from ticker_extract import extract_tickers


def test_plain_symbol():
    assert "AAPL" in extract_tickers("I will buy AAPL today")


def test_multiple_symbols():
    result = extract_tickers("TSLA and NVDA breakout")
    assert "TSLA" in result and "NVDA" in result


def test_company_name_expansion():
    assert extract_tickers("I want to buy apple stock") == {"AAPL": 1}


def test_multiple_company_names():
    result = extract_tickers("Facebook and Netflix")
    assert "META" in result and "NFLX" in result


def test_stopwords_filtered():
    result = extract_tickers("This IS NOT a ticker")
    assert result == {}


def test_dedup_counts_correctly():
    result = extract_tickers("ELPW repeated ELPW")
    assert result.get("ELPW", 0) == 2


def test_sell_line():
    result = extract_tickers("Sell ELPW at 50")
    assert "ELPW" in result


def test_mixed_case_company_and_symbol():
    result = extract_tickers("buy AAPL sell TSLA")
    assert "AAPL" in result and "TSLA" in result


# ── Double-count regression tests ─────────────────────────────────────────────
# Tickers whose lowercase matches a _COMPANY_NAMES key (META, NIO, UBER, etc.)
# were previously counted twice: once by the ticker regex and once via the
# company-name expansion that appended a duplicate to the text.

def test_meta_ticker_not_double_counted():
    assert extract_tickers("META earnings")["META"] == 1


def test_nio_ticker_not_double_counted():
    assert extract_tickers("NIO moving higher")["NIO"] == 1


def test_uber_ticker_not_double_counted():
    assert extract_tickers("UBER up today")["UBER"] == 1


def test_company_name_and_ticker_both_in_text():
    # "facebook" expands to META; "META" already there → should count 2, not 3.
    result = extract_tickers("facebook and META reported earnings")
    assert result.get("META", 0) == 2


def test_company_name_only_counts_once():
    # "facebook" alone → META exactly 1.
    assert extract_tickers("facebook results") == {"META": 1}


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
