import pyaudiowpatch as pyaudio
import numpy as np
import time
import ollama
import threading
from faster_whisper import WhisperModel
from scipy.signal import resample_poly
from queue import Queue, Full
import pyperclip

from workflows import workflow_add_tv, workflow_add_wb, workflow_buy, workflow_sell_all

# ========================= CONFIG =========================
DEVICE_INDEX   = None          # set to an int to skip the prompt

LLM_MODEL      = "gemma2:2b"
LLM_INTERVAL   = 3.0          # min seconds between LLM calls

WHISPER_MODEL     = "small"   # "tiny" = faster, "small" = more accurate
WHISPER_BEAM_SIZE = 3         # 1 = greedy (fastest); 3 = good balance; 5 = most accurate

SAMPLE_RATE       = 44100
TARGET_SR         = 16000
CHUNK_DURATION    = 4.5       # seconds per Whisper chunk
OVERLAP           = 1.2       # seconds of overlap between chunks
SILENCE_THRESHOLD = 0.009     # RMS below this → skip (checked before resampling)

# ── Pre-computed constants (never recalculate inside hot loops) ──────────────
CHUNK_SAMPLES   = int(TARGET_SR * CHUNK_DURATION)     # 72 000 samples
OVERLAP_SAMPLES = int(TARGET_SR * OVERLAP)            # 19 200 samples
ADVANCE_SAMPLES = CHUNK_SAMPLES - OVERLAP_SAMPLES     # 52 800 samples
READ_FRAMES     = int(SAMPLE_RATE * 0.5)              # frames per audio read call

# Simplified resampling ratio  44100 → 16000  (GCD = 100 → 441 / 160)
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
llm_queue      = Queue(maxsize=5)
workflow_queue = Queue(maxsize=10)

running        = threading.Event()
running.set()

_llm_time_lock  = threading.Lock()
_last_llm_time  = 0.0


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
    Read raw audio → stereo-to-mono → silence gate → resample → enqueue.
    All buffers stay as numpy float32; no Python list conversions.
    Silence is checked on the raw mono signal BEFORE the expensive resample step.
    Non-full queue put (put_nowait) drops the chunk rather than blocking this thread.
    """
    local_buf = np.empty(0, dtype=np.float32)

    while running.is_set():
        try:
            data  = stream.read(READ_FRAMES, exception_on_overflow=False)
            raw   = np.frombuffer(data, dtype=np.float32)
            mono  = raw.reshape(-1, 2).mean(axis=1)

            # ── Silence gate — skip resampling entirely if quiet ──────────
            if np.sqrt(np.mean(mono ** 2)) < SILENCE_THRESHOLD:
                continue

            # ── Resample 44 100 → 16 000 Hz ──────────────────────────────
            resampled = resample_poly(mono, RESAMPLE_UP, RESAMPLE_DOWN)
            local_buf = np.concatenate((local_buf, resampled))

            # ── Slice out chunks with overlap and enqueue ─────────────────
            while len(local_buf) >= CHUNK_SAMPLES:
                chunk     = local_buf[:CHUNK_SAMPLES].copy()
                local_buf = local_buf[ADVANCE_SAMPLES:]
                try:
                    audio_queue.put_nowait(chunk)
                except Full:
                    pass   # transcription is behind — drop rather than stall

        except Exception:
            time.sleep(0.05)


# ========================= WORKER: TRANSCRIPTION =========================
def transcription_worker():
    """
    Accumulate audio chunks from the queue, run Whisper on full-length windows,
    and forward non-trivial transcripts to the LLM queue.

    Whisper options:
      beam_size=WHISPER_BEAM_SIZE   — 1 = greedy (3× faster than beam_size=5)
      condition_on_previous_text=False — don't carry context between independent chunks
      no_speech_threshold=0.6       — more aggressively skip non-speech segments
    """
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

                if text and len(text.split()) >= 4:
                    print(f"[{time.strftime('%H:%M:%S')}] 🎤 {text}")
                    if time.time() - _get_llm_time() > LLM_INTERVAL:
                        try:
                            llm_queue.put_nowait(text)
                        except Full:
                            pass

        except Exception:
            continue


# ========================= WORKER: LLM CLASSIFIER =========================
def llm_worker():
    """
    Pull transcripts from llm_queue, classify them with Ollama, and push
    validated (verb, ticker) pairs to workflow_queue.

    num_ctx=512 is plenty for this ~150-token prompt (was 2048).
    """
    while running.is_set():
        try:
            text = llm_queue.get(timeout=1.0)
            if text is None:
                break

            # Re-check interval — a queued item might have waited too long
            if time.time() - _get_llm_time() < LLM_INTERVAL:
                continue

            prompt = (
                "You are a strict stock trading signal extractor.\n"
                "Only respond if a clear stock ticker (3-5 uppercase letters "
                "like AAPL, TSLA, UNH, NVDA) is mentioned.\n\n"
                "Reply with EXACTLY one line and nothing else:\n"
                '- "buy TICKER" if buying is mentioned\n'
                '- "sell TICKER" if selling is mentioned\n'
                '- "watch TICKER" if watching, adding, or tracking is mentioned\n'
                '- "NO ACTION" otherwise\n\n'
                f"Text: {text}"
            )

            resp = ollama.chat(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.0, "num_ctx": 512, "num_predict": 40},
            )

            _set_llm_time(time.time())

            action = resp["message"]["content"].strip().lower()
            parts  = action.split()

            if len(parts) >= 2 and parts[0] in ("buy", "sell", "watch"):
                verb   = parts[0]
                ticker = parts[1].upper()
                if 2 <= len(ticker) <= 5 and ticker.isalpha():
                    print(f"🤖 Signal: {verb.upper()} {ticker}")
                    try:
                        pyperclip.copy(ticker)
                    except Exception:
                        pass
                    try:
                        workflow_queue.put_nowait((verb, ticker))
                    except Full:
                        pass

        except Exception:
            pass


# ========================= WORKER: WORKFLOW EXECUTOR =========================
def workflow_worker():
    """
    Execute GUI automation on its own thread so window-focus delays and
    keystroke timing never block the LLM or transcription threads.
    """
    while running.is_set():
        try:
            item = workflow_queue.get(timeout=1.0)
            if item is None:
                break
            verb, ticker = item

            if verb == "watch":
                workflow_add_tv(ticker)
                time.sleep(0.8)
                workflow_add_wb(ticker)
            elif verb == "buy":
                workflow_buy(ticker)
            elif verb == "sell":
                workflow_sell_all(ticker)

        except Exception:
            pass


# ========================= START =========================
threads = [
    threading.Thread(target=audio_capture,        daemon=True, name="audio"),
    threading.Thread(target=transcription_worker, daemon=True, name="transcription"),
    threading.Thread(target=llm_worker,           daemon=True, name="llm"),
    threading.Thread(target=workflow_worker,      daemon=True, name="workflow"),
]
for t in threads:
    t.start()

print("🎙️  System running")
print("   Speak a ticker + action  (e.g. 'watch AAPL', 'buy TSLA', 'sell UNH')")
print("   Press Ctrl+C to stop.\n")

try:
    while running.is_set():
        time.sleep(0.2)
except KeyboardInterrupt:
    print("\nShutting down...")
    running.clear()
    # Unblock any waiting .get() calls
    for q in (audio_queue, llm_queue, workflow_queue):
        try:
            q.put_nowait(None)
        except Full:
            pass

stream.stop_stream()
stream.close()
p.terminate()
print("✅ Stopped cleanly.")