#!/usr/bin/env python3
"""
ticker_extract.py — pure ticker-recognition logic, ZERO ASR/audio dependencies.

This is the detection brain split out of transcribe_action.py. It imports only
the stdlib (re, time, urllib, pathlib), so `from ticker_extract import
extract_tickers` loads in milliseconds instead of pulling in mlx_whisper / torch
/ pyaudio (which made the old `from transcribe_action import extract_tickers`
take minutes and ~2 GB). That fast import is what makes it practical to unit-test
and iterate on recognition accuracy — the part that actually moves the needle.

transcribe_action.py imports everything public from here, so its behaviour is
unchanged; this is a structural split, not a logic change.

Public API:
    normalize_transcript(text) -> str
    extract_tickers(text)      -> dict[str, int]
    extract_with_stitch(text)  -> dict[str, int]   # cross-chunk boundary aware
    VALID_TICKERS              : set[str]
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

# Only match all-caps tokens — Whisper outputs known tickers in ALL-CAPS,
# common English words in lowercase. This eliminates most false positives
# without any stop-word list.
_TICKER_RE = re.compile(r'\b([A-Z]{2,5})\b')

# Words that match _TICKER_RE but are never valid ticker mentions.
# Keeps common English abbreviations, titles, and date markers from firing.
_TICKER_STOPWORDS: frozenset = frozenset({
    # Time / dates
    "AM", "PM", "AD", "BC",
    # Regulatory / government agencies (not exchange-listed)
    "FDA", "SEC", "FTC", "FED", "IRS", "CDC", "NIH", "DOJ", "DOD",
    # Currencies / macros
    "USD", "EUR", "GBP", "JPY", "CNY",
    # Executive titles
    "CEO", "CFO", "CTO", "COO", "CIO",
    # Market structure terms (not tickers)
    "IPO", "NYSE", "ETF",
    # Very common English two-letter all-caps that appear in transcripts
    "OK", "US", "EU", "UK",
})

# English letter-name phonetics — how TTS voices pronounce individual letters.
# "en vee dee ay" → NVDA, "tee es el ay" → TSLA
_LETTER_NAMES = {
    "ay": "A", "bee": "B", "cee": "C", "see": "C",
    "dee": "D", "ee": "E", "ef": "F", "eff": "F",
    "gee": "G", "jee": "G", "aitch": "H", "haitch": "H",
    "eye": "I", "jay": "J", "kay": "K", "el": "L",
    "em": "M", "en": "N", "oh": "O", "pee": "P",
    "cue": "Q", "queue": "Q", "ar": "R", "arr": "R",
    "ess": "S", "es": "S", "tee": "T", "you": "U",
    "vee": "V", "ex": "X", "eks": "X", "ecks": "X",
    "wye": "Y", "why": "Y", "zee": "Z", "zed": "Z",
}
_letter_name_word    = "(?:" + "|".join(re.escape(w) for w in _LETTER_NAMES) + ")"
_LETTER_NAME_PATTERN = re.compile(
    rf"(?i)\b{_letter_name_word}(?:[ \t]*[.,\-]?[ \t]*{_letter_name_word}){{1,4}}\b"
)

# NATO phonetic alphabet — "charlie romeo bravo papa" → CRBP.
# Includes common Whisper mis-transcriptions (michael→M, romy→R).
_NATO = {
    "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D",
    "echo": "E", "foxtrot": "F", "golf": "G", "hotel": "H",
    "india": "I", "juliet": "J", "kilo": "K", "lima": "L",
    "mike": "M", "michael": "M",
    "november": "N", "oscar": "O", "papa": "P",
    "quebec": "Q", "romeo": "R", "romy": "R",
    "sierra": "S", "tango": "T", "uniform": "U", "victor": "V",
    "whiskey": "W", "xray": "X", "yankee": "Y", "zulu": "Z",
}
_nato_word    = "(?:" + "|".join(re.escape(w) for w in _NATO) + ")"
_NATO_PATTERN = re.compile(
    rf"(?i)\b{_nato_word}(?:[ \t]+{_nato_word}){{1,4}}\b"
)

# Well-known company names that Whisper reliably outputs in lowercase.
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


def _collapse_letter_names(text: str) -> str:
    """en vee dee ay → NVDA, tee es el ay → TSLA"""
    def collapse(m):
        words   = re.split(r'[\s.,\-]+', m.group(0).lower())
        letters = "".join(_LETTER_NAMES.get(w, "") for w in words if w)
        return letters if 2 <= len(letters) <= 5 else m.group(0)
    return _LETTER_NAME_PATTERN.sub(collapse, text)


def _collapse_nato(text: str) -> str:
    """charlie romeo bravo papa → CRBP, bravo romeo kilo romeo → BRKR"""
    def collapse(m):
        words   = m.group(0).lower().split()
        letters = "".join(_NATO.get(w, "") for w in words if w)
        # Require 3+ letters: 2-word NATO matches are almost always fragments,
        # not intentional ticker spells.
        return letters if 3 <= len(letters) <= 5 else m.group(0)
    return _NATO_PATTERN.sub(collapse, text)


def _collapse_hyphens(text: str) -> str:
    """A-I-I-O → AIIO, H-T-T → HTT, B-I-Y-A → BIYA"""
    def collapse(m):
        return m.group(0).replace("-", "").upper()
    return re.sub(r'(?<![A-Za-z])(?:[A-Za-z]-){1,4}[A-Za-z](?![A-Za-z])', collapse, text)


# A real spoken ticker spell is <=5 letters; two back-to-back is <=10. Longer
# single-letter runs are Whisper hallucination streams ("A Z O F A Z U L O ...")
# that the greedy segmenter would otherwise carve dozens of fake tickers from.
_MAX_SPELL_LETTERS = 10


def _collapse_spaced_letters(text: str) -> str:
    """H T T B I Y A → HTT BIYA  (NASDAQ-aware greedy segmentation)"""
    def replace_run(m):
        letters = m.group(0).split()
        if len(letters) > _MAX_SPELL_LETTERS:
            return m.group(0)   # too long to be a real spell — leave it untouched
        result  = []
        i = 0
        while i < len(letters):
            matched = False
            for length in range(min(5, len(letters) - i), 1, -1):
                candidate = "".join(letters[i:i+length]).upper()
                if candidate in _VALID_TICKERS:
                    result.append(candidate)
                    i += length
                    matched = True
                    break
            if not matched:
                result.append(letters[i])
                i += 1
        return " ".join(result)
    return re.sub(r'(?<![A-Za-z])[A-Za-z](?:\s[A-Za-z])+(?![A-Za-z])', replace_run, text)


def normalize_transcript(text: str) -> str:
    """Chain all normalization steps: letter names → NATO → hyphens → spaced letters."""
    text = _collapse_letter_names(text)
    text = _collapse_nato(text)
    text = _collapse_hyphens(text)
    text = _collapse_spaced_letters(text)
    return text


def is_hallucination(text: str) -> bool:
    """
    True if `text` looks like a Whisper hallucination that should NOT be mined for
    tickers. Whisper loops on near-silence/music and emits degenerate streams:
        "B-I-Y-A B-I-Y-A …×31",  "B.I.Y.Y.Y.Y…",  "A-Z-O-F-A-Z-U-L-O-H-O-N-G-O-N-G…"
    Each of those would otherwise spray many 1× false-positive tickers. Three
    signatures, all measured AFTER normalization so collapsed spells are seen:

      1. Repeated-word loop — one token repeated >6×: noise if it's a short
         (<=2 char) or unknown token; even a real ticker repeated >50× is a loop.
      2. Degenerate token — a single long token built from <=3 distinct letters
         ("BIYYYYYY…", "ONGONGONG…").
      3. Prompt/echo dump — a long line that is mostly distinct 2–5 letter caps.
    """
    words = normalize_transcript(text).split()
    if not words:
        return False

    # 1) repeated-word loop
    most = max(set(words), key=words.count)
    rep  = words.count(most)
    core = re.sub(r"[^A-Za-z]", "", most).upper()
    if rep > 6:
        if len(core) <= 2:                       # short unit repeated = noise
            return True
        if core not in _VALID_TICKERS:           # unknown token repeated = junk
            return True
        if rep > 50:                             # even a real ticker, 50+ = loop
            return True

    # 2) degenerate single/double token: long and built from few distinct
    #    letters or dominated by one ("BIYYYYYY…", "EIYAIIAIIIIIII…")
    if len(words) <= 2:
        for w in words:
            letters = re.sub(r"[^A-Za-z]", "", w).upper()
            if len(letters) >= 8:
                distinct = len(set(letters))
                top = max(letters.count(ch) for ch in set(letters))
                if distinct <= 3 or top / len(letters) >= 0.5:
                    return True

    # 3) prompt/echo dump
    caps = [w for w in words if w.isupper() and 2 <= len(w) <= 5]
    if len(words) >= 10 and len(set(caps)) >= 8 and len(caps) / len(words) > 0.70:
        return True

    return False


def extract_tickers(text: str) -> dict[str, int]:
    """Return {ticker: count} for all valid NASDAQ/NYSE tickers found in text."""
    # Expand company names before normalization so they don't interfere with
    # letter-level patterns. Word-boundary matching (not substring) so "intel"
    # doesn't fire on "intelligence".
    extra: list[str] = [
        _COMPANY_NAMES[m.group(1).lower()] for m in _COMPANY_RE.finditer(text)
    ]

    text = normalize_transcript(text)
    if extra:
        text = text + " " + " ".join(extra)

    counts: dict[str, int] = {}
    for m in _TICKER_RE.finditer(text):
        t = m.group(1)  # already uppercase — enforced by regex
        if t in _VALID_TICKERS and t not in _TICKER_STOPWORDS:
            counts[t] = counts.get(t, 0) + 1
    return counts


# =============================================================================
# CROSS-CHUNK TICKER STITCHING
# =============================================================================
# A ticker spelled across a chunk boundary ("...N V" | "D A...") validates as
# neither half alone.  We keep the previous chunk's text and re-extract on the
# joined pair, emitting only tickers that appear in the join but in neither
# half — so normal mentions are never double-counted.
#
# Assumes a single transcription worker (ordering matters for the stitch).

_prev_text:       str            = ""
_prev_tickers:    dict[str, int] = {}
_last_chunk_time: float          = 0.0
STITCH_MAX_GAP                   = 3.0   # seconds; don't stitch across silences


def extract_with_stitch(text: str) -> dict[str, int]:
    """Extract tickers from `text` plus any split across the previous boundary."""
    global _prev_text, _prev_tickers, _last_chunk_time

    now        = time.time()
    contiguous = (now - _last_chunk_time) <= STITCH_MAX_GAP
    _last_chunk_time = now

    # Drop hallucination loops before they spray false positives, and break the
    # stitch chain so we don't glue real text onto garbage.
    if is_hallucination(text):
        _prev_text    = ""
        _prev_tickers = {}
        return {}

    cur = extract_tickers(text)

    out = dict(cur)
    if _prev_text and contiguous:
        joined = extract_tickers(f"{_prev_text} {text}")
        for t, c in joined.items():
            # Boundary-only: present when stitched but in neither half alone
            if t not in cur and t not in _prev_tickers:
                out[t] = out.get(t, 0) + c

    _prev_text    = text
    _prev_tickers = cur
    return out


# Backwards-compatible private alias (transcribe_action.py used this name).
_extract_with_stitch = extract_with_stitch


if __name__ == "__main__":
    import sys
    txt = " ".join(sys.argv[1:]) or "I like NVDA and en vee dee ay, also charlie romeo bravo papa"
    print("normalized:", normalize_transcript(txt))
    print("tickers   :", extract_tickers(txt))
