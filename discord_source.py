#!/usr/bin/env python3
"""
discord_source.py — read trading-alert messages off the Discord window via OCR
and feed them into the dashboard as ticker mentions.

ToS-safe by design: this NEVER logs into Discord or touches its servers. It only
reads pixels already on your own screen. A small Swift/Vision helper
(`discord_ocr`) screenshots the Discord app window and prints the recognized
text; this poller parses the alert lines and POSTs each new alert's ticker to
the same dashboard seam the audio transcriber uses:

    POST http://localhost:8888/api/tickers/add   {"ticker": "NVDA", "count": 1}

So everything downstream (mention tracking → burst detection → signal engine →
toasts/charts) is driven identically to transcription — this is just a cleaner,
second producer that runs ALONGSIDE the transcriber.

Alert formats:
  Classic (ticker is the first token, line contains ">>>>>"):
    INHD Price Volatility Spike! >>>>> 1 Minute High Price = 41.83, ...
    SPY  NEW WEEKLY LOW          >>>>> Price: $739.20 | Bar Low: ...
    DXF  New Daily High          >>>>> Current Price = 0.6379

  Scanner Alert! card (new mobile-style layout, often spans multiple OCR lines):
    Scanner Alert! [ELITE]
    $WOK Price Spike!
    Price          Float Size
    $251           2.42M

Setup:
  1. Build the OCR helper (one time):   swiftc discord_ocr.swift -o discord_ocr
  2. Keep the Discord window with the alert channel visible (not minimized).
  3. Enable in config/bot_config.json:   "discord_ocr_enabled": true
  4. Run standalone for testing:         python discord_source.py
     or let start_all.py launch it automatically when enabled.

The first run will request Screen Recording permission (same as the audio
capture) — grant it to your terminal / launcher.

Config keys (config/bot_config.json), all optional with sane defaults:
  discord_ocr_enabled   bool   default false  (gate used by start_all.py)
  discord_ocr_poll_sec  float  default 2.5    (seconds between OCR captures)
  discord_window_owner  str    default "Discord"
  discord_window_title  str    default ""     (substring filter, optional)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, asdict
from pathlib import Path

# Pure-stdlib ticker validation — single source of truth for the NASDAQ/NYSE
# universe (shared with the audio transcriber). Imports in milliseconds.
sys.path.insert(0, str(Path(__file__).parent / "transcription"))
from ticker_extract import is_valid_ticker  # noqa: E402

ROOT          = Path(__file__).parent
# Optional Swift binary fallback (native Python capture is preferred — same process
# that already holds Screen Recording on macOS 15+/26).
_OCR_APP_BIN  = ROOT / "DiscordOCR.app" / "Contents" / "MacOS" / "discord_ocr"
_OCR_FLAT     = ROOT / "discord_ocr"
OCR_BINARY    = _OCR_APP_BIN if _OCR_APP_BIN.exists() else _OCR_FLAT
OCR_SCRIPT    = ROOT / "discord_ocr.swift"

try:
    import discord_ocr_native as _native_ocr
except Exception:  # pragma: no cover
    _native_ocr = None  # type: ignore
CONFIG_FILE   = ROOT / "config" / "bot_config.json"
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:8888")

# A standard alert line carries this arrow marker between the ticker/headline and
# the data payload. Two-or-more ">" tolerates OCR dropping a couple of arrows.
_ALERT_MARKER = re.compile(r">>+")

# A "Squeeze Potential Alert" body reads e.g. "ATHE ww close over 6.78/7/7.50".
# These have no arrow marker; "close over" is the signature. We treat them as a
# strong catalyst → fire the burst toast immediately (see ingest, "burst" flag).
_SQUEEZE_MARKER = re.compile(r"(?i)\bclose\s+over\b")


# ── Sentiment extraction ──────────────────────────────────────────────────────
# Three non-alert content types in the channel become a rolling sentiment signal:
# bot market-direction alerts, human chat, and TradingView chart labels.

@dataclass
class SentimentEvent:
    ticker: str | None
    score:  float
    source: str      # "hv_alert" | "chat" | "tv_chart"
    raw:    str
    ts:     float


_HV_RE = re.compile(
    r'(?P<bull>🟢|HIGH\s+VOLUME\s+BULLISH)|(?P<bear>🔴|HIGH\s+VOLUME\s+BEARISH)',
    re.IGNORECASE,
)
_CHAT_RE = re.compile(
    r'^\[?\d{1,2}:\d{2}\s*[AP]M\]?'
    r'(?P<user>[^:]{2,60})'
    r':\s*(?P<body>.+)$',
    re.IGNORECASE,
)
_TV_TREND_RE    = re.compile(r'TREND\s*:\s*(?P<trend>LONG|SHORT)', re.IGNORECASE)
_TV_STRATEGY_RE = re.compile(r'STRATEGY\s*:\s*(?P<strat>BULLISH|BEARISH)', re.IGNORECASE)

_CHAT_SKIP = frozenset({"HOD", "LOD", "PDT", "ATH", "AM", "PM", "EST", "ETF",
                        "IPO", "HALT", "SEC", "FDA", "CEO", "BULL", "BEAR"})

# Words that can legitimately sit alone on a card line without being a ticker.
# Only consulted for symbols OUTSIDE the NASDAQ/NYSE universe — a real listed
# ticker (LIVE, CART, OPEN, …) is accepted on the universe check before this
# ever runs, so listing them here costs nothing.
_CARD_HEADLINE_SKIP = frozenset({
    "ALERT", "ELITE", "PRICE", "FLOAT", "SIZE", "VOLUME", "SHARES", "APP",
    "NEW", "HIGH", "LOW", "OPEN", "CLOSE", "LIVE", "CHAT", "HERE", "NOW",
    "TIER", "FREE", "VIP", "PRO", "BUY", "SELL", "LONG", "SHORT", "TRADE",
    "IDEAS", "AUDIENCE", "LISTEN",
})

# ── Drop accounting ───────────────────────────────────────────────────────────
# A rejected card used to vanish without a trace, which is exactly why a feed
# that had stopped surfacing names still looked healthy. Every rejection is
# counted, and the first of each (ticker, reason) pair gets a log line.
_drop_counts: dict[str, int] = {}
_off_universe_count = 0
_noted: set[tuple[str, str]] = set()


def _note_once(ticker: str, message: str) -> bool:
    """Log "$TICKER — message" the first time this pair is seen. Returns whether
    it was logged, so a card that stays on screen for hours can't spam the log."""
    key = (ticker, message)
    if key in _noted:
        return False
    _noted.add(key)
    print(f"[discord] ${ticker} — {message}", flush=True)
    return True


def _note_drop(ticker: str, reason: str) -> None:
    """Count a rejected ticker and log it once per ticker per reason.

    Counted on first sight only. Every OCR frame re-parses whatever is on
    screen, so counting each parse would report how long a card stayed visible
    rather than how many symbols were lost — these are distinct-ticker totals.
    """
    if _note_once(ticker, f"dropped: {reason}"):
        _drop_counts[reason] = _drop_counts.get(reason, 0) + 1


def _note_off_universe(ticker: str, where: str) -> None:
    """Count a ticker accepted despite being absent from the NASDAQ/NYSE list.
    Distinct tickers, for the same reason as _note_drop."""
    global _off_universe_count
    if _note_once(ticker, f"accepted off-universe (OTC) from {where}"):
        _off_universe_count += 1


def drop_counts() -> dict[str, int]:
    """Session totals by reason, POSTed with each heartbeat so the dashboard can
    show what the source is throwing away instead of only what it accepted.
    accepted_off_universe is the count the OTC fix rescued."""
    return {**_drop_counts, "accepted_off_universe": _off_universe_count}


def _off_universe_ticker_ok(raw_headline: str, ticker: str, hm: "re.Match") -> bool:
    """Whether to accept a card ticker that is not in the NASDAQ/NYSE universe.

    valid_tickers.txt is built from nasdaqlisted.txt + otherlisted.txt, which
    carry no OTC symbols — so scanner cards for OTC names (the scanner posts
    plenty) failed validation and were dropped. A headline sitting directly
    under a recognised "… Alert! [TIER]" header is strong enough evidence on
    its own; this just insists the symbol still takes the shape of one.
    """
    if ticker in _CARD_HEADLINE_SKIP or ticker in _CHAT_SKIP:
        return False
    # An alert type is fine when it is a phrase the scanner actually uses; any
    # other trailing words are more likely OCR running two lines together.
    alert_type = (hm.group("alert_type") or "").strip()
    if alert_type and not _SCANNER_ALERT_TYPE_RE.search(alert_type):
        return False
    # "$SYM" (how the scanner writes it) or a bare all-caps SYM. Mixed case is
    # ordinary prose that happened to fit the headline pattern.
    stripped = raw_headline.lstrip()
    return stripped.startswith("$") or ticker == hm.group("ticker")

_BULLISH_KW = {
    "breakout": 1.0, "breaking out": 1.0, "ripping": 1.0, "mooning": 1.0,
    "red to green": 0.9, "new high": 0.9, "bounce": 0.7, "bouncing": 0.7,
    "hod": 0.8, "green": 0.6, "buying": 0.6, "long": 0.7, "bullish": 1.0,
    "calls": 0.8, "holding": 0.4, "support": 0.4, "running": 0.7,
    "flat to green": 0.8, "red to flat": 0.5, "loading": 0.6, "strong": 0.5,
}
_BEARISH_KW = {
    "breakdown": -1.0, "dumping": -1.0, "tanking": -1.0, "bearish": -1.0,
    "all out": -0.7, "selling": -0.7, "loss": -0.6, "sold": -0.6,
    "choppy": -0.5, "chop": -0.4, "red": -0.5, "down": -0.4,
    "fading": -0.6, "rejected": -0.7, "failed": -0.6, "stopped": -0.5,
    "short": -0.8, "puts": -0.8, "dump": -1.0, "lod": -0.8, "weak": -0.5,
}
_EMOJI_SCORES = {
    "🚀": 1.0, "📈": 0.9, "🟢": 0.6, "🔥": 0.7, "💃": 0.5, "🙌": 0.5,
    "📉": -0.9, "🔴": -0.6, "💀": -0.7, "😱": -0.5,
}
# Sorted longest-first so multi-word phrases match before their component words.
_KW_SORTED = sorted({**_BULLISH_KW, **_BEARISH_KW}.items(), key=lambda kv: -len(kv[0]))


class _TvState:
    def __init__(self):
        self.pending_trend  = None
        self.pending_ticker = None


# ── Market Update scanner table detection ─────────────────────────────────────
# Discord periodically posts "Market Update" images with scanner tables:
#   Momentum+HOD | Volume Breakout | Price Spike | Gap Up
# Vision OCR reads the table text inline. We detect the sentinel, track which
# column we're in (column headers are readable text in the image), and emit a
# scanner SentimentEvent per ticker. Scores map to column strength:
#   Momentum+HOD 0.80 (making new highs)   → triggers mention boost (≥0.7)
#   Gap Up       0.75                       → triggers mention boost
#   Vol Breakout 0.60 (unusual volume)
#   Price Spike  0.55 (direction ambiguous)

_MARKET_UPDATE_RE = re.compile(r'\bmarket\s+update\b', re.IGNORECASE)

_SCANNER_COL_RE = re.compile(
    r'(?P<momentum>\bmomentum\b)|'
    r'(?P<gap_up>\bgap\s+up\b)|'
    r'(?P<volume>\bvolume\b.*\bbreakout\b|\bbreakout\b.*\bvolume\b)|'
    r'(?P<spike>\bprice\b.*\bspike\b|\bspike\b.*\bprice\b)',
    re.IGNORECASE,
)

_SCANNER_SCORES = {
    "momentum": 0.80,
    "gap_up":   0.75,
    "volume":   0.60,
    "spike":    0.55,
}
_SCANNER_DEFAULT = 0.60   # used only when a strict row appears before any header

# A genuine scanner table row is "TICKER price pct" — a leading ticker
# followed by two separate numeric tokens (a price-like number, then a
# signed change). That two-number shape is what rejects the chat/alert noise
# (e.g. "BB LIVE Alerts", ">>>>> 1 Minute High Price = 41.83", "Set alerts
# below key levels"): none of those carry two adjacent numbers, so none can
# be misread as tickers. The "$", thousands comma, decimal point, and "%"
# are all optional because Vision OCR frequently drops or misreads exactly
# those glyphs on scanner rows — treating them as required turned normal OCR
# jitter into silently-dropped tickers.
_SCANNER_ROW_RE = re.compile(
    r'^\s*([A-Z]{2,5})\s+\$?\d[\d,]*\.?\d*\s*[+\-]?\d[\d.]*\s*%?')

# Lines that close the scanner table: an alert arrow or a "[H:MM AM]user:" chat
# prefix means we've scrolled past the Market Update image into other messages.
_SCANNER_BOUNDARY_RE = re.compile(r'>>+|^\[?\d{1,2}:\d{2}\s*[AP]M\]?', re.IGNORECASE)


class _ScannerState:
    def __init__(self):
        self.active    = False
        self.col_score = None   # set from a matched column header; None = none yet


def _update_scanner_state(line: str, scanner_state: "_ScannerState",
                          new_sentiment: list, seen: dict, t0: float) -> bool:
    """Detect Market Update scanner tables and emit per-ticker scanner sentiment.

    Strict by design: a line is only emitted as a scanner ticker when the table
    is active AND the line matches the "TICKER $price ±pct%" row shape exactly.
    Alert/chat boundary lines close the table. Returns True when the line was
    consumed by the scanner so the caller can skip the other parsers.
    """
    if _MARKET_UPDATE_RE.search(line):
        scanner_state.active    = True
        scanner_state.col_score = None
        return True

    if not scanner_state.active:
        return False

    # Hit a non-scanner message → the table has ended. Close it and let the
    # other parsers (chat) have this line.
    if _SCANNER_BOUNDARY_RE.search(line):
        scanner_state.active    = False
        scanner_state.col_score = None
        return False

    # Column header → remember the strength for the rows that follow.
    cm = _SCANNER_COL_RE.search(line)
    if cm:
        for grp, score in _SCANNER_SCORES.items():
            if cm.group(grp):
                scanner_state.col_score = score
                break
        return True

    rm = _SCANNER_ROW_RE.match(line)
    if not rm:
        return False
    tkr = rm.group(1)
    if not is_valid_ticker(tkr) or tkr in _CHAT_SKIP:
        return False

    sig = _signature(line)
    if sig in seen:
        return True
    seen[sig] = t0

    score = scanner_state.col_score if scanner_state.col_score is not None else _SCANNER_DEFAULT
    new_sentiment.append(
        SentimentEvent(tkr, score, "scanner", line.strip()[:80], t0))
    return True


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}


# ── Alert parsing ───────────────────────────────────────────────────────────

_PRICE_RE  = re.compile(r'(?:High|Current|Bar High|Bar Low|Close|Last)?\s*Price\s*[=:]\s*\$?([\d,]+\.?\d*)', re.I)
_VOLUME_RE = re.compile(r'(?:Total\s+)?Volume\s*[=:]\s*([\d,]+)', re.I)


def _parse_regular_meta(ticker: str, line: str) -> dict:
    """Extract alert_type, price, and volume from a standard >>>>> alert line."""
    parts = re.split(r'>>+', line, maxsplit=1)
    headline     = parts[0].strip()
    data_section = parts[1].strip() if len(parts) > 1 else ""

    # Strip leading ticker (and any stray punctuation/whitespace) from headline
    alert_type = re.sub(rf'(?i)^\W*{re.escape(ticker)}\s*', '', headline).strip()
    alert_type = re.sub(r'[!?.,]+$', '', alert_type).strip().title()

    price = None
    m = _PRICE_RE.search(data_section)
    if m:
        try:
            price = float(m.group(1).replace(',', ''))
        except ValueError:
            pass

    volume = None
    m = _VOLUME_RE.search(data_section)
    if m:
        try:
            volume = int(m.group(1).replace(',', ''))
        except ValueError:
            pass

    return {"alert_type": alert_type, "price": price, "volume": volume}


def _parse_squeeze_levels(line: str) -> list[float]:
    """Extract slash-separated price levels from a 'close over X/Y/Z' line."""
    m = re.search(r'(?i)close\s+over\s+([\d./]+)', line)
    if not m:
        return []
    levels = []
    for part in m.group(1).split('/'):
        try:
            levels.append(float(part.strip()))
        except ValueError:
            pass
    return levels


# ── Scanner Alert! card parsing (new mobile-style layout) ────────────────────
# Compact cards posted by the scanner bot. Vision OCR often splits the label/
# value pairs onto separate lines, so we scan a short window after the headline.

# The scanner bot posts several differently-branded alert headers ("Scanner
# Alert!", "Find It First Alert!", etc.), all followed by an optional [TIER]
# badge. Match the two known names explicitly, plus any "<words> Alert!" header
# that carries a [TIER] bracket (the bracket keeps the generic branch from
# firing on ordinary chat). Emission is still gated on a valid ticker headline.
_SCANNER_ALERT_HEADER_RE = re.compile(
    r"(?:(?P<brand>Scanner|Find\s+It\s+First)\s+Alert!"
    r"|(?:[A-Za-z]+\s+){1,4}Alert!(?=\s*\[))"
    r"\s*(?:\[(?P<tier>[A-Z]+)\])?",
    re.I,
)


def _normalize_card_brand(brand: str | None) -> str:
    """Map a scanner-alert header brand to a stable tag. Only "Find It First"
    is distinguished today (it drives the momentum monitor's FIRST badge);
    the generic scanner and every other header collapse to ""."""
    if brand and re.search(r"find\s+it\s+first", brand, re.I):
        return "find_it_first"
    return ""

# The card headline is a ticker, optionally followed by an alert type. Some
# cards (e.g. "Find It First") show only the bare ticker ("$AGEN"), so the alert
# type is optional. A bare ticker is only accepted as a card headline when it
# sits under a recognised alert header (see parse_scanner_cards); the standalone
# branch still requires a real alert type so lone uppercase words can't fire.
_SCANNER_CARD_HEADLINE_RE = re.compile(
    r"^\s*\$?(?P<ticker>[A-Z]{2,5})(?:\s+(?P<alert_type>.+?))?\s*!?\s*$",
    re.I,
)

# Timestamp / channel noise that often precedes a card in the OCR stream.
_SCANNER_NOISE_RE = re.compile(
    r"^\d{1,2}:\d{2}\s*[AP]M(?:\s+APP)?$|^(?:Day\s+Trading|APP)$",
    re.I,
)

_SCANNER_ALERT_TYPE_RE = re.compile(
    r"(?i)(?:price\s+)?(?:volatility\s+)?spike|new\s+daily|new\s+weekly|"
    r"gap\s+up|volume\s+breakout|momentum|halts?|resume",
)

# One ordered pass over the card body yields Price/Float labels and numeric
# values in reading order. See _extract_price_float_from_lines for how they are
# paired — between them they handle every observed layout: interleaved
# ("Price $251 Float Size 2.42M"), vertically stacked (label / value / label /
# value), and two-column tables whose header row lists the labels and the
# following row the values ("Float Size Price" / "41.12M $4.05"), in either
# column order.
#
# A label used to count only when a value, another label, or end-of-text came
# next. That was too strict in practice: the OCR sweep pulls Discord's sidebar
# into the card body, so a live $MB card read "… Float Size Price / Premium
# Scanners LIVE / $5.33 / 4.69M" and the Price label was skipped because the
# sidebar's channel name followed it — the price was silently lost. Two narrow
# exclusions replace it, letting arbitrary sidebar text sit between a label and
# its value while still rejecting the two non-column uses of the words:
#   "Price Spike!" / "Price Volatility Spike" — part of a card headline.
#   "Price = 41.83"                           — a classic arrow-alert readout,
#                                               parsed by parse_alert_line.
#
# The exclusions sit inside a lookahead that spans the separator itself. Placed
# after "\s*:?\s*" instead, the engine simply backtracks the whitespace to
# zero width and the guard passes on the very text it was meant to reject.
_FIELD_SCAN_RE = re.compile(
    r"(?P<flabel>Float\s*Size|Float)\b(?!\s*:?\s*=)\s*:?\s*"
    r"|(?P<plabel>Price)\b(?!\s*:?\s*(?:(?:Volatility\s+)?Spike\b|=))\s*:?\s*"
    r"|(?P<value>\$?\d[\d,]*\.?\d*)\s*(?P<suffix>[KMB])?\b",
    re.I,
)


def _parse_scaled_number(num_str: str, suffix: str = "") -> float | None:
    """Parse a human-readable number like 2.42M / 150K into a float."""
    try:
        val = float(num_str.replace(",", ""))
    except ValueError:
        return None
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get((suffix or "").upper(), 1.0)
    return val * mult


def _scanner_field_window_end(lines: list[str], start: int, end: int) -> int:
    """Stop before the next card headline/header so fields don't bleed across alerts."""
    limit = min(end, len(lines))
    for i in range(start, limit):
        text = lines[i].strip()
        if not text or _SCANNER_NOISE_RE.match(text):
            continue
        if i == start:
            continue
        if _SCANNER_ALERT_HEADER_RE.search(text):
            return i
        hm = _SCANNER_CARD_HEADLINE_RE.match(text)
        if hm and hm.group("alert_type") and _SCANNER_ALERT_TYPE_RE.search(
                hm.group("alert_type")):
            return i
    return limit


def _extract_price_float_from_lines(
    lines: list[str], start: int, end: int,
) -> tuple[float | None, float | None, set[int]]:
    """Scan a slice of OCR lines for Price and Float Size fields.

    Walks the card body in reading order and pairs each label with the next value
    (FIFO), so it is correct regardless of whether the card interleaves
    label/value pairs, stacks them vertically, or lays them out as a two-column
    header+value table in either Price-first or Float-first order.
    """
    end = _scanner_field_window_end(lines, start, end)
    window = [(i, lines[i].strip()) for i in range(start, end)
              if lines[i].strip() and not _SCANNER_NOISE_RE.match(lines[i].strip())]

    # Join the window into one blob, tracking which line each character came from
    # so the lines that actually supplied a field can be marked consumed.
    segments: list[tuple[int, int, int]] = []   # (blob_start, blob_end, line_idx)
    pieces: list[str] = []
    pos = 0
    for i, text in window:
        if pieces:
            pieces.append(" ")
            pos += 1
        seg_start = pos
        pieces.append(text)
        pos += len(text)
        segments.append((seg_start, pos, i))
    blob = "".join(pieces)

    def _line_of(char_pos: int) -> int | None:
        for s, e, idx in segments:
            if s <= char_pos < e:
                return idx
        return None

    price: float | None = None
    float_size: float | None = None
    used: set[int] = set()
    pending: list[tuple[str, int | None]] = []   # (kind, label line idx), FIFO

    for m in _FIELD_SCAN_RE.finditer(blob):
        if m.group("flabel") is not None:
            pending.append(("float", _line_of(m.start())))
            continue
        if m.group("plabel") is not None:
            pending.append(("price", _line_of(m.start())))
            continue

        raw    = m.group("value")
        suffix = m.group("suffix") or ""
        val    = _parse_scaled_number(raw.lstrip("$"), suffix)
        if val is None or not pending:
            continue

        # A value carries its own type: "$4.05" is a price, "41.12M" is a float
        # size — a float is never written with "$" and a price never with a
        # K/M/B suffix. Trust that over reading order, because the OCR sweep
        # pulls Discord's sidebar into the card body, so a "Float Size Price"
        # header row is regularly followed by the price value first. Position
        # alone put $5.33 in the float column on a live $MB card and dropped
        # the 4.69M float entirely. Both markers or neither is ambiguous —
        # those still fall back to the reading-order FIFO.
        typed: str | None = None
        if raw.startswith("$") and not suffix:
            typed = "price"
        elif suffix and not raw.startswith("$"):
            typed = "float"

        idx = 0
        if typed is not None:
            # Only honour the hint once that field's label has been seen, so a
            # stray "$5.00" swept in from the sidebar cannot become a price.
            idx = next((k for k, (kind, _) in enumerate(pending)
                        if kind == typed), -1)
            if idx < 0:
                continue

        kind, label_line = pending.pop(idx)
        if kind == "price" and price is None:
            price = val
        elif kind == "float" and float_size is None:
            float_size = val
        else:
            continue
        for li in (label_line, _line_of(m.start())):
            if li is not None:
                used.add(li)

    return price, float_size, used


def _scanner_card_summary(
    ticker: str, alert_type: str, price: float | None,
    float_size: float | None, tier: str | None,
) -> str:
    parts = [f"${ticker} {alert_type}!" if alert_type else f"${ticker}"]
    if price is not None:
        parts.append(f"Price ${price:g}")
    if float_size is not None:
        if float_size >= 1e6:
            parts.append(f"Float {float_size / 1e6:.2f}M")
        elif float_size >= 1e3:
            parts.append(f"Float {float_size / 1e3:.1f}K")
        else:
            parts.append(f"Float {float_size:g}")
    summary = " | ".join(parts)
    return f"[{tier}] {summary}" if tier else summary


def parse_scanner_cards(lines: list[str]) -> tuple[list[dict], set[int]]:
    """Parse new-style Scanner Alert! cards that may span multiple OCR lines.

    Returns (cards, used_line_indices). Each card dict has:
      ticker, kind, line, alert_type, price, float_size, scanner_tier, volume
    """
    cards: list[dict] = []
    used: set[int] = set()
    n = len(lines)

    def _emit_card(
        headline_idx: int, tier: str | None, header_idx: int | None,
        require_fields: bool, brand: str = "",
    ) -> bool:
        raw_headline = lines[headline_idx].strip()
        hm = _SCANNER_CARD_HEADLINE_RE.match(raw_headline)
        if not hm:
            return False
        ticker = hm.group("ticker").upper()
        off_universe = False
        if not is_valid_ticker(ticker):
            if header_idx is None:
                _note_drop(ticker, "not a listed ticker, no alert header above it")
                return False
            if not _off_universe_ticker_ok(raw_headline, ticker, hm):
                _note_drop(ticker, "not a listed ticker and headline failed the OTC guard")
                return False
            off_universe = True

        alert_type = re.sub(
            r"[!?.,]+$", "", (hm.group("alert_type") or "").strip(),
        ).strip().title()

        # Include the headline line — OCR sometimes keeps Price/Float inline there.
        field_start = headline_idx
        field_end = min(headline_idx + 8, n)
        price, float_size, field_used = _extract_price_float_from_lines(
            lines, field_start, field_end,
        )
        field_used.discard(headline_idx)

        if require_fields and price is None and float_size is None:
            return False

        used.add(headline_idx)
        used.update(field_used)
        if header_idx is not None:
            used.add(header_idx)

        cards.append({
            "ticker":       ticker,
            "kind":         "scanner_card",
            "line":         _scanner_card_summary(
                ticker, alert_type, price, float_size, tier,
            ),
            "alert_type":   alert_type,
            "price":        price,
            "float_size":   float_size,
            "scanner_tier": tier,
            "card_brand":   brand,
            "off_universe": off_universe,
            "volume":       None,
        })
        if off_universe:
            _note_off_universe(ticker, "a scanner card")
        return True

    i = 0
    while i < n:
        if i in used:
            i += 1
            continue
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        hdr = _SCANNER_ALERT_HEADER_RE.search(line)
        if hdr:
            tier = (hdr.group("tier") or "").upper() or None
            brand = _normalize_card_brand(hdr.group("brand"))
            headline_idx = None
            for j in range(i + 1, min(i + 5, n)):
                if j in used:
                    continue
                cand = lines[j].strip()
                if not cand or _SCANNER_NOISE_RE.match(cand):
                    continue
                if _SCANNER_CARD_HEADLINE_RE.match(cand):
                    headline_idx = j
                    break
            if headline_idx is not None:
                _emit_card(headline_idx, tier, i, require_fields=False,
                           brand=brand)
            else:
                used.add(i)
            i += 1
            continue

        hm = _SCANNER_CARD_HEADLINE_RE.match(line)
        if hm and hm.group("alert_type") and _SCANNER_ALERT_TYPE_RE.search(
                hm.group("alert_type")):
            prev_has_header = any(
                _SCANNER_ALERT_HEADER_RE.search(lines[j].strip())
                for j in range(max(0, i - 3), i)
                if j not in used
            )
            if not prev_has_header:
                _emit_card(i, None, None, require_fields=True)
        i += 1

    return cards, used


def _scanner_card_to_alert(card: dict) -> dict:
    """Normalise a parsed scanner card into the dashboard ingest alert shape."""
    alert_type = str(card.get("alert_type") or "")
    price_spike = bool(re.search(r"(?i)spike", alert_type))
    return {
        "ticker":       card["ticker"],
        "line":         card["line"],
        "burst":        False,
        "alert_type":   alert_type or None,
        "price":        card.get("price"),
        "volume":       card.get("volume"),
        "float_size":   card.get("float_size"),
        "scanner_tier": card.get("scanner_tier"),
        "card_brand":   card.get("card_brand") or "",
        "off_universe": bool(card.get("off_universe")),
        "price_spike":  price_spike,
    }


def parse_alert_line(line: str) -> tuple[str, str, dict] | tuple[None, None, dict]:
    """Parse one OCR line into (ticker, kind, meta), or (None, None, {}) if it
    isn't an alert. kind is "squeeze" or "alert". meta contains:
      - regular: {alert_type, price, volume}
      - squeeze: {levels: [float, ...]}
    """
    is_squeeze = bool(_SQUEEZE_MARKER.search(line))
    if not (is_squeeze or _ALERT_MARKER.search(line)):
        return None, None, {}
    for tok in line.split():
        alpha = re.sub(r"[^A-Za-z]", "", tok)
        if not alpha:
            continue
        sym = alpha.upper()
        if not (2 <= len(sym) <= 5):
            return None, None, {}
        # Same universe gap as the scanner cards: valid_tickers.txt has no OTC
        # symbols, so OTC alert lines were dropped in silence. Accept an unknown
        # symbol only when the rest of the line is shaped the way the scanner
        # writes one — an all-caps leading symbol followed by a recognised alert
        # phrase ("New Daily High", "Price Volatility Spike") before the arrow,
        # or a squeeze line, whose "close over" marker is specific on its own.
        # Ordinary prose that happens to start with a 5-letter word and contain
        # arrows ("HELLO there SPY >>>>>") fails that shape and still drops.
        if not is_valid_ticker(sym):
            head = _ALERT_MARKER.split(line, 1)[0]
            head = head[head.find(tok) + len(tok):] if tok in head else ""
            shaped = is_squeeze or bool(_SCANNER_ALERT_TYPE_RE.search(head))
            if (alpha != sym or not shaped
                    or sym in _CARD_HEADLINE_SKIP or sym in _CHAT_SKIP):
                _note_drop(sym, "not a listed ticker and alert line failed the OTC guard")
                return None, None, {}
            _note_off_universe(sym, "an alert line")
        if is_squeeze:
            meta = {"levels": _parse_squeeze_levels(line)}
        else:
            meta = _parse_regular_meta(sym, line)
        return sym, ("squeeze" if is_squeeze else "alert"), meta
    return None, None, {}


def _first_valid_ticker(text: str) -> str | None:
    for tok in text.split():
        alpha = re.sub(r"[^A-Za-z]", "", tok).upper()
        if 2 <= len(alpha) <= 5 and alpha not in _CHAT_SKIP and is_valid_ticker(alpha):
            return alpha
    return None


def _score_text(body: str, original_line: str) -> float:
    body_l  = body.lower()
    raw_sum = 0.0
    for phrase, val in _KW_SORTED:
        if phrase in body_l:
            raw_sum += val
            body_l   = body_l.replace(phrase, " ")   # consume so sub-words can't re-match
    for emoji, val in _EMOJI_SCORES.items():
        if emoji in original_line:
            raw_sum += val
    return max(-1.0, min(1.0, raw_sum / 3.0))


def parse_market_signal(line: str) -> tuple[str | None, float, str]:
    m = _HV_RE.search(line)
    if not m:
        return None, 0.0, ""
    score = 1.0 if m.group("bull") else -1.0
    return "SPY", score, ""


def parse_chat_sentiment(line: str) -> tuple[str | None, float, str]:
    m = _CHAT_RE.match(line)
    if not m:
        return None, 0.0, ""
    body = m.group("body")
    if ">>>>>" in body or "bb live alert" in body.lower():
        return None, 0.0, ""
    ticker = _first_valid_ticker(body)
    score  = _score_text(body, line)
    if abs(score) < 0.15:
        return None, 0.0, ""
    return ticker, score, body.strip()


def _update_tv_state(line: str, tv_state: "_TvState",
                     new_sentiment: list, seen: dict, t0: float) -> None:
    """Pair TREND + STRATEGY chart labels across consecutive OCR lines into a
    tv_chart sentiment event. The ticker comes from the chart header line that
    precedes the labels. Cross-scan dedupe is gated on the STRATEGY line's
    signature so a chart visible across polls only fires once."""
    tkr = _first_valid_ticker(line)
    if tkr:
        tv_state.pending_ticker = tkr
    if _TV_TREND_RE.search(line):
        tv_state.pending_trend = True
        return
    sm = _TV_STRATEGY_RE.search(line)
    if sm and tv_state.pending_trend is not None:
        sig = _signature(line)
        if sig not in seen:
            seen[sig] = t0
            score = 1.0 if sm.group("strat").upper() == "BULLISH" else -1.0
            new_sentiment.append(
                SentimentEvent(tv_state.pending_ticker, score, "tv_chart", line.strip(), t0))
        tv_state.pending_trend = None


def _signature(line: str) -> str:
    """Stable de-dupe key: collapse to lower-case alphanumerics so OCR jitter in
    spacing/punctuation doesn't make the same on-screen alert look 'new'. The
    embedded price keeps successive same-ticker alerts distinct (so repeated
    spikes are each counted, which is what drives burst detection)."""
    return re.sub(r"[^A-Za-z0-9]", "", line).lower()


def _scanner_card_signature(card: dict) -> str:
    """Stable de-dupe key for Scanner Alert! cards.

    Ticker + alert type + tier only — deliberately ignores price/float/line text so
    OCR cannot re-fire the same on-screen card when one poll reads Price and the
    next reads Float Size. Session TTL in the main loop prevents duplicates."""
    ticker = str(card.get("ticker", "")).upper()
    alert_type = re.sub(
        r"[^A-Za-z0-9]", "", str(card.get("alert_type") or ""),
    ).lower()
    tier = str(card.get("scanner_tier") or "").upper()
    return f"sc|{ticker}|{alert_type}|{tier}".lower()


# ── OCR + delivery ────────────────────────────────────────────────────────────

def _ocr_command(cfg: dict) -> list[str]:
    """Build the Swift-binary command (fallback only). Empty list = use native."""
    owner = str(cfg.get("discord_window_owner") or "Discord")
    title = str(cfg.get("discord_window_title") or "").strip()
    if _native_ocr is not None and _native_ocr.available():
        return []  # signal: native path
    if OCR_BINARY.exists():
        if OCR_SCRIPT.exists() and OCR_SCRIPT.stat().st_mtime > OCR_BINARY.stat().st_mtime:
            print("[discord] WARNING: discord_ocr.swift is newer than the compiled binary — "
                  "rebuild with:  bash scripts/build_ocr.sh", flush=True)
        cmd = [str(OCR_BINARY)]
    elif OCR_SCRIPT.exists():
        cmd = ["swift", str(OCR_SCRIPT)]
    else:
        print("[discord] ERROR: native OCR unavailable and discord_ocr not found.", flush=True)
        print("  Install PyObjC Quartz, or:  bash scripts/build_ocr.sh", flush=True)
        raise SystemExit(1)
    cmd += ["--owner", owner]
    if title:
        cmd += ["--title", title]
    return cmd


def _run_ocr_native(cfg: dict) -> tuple[list[str], bool]:
    """In-process capture + Vision OCR (preferred — uses Python's Screen Recording)."""
    assert _native_ocr is not None
    owner = str(cfg.get("discord_window_owner") or "Discord")
    title = str(cfg.get("discord_window_title") or "").strip()
    try:
        lines = _native_ocr.capture_and_ocr(owner=owner, title=title, accurate=False)
        return lines, True
    except Exception as e:
        print(f"[discord] OCR failed (native): {e}", flush=True)
        return [], False


def _run_ocr_binary(cmd: list[str]) -> tuple[list[str], bool]:
    """Swift binary fallback. Own process group so hung children can be killed."""
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=18,
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as e:
        if getattr(e, "pid", None):
            try:
                os.killpg(e.pid, 9)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(e.pid, 9)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        try:
            subprocess.run(
                ["pkill", "-9", "-f", str(OCR_BINARY)],
                capture_output=True,
                timeout=2,
            )
        except Exception:
            pass
        print("[discord] OCR timed out", flush=True)
        return [], False
    if out.returncode != 0:
        lines = [ln for ln in (out.stderr or "").strip().splitlines() if ln.strip()]
        if not lines:
            detail = str(out.returncode)
        elif len(lines) <= 2:
            detail = " | ".join(lines)
        else:
            detail = f"{lines[0]} | … | {lines[-1]}"
        print(f"[discord] OCR failed (rc={out.returncode}): {detail}", flush=True)
        return [], False
    return [ln for ln in out.stdout.splitlines() if ln.strip()], True


def _run_ocr(cmd: list[str], cfg: dict | None = None) -> tuple[list[str], bool]:
    """Run one OCR pass. Prefers native Python capture; falls back to Swift binary.

    Returns (lines, ok). ok=False = process-level failure (no window, permission,
    crash) — distinct from a successful capture with zero text lines.
    """
    if not cmd:
        return _run_ocr_native(cfg or _load_config())
    return _run_ocr_binary(cmd)


def _post_ingest(alerts: list[dict], sentiment: list) -> None:
    """POST this poll's newly-captured alerts (and an implicit heartbeat) to the
    dashboard. Sent every poll even when empty so the dashboard knows the source
    is alive. Each alert drives the mention system + the live feed downstream.
    sentiment carries SentimentEvent records (market/chat/chart direction)."""
    try:
        body = json.dumps({"alerts": alerts,
                           "sentiment": [asdict(e) for e in sentiment],
                           "drops": drop_counts()}).encode()
        req  = urllib.request.Request(
            f"{DASHBOARD_URL}/api/discord/ingest",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
        for a in alerts:
            print(f"  → {a['ticker']}", flush=True)
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError) or "timed out" in str(e).lower():
            print(f"[discord] POST timeout — dashboard slow? ({e})", flush=True)
        elif alerts:
            print(f"  → {[a['ticker'] for a in alerts]}  (API error: {e})", flush=True)
        else:
            print(f"[discord] heartbeat POST failed: {e}", flush=True)
    except Exception as e:
        if alerts:
            print(f"  → {[a['ticker'] for a in alerts]}  (API error: {e})", flush=True)
        else:
            print(f"[discord] heartbeat POST failed: {e}", flush=True)


# ── Main loop ─────────────────────────────────────────────────────────────────

# Signatures expire after one full trading session so early-session alerts can
# never be evicted by sheer volume and accidentally re-fire later in the day.
_SESSION_TTL = 8 * 3600   # 8 hours

# Backoff caps how long we wait between retries when the Discord window can't be
# found (e.g. Discord is closed or on a different Space).
_MAX_BACKOFF_SEC = 60.0


def check() -> int:
    """One-shot self-test: capture once, report what OCR saw and parsed, and give
    a verdict. No priming, no POST, no loop. Returns a shell exit code so it's
    scriptable.  Run:  python discord_source.py --check"""
    cfg          = _load_config()
    cmd          = _ocr_command(cfg)
    lines, ok    = _run_ocr(cmd, cfg)
    if not ok:
        print("[discord] VERDICT: ✗ OCR process failed — see error above.")
        return 1
    alerts = []
    cards, _ = parse_scanner_cards(lines)
    for card in cards:
        alerts.append((card["ticker"], card["kind"], card["line"]))
    for ln in lines:
        tkr, kind, _ = parse_alert_line(ln)
        if tkr:
            alerts.append((tkr, kind, ln))

    print(f"[discord] OCR read {len(lines)} text line(s) from the Discord window.")
    if not lines:
        print("[discord] VERDICT: ✗ window not captured — is Discord open, on this "
              "display/Space, and NOT minimized? (Grant Screen Recording if prompted.)")
        return 1
    if not alerts:
        print("[discord] VERDICT: ⚠ window captured, but no alert line is visible. "
              "Scroll the alert channel so the latest alerts are on screen.")
        return 2
    print(f"[discord] parsed {len(alerts)} alert(s):")
    for tkr, kind, ln in alerts:
        if kind == "squeeze":
            tag = "🔥burst"
        elif kind == "scanner_card":
            tag = "scanner"
        else:
            tag = "mention"
        print(f"   {tkr:6} {tag:8} <=  {ln[:70]}")
    print("[discord] VERDICT: ✓ working — these tickers would post as they newly appear "
          "(squeeze alerts fire the burst toast).")
    return 0


def main() -> None:
    cfg      = _load_config()
    poll_sec = float(cfg.get("discord_ocr_poll_sec", 2.5) or 2.5)
    cmd      = _ocr_command(cfg)

    print(f"[discord] OCR source started — polling every {poll_sec:g}s", flush=True)
    if cmd:
        print(f"[discord] backend: swift binary — {' '.join(cmd)}", flush=True)
    else:
        owner = str(cfg.get("discord_window_owner") or "Discord")
        print(f"[discord] backend: native Python capture+Vision (owner={owner!r})", flush=True)
    print(f"[discord] posting alerts → {DASHBOARD_URL}/api/discord/ingest", flush=True)

    # sig → first-seen timestamp; entries expire after _SESSION_TTL seconds.
    seen: dict[str, float] = {}
    # First scan is special: the channel already shows several alerts. We surface
    # each VISIBLE ticker once (so the watchlist populates immediately) but never
    # re-post the same on-screen lines, and we collapse to one-per-ticker so a
    # screen full of repeats (e.g. MTEN ×7) can't fake a startup burst. After the
    # first scan, every genuinely new alert line is posted as it appears.
    primed      = False
    fail_streak = 0   # consecutive OCR process failures (window not found, crash)

    while True:
        t0 = time.time()

        # Expire stale signatures from previous sessions.
        expired = [s for s, ts in seen.items() if t0 - ts > _SESSION_TTL]
        for s in expired:
            del seen[s]

        lines, ok = _run_ocr(cmd, cfg)

        if not ok:
            # Exponential backoff so we don't spam logs when Discord is closed.
            fail_streak += 1
            backoff = min(poll_sec * (2 ** (fail_streak - 1)), _MAX_BACKOFF_SEC)
            time.sleep(backoff)
            continue

        fail_streak = 0
        new_alerts: list[dict] = []
        new_sentiment: list[SentimentEvent] = []
        scanner_state = _ScannerState()  # resets each scan
        first_frame: "OrderedDict[str, dict]" = OrderedDict()   # ticker → alert dict
        cards, card_used = parse_scanner_cards(lines)
        for card in cards:
            sig = _scanner_card_signature(card)
            if sig in seen:
                continue
            seen[sig] = t0
            alert = _scanner_card_to_alert(card)
            if primed:
                new_alerts.append(alert)
            else:
                first_frame.setdefault(card["ticker"], alert)

        for idx, line in enumerate(lines):
            if idx in card_used:
                continue
            sig = _signature(line)
            ticker, kind, meta = parse_alert_line(line)
            if ticker:
                if sig in seen:
                    continue
                seen[sig] = t0
                # A squeeze breakout is a strong catalyst → ask the dashboard to
                # fire the burst toast immediately (simulate a mention burst).
                alert = {"ticker": ticker, "line": line, "burst": kind == "squeeze", **meta}
                if primed:
                    new_alerts.append(alert)
                else:
                    first_frame.setdefault(ticker, alert)
                continue

            # Non-alert line: sentiment comes only from Market Update scanner
            # tables and human chat (HV SPY direction + TV chart labels are
            # intentionally not surfaced — too noisy / not per-ticker actionable).
            if _update_scanner_state(line, scanner_state, new_sentiment, seen, t0):
                continue

            c_ticker, c_score, c_raw = parse_chat_sentiment(line)
            if c_score != 0.0:
                if sig in seen:
                    continue
                seen[sig] = t0
                new_sentiment.append(
                    SentimentEvent(c_ticker, c_score, "chat", c_raw, t0))
        if not primed:
            primed = True
            new_alerts = list(first_frame.values())
            print(f"[discord] startup: surfacing {len(new_alerts)} visible ticker(s); "
                  "watching for new alerts…", flush=True)
        # POST every poll (even with no new alerts) — it doubles as a heartbeat
        # so the dashboard can show the source is alive on a quiet market.
        _post_ingest(new_alerts, new_sentiment)
        elapsed = time.time() - t0
        time.sleep(max(0.0, poll_sec - elapsed))


if __name__ == "__main__":
    if "--check" in sys.argv or "--once" in sys.argv:
        raise SystemExit(check())
    try:
        main()
    except KeyboardInterrupt:
        print("\n[discord] stopped.", flush=True)
