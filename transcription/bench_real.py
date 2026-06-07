#!/usr/bin/env python3
"""
bench_real.py — A/B benchmark on REAL labeled audio (samples/), tracking ticker
accuracy AND speed across ASR configs so we can build up accuracy over time.

Why a separate harness from benchmark_asr.py: that one synthesizes TTS and adds
noise (good for stress curves, too clean to reflect production). This one runs
the actual recorded clips in samples/ and scores against frozen ground-truth
labels in samples/labels.json.

KEY DESIGN — transcription is the only slow step, so each transcript is CACHED
per (clip, asr-config) under /tmp/asr_bench/transcripts/. Extraction (the part
we iterate on: stopwords, NATO, company names) re-runs every time for free. So:
  - change EXTRACTION logic  -> instant re-score, no re-transcribe
  - change an ASR CONFIG     -> only that config re-transcribes (cache miss)
Pass --no-cache to force re-transcription.

macOS-tuned: engine is MLX (Apple-silicon Metal) large-v3-turbo — the production
Mac path. Speed is reported as RTF (audio_seconds / process_seconds); >1 is
faster than real time.

Usage (from repo root):
    venv/bin/python transcription/bench_real.py
    venv/bin/python transcription/bench_real.py --verbose
    venv/bin/python transcription/bench_real.py --include-long   # also Ticker3 (~58m)
    venv/bin/python transcription/bench_real.py --no-cache
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
from scipy.io import wavfile

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from ticker_extract import (                         # noqa: E402  (production logic)
    extract_tickers, extract_with_stitch, reset_stitch_state)
from benchmark_asr import INITIAL_PROMPT            # noqa: E402  (production prompt)

_SAMPLES = _HERE.parent / "samples"
_CACHE   = Path("/tmp/asr_bench/transcripts"); _CACHE.mkdir(parents=True, exist_ok=True)
_RESULTS = _SAMPLES / "bench_results.log"        # append-only progress journal
_MODEL   = "mlx-community/whisper-large-v3-turbo"

# ---------------------------------------------------------------------------
# ASR configs to compare. All MLX large-v3-turbo (accuracy focus; turbo already
# runs ~35-50x real time so model size is not the lever). Each dict is kwargs
# passed straight to mlx_whisper.transcribe — add rows to A/B new ideas.
# ---------------------------------------------------------------------------
ASR_CONFIGS: dict[str, dict] = {
    "A_noprompt": dict(initial_prompt=None),
    "B_prod":     dict(initial_prompt=INITIAL_PROMPT),
}

# Chunk durations (seconds) to A/B in --mode chunked. Production today is 1.5s
# (transcribe_action.CHUNK_DURATION). At RTF ~40x there is headroom to feed
# Whisper bigger windows for more context; this measures the accuracy payoff vs
# the added latency (~chunk_dur before a ticker can surface). Overlap is held at
# production's 0.5s so only the window size varies.
CHUNK_DURATIONS = [1.5, 3.0, 5.0, 8.0]
PROD_OVERLAP    = 0.5


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _load_16k(path: Path) -> np.ndarray:
    """Decode any audio to 16 kHz mono float32 via afconvert (macOS native)."""
    tmp = Path("/tmp/asr_bench"); tmp.mkdir(exist_ok=True)
    out = tmp / (path.stem.replace(" ", "_") + "_16k.wav")
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                    str(path), str(out)], check=True, capture_output=True)
    _, data = wavfile.read(out)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data.astype(np.float32) / 32768.0


def _discover_clips(include_long: bool) -> list[tuple[str, Path, set | None]]:
    """Return [(name, path, expected|None)], deduped by md5, labels from json."""
    labels = json.loads((_SAMPLES / "labels.json").read_text())
    clips, seen = [], {}
    for path in sorted(_SAMPLES.glob("*.mp3")):
        digest = _md5(path)
        if digest in seen:
            print(f"[BENCH] skip {path.name!r} — identical to {seen[digest]!r}")
            continue
        seen[digest] = path.name
        exp = labels.get(path.name)
        expected = set(exp) if exp else None
        # Long unlabeled holdout: skip unless asked.
        if expected is None and not include_long:
            print(f"[BENCH] skip {path.name!r} — unlabeled holdout (use --include-long)")
            continue
        clips.append((path.name, path, expected))
    return clips


def _make_mlx():
    import mlx_whisper

    def transcribe(audio, **kw) -> str:
        r = mlx_whisper.transcribe(
            audio, path_or_hf_repo=_MODEL, language="en",
            temperature=0.0, condition_on_previous_text=False, verbose=False, **kw)
        return (r.get("text") or "").strip()

    return transcribe


def _cached_transcript(transcribe, name, digest, audio, cfg_name, kw, no_cache):
    """Transcribe (or load cached). Returns (text, seconds, from_cache)."""
    key = f"{digest[:10]}__{cfg_name}.txt"
    cf  = _CACHE / key
    if cf.exists() and not no_cache:
        return cf.read_text(), 0.0, True
    t0 = time.perf_counter()
    text = transcribe(audio, **kw)
    sec = time.perf_counter() - t0
    cf.write_text(text)
    return text, sec, False


def _chunk_transcripts(transcribe, digest, audio, chunk_dur, no_cache):
    """Replay the production chunked path: overlapping `chunk_dur` windows
    advancing (chunk_dur - PROD_OVERLAP). Returns (list_of_chunk_texts,
    proc_seconds, from_cache). The per-chunk text list is cached as JSON so
    re-scoring extraction changes is instant."""
    key = f"{digest[:10]}__chunk_{chunk_dur}_{PROD_OVERLAP}.json"
    cf  = _CACHE / key
    if cf.exists() and not no_cache:
        return json.loads(cf.read_text()), 0.0, True
    sr = 16000
    win = int(chunk_dur * sr)
    adv = int((chunk_dur - PROD_OVERLAP) * sr)
    texts, t0 = [], time.perf_counter()
    for start in range(0, len(audio), adv):
        seg = audio[start:start + win]
        if len(seg) < int(0.3 * sr):     # skip a tiny trailing sliver
            break
        texts.append(transcribe(seg, initial_prompt=INITIAL_PROMPT))
    sec = time.perf_counter() - t0
    cf.write_text(json.dumps(texts))
    return texts, sec, False


def _found_via_stitch(chunk_texts: list[str]) -> set:
    """Run the production extraction path over the chunk stream: per-chunk
    extract_with_stitch (hallucination filter + cross-chunk stitch), unioned."""
    reset_stitch_state()
    found: set = set()
    for txt in chunk_texts:
        found |= set(extract_with_stitch(txt))
    return found


def _score(found: set, expected: set) -> dict:
    tp = len(found & expected)
    fp = len(found - expected)
    fn = len(expected - found)
    recall    = tp / len(expected) if expected else 0.0
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return dict(tp=tp, fp=fp, fn=fn, recall=recall, precision=precision, f1=f1,
                missed=sorted(expected - found), spurious=sorted(found - expected))


def _run_chunked(transcribe, clips, args) -> None:
    """A/B chunk duration over the production chunk+stitch path on labeled clips."""
    labeled = [(n, p, e) for n, p, e in clips if e is not None]
    agg = {cd: dict(tp=0, fp=0, fn=0, audio_s=0.0, proc_s=0.0) for cd in CHUNK_DURATIONS}
    for name, path, expected in labeled:
        digest = _md5(path)
        audio = _load_16k(path)
        dur = len(audio) / 16000
        if args.verbose:
            print(f"\n{'#'*72}\n# {name}  ({dur:.0f}s)  expected={sorted(expected)}")
        for cd in CHUNK_DURATIONS:
            texts, sec, cached = _chunk_transcripts(transcribe, digest, audio, cd, args.no_cache)
            found = _found_via_stitch(texts)
            s = _score(found, expected)
            a = agg[cd]
            a["tp"] += s["tp"]; a["fp"] += s["fp"]; a["fn"] += s["fn"]; a["audio_s"] += dur
            if not cached:
                a["proc_s"] += sec
            if args.verbose:
                tag = "cache" if cached else f"{sec:.0f}s/{len(texts)}ch"
                print(f"  [chunk={cd:>4}s] R={s['recall']:.0%} P={s['precision']:.0%} "
                      f"tp={s['tp']} fp={s['fp']} ({tag})  "
                      f"miss={s['missed']} spurious={s['spurious']}")

    print("\n" + "=" * 72)
    print(f"CHUNKED PRODUCTION-PATH BENCHMARK — engine=mlx {_MODEL.split('/')[-1]}  "
          f"clips={len(labeled)} labeled  (extract_with_stitch + hallucination filter)")
    print("=" * 72)
    print(f"{'chunk_dur':<11} {'recall':>7} {'prec':>6} {'f1':>6} {'tp':>4} {'fp':>4} {'fn':>4} {'RTF':>7} {'~latency':>9}")
    print("-" * 72)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    journal = [f"# {stamp}  MODE=chunked  overlap={PROD_OVERLAP}s  durations={CHUNK_DURATIONS}"]
    for cd in CHUNK_DURATIONS:
        a = agg[cd]
        tot = a["tp"] + a["fn"]
        recall = a["tp"] / tot if tot else 0.0
        prec = a["tp"] / (a["tp"] + a["fp"]) if (a["tp"] + a["fp"]) else 1.0
        f1 = 2 * prec * recall / (prec + recall) if (prec + recall) else 0.0
        rtf = a["audio_s"] / a["proc_s"] if a["proc_s"] else float("inf")
        rtf_s = f"{rtf:.0f}x" if a["proc_s"] else "cache"
        line = (f"{str(cd)+'s':<11} {recall:>6.0%} {prec:>6.0%} {f1:>6.0%} "
                f"{a['tp']:>4} {a['fp']:>4} {a['fn']:>4} {rtf_s:>7} {str(cd)+'s':>9}")
        print(line)
        journal.append("  " + line)
    print("-" * 72)
    print("Production today = 1.5s. ~latency = min delay before a ticker can surface "
          "(≈chunk_dur). Bigger window = more context/recall, more latency.")
    with _RESULTS.open("a") as fh:
        fh.write("\n".join(journal) + "\n")
    print(f"[BENCH] appended summary to {_RESULTS.relative_to(_HERE.parent)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["whole", "chunked"], default="whole",
                    help="whole=transcribe full clip (ceiling); chunked=replay the "
                         "production 1.5s-chunk + stitch path and A/B chunk size")
    ap.add_argument("--verbose", action="store_true", help="per-clip transcripts + diffs")
    ap.add_argument("--include-long", action="store_true", help="also run unlabeled holdouts")
    ap.add_argument("--no-cache", action="store_true", help="force re-transcription")
    args = ap.parse_args()

    clips = _discover_clips(args.include_long)
    if not clips:
        print("No labeled clips found in samples/.")
        return
    transcribe = _make_mlx()

    if args.mode == "chunked":
        _run_chunked(transcribe, clips, args)
        return

    # agg[cfg] across labeled clips
    agg = {c: dict(tp=0, fp=0, fn=0, audio_s=0.0, proc_s=0.0, scored=0) for c in ASR_CONFIGS}

    for name, path, expected in clips:
        digest = _md5(path)
        audio = _load_16k(path)
        dur = len(audio) / 16000
        if args.verbose:
            print(f"\n{'#'*72}\n# {name}  ({dur:.0f}s)  expected={sorted(expected) if expected else 'UNLABELED'}")
        for cfg_name, kw in ASR_CONFIGS.items():
            text, sec, cached = _cached_transcript(
                transcribe, name, digest, audio, cfg_name, kw, args.no_cache)
            found = set(extract_tickers(text))
            tag = "cache" if cached else f"{sec:.1f}s"
            if expected is not None:
                s = _score(found, expected)
                a = agg[cfg_name]
                a["tp"] += s["tp"]; a["fp"] += s["fp"]; a["fn"] += s["fn"]
                a["audio_s"] += dur; a["scored"] += 1
                if not cached:
                    a["proc_s"] += sec
                if args.verbose:
                    print(f"  [{cfg_name:<11}] R={s['recall']:.0%} P={s['precision']:.0%} "
                          f"tp={s['tp']} fp={s['fp']} ({tag})  "
                          f"miss={s['missed']} spurious={s['spurious']}")
                    print(f"      ~ {text[:200]}")
            elif args.verbose:
                print(f"  [{cfg_name:<11}] UNLABELED found={sorted(found)} ({tag})")

    # summary
    print("\n" + "=" * 72)
    print(f"REAL-AUDIO BENCHMARK — engine=mlx {_MODEL.split('/')[-1]}  "
          f"clips={sum(1 for *_, e in clips if e is not None)} labeled")
    print("=" * 72)
    print(f"{'config':<13} {'recall':>7} {'prec':>6} {'f1':>6} {'tp':>4} {'fp':>4} {'fn':>4} {'RTF':>7}")
    print("-" * 72)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    journal = [f"# {stamp}  configs={list(ASR_CONFIGS)}"]
    for cfg_name in ASR_CONFIGS:
        a = agg[cfg_name]
        tot = a["tp"] + a["fn"]
        recall = a["tp"] / tot if tot else 0.0
        prec = a["tp"] / (a["tp"] + a["fp"]) if (a["tp"] + a["fp"]) else 1.0
        f1 = 2 * prec * recall / (prec + recall) if (prec + recall) else 0.0
        rtf = a["audio_s"] / a["proc_s"] if a["proc_s"] else float("inf")
        rtf_s = f"{rtf:.0f}x" if a["proc_s"] else "cache"
        line = (f"{cfg_name:<13} {recall:>6.0%} {prec:>6.0%} {f1:>6.0%} "
                f"{a['tp']:>4} {a['fp']:>4} {a['fn']:>4} {rtf_s:>7}")
        print(line)
        journal.append("  " + line)
    print("-" * 72)
    print("recall=of true tickers found; prec=of found that are true; "
          "RTF=audio/proc (>1 faster than real time).")
    with _RESULTS.open("a") as fh:
        fh.write("\n".join(journal) + "\n")
    print(f"[BENCH] appended summary to {_RESULTS.relative_to(_HERE.parent)}")


if __name__ == "__main__":
    main()
