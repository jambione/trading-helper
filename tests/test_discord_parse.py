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


# ── parse_scanner_cards: new mobile-style Scanner Alert! cards ───────────────

def test_scanner_card_wok_price_and_float_split_lines():
    lines = [
        "8:00 AM APP",
        "Scanner Alert! [ELITE]",
        "$WOK Price Spike!",
        "Price",
        "$251",
        "Float Size",
        "2.42M",
    ]
    cards, used = ds.parse_scanner_cards(lines)
    assert len(cards) == 1
    c = cards[0]
    assert c["ticker"] == "WOK"
    assert c["kind"] == "scanner_card"
    assert c["alert_type"] == "Price Spike"
    assert c["price"] == 251.0
    assert c["float_size"] == pytest.approx(2.42e6)
    assert c["scanner_tier"] == "ELITE"
    assert "WOK" in c["line"] and "251" in c["line"]


def test_scanner_card_lucy_float_only():
    """Some cards omit Price and only show Float Size."""
    lines = [
        "8:00 AM APP",
        "Scanner Alert! [ELITE]",
        "$LUCY Price Spike!",
        "Float Size",
        "5.14M",
    ]
    cards, _ = ds.parse_scanner_cards(lines)
    assert len(cards) == 1
    c = cards[0]
    assert c["ticker"] == "LUCY"
    assert c["alert_type"] == "Price Spike"
    assert c["price"] is None
    assert c["float_size"] == pytest.approx(5.14e6)
    assert c["scanner_tier"] == "ELITE"


def test_scanner_card_inline_fields_single_line():
    line = "$WOK Price Spike! Price $251 Float Size 2.42M"
    cards, _ = ds.parse_scanner_cards(["Scanner Alert! [ELITE]", line])
    assert len(cards) == 1
    c = cards[0]
    assert c["ticker"] == "WOK"
    assert c["price"] == 251.0
    assert c["float_size"] == pytest.approx(2.42e6)


def test_scanner_card_headline_without_dollar_sign():
    lines = [
        "Scanner Alert! [ELITE]",
        "LUCY Price Spike!",
        "Float Size 5.14M",
    ]
    cards, _ = ds.parse_scanner_cards(lines)
    assert len(cards) == 1
    assert cards[0]["ticker"] == "LUCY"


def test_scanner_card_signature_ignores_display_line_jitter():
    card_a = {
        "ticker": "WOK", "alert_type": "Price Spike", "scanner_tier": "ELITE",
        "price": 251.0, "float_size": 2_420_000,
        "line": "[ELITE] $WOK Price Spike! | Price $251 | Float 2.42M",
    }
    card_b = dict(card_a)
    card_b["line"] = "[ELITE] $WOK Price Spike! | Float 2.42M"
    card_b["price"] = None
    assert ds._scanner_card_signature(card_a) == ds._scanner_card_signature(card_b)


def test_scanner_card_does_not_duplicate_with_classic_arrow_alert():
    lines = [
        "INHD Price Volatility Spike! >>>>> 1 Minute High Price = 41.83",
        "Scanner Alert! [ELITE]",
        "$WOK Price Spike!",
        "Float Size",
        "2.42M",
    ]
    cards, card_used = ds.parse_scanner_cards(lines)
    assert len(cards) == 1
    assert cards[0]["ticker"] == "WOK"
    tkr, kind, _ = ds.parse_alert_line(lines[0])
    assert (tkr, kind) == ("INHD", "alert")
    assert 0 not in card_used


# ── "Find It First" cards: bare ticker + Float-before-Price columns ───────────

def test_find_it_first_bare_ticker_float_then_price_header_row():
    """The exact $AGEN card: 'Find It First Alert!' header, a bare-ticker
    headline, and a two-column table whose header row is 'Float Size Price' with
    the values 'Float Price' on the next row (Float column before Price)."""
    lines = [
        "6:08 AM APP",
        "Find It First Alert! [ELITE]",
        "$AGEN",
        "Float Size Price",
        "41.12M $4.05",
    ]
    cards, _ = ds.parse_scanner_cards(lines)
    assert len(cards) == 1
    c = cards[0]
    assert c["ticker"] == "AGEN"
    assert c["kind"] == "scanner_card"
    assert c["price"] == 4.05
    assert c["float_size"] == pytest.approx(41.12e6)
    assert c["scanner_tier"] == "ELITE"


def test_find_it_first_bare_ticker_all_cells_split_lines():
    """Same card, but Vision splits every table cell onto its own line."""
    lines = [
        "6:08 AM",
        "APP",
        "Find It First Alert! [ELITE]",
        "$AGEN",
        "Float Size",
        "Price",
        "41.12M",
        "$4.05",
    ]
    cards, _ = ds.parse_scanner_cards(lines)
    assert len(cards) == 1
    c = cards[0]
    assert c["ticker"] == "AGEN"
    assert c["price"] == 4.05
    assert c["float_size"] == pytest.approx(41.12e6)
    assert c["scanner_tier"] == "ELITE"


def test_find_it_first_header_without_dollar_on_ticker():
    lines = [
        "Find It First Alert! [ELITE]",
        "AGEN",
        "Float Size",
        "41.12M",
        "Price",
        "$4.05",
    ]
    cards, _ = ds.parse_scanner_cards(lines)
    assert len(cards) == 1
    assert cards[0]["ticker"] == "AGEN"
    assert cards[0]["price"] == 4.05
    assert cards[0]["float_size"] == pytest.approx(41.12e6)


def test_generic_alert_header_requires_tier_bracket():
    """A generic '<words> Alert!' header is only honoured with a [TIER] badge."""
    with_badge = ds._SCANNER_ALERT_HEADER_RE.search("Momentum Alert! [PRO]")
    assert with_badge and with_badge.group("tier") == "PRO"
    assert ds._SCANNER_ALERT_HEADER_RE.search("sold my last alert lol") is None


def test_bare_ticker_without_header_is_not_a_card():
    """A lone ticker line with no alert header and no alert type must not fire."""
    cards, _ = ds.parse_scanner_cards(["$AGEN", "Float Size", "41.12M"])
    assert cards == []


import pytest
