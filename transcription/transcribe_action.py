"""
transcribe_action.py — CNBC audio → stock ticker detection
Apple Silicon: MLX Whisper large-v3-turbo (Neural Engine / GPU)
Fallback:      faster-whisper medium.en (CPU)

Pipeline:
  BlackHole 48kHz → resample 3:1 → 16kHz → 1.5s chunks → Whisper → extract tickers → dashboard API

Ticker extraction:
  1. Collapse letter-name phonetics  ("en vee dee ay"  → NVDA)
  2. Collapse hyphen-separated letters ("A-I-I-O"       → AIIO)
  3. Collapse space-separated letters  ("H T T B I Y A" → HTT BIYA)  NASDAQ-aware greedy segmentation
  4. Validate all-caps tokens against NASDAQ/NYSE list (~12k symbols)
  5. POST confirmed tickers to dashboard /api/tickers/add
"""

import argparse
import json
import os as _os
import re
import sys as _sys
import time
import threading
import contextlib
import urllib.request
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
_mlx_whisper = None
_USE_MLX = False

if _sys.platform == "darwin":
    try:
        import mlx_whisper as _mlx_whisper
        _USE_MLX = True
        print("[ENGINE] MLX Whisper — Apple Silicon GPU/Neural Engine", flush=True)
    except ImportError:
        print("[ENGINE] mlx_whisper not found — falling back to faster-whisper CPU", flush=True)

_fw_model = None
if not _USE_MLX:
    try:
        from faster_whisper import WhisperModel as _WhisperModel
    except ImportError:
        print("[ERROR] Neither mlx_whisper nor faster_whisper is installed.", flush=True)
        _sys.exit(1)


# =============================================================================
# AUDIO CONFIG
# =============================================================================

if _sys.platform == "darwin":
    SAMPLE_RATE       = 48000
    RESAMPLE_UP       = 1
    RESAMPLE_DOWN     = 3
    SILENCE_THRESHOLD = 0.0002
    GAIN_TARGET_RMS   = 0.10
    GAIN_MAX          = 25.0
else:
    SAMPLE_RATE       = 44100
    RESAMPLE_UP       = 160
    RESAMPLE_DOWN     = 441
    SILENCE_THRESHOLD = 0.008
    GAIN_TARGET_RMS   = None
    GAIN_MAX          = None

TARGET_SR       = 16000
CHUNK_DURATION  = 1.5
OVERLAP         = 0.5
CHUNK_SAMPLES   = int(TARGET_SR * CHUNK_DURATION)
OVERLAP_SAMPLES = int(TARGET_SR * OVERLAP)
ADVANCE_SAMPLES = CHUNK_SAMPLES - OVERLAP_SAMPLES
READ_FRAMES     = int(SAMPLE_RATE * 0.10)


# =============================================================================
# MODEL CONFIG
# =============================================================================

MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
CPU_MODEL = "medium.en"
CPU_BEAM  = 5

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
    "Tickers are sometimes spelled using NATO phonetic alphabet: "
    "November Vee Delta Alpha for NVDA, "
    "Charlie Romeo Bravo Papa for CRBP, "
    "Bravo Romeo Kilo Romeo for BRKR, "
    "Yankee Mike Alpha Tango for YMAT. "
    "calls puts earnings price target breakout resistance."
)


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
# Only include names where the word is unambiguous as a ticker reference.
_COMPANY_NAMES: dict[str, str] = {
    "apple":     "AAPL",
    "microsoft": "MSFT",
    "nvidia":    "NVDA",
    "amazon":    "AMZN",
    "alphabet":  "GOOGL",
    "netflix":   "NFLX",
    "tesla":     "TSLA",
    "intel":     "INTC",
    "facebook":  "META",
    "palantir":  "PLTR",
    "coinbase":  "COIN",
    "spotify":   "SPOT",
    "roblox":    "RBLX",
    "doordash":  "DASH",
}


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


def _collapse_spaced_letters(text: str) -> str:
    """H T T B I Y A → HTT BIYA  (NASDAQ-aware greedy segmentation)"""
    def replace_run(m):
        letters = m.group(0).split()
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


def extract_tickers(text: str) -> dict[str, int]:
    """Return {ticker: count} for all valid NASDAQ/NYSE tickers found in text."""
    # Expand company names before normalization so they don't interfere with
    # letter-level patterns.
    text_lower = text.lower()
    extra: list[str] = []
    for name, ticker in _COMPANY_NAMES.items():
        if name in text_lower:
            extra.append(ticker)

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
# TICKER DELIVERY — POST to dashboard API
# =============================================================================

_DASHBOARD_URL = "http://localhost:8888"


def _send_ticker(ticker: str, count: int = 1):
    try:
        body = json.dumps({"ticker": ticker, "count": count}).encode()
        req  = urllib.request.Request(
            f"{_DASHBOARD_URL}/api/tickers/add",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2):
            pass
        print(f"  → {ticker}", flush=True)
    except Exception as e:
        print(f"  → {ticker}  (API error: {e})", flush=True)


# =============================================================================
# CONFIG — device index
# =============================================================================

_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--device", type=int, default=None)
_args, _ = _parser.parse_known_args()

_cfg_file = Path(__file__).parent.parent / "bot_config.json"
_saved_device = None
if _cfg_file.exists():
    try:
        _cfg_data = json.loads(_cfg_file.read_text())
        _saved_device = _cfg_data.get("device_index")
    except Exception:
        pass

DEVICE_INDEX = _args.device if _args.device is not None else _saved_device

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
    _loopback_keywords = ("blackhole", "loopback", "multi-output")
    _loopback_idx = None
    for i in range(p.get_device_count()):
        dev = p.get_device_info_by_index(i)
        name_lower = dev["name"].lower()
        if dev["maxInputChannels"] > 0 and any(k in name_lower for k in _loopback_keywords):
            _loopback_idx = i
            if "blackhole" in name_lower:
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

_audio_queue = Queue(maxsize=16)
_running     = threading.Event()
_running.set()


# =============================================================================
# AUDIO HELPERS
# =============================================================================

def _apply_gain(mono: np.ndarray) -> np.ndarray:
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
                    text = " ".join(
                        s.text.strip() for s in segs
                        if s.no_speech_prob < 0.6 and s.avg_logprob > -1.0
                    ).strip()

            ms = (time.perf_counter() - t0) * 1000

            if text:
                print(f"[{time.strftime('%H:%M:%S')}] [{ms:.0f}ms] {text}", flush=True)
                for ticker, count in extract_tickers(text).items():
                    _send_ticker(ticker, count)

        except Exception as e:
            msg = str(e).strip()
            if msg:
                print(f"[WARN] {type(e).__name__}: {msg}", flush=True)
            time.sleep(0.05)


# =============================================================================
# START
# =============================================================================

_N_WORKERS = 1

_threads = [
    threading.Thread(target=audio_capture,          daemon=True, name="audio"),
    *[threading.Thread(target=transcription_worker, daemon=True, name=f"transcription-{i+1}")
      for i in range(_N_WORKERS)],
]
for t in _threads:
    t.start()

engine = f"MLX Whisper ({MLX_MODEL})" if _USE_MLX else f"faster-whisper ({CPU_MODEL})"
print(f"\nListening — ASR: {engine}")
print(f"Chunk: {CHUNK_DURATION}s  Overlap: {OVERLAP}s  SR: {SAMPLE_RATE}Hz → {TARGET_SR}Hz")
print("Press Ctrl+C to stop.\n")

try:
    while _running.is_set():
        time.sleep(0.2)
except KeyboardInterrupt:
    print("\nShutting down ...")
    _running.clear()
    for _ in range(_N_WORKERS):
        try:
            _audio_queue.put_nowait(None)
        except Full:
            pass

stream.stop_stream()
stream.close()
p.terminate()
print("Stopped.")
