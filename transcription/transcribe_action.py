"""
transcribe_action.py — CNBC audio → stock ticker detection
Apple Silicon: MLX Whisper large-v3-turbo (Neural Engine / GPU)
Fallback:      faster-whisper medium.en (CPU)

Pipeline:
  BlackHole 48kHz → resample 3:1 → 16kHz → 3s chunks → Whisper → tickers → dashboard API
"""

import argparse
import json
import os as _os
import sys as _sys
import re
import time
import threading
import contextlib
import urllib.request
import urllib.error
from pathlib import Path
from queue import Queue, Full

import numpy as np
from scipy.signal import resample_poly

# ── Platform audio import ─────────────────────────────────────────────────────
if _sys.platform == "win32":
    import pyaudiowpatch as pyaudio
else:
    import pyaudio

# ── ASR engine selection ──────────────────────────────────────────────────────
# On Apple Silicon, MLX Whisper uses the GPU / Neural Engine — far faster than CPU.
# We import both at the top so there are no surprise NameErrors later.

_mlx_whisper = None
_USE_MLX = False

if _sys.platform == "darwin":
    try:
        import mlx_whisper as _mlx_whisper
        _USE_MLX = True
        print("[ENGINE] MLX Whisper — Apple Silicon GPU/Neural Engine", flush=True)
    except ImportError:
        print("[ENGINE] mlx_whisper not found — falling back to faster-whisper CPU", flush=True)

_fw_model = None   # faster-whisper WhisperModel, loaded in _init_asr()
if not _USE_MLX:
    try:
        from faster_whisper import WhisperModel as _WhisperModel
    except ImportError:
        print("[ERROR] Neither mlx_whisper nor faster_whisper is installed.", flush=True)
        _sys.exit(1)


# =============================================================================
# AUDIO CONFIG
# =============================================================================

# Mac: BlackHole loopback runs at 48kHz. Whisper needs 16kHz. 48/3 = 16 exactly.
# Windows: WASAPI loopback typically at 44100Hz.
if _sys.platform == "darwin":
    SAMPLE_RATE       = 48000
    RESAMPLE_UP       = 1
    RESAMPLE_DOWN     = 3
    SILENCE_THRESHOLD = 0.0002   # BlackHole signal is quiet; err on the side of capture
    GAIN_TARGET_RMS   = 0.10     # scale up quiet loopback signal
    GAIN_MAX          = 25.0
else:
    SAMPLE_RATE       = 44100
    RESAMPLE_UP       = 160
    RESAMPLE_DOWN     = 441
    SILENCE_THRESHOLD = 0.008
    GAIN_TARGET_RMS   = None
    GAIN_MAX          = None

TARGET_SR       = 16000
CHUNK_DURATION  = 1.5    # shorter chunks = faster latency (1.5s window)
OVERLAP         = 0.5    # 0.5s overlap: boundary protection without 3× count inflation
CHUNK_SAMPLES   = int(TARGET_SR * CHUNK_DURATION)
OVERLAP_SAMPLES = int(TARGET_SR * OVERLAP)
ADVANCE_SAMPLES = CHUNK_SAMPLES - OVERLAP_SAMPLES  # 1.5s of new audio per result
READ_FRAMES     = int(SAMPLE_RATE * 0.10)   # 100ms reads — fills buffer quickly


# =============================================================================
# MODEL CONFIG
# =============================================================================

# whisper-large-v3-turbo: same accuracy as large-v3, ~8x faster decoder.
# On Apple Silicon M-series it processes 3s audio in ~200-400ms.
MLX_MODEL = "mlx-community/whisper-large-v3-turbo"

# CPU fallback — medium.en is the best balance of speed/accuracy without a GPU
CPU_MODEL = "medium.en"
CPU_BEAM  = 5


# =============================================================================
# TRANSCRIPT NORMALIZATION
# =============================================================================

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

# English alphabet letter-name phonetics — how TTS voices pronounce individual letters.
# e.g. "en vee dee ay" → NVDA, "tee es el ay" → TSLA, "em ess ef tee" → MSFT
_LETTER_NAMES = {
    "ay":      "A", "bee":    "B", "cee":    "C", "see":   "C",
    "dee":     "D", "ee":     "E", "ef":     "F", "eff":   "F",
    "gee":     "G", "jee":    "G", "aitch":  "H", "haitch":"H",
    "eye":     "I", "jay":    "J", "kay":    "K", "el":    "L",
    "em":      "M", "en":     "N", "oh":     "O", "pee":   "P",
    "cue":     "Q", "queue":  "Q", "ar":     "R", "arr":   "R",
    "ess":     "S", "es":     "S", "tee":    "T", "you":   "U",
    "vee":     "V", "ex":     "X", "eks":    "X", "ecks":  "X", "wye":   "Y", "why":   "Y",
    "zee":     "Z", "zed":    "Z",
}
_letter_name_word    = "(?:" + "|".join(re.escape(w) for w in _LETTER_NAMES) + ")"
_LETTER_NAME_SEP     = r"[ \t]*[.\-,]?[ \t]*"   # space, period, dash, or comma between names
_LETTER_NAME_PATTERN = re.compile(
    rf"(?i)\b{_letter_name_word}(?:{_LETTER_NAME_SEP}{_letter_name_word}){{1,4}}\b"
)


def normalize_transcript(text: str) -> str:
    """Collapse NATO phonetics, letter-name phonetics, dot-notation, hyphen-spelling,
    and space-separated letters into ticker candidates.

    Handles all five forms a computer TTS voice uses to spell out tickers:
      N-V-D-A                    → NVDA  (hyphen-separated)
      N.V.D.A.                   → NVDA  (dot-separated)
      November Victor Delta Alpha → NVDA  (NATO phonetics)
      N V D A                    → NVDA  (space-separated single letters)
      en vee dee ay              → NVDA  (English letter-name phonetics ← TTS default)
    """
    # Letter-name phonetics — run FIRST so "en vee dee ay" → NVDA before other passes
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
        return letters if 3 <= len(letters) <= 5 else m.group(0)

    text = re.sub(r'(?<!\w)(?:[A-Za-z]\.){2,5}', collapse_dots, text)

    def collapse_hyphens(m):
        return m.group(0).replace("-", "").upper()

    text = re.sub(r'(?<!\w)(?:[A-Za-z]-){2,4}[A-Za-z](?!\w)', collapse_hyphens, text)

    # Space-separated single letters: "N V D A" → "NVDA"
    # The computer TTS reads each letter individually with a pause between them.
    # Whisper transcribes this as 2-5 consecutive single-letter tokens separated by spaces.
    # Negative lookbehind/ahead prevents matching inside normal words.
    def collapse_spaced_letters(m):
        letters = re.sub(r'\s+', '', m.group(0)).upper()
        return letters if 2 <= len(letters) <= 5 else m.group(0)

    text = re.sub(
        r'(?<![A-Za-z])([A-Za-z])(?: ([A-Za-z])){1,3}(?![A-Za-z])',
        collapse_spaced_letters,
        text,
    )

    return text


# =============================================================================
# VALID TICKER LIST  (NASDAQ + NYSE, refreshed weekly from nasdaqtrader.com)
# =============================================================================

_TICKER_CACHE_FILE = Path(__file__).parent.parent / "valid_tickers.txt"
_TICKER_CACHE_DAYS = 7


def _load_valid_tickers() -> set:
    """Return ~10k valid US ticker symbols from NASDAQ Trader.
    Downloads on first run (or after cache expires), then reads from local file.
    Falls back to empty set on any error so the transcriber still works offline."""

    def _fetch() -> set:
        import urllib.request as _ur
        tickers = set()
        sources = [
            ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
             lambda line: line.split("|")[0].strip()),
            ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
             lambda line: line.split("|")[0].strip()),
        ]
        for url, extract in sources:
            try:
                req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with _ur.urlopen(req, timeout=10) as r:
                    for raw in r.read().decode("utf-8", errors="ignore").splitlines():
                        sym = extract(raw)
                        if (sym and sym.isalpha() and 1 <= len(sym) <= 5
                                and sym not in ("Symbol", "ACTSymbol")):
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
                    print(f"[TICKERS] Loaded {len(syms):,} valid tickers from cache "
                          f"({age_days:.1f}d old).", flush=True)
                    return syms
        print("[TICKERS] Downloading ticker list from NASDAQ Trader…", flush=True)
        tickers = _fetch()
        if tickers:
            _TICKER_CACHE_FILE.write_text("\n".join(sorted(tickers)))
            print(f"[TICKERS] Downloaded and cached {len(tickers):,} tickers.", flush=True)
        return tickers
    except Exception as e:
        print(f"[TICKERS] Could not load ticker list: {e}", flush=True)
        return set()


_VALID_TICKERS: set = _load_valid_tickers()


# =============================================================================
# OLLAMA LOCAL LLM — context-aware ticker classification
# =============================================================================
# qwen2.5:0.5b is ~400 MB and runs at 300-500 t/s on Apple Silicon M1.
# Install:  brew install ollama && ollama pull qwen2.5:0.5b
# The LLM receives only the short candidate list (≤10 words), not the full
# transcript, so round-trips are consistently under 150ms.

OLLAMA_MODEL       = "qwen3:0.6b"
OLLAMA_URL         = "http://localhost:11434/api/generate"
OLLAMA_TIMEOUT     = 0.25  # reduced from 0.50 for faster latency
OLLAMA_RETRIES     = 0     # no retries — fail fast for speed
OLLAMA_RETRY_SLEEP = 0.10

_ollama_ok      = False  # enabled during accuracy optimization; qwen3:0.6b for phonetic validation
_ollama_fail_ts = 0.0

_METRICS_LOCK = threading.Lock()
_METRICS = {
    "queue_drops": 0,
    "ollama_timeouts": 0,
    "ollama_failures": 0,
    "ollama_retry_ok": 0,
    "ticker_candidates_total": 0,
    "ticker_candidates_validated": 0,
    "ticker_candidates_failed": 0,
}


def _metric_inc(key: str, value: int = 1):
    with _METRICS_LOCK:
        _METRICS[key] = int(_METRICS.get(key, 0)) + value


def _metrics_payload() -> dict:
    with _METRICS_LOCK:
        return {
            "queue_drops": int(_METRICS.get("queue_drops", 0)),
            "ollama_timeouts": int(_METRICS.get("ollama_timeouts", 0)),
            "ollama_failures": int(_METRICS.get("ollama_failures", 0)),
            "ollama_retry_ok": int(_METRICS.get("ollama_retry_ok", 0)),
            "ticker_candidates_total": int(_METRICS.get("ticker_candidates_total", 0)),
            "ticker_candidates_validated": int(_METRICS.get("ticker_candidates_validated", 0)),
            "ticker_candidates_failed": int(_METRICS.get("ticker_candidates_failed", 0)),
            "queue_size": int(_audio_queue.qsize()) if '_audio_queue' in globals() else 0,
            "queue_capacity": 16,
            "workers": int(_N_WORKERS) if '_N_WORKERS' in globals() else 0,
            "ollama_ready": bool(_ollama_ok),
        }


def get_runtime_metrics() -> dict:
    return _metrics_payload()


def _ping_ollama(verbose: bool = True) -> bool:
    global _ollama_ok
    prev = _ollama_ok
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1.0) as r:
            tags = json.loads(r.read())
        models = [m["name"].split(":")[0] for m in tags.get("models", [])]
        _ollama_ok = OLLAMA_MODEL.split(":")[0] in models
        if not _ollama_ok and (verbose or prev != _ollama_ok):
            print(f"[OLLAMA] Model '{OLLAMA_MODEL}' not found. Run: ollama pull {OLLAMA_MODEL}", flush=True)
    except Exception:
        _ollama_ok = False
    if verbose or prev != _ollama_ok:
        status = "ready" if _ollama_ok else "not available"
        print(f"[OLLAMA] {status}", flush=True)
    return _ollama_ok


def _ollama_classify(candidates: list[str], context: str) -> list[str] | None:
    """Return confirmed tickers, [] if Ollama says none, None on failure/timeout."""
    global _ollama_fail_ts
    if not candidates:
        return []
    if time.monotonic() - _ollama_fail_ts < 30:
        return None

    words = " ".join(candidates)
    prompt = (
        f'Transcript: "{context}"\n'
        f'Candidates: {words}\n'
        f'Which candidates are US stock ticker symbols actually being discussed? '
        f'Reply with only the valid tickers space-separated, or NONE.'
    )
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {"num_predict": 20, "temperature": 0, "num_ctx": 128},
    }
    candidate_set = set(candidates)
    attempts = max(1, int(OLLAMA_RETRIES) + 1)

    for attempt in range(attempts):
        try:
            req = urllib.request.Request(
                OLLAMA_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
                response = json.loads(r.read()).get("response", "").strip().upper()
            if attempt > 0:
                _metric_inc("ollama_retry_ok")
            if not response or response == "NONE":
                return []
            tokens = re.findall(r'\b[A-Z]{2,5}\b', response)
            return [t for t in tokens if t in candidate_set]
        except Exception as e:
            is_timeout = isinstance(e, TimeoutError)
            if not is_timeout and isinstance(e, urllib.error.URLError):
                reason = getattr(e, "reason", None)
                is_timeout = isinstance(reason, TimeoutError) or ("timed out" in str(reason).lower())
            if not is_timeout:
                is_timeout = "timed out" in str(e).lower()
            if is_timeout:
                _metric_inc("ollama_timeouts")
            else:
                _metric_inc("ollama_failures")
            if attempt < attempts - 1:
                print(f"[OLLAMA] classify failed attempt {attempt + 1}/{attempts}: {type(e).__name__}", flush=True)
                time.sleep(OLLAMA_RETRY_SLEEP)
                continue
            _ollama_fail_ts = time.monotonic()
            print(f"[OLLAMA] classify unavailable: {type(e).__name__}", flush=True)
            if _ollama_ok:
                _ping_ollama(verbose=False)
            return None


def _ollama_phonetic_validate(candidate: str) -> str | None:
    """Use Ollama to resolve phonetically-misheard tickers.

    When a TTS voice spells a ticker letter-by-letter (e.g., 'N V D A'),
    Whisper sometimes mishears vowels or doubles consonants. Ask Ollama
    to reason about the correct US stock ticker this resembles.

    Returns the corrected ticker if found, or None.
    """
    global _ollama_fail_ts
    if not _ollama_ok or not candidate or len(candidate) > 5:
        return None
    if time.monotonic() - _ollama_fail_ts < 30:
        return None

    prompt = (
        f"Computer voice reading stock ticker letter-by-letter.\n"
        f"ASR heard: {candidate}\n"
        f"What is the correct US stock ticker symbol (2-5 letters)?\n"
        f"Examples: NVDA (N-V-D-A), TSLA (T-S-L-A), AAPL (A-A-P-L)\n"
        f"Reply with ONLY the ticker symbol, or NONE if unclear."
    )
    payload = {
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {"num_predict": 6, "temperature": 0, "num_ctx": 256},
    }

    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as r:
            response = json.loads(r.read()).get("response", "").strip().upper()
        if not response or response == "NONE":
            return None
        match = re.search(r'\b([A-Z]{2,5})\b', response)
        if match and match.group(1) in _VALID_TICKERS:
            return match.group(1)
    except Exception as e:
        is_timeout = "timed out" in str(e).lower() or isinstance(e, TimeoutError)
        if is_timeout:
            _metric_inc("ollama_timeouts")
        else:
            _metric_inc("ollama_failures")
        if not is_timeout:
            _ollama_fail_ts = time.monotonic()
    return None


# =============================================================================
# TICKER EXTRACTION
# =============================================================================

_STOP_WORDS = {
    # Common English
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
    # Numbers spelled out
    "ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE",
    "TEN", "ELEVEN", "TWELVE", "THIRTEEN", "TWENTY", "THIRTY", "FORTY", "FIFTY",
    "HUNDRED", "THOUSAND", "MILLION", "BILLION",
    # Finance / market terms that look like tickers
    "ETF", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FDA", "FED", "GDP",
    "CPI", "EPS", "ATH", "ATL", "RSI", "SMA", "EMA", "BEAR", "BULL",
    "CALL", "PUTS", "SPAC", "REIT", "BOND", "DEBT", "CASH", "RATE", "RISK",
    "LOSS", "GAIN", "NEWS", "CNBC", "NYSE", "VWAP", "MACD",
    "HALT", "ALERT", "LEVEL", "STOCK", "PRICE", "TRADE", "SHARE",
    "OPEN", "HIGH", "CLOSE", "AFTER", "ABOVE", "BELOW", "RANGE",
    "BEAT", "MISS", "GUIDE", "VIEW", "RAISE", "LOWER", "CUTS",
    "SPIKE", "SPIKE", "VOLUME", "VOLATILITY", "FLAG", "FACILITY",
    # Months / days
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    "JAN", "FEB", "MAR", "APR", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
    # Filler / noise
    "HEY", "YEAH", "OKAY", "WELL", "LIKE", "JUST", "SAID",
    "STILL", "REALLY", "PRETTY", "MIGHT", "MAYBE",
    # Common English words that are also valid NASDAQ tickers — almost never
    # the subject of a ticker discussion; Ollama can't filter these fast enough
    "REAL", "BAND", "IRON", "FAST", "LOVE", "CAPS", "CUT",
    # Tech / finance acronyms that shadow common English speech on air
    "AWS", "BIG", "CHIP", "CARE", "CORE", "FUND", "BOOM", "GROW",
    # Common words that Whisper outputs all-caps and then Levenshtein-match tickers
    # e.g. HALF→CALF, RAN→RAA, FIND→FINT, DR→TR, CRP→KRP (fragment of MCRP)
    "HALF", "RAN", "FIND", "DR", "CRP", "MRP", "DRP",
    # Noise fragments from scanner alerts / audio artefacts
    "ALERT", "SCAN", "SCANNER", "SPIKE", "DETECT", "DETECTED",
    # NATO phonetic alphabet words (2-5 chars) — collapsed in pairs by normalize_transcript,
    # but isolated NATO words slip through and Levenshtein-match real tickers
    # e.g. KILO→SILO, LIMA→LIMI, MIKE→BIKE
    "ECHO", "GOLF", "KILO", "LIMA", "MIKE", "PAPA", "ZULU", "DELTA", "INDIA",
    # Common English words appearing all-caps in scanner audio that match tickers at edit-distance 1
    # e.g. CENTS→CENTA, DEAL→DIAL, OFF→OVF
    "CENTS", "DEAL", "OFF",
}

# Company name → ticker (spoken names on air)
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

# Known Whisper mishears → correct ticker
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

_TICKER_RE  = re.compile(r'\b([A-Za-z]{2,5})\b')
_NAME_RE    = {n: re.compile(rf'\b{re.escape(n)}\b', re.I) for n in _NAME_TO_TICKER}
_MISHEAR_RE = {k: re.compile(rf'\b{re.escape(k)}\b', re.I) for k in _MISHEAR_MAP}

# ASR letter-spelling confusions: when a TTS voice spells a ticker letter-by-letter
# Whisper often mishears the vowel ("A" → "E"), drops to wrong consonant pairs, or
# multiplies sustained vowels ("A I I O" → "E-I-I-I-O"). Phonetic-correct unknown
# letter-spelled candidates against the NASDAQ list at edit distance 1.
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


def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        cur_row = [i + 1] + [0] * len(s2)
        for j, c2 in enumerate(s2):
            cur_row[j + 1] = min(
                prev_row[j + 1] + 1,
                cur_row[j] + 1,
                prev_row[j] + (0 if c1 == c2 else 1),
            )
        prev_row = cur_row
    return prev_row[-1]


def _phonetic_match(candidate: str, ticker_set: set) -> str | None:
    """Map an ASR-misheard letter-spelling to a real ticker, or None.

    Tries transformations in order of confidence:
      1. Collapse runs of repeated chars (EIIIO → EIIO).
      2. Single-character substitution from ASR confusion pairs.
      3. Levenshtein distance 1 fallback for unmatched mishears.
    """
    if not ticker_set:
        return None
    tries = [candidate]
    collapsed = re.sub(r'(.)\1{2,}', r'\1\1', candidate)
    if collapsed != candidate:
        tries.append(collapsed)

    # Try exact/confusion matches first
    for cand in tries:
        if cand in ticker_set:
            return cand
        for i, ch in enumerate(cand):
            for swap in _PHONETIC_CONFUSIONS.get(ch, ()):
                fixed = cand[:i] + swap + cand[i+1:]
                if fixed in ticker_set:
                    return fixed

    # Levenshtein fallback: find any ticker at distance 1
    for ticker in ticker_set:
        if len(ticker) == len(candidate) and _levenshtein_distance(candidate, ticker) == 1:
            return ticker
    return None


def extract_tickers(text: str) -> dict:
    """Return {ticker: mention_count} for this chunk.
    Counts every occurrence in the token scan (e.g. 'MNTS MNTS MNTS' → {'MNTS': 3}).
    Company-name and mishear matches always contribute 1.
    """
    counts: dict[str, int] = {}
    seen   = set()           # unique candidates for validation + dedup of name/mishear paths
    lower  = text.lower()

    # 1. Token scan — count raw occurrences, then validate unique candidates.
    # Track which candidates were originally all-caps (letter-spellings collapsed
    # by normalize_transcript); phonetic correction only applies to those.
    candidates    = []
    was_spelled   = {}
    for m in _TICKER_RE.finditer(text):
        raw = m.group(1)
        t   = raw.upper()
        if t not in _STOP_WORDS:
            counts[t] = counts.get(t, 0) + 1   # count every occurrence
            if t not in seen:
                candidates.append(t)
                seen.add(t)
                was_spelled[t] = raw.isupper()

    if candidates:
        _metric_inc("ticker_candidates_total", len(candidates))

        # Step 1: NASDAQ validation always runs — never skipped.
        # Ollama cannot cause misses because it only filters what NASDAQ already confirmed.
        if _VALID_TICKERS:
            confirmed = []
            for t in candidates:
                if t in _VALID_TICKERS:
                    confirmed.append(t)
                elif was_spelled.get(t) and len(t) >= 4:
                    # Spelled-out ticker not directly in NASDAQ — try phonetic correction.
                    # Min length 4: 3-letter candidates have too high a false-positive rate
                    # via Levenshtein (e.g. AMB→EMB, MNT→BNT, OFF→OVF).
                    fixed = _phonetic_match(t, _VALID_TICKERS)
                    if fixed and fixed != t:
                        confirmed.append(fixed)
                        counts[fixed] = counts.get(fixed, 0) + counts.pop(t, 0)
                        print(f"[PHONETIC] {t} → {fixed}", flush=True)
                    elif _ollama_ok:
                        ollama_fixed = _ollama_phonetic_validate(t)
                        if ollama_fixed and ollama_fixed != t:
                            confirmed.append(ollama_fixed)
                            counts[ollama_fixed] = counts.get(ollama_fixed, 0) + counts.pop(t, 0)
                            print(f"[OLLAMA_PHONETIC] {t} → {ollama_fixed}", flush=True)
        else:
            confirmed = list(candidates)

        # Drop any raw counts for tokens that failed validation
        confirmed_set = set(confirmed)
        counts = {t: c for t, c in counts.items() if t in confirmed_set}

        # Track validation success/failure
        failed = [t for t in candidates if t not in confirmed_set and was_spelled.get(t)]
        _metric_inc("ticker_candidates_validated", len(confirmed))
        if failed:
            _metric_inc("ticker_candidates_failed", len(failed))
            if len(failed) <= 3:
                print(f"[UNRESOLVED] {failed} (from: {text[:60]})", flush=True)

    # 2. Company name scan — 1 mention each
    for name, ticker in _NAME_TO_TICKER.items():
        if ticker not in seen and _NAME_RE[name].search(lower):
            counts[ticker] = counts.get(ticker, 0) + 1
            seen.add(ticker)

    # 3. Mishear correction — 1 mention each
    for mishear, ticker in _MISHEAR_MAP.items():
        if ticker not in seen and _MISHEAR_RE[mishear].search(lower):
            counts[ticker] = counts.get(ticker, 0) + 1
            seen.add(ticker)

    return counts


# =============================================================================
# WHISPER INITIAL PROMPT
# =============================================================================
# Seeding Whisper with common tickers dramatically improves recognition.
# It biases the decoder toward financial vocabulary.

INITIAL_PROMPT = (
    "CNBC financial news. Stock tickers and prices: "
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
    "GME AMC PLTR UBER LYFT ABNB DASH DKNG SOUN "
    "Tickers are sometimes spelled letter by letter: "
    "N V D A, A A P L, A M Z N, M S F T, T S L A. "
    "calls puts earnings price target breakout resistance."
)


# =============================================================================
# CONFIG — device index, API keys
# =============================================================================

_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--device", type=int, default=None)
_args, _ = _parser.parse_known_args()

_cfg_file     = Path(__file__).parent.parent / "bot_config.json"
_secrets_file = Path(__file__).parent.parent / "secrets.json"
_alpaca_key = _alpaca_secret = ""

_saved_device = None
_saved_workers = None
if _cfg_file.exists():
    try:
        _cfg_data = json.loads(_cfg_file.read_text())
        _saved_device = _cfg_data.get("device_index")
        _saved_workers = _cfg_data.get("transcriber_workers")
    except Exception:
        pass
if _secrets_file.exists():
    try:
        _s = json.loads(_secrets_file.read_text())
        _alpaca_key    = _s.get("api_key", "")
        _alpaca_secret = _s.get("secret_key", "")
    except Exception:
        pass

DEVICE_INDEX = _args.device if _args.device is not None else _saved_device
try:
    _N_WORKERS = max(1, int(_saved_workers)) if _saved_workers is not None else 3
except Exception:
    _N_WORKERS = 3

# Validate saved device index (a Windows index may not exist on Mac)
_tmp_p = pyaudio.PyAudio()
_valid_inputs = [i for i in range(_tmp_p.get_device_count())
                 if _tmp_p.get_device_info_by_index(i)["maxInputChannels"] > 0]
_tmp_p.terminate()
if DEVICE_INDEX is not None and DEVICE_INDEX not in _valid_inputs:
    print(f"[WARN] Saved device {DEVICE_INDEX} not found — will auto-detect.")
    DEVICE_INDEX = None


# =============================================================================
# ASR INIT
# =============================================================================

@contextlib.contextmanager
def _suppress_stderr():
    """Silence tqdm / mlx progress bars without breaking real stderr output."""
    old_fd = None
    try:
        devnull = _os.open(_os.devnull, _os.O_WRONLY)
        old_fd  = _os.dup(2)
        _os.dup2(devnull, 2)
        _os.close(devnull)
    except Exception:
        pass
    try:
        yield
    finally:
        if old_fd is not None:
            try:
                _os.dup2(old_fd, 2)
                _os.close(old_fd)
            except Exception:
                pass


def _init_asr():
    global _fw_model
    if _USE_MLX:
        print(f"[ASR] Loading {MLX_MODEL} via MLX Whisper ...", flush=True)
        # Warm-up run — downloads weights if not cached, compiles Metal shaders
        probe = np.zeros(TARGET_SR, dtype=np.float32)
        with _suppress_stderr():
            _mlx_whisper.transcribe(
                probe,
                path_or_hf_repo=MLX_MODEL,
                language="en",
                verbose=False,
            )
        print("[ASR] MLX Whisper ready.", flush=True)
    else:
        try:
            import ctranslate2 as _ct2
            hw = "cuda" if _ct2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            hw = "cpu"
        ct = "float16" if hw == "cuda" else "int8"
        print(f"[ASR] Loading {CPU_MODEL} via faster-whisper on {hw} ({ct}) ...", flush=True)
        _fw_model = _WhisperModel(CPU_MODEL, device=hw, compute_type=ct)
        print(f"[ASR] faster-whisper ready on {hw}.", flush=True)

_init_asr()


# =============================================================================
# AUDIO SETUP
# =============================================================================

p = pyaudio.PyAudio()

print("\nAvailable audio input devices:")
for i in range(p.get_device_count()):
    dev = p.get_device_info_by_index(i)
    if dev["maxInputChannels"] > 0:
        tag = " ← LOOPBACK" if "loopback" in dev["name"].lower() else ""
        bh  = " ← BLACKHOLE" if "blackhole" in dev["name"].lower() else ""
        print(f"  {i:2d}: {dev['name']}{tag}{bh}")

if _sys.platform == "darwin":
    # On Mac, ALWAYS use a loopback device — never a microphone.
    # Scan for BlackHole (preferred) or any device with "loopback" in the name.
    # Ignore any saved device_index that doesn't match a loopback source.
    _loopback_keywords = ("blackhole", "loopback", "multi-output")
    _loopback_idx = None
    for i in range(p.get_device_count()):
        dev = p.get_device_info_by_index(i)
        name_lower = dev["name"].lower()
        if dev["maxInputChannels"] > 0 and any(k in name_lower for k in _loopback_keywords):
            _loopback_idx = i
            if "blackhole" in name_lower:   # prefer BlackHole over generic loopback
                break
    if _loopback_idx is not None:
        if DEVICE_INDEX != _loopback_idx:
            print(f"\n[AUDIO] Overriding device {DEVICE_INDEX} → loopback device {_loopback_idx}: "
                  f"{p.get_device_info_by_index(_loopback_idx)['name']}")
        DEVICE_INDEX = _loopback_idx
    else:
        print("[ERROR] No BlackHole or loopback device found on Mac.")
        print("  Install BlackHole 2ch and set up a Multi-Output Device in Audio MIDI Setup.")
        p.terminate()
        raise SystemExit(1)
elif DEVICE_INDEX is None:
    try:
        choice = input("\nEnter device index (Enter = default): ").strip()
        DEVICE_INDEX = int(choice) if choice else p.get_default_input_device_info()["index"]
    except Exception:
        DEVICE_INDEX = p.get_default_input_device_info()["index"]

print(f"Using device: {DEVICE_INDEX}", flush=True)

_dev_info = p.get_device_info_by_index(DEVICE_INDEX)
_channels = min(2, int(_dev_info["maxInputChannels"]))

try:
    stream = p.open(
        format=pyaudio.paFloat32,
        channels=_channels,
        rate=SAMPLE_RATE,
        input=True,
        input_device_index=DEVICE_INDEX,
        frames_per_buffer=READ_FRAMES,
    )
except OSError as e:
    print(f"[ERROR] Could not open device {DEVICE_INDEX}: {e}")
    print("Check that BlackHole 2ch is installed and set as output in Audio MIDI Setup.")
    p.terminate()
    raise SystemExit(1)


# =============================================================================
# SHARED STATE
# =============================================================================

_audio_queue = Queue(maxsize=16)  # larger buffer for 2 parallel workers
_running     = threading.Event()
_running.set()

# Session-level dedup — don't spam the dashboard with the same ticker repeatedly
_session_tickers: set = set()
_session_lock = threading.Lock()


# =============================================================================
# TICKER DELIVERY — POST to dashboard API
# =============================================================================

_DASHBOARD_URL = "http://localhost:8888"


def _send_ticker(ticker: str, count: int = 1):
    """POST ticker to dashboard.
    First occurrence: adds to watchlist (/api/tickers/add).
    Subsequent occurrences: records mention count (/api/tickers/mention).
    Falls back to writing the file if API is down.
    """
    ticker = ticker.upper()
    with _session_lock:
        is_new = ticker not in _session_tickers
        if is_new:
            _session_tickers.add(ticker)

    try:
        import urllib.request as _req
        if is_new:
            # First time this session — add to watchlist (also records mention × count)
            body     = json.dumps({"ticker": ticker, "count": count}).encode()
            endpoint = f"{_DASHBOARD_URL}/api/tickers/add"
        else:
            # Already on watchlist — just record the mention count
            body     = json.dumps({"ticker": ticker, "count": count}).encode()
            endpoint = f"{_DASHBOARD_URL}/api/tickers/mention"

        req = _req.Request(endpoint, data=body,
                           headers={"Content-Type": "application/json"}, method="POST")
        with _req.urlopen(req, timeout=2) as resp:
            result = json.loads(resp.read())
            if is_new:
                label = "" if result.get("is_new", True) else "  (already listed)"
                print(f"  → {ticker}{label}", flush=True)
    except Exception as e:
        if is_new:
            _fallback_write(ticker, str(e))


def _fallback_write(ticker: str, reason: str):
    log_file = Path(__file__).parent / "wb_watchlist.json"
    try:
        from datetime import datetime, timezone
        now_iso  = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        existing = []
        if log_file.exists():
            raw = json.loads(log_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                existing = raw
        known = {(e["ticker"] if isinstance(e, dict) else e) for e in existing}
        if ticker not in known:
            existing.append({"ticker": ticker, "added": now_iso})
            log_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        print(f"  → {ticker}  (file write — API: {reason})", flush=True)
    except Exception as e2:
        print(f"  → {ticker}  (could not save: {e2})", flush=True)


# =============================================================================
# AUDIO HELPERS
# =============================================================================

def _apply_gain(mono: np.ndarray) -> np.ndarray:
    """Scale BlackHole's quiet loopback signal up to a useful RMS level."""
    if GAIN_TARGET_RMS is None:
        return mono
    rms = float(np.sqrt(np.mean(mono ** 2)))
    if rms < 1e-9:
        return mono
    gain = min(GAIN_TARGET_RMS / rms, GAIN_MAX)
    out  = mono * gain
    peak = float(np.max(np.abs(out)))
    if peak > 1.0:
        out = out / peak
    return out


# =============================================================================
# WORKER: AUDIO CAPTURE
# =============================================================================

def audio_capture():
    buf = np.empty(0, dtype=np.float32)

    while _running.is_set():
        try:
            data = stream.read(READ_FRAMES, exception_on_overflow=False)
            raw  = np.frombuffer(data, dtype=np.float32)
            mono = raw.reshape(-1, _channels).mean(axis=1)

            rms = float(np.sqrt(np.mean(mono ** 2)))
            if rms < SILENCE_THRESHOLD:
                continue

            mono = _apply_gain(mono)
            resampled = resample_poly(mono, RESAMPLE_UP, RESAMPLE_DOWN)
            buf = np.concatenate((buf, resampled))

            while len(buf) >= CHUNK_SAMPLES:
                chunk = buf[:CHUNK_SAMPLES].copy()
                buf   = buf[ADVANCE_SAMPLES:].copy()
                try:
                    _audio_queue.put_nowait(chunk)
                except Full:
                    _metric_inc("queue_drops")
                    with _METRICS_LOCK:
                        qd = int(_METRICS.get("queue_drops", 0))
                    if qd <= 5 or qd % 25 == 0:
                        print(f"[QUEUE] drop oldest chunk total={qd} size={_audio_queue.qsize()}/{_audio_queue.maxsize}", flush=True)
                    try:
                        _audio_queue.get_nowait()
                        _audio_queue.put_nowait(chunk)
                    except Exception:
                        pass
        except Exception:
            time.sleep(0.05)


# =============================================================================
# WORKER: TRANSCRIPTION
# =============================================================================

def transcription_worker():
    last_metrics_emit = 0.0
    while _running.is_set():
        try:
            chunk = _audio_queue.get(timeout=1.0)
            if chunk is None:
                break

            t0 = time.perf_counter()

            with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
                if _USE_MLX:
                    with _suppress_stderr():
                        result = _mlx_whisper.transcribe(
                            chunk,
                            path_or_hf_repo=MLX_MODEL,
                            language="en",
                            initial_prompt=INITIAL_PROMPT,
                            temperature=0.0,
                            no_speech_threshold=0.4,
                            compression_ratio_threshold=2.4,
                            condition_on_previous_text=False,
                            verbose=False,
                        )
                    text = result.get("text", "").strip()
                else:
                    segs, _ = _fw_model.transcribe(
                        chunk,
                        language="en",
                        initial_prompt=INITIAL_PROMPT,
                        beam_size=CPU_BEAM,
                        temperature=0.0,
                        vad_filter=True,
                        no_speech_threshold=0.4,
                        compression_ratio_threshold=2.4,
                        condition_on_previous_text=False,
                    )
                    text = " ".join(s.text.strip() for s in segs).strip()

            ms = (time.perf_counter() - t0) * 1000

            # Normalize FIRST so the hallucination guard sees collapsed letter-
            # spellings ("A I I O A I I O" → "AIIO AIIO"). The repeated-token
            # check then matches the known-ticker bypass instead of skipping the
            # whole chunk for repeating "A" or "I".
            if text:
                text = normalize_transcript(text)

            # Hallucination vs. real mention guard.
            #
            # Real Whisper loops on silence are short tokens (1-2 chars) repeated
            # many times: "T T T T T T T T T". A real TTS read of a ticker can
            # legitimately repeat the same 3-5 char symbol 10-30 times in one
            # chunk. Distinguish by length AND by whether the repeated token
            # matches a known ticker (exactly or phonetically).
            #
            # Pattern 2: prompt echo — many *different* ALL-CAPS tickers echoed
            # at once; left as-is.
            if text:
                _words        = text.split()
                if _words:
                    _most_rep_raw = max(set(_words), key=_words.count)
                    _rep_count    = _words.count(_most_rep_raw)
                    # Strip surrounding punctuation before matching against the
                    # ticker set: "AIIO," should count as AIIO.
                    _most_rep     = re.sub(r'[^A-Za-z]', '', _most_rep_raw).upper()
                    _is_known_ticker = bool(_most_rep) and (
                        _most_rep in _VALID_TICKERS or
                        (2 <= len(_most_rep) <= 5
                         and _phonetic_match(_most_rep, _VALID_TICKERS) is not None)
                    )
                    if _rep_count > 6:
                        if len(_most_rep) <= 2:
                            _is_loop = True            # single/double-char loop
                        elif _is_known_ticker:
                            _is_loop = _rep_count > 50 # allow heavy real TTS reads
                        else:
                            _is_loop = True            # unknown 3+ char repeated
                    else:
                        _is_loop = False
                    _upper        = [w for w in _words if w.isupper() and 2 <= len(w) <= 5]
                    _unique_upper = len(set(_upper))
                    _is_echo = (len(_words) >= 10
                                and _unique_upper >= 8
                                and len(_upper) / len(_words) > 0.70)
                    if _is_loop or _is_echo:
                        print(f"[{time.strftime('%H:%M:%S')}] [{ms:.0f}ms] [SKIP hallucination] {text[:80]}", flush=True)
                        continue

            print(f"[{time.strftime('%H:%M:%S')}] [{ms:.0f}ms] {text}", flush=True)

            if not text or len(text.split()) < 2:
                continue

            tickers = extract_tickers(text)   # {ticker: count}

            for t, cnt in tickers.items():
                _send_ticker(t, cnt)

            now = time.time()
            if now - last_metrics_emit >= 5:
                last_metrics_emit = now
                print(f"[METRICS] {json.dumps(_metrics_payload(), sort_keys=True)}", flush=True)

        except Exception as e:
            msg = str(e).strip()
            if msg:
                print(f"[WARN] {type(e).__name__}: {msg}", flush=True)
            time.sleep(0.05)


def _ollama_health_worker():
    while _running.is_set():
        try:
            _ping_ollama(verbose=False)
        except Exception:
            pass
        time.sleep(60)


# =============================================================================
# START
# =============================================================================

# parallel transcription workers — configurable via bot_config.json: transcriber_workers

_threads = [
    threading.Thread(target=audio_capture,        daemon=True, name="audio"),
    *[threading.Thread(target=transcription_worker, daemon=True, name=f"transcription-{i+1}")
      for i in range(_N_WORKERS)],
    threading.Thread(target=_ollama_health_worker, daemon=True, name="ollama-health"),
]
for t in _threads:
    t.start()

# Initial Ollama ping before printing status
_ping_ollama(verbose=True)

engine = f"MLX Whisper ({MLX_MODEL})" if _USE_MLX else f"faster-whisper ({CPU_MODEL})"
ticker_engine = f"Ollama ({OLLAMA_MODEL})" if _ollama_ok else ("NASDAQ list" if _VALID_TICKERS else "stop-word filter")
print(f"\nListening — ASR: {engine}  |  Ticker classifier: {ticker_engine}")
print(f"Chunk: {CHUNK_DURATION}s  Overlap: {OVERLAP}s  Workers: {_N_WORKERS}  SR: {SAMPLE_RATE}Hz → {TARGET_SR}Hz")
print("Press Ctrl+C to stop.\n")

try:
    while _running.is_set():
        time.sleep(0.2)
except KeyboardInterrupt:
    print("\nShutting down ...")
    _running.clear()
    # Send one None per worker so each unblocks from queue.get()
    for _ in range(_N_WORKERS):
        try:
            _audio_queue.put_nowait(None)
        except Full:
            pass

stream.stop_stream()
stream.close()
p.terminate()
print("Stopped.")
