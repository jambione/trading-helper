#!/usr/bin/env python3
"""
spell_pipeline.py — clean two-stage ticker pipeline, rebuilt from basics.

The audio is almost always a ticker spelled out (letters, NATO words, or the
bare symbol) in under ~2s with no sentence context. So we split the problem
into two stages with opposite goals:

    Stage 1  identify(audio) -> str    FAST. Just capture the characters said.
    Stage 2  recognize(str)  -> set    ACCURATE. Resolve characters to tickers.

Speed lives in Stage 1 (a small model is fine — its only job is getting the
characters). Accuracy lives in Stage 2 (validate against the official list,
fix phonetics). Each rule in Stage 2 must be EARNED by bench_recognize.py:
nothing goes in on faith. We start with the absolute minimum and add a rule
only when the benchmark proves it pays.

This module deliberately reuses NOTHING from ticker_extract.py except the
official VALID_TICKERS set (the NASDAQ/NYSE universe, already cached on disk).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from ticker_extract import VALID_TICKERS   # official NASDAQ/NYSE set (cached file)


# ===========================================================================
# STAGE 2 — recognize(text) -> set[str]
# ---------------------------------------------------------------------------
# v0, the basics: a token that is already an all-caps 1-to-5 letter string AND
# a real ticker. Nothing else. Spelled / NATO / phonetic forms FAIL here on
# purpose — we add handling only when bench_recognize.py shows we need it.
# (Single-letter tickers like "A"/"F" are deferred: bare "I"/"A" in speech are
#  almost never tickers, so the basic floor is 2 chars. Earn 1-char later.)
# ===========================================================================
_TOKEN_RE = re.compile(r"\b[A-Z]{2,5}\b")

# Earned by bench_recognize: all-caps tokens that are valid tickers but, in a
# news/scanner stream, are overwhelmingly the agency/topic word, not the symbol.
# Add a token here ONLY when the benchmark shows it firing as a false positive.
_STOPWORDS: frozenset = frozenset({"CIA", "AI"})


# Closed phonetic alphabets — a spoken letter renders as a single A-Z char, a
# NATO word, or a letter-name. These are complete canonical tables (plus the few
# Whisper mis-hearings the real clips showed, e.g. Mike->Michael), not ad-hoc
# rules. A token contributes one letter to a spell if it matches one of these.
_NATO = {
    "alpha": "A", "bravo": "B", "charlie": "C", "delta": "D", "echo": "E",
    "foxtrot": "F", "golf": "G", "hotel": "H", "india": "I", "juliet": "J",
    "kilo": "K", "lima": "L", "mike": "M", "michael": "M", "november": "N",
    "oscar": "O", "papa": "P", "quebec": "Q", "romeo": "R", "sierra": "S",
    "tango": "T", "uniform": "U", "victor": "V", "whiskey": "W", "xray": "X",
    "yankee": "Y", "zulu": "Z",
}
_LETTER_NAMES = {
    "ay": "A", "bee": "B", "cee": "C", "see": "C", "dee": "D", "ee": "E",
    "ef": "F", "eff": "F", "gee": "G", "aitch": "H", "eye": "I", "jay": "J",
    "kay": "K", "el": "L", "em": "M", "en": "N", "oh": "O", "pee": "P",
    "cue": "Q", "queue": "Q", "ar": "R", "ess": "S", "es": "S", "tee": "T",
    "you": "U", "vee": "V", "ex": "X", "eks": "X", "wye": "Y", "why": "Y",
    "zee": "Z", "zed": "Z",
}


def _letter_of(tok: str) -> str | None:
    t = tok.strip(".,!?").lower()
    if len(t) == 1 and t.isalpha():
        return t.upper()
    return _NATO.get(t) or _LETTER_NAMES.get(t)


def _emit_run(run: list[str], out: set[str]) -> None:
    """Turn an assembled letter run into a candidate symbol. A 2-5 run is one
    spell. A longer run is the scanner spamming ONE ticker back-to-back
    ('E O P W E O P W ...') with no separator between reps — detect the repeated
    2-5 unit and emit it once. Runs that aren't a clean repetition are dropped
    (a safe non-guess; greedy multi-ticker segmentation can be earned later)."""
    n = len(run)
    if 2 <= n <= 5:
        out.add("".join(run))
    elif n > 5:
        for p in range(2, 6):                          # smallest repeating unit, 2-5
            if n % p == 0 and all(run[i] == run[i % p] for i in range(n)):
                out.add("".join(run[:p]))
                return


def _spell_candidates(text: str) -> set[str]:
    """Assemble runs of single spoken letters into candidate symbols.
    'G A M B' -> GAMB ; 'G-A-M-B' / 'E.L.P.W.' -> GAMB / ELPW. A hyphen OR period
    between two letters is a separator (Whisper renders a spelled ticker as
    'E.L.P.W.' or 'E-L-P-W' as often as 'E L P W'). See _emit_run for length
    handling (single spell vs repeated-unit spam)."""
    text = re.sub(r"(?<=[A-Za-z])[.\-](?=[A-Za-z])", " ", text)
    out: set[str] = set()
    run: list[str] = []
    for tok in text.split() + [""]:
        L = _letter_of(tok) if tok else None
        if L:
            run.append(L)
        else:
            _emit_run(run, out)
            run = []
    return out


# Company names spoken as words (price-tape style: "Meta at 528"). Curated to
# UNAMBIGUOUS names only — every entry is matched on word boundaries, and the
# risky bare words are deliberately absent: no "arm"/"coin"/"hood" (body part /
# flip a coin / neighborhood), no "target"/"block"/"square"/"ford". Ambiguous
# tickers are recovered only via a safe full form ("coinbase", "robinhood").
# Each entry is earned by a bench_recognize case (positive or FP-guard negative).
_COMPANY = {
    "apple": "AAPL", "microsoft": "MSFT", "nvidia": "NVDA", "amazon": "AMZN",
    "google": "GOOGL", "alphabet": "GOOGL", "netflix": "NFLX", "tesla": "TSLA",
    "meta": "META", "palantir": "PLTR", "coinbase": "COIN", "robinhood": "HOOD",
    "broadcom": "AVGO", "qualcomm": "QCOM", "intel": "INTC", "micron": "MU",
    "supermicro": "SMCI", "rivian": "RIVN", "lucid": "LCID", "exxon": "XOM",
    "chevron": "CVX", "oracle": "ORCL", "salesforce": "CRM", "moderna": "MRNA",
    "starbucks": "SBUX", "disney": "DIS", "boeing": "BA", "costco": "COST",
    "walmart": "WMT", "pfizer": "PFE", "uber": "UBER", "airbnb": "ABNB",
}
_COMPANY_RE = re.compile(
    r"\b(" + "|".join(sorted(map(re.escape, _COMPANY), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def candidate_tokens(text: str) -> set[str]:
    """The raw candidate strings recognize() considers (all-caps tokens + assembled
    letter spells), before validation/correction. The learner needs these to tally
    unresolved mis-hears."""
    return set(_TOKEN_RE.findall(text)) | _spell_candidates(text)


def _within_edit1(a: str, b: str) -> bool:
    """True if a and b differ by at most one edit (substitution / insertion /
    deletion). Whisper mis-hears one letter of a spell ('ELPW' -> 'EOPW'), so a
    one-edit gap to a known watchlist ticker is the signature to snap on."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if la == lb:                                   # one substitution
        return sum(x != y for x, y in zip(a, b)) == 1
    if abs(la - lb) != 1:                          # >1 length gap is not one indel
        return False
    if la > lb:                                    # make `a` the shorter one
        a, b, la, lb = b, a, lb, la
    i = j = 0
    skipped = False
    while i < la and j < lb:                       # one deletion from the longer
        if a[i] == b[j]:
            i += 1; j += 1
        elif skipped:
            return False
        else:
            skipped = True; j += 1
    return True


def _fuzzy_snap(cand: str, watchlist: set) -> str | None:
    """Snap a mis-heard candidate to a watchlist ticker iff exactly one is within
    one edit. Bounded by the (small, user-controlled) watchlist and a 3-char
    floor, so it can't spray corrections across the whole ticker universe."""
    if len(cand) < 3:
        return None
    hits = [w for w in watchlist if _within_edit1(cand, w)]
    return hits[0] if len(hits) == 1 else None


def recognize(text: str, watchlist: set | None = None,
              corrections: dict | None = None) -> set[str]:
    """Resolve spoken characters to tickers. Resolution order per candidate:
      1. `corrections[c]` — a learned/user-confirmed mapping (DETERMINISTIC: a
         consistent mis-hear like 'EOPW' always becomes 'ELPW', every time).
      2. c is itself a valid ticker.
      3. `watchlist` fuzzy-snap — non-ticker one edit from a single tracked symbol.
    Plus company names spoken as words. Valid tickers are never overridden by the
    fuzzy step (corrections, being explicit, may override anything)."""
    corr = corrections or {}
    wl = {w.upper() for w in watchlist} if watchlist else set()
    raw = set(_TOKEN_RE.findall(text)) | _spell_candidates(text)
    out = {_COMPANY[m.group(1).lower()] for m in _COMPANY_RE.finditer(text)}
    for c in raw:
        if c in _STOPWORDS:
            continue
        if c in corr:                       # 1. deterministic learned/user correction
            out.add(corr[c])
        elif c in VALID_TICKERS:            # 2. already a real ticker
            out.add(c)
        elif wl and (snap := _fuzzy_snap(c, wl)):   # 3. fuzzy-snap to watchlist
            out.add(snap)
    return out


def load_watchlist(path) -> set[str]:
    """Read the dashboard ticker log (JSON list of strings or {ticker,...}) into a
    set of symbols — the user's active watchlist, used to bias both stages.
    Returns an empty set on any error (the pipeline just runs unbiased)."""
    import json
    out: set[str] = set()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        for item in raw if isinstance(raw, list) else []:
            t = item if isinstance(item, str) else item.get("ticker", "")
            t = str(t).strip().upper()
            if 2 <= len(t) <= 5 and t.isalpha():
                out.add(t)
    except Exception:
        pass
    return out


def load_seed_tickers(path) -> set[str]:
    """Read a PERSISTENT seed watchlist: plain text, one ticker per line, blank
    lines and #-comments ignored. The user curates their scanner's known universe
    here so mis-heard spells always have something to snap to. Unlike the live
    ticker log this never purges. Returns an empty set on any error."""
    out: set[str] = set()
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            t = line.split("#", 1)[0].strip().upper()
            if 2 <= len(t) <= 5 and t.isalpha():
                out.add(t)
    except Exception:
        pass
    return out


# ===========================================================================
# LEARNED CORRECTIONS — a persistent, deterministic mis-hear -> ticker map.
# The scanner mis-hears a ticker the SAME way every time within a burst, so once
# we know 'EOPW' means 'ELPW' the fix is exact and permanent (not probabilistic).
# ===========================================================================
def load_corrections(path) -> dict[str, str]:
    """Load {misheard_form: ticker} from JSON. Values must be real tickers; keys
    are upper-cased alpha. Returns {} on any error."""
    import json
    out: dict[str, str] = {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        for k, v in (raw.items() if isinstance(raw, dict) else []):
            k, v = str(k).strip().upper(), str(v).strip().upper()
            if k.isalpha() and 2 <= len(v) <= 5 and v.isalpha() and v in VALID_TICKERS and k != v:
                out[k] = v
    except Exception:
        pass
    return out


def add_correction(path, misheard: str, ticker: str) -> bool:
    """Persist one mapping (misheard -> ticker). The target must be a real ticker.
    Atomic write so the live reader never sees a half file. Returns success."""
    import json, os, tempfile
    misheard, ticker = str(misheard).strip().upper(), str(ticker).strip().upper()
    if not (misheard.isalpha() and 2 <= len(ticker) <= 5 and ticker.isalpha()
            and ticker in VALID_TICKERS and misheard != ticker):
        return False
    cur = load_corrections(path)
    cur[misheard] = ticker
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=2, sort_keys=True)
        os.replace(tmp, str(p))
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        return False
    return True


class CorrectionLearner:
    """Self-supervised learner for consistent mis-hears. It tallies invalid
    candidate tokens it can't resolve; when a real ticker is later heard cleanly,
    any frequently-seen invalid token one edit away from it is recorded as a
    permanent correction. Also exposes the high-count unresolved tokens so a human
    can map them in one click (the reliable path when a clean hearing never comes).
    """

    def __init__(self, path, min_count: int = 3):
        import collections
        self.path = path
        self.min_count = min_count
        self.corrections = load_corrections(path)
        self._unresolved = collections.Counter()

    def reload(self):
        """Pick up corrections added out-of-band (e.g. by the dashboard)."""
        self.corrections = load_corrections(self.path)

    def observe(self, raw_candidates: set, emitted: set) -> list:
        """Feed one utterance's raw candidate tokens and the tickers it emitted.
        Returns any newly-learned (misheard, ticker) pairs."""
        for c in raw_candidates:
            if (c not in self.corrections and c not in VALID_TICKERS
                    and c not in _STOPWORDS and c.isalpha() and 2 <= len(c) <= 5):
                self._unresolved[c] += 1
        learned = []
        anchors = {t for t in emitted if t in VALID_TICKERS}
        for t in anchors:
            for m, cnt in list(self._unresolved.items()):
                if cnt < self.min_count or m == t or not _within_edit1(m, t):
                    continue
                # unambiguous: m is one edit from exactly one emitted ticker
                if sum(1 for a in anchors if _within_edit1(m, a)) != 1:
                    continue
                if add_correction(self.path, m, t):
                    self.corrections[m] = t
                    del self._unresolved[m]
                    learned.append((m, t))
        return learned

    def pending(self, n: int = 10) -> list:
        """High-frequency unresolved tokens, for the dashboard to offer for
        one-click mapping. Excludes anything already corrected."""
        return [{"token": k, "count": c}
                for k, c in self._unresolved.most_common(n) if k not in self.corrections]


# ===========================================================================
# STAGE 1 — identify(audio) -> str
# ---------------------------------------------------------------------------
# Thin wrapper over MLX Whisper (the Apple-silicon path). Model is swappable so
# bench can A/B speed vs character fidelity (tiny -> turbo). No prompt by
# default: with no sentence context a prompt mostly adds latency/bias.
# ===========================================================================
def segment_on_silence(audio, sr: int = 16000, frame_ms: int = 30,
                       min_silence_ms: int = 300, min_speech_ms: int = 200,
                       pad_ms: int = 120, thresh_mult: float = 2.5):
    """Split audio into speech segments on silence gaps (energy-based, numpy only).

    The point: cut at the natural pause AFTER each spoken spell so a segment is
    one complete utterance — never a mid-word split — and skip the silence so we
    only transcribe where someone is talking. That recovers whole-clip accuracy
    at low latency and is far faster on the mostly-silent scanner audio. Returns
    a list of (start_sample, end_sample). Chime/noise that passes the energy gate
    transcribes to junk and is dropped by recognize()'s ticker validation."""
    import numpy as _np
    frame = int(sr * frame_ms / 1000)
    n = len(audio) // frame
    if n == 0:
        return [(0, len(audio))]
    rms = _np.sqrt((audio[:n * frame].reshape(n, frame) ** 2).mean(axis=1) + 1e-12)
    floor = float(_np.percentile(rms, 20))                 # noise floor
    thr = max(floor * thresh_mult, float(rms.max()) * 0.05)
    voiced = rms > thr
    min_sil = max(1, min_silence_ms // frame_ms)
    pad, min_sp = int(pad_ms * sr / 1000), int(min_speech_ms * sr / 1000)
    out, i = [], 0
    while i < n:
        if not voiced[i]:
            i += 1
            continue
        j, sil = i, 0                                       # extend until min_sil silence
        while j < n and (voiced[j] or sil < min_sil):
            sil = 0 if voiced[j] else sil + 1
            j += 1
        a, b = max(0, i * frame - pad), min(len(audio), j * frame + pad)
        if b - a >= min_sp:
            out.append((a, b))
        i = j
    return out or [(0, len(audio))]


class StreamingSegmenter:
    """Incremental endpointing for LIVE audio — the streaming twin of
    segment_on_silence(). Feed it audio frames (any length, 16 kHz mono) one at a
    time with push(); it buffers speech and RETURNS a completed segment the
    moment a trailing silence gap closes an utterance (or a max length is hit),
    else None. So a ticker surfaces ~one utterance + min_silence after it's
    spoken — no fixed 1.5s grid, no mid-word splits.

    Voicing is ADAPTIVE: the threshold tracks the recent noise floor, so the
    gaps between spelled letters are found at whatever level the live capture
    runs at (a fixed threshold globs reps into one blob when the floor is high,
    which is what makes Whisper mis-spell 'ELPW' as 'EOPW'). A short pre-roll is
    prepended so the first letter's onset isn't clipped. Pass an explicit
    `voiced` to override the adaptive decision (used by tests). Call flush() on
    shutdown to drain a final in-progress segment.
    """

    def __init__(self, sr: int = 16000, min_silence_ms: int = 300,
                 min_speech_ms: int = 200, max_segment_ms: int = 10000,
                 pad_ms: int = 120, floor_mult: float = 4.0,
                 floor_window: int = 60, preroll: int = 2):
        import collections
        self.sr = sr
        self.min_sil = min_silence_ms / 1000.0
        self.min_sp = min_speech_ms / 1000.0
        self.max_seg = max_segment_ms / 1000.0
        self.pad = pad_ms / 1000.0
        self.floor_mult = floor_mult
        self._recent = collections.deque(maxlen=floor_window)  # recent frame RMS
        self._preroll = collections.deque(maxlen=preroll)      # frames before speech
        self._buf: list = []      # frames accumulated for the current segment
        self._speech = 0.0        # seconds of voiced audio in _buf
        self._sil = 0.0           # seconds of trailing silence since last voice

    def _is_voiced(self, rms: float) -> bool:
        """Adaptive: voiced if well above the recent noise floor (20th pct of the
        rolling window), with a tiny absolute floor for true digital silence."""
        if self._recent:
            ordered = sorted(self._recent)
            floor = ordered[len(ordered) // 5]                 # ~20th percentile
        else:
            floor = 0.0
        return rms > max(floor * self.floor_mult, 1e-4)

    def push(self, frame, voiced=None):
        """Feed one frame. Returns a completed segment (np.ndarray) or None."""
        import numpy as np
        rms = float(np.sqrt(np.mean(frame ** 2) + 1e-12)) if len(frame) else 0.0
        self._recent.append(rms)
        dur = len(frame) / self.sr
        if voiced is None:
            voiced = self._is_voiced(rms)

        if voiced:
            if not self._buf and self._preroll:                # leading pad (onset)
                self._buf.extend(self._preroll)
            self._preroll.clear()
            self._buf.append(frame)
            self._speech += dur
            self._sil = 0.0
            if self._speech >= self.max_seg:      # cap runaway (avoids looping)
                return self._emit()
            return None

        # unvoiced
        if not self._buf:
            self._preroll.append(frame)                        # remember for onset pad
            return None
        self._sil += dur                                       # trailing silence
        if self._sil <= self.pad:
            self._buf.append(frame)
        if self._sil >= self.min_sil:
            return self._emit()
        return None

    def _emit(self):
        import numpy as np
        seg = np.concatenate(self._buf) if (self._buf and self._speech >= self.min_sp) else None
        self._buf, self._speech, self._sil = [], 0.0, 0.0
        return seg

    def flush(self):
        """Drain any in-progress segment (call on shutdown)."""
        return self._emit()


def watchlist_prompt(watchlist) -> str:
    """Stage-1 bias: a short prompt suffix listing the user's tracked tickers, so
    Whisper is primed to hear them (e.g. spell 'ELPW' rather than 'EOPW'). Append
    to the base initial_prompt. Empty watchlist -> empty string."""
    if not watchlist:
        return ""
    return " Watchlist tickers: " + " ".join(sorted(watchlist)) + "."


def make_identifier(model: str = "mlx-community/whisper-large-v3-turbo", **opts):
    import mlx_whisper

    def identify(audio) -> str:
        r = mlx_whisper.transcribe(
            audio, path_or_hf_repo=model, language="en",
            temperature=0.0, verbose=False, **opts)
        return (r.get("text") or "").strip()

    return identify


if __name__ == "__main__":
    import sys as _s
    print(recognize(" ".join(_s.argv[1:]) or "ELPW MCRP and the CIA"))
