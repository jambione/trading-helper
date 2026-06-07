#!/usr/bin/env python3
"""
benchmark_asr.py — measure the effect of ASR-side biasing on ticker recall.

benchmark.py is text-only: it feeds labeled strings to extract_tickers and so
cannot see anything that happens in the audio -> text step. ASR biasing lives
entirely in that step, so it needs its own harness. This one exercises the full
audio -> text -> tickers path and runs the SAME production extraction
(ticker_extract) on every transcript, so the only variable is how Whisper was
conditioned.

Three conditions per clip:

    A baseline   no initial_prompt, no hotwords
    B prod       initial_prompt = INITIAL_PROMPT          (today's production)
    C biased     initial_prompt + hotwords=<watchlist>    (the prototype)

Ground truth is exact because the audio is synthesized from labeled phrases with
macOS `say` (fully reproducible, no hand-labeling). Synthetic TTS is far cleaner
than live CNBC audio, so with no degradation every condition scores ~100% and
there is nothing to see. Use --snr / --rate to stress the ASR into the error
regime where biasing can actually move the numbers. The SAME degraded clip is
reused across all three conditions, so only the conditioning differs.

To measure the real-world delta, drop labeled real clips into CORPUS as
("", {"NVDA"}, "samples/clip.wav") and they'll be used in place of TTS.

Usage (from repo root):
    venv/bin/python transcription/benchmark_asr.py
    venv/bin/python transcription/benchmark_asr.py --snr 8 --rate 260 --verbose
    venv/bin/python transcription/benchmark_asr.py --model tiny.en --snr 6
    venv/bin/python transcription/benchmark_asr.py --engine mlx          # A/B only
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from scipy.io import wavfile

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from ticker_extract import extract_tickers  # noqa: E402  (same logic production uses)


# ---------------------------------------------------------------------------
# Labeled corpus.  text -> what `say` speaks; expect -> ground-truth tickers.
# Optional 3rd element is a path to a real audio clip used instead of TTS.
# Letter-spelled / NATO / less-common names are where biasing should help most.
# ---------------------------------------------------------------------------
CORPUS: list[tuple] = [
    # plain company names (extraction maps name -> ticker)
    ("Apple and Microsoft are both rallying today",          {"AAPL", "MSFT"}),
    ("Nvidia just hit a new all time high",                  {"NVDA"}),
    ("Tesla and Amazon and Google are all green",            {"TSLA", "AMZN", "GOOGL"}),
    ("Netflix earnings beat expectations",                   {"NFLX"}),
    # letter-spelled (say pronounces single letters: en vee dee ay)
    ("The ticker is N V D A",                                {"NVDA"}),
    ("I am buying T S L A right now",                        {"TSLA"}),
    ("Take a look at A M D today",                           {"AMD"}),
    ("P L T R is breaking out",                              {"PLTR"}),
    ("Watch I N T C into earnings",                          {"INTC"}),
    ("M S F T calls are very active",                        {"MSFT"}),
    # NATO phonetic
    ("The symbol is November Victor Delta Alpha",            {"NVDA"}),
    # mixed in a sentence
    ("Between A A P L and N V D A I prefer Apple",           {"AAPL", "NVDA"}),
    # --- harder: less-common tickers, spelled. Whisper has no strong prior for
    #     these, so they are where hotword biasing should earn its keep. ---
    ("Big move in S O U N this morning",                     {"SOUN"}),
    ("I O N Q is up on quantum news",                        {"IONQ"}),
    ("Watch R G T I and Q B T S today",                      {"RGTI", "QBTS"}),
    ("B B A I broke resistance",                             {"BBAI"}),
    ("R K L B and A S T S are space plays",                  {"RKLB", "ASTS"}),
    ("S M C I is leading the AI server names",               {"SMCI"}),
    ("A V G O and M R V L both reported",                    {"AVGO", "MRVL"}),
    ("L U N R landed a contract",                            {"LUNR"}),
]

# Conditioning text. INITIAL_PROMPT is copied from transcribe_action.py — keep
# in sync if the production prompt changes. It biases toward common large caps.
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
    "November Victor Delta Alpha for NVDA. "
    "calls puts earnings price target breakout resistance."
)

# Hotwords = the watchlist you actually track. The decoder boosts these tokens'
# log-probs (a stronger lever than the prompt, and faster-whisper only). Built
# from every corpus ticker + a common core so the less-common names are biased
# too — which is exactly the case the prompt alone does not cover.
_CORE = ("NVDA AAPL AMD TSLA MSFT AMZN GOOGL META NFLX INTC QCOM AVGO MU PLTR "
         "COIN MSTR SMCI ARM MRVL CRM SNOW NET DDOG CRWD HOOD SOFI SPY QQQ JPM "
         "GS BAC WFC COST HD DIS XOM CVX BA WMT TGT PFE MRNA UNH GME AMC UBER ABNB")
HOTWORDS = " ".join(sorted({*_CORE.split(), *(t for _, e, *_ in CORPUS for t in e)}))


def _ensure_audio(text: str, cache: Path, rate: int | None) -> np.ndarray:
    """Synthesize `text` with macOS `say`; return 16 kHz mono float32 samples."""
    tag = f"_{rate}wpm" if rate else ""
    key = "".join(c if c.isalnum() else "_" for c in text)[:54] + tag
    wav = cache / f"{key}.wav"
    if not wav.exists():
        aiff = cache / f"{key}.aiff"
        say_cmd = ["say"] + (["-r", str(rate)] if rate else []) + ["-o", str(aiff), text]
        subprocess.run(say_cmd, check=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
            check=True, capture_output=True,
        )
        aiff.unlink(missing_ok=True)
    _, data = wavfile.read(wav)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data.astype(np.float32) / 32768.0


def _load_real(path: str) -> np.ndarray:
    """Load a real audio clip (any format afconvert reads) as 16 kHz mono float32."""
    src = (Path(path) if Path(path).is_absolute() else _HERE.parent / path)
    tmp = Path("/tmp/asr_bench") / (src.stem + "_16k.wav")
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(src), str(tmp)],
        check=True, capture_output=True,
    )
    _, data = wavfile.read(tmp)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data.astype(np.float32) / 32768.0


def _degrade(audio: np.ndarray, snr_db: float | None, seed: int) -> np.ndarray:
    """Add Gaussian noise at a target SNR (dB). Same seed -> identical noise, so
    every condition sees the SAME degraded clip and only conditioning varies."""
    if snr_db is None:
        return audio
    rng = np.random.default_rng(seed)
    sig_pow = float(np.mean(audio ** 2)) or 1e-12
    noise_pow = sig_pow / (10 ** (snr_db / 10))
    noise = rng.normal(0.0, np.sqrt(noise_pow), size=audio.shape).astype(np.float32)
    out = audio + noise
    peak = float(np.max(np.abs(out))) or 1.0
    return (out / peak).astype(np.float32) if peak > 1.0 else out


# ---------------------------------------------------------------------------
# Engines: each returns transcript text for (audio, initial_prompt, hotwords).
# ---------------------------------------------------------------------------
def _make_fw(model_name: str):
    from faster_whisper import WhisperModel
    print(f"[BENCH] loading faster-whisper {model_name} (cpu/int8) ...", flush=True)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")

    def transcribe(audio, initial_prompt=None, hotwords=None) -> str:
        segs, _ = model.transcribe(
            audio, language="en", beam_size=5, temperature=0.0,
            vad_filter=True, condition_on_previous_text=False,
            initial_prompt=initial_prompt, hotwords=hotwords,
        )
        return " ".join(s.text.strip() for s in segs).strip()

    return transcribe, True  # supports hotwords


def _make_mlx(model_repo: str):
    import mlx_whisper
    print(f"[BENCH] using MLX {model_repo} (no hotwords support) ...", flush=True)

    def transcribe(audio, initial_prompt=None, hotwords=None) -> str:
        r = mlx_whisper.transcribe(
            audio, path_or_hf_repo=model_repo, language="en",
            initial_prompt=initial_prompt, temperature=0.0,
            condition_on_previous_text=False, verbose=False,
        )
        return (r.get("text") or "").strip()

    return transcribe, False  # no hotwords


def _score(found: dict, expect: set) -> tuple[int, int]:
    """Return (true_positives, false_positives) for one clip."""
    got = set(found)
    return len(got & expect), len(got - expect)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["fw", "mlx"], default="fw")
    ap.add_argument("--model", default=None,
                    help="fw: tiny.en/small.en/medium.en  mlx: hf repo")
    ap.add_argument("--snr", type=float, default=None, help="add noise at this SNR in dB (e.g. 8)")
    ap.add_argument("--rate", type=int, default=None, help="say speech rate, words/min (e.g. 260)")
    ap.add_argument("--verbose", action="store_true", help="per-clip transcripts")
    args = ap.parse_args()

    cache = Path("/tmp/asr_bench"); cache.mkdir(exist_ok=True)

    if args.engine == "fw":
        transcribe, has_hot = _make_fw(args.model or "small.en")
    else:
        transcribe, has_hot = _make_mlx(args.model or "mlx-community/whisper-large-v3-turbo")

    conditions = [("A baseline", None, None), ("B prod    ", INITIAL_PROMPT, None)]
    if has_hot:
        conditions.append(("C biased  ", INITIAL_PROMPT, HOTWORDS))
    else:
        print("[BENCH] engine has no hotwords — running A/B only (prompt is the only lever)")

    # Build clips. Real clips (3rd element) load from disk; the rest are TTS.
    # Degrade once per clip with a fixed seed so all conditions share the audio.
    clips = []
    for i, item in enumerate(CORPUS):
        text, expect = item[0], item[1]
        path = item[2] if len(item) > 2 else None
        audio = _load_real(path) if path else _ensure_audio(text, cache, args.rate)
        clips.append((text or path, expect, _degrade(audio, args.snr, i)))

    total_expected = sum(len(e) for _, e, *_ in CORPUS)
    agg = {name: {"tp": 0, "fp": 0, "ms": 0.0} for name, _, _ in conditions}

    for text, expect, audio in clips:
        if args.verbose:
            print(f"\n--- {text!r}  expect={sorted(expect)}")
        for name, prompt, hot in conditions:
            t0 = time.perf_counter()
            out = transcribe(audio, initial_prompt=prompt, hotwords=hot)
            ms = (time.perf_counter() - t0) * 1000
            found = extract_tickers(out)
            tp, fp = _score(found, expect)
            agg[name]["tp"] += tp; agg[name]["fp"] += fp; agg[name]["ms"] += ms
            if args.verbose:
                miss = sorted(expect - set(found))
                spur = sorted(set(found) - expect)
                print(f"  {name}: tp={tp} fp={fp} {ms:5.0f}ms  miss={miss} spurious={spur}\n"
                      f"            ~ {out!r}")

    n = len(clips)
    print("\n" + "=" * 66)
    print(f"ASR BIASING BENCHMARK — engine={args.engine}  model={args.model or 'default'}  "
          f"clips={n}\n  expected={total_expected}  snr={args.snr}dB  rate={args.rate}wpm")
    print("=" * 66)
    print(f"{'condition':<12} {'recall':>8} {'tp':>4} {'fp':>4} {'avg_ms':>8}")
    print("-" * 66)
    for name, _, _ in conditions:
        a = agg[name]
        recall = a["tp"] / total_expected if total_expected else 0.0
        print(f"{name:<12} {recall:>7.1%} {a['tp']:>4} {a['fp']:>4} {a['ms']/n:>7.0f}")
    print("-" * 66)
    print("recall = ticker mentions recovered; fp = spurious tickers. "
          "Higher recall + lower fp is better.")


if __name__ == "__main__":
    main()
