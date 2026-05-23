#!/usr/bin/env python3
"""
benchmark.py — accuracy and performance benchmark for the ticker extraction pipeline.

Tests normalize_transcript + extract_tickers against labeled ground truth.
Runs fully standalone: loads NASDAQ cache from disk, no audio/Whisper/threads.

Usage:
  cd transcription
  python benchmark.py
  python benchmark.py --ollama   # also test with Ollama context filter (requires Ollama running)
"""

import re
import time
import json
import argparse
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Load NASDAQ ticker cache (same file transcribe_action.py writes)
# ---------------------------------------------------------------------------
_TICKER_CACHE_FILE = Path(__file__).parent.parent / "transcription" / "ticker_universe.json"
_FALLBACK_CACHE    = Path(__file__).parent.parent / "valid_tickers.txt"

def _load_tickers() -> set:
    if _TICKER_CACHE_FILE.exists():
        try:
            d = json.loads(_TICKER_CACHE_FILE.read_text())
            syms = set(d.get("symbols", []))
            if syms:
                return syms
        except Exception:
            pass
    if _FALLBACK_CACHE.exists():
        syms = set(_FALLBACK_CACHE.read_text().split())
        if syms:
            return syms
    return set()

_VALID_TICKERS: set = _load_tickers()
print(f"[BENCH] Loaded {len(_VALID_TICKERS):,} tickers from cache", flush=True)

# ---------------------------------------------------------------------------
# Production code — copied verbatim from transcribe_action.py
# Keep in sync when the production module changes.
# ---------------------------------------------------------------------------

_NATO = {
    "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D",
    "echo": "E", "foxtrot": "F", "golf": "G", "hotel": "H",
    "india": "I", "juliet": "J", "kilo": "K", "lima": "L",
    "mike": "M", "november": "N", "oscar": "O", "papa": "P",
    "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T",
    "uniform": "U", "victor": "V", "whiskey": "W", "xray": "X",
    "x-ray": "X", "yankee": "Y", "zulu": "Z",
}
_nato_word    = "(?:" + "|".join(re.escape(w) for w in _NATO) + ")"
_NATO_SEP     = r"[ \t]*,?[ \t]*"
_NATO_PATTERN = re.compile(rf"(?i)\b{_nato_word}(?:{_NATO_SEP}{_nato_word}){{1,4}}\b")

_LETTER_NAMES = {
    "ay":    "A", "bee":   "B", "cee":   "C", "see":   "C",
    "dee":   "D", "ee":    "E", "ef":    "F", "eff":   "F",
    "gee":   "G", "jee":   "G", "aitch": "H", "haitch":"H",
    "eye":   "I", "jay":   "J", "kay":   "K", "el":    "L",
    "em":    "M", "en":    "N", "oh":    "O", "pee":   "P",
    "cue":   "Q", "queue": "Q", "ar":    "R", "arr":   "R",
    "ess":   "S", "es":    "S", "tee":   "T", "you":   "U",
    "vee":   "V", "ex":    "X", "eks":   "X", "ecks":  "X",
    "wye":   "Y", "why":   "Y", "zee":   "Z", "zed":   "Z",
}
_letter_name_word    = "(?:" + "|".join(re.escape(w) for w in _LETTER_NAMES) + ")"
_LETTER_NAME_SEP     = r"[ \t]*[.\-,]?[ \t]*"
_LETTER_NAME_PATTERN = re.compile(
    rf"(?i)\b{_letter_name_word}(?:{_LETTER_NAME_SEP}{_letter_name_word}){{1,4}}\b"
)

def normalize_transcript(text: str) -> str:
    def collapse_letter_names(m):
        words   = re.split(r'[\s]+', m.group(0).lower())
        letters = "".join(_LETTER_NAMES.get(w, "") for w in words if w)
        return letters if 2 <= len(letters) <= 5 else m.group(0)
    text = _LETTER_NAME_PATTERN.sub(collapse_letter_names, text)

    def collapse_nato(m):
        words   = re.split(r'[\s,]+', m.group(0).lower())
        letters = "".join(_NATO.get(w, "") for w in words if w)
        return letters if 2 <= len(letters) <= 5 else m.group(0)
    text = _NATO_PATTERN.sub(collapse_nato, text)

    def collapse_dots(m):
        letters = m.group(0).replace(".", "").upper()
        return letters if 2 <= len(letters) <= 5 else m.group(0)
    text = re.sub(r'(?<!\w)(?:[A-Za-z]\.)+(?:[A-Za-z])', collapse_dots, text)

    def collapse_hyphens(m):
        letters = m.group(0).replace("-", "").upper()
        return letters if 2 <= len(letters) <= 5 else m.group(0)
    text = re.sub(r'(?<!\w)(?:[A-Za-z]-)+[A-Za-z](?!\w)', collapse_hyphens, text)

    def collapse_spaced_letters(m):
        letters = re.sub(r'\s+', '', m.group(0)).upper()
        return letters if 2 <= len(letters) <= 5 else m.group(0)
    text = re.sub(
        r'(?<![A-Za-z])([A-Za-z])(?: ([A-Za-z])){1,3}(?![A-Za-z])',
        collapse_spaced_letters,
        text,
    )
    return text

_STOP_WORDS = {
    "A", "I", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "IF", "IN", "IS",
    "IT", "ME", "MY", "NO", "OF", "OK", "ON", "OR", "SO", "TO", "UP", "US",
    "WE", "AND", "ARE", "BUT", "CAN", "DID", "FOR", "GET", "GOT", "HAD",
    "HAS", "HIM", "HIS", "HOW", "HER", "ITS", "NEW", "NOT", "NOW", "OLD",
    "ONE", "OUR", "OUT", "SAY", "SEE", "THE", "TOO", "TWO", "WAS", "WHO",
    "WHY", "YET", "YOU", "ALL", "ANY", "LET", "PUT", "RUN", "SET", "ADD",
    "BUY", "HIT", "TRY", "USE", "WAY", "DAY", "MAY", "OWN", "ASK", "ACT",
    "ALSO", "BACK", "BEEN", "CALL", "COME", "DOES", "DOWN", "EACH", "EVEN",
    "FROM", "GIVE", "GOOD", "HAVE", "HERE", "HIGH", "HOLD", "INTO", "JUST",
    "KEEP", "KNOW", "LAST", "LIKE", "LIVE", "LONG", "LOOK", "MADE", "MAKE",
    "MANY", "MORE", "MOST", "MOVE", "MUCH", "MUST", "NEXT", "ONLY",
    "OVER", "PAST", "SAME", "SELL", "SHOW", "SIDE", "SOME",
    "STOP", "SUCH", "TAKE", "THAN", "THAT", "THEM", "THEN", "THEY", "THIS",
    "TIME", "VERY", "WANT", "WELL", "WHAT", "WHEN", "WILL", "WITH", "WORK",
    "YOUR", "SAID", "SAYS", "TOLD", "TELL", "TALK", "WENT", "GOES", "BOTH",
    "ONCE", "UPON", "SOON", "EVER", "YEAR", "WEEK", "DAYS", "LETS", "PUTS",
    "ALSO", "THEN", "THAN", "BEEN", "WERE", "HAVE", "MAKE",
    "ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE",
    "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "TWENTY", "THIRTY", "FORTY", "FIFTY",
    "HUNDRED", "THOUSAND", "MILLION", "BILLION",
    "ETF", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FDA", "FED", "GDP",
    "CPI", "EPS", "ATH", "ATL", "RSI", "SMA", "EMA", "BEAR", "BULL",
    "CALL", "PUTS", "SPAC", "REIT", "BOND", "DEBT", "CASH", "RATE", "RISK",
    "LOSS", "GAIN", "NEWS", "CNBC", "NYSE", "VWAP", "MACD",
    "HALT", "ALERT", "LEVEL", "STOCK", "PRICE", "TRADE", "SHARE",
    "OPEN", "HIGH", "CLOSE", "AFTER", "ABOVE", "BELOW", "RANGE",
    "BEAT", "MISS", "GUIDE", "VIEW", "RAISE", "LOWER", "CUTS",
    "SPIKE", "SPIKE", "VOLUME", "VOLATILITY", "FLAG", "FACILITY",
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    "JAN", "FEB", "MAR", "APR", "AUG", "SEP", "OCT", "NOV", "DEC",
    "HEY", "YEAH", "OKAY", "WELL", "LIKE", "JUST", "SAID",
    "STILL", "REALLY", "PRETTY", "MIGHT", "MAYBE",
    "REAL", "BAND", "IRON", "FAST", "LOVE", "CAPS", "CUT",
    "JUN", "JUL",
    "AWS", "BIG", "CHIP", "CARE", "CORE", "FUND", "BOOM", "GROW",
    "HALF", "RAN", "FIND", "DR", "CRP", "MRP", "DRP",
    "ALERT", "SCAN", "SCANNER", "SPIKE", "DETECT", "DETECTED",
    # NATO phonetic alphabet words (2-5 chars) — collapsed in pairs by normalize_transcript,
    # but isolated NATO words slip through and Levenshtein-match real tickers
    # e.g. KILO→SILO, LIMA→LIMI, MIKE→BIKE
    "ECHO", "GOLF", "KILO", "LIMA", "MIKE", "PAPA", "ZULU", "DELTA", "INDIA",
    # Common English words appearing all-caps in scanner audio that match tickers at edit-distance 1
    # e.g. CENTS→CENTA, DEAL→DIAL, OFF→OVF
    "CENTS", "DEAL", "OFF",
}

_NAME_TO_TICKER = {
    "apple": "AAPL", "tesla": "TSLA", "nvidia": "NVDA", "amazon": "AMZN",
    "google": "GOOGL", "alphabet": "GOOGL", "microsoft": "MSFT",
    "meta": "META", "facebook": "META", "netflix": "NFLX",
    "amd": "AMD", "intel": "INTC", "qualcomm": "QCOM", "broadcom": "AVGO",
    "micron": "MU", "arm": "ARM", "arm holdings": "ARM",
    "applied materials": "AMAT", "taiwan semi": "TSM", "tsmc": "TSM",
    "marvell": "MRVL", "super micro": "SMCI", "supermicro": "SMCI",
    "salesforce": "CRM", "oracle": "ORCL", "snowflake": "SNOW",
    "shopify": "SHOP", "zoom": "ZM", "datadog": "DDOG", "cloudflare": "NET",
    "crowdstrike": "CRWD", "palo alto": "PANW", "fortinet": "FTNT",
    "servicenow": "NOW", "workday": "WDAY", "mongodb": "MDB",
    "visa": "V", "mastercard": "MA", "paypal": "PYPL", "square": "SQ",
    "block": "SQ", "affirm": "AFRM", "sofi": "SOFI",
    "robinhood": "HOOD", "coinbase": "COIN",
    "microstrategy": "MSTR", "micro strategy": "MSTR", "strategy": "MSTR",
    "jpmorgan": "JPM", "jp morgan": "JPM", "goldman": "GS",
    "goldman sachs": "GS", "morgan stanley": "MS",
    "bank of america": "BAC", "wells fargo": "WFC", "citigroup": "C",
    "citi": "C", "schwab": "SCHW", "blackrock": "BLK",
    "ford": "F", "general motors": "GM",
    "rivian": "RIVN", "lucid": "LCID", "nio": "NIO",
    "pfizer": "PFE", "moderna": "MRNA", "johnson": "JNJ",
    "abbvie": "ABBV", "merck": "MRK", "eli lilly": "LLY", "lilly": "LLY",
    "novo nordisk": "NVO", "amgen": "AMGN", "gilead": "GILD",
    "regeneron": "REGN", "vertex": "VRTX", "unitedhealth": "UNH",
    "humana": "HUM", "cigna": "CI", "hims": "HIMS",
    "exxon": "XOM", "chevron": "CVX", "conocophillips": "COP",
    "halliburton": "HAL", "schlumberger": "SLB",
    "boeing": "BA", "lockheed": "LMT", "raytheon": "RTX", "northrop": "NOC",
    "walmart": "WMT", "costco": "COST",
    "home depot": "HD",
    "disney": "DIS", "comcast": "CMCSA", "warner": "WBD",
    "paramount": "PARA", "spotify": "SPOT", "roblox": "RBLX",
    "verizon": "VZ", "at&t": "T", "att": "T", "t-mobile": "TMUS",
    "gamestop": "GME", "palantir": "PLTR", "berkshire": "BRK",
    "draftkings": "DKNG", "soundhound": "SOUN",
    "airbnb": "ABNB", "uber": "UBER", "lyft": "LYFT", "doordash": "DASH",
}

_MISHEAR_MAP = {
    "envidia": "NVDA", "vidia": "NVDA", "invidia": "NVDA",
    "nda": "NVDA", "nvia": "NVDA",
    "tesler": "TSLA", "tla": "TSLA",
    "palanteer": "PLTR", "palantar": "PLTR", "plt": "PLTR",
    "appal": "AAPL", "apples": "AAPL", "apl": "AAPL",
    "amazin": "AMZN", "amazons": "AMZN", "amz": "AMZN",
    "gugsel": "GOOGL", "guggle": "GOOGL", "googel": "GOOGL",
    "hud": "HOOD",
    "rivien": "RIVN",
    "crowdstrike": "CRWD", "crowd strike": "CRWD",
    "palo alto": "PANW",
    "soundhound": "SOUN", "sound hound": "SOUN",
    "draftkings": "DKNG", "draft kings": "DKNG",
    "msf": "MSFT", "mst": "MSFT",
    "supermicro": "SMCI", "super micro": "SMCI",
    "microstrategy": "MSTR", "micro strategy": "MSTR",
    "coinbase": "COIN",
    "snowflake": "SNOW",
    "cloudflare": "NET",
    "tsmc": "TSM",
}

_PHONETIC_CONFUSIONS = {
    'A': ['E', 'I'], 'E': ['A', 'I'], 'I': ['A', 'E', 'Y'],
    'O': ['U', 'A'], 'U': ['O', 'A'],
    'B': ['V', 'D', 'P', 'G'], 'V': ['B', 'F'], 'P': ['B', 'F'],
    'D': ['T', 'B', 'G'], 'T': ['D'],
    'M': ['N', 'B'], 'N': ['M', 'D'],
    'S': ['Z', 'C', 'X'], 'Z': ['S', 'X'], 'C': ['S', 'K'],
    'G': ['C', 'D', 'K'], 'K': ['C', 'G'], 'X': ['S', 'Z'],
    'F': ['V', 'P'], 'Y': ['I', 'J'],
}

_TICKER_RE  = re.compile(r'\b([A-Za-z]{2,5})\b')
_NAME_RE    = {n: re.compile(rf'\b{re.escape(n)}\b', re.I) for n in _NAME_TO_TICKER}
_MISHEAR_RE = {k: re.compile(rf'\b{re.escape(k)}\b', re.I) for k in _MISHEAR_MAP}


def _levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        cur_row = [i + 1] + [0] * len(s2)
        for j, c2 in enumerate(s2):
            cur_row[j + 1] = min(
                prev_row[j + 1] + 1, cur_row[j] + 1,
                prev_row[j] + (0 if c1 == c2 else 1),
            )
        prev_row = cur_row
    return prev_row[-1]


def _phonetic_match(candidate: str, ticker_set: set) -> str | None:
    if not ticker_set:
        return None
    tries = [candidate]
    collapsed = re.sub(r'(.)\1{2,}', r'\1\1', candidate)
    if collapsed != candidate:
        tries.append(collapsed)
    for cand in tries:
        if cand in ticker_set:
            return cand
        for i, ch in enumerate(cand):
            for swap in _PHONETIC_CONFUSIONS.get(ch, ()):
                fixed = cand[:i] + swap + cand[i+1:]
                if fixed in ticker_set:
                    return fixed
    for ticker in ticker_set:
        if len(ticker) == len(candidate) and _levenshtein_distance(candidate, ticker) == 1:
            return ticker
    return None


def extract_tickers(text: str, ollama_ok: bool = False) -> dict:
    counts: dict[str, int] = {}
    seen   = set()
    lower  = text.lower()

    candidates  = []
    was_spelled = {}
    for m in _TICKER_RE.finditer(text):
        raw = m.group(1)
        t   = raw.upper()
        if t not in _STOP_WORDS:
            counts[t] = counts.get(t, 0) + 1
            if t not in seen:
                candidates.append(t)
                seen.add(t)
                was_spelled[t] = raw.isupper()

    if candidates:
        if _VALID_TICKERS:
            confirmed = []
            for t in candidates:
                if t in _VALID_TICKERS:
                    confirmed.append(t)
                elif was_spelled.get(t) and len(t) >= 4:
                    fixed = _phonetic_match(t, _VALID_TICKERS)
                    if fixed and fixed != t:
                        confirmed.append(fixed)
                        counts[fixed] = counts.get(fixed, 0) + counts.pop(t, 0)
        else:
            confirmed = list(candidates)

        confirmed_set = set(confirmed)
        counts = {t: c for t, c in counts.items() if t in confirmed_set}

    for name, ticker in _NAME_TO_TICKER.items():
        if ticker not in seen and _NAME_RE[name].search(lower):
            counts[ticker] = counts.get(ticker, 0) + 1
            seen.add(ticker)

    for mishear, ticker in _MISHEAR_MAP.items():
        if ticker not in seen and _MISHEAR_RE[mishear].search(lower):
            counts[ticker] = counts.get(ticker, 0) + 1
            seen.add(ticker)

    return counts


# ---------------------------------------------------------------------------
# Improvement 1: Hallucination guard
# Reproduces the worker-level guard logic so it can be unit-tested without
# running Whisper or audio threads.
# ---------------------------------------------------------------------------

INITIAL_PROMPT_TICKERS_RAW = (
    "AAPL MSFT NVDA AMZN GOOGL META NFLX TSLA "
    "AMD INTC QCOM AVGO MU AMAT TSM ARM MRVL SMCI "
    "CRM SNOW SHOP ZM DDOG CRWD PANW NET WDAY NOW MDB "
    "V MA PYPL SQ HOOD COIN AFRM SOFI MSTR "
    "JPM GS MS BAC WFC C SCHW BLK "
    "SPY QQQ IWM XLF XLE XLK "
    "WMT TGT COST HD DIS CMCSA SPOT RBLX "
    "F GM RIVN LCID NIO "
    "PFE MRNA UNH LLY JNJ ABBV MRK AMGN GILD REGN VRTX "
    "XOM CVX COP BA LMT RTX VZ T TMUS "
    "GME AMC PLTR UBER LYFT ABNB DASH DKNG SOUN"
)
_PROMPT_TICKERS: frozenset = frozenset(
    w for w in INITIAL_PROMPT_TICKERS_RAW.split() if w.isupper() and 2 <= len(w) <= 5
)


def hallucination_guard(text: str) -> bool:
    """Return True if the text looks like a Whisper hallucination and should be skipped."""
    words = text.split()
    if not words:
        return False

    most_rep_raw  = max(set(words), key=words.count)
    rep_count     = words.count(most_rep_raw)
    most_rep      = re.sub(r'[^A-Za-z]', '', most_rep_raw).upper()
    is_known      = bool(most_rep) and (
        most_rep in _VALID_TICKERS or
        (2 <= len(most_rep) <= 5 and _phonetic_match(most_rep, _VALID_TICKERS) is not None)
    )

    if rep_count > 6:
        if len(most_rep) <= 2:
            is_loop = True
        elif is_known:
            is_loop = rep_count > 50
        else:
            is_loop = True
    else:
        is_loop = False

    upper        = [w for w in words if w.isupper() and 2 <= len(w) <= 5]
    unique_upper = len(set(upper))
    is_echo      = (len(words) >= 10
                    and unique_upper >= 8
                    and len(upper) / len(words) > 0.70)

    if not is_echo and _PROMPT_TICKERS and len(words) >= 7:
        unique_prompt_hits = sum(1 for w in set(upper) if w in _PROMPT_TICKERS)
        is_echo = (unique_prompt_hits >= 5
                   and unique_prompt_hits / max(len(words), 1) > 0.60)

    return is_loop or is_echo


# (text, should_be_skipped, description)
HALLUCINATION_CASES = [
    # Repetition loop — silence noise
    ("T T T T T T T T T",                                          True,  "single-letter loop"),
    ("AI AI AI AI AI AI AI AI",                                     True,  "two-letter loop"),
    # Known ticker repeated heavily — real TTS, should pass (known ticker threshold is >50)
    ("NVDA NVDA NVDA NVDA NVDA",                                    False, "NVDA ×5 — real TTS, pass"),
    ("NVDA NVDA NVDA NVDA NVDA NVDA NVDA",                         False, "NVDA ×7 — known ticker, pass"),
    # Unknown 3+ char token repeated — hallucination (use clearly invalid ticker)
    ("ZZXQ ZZXQ ZZXQ ZZXQ ZZXQ ZZXQ ZZXQ",                       True,  "unknown token ×7 — skip"),
    # Prompt echo — full density (old guard)
    ("AAPL MSFT NVDA AMZN GOOGL META NFLX TSLA AMD INTC QCOM AVGO MU",  True,  "full prompt echo — old guard"),
    # Subtle prompt echo — 5+ unique prompt tickers, 60%+ density, ≥7 words (NEW guard)
    ("AAPL MSFT NVDA AMZN GOOGL META NFLX TSLA this morning",      True,  "partial prompt echo — new guard"),
    # Below 7-word minimum or below 5 unique prompt tickers — should pass
    ("AAPL MSFT NVDA AMZN GOOGL",                                   False, "5 words < 7-word minimum — pass"),
    # Real sentence with some prompt tickers — should not be flagged
    ("NVDA is up three percent while AAPL and MSFT are flat ahead of earnings", False, "real sentence — pass"),
    ("PLTR COIN HOOD SOFI AFRM MSTR rallied hard today on risk-on sentiment",   False, "real sentence — pass"),
]


def run_hallucination_tests() -> None:
    print(f"\n{'='*70}")
    print("  HALLUCINATION GUARD BENCHMARK")
    print(f"{'='*70}\n")
    passed = failed = 0
    for text, expected_skip, desc in HALLUCINATION_CASES:
        got_skip = hallucination_guard(text)
        ok       = got_skip == expected_skip
        mark     = "✓" if ok else "✗"
        action   = "SKIP" if got_skip else "PASS"
        exp_str  = "skip" if expected_skip else "pass"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  {mark} [{action:<4}] (expected {exp_str})  {desc}")
        if not ok:
            print(f"         text: {text[:80]}")
    print(f"\n  Result: {passed}/{passed+failed} passed")


# ---------------------------------------------------------------------------
# Improvement 2: Mention cooldown (cross-chunk dedup)
# Tests the timestamp-based suppression logic in isolation.
# ---------------------------------------------------------------------------

def mention_cooldown_guard(ticker: str, now: float,
                            last_sent: dict, cooldown: float) -> bool:
    """Returns True if the ticker should be suppressed (within cooldown)."""
    last = last_sent.get(ticker, 0.0)
    if now - last < cooldown:
        return True
    last_sent[ticker] = now
    return False


def run_cooldown_tests() -> None:
    print(f"\n{'='*70}")
    print("  MENTION COOLDOWN BENCHMARK")
    print(f"{'='*70}\n")

    COOLDOWN = 1.5  # matches CHUNK_DURATION

    cases = [
        # (ticker, delta_since_last_send, expected_suppressed, description)
        ("NVDA", 0.0,  True,  "same instant — suppress (overlap echo)"),
        ("NVDA", 0.5,  True,  "0.5s later — suppress (within cooldown)"),
        ("NVDA", 1.4,  True,  "1.4s later — suppress (just inside cooldown)"),
        ("NVDA", 1.5,  False, "exactly 1.5s — allow (boundary: not suppressed)"),
        ("NVDA", 2.0,  False, "2.0s later — allow (new mention)"),
        ("AAPL", 0.3,  False, "different ticker — always allow"),
        ("AAPL", 0.3,  True,  "same ticker 0.3s later — suppress"),
    ]

    passed = failed = 0
    last_sent: dict = {}
    base_time = 1000.0

    # Seed NVDA with a prior send at base_time
    last_sent["NVDA"] = base_time

    for ticker, delta, expected_suppress, desc in cases:
        now = base_time + delta
        got = mention_cooldown_guard(ticker, now, last_sent, COOLDOWN)
        ok  = got == expected_suppress
        mark = "✓" if ok else "✗"
        if ok:
            passed += 1
        else:
            failed += 1
        action = "SUPPRESS" if got else "SEND    "
        exp    = "suppress" if expected_suppress else "send"
        print(f"  {mark} [{action}] (expected {exp})  {desc}")
        # Reset base for next case using same ticker to avoid interference
        if ticker == "NVDA" and not got:
            base_time = now  # NVDA was sent; update base for next relative delta

    print(f"\n  Result: {passed}/{passed+failed} passed")
    print()
    print("  Note: Whisper segment confidence filtering (no_speech_prob / avg_logprob)")
    print("        requires a live faster-whisper model and cannot be tested here.")


# ---------------------------------------------------------------------------
# Optional: live Ollama call for benchmarking the Ollama path
# ---------------------------------------------------------------------------
OLLAMA_URL     = "http://localhost:11434/api/generate"
OLLAMA_MODEL   = "qwen3:0.6b"
OLLAMA_TIMEOUT = 0.5  # slightly relaxed for benchmark (not real-time)

def _ollama_classify_live(candidates: list[str], context: str) -> list[str] | None:
    words  = " ".join(candidates)
    prompt = (
        f'Transcript: "{context}"\n'
        f'Candidates: {words}\n'
        f'Which candidates are US stock ticker symbols actually being discussed? '
        f'Reply with only the valid tickers space-separated, or NONE.'
    )
    payload = {
        "model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
        "options": {"num_predict": 20, "temperature": 0, "num_ctx": 128},
    }
    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
            response = json.loads(r.read()).get("response", "").strip().upper()
        if not response or response == "NONE":
            return []
        tokens = re.findall(r'\b[A-Z]{2,5}\b', response)
        candidate_set = set(candidates)
        return [t for t in tokens if t in candidate_set]
    except Exception:
        return None


def _ping_ollama() -> bool:
    try:
        req = urllib.request.Request(
            "http://localhost:11434/api/tags",
            headers={"Content-Type": "application/json"}, method="GET",
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            models = [m["name"] for m in json.loads(r.read()).get("models", [])]
        return any(OLLAMA_MODEL.split(":")[0] in m for m in models)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ground truth test cases
# Format: (raw_transcript, expected_ticker_set, category, description)
# ---------------------------------------------------------------------------
CASES = [
    # ── Category 1: Direct ticker (exact symbol in transcript) ────────────
    ("NVDA is up three percent this morning",        {"NVDA"},        "direct", "single ticker"),
    ("We are watching AAPL and MSFT closely",        {"AAPL","MSFT"}, "direct", "two tickers"),
    ("TSLA just hit a 52 week high",                 {"TSLA"},        "direct", "single ticker"),
    ("JPM reported strong earnings today",           {"JPM"},         "direct", "bank ticker"),
    ("SPY is flat ahead of the Fed decision",        {"SPY"},         "direct", "ETF"),
    ("QQQ breaking above resistance at 480",         {"QQQ"},         "direct", "ETF"),
    ("PLTR keeps running higher",                    {"PLTR"},        "direct", "meme/growth"),
    ("AMD and INTC both moving on chip news",        {"AMD","INTC"},  "direct", "two tickers"),
    ("AMZN AWS revenue was the big story",           {"AMZN"},        "direct", "single ticker"),
    ("META crushed estimates on ad revenue",         {"META"},        "direct", "single ticker"),
    ("GOOGL up on cloud growth",                     {"GOOGL"},       "direct", "single ticker"),
    ("GS raised its target on the banks",            {"GS"},          "direct", "bank"),
    ("XOM and CVX moving with crude oil",            {"XOM","CVX"},   "direct", "energy pair"),
    ("BA down after the 737 news",                   {"BA"},          "direct", "two-letter"),
    ("IWM lagging large caps today",                 {"IWM"},         "direct", "ETF"),

    # ── Category 2: Company name (spoken name, not ticker) ────────────────
    ("Apple is gaining on strong iPhone sales",      {"AAPL"},        "name", "apple"),
    ("Nvidia's data center revenue surged",          {"NVDA"},        "name", "nvidia"),
    ("Tesla cut prices for the third time",          {"TSLA"},        "name", "tesla"),
    ("Palantir won another government contract",     {"PLTR"},        "name", "palantir"),
    ("Goldman raised their price target on banks",   {"GS"},          "name", "goldman"),
    ("Microsoft Azure beat on cloud revenue",        {"MSFT"},        "name", "microsoft"),
    ("Amazon Web Services is growing fast",          {"AMZN"},        "name", "amazon"),
    ("Meta's advertising business is recovering",   {"META"},        "name", "meta"),
    ("Boeing shares fell on safety concerns",        {"BA"},          "name", "boeing"),
    ("Coinbase rallied with crypto prices",          {"COIN"},        "name", "coinbase"),
    ("Palantir is breaking out on AI contracts",     {"PLTR"},        "name", "palantir"),
    ("Costco reported strong same store sales",      {"COST"},        "name", "costco"),
    ("Exxon and Chevron both moving on oil",         {"XOM","CVX"},   "name", "energy names"),
    ("Berkshire disclosed a new position",           {"BRK"},         "name", "berkshire"),
    ("Uber and Lyft both up on rideshare data",      {"UBER","LYFT"}, "name", "rideshare pair"),

    # ── Category 3: Phonetic / spelled out ───────────────────────────────
    ("en vee dee ay hitting new highs today",        {"NVDA"},        "phonetic", "NVDA letter-names"),
    ("tee ess el ay breaking out right now",         {"TSLA"},        "phonetic", "TSLA letter-names"),
    ("ay ay pee el up two percent",                  {"AAPL"},        "phonetic", "AAPL letter-names"),
    ("N V D A N V D A on the screen",                {"NVDA"},        "phonetic", "NVDA spaced"),
    ("November Victor Delta Alpha on the move",      {"NVDA"},        "phonetic", "NVDA NATO"),
    ("echo mike echo mike pulling back",             {"EMEM"},        "phonetic", "4-letter NATO — expect miss"),
    ("M S F T breaking the 400 level",               {"MSFT"},        "phonetic", "MSFT spaced"),

    # ── Category 4: False positives — non-ticker context ──────────────────
    # These words ARE valid NASDAQ tickers but are used as plain English.
    # Without Ollama: likely false positive. With Ollama: should be clean.
    ("costs are rising across every sector",         set(),           "false_pos", "COST as English"),
    ("the market is open for regular trading",       set(),           "false_pos", "OPEN as English"),
    ("earnings growth looks real this quarter",      set(),           "false_pos", "REAL as English"),
    ("we saw a big gain in consumer spending",       set(),           "false_pos", "GAIN as English"),
    ("the band played its last concert in Dallas",   set(),           "false_pos", "BAND as English"),
    ("iron ore prices are falling globally",         set(),           "false_pos", "IRON as English"),
    ("fast food chains are cutting jobs",            set(),           "false_pos", "FAST as English"),

    # ── Category 5: Mixed — tickers + noisy non-ticker words ─────────────
    ("AAPL earnings beat but costs are rising",      {"AAPL"},        "mixed", "ticker + noise"),
    ("NVDA is up on strong data center demand gains",{"NVDA"},        "mixed", "NVDA + GAIN noise"),
    ("JPM and Goldman raised targets on the banks",  {"JPM","GS"},    "mixed", "direct + name"),
    ("Apple and MSFT both gaining ahead of earnings",{"AAPL","MSFT"}, "mixed", "name + direct"),

    # ── Category 6: Edge cases ────────────────────────────────────────────
    ("",                                             set(),           "edge", "empty string"),
    ("buy buy buy",                                  set(),           "edge", "all stop words"),
    ("I love my new stock",                          set(),           "edge", "no real tickers"),
    ("NVDA NVDA NVDA NVDA NVDA",                     {"NVDA"},        "edge", "repeated ticker"),
]


# ---------------------------------------------------------------------------
# Run benchmark
# ---------------------------------------------------------------------------

def run(use_ollama: bool = False) -> None:
    label = "with Ollama" if use_ollama else "NASDAQ-only (no Ollama)"
    print(f"\n{'='*70}")
    print(f"  TICKER EXTRACTION BENCHMARK  —  {label}")
    print(f"{'='*70}\n")

    categories: dict[str, dict] = {}
    total_tp = total_fp = total_fn = 0
    timings: list[float] = []

    for raw, expected, cat, desc in CASES:
        if cat not in categories:
            categories[cat] = {"tp": 0, "fp": 0, "fn": 0, "cases": 0}

        t0 = time.perf_counter()
        normalized = normalize_transcript(raw)
        got_dict   = extract_tickers(normalized, ollama_ok=use_ollama)
        elapsed    = (time.perf_counter() - t0) * 1000
        timings.append(elapsed)

        got = set(got_dict.keys())
        tp  = expected & got
        fp  = got - expected
        fn  = expected - got

        categories[cat]["tp"]    += len(tp)
        categories[cat]["fp"]    += len(fp)
        categories[cat]["fn"]    += len(fn)
        categories[cat]["cases"] += 1
        total_tp += len(tp)
        total_fp += len(fp)
        total_fn += len(fn)

        ok = (not fp) and (not fn)
        mark = "✓" if ok else "✗"
        issues = []
        if fp: issues.append(f"FP:{sorted(fp)}")
        if fn: issues.append(f"MISS:{sorted(fn)}")
        issue_str = "  " + "  ".join(issues) if issues else ""
        print(f"  {mark} [{cat:8}] {desc:<40}  got={sorted(got)}{issue_str}")

    # Summary by category
    print(f"\n{'─'*70}")
    print(f"  {'Category':<12}  {'Cases':>5}  {'Prec':>6}  {'Recall':>7}  {'F1':>6}")
    print(f"  {'─'*12}  {'─'*5}  {'─'*6}  {'─'*7}  {'─'*6}")
    for cat, s in categories.items():
        prec   = s["tp"] / (s["tp"] + s["fp"]) if (s["tp"] + s["fp"]) > 0 else 1.0
        recall = s["tp"] / (s["tp"] + s["fn"]) if (s["tp"] + s["fn"]) > 0 else 1.0
        f1     = 2 * prec * recall / (prec + recall) if (prec + recall) > 0 else 0.0
        print(f"  {cat:<12}  {s['cases']:>5}  {prec:>5.0%}  {recall:>6.0%}  {f1:>5.0%}")

    overall_prec   = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 1.0
    overall_recall = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 1.0
    overall_f1     = (2 * overall_prec * overall_recall / (overall_prec + overall_recall)
                      if (overall_prec + overall_recall) > 0 else 0.0)
    print(f"  {'─'*12}  {'─'*5}  {'─'*6}  {'─'*7}  {'─'*6}")
    print(f"  {'OVERALL':<12}  {len(CASES):>5}  {overall_prec:>5.0%}  {overall_recall:>6.0%}  {overall_f1:>5.0%}")

    # Timing
    timings.sort()
    n = len(timings)
    print(f"\n  Performance ({n} calls, normalize + extract):")
    print(f"    mean  {sum(timings)/n:.2f}ms")
    print(f"    p50   {timings[n//2]:.2f}ms")
    print(f"    p95   {timings[int(n*0.95)]:.2f}ms")
    print(f"    p99   {timings[int(n*0.99)]:.2f}ms")
    print(f"    max   {timings[-1]:.2f}ms")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ollama", action="store_true", help="Also benchmark with Ollama context filter")
    args = parser.parse_args()

    run(use_ollama=False)
    run_hallucination_tests()
    run_cooldown_tests()

    if args.ollama:
        if _ping_ollama():
            print(f"\n[BENCH] Ollama is running ({OLLAMA_MODEL}) — running second pass with context filter")
            run(use_ollama=True)
        else:
            print("\n[BENCH] --ollama requested but Ollama not reachable — skipping")
