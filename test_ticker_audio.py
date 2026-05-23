"""
Offline ticker diagnostic — tests the audio file directly without starting the live capture.
Usage:  python3 test_ticker_audio.py [path/to/audio.mp3]
"""
import sys, re, json, subprocess, numpy as np, wave, time
from pathlib import Path

# ── Audio file ─────────────────────────────────────────────────────────────
mp3 = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path.home() / "Library/Application Support/Claude/local-agent-mode-sessions"
    "/85e0f41e-8179-4fc3-ae71-aa5e45d4b22b/74ea658c-cb86-4eba-a454-4c29b4fb5fb4"
    "/local_67f2143c-98c2-4b05-8c8d-5b28acf949f8/uploads/ticker 1.mp3"
)
tmp_wav = "/tmp/ticker_test.wav"
print(f"Converting {mp3.name}...")
subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(mp3), tmp_wav],
               check=True, capture_output=True)

with wave.open(tmp_wav) as wf:
    frames = wf.readframes(wf.getnframes())
    audio  = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
print(f"Audio: {len(audio)/16000:.1f}s  RMS={np.sqrt(np.mean(audio**2)):.4f}\n")

# ── Load normalization + extraction logic (no side effects) ─────────────────
tx_path = Path(__file__).parent / "transcription" / "transcribe_action.py"
_src = tx_path.read_text()

# Inline just the parts we need — stops at CONFIG to avoid opening audio devices
_cutoff = _src.index("# ========================= TICKER UNIVERSE =========================")
exec(compile(_src[:_cutoff], str(tx_path), "exec"), _ns := {"__file__": str(tx_path)})

# Load universe cache
import json as _json
_uni_file = Path(__file__).parent / "transcription" / "ticker_universe.json"
ticker_universe: set = set()
if _uni_file.exists():
    try:
        _d = _json.loads(_uni_file.read_text())
        ticker_universe = set(_d.get("symbols", []))
        print(f"Universe: {len(ticker_universe)} symbols loaded")
    except Exception:
        pass
_ns["_ticker_universe"] = ticker_universe

# ── Transcribe ──────────────────────────────────────────────────────────────
from faster_whisper import WhisperModel
print("Loading Whisper small.en...")
model = WhisperModel("small.en", device="cpu", compute_type="int8")

PROMPT = (
    "CNBC financial news, stock market trading on NYSE and NASDAQ. "
    "Tickers: AAPL TSLA NVDA AMZN GOOGL MSFT META NFLX PLTR COIN "
    "UBER AMD QCOM AVGO MU SPY QQQ JPM GS BAC WFC COST HD DIS "
    "XOM CVX COP BA LMT WMT TGT PFE MRNA UNH GME AMC MSTR SMCI "
    "Earnings price target breakout resistance support."
)

print("Transcribing (full file, beam_size=5)...\n")
t0 = time.time()
segs, _ = model.transcribe(audio, language="en", beam_size=5,
                            vad_filter=True, temperature=0.0,
                            initial_prompt=PROMPT,
                            condition_on_previous_text=False,
                            no_speech_threshold=0.35)
full_text = " ".join(s.text.strip() for s in segs)
print(f"Transcription took {time.time()-t0:.1f}s\n")

print("=" * 60)
print("TRANSCRIPT:")
print(full_text)
print()

# ── Normalize + extract ─────────────────────────────────────────────────────
normalize_transcript = _ns["normalize_transcript"]
extract_tickers_fn   = _ns["extract_tickers"]
_ns["_ticker_universe"] = ticker_universe   # make sure universe is injected

norm = normalize_transcript(full_text)
print("NORMALIZED:")
print(norm)
print()

# Monkey-patch universe into the exec'd namespace's global
# Call extract_tickers with universe injected
_TICKER_RE  = re.compile(r'\b([A-Za-z]{2,5})\b')
_STOP_WORDS = _ns["_STOP_WORDS"]
_NAME_TO_TICKER = _ns["_NAME_TO_TICKER"]
_NAME_RE    = {n: re.compile(rf'\b{re.escape(n)}\b', re.I) for n in _NAME_TO_TICKER}

# Mishear map — same as production
_MISHEAR_MAP = {
    "gugsel": "GOOGL", "guggle": "GOOGL", "gugle": "GOOGL", "googel": "GOOGL",
    "hud":    "HOOD",
    "envidia":"NVDA",  "vidia":  "NVDA",
    "tesler": "TSLA",  "palanteer": "PLTR",
    "appal":  "AAPL",  "amazin": "AMZN",
    "microstrategy": "MSTR", "micro strategy": "MSTR",
}
_MISHEAR_RE_T = {k: re.compile(rf'\b{re.escape(k)}\b', re.I) for k in _MISHEAR_MAP}

found, seen = [], set()
lower = norm.lower()

# 1. All tokens (case-insensitive)
for m in _TICKER_RE.finditer(norm):
    t = m.group(1).upper()
    if t in _STOP_WORDS or t in seen:
        continue
    if ticker_universe and t not in ticker_universe:
        continue
    found.append(t); seen.add(t)

# 2. Company names
for name, ticker in _NAME_TO_TICKER.items():
    if ticker not in seen and _NAME_RE[name].search(lower):
        found.append(ticker); seen.add(ticker)

# 3. Mishear corrections
for mishear, ticker in _MISHEAR_MAP.items():
    if ticker not in seen and _MISHEAR_RE_T[mishear].search(lower):
        found.append(ticker); seen.add(ticker)
        print(f"  [MISHEAR] '{mishear}' → {ticker}")

print(f"\nTICKERS FOUND ({len(found)}): {found}")
print()

# Debug: what tokens couldn't be resolved
all_tokens = list(dict.fromkeys(m.group(1).upper() for m in _TICKER_RE.finditer(norm)))
blocked = [t for t in all_tokens if t in _STOP_WORDS]
unknown = [t for t in all_tokens if t not in _STOP_WORDS and ticker_universe and t not in ticker_universe and t not in seen]
print(f"ALL TOKENS (uppercased): {all_tokens}")
print(f"BLOCKED by stop words:   {[t for t in blocked if t not in ('THE','AT','UP','DOWN','IN','OF')]}")
print(f"UNRESOLVED (not caught): {unknown}")
