"""
test_company_names.py — regression tests for word-boundary company-name mapping.

Locks in two fixes:
  1. Recall: common company names (goldman, boeing, costco, …) map to tickers.
  2. Precision: matching is word-boundary, not substring, so "intelligence"
     no longer fires INTC and "price target" no longer fires TGT.

ASR-free (imports ticker_extract), runs in milliseconds.
    venv/bin/python -m pytest transcription/test_company_names.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ticker_extract import extract_tickers   # noqa: E402


def _tk(text):
    return set(extract_tickers(text).keys())


# ── Recall: names that production used to miss ────────────────────────────────

def test_common_names_now_resolve():
    assert "GS" in _tk("goldman is reporting earnings")
    assert "BA" in _tk("boeing got a new order")
    assert "COST" in _tk("costco membership numbers look good")
    assert {"CVX", "XOM"} <= _tk("chevron and exxon both up")
    assert {"LYFT", "UBER"} <= _tk("lyft and uber rideshare war")
    assert "META" in _tk("i'm long meta here")


def test_phrase_beats_partial():
    # "goldman sachs" and "jp morgan" resolve as phrases
    assert "GS" in _tk("goldman sachs upgraded the stock")
    assert "JPM" in _tk("jp morgan raised the target")


# ── Precision: word boundaries, not substrings ────────────────────────────────

def test_substring_collisions_do_not_fire():
    assert "INTC" not in _tk("artificial intelligence is the theme")
    assert "F"    not in _tk("I studied at Stanford")
    assert "TGT"  not in _tk("the price target is fifty dollars")  # no "target" mapping
    assert _tk("comfortable margins and afford it") == set()
    assert "NKE"  not in _tk("a unique opportunity")  # 'nike' not a substring here anyway


def test_clean_sentence_is_empty():
    assert _tk("the market looked strong into the close today") == set()


# ── Still works: direct tickers + phonetics unaffected ────────────────────────

def test_direct_and_phonetic_intact():
    assert _tk("NVDA and AMD ripping") == {"NVDA", "AMD"}
    assert "CRBP" in _tk("charlie romeo bravo papa")
