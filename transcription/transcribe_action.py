import argparse
import json
import os as _os
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
# On macOS: try MLX Whisper (Apple Silicon GPU) first, fall back to faster-whisper.
# On Windows/Linux: faster-whisper only.

_USE_MLX = False
if _sys.platform == "darwin":
    try:
        import mlx_whisper as _mlx_whisper
        _USE_MLX = True
        print("[ENGINE] MLX Whisper — Apple Silicon GPU/Neural Engine", flush=True)
    except ImportError:
        pass

if not _USE_MLX:
    from faster_whisper import WhisperModel
    print("[ENGINE] faster-whisper (CPU)", flush=True)


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
_NATO_SEP   = r"[ \t]*,?[ \t]*"
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
    # financial/market terms that aren't tickers
    "ETF", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FDA", "FED", "GDP",
    "CPI", "EPS", "ATH", "ATL", "RSI", "SMA", "EMA", "MACD", "BEAR", "BULL",
    "CALL", "PUTS", "SPAC", "REIT", "BOND", "DEBT", "CASH", "RATE", "RISK",
    "LOSS", "GAIN", "NEWS", "CNBC", "NYSE", "NASDAQ", "VWAP",
    # months / days
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    "JAN", "FEB", "MAR", "APR", "AUG", "SEP", "OCT", "NOV", "DEC",
    # noise / hallucination artifacts
    "HEY", "THANKS", "YEAH", "THEIR", "THERE", "THESE", "THOSE",
    "STILL", "REALLY", "PRETTY", "MIGHT", "MAYBE", "WHICH", "WHERE", "WHILE",
    "HALT", "HALTED", "ALERT", "LEVELS", "LEVEL", "STOCKS", "STOCK",
    "PRICE", "PRICES", "VOLUME", "MARKET", "TRADE", "SHARES",
    "CUR", "UCI", "LPG", "RFX", "ROADS", "PRFS", "SNG", "SNDR",
    "BIYM", "SBL", "YIA", "PPG", "WWW", "BAGER",
}

_NAME_TO_TICKER = {
    # Big Tech
    "apple": "AAPL", "tesla": "TSLA", "nvidia": "NVDA", "amazon": "AMZN",
    "google": "GOOGL", "alphabet": "GOOGL", "microsoft": "MSFT",
    "meta": "META", "facebook": "META", "netflix": "NFLX",
    # Semis
    "amd": "AMD", "intel": "INTC", "qualcomm": "QCOM", "broadcom": "AVGO",
    "micron": "MU", "arm": "ARM", "arm holdings": "ARM",
    "applied materials": "AMAT", "taiwan semi": "TSM", "tsmc": "TSM",
    "marvell": "MRVL", "super micro": "SMCI", "supermicro": "SMCI",
    # Software / Cloud
    "salesforce": "CRM", "oracle": "ORCL", "snowflake": "SNOW",
    "shopify": "SHOP", "zoom": "ZM", "datadog": "DDOG", "cloudflare": "NET",
    "crowdstrike": "CRWD", "palo alto": "PANW", "fortinet": "FTNT",
    "servicenow": "NOW", "workday": "WDAY", "mongodb": "MDB",
    # Fintech / Payments
    "visa": "V", "mastercard": "MA", "paypal": "PYPL", "square": "SQ",
    "block": "SQ", "affirm": "AFRM", "sofi": "SOFI",
    "robinhood": "HOOD", "robin hood": "HOOD", "coinbase": "COIN",
    "microstrategy": "MSTR", "micro strategy": "MSTR", "strategy": "MSTR",
    # Banks
    "jpmorgan": "JPM", "jp morgan": "JPM", "goldman": "GS",
    "goldman sachs": "GS", "morgan stanley": "MS",
    "bank of america": "BAC", "wells fargo": "WFC", "citigroup": "C",
    "citi": "C", "schwab": "SCHW", "blackrock": "BLK",
    # EV / Autos
    "ford": "F", "general motors": "GM", "gm": "GM",
    "rivian": "RIVN", "lucid": "LCID", "nio": "NIO",
    # Healthcare
    "pfizer": "PFE", "moderna": "MRNA", "johnson": "JNJ",
    "abbvie": "ABBV", "merck": "MRK", "eli lilly": "LLY", "lilly": "LLY",
    "novo nordisk": "NVO", "amgen": "AMGN", "gilead": "GILD",
    "regeneron": "REGN", "vertex": "VRTX", "unitedhealth": "UNH",
    "humana": "HUM", "cigna": "CI", "hims": "HIMS",
    # Energy
    "exxon": "XOM", "chevron": "CVX", "conocophillips": "COP",
    "halliburton": "HAL", "schlumberger": "SLB",
    # Defense
    "boeing": "BA", "lockheed": "LMT", "raytheon": "RTX", "northrop": "NOC",
    # Retail / Consumer
    "walmart": "WMT", "target": "TGT", "costco": "COST",
    "home depot": "HD", "amazon": "AMZN",
    # Media / Entertainment
    "disney": "DIS", "comcast": "CMCSA", "warner": "WBD",
    "paramount": "PARA", "spotify": "SPOT", "netflix": "NFLX",
    "roblox": "RBLX",
    # Telecom
    "verizon": "VZ", "at&t": "T", "att": "T", "t-mobile": "TMUS",
    # Meme / Momentum
    "gamestop": "GME", "game stop": "GME", "amc": "AMC",
    "palantir": "PLTR", "berkshire": "BRK",
    "draftkings": "DKNG", "draft kings": "DKNG",
    "soundhound": "SOUN", "sound hound": "SOUN",
    # Travel
    "airbnb": "ABNB", "uber": "UBER", "lyft": "LYFT", "doordash": "DASH",
    # ETFs
    "spy": "SPY", "qqq": "QQQ", "iwm": "IWM",
}

# Case-insensitive: catches "coin", "rivn", "lcid" etc. spoken in lowercase
_TICKER_RE = re.compile(r'\b([A-Za-z]{2,5})\b')
_NAME_RE   = {name: re.compile(rf'\b{re.escape(name)}\b', re.I)
              for name in _NAME_TO_TICKER}

# Known Whisper misrecognitions → correct ticker
_MISHEAR_MAP = {
    "gugsel": "GOOGL", "guggle": "GOOGL", "gugle": "GOOGL", "googel": "GOOGL",
    "hud": "HOOD",
    "envidia": "NVDA", "vidia": "NVDA", "invidia": "NVDA",
    "nda": "NVDA", "nvia": "NVDA",
    "tesler": "TSLA", "tla": "TSLA",
    "palanteer": "PLTR", "palantir": "PLTR", "palantar": "PLTR",
    "plt": "PLTR", "ltr": "PLTR",
    "appal": "AAPL", "apples": "AAPL", "apl": "AAPL",
    "amazin": "AMZN", "amazons": "AMZN", "amz": "AMZN",
    "microstrategy": "MSTR", "micro strategy": "MSTR",
    "supermicro": "SMCI", "super micro": "SMCI",
    "rivien": "RIVN",
    "crowdstrike": "CRWD", "crowd strike": "CRWD",
    "palo alto": "PANW",
    "cloudflare": "NET",
    "snowflake": "SNOW",
    "coinbase": "COIN",
    "soundhound": "SOUN", "sound hound": "SOUN",
    "draftkings": "DKNG", "draft kings": "DKNG",
    "msf": "MSFT", "mst": "MSFT",
}
_MISHEAR_RE = {k: re.compile(rf'\b{re.escape(k)}\b', re.I) for k in _MISHEAR_MAP}

_ticker_universe: set = set()


def extract_tickers(text: str) -> list:
    found = []
    seen  = set()

    # 2-5 char tokens, uppercase after match (catches lowercase like "coin" → COIN)
    for m in _TICKER_RE.finditer(text):
        t = m.group(1).upper()
        if t in _STOP_WORDS or t in seen:
            continue
        if _ticker_universe and t not in _ticker_universe:
            continue
        found.append(t)
        seen.add(t)

    lower = text.lower()

    # Spoken company names
    for name, ticker in _NAME_TO_TICKER.items():
        if ticker not in seen and _NAME_RE[name].search(lower):
            found.append(ticker)
            seen.add(ticker)

    # Whisper misrecognitions
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
    global _logged_tickers, _log_file_mtime
    try:
        mtime = TICKER_LOG_FILE.stat().st_mtime
        if mtime == _log_file_mtime:
            return
        data = json.loads(TICKER_LOG_FILE.read_text(encoding="utf-8"))
        new_set = {t.strip().upper() for t in data if isinstance(t, str) and t.strip()} if isinstance(data, list) else set()
        _logged_tickers = new_set
        _log_file_mtime = mtime
    except Exception:
        pass


def log_ticker(ticker: str) -> bool:
    global _log_file_mtime
    ticker = ticker.upper()
    with _log_lock:
        _sync_from_file()
        if ticker in _logged_tickers:
            return False
        _logged_tickers.add(ticker)
        snapshot = sorted(_logged_tickers)
        try:
            TICKER_LOG_FILE.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            _log_file_mtime = TICKER_LOG_FILE.stat().st_mtime
            print(f"[LOG] {ticker}", flush=True)
            return True
        except Exception as e:
            print(f"[LOG] Could not write watchlist: {e}")
            return False


# ========================= TICKER UNIVERSE =========================

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
_saved_device = None
_alpaca_key   = ""
_alpaca_secret = ""
_finnhub_key  = ""

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
        pass

DEVICE_INDEX = _args.device if _args.device is not None else _saved_device

# Mac: validate saved device index (Windows index won't exist on Mac)
if DEVICE_INDEX is not None:
    _tmp_p = pyaudio.PyAudio()
    _valid  = [i for i in range(_tmp_p.get_device_count())
               if _tmp_p.get_device_info_by_index(i)["maxInputChannels"] > 0]
    _tmp_p.terminate()
    if DEVICE_INDEX not in _valid:
        print(f"[WARN] Saved device {DEVICE_INDEX} not found — resetting to auto-detect.")
        DEVICE_INDEX = None

# Model config
if _USE_MLX:
    WHISPER_MODEL = "mlx-community/whisper-medium.en-mlx-4bit"  # English-only, 4-bit quantized
else:
    WHISPER_MODEL = "medium.en"   # more accurate than small.en
WHISPER_BEAM_SIZE = 5             # higher beam = more accurate (slower on CPU)

# Audio config — Mac uses BlackHole at 48kHz; Windows uses WASAPI loopback at 44.1kHz
if _sys.platform == "darwin":
    SAMPLE_RATE       = 48000   # BlackHole hardware rate
    RESAMPLE_UP       = 1       # 48000 → 16000 is exactly 3:1
    RESAMPLE_DOWN     = 3
    SILENCE_THRESHOLD = 0.0005  # BlackHole loopback is quiet (~0.001 RMS)
    GAIN_TARGET_RMS   = 0.12    # adaptive gain target
    GAIN_MAX          = 20.0
else:
    SAMPLE_RATE       = 44100
    RESAMPLE_UP       = 160
    RESAMPLE_DOWN     = 441
    SILENCE_THRESHOLD = 0.009
    GAIN_TARGET_RMS   = None    # no gain adjustment on Windows
    GAIN_MAX          = None

TARGET_SR       = 16000
CHUNK_DURATION  = 6.0   # longer window = more context for Whisper = better accuracy
OVERLAP         = 1.5   # 1.5s overlap prevents words from being cut at chunk edges
CHUNK_SAMPLES   = int(TARGET_SR * CHUNK_DURATION)
OVERLAP_SAMPLES = int(TARGET_SR * OVERLAP)
ADVANCE_SAMPLES = CHUNK_SAMPLES - OVERLAP_SAMPLES
READ_FRAMES     = int(SAMPLE_RATE * 0.5)

_ticker_universe = _init_universe(_alpaca_key, _alpaca_secret)

_PROMPT_BASE = (
    "CNBC financial news. NYSE NASDAQ stock tickers: "
    "AAPL MSFT NVDA AMZN GOOGL META NFLX TSLA ORCL "
    "AMD INTC QCOM AVGO MU AMAT TSM ARM MRVL SMCI "
    "CRM SNOW SHOP ZM DDOG CRWD PANW NET WDAY NOW MDB "
    "V MA PYPL SQ HOOD COIN AFRM SOFI MSTR "
    "JPM GS MS BAC WFC C SCHW "
    "SPY QQQ IWM XLF XLE XLK "
    "WMT TGT COST HD DIS CMCSA SPOT RBLX "
    "F GM RIVN LCID NIO "
    "PFE MRNA UNH LLY JNJ ABBV MRK AMGN GILD REGN VRTX "
    "XOM CVX COP BA LMT RTX VZ T TMUS "
    "GME AMC PLTR UBER LYFT ABNB DASH DKNG SOUN MSTR "
    "calls puts earnings price target breakout resistance support."
)
INITIAL_PROMPT = _PROMPT_BASE
if _logged_tickers:
    INITIAL_PROMPT += f" Watchlist: {' '.join(sorted(_logged_tickers))}."


# ========================= MODEL INIT =========================

import contextlib as _contextlib

@_contextlib.contextmanager
def _suppress_stderr():
    """Silence tqdm progress bars from mlx_whisper."""
    old_stderr = None
    try:
        devnull_fd = _os.open(_os.devnull, _os.O_WRONLY)
        old_stderr = _os.dup(2)
        _os.dup2(devnull_fd, 2)
        _os.close(devnull_fd)
    except Exception:
        pass
    try:
        yield
    finally:
        if old_stderr is not None:
            try:
                _os.dup2(old_stderr, 2)
                _os.close(old_stderr)
            except Exception:
                pass


whisper = None  # faster-whisper model object (None when using MLX)

def _init_whisper():
    global whisper
    if _USE_MLX:
        print(f"Loading MLX Whisper '{WHISPER_MODEL}'...", flush=True)
        try:
            _probe = np.zeros(1600, dtype=np.float32)
            with _suppress_stderr():
                _mlx_whisper.transcribe(_probe, path_or_hf_repo=WHISPER_MODEL,
                                        language="en", verbose=False)
            print("MLX Whisper ready.", flush=True)
        except Exception as e:
            print(f"[WARN] MLX Whisper failed ({e})", flush=True)
        return

    try:
        import ctranslate2 as _ct2
        hw = "cuda" if _ct2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        hw = "cpu"
    ct = "float16" if hw == "cuda" else "int8"
    print(f"Loading Whisper '{WHISPER_MODEL}' on {hw} ({ct})...", flush=True)
    whisper = WhisperModel(WHISPER_MODEL, device=hw, compute_type=ct)
    if hw == "cuda":
        try:
            _probe = np.zeros(1600, dtype=np.float32)
            list(whisper.transcribe(_probe, beam_size=1)[0])
            print("CUDA verified.", flush=True)
        except Exception as e:
            print(f"[WARN] CUDA unavailable ({e}) — falling back to CPU int8", flush=True)
            whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    print(f"Whisper ready on {hw}.", flush=True)

_init_whisper()

import inspect as _inspect
_TRANSCRIBE_EXTRAS: dict = {}
if not _USE_MLX and whisper:
    try:
        _sig = _inspect.signature(whisper.transcribe)
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
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if dev["maxInputChannels"] > 0 and "blackhole" in dev["name"].lower():
                DEVICE_INDEX = i
                print(f"Auto-selected BlackHole device {i}: {dev['name']}")
                break
        if DEVICE_INDEX is None:
            print("No BlackHole device found. Install: brew install blackhole-2ch")
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


# ========================= SHARED STATE =========================

audio_queue = Queue(maxsize=12)
running     = threading.Event()
running.set()


# ========================= AUDIO HELPERS =========================

def _apply_gain(mono: np.ndarray) -> np.ndarray:
    """Mac only: scale to GAIN_TARGET_RMS then hard-limit to prevent clipping."""
    if GAIN_TARGET_RMS is None:
        return mono
    rms = float(np.sqrt(np.mean(mono ** 2)))
    if rms < 1e-8:
        return mono
    gain = min(GAIN_TARGET_RMS / rms, GAIN_MAX)
    out  = mono * gain
    peak = float(np.max(np.abs(out)))
    if peak > 1.0:
        out = out / peak
    return out


# ========================= WORKER: AUDIO CAPTURE =========================

def audio_capture():
    local_buf = np.empty(0, dtype=np.float32)

    while running.is_set():
        try:
            data  = stream.read(READ_FRAMES, exception_on_overflow=False)
            raw   = np.frombuffer(data, dtype=np.float32)
            mono  = raw.reshape(-1, _channels).mean(axis=1)

            if np.sqrt(np.mean(mono ** 2)) < SILENCE_THRESHOLD:
                continue

            mono = _apply_gain(mono)
            resampled = resample_poly(mono, RESAMPLE_UP, RESAMPLE_DOWN)
            local_buf = np.concatenate((local_buf, resampled))

            while len(local_buf) >= CHUNK_SAMPLES:
                chunk     = local_buf[:CHUNK_SAMPLES].copy()
                local_buf = local_buf[ADVANCE_SAMPLES:].copy()
                try:
                    audio_queue.put_nowait(chunk)
                except Full:
                    pass

        except Exception:
            time.sleep(0.05)


# ========================= WORKER: TRANSCRIPTION =========================

def transcription_worker():
    while running.is_set():
        try:
            chunk = audio_queue.get(timeout=1.0)
            if chunk is None:
                break

            _t = time.perf_counter()
            with np.errstate(divide='ignore', over='ignore', invalid='ignore'):
                if _USE_MLX:
                    with _suppress_stderr():
                        result = _mlx_whisper.transcribe(
                            chunk,
                            path_or_hf_repo=WHISPER_MODEL,
                            language="en",
                            initial_prompt=INITIAL_PROMPT,
                            temperature=0.0,          # deterministic — no random sampling
                            no_speech_threshold=0.4,  # skip silent/music segments
                            compression_ratio_threshold=2.0,  # skip garbled output
                            condition_on_previous_text=False, # no context bleed between chunks
                            verbose=False,
                        )
                    text = result.get("text", "").strip()
                else:
                    segments, _ = whisper.transcribe(
                        chunk,
                        language="en",
                        initial_prompt=INITIAL_PROMPT,
                        vad_filter=True,              # built-in silence detection
                        beam_size=WHISPER_BEAM_SIZE,  # 5-beam for accuracy
                        temperature=0.0,              # deterministic
                        condition_on_previous_text=False,
                        no_speech_threshold=0.4,
                        compression_ratio_threshold=2.0,
                        **_TRANSCRIBE_EXTRAS,
                    )
                    text = " ".join(s.text.strip() for s in segments).strip()

            print(f"[TIME] {(time.perf_counter()-_t)*1000:.0f}ms", flush=True)

            text = normalize_transcript(text)

            if not text or len(text.split()) < 2:
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
