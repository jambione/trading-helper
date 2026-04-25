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
_NATO_PATTERN = re.compile(rf"(?i)\b{_nato_word}(?:[ \t]+{_nato_word}){{1,4}}\b")


def normalize_transcript(text: str) -> str:
    def collapse_nato(m: re.Match) -> str:
        letters = "".join(_NATO.get(w, "") for w in m.group(0).lower().split())
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


def extract_tickers(text: str) -> list:
    found = []
    seen  = set()

    # All-caps tokens — spoken as ticker letters or caught by normalize_transcript
    for m in _TICKER_RE.finditer(text):
        t = m.group(1)
        if t not in _STOP_WORDS and t not in seen:
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
try:
    _existing = json.loads(TICKER_LOG_FILE.read_text(encoding="utf-8"))
    if isinstance(_existing, list):
        _logged_tickers = {t.strip().upper() for t in _existing if isinstance(t, str) and t.strip()}
except Exception:
    pass


def log_ticker(ticker: str):
    ticker = ticker.upper()
    with _log_lock:
        if ticker in _logged_tickers:
            return
        _logged_tickers.add(ticker)
        snapshot = sorted(_logged_tickers)
    try:
        TICKER_LOG_FILE.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        print(f"[LOG] {ticker}")
    except Exception as e:
        print(f"[LOG] Could not write watchlist: {e}")


# ========================= CONFIG =========================

_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--device", type=int, default=None)
_args, _ = _parser.parse_known_args()

_cfg_file = Path(__file__).parent.parent / "bot_config.json"
_saved_device = None
if _cfg_file.exists():
    try:
        _saved_device = json.loads(_cfg_file.read_text()).get("device_index")
    except Exception:
        pass

DEVICE_INDEX = _args.device if _args.device is not None else _saved_device

WHISPER_MODEL     = "small"
WHISPER_BEAM_SIZE = 3

SAMPLE_RATE       = 44100
TARGET_SR         = 16000
CHUNK_DURATION    = 4.5
OVERLAP           = 1.2
SILENCE_THRESHOLD = 0.009

CHUNK_SAMPLES   = int(TARGET_SR * CHUNK_DURATION)
OVERLAP_SAMPLES = int(TARGET_SR * OVERLAP)
ADVANCE_SAMPLES = CHUNK_SAMPLES - OVERLAP_SAMPLES
READ_FRAMES     = int(SAMPLE_RATE * 0.5)

RESAMPLE_UP   = 160
RESAMPLE_DOWN = 441


# ========================= MODEL INIT =========================

print(f"Loading Whisper '{WHISPER_MODEL}' model...")
whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print("Whisper ready.")


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
    local_buf = np.empty(0, dtype=np.float32)

    while running.is_set():
        try:
            chunk = audio_queue.get(timeout=1.0)
            if chunk is None:
                break

            local_buf = np.concatenate((local_buf, chunk))

            while len(local_buf) >= CHUNK_SAMPLES:
                whisper_in = local_buf[:CHUNK_SAMPLES]
                local_buf  = local_buf[ADVANCE_SAMPLES:]

                segments, _ = whisper.transcribe(
                    whisper_in,
                    language="en",
                    vad_filter=True,
                    beam_size=WHISPER_BEAM_SIZE,
                    temperature=0.0,
                    condition_on_previous_text=False,
                    no_speech_threshold=0.45,
                )
                text = " ".join(s.text.strip() for s in segments).strip()
                text = normalize_transcript(text)

                if not text or len(text.split()) < 4:
                    continue

                print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)

                for ticker in extract_tickers(text):
                    log_ticker(ticker)

        except Exception:
            continue


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
