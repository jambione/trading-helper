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
CHUNK_DURATION  = 3.0    # 3s gives Whisper enough sentence context for accuracy
OVERLAP         = 0.5    # 0.5s overlap so words aren't cut at chunk edges
CHUNK_SAMPLES   = int(TARGET_SR * CHUNK_DURATION)
OVERLAP_SAMPLES = int(TARGET_SR * OVERLAP)
ADVANCE_SAMPLES = CHUNK_SAMPLES - OVERLAP_SAMPLES  # 2.5s of new audio per chunk
READ_FRAMES     = int(SAMPLE_RATE * 0.10)   # read 100ms at a time — fills buffer faster


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


def normalize_transcript(text: str) -> str:
    """Collapse NATO phonetics, dot-notation, and hyphen-spelling into ticker candidates."""
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
    return text


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
    # Finance / market terms that look like tickers
    "ETF", "IPO", "CEO", "CFO", "COO", "CTO", "SEC", "FDA", "FED", "GDP",
    "CPI", "EPS", "ATH", "ATL", "RSI", "SMA", "EMA", "BEAR", "BULL",
    "CALL", "PUTS", "SPAC", "REIT", "BOND", "DEBT", "CASH", "RATE", "RISK",
    "LOSS", "GAIN", "NEWS", "CNBC", "NYSE", "VWAP", "MACD",
    "HALT", "ALERT", "LEVEL", "STOCK", "PRICE", "TRADE", "SHARE",
    "OPEN", "HIGH", "CLOSE", "AFTER", "ABOVE", "BELOW", "RANGE",
    "BEAT", "MISS", "GUIDE", "VIEW", "RAISE", "LOWER", "CUTS",
    # Months / days
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    "JAN", "FEB", "MAR", "APR", "AUG", "SEP", "OCT", "NOV", "DEC",
    # Filler / noise
    "HEY", "YEAH", "OKAY", "WELL", "LIKE", "JUST", "SAID",
    "STILL", "REALLY", "PRETTY", "MIGHT", "MAYBE",
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
    "walmart": "WMT", "target": "TGT", "costco": "COST",
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
}

_TICKER_RE  = re.compile(r'\b([A-Za-z]{2,5})\b')
_NAME_RE    = {n: re.compile(rf'\b{re.escape(n)}\b', re.I) for n in _NAME_TO_TICKER}
_MISHEAR_RE = {k: re.compile(rf'\b{re.escape(k)}\b', re.I) for k in _MISHEAR_MAP}


def extract_tickers(text: str) -> list:
    found, seen = [], set()
    lower = text.lower()

    # 1. Token scan — catches explicit uppercase tickers like AAPL, TSLA
    for m in _TICKER_RE.finditer(text):
        t = m.group(1).upper()
        if t not in _STOP_WORDS and t not in seen:
            found.append(t)
            seen.add(t)

    # 2. Company name scan — catches "nvidia", "tesla", "apple" spoken naturally
    for name, ticker in _NAME_TO_TICKER.items():
        if ticker not in seen and _NAME_RE[name].search(lower):
            found.append(ticker)
            seen.add(ticker)

    # 3. Mishear correction — catches Whisper-specific garbles
    for mishear, ticker in _MISHEAR_MAP.items():
        if ticker not in seen and _MISHEAR_RE[mishear].search(lower):
            found.append(ticker)
            seen.add(ticker)

    return found


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

if DEVICE_INDEX is None:
    if _sys.platform == "darwin":
        for i in range(p.get_device_count()):
            dev = p.get_device_info_by_index(i)
            if dev["maxInputChannels"] > 0 and "blackhole" in dev["name"].lower():
                DEVICE_INDEX = i
                print(f"\nAuto-selected BlackHole device {i}: {dev['name']}")
                break
    if DEVICE_INDEX is None:
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

_audio_queue = Queue(maxsize=8)
_running     = threading.Event()
_running.set()

# Session-level dedup — don't spam the dashboard with the same ticker repeatedly
_session_tickers: set = set()
_session_lock = threading.Lock()


# =============================================================================
# TICKER DELIVERY — POST to dashboard API
# =============================================================================

_DASHBOARD_URL = "http://localhost:8888"


def _send_ticker(ticker: str):
    """POST ticker to dashboard. Falls back to writing the file if API is down."""
    ticker = ticker.upper()
    with _session_lock:
        if ticker in _session_tickers:
            return
        _session_tickers.add(ticker)

    try:
        import urllib.request as _req
        body    = json.dumps({"ticker": ticker}).encode()
        request = _req.Request(
            f"{_DASHBOARD_URL}/api/tickers/add",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _req.urlopen(request, timeout=2) as resp:
            result = json.loads(resp.read())
            label  = "" if result.get("is_new", True) else "  (already listed)"
            print(f"  → {ticker}{label}", flush=True)
    except Exception as e:
        # Dashboard not running — write directly in dashboard's timestamped format
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
                    # Queue full — transcription is too slow; drop oldest
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
                            compression_ratio_threshold=2.0,
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
                        compression_ratio_threshold=2.0,
                        condition_on_previous_text=False,
                    )
                    text = " ".join(s.text.strip() for s in segs).strip()

            ms = (time.perf_counter() - t0) * 1000
            print(f"[{time.strftime('%H:%M:%S')}] [{ms:.0f}ms] {text}", flush=True)

            if not text or len(text.split()) < 2:
                continue

            text = normalize_transcript(text)
            tickers = extract_tickers(text)

            for t in tickers:
                _send_ticker(t)

        except Exception as e:
            msg = str(e).strip()
            if msg:
                print(f"[WARN] {type(e).__name__}: {msg}", flush=True)
            time.sleep(0.05)


# =============================================================================
# START
# =============================================================================

_threads = [
    threading.Thread(target=audio_capture,        daemon=True, name="audio"),
    threading.Thread(target=transcription_worker, daemon=True, name="transcription"),
]
for t in _threads:
    t.start()

engine = f"MLX Whisper ({MLX_MODEL})" if _USE_MLX else f"faster-whisper ({CPU_MODEL})"
print(f"\nListening — engine: {engine}")
print(f"Chunk: {CHUNK_DURATION}s  Overlap: {OVERLAP}s  SR: {SAMPLE_RATE}Hz → {TARGET_SR}Hz")
print("Press Ctrl+C to stop.\n")

try:
    while _running.is_set():
        time.sleep(0.2)
except KeyboardInterrupt:
    print("\nShutting down ...")
    _running.clear()
    try:
        _audio_queue.put_nowait(None)
    except Full:
        pass

stream.stop_stream()
stream.close()
p.terminate()
print("Stopped.")
