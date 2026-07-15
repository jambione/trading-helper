"""A/B-compare two L2 sessions on decision-window recall (warm-start on vs off).

The point: prove whether warm-start actually helps the fast symbol-hop
workflow, with a number instead of a hunch. Warm-start seeds the 5m trend
the instant you switch symbols; this scores whether that makes the signal
CAPTURE more real moves (and MISS fewer) in the first few minutes after a
switch -- exactly the window that used to be blind.

Workflow
--------
Run two sessions that log to their own files (set `csv_name` in config.json):

  session 1:  "warm_start": true,   "csv_name": "warm_on.csv"
  session 2:  "warm_start": false,  "csv_name": "warm_off.csv"

Then compare them:

  python compare_ab.py warm_on.csv warm_off.csv
  python compare_ab.py A.csv B.csv --since-switch 180 --move-thresh 1.0 \
                                   --horizon 120 --stride 15

Reading it
----------
Among anchors in the first --since-switch seconds after a symbol switch that
ACTUALLY moved >= --move-thresh by +horizon:
  captured = called the right direction   (recall -- higher is better)
  missed   = stayed NEUTRAL on a real move (the failure you want to cut)
  wrong    = called the opposite way       (watch this: if warm-start cuts
             'missed' but 'wrong' climbs, the trade-price seed is biasing the
             trend and warm-start is hurting, not helping).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from score_confidence import (  # noqa: E402
    Bucket, _median, capture_stats, load_rows, score,
)


def _measure(path: Path, horizon: float, stride: float,
             since_switch: float, move_thresh: float) -> dict | None:
    rows = load_rows([path])
    if not rows:
        return None
    preds, base, anchors, nsegs, moves, _cond, _sweep = score(
        rows, [horizon], stride, since_switch)
    cap = capture_stats(moves, horizon, move_thresh)
    bucket: Bucket = preds["banner"][horizon]
    hit = (100.0 * sum(1 for x in bucket.dirret if x > 0) / bucket.n
           if bucket.n else None)
    edge = _median(bucket.dirret) if bucket.n else None
    return {"rows": len(rows), "anchors": anchors, "segs": nsegs,
            "cap": cap, "banner_hit": hit, "banner_edge": edge,
            "banner_n": bucket.n}


def _pct(part: int, whole: int) -> str:
    return f"{part} ({100.0 * part / whole:3.0f}%)" if whole else f"{part} (--)"


def _row(label: str, a: str, b: str) -> str:
    return f"  {label:<22} {a:<20} {b:<20}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file_a", help="warm-start ON log (e.g. warm_on.csv)")
    ap.add_argument("file_b", help="warm-start OFF log (e.g. warm_off.csv)")
    ap.add_argument("--since-switch", type=float, default=180.0,
                    help="decision window: seconds after a symbol switch (180)")
    ap.add_argument("--move-thresh", type=float, default=1.0,
                    help="%% move that counts as a real move (1.0)")
    ap.add_argument("--horizon", type=float, default=120.0,
                    help="forward horizon in seconds (120)")
    ap.add_argument("--stride", type=float, default=15.0,
                    help="min seconds between scored anchors (15)")
    a = ap.parse_args()

    pa, pb = HERE / a.file_a, HERE / a.file_b
    for p in (pa, pb):
        if not p.exists():
            print(f"missing: {p.name} (run the session that writes it first)")
            return

    A = _measure(pa, a.horizon, a.stride, a.since_switch, a.move_thresh)
    B = _measure(pb, a.horizon, a.stride, a.since_switch, a.move_thresh)
    if A is None or B is None:
        print("no usable rows in", pa.name if A is None else pb.name)
        return

    print(f"\nA/B: decision-window recall in the first {a.since_switch:.0f}s "
          f"after a symbol switch")
    print(f"  move threshold {a.move_thresh:.2f}%   horizon +{int(a.horizon)}s"
          f"   stride {a.stride:.0f}s\n")
    print(_row("", f"A: {a.file_a}", f"B: {a.file_b}"))
    print(_row("rows / anchors",
               f"{A['rows']} / {A['anchors']}", f"{B['rows']} / {B['anchors']}"))

    na, nb = A["cap"]["n"], B["cap"]["n"]
    print(_row("real moves (n)", str(na), str(nb)))
    for pillar in ("banner", "tape"):
        ca, wa, ma = A["cap"][pillar]
        cb, wb, mb = B["cap"][pillar]
        print(_row(f"{pillar} captured", _pct(ca, na), _pct(cb, nb)))
        print(_row(f"{pillar} wrong",    _pct(wa, na), _pct(wb, nb)))
        print(_row(f"{pillar} missed",   _pct(ma, na), _pct(mb, nb)))
    print(_row("banner hit% (of calls)",
               f"{A['banner_hit']:.0f}%" if A["banner_hit"] is not None else "--",
               f"{B['banner_hit']:.0f}%" if B["banner_hit"] is not None else "--"))

    # verdict on the banner, in the decision window, on captured/missed deltas
    print()
    if na and nb:
        d_missed = 100.0 * (A["cap"]["banner"][2] / na - B["cap"]["banner"][2] / nb)
        d_cap = 100.0 * (A["cap"]["banner"][0] / na - B["cap"]["banner"][0] / nb)
        d_wrong = 100.0 * (A["cap"]["banner"][1] / na - B["cap"]["banner"][1] / nb)
        print(f"Verdict (A vs B): captured {d_cap:+.0f} pts, "
              f"missed {d_missed:+.0f} pts, wrong {d_wrong:+.0f} pts.")
        if d_cap > 0 and d_missed < 0 and d_wrong <= 2:
            print("  -> warm-start improved decision-window recall.")
        elif d_missed < 0 and d_wrong > 2:
            print("  -> warm-start cut misses but ADDED wrong calls: the "
                  "trade-price seed is biasing the trend. Investigate/back out.")
        elif abs(d_cap) < 1 and abs(d_missed) < 1:
            print("  -> no meaningful difference (or too little data).")
        else:
            print("  -> mixed / inconclusive; get more anchors before deciding.")
    if min(na, nb) < 15:
        print(f"  NOTE: small samples (n_A={na}, n_B={nb}); treat as directional "
              "until you have a few RTH sessions each.")


if __name__ == "__main__":
    main()
