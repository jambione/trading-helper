"""Score the L2 monitor's signals against what price actually did next.

Answers one question: do the things the monitor keys on have any forward
edge, measured on your own logged data? It reconstructs the confidence
TREND pillar exactly as l2_core computes it (the one pillar recoverable
from the log), and also scores logged IMBALANCE and the logged BUY/SELL
signals, against forward returns at +1/+2/+5 min.

    python score_confidence.py            # scores l2_log.csv
    python score_confidence.py --all      # include archived l2_log-old-*.csv
    python score_confidence.py --stride 20 --horizons 60,120,300
    python score_confidence.py --since-switch 180 --move-thresh 1.0
        # only the first 3 min after each symbol switch (the fast-hop
        # decision window), and count 1%+ moves for the recall report

What it can and can't measure
-----------------------------
Older logs carry only mid-price, imbalance, and the engine BUY/SELL
signal. The newer build also logs the confidence banner's stance and each
pillar's own vote, so the full three-pillar signal is scorable per pillar
once you have sessions from that build. What IS scorable:

  * banner stance - the real logged 5m LONG/BEAR/NEUTRAL call (newer logs).
  * trend pillar  - reconstructed from the logged mid series with the
                    same robust median-band endpoints and coverage gate
                    as SignalEngine.trend_pct(300). This is the backbone
                    of the banner; if even it has no forward edge, the
                    premise is weak. Works on all logs.
  * tape pillar   - the logged 60s executed buy/sell vote (newer logs);
                    cannot be reconstructed from price, so pre-newer-build
                    logs can't score it.
  * vwap pillar   - the logged price-vs-session-VWAP vote (newer logs).
  * imbalance     - the input the banner deliberately DE-weights; scoring
                    it here checks that decision against your own data.
  * logged signal - the old imbalance-based engine's BUY/SELL calls.

Robustness: OCR glitches (mid 139 -> 6) wreck mean returns, so the
headline numbers are HIT-RATE (sign-based) and MEDIAN forward return,
both glitch-resistant; mean is shown but flagged. Adjacent rows are
near-duplicates, so anchors are sub-sampled every --stride seconds to
avoid counting the same moment hundreds of times.
"""
from __future__ import annotations

import argparse
import bisect
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent

# --- trend pillar reconstruction (mirrors l2_core.SignalEngine.trend_pct
#     and confidence(): vote long if t5>0.1, short if t5<-0.1) -------------
TREND_WINDOW = 300.0
MIN_COVERAGE = 0.6
TREND_DEADBAND = 0.1        # % — confidence() long/short threshold
# imbalance thresholds mirror config defaults (imbalance_buy/sell)
IMB_BUY = 1.8
IMB_SELL = 0.55
SEGMENT_GAP = 90.0         # s — a bigger gap means a fresh capture/reset


def _median(v: list[float]) -> float:
    s = sorted(v)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def load_rows(paths: list[Path]) -> list[dict]:
    """Parse the CSV(s) into rows with epoch ts and mid; skip junk."""
    import csv
    rows: list[dict] = []
    for p in paths:
        if not p.exists():
            continue
        with open(p, newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    ts = datetime.fromisoformat(r["time"]).timestamp()
                    bid = float(r["best_bid"])
                    ask = float(r["best_ask"])
                    imb = float(r["imbalance"])
                except (ValueError, KeyError, TypeError):
                    continue          # header dregs / malformed row
                if bid <= 0 or ask <= 0:
                    continue
                rows.append({"ts": ts, "sym": r.get("symbol", ""),
                             "mid": (bid + ask) / 2.0,
                             "spread_pct": _sfloat(r.get("spread_pct")),
                             "imb": imb, "sig": (r.get("signal") or "").strip(),
                             # present only once stance logging is on (newer logs)
                             "stance": (r.get("stance") or "").strip(),
                             # per-pillar votes (-1/0/1); None when the pillar
                             # abstained or the log predates vote logging
                             "tape_v": _svote(r.get("tape_vote")),
                             "vwap_v": _svote(r.get("vwap_vote"))})
    rows.sort(key=lambda d: (d["sym"], d["ts"]))
    return rows


def _sfloat(s) -> float | None:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _svote(s) -> int | None:
    """Parse a logged pillar vote to -1/0/1, or None (blank/absent = the
    pillar abstained, or the log predates vote logging)."""
    s = (s or "").strip()
    if s in ("-1", "0", "1"):
        return int(s)
    return None


def segments(rows: list[dict]) -> list[list[dict]]:
    """Split into contiguous same-symbol runs (state resets on a symbol
    switch or a capture gap, so a trend must not span the boundary)."""
    segs, cur = [], []
    for r in rows:
        if cur and (r["sym"] != cur[-1]["sym"]
                    or r["ts"] - cur[-1]["ts"] > SEGMENT_GAP):
            segs.append(cur)
            cur = []
        cur.append(r)
    if cur:
        segs.append(cur)
    return segs


def trend_pct(win: list[dict], seconds: float) -> float | None:
    """Robust % drift over `win` (rows within the trailing window), matching
    SignalEngine.trend_pct: median-band endpoints + coverage gate."""
    if len(win) < 2 or win[-1]["ts"] - win[0]["ts"] < MIN_COVERAGE * seconds:
        return None
    band = max(3.0, seconds * 0.05)
    t0, t1 = win[0]["ts"], win[-1]["ts"]
    m0 = _median([r["mid"] for r in win if r["ts"] <= t0 + band])
    m1 = _median([r["mid"] for r in win if r["ts"] >= t1 - band])
    return 100.0 * (m1 - m0) / m0 if m0 else None


def forward_ret(seg: list[dict], ts_list: list[float], i: int,
                horizon: float) -> float | None:
    """% change in mid from row i to the sample nearest ts_i+horizon,
    within tolerance; None if the segment ends first or has a gap there."""
    target = seg[i]["ts"] + horizon
    j = bisect.bisect_left(ts_list, target)
    best, bestd = None, None
    for k in (j - 1, j):
        if 0 <= k < len(seg):
            d = abs(seg[k]["ts"] - target)
            if bestd is None or d < bestd:
                bestd, best = d, k
    tol = max(10.0, 0.15 * horizon)
    if best is None or bestd > tol:
        return None
    m0, m1 = seg[i]["mid"], seg[best]["mid"]
    return 100.0 * (m1 - m0) / m0 if m0 else None


class Bucket:
    """Forward-return stats for one predicted direction (+1 long / -1 short)."""

    def __init__(self):
        self.dirret: list[float] = []   # fwd_ret * direction (edge if >0)
        self.rawret: list[float] = []   # signed fwd_ret
        self.extreme = 0                # |fwd_ret| > 25% (OCR glitch-ish)

    def add(self, fwd: float, direction: int):
        if abs(fwd) > 25.0:
            self.extreme += 1
        self.rawret.append(fwd)
        self.dirret.append(fwd * direction)

    @property
    def n(self):
        return len(self.dirret)

    def line(self, name: str) -> str:
        if not self.n:
            return f"  {name:<26} (no samples)"
        hit = 100.0 * sum(1 for x in self.dirret if x > 0) / self.n
        return (f"  {name:<26} n={self.n:<5} "
                f"hit={hit:5.1f}%  med_edge={_median(self.dirret):+6.3f}%  "
                f"mean_edge={sum(self.dirret) / self.n:+6.3f}%"
                + (f"  [{self.extreme} glitchy]" if self.extreme else ""))


def score(rows: list[dict], horizons: list[float], stride: float,
          since_switch: float = 0.0):
    """since_switch>0 restricts anchors to the first N seconds after a symbol
    switch (segment start) -- the actual few-minute decision window when you
    hop onto a ticker, rather than idealized fully-warmed reads."""
    segs = segments(rows)
    # predictor -> horizon -> Bucket. 'banner' is the real logged 5m stance
    # (only populated once stance logging is on); the rest are reconstructed.
    preds = {"banner": {}, "trend": {}, "tape": {}, "vwap": {},
             "imbalance": {}, "signal": {}}
    for name in preds:
        for h in horizons:
            preds[name][h] = Bucket()
    base = {h: [] for h in horizons}       # all anchors: unconditional fwd ret
    # every scored anchor's (fwd_ret, banner_vote, tape_vote) per horizon,
    # incl. neutral votes -- so capture_report can count MISSED real moves
    moves = {h: [] for h in horizons}

    anchors = 0
    for seg in segs:
        ts_list = [r["ts"] for r in seg]
        seg_start = seg[0]["ts"]
        left = 0
        last_anchor = -1e9
        for i, r in enumerate(seg):
            # slide the trailing-window left edge
            while seg[left]["ts"] < r["ts"] - TREND_WINDOW:
                left += 1
            # decision-window filter: once we're past N s into the segment,
            # every later row is too (rows are time-ordered) -> stop this seg
            if since_switch and r["ts"] - seg_start > since_switch:
                break
            if r["ts"] - last_anchor < stride:
                continue                    # sub-sample: one anchor / stride
            last_anchor = r["ts"]
            anchors += 1
            win = seg[left:i + 1]
            t5 = trend_pct(win, TREND_WINDOW)
            tvote = None
            if t5 is not None:
                tvote = 1 if t5 > TREND_DEADBAND else -1 if t5 < -TREND_DEADBAND else 0
            ivote = 1 if r["imb"] >= IMB_BUY else -1 if r["imb"] <= IMB_SELL else 0
            svote = 1 if r["sig"] == "BUY" else -1 if r["sig"] == "SELL" else 0
            bvote = 1 if r["stance"] == "LONG" else -1 if r["stance"] == "BEAR" else 0
            # tape/vwap pillars come straight from the logged live votes -
            # they can't be reconstructed from price the way trend can
            tapevote, vwapvote = r.get("tape_v"), r.get("vwap_v")
            for h in horizons:
                fwd = forward_ret(seg, ts_list, i, h)
                if fwd is None:
                    continue
                base[h].append(fwd)
                moves[h].append((fwd, bvote, tapevote))
                if bvote in (1, -1):
                    preds["banner"][h].add(fwd, bvote)
                if tvote in (1, -1):
                    preds["trend"][h].add(fwd, tvote)
                if tapevote in (1, -1):
                    preds["tape"][h].add(fwd, tapevote)
                if vwapvote in (1, -1):
                    preds["vwap"][h].add(fwd, vwapvote)
                if ivote in (1, -1):
                    preds["imbalance"][h].add(fwd, ivote)
                if svote in (1, -1):
                    preds["signal"][h].add(fwd, svote)
    return preds, base, anchors, len(segs), moves


def report(preds, base, anchors, nsegs, horizons, stride):
    print(f"\nAnchors scored: {anchors} (one per {stride:.0f}s) "
          f"across {nsegs} symbol segments\n")
    print("Reading it: hit% = share of calls price moved the CALLED way "
          "(50% = coin flip).")
    print("            med_edge = median of (forward_return x direction); "
          ">0 means real forward edge.\n")
    for h in horizons:
        b = base[h]
        if b:
            up = 100.0 * sum(1 for x in b if x > 0) / len(b)
            print(f"-- horizon +{int(h)}s "
                  f"-- baseline: {up:.1f}% of all moments rose, "
                  f"median move {_median(b):+.3f}%")
        else:
            print(f"-- horizon +{int(h)}s -- no forward samples")
        for name, label in (("banner", "5m BANNER stance (logged)"),
                             ("trend", "TREND pillar (reconstructed)"),
                             ("tape", "TAPE pillar (logged)"),
                             ("vwap", "VWAP pillar (logged)"),
                             ("imbalance", "imbalance >=1.8 / <=0.55"),
                             ("signal", "logged BUY / SELL signal")):
            # banner/tape/vwap only exist once the newer logging is on;
            # skip them silently on older logs rather than print empty lines
            if name in ("banner", "tape", "vwap") and not preds[name][h].n:
                continue
            print(preds[name][h].line(label))
        print()
    print("Note: 'med_edge' near 0 or negative = that predictor did not "
          "anticipate the next move on this data.\n"
          "TAPE/VWAP pillars are scored only from rows logged by the newer "
          "build (trend/tape/vwap_vote columns); older logs show trend only.")


def capture_stats(moves, horizon, move_thresh) -> dict:
    """Recall numbers at one horizon: among anchors that actually moved
    >= move_thresh, how the banner and tape votes fared. Returns
    {'n': int, 'banner': (captured, wrong, missed), 'tape': (...)} where
    missed = the pillar stayed neutral/abstained on a real move. Shared by
    capture_report and compare_ab.py so both count identically."""
    real = [m for m in moves.get(horizon, []) if abs(m[0]) >= move_thresh]
    n = len(real)

    def stat(idx):  # idx 1 = banner vote, 2 = tape vote
        cap = sum(1 for m in real
                  if m[idx] in (1, -1) and (m[0] > 0) == (m[idx] > 0))
        wrong = sum(1 for m in real
                    if m[idx] in (1, -1) and (m[0] > 0) != (m[idx] > 0))
        return cap, wrong, n - cap - wrong              # missed = the rest

    return {"n": n, "banner": stat(1), "tape": stat(2)}


def capture_report(moves, horizons, move_thresh):
    """RECALL view: among anchors that actually moved >= move_thresh by the
    horizon, how often did we call the direction vs stay neutral. 'missed'
    (we abstained on a real move) is the failure you asked to cut."""
    print(f"\nMove capture (RECALL) -- of anchors that actually moved "
          f">= {move_thresh:.2f}% by the horizon:")
    print("  captured = called the right way | wrong = called opposite | "
          "missed = stayed NEUTRAL on a real move")
    for h in horizons:
        st = capture_stats(moves, h, move_thresh)
        n = st["n"]
        if not n:
            print(f"  +{int(h)}s: no moves >= {move_thresh:.2f}%")
            continue
        for label, key in (("banner", "banner"), ("tape  ", "tape")):
            c, w, miss = st[key]
            if c == w == 0 and miss == n:
                # pillar never voted on any real move (e.g. no tape logged)
                print(f"  +{int(h)}s {label}: n={n}  (no votes logged)")
                continue
            print(f"  +{int(h)}s {label}: n={n}  captured {c} "
                  f"({100 * c / n:3.0f}%)  wrong {w} ({100 * w / n:3.0f}%)  "
                  f"missed {miss} ({100 * miss / n:3.0f}%)")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true",
                    help="include archived l2_log-old-*.csv files")
    ap.add_argument("--horizons", default="60,120,300",
                    help="forward horizons in seconds, comma-separated")
    ap.add_argument("--stride", type=float, default=15.0,
                    help="min seconds between scored anchors (default 15)")
    ap.add_argument("--since-switch", type=float, default=0.0,
                    help="only score anchors within N seconds of a symbol "
                         "switch -- your few-minute decision window (0=off)")
    ap.add_argument("--move-thresh", type=float, default=1.0,
                    help="%% move that counts as a 'real move' for the "
                         "recall/capture report (default 1.0)")
    a = ap.parse_args()

    paths = [HERE / "l2_log.csv"]
    if a.all:
        # -old-* archives plus any -<stamp> fallback session logs
        paths += sorted(p for p in HERE.glob("l2_log-*.csv")
                        if p != HERE / "l2_log.csv")
    horizons = [float(x) for x in a.horizons.split(",") if x.strip()]

    rows = load_rows(paths)
    if not rows:
        print("No usable rows found in", ", ".join(p.name for p in paths))
        return
    print(f"Loaded {len(rows)} rows from "
          f"{', '.join(p.name for p in paths if p.exists())}")
    if a.since_switch:
        print(f"Decision-window mode: only anchors within "
              f"{a.since_switch:.0f}s of a symbol switch.")
    preds, base, anchors, nsegs, moves = score(
        rows, horizons, a.stride, a.since_switch)
    report(preds, base, anchors, nsegs, horizons, a.stride)
    capture_report(moves, horizons, a.move_thresh)


if __name__ == "__main__":
    main()
