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


# ── Sidebar bleed: values typed by their own markers, not by position ─────────
# The OCR sweep captures Discord's channel sidebar alongside the card, so
# unrelated text lands between a label row and its values. These are verbatim
# captures from the live window — both parsed wrong before the field scan
# started trusting "$" and K/M/B over reading order.

def test_sidebar_text_between_label_row_and_values_keeps_price_and_float():
    """Live $MB frame. Column order is Float-then-Price but the values arrive
    price-first, with a sidebar channel name between them. Position alone put
    $5.33 in the float column and lost the 4.69M float entirely."""
    lines = [
        "Find It First Alert! [ELITE]",
        "APP",
        "• LIVE Trading...",
        "$MB",
        "05",
        "50",
        "P...",
        "Float Size Price",
        "Premium Scanners LIVE",
        "$5.33",
        "4.69M",
    ]
    cards, _ = ds.parse_scanner_cards(lines)
    assert len(cards) == 1
    assert cards[0]["ticker"] == "MB"
    assert cards[0]["price"] == 5.33
    assert cards[0]["float_size"] == pytest.approx(4.69e6)


def test_sidebar_numbers_before_any_label_are_not_mistaken_for_fields():
    """Live $NEPH frame. Only the float is on screen; the stray sidebar digits
    must not be promoted into the price just because a label row follows."""
    lines = [
        "Find It First Alert! [ELITE]",
        "APP",
        "10-quick-notes",
        "$NEPH",
        "-discord-tips",
        "Float Size Price",
        "#general",
        "6.04M",
        "NEW",
    ]
    cards, _ = ds.parse_scanner_cards(lines)
    assert len(cards) == 1
    assert cards[0]["float_size"] == pytest.approx(6.04e6)
    assert cards[0]["price"] is None


def test_arrow_alert_price_readout_is_not_a_column_label():
    """'Price = 41.83' in a classic arrow alert is a readout, not a card column
    — otherwise the arrow line emits a duplicate card of its own."""
    cards, _ = ds.parse_scanner_cards(
        ["INHD Price Volatility Spike! >>>>> 1 Minute High Price = 41.83"])
    assert cards == []


import pytest


# ── Off-universe symbols must bring evidence they are real ───────────────────
# Loosening the ticker universe for OTC names let OCR-invented symbols through:
# $HOM, $BOM and $AANA all reached the live momentum list on 2026-08-07 and none
# of them was ever on screen. HOM is a real symbol whose last IEX print is from
# 2023, so it even quoted — an 8.72 price sitting beside live ones.

def test_unknown_symbol_without_a_float_size_is_rejected():
    """The phantoms' tell: no float size, and a "price" scraped from whatever
    digits were nearby. A real scanner card always carries the float."""
    for sym, price in (("HOM", "$1422"), ("BOM", "$9595"), ("AANA", "$4")):
        cards, _ = ds.parse_scanner_cards(
            ["Find It First Alert! [ELITE]", f"${sym}", "Price", price])
        assert cards == [], f"{sym} should not have been admitted"


def test_unknown_symbol_with_a_float_size_is_still_admitted(monkeypatch):
    """The OTC names this loosening exists for do carry one.

    The symbol is forced off-universe instead of being picked from real life:
    this test named a genuine OTC ticker until NASDAQ listed it, at which point
    the next 7-day refresh of valid_tickers.txt turned it green-to-red with no
    code change behind it.
    """
    monkeypatch.setattr(ds, "is_valid_ticker", lambda sym: False)
    cards, _ = ds.parse_scanner_cards(
        ["Find It First Alert! [ELITE]", "$BSEM", "Float Size", "11.49M"])
    assert len(cards) == 1
    assert cards[0]["ticker"] == "BSEM"
    assert cards[0]["off_universe"] is True


def test_a_listed_ticker_needs_no_float_size():
    """The universe already vouched for it; requiring a float would drop real
    cards whose float line the OCR happened to miss."""
    cards, _ = ds.parse_scanner_cards(
        ["Find It First Alert! [ELITE]", "$VATE", "Price", "$7.41"])
    assert len(cards) == 1
    assert cards[0]["ticker"] == "VATE"
    assert cards[0]["off_universe"] is False


def test_post_ingest_retries_after_401(monkeypatch):
    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=5):
        calls.append(dict(req.headers))
        if len(calls) == 1:
            raise ds.urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", hdrs=None, fp=None)
        return _Resp()

    monkeypatch.setattr(ds, "_AUTH_TOKEN", "stale")
    monkeypatch.setattr(ds, "DASHBOARD_USER", "jmb")
    monkeypatch.setattr(ds, "DASHBOARD_PASS", "x")
    monkeypatch.setattr(ds, "_dashboard_login", lambda: "fresh")
    monkeypatch.setattr(ds.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ds, "drop_counts", lambda: {})
    ds._post_ingest([], [])
    assert len(calls) == 2
    assert "Bearer stale" in str(calls[0].get("Authorization") or calls[0].get("authorization"))
    assert "Bearer fresh" in str(calls[1].get("Authorization") or calls[1].get("authorization"))
