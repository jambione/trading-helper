#!/usr/bin/env python3
"""
ticker_extract.py — pure ticker-recognition + validation, ZERO audio deps.

Imports only the stdlib (re, time, urllib, pathlib), so loading it is instant.
It owns the NASDAQ/NYSE ticker universe (cached in ../valid_tickers.txt) and the
validation/extraction helpers. The Discord OCR source (discord_source.py) reuses
is_valid_ticker() from here so there is a single source of truth for what counts
as a real ticker.

Public API:
    extract_tickers(text) -> dict[str, int]
    is_valid_ticker(sym)  -> bool
    VALID_TICKERS         : set[str]
"""

from __future__ import annotations

import re
import time
import urllib.request
from pathlib import Path


# =============================================================================
# TICKER VALIDATION — NASDAQ + NYSE list
# =============================================================================

_TICKER_CACHE_FILE = Path(__file__).parent.parent / "valid_tickers.txt"
_TICKER_CACHE_DAYS = 7


def _load_valid_tickers() -> set:
    def _fetch() -> set:
        tickers = set()
        sources = [
            ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
             lambda line: line.split("|")[0].strip()),
            ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
             lambda line: line.split("|")[0].strip()),
        ]
        for url, extract in sources:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    for raw in r.read().decode("utf-8", errors="ignore").splitlines():
                        sym = extract(raw)
                        if sym and sym.isalpha() and 1 <= len(sym) <= 5 \
                                and sym not in ("Symbol", "ACTSymbol"):
                            tickers.add(sym.upper())
            except Exception as e:
                print(f"[TICKERS] Warning: could not fetch {url}: {e}", flush=True)
        return tickers

    try:
        if _TICKER_CACHE_FILE.exists():
            age_days = (time.time() - _TICKER_CACHE_FILE.stat().st_mtime) / 86400
            if age_days < _TICKER_CACHE_DAYS:
                syms = set(_TICKER_CACHE_FILE.read_text().split())
                if syms:
                    print(f"[TICKERS] Loaded {len(syms):,} tickers from cache ({age_days:.1f}d old).", flush=True)
                    return syms
        print("[TICKERS] Downloading ticker list from NASDAQ Trader …", flush=True)
        tickers = _fetch()
        if tickers:
            _TICKER_CACHE_FILE.write_text("\n".join(sorted(tickers)))
            print(f"[TICKERS] Downloaded and cached {len(tickers):,} tickers.", flush=True)
        return tickers
    except Exception as e:
        print(f"[TICKERS] Could not load ticker list: {e}", flush=True)
        return set()


_VALID_TICKERS: set = _load_valid_tickers()
VALID_TICKERS = _VALID_TICKERS   # public alias

# Match all-caps tokens — OCR output keeps tickers in ALL-CAPS.
_TICKER_RE = re.compile(r'\b([A-Z]{2,5})\b')

# Words that match _TICKER_RE but are never valid ticker mentions.
_TICKER_STOPWORDS: frozenset = frozenset({
    # Time / dates
    "AM", "PM", "AD", "BC",
    # Regulatory / government agencies (not exchange-listed). CIA is Citizens
    # Inc's symbol, but in a news stream the agency ("the CIA says ...") swamps
    # genuine $CIA mentions — same rationale as AI/TC below.
    "FDA", "SEC", "FTC", "FED", "IRS", "CDC", "NIH", "DOJ", "DOD", "CIA",
    # Currencies / macros
    "USD", "EUR", "GBP", "JPY", "CNY",
    # Executive titles
    "CEO", "CFO", "CTO", "COO", "CIO",
    # Market structure terms (not tickers)
    "IPO", "NYSE", "ETF",
    # Very common English two-letter all-caps that appear in transcripts
    "OK", "US", "EU", "UK",
    # Topic words / mis-splits that collide with real tickers in CNBC audio
    # ("AI server" -> AI; spelled "I N T C" mis-heard -> TC). AI is C3.ai's
    # symbol, but in a transcription stream the topic-word false positives
    # vastly outnumber genuine $AI mentions.
    "AI", "TC",
})

# Well-known company names that may appear in OCR text.
# Matched on WORD BOUNDARIES (see _COMPANY_RE) — never as substrings — so
# "intel" no longer fires on "intelligence" and "ford" stays off "Stanford".
# Curated to avoid common-English collisions: deliberately NO "target" (price
# target), "arm", "block", "square", bare "gm"/"ms" — those are added only via
# unambiguous full phrases.
_COMPANY_NAMES: dict[str, str] = {
    # Mega-cap tech
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "amazon": "AMZN",
    "alphabet": "GOOGL", "google": "GOOGL", "netflix": "NFLX", "tesla": "TSLA",
    "intel": "INTC", "meta": "META", "facebook": "META", "palantir": "PLTR",
    "coinbase": "COIN", "spotify": "SPOT", "roblox": "RBLX", "doordash": "DASH",
    "broadcom": "AVGO", "qualcomm": "QCOM", "oracle": "ORCL", "salesforce": "CRM",
    "adobe": "ADBE", "micron": "MU", "snowflake": "SNOW", "shopify": "SHOP",
    "paypal": "PYPL", "uber": "UBER", "lyft": "LYFT", "airbnb": "ABNB",
    # Finance
    "goldman": "GS", "goldman sachs": "GS", "jpmorgan": "JPM", "jp morgan": "JPM",
    "bank of america": "BAC", "citigroup": "C", "wells fargo": "WFC",
    "morgan stanley": "MS", "blackrock": "BLK", "robinhood": "HOOD",
    "sofi": "SOFI", "visa": "V", "mastercard": "MA",
    # Consumer / industrial
    "boeing": "BA", "costco": "COST", "walmart": "WMT", "disney": "DIS",
    "nike": "NKE", "starbucks": "SBUX", "mcdonalds": "MCD", "mcdonald's": "MCD",
    "coca cola": "KO", "pepsi": "PEP", "home depot": "HD",
    # Energy
    "chevron": "CVX", "exxon": "XOM", "exxonmobil": "XOM",
    # Auto / EV
    "ford": "F", "general motors": "GM", "rivian": "RIVN", "lucid": "LCID",
    "nio": "NIO",
    # Pharma
    "pfizer": "PFE", "moderna": "MRNA", "eli lilly": "LLY",
    # Semis / other popular trading names
    "taiwan semi": "TSM", "asml": "ASML", "marvell": "MRVL",
    "microstrategy": "MSTR", "marathon digital": "MARA", "riot": "RIOT",
    "gamestop": "GME", "blackberry": "BB",
}
# Longest names first so "goldman sachs" wins over "goldman"; word-boundaried
# and case-insensitive so "Goldman Sachs" / "GOLDMAN" all match.
_COMPANY_RE = re.compile(
    r"\b(" + "|".join(re.escape(n) for n in
                      sorted(_COMPANY_NAMES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def is_valid_ticker(sym: str) -> bool:
    """True if `sym` is a real NASDAQ/NYSE symbol and not a known false-positive
    stopword. Single source of truth for the ticker universe, reused by other
    producers (e.g. the Discord OCR source) so they don't duplicate the list."""
    s = sym.strip().upper()
    return bool(s) and s in _VALID_TICKERS and s not in _TICKER_STOPWORDS


def extract_tickers(text: str) -> dict[str, int]:
    """Return {ticker: count} for all valid NASDAQ/NYSE tickers found in text.

    Company names (e.g. "apple", "nvidia") are expanded to their symbols before
    scanning. Word-boundary matching prevents "intel" firing on "intelligence".
    """
    extra: list[str] = [
        _COMPANY_NAMES[m.group(1).lower()] for m in _COMPANY_RE.finditer(text)
    ]
    if extra:
        text = text + " " + " ".join(extra)

    counts: dict[str, int] = {}
    for m in _TICKER_RE.finditer(text):
        t = m.group(1)
        if t in _VALID_TICKERS and t not in _TICKER_STOPWORDS:
            counts[t] = counts.get(t, 0) + 1
    return counts


if __name__ == "__main__":
    import sys
    txt = " ".join(sys.argv[1:]) or "I like NVDA and Apple, also nvidia earnings"
    print("tickers:", extract_tickers(txt))
