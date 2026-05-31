#!/usr/bin/env python3
"""
test_audio_file.py — end-to-end pipeline test against a local audio file.

Loads audio → chunks (same settings as live capture) → MLX Whisper
→ normalize → extract tickers.  Shows per-chunk transcript + tickers
and a final summary.

Usage (from repo root):
    python3 transcription/test_audio_file.py
    python3 transcription/test_audio_file.py "samples/Ticker audio 2.mp3"
    python3 transcription/test_audio_file.py some_file.mp3 --whole
    python3 transcription/test_audio_file.py some_file.mp3 --show-empty

Requires macOS (uses afconvert for MP3 → WAV conversion).
"""

import contextlib
import os
import sys
import argparse
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, str(Path(__file__).parent))
# Import the SAME pure logic production uses (ticker_extract), so this test
# measures real production behaviour — including the hallucination guard.
from ticker_extract import normalize_transcript, extract_tickers, is_hallucination  # noqa: E402

import mlx_whisper

# ── Pipeline config — must match transcribe_action.py ────────────────────────

CHUNK_DURATION  = 1.5
OVERLAP         = 0.5
TARGET_SR       = 16000
CHUNK_SAMPLES   = int(TARGET_SR * CHUNK_DURATION)
OVERLAP_SAMPLES = int(TARGET_SR * OVERLAP)
ADVANCE_SAMPLES = CHUNK_SAMPLES - OVERLAP_SAMPLES  # 1.0s of new audio per chunk

MLX_MODEL = "mlx-community/whisper-large-v3-turbo"

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
    "XOM CVX COP BA LMT RTX VZ T TMUS"
)

DEFAULT_FILE = Path(__file__).parent.parent / "samples" / "Ticker audio 2.mp3"


# ── Audio loading ─────────────────────────────────────────────────────────────

def load_audio_16k(path: Path) -> np.ndarray:
    """Convert any audio file to 16kHz mono float32 via macOS afconvert."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
             str(path), str(tmp_path)],
            check=True, capture_output=True,
        )
        sr, data = wavfile.read(str(tmp_path))
        assert sr == 16000, f"Expected 16kHz, got {sr}Hz"
        if data.dtype == np.int16:
            audio = data.astype(np.float32) / 32768.0
        elif data.dtype == np.int32:
            audio = data.astype(np.float32) / 2147483648.0
        else:
            audio = data.astype(np.float32)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return audio
    finally:
        tmp_path.unlink(missing_ok=True)


# ── Transcription helpers ─────────────────────────────────────────────────────

@contextlib.contextmanager
def _suppress_stderr():
    """Silence MLX Whisper's tqdm progress bars on stderr."""
    with open(os.devnull, "w") as devnull:
        old = os.dup(2)
        os.dup2(devnull.fileno(), 2)
        try:
            yield
        finally:
            os.dup2(old, 2)
            os.close(old)


def _whisper(chunk: np.ndarray) -> str:
    with _suppress_stderr():
        result = mlx_whisper.transcribe(
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
    return result.get("text", "").strip()


def run_chunked(audio: np.ndarray, show_empty: bool = False) -> dict[str, int]:
    n_chunks = max(0, (len(audio) - OVERLAP_SAMPLES) // ADVANCE_SAMPLES)
    print(f"\n{'─'*72}")
    print(f"  {len(audio)/TARGET_SR:.1f}s audio  →  ~{n_chunks} chunks  "
          f"({CHUNK_DURATION}s window / {OVERLAP}s overlap / {CHUNK_DURATION-OVERLAP}s step)")
    print(f"{'─'*72}\n")

    buf = audio.copy()
    chunk_num = 0
    totals: dict[str, int] = {}

    while len(buf) >= CHUNK_SAMPLES:
        chunk     = buf[:CHUNK_SAMPLES]
        buf       = buf[ADVANCE_SAMPLES:]
        chunk_num += 1

        t0  = time.perf_counter()
        raw = _whisper(chunk)
        ms  = (time.perf_counter() - t0) * 1000

        if not raw:
            if show_empty:
                print(f"[{chunk_num:02d}] {ms:>5.0f}ms  (silence)\n")
            continue

        # Hallucination guard — the SAME shared function production calls.
        normalized = normalize_transcript(raw)
        if is_hallucination(raw):
            print(f"[{chunk_num:02d}] {ms:>5.0f}ms  [SKIP hallucination] {normalized[:80]}\n")
            continue
        tickers    = extract_tickers(normalized)

        for t, c in tickers.items():
            totals[t] = totals.get(t, 0) + c

        ticker_str = "  ".join(
            f"{t}×{c}" if c > 1 else t for t, c in sorted(tickers.items())
        )
        print(f"[{chunk_num:02d}] {ms:>5.0f}ms  {raw}")
        if normalized != raw:
            print(f"       norm → {normalized}")
        if ticker_str:
            print(f"       tickers: {ticker_str}")
        print()

    return totals


def run_whole_file(audio: np.ndarray) -> dict[str, int]:
    print(f"\n{'─'*72}")
    print(f"  Whole-file transcription ({len(audio)/TARGET_SR:.1f}s total)")
    print(f"{'─'*72}\n")

    t0  = time.perf_counter()
    raw = _whisper(audio)
    ms  = (time.perf_counter() - t0) * 1000

    normalized = normalize_transcript(raw) if raw else ""
    tickers    = extract_tickers(normalized) if normalized else {}

    print(f"[{ms:.0f}ms] {raw}")
    if normalized != raw:
        print(f"norm → {normalized}")
    return tickers


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test ticker pipeline against an audio file.")
    parser.add_argument("file", nargs="?", default=str(DEFAULT_FILE),
                        help="Audio file to test (default: Ticker audio 2.mp3)")
    parser.add_argument("--whole", action="store_true",
                        help="Transcribe whole file at once instead of chunking")
    parser.add_argument("--show-empty", action="store_true",
                        help="Print chunks with no speech")
    args = parser.parse_args()

    audio_path = Path(args.file)
    if not audio_path.exists():
        # Try relative to repo root
        audio_path = Path(__file__).parent.parent / args.file
    if not audio_path.exists():
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    print(f"[ASR] Loading {MLX_MODEL} ...")
    _whisper(np.zeros(TARGET_SR, dtype=np.float32))  # warm up
    print("[ASR] Ready.\n")

    print(f"[AUDIO] Converting {audio_path.name} to 16kHz mono ...")
    audio = load_audio_16k(audio_path)
    print(f"[AUDIO] {len(audio)/TARGET_SR:.1f}s  ({len(audio):,} samples)\n")

    if args.whole:
        tickers = run_whole_file(audio)
    else:
        tickers = run_chunked(audio, show_empty=args.show_empty)

    if tickers:
        print(f"\n{'═'*72}")
        print("SUMMARY — tickers detected:")
        for ticker, count in sorted(tickers.items(), key=lambda x: -x[1]):
            bar = "█" * min(count, 50)
            print(f"  {ticker:<6}  {count:>4}×  {bar}")
        print()
    else:
        print("\nNo tickers detected.")
