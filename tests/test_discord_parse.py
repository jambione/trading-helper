"""
test_discord_parse.py — offline tests for the Discord OCR alert parser.

Covers parse_alert_line (ticker + kind + metadata extraction, rejection of
non-alert / sidebar noise) and _signature (OCR-jitter-stable de-dupe key).
No screen capture, no network — pure string logic over realistic OCR output.

Run:
    venv/bin/python -m pytest tests/test_discord_parse.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord_source as ds   # noqa: E402


# ── parse_alert_line: ticker + kind ──────────────────────────────────────────

def test_volatility_spike():
    line = "INHD Price Volatility Spike! >>>>> 1 Minute High Price = 41.83, 1 Minute Total"
    tkr, kind, _ = ds.parse_alert_line(line)
    assert (tkr, kind) == ("INHD", "alert")


def test_spike_no_space_before_arrows():
    line = "STAK Price Volatility Spike!>>>>> 1 Minute High Price = 6.29, 1 Minute Total"
    tkr, kind, _ = ds.parse_alert_line(line)
    assert (tkr, kind) == ("STAK", "alert")


def test_weekly_low():
    line = "SPY NEW WEEKLY LOW >>>>> Price: $739.20 | Bar Low: $738.19"
    tkr, kind, _ = ds.parse_alert_line(line)
    assert (tkr, kind) == ("SPY", "alert")


def test_new_daily_high():
    line = "DXF New Daily High >>>>> Current Price = 0.6379"
    tkr, kind, _ = ds.parse_alert_line(line)
    assert (tkr, kind) == ("DXF", "alert")


def test_leading_emoji_or_symbol_is_skipped():
    line = "\U0001F4C9 SPY NEW WEEKLY LOW >>>>> Price: $739.20"
    tkr, kind, _ = ds.parse_alert_line(line)
    assert (tkr, kind) == ("SPY", "alert")


def test_squeeze_alert_is_burst():
    tkr, kind, _ = ds.parse_alert_line("ATHE ww close over 6.78/7/7.50")
    assert (tkr, kind) == ("ATHE", "squeeze")


def test_squeeze_alert_simple_close_over():
    tkr, kind, _ = ds.parse_alert_line("DXF close over 1.20/1.35")
    assert (tkr, kind) == ("DXF", "squeeze")


# ── parse_alert_line: metadata — regular alerts ───────────────────────────────

def test_volatility_spike_price_extracted():
    line = "INHD Price Volatility Spike! >>>>> 1 Minute High Price = 41.83, 1 Minute Total Volume = 39001"
    _, _, meta = ds.parse_alert_line(line)
    assert meta["price"]  == 41.83
    assert meta["volume"] == 39001


def test_volatility_spike_alert_type():
    line = "STAK Price Volatility Spike! >>>>> 1 Minute High Price = 6.29"
    _, _, meta = ds.parse_alert_line(line)
    assert "Volatility Spike" in meta["alert_type"]


def test_weekly_low_price_extracted():
    line = "SPY NEW WEEKLY LOW >>>>> Price: $739.20 | Bar Low: $738.19"
    _, _, meta = ds.parse_alert_line(line)
    assert meta["price"] == 739.20


def test_daily_high_price_extracted():
    line = "DXF New Daily High >>>>> Current Price = 0.6379"
    _, _, meta = ds.parse_alert_line(line)
    assert meta["price"] == pytest.approx(0.6379, rel=1e-4)


def test_regular_alert_no_volume_gives_none():
    line = "DXF New Daily High >>>>> Current Price = 0.6379"
    _, _, meta = ds.parse_alert_line(line)
    assert meta["volume"] is None


# ── parse_alert_line: metadata — squeeze alerts ───────────────────────────────

def test_squeeze_levels_three():
    _, _, meta = ds.parse_alert_line("ATHE ww close over 6.78/7/7.50")
    assert meta["levels"] == [6.78, 7.0, 7.50]


def test_squeeze_levels_two():
    _, _, meta = ds.parse_alert_line("DXF close over 1.20/1.35")
    assert meta["levels"] == [1.20, 1.35]


# ── parse_alert_line: rejection cases ────────────────────────────────────────

def test_line_without_any_marker_is_rejected():
    assert ds.parse_alert_line("Volume = 39001")[:2] == (None, None)


def test_sidebar_channel_name_rejected():
    assert ds.parse_alert_line("daytrading-chat")[:2]          == (None, None)
    assert ds.parse_alert_line("Bullish Bob's Trading Hub")[:2] == (None, None)


def test_arrow_line_with_non_ticker_first_word_rejected():
    assert ds.parse_alert_line("Some random note >>>>> blah")[:2] == (None, None)


def test_first_word_not_ticker_does_not_fall_through_to_later_ticker():
    assert ds.parse_alert_line("HELLO there SPY >>>>> noise")[:2] == (None, None)


# ── _signature: OCR-jitter-stable de-dupe ────────────────────────────────────

def test_signature_ignores_spacing_and_punctuation_jitter():
    a = "STAK Price Volatility Spike! >>>>> 1 Minute High Price = 6.29"
    b = "STAK Price Volatility Spike!>>>>> 1 Minute High Price = 6.29"
    assert ds._signature(a) == ds._signature(b)


def test_signature_distinguishes_successive_alerts_by_price():
    a = "MTEN Price Volatility Spike! >>>>> 1 Minute High Price = 1.92"
    b = "MTEN Price Volatility Spike! >>>>> 1 Minute High Price = 2.02"
    assert ds._signature(a) != ds._signature(b)


import pytest
