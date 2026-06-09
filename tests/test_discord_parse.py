"""
test_discord_parse.py — offline tests for the Discord OCR alert parser.

Covers parse_alert_line (ticker extraction + rejection of non-alert / sidebar
noise) and _signature (OCR-jitter-stable de-dupe key). No screen capture, no
network — pure string logic over realistic OCR output lines.

Run:
    venv/bin/python -m pytest tests/test_discord_parse.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord_source as ds   # noqa: E402


# ── parse_alert_line: real alert shapes ──────────────────────────────────────

def test_volatility_spike():
    line = "INHD Price Volatility Spike! >>>>> 1 Minute High Price = 41.83, 1 Minute Total"
    assert ds.parse_alert_line(line) == "INHD"


def test_spike_no_space_before_arrows():
    # OCR sometimes glues the headline to the arrows.
    line = "STAK Price Volatility Spike!>>>>> 1 Minute High Price = 6.29, 1 Minute Total"
    assert ds.parse_alert_line(line) == "STAK"


def test_weekly_low():
    line = "SPY NEW WEEKLY LOW >>>>> Price: $739.20 | Bar Low: $738.19"
    assert ds.parse_alert_line(line) == "SPY"


def test_new_daily_high():
    line = "DXF New Daily High >>>>> Current Price = 0.6379"
    assert ds.parse_alert_line(line) == "DXF"


def test_leading_emoji_or_symbol_is_skipped():
    line = "\U0001F4C9 SPY NEW WEEKLY LOW >>>>> Price: $739.20"
    assert ds.parse_alert_line(line) == "SPY"


# ── parse_alert_line: rejection cases ────────────────────────────────────────

def test_line_without_arrow_marker_is_rejected():
    # The wrapped second line of an alert (no ">>>>>") must not parse.
    assert ds.parse_alert_line("Volume = 39001") is None


def test_sidebar_channel_name_rejected():
    assert ds.parse_alert_line("daytrading-chat") is None
    assert ds.parse_alert_line("Bullish Bob's Trading Hub") is None


def test_arrow_line_with_non_ticker_first_word_rejected():
    # Has the marker but the first real word isn't a valid ticker → not an alert,
    # and we must NOT scan deeper and pick up a later word that happens to be one.
    assert ds.parse_alert_line("Some random note >>>>> blah") is None


def test_first_word_not_ticker_does_not_fall_through_to_later_ticker():
    # "HELLO" is not a valid ticker; even though "SPY" appears later, the first
    # real word gates the line, so this is rejected.
    assert ds.parse_alert_line("HELLO there SPY >>>>> noise") is None


# ── _signature: OCR-jitter-stable de-dupe ────────────────────────────────────

def test_signature_ignores_spacing_and_punctuation_jitter():
    a = "STAK Price Volatility Spike! >>>>> 1 Minute High Price = 6.29"
    b = "STAK Price Volatility Spike!>>>>> 1 Minute High Price = 6.29"
    assert ds._signature(a) == ds._signature(b)


def test_signature_distinguishes_successive_alerts_by_price():
    a = "MTEN Price Volatility Spike! >>>>> 1 Minute High Price = 1.92"
    b = "MTEN Price Volatility Spike! >>>>> 1 Minute High Price = 2.02"
    assert ds._signature(a) != ds._signature(b)
