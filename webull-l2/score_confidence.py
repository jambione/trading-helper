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
    python score_confidence.py --by agree,spread,tape_live
        # banner hit-rate CONDITIONED on agreement level (is 3/3 really
        # better than 2/3?), spread bucket, and tape-feed liveness
    python score_confidence.py --all --save-baseline benchmarks/baseline.json
    python score_confidence.py --all --vs benchmarks/baseline.json
        # freeze a benchmark before a change, then print deltas after it
    python score_confidence.py --all --sweep trend --sweep-range 0.05,0.30,0.025
        # sweep the trend deadband against forward returns (calibration)

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
import json
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
                             "file": p.name,
                             "mid": (bid + ask) / 2.0,
                             "spread_pct": _sfloat(r.get("spread_pct")),
                             "imb": imb, "sig": (r.get("signal") or "").strip(),
                             # present only once stance logging is on (newer logs)
                             "stance": (r.get("stance") or "").strip(),
                             # per-pillar votes (-1/0/1); None when the pillar
                             # abstained or the log predates vote logging
                             "tape_v": _svote(r.get("tape_vote")),
                             "vwap_v": _svote(r.get("vwap_vote")),
                             # confidence meter state, for conditioned scoring
                             "agree": _sint(r.get("agree")),
                             "total": _sint(r.get("total")),
                             "tape_live": _sint(r.get("tape_live")),
                             # raw values (newest builds): what --sweep needs
                             # to re-derive votes at candidate thresholds
                             "t5_raw": _sfloat(r.get("t5")),
                             "tape_dom": _sfloat(r.get("tape_dom")),
                             "tape_dom_big": _sfloat(r.get("tape_dom_big")),
                             "tape_dom_w": _sfloat(r.get("tape_dom_w")),
                             "tape_sided_n": _sint(r.get("tape_sided_n")),
                             "tape_sided_share":
                                 _sfloat(r.get("tape_sided_share")),
                             "tape_age": _sfloat(r.get("tape_age")),
                             "vwap_raw": _sfloat(r.get("vwap")),
                             "quality": _sfloat(r.get("quality"))})
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


def _sint(s) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
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


def _cond_labels(r: dict) -> list[tuple[str, str]]:
    """(condition, bucket) pairs for one row -- the splits the conditioned
    banner report groups by. Only conditions the row actually carries."""
    out = []
    if r.get("agree") is not None and r.get("total"):
        out.append(("agree", f"{r['agree']}/{r['total']}"))
    sp = r.get("spread_pct")
    if sp is not None:
        out.append(("spread", "<0.30%" if sp < 0.30 else
                    "0.30-1.00%" if sp <= 1.00 else ">1.00%"))
    if r.get("tape_live") is not None:
        out.append(("tape_live", "tape live" if r["tape_live"] else "tape dark"))
    q = r.get("quality")
    if q is not None:
        out.append(("quality", "grade A" if q >= 0.8 else
                    "grade B" if q >= 0.5 else "grade C"))
    return out


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
    # banner performance split by condition (agree level / spread / tape
    # liveness): condition -> bucket label -> horizon -> Bucket
    cond: dict = {"agree": {}, "spread": {}, "tape_live": {}, "quality": {}}
    # (t5, {h: fwd}) per anchor with a valid trend read -- raw material for
    # the deadband sweep (votes are re-derived per candidate threshold)
    sweep_pts: list[tuple[float, dict]] = []

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
            fwds: dict = {}
            for h in horizons:
                fwd = forward_ret(seg, ts_list, i, h)
                if fwd is None:
                    continue
                fwds[h] = fwd
                base[h].append(fwd)
                moves[h].append((fwd, bvote, tapevote))
                if bvote in (1, -1):
                    preds["banner"][h].add(fwd, bvote)
                    for cname, blabel in _cond_labels(r):
                        by = cond[cname].setdefault(
                            blabel, {hh: Bucket() for hh in horizons})
                        by[h].add(fwd, bvote)
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
            if t5 is not None and fwds:
                sweep_pts.append((t5, fwds))
    return preds, base, anchors, len(segs), moves, cond, sweep_pts


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


MIN_CELL_N = 40    # below this a bucket/sweep cell is flagged unreliable


def conditioned_report(cond: dict, horizons: list[float], by: list[str]):
    """Banner hit-rate split by condition -- the premise checks: does 3/3
    beat 2/3, does a tight spread beat a wide one, does a live tape help."""
    titles = {"agree": "banner by AGREEMENT level (the meter's premise)",
              "spread": "banner by SPREAD bucket",
              "tape_live": "banner by TAPE liveness",
              "quality": "banner by DATA-QUALITY grade (A-rows should "
                         "beat C-rows or the grade is noise)"}
    for cname in by:
        groups = cond.get(cname, {})
        print(f"\n== {titles.get(cname, cname)} ==")
        if not groups:
            print("  (nothing logged for this split -- needs newer-build logs)")
            continue
        for label in sorted(groups):
            for h in horizons:
                b = groups[label][h]
                if not b.n:
                    continue
                hit = 100.0 * sum(1 for x in b.dirret if x > 0) / b.n
                warn = "  [low n]" if b.n < MIN_CELL_N else ""
                print(f"  {f'+{int(h)}s':<6} {label:<12} n={b.n:<5} "
                      f"hit={hit:5.1f}%  "
                      f"med_edge={_median(b.dirret):+6.3f}%{warn}")
        print()


def sweep_trend(sweep_pts: list, horizons: list[float],
                values: list[float]):
    """Re-vote the reconstructed trend pillar at each candidate deadband and
    score it forward. Read the stable NEIGHBORHOOD, not the single best cell
    -- a lone spike at one threshold is overfit, a plateau is a setting."""
    print(f"\n== TREND deadband sweep ({len(sweep_pts)} anchors with a "
          f"trend read) ==")
    print("  deadband: vote long if t5 > x, short if t5 < -x "
          f"(current {TREND_DEADBAND})")
    for h in horizons:
        print(f"\n  -- horizon +{int(h)}s --")
        print(f"  {'deadband':<10} {'n':>5} {'hit%':>7} {'med_edge':>9}")
        for v in values:
            b = Bucket()
            for t5, fwds in sweep_pts:
                if h not in fwds:
                    continue
                vote = 1 if t5 > v else -1 if t5 < -v else 0
                if vote:
                    b.add(fwds[h], vote)
            if not b.n:
                print(f"  {v:<10.3f} {0:>5}   (no votes)")
                continue
            hit = 100.0 * sum(1 for x in b.dirret if x > 0) / b.n
            warn = "  [low n]" if b.n < MIN_CELL_N else ""
            print(f"  {v:<10.3f} {b.n:>5} {hit:>6.1f}% "
                  f"{_median(b.dirret):>+8.3f}%{warn}")
    print()


# 1-D sweeps around the live defaults; the cartesian grid overfits on the
# session counts this tool realistically sees, so each parameter is swept
# with the others held at their config defaults.
GRID = {
    "tape_dom_min": [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40],
    "tape_sided_share": [0.3, 0.4, 0.5, 0.6, 0.7],
    "vwap_deadband": [0.0002, 0.0005, 0.0010, 0.0020],
    "dom_big_min": [0.25, 0.40, 0.60, 0.80],
    "dom_w_min": [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40],
}


def _raw_gate(r: dict, min_sided: int = 4, share: float = 0.5) -> bool:
    """The live volume-aware tape gate, re-applied from logged raw values."""
    n, s = r.get("tape_sided_n"), r.get("tape_sided_share")
    return n is not None and s is not None and n >= min_sided and s >= share


def _vote_from_raw(r: dict, param: str, v: float) -> int | None:
    """Re-derive one pillar vote from the logged RAW values at a candidate
    threshold -- the whole point of logging raw numbers next to votes."""
    if param == "tape_dom_min":
        d = r.get("tape_dom")
        if d is None or not _raw_gate(r):
            return None
        return 1 if d >= v else -1 if d <= -v else 0
    if param == "tape_sided_share":
        d = r.get("tape_dom")
        if d is None or not _raw_gate(r, share=v):
            return None
        return 1 if d >= 0.25 else -1 if d <= -0.25 else 0
    if param == "vwap_deadband":
        vw, mid = r.get("vwap_raw"), r.get("mid")
        if not vw or not mid:
            return None
        rel = (mid - vw) / vw
        return 1 if rel > v else -1 if rel < -v else 0
    if param == "dom_big_min":
        d = r.get("tape_dom_big")
        if d is None or not _raw_gate(r):
            return None
        return 1 if d >= v else -1 if d <= -v else 0
    if param == "dom_w_min":
        d = r.get("tape_dom_w")
        if d is None or not _raw_gate(r):
            return None
        return 1 if d >= v else -1 if d <= -v else 0
    return None


def _grid_anchors(rows: list[dict], horizons: list[float], stride: float,
                  since_switch: float) -> list[tuple[dict, dict]]:
    """(row, {h: fwd}) per scored anchor -- same segmentation/stride rules
    as score(), collected once so every sweep cell samples identically."""
    out = []
    for seg in segments(rows):
        ts_list = [r["ts"] for r in seg]
        seg_start = seg[0]["ts"]
        last_anchor = -1e9
        for i, r in enumerate(seg):
            if since_switch and r["ts"] - seg_start > since_switch:
                break
            if r["ts"] - last_anchor < stride:
                continue
            last_anchor = r["ts"]
            fwds = {}
            for h in horizons:
                fwd = forward_ret(seg, ts_list, i, h)
                if fwd is not None:
                    fwds[h] = fwd
            if fwds:
                out.append((r, fwds))
    return out


def sweep_grid(rows: list[dict], horizons: list[float], stride: float,
               since_switch: float):
    """Threshold sweeps from logged RAW values (newer-build logs only).
    Prints a train/eval split by session file when there's more than one,
    because a threshold picked on the same data it's judged on is a story,
    not a setting. Read plateaus, not the single best cell."""
    anchors = _grid_anchors(rows, horizons, stride, since_switch)
    have_raw = [a for a in anchors if a[0].get("tape_dom") is not None
                or a[0].get("vwap_raw") is not None]
    if not have_raw:
        print("\n== sweep grid: no raw-value columns in these logs "
              "(needs sessions from the newer build) ==")
        return
    files = sorted({r["file"] for r, _ in anchors})
    eval_file = files[-1] if len(files) > 1 else None
    print(f"\n== threshold sweep from logged raw values "
          f"({len(have_raw)} anchors, {len(files)} session file(s)) ==")
    if eval_file:
        print(f"  train = all but {eval_file}; eval = {eval_file} "
              "(a real setting survives on eval too)")
    for param, values in GRID.items():
        print(f"\n  -- {param} --")
        hdr = f"  {'value':<10}"
        for h in horizons:
            hdr += f" | +{int(h)}s n / hit% / med_edge"
        print(hdr + ("   [train | eval hit%]" if eval_file else ""))
        for v in values:
            cells = []
            ev_bits = []
            for h in horizons:
                b, be = Bucket(), Bucket()
                for r, fwds in anchors:
                    if h not in fwds:
                        continue
                    vote = _vote_from_raw(r, param, v)
                    if vote not in (1, -1):
                        continue
                    if eval_file and r["file"] == eval_file:
                        be.add(fwds[h], vote)
                    else:
                        b.add(fwds[h], vote)
                if b.n:
                    hit = 100.0 * sum(1 for x in b.dirret if x > 0) / b.n
                    warn = "*" if b.n < MIN_CELL_N else " "
                    cells.append(f"{b.n:>4} / {hit:5.1f}% / "
                                 f"{_median(b.dirret):+6.3f}%{warn}")
                else:
                    cells.append("   0 /   --  /    --  ")
                if eval_file:
                    if be.n:
                        ehit = (100.0 * sum(1 for x in be.dirret if x > 0)
                                / be.n)
                        ev_bits.append(f"{ehit:.0f}%(n{be.n})")
                    else:
                        ev_bits.append("--")
            line = f"  {v:<10.4f} | " + " | ".join(cells)
            if eval_file:
                line += "   [" + " ".join(ev_bits) + "]"
            print(line)
    print("\n  * = under the reliability floor "
          f"(n < {MIN_CELL_N}); ignore those cells.")


def baseline_dict(preds, base, anchors, nsegs, horizons) -> dict:
    """The frozen-benchmark shape: per predictor per horizon n/hit/edges."""
    out = {"meta": {"created": datetime.now().isoformat(timespec="seconds"),
                    "anchors": anchors, "segments": nsegs},
           "baseline": {}, "predictors": {}}
    for h in horizons:
        b = base[h]
        if b:
            out["baseline"][str(int(h))] = {
                "n": len(b),
                "up_pct": round(100.0 * sum(1 for x in b if x > 0) / len(b), 1),
                "med_move": round(_median(b), 3)}
    for name, byh in preds.items():
        cur = {}
        for h, b in byh.items():
            if b.n:
                cur[str(int(h))] = {
                    "n": b.n,
                    "hit": round(100.0 * sum(1 for x in b.dirret if x > 0)
                                 / b.n, 1),
                    "med_edge": round(_median(b.dirret), 3),
                    "mean_edge": round(sum(b.dirret) / b.n, 3)}
        if cur:
            out["predictors"][name] = cur
    return out


def vs_report(current: dict, path: Path):
    """Delta view against a frozen baseline JSON (--save-baseline)."""
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        print(f"\ncould not read baseline {path}: {e}")
        return
    print(f"\n== vs baseline {path.name} "
          f"(saved {old.get('meta', {}).get('created', '?')}) ==")
    print("  positive deltas = better than the frozen run\n")
    for name, byh in current["predictors"].items():
        oldp = old.get("predictors", {}).get(name, {})
        for h, c in byh.items():
            o = oldp.get(h)
            if not o:
                print(f"  {name:<10} +{h}s  n={c['n']:<5} "
                      "(not in baseline)")
                continue
            print(f"  {name:<10} +{h}s  n {o['n']}->{c['n']}  "
                  f"hit {o['hit']:.1f}->{c['hit']:.1f} "
                  f"({c['hit'] - o['hit']:+.1f})  "
                  f"med_edge {o['med_edge']:+.3f}->{c['med_edge']:+.3f} "
                  f"({c['med_edge'] - o['med_edge']:+.3f})")
    print()


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
    ap.add_argument("--by", default="",
                    help="conditioned banner report: comma list of "
                         "agree,spread,tape_live (or 'all')")
    ap.add_argument("--save-baseline", metavar="FILE",
                    help="write this run's per-predictor stats to a JSON "
                         "benchmark file (freeze before a change)")
    ap.add_argument("--vs", metavar="FILE",
                    help="print deltas against a frozen baseline JSON")
    ap.add_argument("--sweep", choices=["trend", "grid"],
                    help="'trend' sweeps the deadband (reconstructible from "
                         "any log); 'grid' sweeps tape/vwap thresholds from "
                         "the raw-value columns (newer-build logs)")
    ap.add_argument("--sweep-range", default="0.05,0.30,0.025",
                    help="sweep lo,hi,step (default 0.05,0.30,0.025)")
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
    preds, base, anchors, nsegs, moves, cond, sweep_pts = score(
        rows, horizons, a.stride, a.since_switch)
    report(preds, base, anchors, nsegs, horizons, a.stride)
    capture_report(moves, horizons, a.move_thresh)

    if a.by:
        wanted = (["agree", "spread", "tape_live", "quality"]
                  if a.by == "all"
                  else [s.strip() for s in a.by.split(",") if s.strip()])
        conditioned_report(cond, horizons, wanted)

    if a.sweep == "trend":
        try:
            lo, hi, step = (float(x) for x in a.sweep_range.split(","))
        except ValueError:
            print(f"bad --sweep-range {a.sweep_range!r} (want lo,hi,step)")
            return
        values, v = [], lo
        while v <= hi + 1e-9:
            values.append(round(v, 4))
            v += step
        sweep_trend(sweep_pts, horizons, values)
    elif a.sweep == "grid":
        sweep_grid(rows, horizons, a.stride, a.since_switch)

    if a.save_baseline or a.vs:
        snap = baseline_dict(preds, base, anchors, nsegs, horizons)
        if a.vs:
            vs_report(snap, Path(a.vs) if Path(a.vs).is_absolute()
                      else HERE / a.vs)
        if a.save_baseline:
            out = (Path(a.save_baseline) if Path(a.save_baseline).is_absolute()
                   else HERE / a.save_baseline)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(snap, indent=2))
            print(f"baseline saved -> {out}")


if __name__ == "__main__":
    main()
