import argparse
import json
import os as _os
import resource
import sys as _sys
# pyaudiowpatch provides WASAPI loopback on Windows; fall back to standard pyaudio elsewhere
if _sys.platform == "win32":
    import pyaudiowpatch as pyaudio
else:
    import pyaudio
import numpy as np
import time
import re
import threading
from scipy.signal import resample_poly
from queue import Queue, Full
from pathlib import Path

# ── Engine selection ──────────────────────────────────────────────────────────
# MLX runs on Apple Silicon GPU/Neural Engine — 5-10× faster than CPU Whisper.
# Falls back to faster-whisper on Windows or Intel Macs.
_USE_MLX = False
try:
    if _sys.platform == "darwin":
        import mlx_whisper as _mlx_whisper
        _USE_MLX = True
        print("[ENGINE] MLX Whisper detected — using Apple Silicon GPU/Neural Engine", flush=True)
except ImportError:
    pass

if not _USE_MLX:
    from faster_whisper import WhisperModel
    print("[ENGINE] Using faster-whisper (CPU)", flush=True)


# ── Silero VAD (optional) ─────────────────────────────────────────────────────
# More accurate than simple RMS — distinguishes speech from music/noise.
# Install:  pip install silero-vad
# Falls back to RMS threshold silently if not installed.
_USE_SILERO_VAD   = False
_silero_vad_model = None
_silero_get_ts    = None   # get_speech_timestamps function
try:
    from silero_vad import load_silero_vad, get_speech_timestamps as _silero_get_ts
    _silero_vad_model = load_silero_vad()
    _USE_SILERO_VAD   = True
    print("[VAD] Silero VAD loaded — speech detection active", flush=True)
except ImportError:
    print("[VAD] silero-vad not installed — using RMS threshold  (pip install silero-vad to improve)", flush=True)
except Exception as _e:
    print(f"[VAD] Silero VAD load failed ({_e}) — using RMS threshold", flush=True)


# ── Noise suppression (optional) ─────────────────────────────────────────────
# noisereduce estimates background noise from the first 0.5s of each chunk
# and subtracts it — helps with hiss, hum, and fan noise from loopback audio.
# Install:  pip install noisereduce
_USE_NOISEREDUCE = False   # disabled — CNBC loopback is already clean; noisereduce
                           # corrupts ticker consonants (TSLA→TLA, NVDA→NDA) because
                           # the "quiet" reference window always contains speech harmonics
try:
    import noisereduce as _nr
except ImportError:
    pass


# ========================= TRANSCRIPT NORMALIZATION =========================

_NATO = {
    "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D",
    "echo": "E", "foxtrot": "F", "golf": "G", "hotel": "H",
    "india": "I", "juliet": "J", "kilo": "K", "lima": "L",
    "mike": "M", "november": "N", "oscar": "O", "papa": "P",
    "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T",
    "uniform": "U", "victor": "V", "whiskey": "W", "xray": "X",
    "x-ray": "X", "yankee": "Y", "zulu": "Z",
}

_nato_word = "(?:" + "|".join(re.escape(w) for w in _NATO) + ")"
_NATO_SEP   = r"[ \t]*,?[ \t]*"   # optional comma between NATO words
# Allow unlimited NATO words (will be deduplicated in collapse_nato)
_NATO_PATTERN = re.compile(rf"(?i)\b{_nato_word}(?:{_NATO_SEP}{_nato_word})*\b")
# Pattern to match space/comma-separated single capital letters (e.g., "E L P W")
_SINGLE_LETTERS_PATTERN = re.compile(r'\b[A-Z](?:[ \t,]+[A-Z])+\b')


def _deduplicate_ticker(ticker: str) -> str:
    """
    Handle repeated ticker sequences. E.g., "ELPWELPW" → "ELPW".
    Detects if the ticker is a pattern repeated 2+ times and collapses it.
    """
    if len(ticker) < 4 or len(ticker) > 10:
        return ticker
    
    # Try divisors from 2 to 5 (for tickers of 2-5 chars repeated 2-5 times)
    for divisor in range(2, min(6, len(ticker) // 2 + 1)):  # max 5 repetitions
        if len(ticker) % divisor == 0:
            segment_len = len(ticker) // divisor
            # Only consider if segment would be a valid ticker (2-5 chars)
            if not (2 <= segment_len <= 5):
                continue
                
            segment = ticker[:segment_len]
            # Check if entire string is this segment repeated
            if all(ticker[i*segment_len:(i+1)*segment_len] == segment 
                   for i in range(divisor)):
                return segment
    
    return ticker


def normalize_transcript(text: str) -> str:
    # 1. Collapse NATO alphabet sequences (including repeated ones)
    def collapse_nato(m: re.Match) -> str:
        words = re.split(r'[\s,]+', m.group(0).lower())
        letters = "".join(_NATO.get(w, "") for w in words if w)
        # Deduplicate if the same ticker was spoken multiple times
        letters = _deduplicate_ticker(letters)
        return letters if 2 <= len(letters) <= 5 else m.group(0)

    text = _NATO_PATTERN.sub(collapse_nato, text)

    # 2. Collapse space/comma-separated single letters (e.g., "E L P W" → "ELPW")
    # Apply repeatedly to handle multiple sequences
    def collapse_single_letters(m: re.Match) -> str:
        letters = re.sub(r'[\s,]+', '', m.group(0).upper())
        # Deduplicate repeated patterns
        letters = _deduplicate_ticker(letters)
        return letters if 2 <= len(letters) <= 5 else m.group(0)
    
    # Keep applying until no more matches (handles multiple sequences)
    prev_text = None
    while prev_text != text:
        prev_text = text
        text = _SINGLE_LETTERS_PATTERN.sub(collapse_single_letters, text)

    # 3. Collapse dot-separated letters (e.g., "E.L.P.W" → "ELPW")
    def collapse_dots(m: re.Match) -> str:
        letters = m.group(0).replace(".", "").upper()
        return letters if 2 <= len(letters) <= 5 else m.group(0)

    text = re.sub(r'(?<!\w)(?:[A-Za-z]\.)+(?:[A-Za-z])', collapse_dots, text)

    # 4. Collapse hyphen-separated letters (e.g., "E-L-P-W" → "ELPW")
    def collapse_hyphens(m: re.Match) -> str:
        letters = m.group(0).replace("-", "").upper()
        return letters if 2 <= len(letters) <= 5 else m.group(0)

    text = re.sub(r'(?<!\w)(?:[A-Za-z]-)+[A-Za-z](?!\w)', collapse_hyphens, text)

    return text


# ========================= TICKER EXTRACTION =========================

# Uppercase tokens Whisper produces that are not stock tickers.
# NOTE: Do NOT add real tickers here (e.g. COST, OPEN, REAL) — the Alpaca
# universe check handles false positives; these stop words are only for
# common English words that would never be valid tickers.
_STOP_WORDS = {
    "A", "I", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "IF", "IN", "IS",
    "IT", "ME", "MY", "NO", "OF", "OK", "ON", "OR", "SO", "TO", "UP", "US",
    "WE", "AND", "ARE", "BUT", "CAN", "DID", "FOR", "GET", "GOT", "HAD",
    "HAS", "HIM", "HIS", "HOW", "HER", "ITS", "NEW", "NOT", "NOW", "OLD",
    "ONE", "OUR", "OUT", "SAY", "SEE", "THE", "TOO", "TWO", "WAS", "WHO",
    "WHY", "YET", "YOU", "ALL", "ANY", "LET", "PUT", "RUN", "SET", "ADD",
    "HIT", "TRY", "USE", "WAY", "DAY", "MAY", "OWN", "ASK", "ACT",
    "ALSO", "BACK", "BEEN", "CALL", "COME", "DOES", "DOWN", "EACH", "EVEN",
    "FROM", "GIVE", "GOOD", "HAVE", "HERE", "HIGH", "HOLD", "INTO", "JUST",
    "KEEP", "KNOW", "LAST", "LIKE", "LONG", "LOOK", "MADE", "MAKE",
    "MANY", "MORE", "MOST", "MOVE", "MUCH", "MUST", "NEXT", "ONLY",
    "OVER", "PAST", "SAME", "SELL", "SHOW", "SIDE", "SOME",
    "STOP", "SUCH", "TAKE", "THAN", "THAT", "THEM", "THEN", "THEY", "THIS",
    "TIME", "VERY", "WANT", "WELL", "WHAT", "WHEN", "WILL", "WITH", "WORK",
    "YOUR", "SAID", "SAYS", "TOLD", "TELL", "TALK", "WENT", "GOES", "BOTH",
    "ONCE", "UPON", "SOON", "EVER", "YEAR", "WEEK", "DAYS", "LETS", "PUTS",
    "LIVE", "BUY",
    # financial/market terms that are NOT tickers
    "ETF", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FDA", "FED", "GDP",
    "CPI", "EPS", "ATH", "ATL", "RSI", "SMA", "EMA", "MACD", "BEAR", "BULL",
    "CALL", "PUTS", "SPAC", "REIT", "BOND", "DEBT", "CASH", "RATE", "RISK",
    "LOSS", "GAIN", "NEWS", "CNBC", "NYSE", "NASDAQ", "VWAP",
    # months / days
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    "JAN", "FEB", "MAR", "APR", "AUG", "SEP", "OCT", "NOV", "DEC",
    # Noise / Whisper hallucination artifacts
    "HEY", "THANKS", "YEAH", "THEIR", "THERE", "THESE", "THOSE",
    "STILL", "REALLY", "PRETTY", "MIGHT", "MAYBE", "WHICH", "WHERE", "WHILE",
    "HALT", "HALTED", "ALERT", "LEVELS", "LEVEL", "STOCKS", "STOCK",
    "PRICE", "PRICES", "VOLUME", "MARKET", "TRADE", "SHARES",
    "CUR", "UCI", "LPG", "RFX", "ROADS", "PRFS", "SNG", "SNDR",
    "BIYM", "SBL", "YIA", "PPG", "WWW", "BAGER",
}

# Spoken company names → ticker (Whisper capitalizes proper nouns).
# Add any name a trader would say out loud that maps to a ticker.
_NAME_TO_TICKER = {
    # Big Tech
    "apple": "AAPL", "tesla": "TSLA", "nvidia": "NVDA", "amazon": "AMZN",
    "google": "GOOGL", "alphabet": "GOOGL", "microsoft": "MSFT",
    "meta": "META", "facebook": "META", "netflix": "NFLX",
    # Semiconductors
    "amd": "AMD", "intel": "INTC", "qualcomm": "QCOM", "broadcom": "AVGO",
    "micron": "MU", "arm": "ARM", "arm holdings": "ARM",
    "applied materials": "AMAT", "lam research": "LRCX", "klac": "KLAC",
    "taiwan semi": "TSM", "tsmc": "TSM", "marvell": "MRVL",
    "super micro": "SMCI", "supermicro": "SMCI",
    # Software / Cloud
    "salesforce": "CRM", "oracle": "ORCL", "snowflake": "SNOW",
    "shopify": "SHOP", "zoom": "ZM", "datadog": "DDOG", "cloudflare": "NET",
    "crowdstrike": "CRWD", "palo alto": "PANW", "fortinet": "FTNT",
    "servicenow": "NOW", "workday": "WDAY", "mongodb": "MDB",
    "confluent": "CFLT", "elastic": "ESTC", "gitlab": "GTLB",
    "twilio": "TWLO", "okta": "OKTA", "docusign": "DOCU",
    "hubspot": "HUBS", "asana": "ASAN", "bill.com": "BILL",
    # Fintech / Payments
    "visa": "V", "mastercard": "MA", "paypal": "PYPL", "square": "SQ",
    "block": "SQ", "affirm": "AFRM", "sofi": "SOFI", "nu bank": "NU",
    "nubank": "NU", "robinhood": "HOOD", "coinbase": "COIN",
    "microstrategy": "MSTR", "strategy": "MSTR",
    # Banks / Finance
    "jpmorgan": "JPM", "jp morgan": "JPM", "goldman": "GS",
    "goldman sachs": "GS", "morgan stanley": "MS",
    "bank of america": "BAC", "wells fargo": "WFC", "citigroup": "C",
    "citi": "C", "schwab": "SCHW", "blackrock": "BLK",
    # EV / Autos
    "ford": "F", "general motors": "GM", "gm": "GM",
    "rivian": "RIVN", "lucid": "LCID", "nio": "NIO",
    "li auto": "LI", "xpeng": "XPEV",
    # Crypto-adjacent
    "coinbase": "COIN", "riot": "RIOT", "marathon": "MARA",
    "marathon digital": "MARA", "cleanspark": "CLSK",
    # Retail / Consumer
    "walmart": "WMT", "target": "TGT", "costco": "COST",
    "home depot": "HD", "lowes": "LOW", "lowe's": "LOW",
    "amazon": "AMZN", "chewy": "CHWY", "wayfair": "W",
    "carvana": "CVNA", "carmax": "KMX",
    # Media / Entertainment
    "disney": "DIS", "comcast": "CMCSA", "warner": "WBD",
    "paramount": "PARA", "spotify": "SPOT", "netflix": "NFLX",
    "roblox": "RBLX", "unity": "U",
    # Telecom
    "verizon": "VZ", "at&t": "T", "att": "T", "t-mobile": "TMUS",
    # Healthcare / Pharma / Biotech
    "pfizer": "PFE", "moderna": "MRNA", "johnson": "JNJ",
    "johnson and johnson": "JNJ", "abbvie": "ABBV", "merck": "MRK",
    "eli lilly": "LLY", "lilly": "LLY", "novo nordisk": "NVO",
    "amgen": "AMGN", "gilead": "GILD", "regeneron": "REGN",
    "biogen": "BIIB", "vertex": "VRTX", "illumina": "ILMN",
    "unitedhealth": "UNH", "humana": "HUM", "cigna": "CI",
    "hims": "HIMS", "hims and hers": "HIMS",
    # Energy
    "exxon": "XOM", "chevron": "CVX", "conocophillips": "COP",
    "halliburton": "HAL", "schlumberger": "SLB",
    "plug power": "PLUG", "plug": "PLUG", "bloom energy": "BE",
    # Aerospace / Defense
    "boeing": "BA", "lockheed": "LMT", "raytheon": "RTX",
    "northrop": "NOC", "l3harris": "LHX", "spacex": "SPCE",
    # Meme / Retail favorites
    "gamestop": "GME", "game stop": "GME", "amc": "AMC",
    "amc entertainment": "AMC", "blackberry": "BB",
    "bed bath": "BBBY", "beyond meat": "BYND",
    # Travel / Hospitality
    "airbnb": "ABNB", "booking": "BKNG", "expedia": "EXPE",
    "uber": "UBER", "lyft": "LYFT", "doordash": "DASH",
    "delta": "DAL", "united airlines": "UAL", "southwest": "LUV",
    "royal caribbean": "RCL", "carnival": "CCL",
    # ETFs / Indices (commonly spoken by name)
    "spy": "SPY", "s and p": "SPY", "s&p": "SPY",
    "qqq": "QQQ", "cubes": "QQQ", "nasdaq etf": "QQQ",
    "iwm": "IWM", "russell": "IWM",
    "dia": "DIA", "dow etf": "DIA",
    "xlf": "XLF", "xle": "XLE", "xlk": "XLK", "xli": "XLI",
    "arc innovation": "ARKK", "ark": "ARKK",
    # Other commonly mentioned
    "palantir": "PLTR", "berkshire": "BRK",
    "draft kings": "DKNG", "draftkings": "DKNG",
    "toast": "TOST", "bumble": "BMBL", "match": "MTCH",
    "pinterest": "PINS", "snap": "SNAP",
    "joby": "JOBY", "joby aviation": "JOBY",
    "rocket": "RKT", "rocket companies": "RKT",
    "soun": "SOUN", "soundhound": "SOUN",
}

_TICKER_RE = re.compile(r'\b([A-Za-z]{2,5})\b')  # case-insensitive — uppercase after match
_NAME_RE   = {name: re.compile(rf'\b{re.escape(name)}\b', re.I)
              for name in _NAME_TO_TICKER}

# Whisper sometimes mishears tickers as similar-sounding nonsense words.
# Map those known misrecognitions directly to the correct ticker.
_MISHEAR_MAP = {
    # Google / Alphabet
    "gugsel": "GOOGL", "guggle": "GOOGL", "gugle": "GOOGL", "googel": "GOOGL",
    "googl":  "GOOGL", "goog":   "GOOG",
    # Robinhood
    "hud":    "HOOD",  "hood":   "HOOD",
    # Nvidia — truncated variants
    "envidia":"NVDA",  "vidia":  "NVDA",  "invidia": "NVDA",
    "nda":    "NVDA",  "nvda":   "NVDA",  "nvia":    "NVDA",
    # Tesla — also catches truncated Whisper output when leading phoneme is dropped
    "tesler": "TSLA",  "tesla":  "TSLA",
    "tla":    "TSLA",  "tsla":   "TSLA",
    # Palantir
    "palanteer": "PLTR", "palantir": "PLTR", "palantar": "PLTR", "palanter": "PLTR",
    # Apple
    "appal":  "AAPL",  "apples": "AAPL",  "aple": "AAPL",
    # Amazon
    "amazin": "AMZN",  "amazons":"AMZN",
    # Strategy / MicroStrategy
    "microstrategy": "MSTR", "micro strategy": "MSTR", "strategy": "MSTR",
    # Microsoft truncations
    "msf":    "MSFT",  "mst":    "MSFT",  "msft":   "MSFT",
    # AAPL truncations
    "apl":    "AAPL",  "aap":    "AAPL",
    # AMZN truncations
    "amz":    "AMZN",  "azn":    "AMZN",
    # META truncations
    "met":    "META",
    # GOOGL truncations
    "goo":    "GOOGL", "gogl":   "GOOGL",
    # PLTR truncations
    "plt":    "PLTR",  "ltr":    "PLTR",
    # AMD truncations
    "amd":    "AMD",
    # Coinbase
    "coinbase": "COIN",
    # SoundHound
    "soundhound": "SOUN", "sound hound": "SOUN",
    # Palantir variants Whisper medium.en sometimes produces
    "pltr":   "PLTR",
    # Super Micro
    "supermicro": "SMCI", "super micro": "SMCI",
    # Rivian
    "rivien": "RIVN",  "rivian": "RIVN",
    # Lucid
    "lucid":  "LCID",
    # Affirm
    "affirm": "AFRM",
    # SoFi
    "sofi":   "SOFI",
    # DraftKings
    "draftkings": "DKNG", "draft kings": "DKNG",
    # Snowflake
    "snowflake": "SNOW",
    # CrowdStrike
    "crowdstrike": "CRWD", "crowd strike": "CRWD",
    # Palo Alto
    "palo alto": "PANW",
    # Cloudflare
    "cloudflare": "NET",
    # Robinhood full name
    "robinhood": "HOOD", "robin hood": "HOOD",
}
_MISHEAR_RE = {k: re.compile(rf'\b{re.escape(k)}\b', re.I) for k in _MISHEAR_MAP}

_ticker_universe: set = set()   # populated at startup from Alpaca; empty = fallback mode


def extract_tickers(text: str) -> list:
    found = []
    seen  = set()

    # All tokens 2-5 chars — uppercase before checking so "coin" → COIN, "rivn" → RIVN
    for m in _TICKER_RE.finditer(text):
        t = m.group(1).upper()
        if t in _STOP_WORDS or t in seen:
            continue
        if _ticker_universe and t not in _ticker_universe:
            continue
        found.append(t)
        seen.add(t)

    # Spoken company names — catches names Whisper spells out
    lower = text.lower()
    for name, ticker in _NAME_TO_TICKER.items():
        if ticker not in seen and _NAME_RE[name].search(lower):
            found.append(ticker)
            seen.add(ticker)

    # Whisper misrecognitions — known phonetic substitutions
    for mishear, ticker in _MISHEAR_MAP.items():
        if ticker not in seen and _MISHEAR_RE[mishear].search(lower):
            found.append(ticker)
            seen.add(ticker)

    return found


# ========================= TICKER LOG =========================

TICKER_LOG_FILE = Path(__file__).parent / "wb_watchlist.json"
_log_lock       = threading.Lock()

_logged_tickers: set = set()
_log_file_mtime: float = -1.0

try:
    _existing = json.loads(TICKER_LOG_FILE.read_text(encoding="utf-8"))
    if isinstance(_existing, list):
        _logged_tickers = {t.strip().upper() for t in _existing if isinstance(t, str) and t.strip()}
    _log_file_mtime = TICKER_LOG_FILE.stat().st_mtime
except Exception:
    pass


def _sync_from_file():
    """Re-read the watchlist if it changed on disk (e.g. cleared by the dashboard)."""
    global _logged_tickers, _log_file_mtime
    try:
        mtime = TICKER_LOG_FILE.stat().st_mtime
        if mtime == _log_file_mtime:
            return
        data = json.loads(TICKER_LOG_FILE.read_text(encoding="utf-8"))
        new_set = {t.strip().upper() for t in data if isinstance(t, str) and t.strip()} if isinstance(data, list) else set()
        print(f"[SYNC] File changed — was {sorted(_logged_tickers)}, now {sorted(new_set)}", flush=True)
        _logged_tickers = new_set
        _log_file_mtime = mtime
    except Exception:
        pass


def log_ticker(ticker: str) -> bool:
    """Log a new ticker to the watchlist JSON. Returns True if it was newly added."""
    global _log_file_mtime
    ticker = ticker.upper()
    with _log_lock:
        # 1. Sync current state from disk to avoid overwriting changes from other processes
        _sync_from_file()
        
        # 2. Skip if already in memory (which is now synced with disk)
        if ticker in _logged_tickers:
            return False
        
        # 3. Add to set and prepare snapshot
        _logged_tickers.add(ticker)
        snapshot = sorted(_logged_tickers)
        
        # 4. Write to disk immediately while holding the lock
        try:
            TICKER_LOG_FILE.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            _log_file_mtime = TICKER_LOG_FILE.stat().st_mtime
            print(f"[LOG] {ticker}", flush=True)
            return True
        except Exception as e:
            print(f"[LOG] Could not write watchlist: {e}")
            return False


# ========================= TICKER UNIVERSE =========================
# Fetched once from Alpaca at startup and cached for 24h.
# extract_tickers() gates every candidate against this set, collapsing
# false positives to near-zero — random words are not valid equity symbols.

_UNIVERSE_FILE    = Path(__file__).parent / "ticker_universe.json"
_UNIVERSE_MAX_AGE = 24 * 3600


def _load_universe_cache() -> set:
    try:
        data = json.loads(_UNIVERSE_FILE.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) < _UNIVERSE_MAX_AGE:
            symbols = set(data["symbols"])
            print(f"[UNIVERSE] {len(symbols)} symbols loaded from cache", flush=True)
            return symbols
    except Exception:
        pass
    return set()


def _fetch_universe(api_key: str, secret_key: str) -> set:
    if not api_key or not secret_key:
        print("[UNIVERSE] No API credentials — skipping fetch", flush=True)
        return set()
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass
        is_paper = api_key.startswith("PK")
        client   = TradingClient(api_key, secret_key, paper=is_paper)
        assets   = client.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY))
        symbols  = {
            a.symbol for a in assets
            if getattr(a, "tradable", False) and re.fullmatch(r"[A-Z]{1,5}", a.symbol)
        }
        _UNIVERSE_FILE.write_text(
            json.dumps({"ts": time.time(), "symbols": sorted(symbols)}, indent=2),
            encoding="utf-8",
        )
        print(f"[UNIVERSE] {len(symbols)} symbols fetched from Alpaca", flush=True)
        return symbols
    except Exception as e:
        print(f"[UNIVERSE] Fetch failed: {e}", flush=True)
        return set()


def _init_universe(api_key: str, secret_key: str) -> set:
    universe = _load_universe_cache()
    if not universe:
        universe = _fetch_universe(api_key, secret_key)
    return universe


# ========================= CONFIG =========================

_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--device", type=int, default=None)
_args, _ = _parser.parse_known_args()

_cfg_file     = Path(__file__).parent.parent / "bot_config.json"
_secrets_file = Path(__file__).parent.parent / "secrets.json"
_saved_device  = None
_alpaca_key    = ""
_alpaca_secret = ""
if _cfg_file.exists():
    try:
        _saved_device = json.loads(_cfg_file.read_text()).get("device_index")
    except Exception:
        pass
if _secrets_file.exists():
    try:
        _s = json.loads(_secrets_file.read_text())
        _alpaca_key    = _s.get("api_key", "")
        _alpaca_secret = _s.get("secret_key", "")
        _finnhub_key   = _s.get("finnhub_key", "")
    except Exception:
        _finnhub_key = ""

DEVICE_INDEX = _args.device if _args.device is not None else _saved_device

# Validate that the saved device index actually exists on this system.
# A Windows device index stored in bot_config.json will be invalid on Mac.
if DEVICE_INDEX is not None:
    _tmp_p = pyaudio.PyAudio()
    _valid_indices = [
        i for i in range(_tmp_p.get_device_count())
        if _tmp_p.get_device_info_by_index(i)["maxInputChannels"] > 0
    ]
    _tmp_p.terminate()
    if DEVICE_INDEX not in _valid_indices:
        print(f"[WARN] Saved device index {DEVICE_INDEX} not found on this system — resetting to auto-detect.")
        DEVICE_INDEX = None

# small.en is English-only: same size as small but faster and more accurate for English
# MLX uses HuggingFace MLX-community models; faster-whisper uses its own format
WHISPER_MODEL_MLX = "mlx-community/whisper-large-v3-turbo"  # confirmed HF repo; use whisper-large-v3-turbo-4bit for lower memory if available
WHISPER_MODEL_CPU = "small.en"   # fallback for non-Apple-Silicon / before MLX installed
WHISPER_MODEL     = WHISPER_MODEL_MLX if _USE_MLX else WHISPER_MODEL_CPU
WHISPER_BEAM_SIZE = 5      # Higher beam = better accuracy (3 was too fast/greedy)

SAMPLE_RATE       = 48000  # Match BlackHole's actual hardware rate (set in Audio MIDI Setup)
TARGET_SR         = 16000
CHUNK_DURATION    = 4.0    # 4s gives Whisper a full sentence with room for boundary tickers
OVERLAP           = 1.0    # 1s overlap — tickers at chunk edges appear in both chunks
SILENCE_THRESHOLD = 0.0005  # BlackHole loopback signal is quiet (~0.001 RMS)
GAIN_TARGET_RMS   = 0.12   # adaptive gain targets this RMS level (Whisper likes ~0.1-0.15)
GAIN_MAX          = 20.0   # never amplify more than 20× — prevents noise explosion

CHUNK_SAMPLES   = int(TARGET_SR * CHUNK_DURATION)
OVERLAP_SAMPLES = int(TARGET_SR * OVERLAP)
ADVANCE_SAMPLES = CHUNK_SAMPLES - OVERLAP_SAMPLES
READ_FRAMES     = int(SAMPLE_RATE * 0.5)  # Stable buffer size — prevents crackling

RESAMPLE_UP   = 1    # 48000 → 16000 is exactly 3:1 — clean integer ratio
RESAMPLE_DOWN = 3

# Load ticker universe (gates extract_tickers against known valid symbols)
_ticker_universe = _init_universe(_alpaca_key, _alpaca_secret)


def _fetch_finnhub_trending(finnhub_key: str, max_tickers: int = 25) -> list:
    """
    Fetch today's most-mentioned tickers from Finnhub market news.
    Called once at startup — uses stdlib only, 5s timeout, fails silently.
    """
    if not finnhub_key:
        return []
    try:
        import urllib.request
        url = f"https://finnhub.io/api/v1/news?category=general&token={finnhub_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "trading-helper/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            articles = json.loads(resp.read().decode())
        counts: dict = {}
        for article in articles:
            related = article.get("related", "") or ""
            for t in related.split(","):
                t = t.strip().upper()
                if t and re.fullmatch(r"[A-Z]{1,5}", t):
                    counts[t] = counts.get(t, 0) + 1
        trending = sorted(counts, key=lambda x: counts[x], reverse=True)[:max_tickers]
        if trending:
            print(f"[FINNHUB] Trending today: {' '.join(trending)}", flush=True)
        return trending
    except Exception as e:
        print(f"[FINNHUB] Skipping trending fetch: {e}", flush=True)
        return []


_finnhub_trending = _fetch_finnhub_trending(_finnhub_key)

# Build initial prompt — priority order:
#   1. Base financial context (always present)
#   2. Finnhub trending tickers for today (dynamic, fetched at startup)
#   3. Current watchlist (tickers already seen this session)
_PROMPT_BASE = (
    "CNBC financial news. NYSE NASDAQ stock tickers: "
    # Mega-cap tech
    "AAPL MSFT NVDA AMZN GOOGL GOOG META NFLX TSLA ORCL IBM "
    # Semis
    "AMD INTC QCOM AVGO MU AMAT LRCX KLAC TSM ARM MRVL SMCI "
    # Cloud / SaaS
    "CRM SNOW SHOP ZM DDOG CRWD PANW NET OKTA TWLO HUBS WDAY NOW MDB "
    # Fintech / Crypto
    "V MA PYPL SQ HOOD COIN AFRM SOFI MSTR "
    # Banks
    "JPM GS MS BAC WFC C SCHW BLK "
    # ETFs
    "SPY QQQ IWM DIA XLF XLE XLK ARKK "
    # Retail / Consumer
    "WMT TGT COST HD LOW AMZN CHWY CVNA "
    # Media / Entertainment
    "DIS CMCSA WBD PARA SPOT RBLX "
    # EV / Autos
    "F GM RIVN LCID NIO XPEV "
    # Healthcare / Biotech
    "PFE MRNA UNH LLY NVO JNJ ABBV MRK AMGN GILD REGN VRTX BIIB "
    # Energy
    "XOM CVX COP HAL SLB "
    # Defense
    "BA LMT RTX NOC "
    # Telecom
    "VZ T TMUS "
    # Meme / Retail
    "GME AMC BBBY "
    # High-vol momentum names
    "PLTR UBER LYFT ABNB DASH DKNG SOUN JOBY RKT SMCI MSTR "
    # Options / trading terms
    "calls puts earnings price target breakout resistance support "
    "moving average VWAP short squeeze gamma squeeze."
)
INITIAL_PROMPT = _PROMPT_BASE
if _finnhub_trending:
    INITIAL_PROMPT += f" Trending: {' '.join(_finnhub_trending)}."
if _logged_tickers:
    INITIAL_PROMPT += f" Watchlist: {' '.join(sorted(_logged_tickers))}."


# ========================= MODEL INIT =========================

_whisper_cpu = None   # faster-whisper model (CPU fallback only)

import contextlib as _contextlib

@_contextlib.contextmanager
def _suppress_stderr():
    """Redirect stderr to /dev/null — silences tqdm progress bars from mlx_whisper."""
    # Setup: try to redirect stderr; if it fails, proceed without suppression
    old_stderr = None
    try:
        devnull_fd = _os.open(_os.devnull, _os.O_WRONLY)
        old_stderr = _os.dup(2)
        _os.dup2(devnull_fd, 2)
        _os.close(devnull_fd)
    except Exception:
        pass  # fd tricks failed — yield without suppression
    # Single yield point — no yield inside except, so throw() always stops the generator
    try:
        yield
    finally:
        if old_stderr is not None:
            try:
                _os.dup2(old_stderr, 2)
                _os.close(old_stderr)
            except Exception:
                pass


def _init_whisper():
    if _USE_MLX:
        # MLX loads the model on first transcribe call and caches it internally.
        # Warm it up now with a silent probe so the first real chunk isn't slow.
        print(f"Loading MLX Whisper '{WHISPER_MODEL}' (Apple Silicon)...", flush=True)
        try:
            _probe = np.zeros(1600, dtype=np.float32)
            with _suppress_stderr():
                _mlx_whisper.transcribe(_probe, path_or_hf_repo=WHISPER_MODEL,
                                        language="en", verbose=False)
            print("MLX Whisper ready (GPU/Neural Engine).", flush=True)
        except Exception as e:
            print(f"[WARN] MLX model load failed ({e}) — falling back to small.en CPU", flush=True)
            from faster_whisper import WhisperModel as _FW
            return _FW("small.en", device="cpu", compute_type="int8")
        return None   # MLX is stateless — no model object needed
    else:
        try:
            import ctranslate2 as _ct2
            hw = "cuda" if _ct2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            hw = "cpu"
        ct = "float16" if hw == "cuda" else "int8"
        print(f"Loading Whisper '{WHISPER_MODEL}' on {hw} ({ct})...", flush=True)
        m = WhisperModel(WHISPER_MODEL, device=hw, compute_type=ct)
        print(f"Whisper ready on {hw}.", flush=True)
        return m

_whisper_cpu = _init_whisper()

# Detect optional faster-whisper params (CPU path only)
import inspect as _inspect
_TRANSCRIBE_EXTRAS: dict = {}
if not _USE_MLX and _whisper_cpu:
    try:
        _sig = _inspect.signature(_whisper_cpu.transcribe)
        if "repetition_penalty" in _sig.parameters:
            _TRANSCRIBE_EXTRAS["repetition_penalty"] = 1.1
    except Exception:
        pass
print(f"[INFO] extras: {list(_TRANSCRIBE_EXTRAS) or 'none'}", flush=True)


# ========================= AUDIO SETUP =========================

p = pyaudio.PyAudio()

print("Available audio input devices:")
for i in range(p.get_device_count()):
    dev = p.get_device_info_by_index(i)
    if dev["maxInputChannels"] > 0:
        tag = " <- LOOPBACK" if "loopback" in dev["name"].lower() else ""
        print(f"  {i:2d}: {dev['name']}{tag}")

if DEVICE_INDEX is None:
    if _sys.platform == "darwin":
        # Prefer BlackHole loopback device for system audio capture on macOS
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if dev["maxInputChannels"] > 0 and "blackhole" in dev["name"].lower():
                DEVICE_INDEX = i
                print(f"Auto-selected BlackHole device {i}: {dev['name']}")
                break
        if DEVICE_INDEX is None:
            print("\nNo BlackHole device found. Install it with: brew install blackhole-2ch")
            print("Then set your system audio output to a Multi-Output Device that includes BlackHole.")
    if DEVICE_INDEX is None:
        try:
            choice = input("\nEnter device index (press Enter for default): ").strip()
            DEVICE_INDEX = int(choice) if choice else p.get_default_input_device_info()["index"]
        except Exception:
            DEVICE_INDEX = p.get_default_input_device_info()["index"]

print(f"Using audio device index: {DEVICE_INDEX}")

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
    print(f"ERROR: Could not open device {DEVICE_INDEX}: {e}")
    print("Update device_index in bot_config.json to one of the devices listed above.")
    p.terminate()
    raise SystemExit(1)


# ========================= AUDIO HELPERS =========================

def _apply_adaptive_gain(mono: np.ndarray) -> np.ndarray:
    """
    Optionally denoise, then scale audio to GAIN_TARGET_RMS and hard-limit.
    Better than a fixed multiplier because BlackHole levels vary with system volume.
    """
    rms = float(np.sqrt(np.mean(mono ** 2)))
    if rms < 1e-8:
        return mono
    gain = min(GAIN_TARGET_RMS / rms, GAIN_MAX)
    out  = mono * gain
    peak = float(np.max(np.abs(out)))
    if peak > 1.0:
        out = out / peak
    return out


def _chunk_has_speech(chunk: np.ndarray) -> bool:
    """
    Return True if the chunk contains actual speech.
    Uses Silero VAD when available; falls back to RMS check.
    """
    if _USE_SILERO_VAD and _silero_vad_model is not None and _silero_get_ts is not None:
        try:
            import torch as _torch
            tensor = _torch.from_numpy(chunk)
            ts = _silero_get_ts(tensor, _silero_vad_model,
                                sampling_rate=TARGET_SR,
                                min_speech_duration_ms=150)
            return len(ts) > 0
        except Exception:
            pass  # fall through to RMS
    # RMS fallback — chunk is already gain-adjusted so 0.02 is a reasonable floor
    return float(np.sqrt(np.mean(chunk ** 2))) > 0.02


# ========================= SHARED STATE =========================

audio_queue  = Queue(maxsize=20)
ticker_queue = Queue(maxsize=200)   # transcription → ticker logger (disk writes)
ollama_queue = Queue(maxsize=10)    # transcription → ollama worker (async LLM correction)
running      = threading.Event()
running.set()


# ========================= WORKER: AUDIO CAPTURE =========================

MAX_BUF_SAMPLES = int(TARGET_SR * 20)   # hard cap: never hold more than 20s of audio


def audio_capture():
    local_buf = np.empty(0, dtype=np.float32)

    while running.is_set():
        try:
            data  = stream.read(READ_FRAMES, exception_on_overflow=False)
            raw   = np.frombuffer(data, dtype=np.float32)
            mono  = raw.reshape(-1, _channels).mean(axis=1)

            if np.sqrt(np.mean(mono ** 2)) < SILENCE_THRESHOLD:
                continue

            # Adaptive gain: scale to target RMS then hard-limit
            mono = _apply_adaptive_gain(mono)

            resampled = resample_poly(mono, RESAMPLE_UP, RESAMPLE_DOWN)
            local_buf = np.concatenate((local_buf, resampled))

            # Safety cap: if buffer grows too large, drop the oldest audio
            if len(local_buf) > MAX_BUF_SAMPLES:
                print("[WARN] Audio buffer too large — dropping old audio", flush=True)
                local_buf = local_buf[-CHUNK_SAMPLES:].copy()

            while len(local_buf) >= CHUNK_SAMPLES:
                chunk     = local_buf[:CHUNK_SAMPLES].copy()
                # .copy() forces a new allocation so the slice doesn't keep the
                # old large array alive in memory (numpy view leak prevention)
                local_buf = local_buf[ADVANCE_SAMPLES:].copy()
                try:
                    audio_queue.put_nowait(chunk)
                except Full:
                    pass

        except Exception:
            time.sleep(0.05)


# ========================= WORKER: RESOURCE MONITOR =========================

def resource_monitor(interval=300):
    """Print memory usage every 5 minutes so leaks are visible in the log."""
    while running.is_set():
        for _ in range(interval * 5):   # check running every 0.2s
            if not running.is_set():
                return
            time.sleep(0.2)
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
        q_depth = audio_queue.qsize()
        print(f"[RESOURCE] Memory: {mem_mb:.0f} MB | Queue depth: {q_depth}", flush=True)


# ========================= WORKER: TRANSCRIPTION =========================

# ========================= WORKER: TICKER LOGGER =========================

def ticker_logger_worker():
    """
    Dedicated thread that drains ticker_queue and writes to disk.
    Keeps disk I/O off the transcription hot path.
    """
    while running.is_set():
        try:
            ticker = ticker_queue.get(timeout=0.5)
            if ticker is None:
                break
            log_ticker(ticker)
        except Exception:
            pass


# ========================= WORKER: OLLAMA ASYNC CORRECTOR =========================

def ollama_worker():
    """
    Runs Ollama LLM correction in a background thread — never blocks Whisper.
    Picks up text from ollama_queue, corrects it, and logs any NEW tickers found
    that the raw Whisper extraction missed.
    """
    while running.is_set():
        try:
            text = ollama_queue.get(timeout=1.0)
            if text is None:
                break
            corrected = _llm_correct(text)
            if corrected and corrected != text:
                # Log any tickers the LLM found that raw extraction missed
                for t in extract_tickers(corrected):
                    try:
                        ticker_queue.put_nowait(t)
                    except Full:
                        pass
        except Exception:
            pass


# ========================= OLLAMA LLM POST-PROCESSING =========================
# Optional: runs a local LLM to fix garbled tickers/numbers after Whisper.
# Requires Ollama running locally: https://ollama.com
# Model must be pulled first, e.g.:  ollama pull llama3.2:1b
# Set to "" to disable entirely.

_OLLAMA_MODEL = "llama3.2:1b"    # fast on Apple Silicon; llama3.2:3b for more accuracy
_OLLAMA_URL   = "http://localhost:11434/api/generate"
_OLLAMA_SYSTEM = (
    "You are a financial transcript corrector. The input is raw speech-to-text from CNBC. "
    "Fix garbled stock ticker symbols (e.g. 'P S O' → 'PLTR', 'envidia' → 'NVDA'), "
    "correct prices and numbers, keep all other words exactly as-is. "
    "Output ONLY the corrected transcript — no explanations, no extra text."
)

def _llm_correct(text: str) -> str:
    """
    Run Ollama to clean up Whisper's output — catches garbles the mishear map missed.
    Returns the original text unchanged if Ollama is not running or exceeds timeout.
    Only called when text is long enough to be worth the round-trip.
    """
    if not _OLLAMA_MODEL or len(text.split()) < 5:
        return text
    try:
        import urllib.request as _ur
        payload = json.dumps({
            "model":  _OLLAMA_MODEL,
            "system": _OLLAMA_SYSTEM,
            "prompt": text,
            "stream": False,
            "options": {"temperature": 0, "num_predict": 300},
        }).encode()
        req = _ur.Request(
            _OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with _ur.urlopen(req, timeout=2) as resp:
            corrected = json.loads(resp.read()).get("response", "").strip()
            return corrected if corrected else text
    except Exception:
        return text   # Ollama not running or too slow — silently fall back


def _is_hallucination(text: str) -> bool:
    """Detect Whisper hallucination loops — same short phrase repeated 4+ times."""
    words = text.split()
    if len(words) < 8:
        return False
    for n in (2, 3, 4):
        for i in range(len(words) - n):
            phrase = tuple(words[i:i + n])
            count  = sum(1 for j in range(len(words) - n + 1)
                         if tuple(words[j:j + n]) == phrase)
            if count >= 4:
                return True
    return False


def transcription_worker():
    # audio_capture already produces correctly-windowed CHUNK_SAMPLES chunks;
    # process each one directly rather than re-sliding over accumulated audio.
    while running.is_set():
        try:
            chunk = audio_queue.get(timeout=1.0)
            if chunk is None:
                break

            # Silero VAD: skip chunks with no real speech (saves a Whisper call)
            if not _chunk_has_speech(chunk):
                continue

            # Noise suppression — one call per chunk at correct 16kHz sample rate.
            # IMPORTANT: use the quietest 0.3s window as the noise reference, NOT the
            # chunk start — using the start caused leading syllables of tickers to be
            # treated as "noise" and subtracted, losing the first character.
            if _USE_NOISEREDUCE:
                try:
                    _t_nr = time.perf_counter()
                    _win = int(TARGET_SR * 0.3)   # 0.3s window = 4800 samples
                    if len(chunk) >= _win * 3:
                        # Score every non-overlapping window by RMS; pick the quietest
                        _n_wins = len(chunk) // _win
                        _rms = [float(np.sqrt(np.mean(chunk[i*_win:(i+1)*_win]**2)))
                                for i in range(_n_wins)]
                        _qi  = int(np.argmin(_rms))
                        _noise_ref = chunk[_qi*_win:(_qi+1)*_win]
                    else:
                        _noise_ref = chunk[:_win]
                    chunk = _nr.reduce_noise(
                        y=chunk, sr=TARGET_SR,
                        y_noise=_noise_ref,
                        stationary=False, prop_decrease=0.75,
                    )
                    print(f"[TIME] noisereduce: {(time.perf_counter()-_t_nr)*1000:.0f}ms", flush=True)
                except Exception:
                    pass

            _t_w = time.perf_counter()
            with np.errstate(divide='ignore', over='ignore', invalid='ignore'):
                if _USE_MLX:
                    with _suppress_stderr():
                        result = _mlx_whisper.transcribe(
                            chunk,
                            path_or_hf_repo=WHISPER_MODEL,
                            language="en",
                            initial_prompt=INITIAL_PROMPT,
                            temperature=0.0,
                            no_speech_threshold=0.3,
                            condition_on_previous_text=False,
                            verbose=False,
                        )
                    text = result.get("text", "").strip()
                else:
                    segments, _ = _whisper_cpu.transcribe(
                        chunk,
                        language="en",
                        initial_prompt=INITIAL_PROMPT,
                        vad_filter=True,
                        beam_size=WHISPER_BEAM_SIZE,
                        temperature=0.0,
                        condition_on_previous_text=False,
                        no_speech_threshold=0.35,
                        **_TRANSCRIBE_EXTRAS,
                    )
                    text = " ".join(s.text.strip() for s in segments).strip()
            print(f"[TIME] whisper: {(time.perf_counter()-_t_w)*1000:.0f}ms", flush=True)

            text = normalize_transcript(text)

            if not text or len(text.split()) < 3:
                continue

            if _is_hallucination(text):
                continue

            print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

            # Extract tickers immediately from raw Whisper output (fast path — no Ollama delay)
            for t in extract_tickers(text):
                try:
                    ticker_queue.put_nowait(t)
                except Full:
                    pass

            # Ollama runs asynchronously — may find additional tickers Whisper garbled
            # Does NOT block the transcription thread; any extra tickers appear ~1-2s later
            try:
                ollama_queue.put_nowait(text)
            except Full:
                pass

        except Exception as e:
            msg = str(e).strip()
            if msg:
                print(f"[WARN] chunk skipped ({type(e).__name__}): {msg}", flush=True)
            time.sleep(0.05)


# ========================= START =========================

threads = [
    threading.Thread(target=audio_capture,        daemon=True, name="audio"),
    threading.Thread(target=transcription_worker, daemon=True, name="transcription-1"),
    threading.Thread(target=ticker_logger_worker, daemon=True, name="ticker-logger"),
    threading.Thread(target=ollama_worker,        daemon=True, name="ollama"),
    threading.Thread(target=resource_monitor,     daemon=True, name="monitor"),
]
for t in threads:
    t.start()

print(f"Listening - tickers will be saved to: {TICKER_LOG_FILE}")
print("Press Ctrl+C to stop.")

try:
    while running.is_set():
        time.sleep(0.2)
except KeyboardInterrupt:
    print("\nShutting down...")
    running.clear()
    for q in (audio_queue, ticker_queue, ollama_queue):
        try:
            q.put_nowait(None)
        except Full:
            pass

stream.stop_stream()
stream.close()
p.terminate()
print("Stopped.")
