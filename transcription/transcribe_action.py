"""
transcribe_action.py — CNBC audio → stock ticker detection
Apple Silicon: MLX Whisper large-v3-turbo (Neural Engine / GPU)
Fallback:      faster-whisper medium.en (CPU)

Pipeline (macOS):  ScreenCaptureKit → 48kHz float32 → resample 3:1 → 16kHz → 1.5s chunks → Whisper → extract tickers → dashboard API
Pipeline (Windows): PyAudio loopback → 44.1kHz → resample → 16kHz → 1.5s chunks → Whisper → extract tickers → dashboard API

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
import shutil
import subprocess
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
# TICKER RECOGNITION — imported from ticker_extract (pure, ASR-free)
# =============================================================================
# The recognition logic lives in ticker_extract.py with ZERO ASR/audio deps so
# it imports in milliseconds and can be unit-tested without loading Whisper.
# Re-exported here so this module's public names are unchanged.
_sys.path.insert(0, str(Path(__file__).parent))
from ticker_extract import (   # noqa: E402,F401
    normalize_transcript,
    extract_tickers,
    extract_with_stitch,
    _extract_with_stitch,
    _VALID_TICKERS,
    STITCH_MAX_GAP,
)

# Experimental two-stage "spell pipeline" (silence-segmentation + recognize),
# OFF by default. Toggle via the dashboard (sets SPELL_PIPELINE=1 in the
# transcriber's env at launch). When on, audio_capture endpoints on silence
# instead of fixed 1.5s chunks, and the worker uses recognize() not stitch.
# See transcription/spell_pipeline.py and SESSION_HANDOFF.md.
from spell_pipeline import (   # noqa: E402
    StreamingSegmenter, recognize, load_watchlist, load_seed_tickers, watchlist_prompt)
_USE_SPELL_PIPELINE = _os.environ.get("SPELL_PIPELINE") == "1"

# Watchlist biases BOTH stages in spell pipeline mode: appended to the ASR prompt
# (Stage 1) and used to fuzzy-correct mis-heard spells (Stage 2). It is the UNION
# of two sources, cached and refreshed every ~10s so edits apply without restart:
#   1. config/scanner_watchlist.txt — persistent, user-curated scanner universe
#   2. transcription/ticker_log.csv  — the dashboard's live tracked tickers (purges)
_SEED_FILE      = Path(__file__).parent.parent / "config" / "scanner_watchlist.txt"
_WATCHLIST_FILE = Path(__file__).parent / "ticker_log.csv"
_wl_cache = {"t": 0.0, "wl": set()}


def _get_watchlist() -> set:
    now = time.time()
    if now - _wl_cache["t"] > 10.0:
        _wl_cache["wl"] = load_seed_tickers(_SEED_FILE) | load_watchlist(_WATCHLIST_FILE)
        _wl_cache["t"] = now
    return _wl_cache["wl"]


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
# CONFIG — device index (Windows only; macOS uses ScreenCaptureKit)
# =============================================================================

DEVICE_INDEX = None
if _sys.platform != "darwin":
    _parser = argparse.ArgumentParser(add_help=False)
    _parser.add_argument("--device", type=int, default=None)
    _args, _ = _parser.parse_known_args()

    _cfg_file = Path(__file__).parent.parent / "config" / "bot_config.json"
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

_SCK_PROC  = None
stream     = None
_channels  = 2

if _sys.platform == "darwin":
    # macOS: use ScreenCaptureKit helper to capture system audio directly.
    # No virtual audio device (BlackHole) or Multi-Output Device needed.
    _sck_dir    = Path(__file__).parent
    _sck_binary = _sck_dir / "sck_audio"
    _sck_script = _sck_dir / "sck_audio.swift"
    if _sck_binary.exists():
        _sck_cmd = [str(_sck_binary), str(SAMPLE_RATE), str(_channels)]
        print(f"[AUDIO] macOS — ScreenCaptureKit ({_sck_binary.name})", flush=True)
    elif shutil.which("swift") and _sck_script.exists():
        _sck_cmd = ["swift", str(_sck_script), str(SAMPLE_RATE), str(_channels)]
        print("[AUDIO] macOS — ScreenCaptureKit (swift interpreter; compile sck_audio for faster startup)", flush=True)
    else:
        print("[ERROR] sck_audio binary not found. Rebuild it from transcription/:")
        print("  swiftc sck_audio.swift -o sck_audio")
        raise SystemExit(1)
    _SCK_PROC = subprocess.Popen(_sck_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

else:
    # Windows: use PyAudio loopback device
    p = pyaudio.PyAudio()
    print("\nAvailable audio input devices:")
    for i in range(p.get_device_count()):
        dev = p.get_device_info_by_index(i)
        if dev["maxInputChannels"] > 0:
            tag = " ← LOOPBACK" if "loopback" in dev["name"].lower() else ""
            print(f"  {i:2d}: {dev['name']}{tag}")
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

def _enqueue_chunk(chunk):
    """Put a chunk on the work queue, dropping the oldest if it's full."""
    try:
        _audio_queue.put_nowait(chunk)
    except Full:
        try:
            _audio_queue.get_nowait()
            _audio_queue.put_nowait(chunk)
        except Exception:
            pass


def audio_capture():
    buf = np.empty(0, dtype=np.float32)
    _chunk_bytes = READ_FRAMES * _channels * 4  # 4 bytes per sample (float32 or int32)
    # Spell-pipeline mode: endpoint on silence (variable-length segments) instead
    # of the fixed 1.5s grid below. Built per-stream so state never leaks.
    segmenter = StreamingSegmenter() if _USE_SPELL_PIPELINE else None

    while _running.is_set():
        try:
            if _SCK_PROC is not None:
                data = _SCK_PROC.stdout.read(_chunk_bytes)
                if len(data) < _chunk_bytes:
                    time.sleep(0.05)
                    continue
                # SCKit outputs float32 PCM directly
                raw = np.frombuffer(data, dtype=np.float32)
            else:
                data = stream.read(READ_FRAMES, exception_on_overflow=False)
                raw  = np.frombuffer(data, dtype=np.float32)
            mono = raw.reshape(-1, _channels).mean(axis=1)

            rms = float(np.sqrt(np.mean(mono ** 2)))

            if segmenter is not None:
                # Feed every frame so the segmenter can see the silence GAPS that
                # mark utterance ends. Voicing is ADAPTIVE inside the segmenter
                # (tracks the live noise floor) — a fixed threshold globs reps
                # into one blob and makes Whisper mis-spell. Gain is applied once
                # per emitted segment.
                resampled = resample_poly(mono, RESAMPLE_UP, RESAMPLE_DOWN)
                seg = segmenter.push(resampled)
                if seg is not None:
                    _enqueue_chunk(_apply_gain(seg))
                continue

            if rms < SILENCE_THRESHOLD:
                continue

            mono = _apply_gain(mono)
            resampled = resample_poly(mono, RESAMPLE_UP, RESAMPLE_DOWN)
            buf = np.concatenate((buf, resampled))

            while len(buf) >= CHUNK_SAMPLES:
                chunk = buf[:CHUNK_SAMPLES].copy()
                buf   = buf[ADVANCE_SAMPLES:].copy()
                _enqueue_chunk(chunk)
        except Exception:
            time.sleep(0.05)

    if segmenter is not None:                      # drain final in-progress segment
        seg = segmenter.flush()
        if seg is not None:
            _enqueue_chunk(_apply_gain(seg))


# (cross-chunk stitching now lives in ticker_extract.extract_with_stitch)


# =============================================================================
# WORKER: TRANSCRIPTION
# =============================================================================

def transcription_worker():
    while _running.is_set():
        try:
            chunk = _audio_queue.get(timeout=1.0)
            if chunk is None:
                break

            # Spell-pipeline mode: bias the ASR prompt with the active watchlist
            # (Stage 1) and pass the same watchlist to recognize() (Stage 2).
            if _USE_SPELL_PIPELINE:
                watchlist = _get_watchlist()
                prompt = INITIAL_PROMPT + watchlist_prompt(watchlist)
            else:
                watchlist = None
                prompt = INITIAL_PROMPT

            t0 = time.perf_counter()

            with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
                if _USE_MLX:
                    with _suppress_stderr():
                        result = _mlx_whisper.transcribe(
                            chunk,
                            path_or_hf_repo=MLX_MODEL,
                            language="en",
                            initial_prompt=prompt,
                            temperature=0.0,
                            no_speech_threshold=0.4,
                            compression_ratio_threshold=2.4,
                            condition_on_previous_text=False,
                            verbose=False,
                        )
                    # Drop low-confidence segments (hallucinations on noise/music)
                    # using the same thresholds as the faster-whisper path below.
                    segs = result.get("segments") or []
                    if segs:
                        text = " ".join(
                            s.get("text", "").strip() for s in segs
                            if s.get("no_speech_prob", 0.0) < 0.6
                            and s.get("avg_logprob", 0.0) > -1.0
                        ).strip()
                    else:
                        text = result.get("text", "").strip()
                else:
                    segs, _ = _fw_model.transcribe(
                        chunk,
                        language="en",
                        initial_prompt=prompt,
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
                if _USE_SPELL_PIPELINE:
                    # Stage 2: each silence-bounded segment is one utterance, so
                    # recognize() over it (no cross-chunk stitch needed). The
                    # watchlist fuzzy-corrects mis-heard spells (EOPW -> ELPW).
                    for ticker in recognize(text, watchlist=watchlist):
                        _send_ticker(ticker, 1)
                else:
                    for ticker, count in _extract_with_stitch(text).items():
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

if _SCK_PROC is not None:
    _SCK_PROC.terminate()
    _SCK_PROC.wait(timeout=2)
elif stream is not None:
    stream.stop_stream()
    stream.close()
    p.terminate()
print("Stopped.")
