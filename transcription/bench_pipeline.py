#!/usr/bin/env python3
"""
bench_pipeline.py — end-to-end TWO-STAGE benchmark on the real sample clips.

A/Bs the STAGE-1 model (speed axis: tiny -> turbo) while holding STAGE-2
recognize() fixed (accuracy axis). It tests the core hypothesis behind the
two-stage split: a fast, small model that just captures the characters, paired
with smart recognition, gives SPEED and ACCURACY together — so we don't need
the big model in the hot path.

Metrics per model:
    recall / precision / f1   — accuracy, from Stage-2 on Stage-1's text
    RTF                       — audio_seconds / process_seconds (speed)

Transcripts are cached per (clip, model) so re-scoring a Stage-2 change is
instant (no re-transcription). --no-cache forces re-run.

Run (from repo root):
    venv/bin/python transcription/bench_pipeline.py
    venv/bin/python transcription/bench_pipeline.py --verbose
    venv/bin/python transcription/bench_pipeline.py --models tiny.en,turbo
"""
import argparse
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from spell_pipeline import recognize, make_identifier, segment_on_silence  # noqa: E402
from bench_real import _discover_clips, _load_16k, _md5, _score, _CACHE, _RESULTS  # noqa: E402
from benchmark_asr import INITIAL_PROMPT                       # noqa: E402  (prod long prompt)

# Stage-1 prompt variants to A/B. Isolated segments lose the cross-utterance
# context that makes Whisper format a bare word as a SYMBOL ("ARM" vs "arm");
# a short primer may restore casing without the prod prompt's large-cap bias.
PROMPTS = {
    "none":  None,
    "short": "Stock ticker symbols: ",
    "prod":  INITIAL_PROMPT,
}

# Stage-1 candidates, smallest/fastest first. .en variants are English-only and
# faster than the multilingual ones; turbo is today's production model.
MODELS: dict[str, str] = {
    "tiny.en":  "mlx-community/whisper-tiny.en-mlx",
    "base.en":  "mlx-community/whisper-base.en-mlx",
    "small.en": "mlx-community/whisper-small.en-mlx",
    "turbo":    "mlx-community/whisper-large-v3-turbo",
}


def _cached_identify(identify, digest, audio, model_name, chunk, prompt_name, no_cache, min_sil=300):
    """Whole-clip (chunk=0) or chunked (chunk>0, in seconds) identification.
    Chunked replays the production streaming reality AND avoids the whole-clip
    looping a small model falls into on noisy/non-speech audio. Per-chunk texts
    are joined with spaces; Stage-2 recognize() unions over the joined text.
    Returns (text, proc_seconds, from_cache)."""
    mode_key = "seg" if chunk == "seg" else (f"c{chunk}" if chunk else "whole")
    if chunk == "seg" and min_sil != 300:
        mode_key += f"ms{min_sil}"
    pkey = "" if prompt_name == "none" else f"_p{prompt_name}"
    key = f"{digest[:10]}__pipe_{model_name}_{mode_key}{pkey}.txt"
    cf = _CACHE / key
    if cf.exists() and not no_cache:
        return cf.read_text(), 0.0, True
    t0 = time.perf_counter()
    if chunk == "seg":                               # silence-segmented (VAD-ish)
        spans = segment_on_silence(audio, min_silence_ms=min_sil)
        text = " ".join(identify(audio[a:b]) for a, b in spans)
    elif not chunk:
        text = identify(audio)
    else:
        sr, win = 16000, int(chunk * 16000)
        adv = int((chunk - 0.5) * 16000)            # 0.5s overlap, like production
        parts = []
        for start in range(0, len(audio), adv):
            seg = audio[start:start + win]
            if len(seg) < int(0.3 * sr):
                break
            parts.append(identify(seg))
        text = " ".join(parts)
    sec = time.perf_counter() - t0
    cf.write_text(text)
    return text, sec, False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=None, help="comma list subset of: " + ",".join(MODELS))
    ap.add_argument("--chunk", type=float, default=0.0,
                    help="chunk seconds (0=whole clip). e.g. 3 replays the <2s-spell "
                         "streaming reality and avoids whole-clip small-model looping")
    ap.add_argument("--segment", action="store_true",
                    help="silence-segment the audio (one spell per segment, skip "
                         "silence) instead of fixed chunks — overrides --chunk")
    ap.add_argument("--prompt", choices=list(PROMPTS), default="none",
                    help="Stage-1 initial_prompt variant (default none)")
    ap.add_argument("--seg-min-silence", type=int, default=300,
                    help="min silence gap (ms) to split a segment (default 300); "
                         "larger keeps continuous speech in bigger context windows")
    ap.add_argument("--include-long", action="store_true",
                    help="also run unlabeled holdout clips (e.g. Ticker3.mp3): report "
                         "the found-ticker set + speed, no recall/precision (no labels)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    chunk = "seg" if args.segment else args.chunk
    names = args.models.split(",") if args.models else list(MODELS)
    clips = _discover_clips(include_long=args.include_long)
    if not args.include_long:
        clips = [(n, p, e) for n, p, e in clips if e is not None]
    if not clips:
        print("No clips."); return

    agg = {m: dict(tp=0, fp=0, fn=0, audio_s=0.0, proc_s=0.0) for m in names}
    for model_name in names:
        popt = {} if PROMPTS[args.prompt] is None else {"initial_prompt": PROMPTS[args.prompt]}
        identify = make_identifier(MODELS[model_name], **popt)
        for name, path, expected in clips:
            digest = _md5(path)
            audio = _load_16k(path)
            dur = len(audio) / 16000
            text, sec, cached = _cached_identify(identify, digest, audio, model_name, chunk,
                                                 args.prompt, args.no_cache, args.seg_min_silence)
            found = recognize(text)
            tag = "cache" if cached else f"{sec:.1f}s ({dur/sec:.0f}x)"
            if expected is None:                          # unlabeled holdout: report, don't score
                print(f"[{model_name:<8}] {name}  ({dur:.0f}s audio, {tag})")
                print(f"           found {len(found)} tickers: {sorted(found)}")
                continue
            s = _score(found, expected)
            a = agg[model_name]
            a["tp"] += s["tp"]; a["fp"] += s["fp"]; a["fn"] += s["fn"]; a["audio_s"] += dur
            if not cached:
                a["proc_s"] += sec
            if args.verbose:
                print(f"[{model_name:<8}] {name:<20} R={s['recall']:.0%} P={s['precision']:.0%} "
                      f"({tag}) miss={s['missed']} spurious={s['spurious']}")

    base_mode = "silence-segmented" if chunk == "seg" else (f"chunk={args.chunk}s" if args.chunk else "whole-clip")
    mode = base_mode + (f", prompt={args.prompt}" if args.prompt != "none" else "")
    print("\n" + "=" * 70)
    print(f"TWO-STAGE PIPELINE — Stage-1 model A/B ({mode}, Stage-2 recognize fixed)")
    print("=" * 70)
    print(f"{'model':<10} {'recall':>7} {'prec':>6} {'f1':>6} {'tp':>4} {'fp':>4} {'fn':>4} {'RTF':>8}")
    print("-" * 70)
    stamp = time.strftime("%Y-%m-%d %H:%M")
    journal = [f"# {stamp}  PIPELINE Stage-1 model A/B  {mode}  models={names}"]
    for model_name in names:
        a = agg[model_name]
        tot = a["tp"] + a["fn"]
        recall = a["tp"] / tot if tot else 0.0
        prec = a["tp"] / (a["tp"] + a["fp"]) if (a["tp"] + a["fp"]) else 1.0
        f1 = 2 * prec * recall / (prec + recall) if (prec + recall) else 0.0
        rtf = a["audio_s"] / a["proc_s"] if a["proc_s"] else float("inf")
        rtf_s = f"{rtf:.0f}x" if a["proc_s"] else "cache"
        line = (f"{model_name:<10} {recall:>6.0%} {prec:>6.0%} {f1:>6.0%} "
                f"{a['tp']:>4} {a['fp']:>4} {a['fn']:>4} {rtf_s:>8}")
        print(line)
        journal.append("  " + line)
    print("-" * 70)
    print("Hypothesis: small model + Stage-2 recognize = speed AND accuracy.")
    with _RESULTS.open("a") as fh:
        fh.write("\n".join(journal) + "\n")
    print(f"[BENCH] appended to {_RESULTS.relative_to(_HERE.parent)}")


if __name__ == "__main__":
    main()
