import argparse
import json
import pyaudiowpatch as pyaudio
import numpy as np
import time
import re
import threading
from faster_whisper import WhisperModel
from scipy.signal import resample_poly
from queue import Queue, Full
from pathlib import Path


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
_NATO_PATTERN = re.compile(rf"(?i)\b{_nato_word}(?:{_NATO_SEP}{_nato_word}){{1,4}}\b")


def normalize_transcript(text: str) -> str:
    def collapse_nato(m: re.Match) -> str:
        words = re.split(r'[\s,]+', m.group(0).lower())
        letters = "".join(_NATO.get(w, "") for w in words if w)
        return letters if 2 <= len(letters) <= 5 else m.group(0)

    text = _NATO_PATTERN.sub(collapse_nato, text)

    def collapse_dots(m: re.Match) -> str:
        letters = m.group(0).replace(".", "").upper()
        return letters if 3 <= len(letters) <= 5 else m.group(0)

    text = re.sub(r'(?<!\w)(?:[A-Za-z]\.){2,5}', collapse_dots, text)

    def collapse_hyphens(m: re.Match) -> str:
        return m.group(0).replace("-", "").upper()

    text = re.sub(r'(?<!\w)(?:[A-Za-z]-){2,4}[A-Za-z](?!\w)', collapse_hyphens, text)

    return text


# ========================= TICKER EXTRACTION =========================

# Uppercase tokens Whisper produces that are not stock tickers
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
    "MANY", "MORE", "MOST", "MOVE", "MUCH", "MUST", "NEXT", "ONLY", "OPEN",
    "OVER", "PAST", "PLAY", "REAL", "SAME", "SELL", "SHOW", "SIDE", "SOME",
    "STOP", "SUCH", "TAKE", "THAN", "THAT", "THEM", "THEN", "THEY", "THIS",
    "TIME", "VERY", "WANT", "WELL", "WHAT", "WHEN", "WILL", "WITH", "WORK",
    "YOUR", "SAID", "SAYS", "TOLD", "TELL", "TALK", "WENT", "GOES", "BOTH",
    "ONCE", "UPON", "SOON", "EVER", "YEAR", "WEEK", "DAYS", "LETS", "PUTS",
    # financial/market terms that aren't tickers
    "ETF", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FDA", "FED", "GDP",
    "CPI", "EPS", "ATH", "ATL", "RSI", "SMA", "EMA", "MACD", "BEAR", "BULL",
    "CALL", "PUTS", "SPAC", "REIT", "BOND", "DEBT", "CASH", "RATE", "RISK",
    "LOSS", "GAIN", "NEWS", "CNBC", "NYSE", "NASDAQ",
    # months / days
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    "JAN", "FEB", "MAR", "APR", "AUG", "SEP", "OCT", "NOV", "DEC",
    # Additional noise words
    "HEY", "HERE", "THANKS", "YEAH", "THAT", "THEIR", "THEM", "THERE", "THESE",
    "THOSE", "STILL", "REALLY", "PRETTY", "MIGHT", "MAYBE", "WHICH", "WHERE",
    "WHICH", "WHERE", "WHEN", "WHILE", "THESE", "THOSE", "THEIR", "THERE",
    "HALT", "HALTED", "CUR", "UCI", "LPG", "RFX", "ROADS", "VWAP", "PRFS", 
    "SNG", "SNDR", "BIYM", "ALERT", "SBL", "UT", "YIA", "BIO", "PPG", "WWW",
    "WWW", "LEVELS", "LEVEL", "STOCKS", "STOCK", "PRICE", "PRICES", "VOLUME",
    "BAGER", "ALERT", "LEVEL", "STOCKS", "PRICE", "MARKET", "TRADE", "SHARES",
}

# Spoken company names → ticker (Whisper capitalizes proper nouns)
_NAME_TO_TICKER = {
    "apple": "AAPL", "tesla": "TSLA", "nvidia": "NVDA", "amazon": "AMZN",
    "google": "GOOGL", "alphabet": "GOOGL", "microsoft": "MSFT",
    "meta": "META", "facebook": "META", "netflix": "NFLX",
    "palantir": "PLTR", "coinbase": "COIN", "robinhood": "HOOD",
    "uber": "UBER", "lyft": "LYFT", "airbnb": "ABNB", "snowflake": "SNOW",
    "salesforce": "CRM", "oracle": "ORCL", "intel": "INTC", "amd": "AMD",
    "qualcomm": "QCOM", "broadcom": "AVGO", "micron": "MU",
    "jpmorgan": "JPM", "goldman": "GS", "morgan stanley": "MS",
    "bank of america": "BAC", "wells fargo": "WFC", "citigroup": "C",
    "visa": "V", "mastercard": "MA", "paypal": "PYPL", "square": "SQ",
    "shopify": "SHOP", "zoom": "ZM", "spotify": "SPOT", "pinterest": "PINS",
    "snap": "SNAP", "twitter": "X", "discord": "DSCO",
    "berkshire": "BRK", "johnson": "JNJ", "pfizer": "PFE", "moderna": "MRNA",
    "unitedhealth": "UNH", "humana": "HUM", "cigna": "CI",
    "exxon": "XOM", "chevron": "CVX", "conocophillips": "COP",
    "boeing": "BA", "lockheed": "LMT", "raytheon": "RTX",
    "walmart": "WMT", "target": "TGT", "costco": "COST", "home depot": "HD",
    "disney": "DIS", "comcast": "CMCSA", "verizon": "VZ", "att": "T",
    "ford": "F", "general motors": "GM", "rivian": "RIVN", "lucid": "LCID",
}

_TICKER_RE = re.compile(r'\b([A-Z]{2,5})\b')
_NAME_RE   = {name: re.compile(rf'\b{re.escape(name)}\b', re.I)
              for name in _NAME_TO_TICKER}

_ticker_universe: set = set()   # populated at startup from Alpaca; empty = fallback mode


def extract_tickers(text: str) -> list:
    found = []
    seen  = set()

    # All-caps tokens — spoken as ticker letters or caught by normalize_transcript
    for m in _TICKER_RE.finditer(text):
        t = m.group(1)
        if t in _STOP_WORDS or t in seen:
            continue
        # If the universe is loaded, only accept known valid US equity symbols
        if _ticker_universe and t not in _ticker_universe:
            continue
        found.append(t)
        seen.add(t)

    # Spoken company names
    lower = text.lower()
    for name, ticker in _NAME_TO_TICKER.items():
        if ticker not in seen and _NAME_RE[name].search(lower):
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
    except Exception:
        pass

DEVICE_INDEX = _args.device if _args.device is not None else _saved_device

# small.en is English-only: same size as small but faster and more accurate for English
WHISPER_MODEL     = "small.en"
WHISPER_BEAM_SIZE = 3

SAMPLE_RATE       = 44100
TARGET_SR         = 16000
CHUNK_DURATION    = 3.0    # shorter chunks = lower latency (was 4.5)
OVERLAP           = 0.7    # enough overlap to catch words split at chunk boundary
SILENCE_THRESHOLD = 0.009

CHUNK_SAMPLES   = int(TARGET_SR * CHUNK_DURATION)
OVERLAP_SAMPLES = int(TARGET_SR * OVERLAP)
ADVANCE_SAMPLES = CHUNK_SAMPLES - OVERLAP_SAMPLES
READ_FRAMES     = int(SAMPLE_RATE * 0.5)

RESAMPLE_UP   = 160
RESAMPLE_DOWN = 441

# Load ticker universe (gates extract_tickers against known valid symbols)
_ticker_universe = _init_universe(_alpaca_key, _alpaca_secret)

# Build initial prompt seeded with the current watchlist so Whisper is primed
# to recognise those specific symbols accurately.
_PROMPT_BASE = (
    "CNBC financial news, stock market trading on NYSE and NASDAQ. "
    "Tickers: AAPL TSLA NVDA AMZN GOOGL MSFT META NFLX PLTR COIN HOOD "
    "UBER LYFT ABNB SNOW CRM ORCL INTC AMD QCOM AVGO MU SPY QQQ IWM "
    "JPM GS MS BAC WFC C V MA PYPL SQ SHOP ZM SPOT PINS SNAP "
    "XOM CVX COP BA LMT RTX WMT TGT COST HD DIS CMCSA VZ T "
    "F GM RIVN LCID PFE MRNA UNH HUM CI BRK JNJ AMGN GILD "
    "Earnings per share, price target, analyst upgrade, buy hold sell, "
    "market cap, S and P five hundred, Dow Jones, breakout, resistance."
)
INITIAL_PROMPT = _PROMPT_BASE + (
    f" Watchlist: {' '.join(sorted(_logged_tickers))}." if _logged_tickers else ""
)


# ========================= MODEL INIT =========================

def _init_whisper():
    """Load model on GPU if available and working, otherwise CPU int8."""
    try:
        import ctranslate2 as _ct2
        hw = "cuda" if _ct2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        hw = "cpu"
    ct = "float16" if hw == "cuda" else "int8"

    print(f"Loading Whisper '{WHISPER_MODEL}' model on {hw} ({ct})...", flush=True)
    m = WhisperModel(WHISPER_MODEL, device=hw, compute_type=ct)

    if hw == "cuda":
        # Force CUDA kernels to load now — catches missing DLLs (cublas, etc.) before
        # the capture loop starts, so we can fall back cleanly instead of error-looping.
        try:
            _probe = np.zeros(1600, dtype=np.float32)
            list(m.transcribe(_probe, beam_size=1)[0])
            print(f"CUDA verified.", flush=True)
        except Exception as e:
            print(f"[WARN] CUDA unavailable ({e}) — falling back to CPU int8", flush=True)
            hw, ct = "cpu", "int8"
            m = WhisperModel(WHISPER_MODEL, device=hw, compute_type=ct)

    print(f"Whisper ready on {hw}.", flush=True)
    return m

whisper = _init_whisper()

# Detect which optional params the installed faster-whisper version supports
import inspect as _inspect
_TRANSCRIBE_EXTRAS: dict = {}
try:
    _sig = _inspect.signature(whisper.transcribe)
    if "repetition_penalty" in _sig.parameters:
        _TRANSCRIBE_EXTRAS["repetition_penalty"] = 1.1
except Exception:
    pass
print(f"[INFO] faster-whisper extras: {list(_TRANSCRIBE_EXTRAS) or 'none'}", flush=True)


# ========================= AUDIO SETUP =========================

p = pyaudio.PyAudio()

print("Available audio input devices:")
for i in range(p.get_device_count()):
    dev = p.get_device_info_by_index(i)
    if dev["maxInputChannels"] > 0:
        tag = " <- LOOPBACK" if "loopback" in dev["name"].lower() else ""
        print(f"  {i:2d}: {dev['name']}{tag}")

if DEVICE_INDEX is None:
    try:
        choice = input("\nEnter device index (press Enter for default): ").strip()
        DEVICE_INDEX = int(choice) if choice else p.get_default_input_device_info()["index"]
    except Exception:
        DEVICE_INDEX = p.get_default_input_device_info()["index"]

print(f"Using audio device index: {DEVICE_INDEX}")

try:
    stream = p.open(
        format=pyaudio.paFloat32,
        channels=2,
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


# ========================= SHARED STATE =========================

audio_queue = Queue(maxsize=12)
running     = threading.Event()
running.set()


# ========================= WORKER: AUDIO CAPTURE =========================

def audio_capture():
    local_buf = np.empty(0, dtype=np.float32)

    while running.is_set():
        try:
            data  = stream.read(READ_FRAMES, exception_on_overflow=False)
            raw   = np.frombuffer(data, dtype=np.float32)
            mono  = raw.reshape(-1, 2).mean(axis=1)

            if np.sqrt(np.mean(mono ** 2)) < SILENCE_THRESHOLD:
                continue

            resampled = resample_poly(mono, RESAMPLE_UP, RESAMPLE_DOWN)
            local_buf = np.concatenate((local_buf, resampled))

            while len(local_buf) >= CHUNK_SAMPLES:
                chunk     = local_buf[:CHUNK_SAMPLES].copy()
                local_buf = local_buf[ADVANCE_SAMPLES:]
                try:
                    audio_queue.put_nowait(chunk)
                except Full:
                    pass

        except Exception:
            time.sleep(0.05)


# ========================= WORKER: TRANSCRIPTION =========================

def transcription_worker():
    # audio_capture already produces correctly-windowed CHUNK_SAMPLES chunks;
    # process each one directly rather than re-sliding over accumulated audio.
    while running.is_set():
        try:
            chunk = audio_queue.get(timeout=1.0)
            if chunk is None:
                break

            segments, _ = whisper.transcribe(
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
            text = normalize_transcript(text)

            if not text or len(text.split()) < 3:
                continue

            print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

            for t in extract_tickers(text):
                log_ticker(t)

        except Exception as e:
            msg = str(e).strip()
            if msg:
                print(f"[WARN] chunk skipped ({type(e).__name__}): {msg}", flush=True)
            time.sleep(0.05)


# ========================= START =========================

threads = [
    threading.Thread(target=audio_capture,        daemon=True, name="audio"),
    threading.Thread(target=transcription_worker, daemon=True, name="transcription"),
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
    try:
        audio_queue.put_nowait(None)
    except Full:
        pass

stream.stop_stream()
stream.close()
p.terminate()
print("Stopped.")
