import argparse
import json
import pyaudiowpatch as pyaudio
import numpy as np
import time
import re
import subprocess
import urllib.request
import ollama
import threading
from faster_whisper import WhisperModel
from scipy.signal import resample_poly
from queue import Queue, Full
from collections import deque
from pathlib import Path


# ========================= OLLAMA STARTUP CHECK =========================

def ensure_ollama_running(timeout: int = 20):
    """
    Ping the Ollama API. If it's not responding, launch `ollama serve`
    and wait up to `timeout` seconds for it to become ready.
    """
    url = "http://localhost:11434/api/tags"

    def is_ready() -> bool:
        try:
            urllib.request.urlopen(url, timeout=2)
            return True
        except Exception:
            return False

    if is_ready():
        print("✅ Ollama already running.\n")
        return

    print("⚡ Ollama not detected — starting ollama serve...")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,  # no console popup on Windows
        )
    except Exception as e:
        print(f"❌ Could not start ollama serve: {e}")
        print("   Make sure Ollama is installed: https://ollama.com/download")
        raise SystemExit(1)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_ready():
            print("✅ Ollama ready.\n")
            return
        time.sleep(0.5)

    print(f"❌ Ollama did not respond within {timeout}s — aborting.")
    raise SystemExit(1)


ensure_ollama_running()


# ========================= TRANSCRIPT NORMALIZATION =========================

# NATO phonetic alphabet → letter mapping
_NATO = {
    "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D",
    "echo": "E", "foxtrot": "F", "golf": "G", "hotel": "H",
    "india": "I", "juliet": "J", "kilo": "K", "lima": "L",
    "mike": "M", "november": "N", "oscar": "O", "papa": "P",
    "quebec": "Q", "romeo": "R", "sierra": "S", "tango": "T",
    "uniform": "U", "victor": "V", "whiskey": "W", "xray": "X",
    "x-ray": "X", "yankee": "Y", "zulu": "Z",
}

# Pre-compiled pattern: 2–5 consecutive NATO words (case-insensitive)
_nato_word = "(?:" + "|".join(re.escape(w) for w in _NATO) + ")"
_NATO_PATTERN = re.compile(
    rf"(?i)\b{_nato_word}(?:[ \t]+{_nato_word}){{1,4}}\b"
)


def normalize_transcript(text: str) -> str:
    """
    Collapses all spelled-out ticker formats into solid uppercase strings
    before the text is printed or sent to the LLM.

      NATO phonetic    Charlie Oscar Sierra        →  COS
                       Alpha Alpha Papa Lima       →  AAPL
                       Tango Sierra Lima Alpha     →  TSLA

      Dot-separated    U.S.A.R.  →  USAR
                       A.A.P.L.  →  AAPL
                       N.V.D.A   →  NVDA  (no trailing dot)

      Hyphen-separated F-C-H-L   →  FCHL
                       T-S-L-A   →  TSLA
    """
    # ── NATO phonetic alphabet ───────────────────────────────────────────────
    def collapse_nato(m: re.Match) -> str:
        words   = m.group(0).lower().split()
        letters = "".join(_NATO.get(w, "") for w in words)
        if 2 <= len(letters) <= 5:
            return letters
        return m.group(0)   # leave unchanged if out of ticker-length range

    text = _NATO_PATTERN.sub(collapse_nato, text)

    # ── Dot-separated: U.S.A.R. or N.V.D.A ─────────────────────────────────
    def collapse_dots(m: re.Match) -> str:
        letters = m.group(0).replace(".", "").upper()
        if 3 <= len(letters) <= 5:   # require 3+ to avoid collapsing "U.S."
            return letters
        return m.group(0)

    text = re.sub(r'(?<!\w)(?:[A-Za-z]\.){2,5}', collapse_dots, text)

    # ── Hyphen-separated: F-C-H-L or T-S-L-A ───────────────────────────────
    def collapse_hyphens(m: re.Match) -> str:
        return m.group(0).replace("-", "").upper()

    text = re.sub(r'(?<!\w)(?:[A-Za-z]-){2,4}[A-Za-z](?!\w)', collapse_hyphens, text)

    return text

# ========================= LOG FILES =========================
TICKER_LOG_FILE     = Path(__file__).parent / "transcribed_stocks.txt"
TRANSCRIPT_LOG_FILE = Path(__file__).parent / "transcript.log"
_log_lock = threading.Lock()

# Load existing tickers into memory so we don't re-add ones from a previous session
_logged_tickers: set = set()
try:
    _existing = TICKER_LOG_FILE.read_text(encoding="utf-8").strip()
    if _existing:
        _logged_tickers = {t.strip().upper() for t in _existing.split(",") if t.strip()}
except Exception:
    pass

# Truncate transcript log at session start so dashboard shows only current run
try:
    TRANSCRIPT_LOG_FILE.write_text("")
except Exception:
    pass


def _write_transcript_log(line: str):
    ts = time.strftime("%H:%M:%S")
    try:
        with open(TRANSCRIPT_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} {line}\n")
    except Exception:
        pass


def log_ticker(ticker: str):
    """Add ticker to in-memory set and persist the full list to transcribed_stocks.txt."""
    ticker = ticker.upper()
    with _log_lock:
        if ticker in _logged_tickers:
            return
        _logged_tickers.add(ticker)
        snapshot = sorted(_logged_tickers)
    try:
        TICKER_LOG_FILE.write_text(",".join(snapshot), encoding="utf-8")
        msg = f"[LOG] {ticker}"
        print(msg)
        _write_transcript_log(msg)
    except Exception as e:
        print(f"[LOG] Could not write ticker log: {e}")


# ========================= CONFIG =========================
# --device N from CLI, else read from bot_config.json, else prompt
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

LLM_MODEL      = "gemma2:2b"
LLM_INTERVAL   = 2.0          # min seconds between LLM calls

WHISPER_MODEL     = "small"   # "tiny" = faster, "small" = more accurate
WHISPER_BEAM_SIZE = 3         # 1 = greedy (fastest); 3 = good balance; 5 = most accurate

SAMPLE_RATE       = 44100
TARGET_SR         = 16000
CHUNK_DURATION    = 4.5       # seconds per Whisper chunk
OVERLAP           = 1.2       # seconds of overlap between chunks
SILENCE_THRESHOLD = 0.009     # RMS below this → skip (checked before resampling)

# Rolling transcript context — how many recent lines to send to the LLM together.
# More lines = better context for ticker detection at the cost of a slightly longer prompt.
TRANSCRIPT_CONTEXT_LINES = 5

# ── Pre-computed constants ────────────────────────────────────────────────────
CHUNK_SAMPLES   = int(TARGET_SR * CHUNK_DURATION)     # 72 000
OVERLAP_SAMPLES = int(TARGET_SR * OVERLAP)            # 19 200
ADVANCE_SAMPLES = CHUNK_SAMPLES - OVERLAP_SAMPLES     # 52 800
READ_FRAMES     = int(SAMPLE_RATE * 0.5)

# 44 100 → 16 000  simplified ratio (GCD=100 → 441/160)
RESAMPLE_UP   = 160
RESAMPLE_DOWN = 441

# ========================= MODEL INIT =========================
print(f"Loading Whisper '{WHISPER_MODEL}' model...")
whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
print("✅ Whisper ready.\n")
print(f"Loading LLM: {LLM_MODEL}...")
print("✅ LLM ready.\n")

# ========================= AUDIO SETUP =========================
p = pyaudio.PyAudio()

print("Available audio input devices:")
for i in range(p.get_device_count()):
    dev = p.get_device_info_by_index(i)
    if dev["maxInputChannels"] > 0:
        tag = " ← LOOPBACK (system audio)" if "loopback" in dev["name"].lower() else ""
        print(f"  {i:2d}: {dev['name']}{tag}")

if DEVICE_INDEX is None:
    try:
        choice = input(
            "\nEnter device index (loopback = system audio, mic = AirPods/built-in; "
            "press Enter for default): "
        ).strip()
        DEVICE_INDEX = int(choice) if choice else p.get_default_input_device_info()["index"]
    except Exception:
        DEVICE_INDEX = p.get_default_input_device_info()["index"]

print(f"✅ Using audio device index: {DEVICE_INDEX}\n")

stream = p.open(
    format=pyaudio.paFloat32,
    channels=2,
    rate=SAMPLE_RATE,
    input=True,
    input_device_index=DEVICE_INDEX,
    frames_per_buffer=READ_FRAMES,
)

# ========================= SHARED STATE =========================
audio_queue    = Queue(maxsize=12)
llm_queue      = Queue(maxsize=8)

running = threading.Event()
running.set()

_llm_time_lock = threading.Lock()
_last_llm_time = 0.0


def _get_llm_time() -> float:
    with _llm_time_lock:
        return _last_llm_time


def _set_llm_time(t: float):
    global _last_llm_time
    with _llm_time_lock:
        _last_llm_time = t


# ========================= WORKER: AUDIO CAPTURE =========================
def audio_capture():
    """
    Reads audio → stereo-to-mono → silence gate → resample to 16 kHz → enqueue.
    Silence is gated BEFORE resampling to skip the expensive resample on quiet frames.
    """
    local_buf = np.empty(0, dtype=np.float32)

    while running.is_set():
        try:
            data  = stream.read(READ_FRAMES, exception_on_overflow=False)
            raw   = np.frombuffer(data, dtype=np.float32)
            mono  = raw.reshape(-1, 2).mean(axis=1)

            # Gate on raw mono — skip resampling entirely if silent
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
                    pass  # drop rather than stall the audio thread

        except Exception:
            time.sleep(0.05)


# ========================= WORKER: TRANSCRIPTION =========================
def transcription_worker():
    """
    Accumulates audio chunks, runs Whisper, and maintains a rolling buffer of
    recent transcript lines. Every new line triggers an LLM check using the
    last TRANSCRIPT_CONTEXT_LINES lines combined — giving the LLM enough
    context to reliably detect tickers that appear across multiple short chunks.
    """
    local_buf         = np.empty(0, dtype=np.float32)
    transcript_window = deque(maxlen=TRANSCRIPT_CONTEXT_LINES)

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

                print(f"[{time.strftime('%H:%M:%S')}] {text}")
                _write_transcript_log(text)

                # Add to rolling window and send combined context to LLM
                transcript_window.append(text)
                if time.time() - _get_llm_time() > LLM_INTERVAL:
                    combined = " ".join(transcript_window)
                    try:
                        llm_queue.put_nowait(combined)
                    except Full:
                        pass

        except Exception:
            continue


# ========================= WORKER: LLM CLASSIFIER =========================
def llm_worker():
    """
    Extracts stock ticker symbols from combined transcript context and logs them.
    Receives the last N transcript lines joined as one string.
    """
    while running.is_set():
        try:
            text = llm_queue.get(timeout=1.0)
            if text is None:
                break

            if time.time() - _get_llm_time() < LLM_INTERVAL:
                continue

            prompt = (
                "You are a stock ticker extractor.\n"
                "Only respond if a clear stock ticker symbol (2-5 uppercase letters "
                "like AAPL, TSLA, UNH, BLD, QXO, CM) is mentioned.\n\n"
                "Reply with EXACTLY one line and nothing else:\n"
                '- The ticker symbol (e.g. "AAPL") if a stock is clearly mentioned\n'
                '- "NO ACTION" otherwise\n\n'
                f"Text: {text}"
            )

            resp = ollama.chat(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.0, "num_ctx": 512, "num_predict": 20},
            )

            _set_llm_time(time.time())

            result = resp["message"]["content"].strip().upper()
            # Accept plain ticker (e.g. "AAPL") or old-style "WATCH AAPL"
            parts = result.split()
            ticker = None
            if len(parts) == 1 and parts[0] not in ("NO", "NO ACTION"):
                ticker = parts[0]
            elif len(parts) >= 2 and parts[0] in ("BUY", "SELL", "WATCH"):
                ticker = parts[1]

            if ticker and 2 <= len(ticker) <= 5 and ticker.isalpha():
                log_ticker(ticker)

        except Exception:
            pass


# ========================= START =========================
threads = [
    threading.Thread(target=audio_capture,        daemon=True, name="audio"),
    threading.Thread(target=transcription_worker, daemon=True, name="transcription"),
    threading.Thread(target=llm_worker,           daemon=True, name="llm"),
]
for t in threads:
    t.start()

print("🎙️  Listening — tickers will be logged to:", TICKER_LOG_FILE)
print("   Press Ctrl+C to stop.\n")

try:
    while running.is_set():
        time.sleep(0.2)
except KeyboardInterrupt:
    print("\nShutting down...")
    running.clear()
    for q in (audio_queue, llm_queue):
        try:
            q.put_nowait(None)
        except Full:
            pass

stream.stop_stream()
stream.close()
p.terminate()
print("✅ Stopped cleanly.")
