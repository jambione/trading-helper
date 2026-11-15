#!/usr/bin/env python3
"""pdh_retest.py - the pasted video's previous-day high/low break-and-retest, as written (docs/studies/pdh_retest_prereg.json).

LONG: first 5-minute candle (09:30 grid) closing above the prior day's high; within 30 one-minute bars a bar dips to within
0.10% of the level, closes above it and green -> buy the next open; stop 0.15% under the level, target 2x risk, else the
15:59 close. SHORT is the mirror at the prior day's low. Control: the same side/bracket at the same minute on every other
universe name. Judged per trade, day-clustered t, halves by date.

    .venv/bin/python tools/studies/pdh_retest.py                     # primary (sip, 400 liquid names), checks first
    .venv/bin/python tools/studies/pdh_retest.py --warehouse sip_top --top-n 60 --cost-bp 2
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as H  # noqa: E402

ZONE, DEAD, STOP, MIN_RISK, WAIT, RR = 0.0010, 0.0010, 0.0015, 0.0005, 30, 2.0


# ---------- the bracket ----------
def bracket(o, h, lo, c, i: int, side: int, risk: float) -> float | None:
    """Return in bp of entering at o[i] (side +1 long / -1 short) with a stop `risk` (fraction) away and a 2x target,
    exiting at the first touch (stop first if both in one bar) or the last close. None if the entry bar is missing."""
    e = o[i]
    if not e > 0:
        return None
    stop, tgt = e * (1 - side * risk), e * (1 + side * RR * risk)
    T = len(o)
    for j in range(i, T):
        if np.isnan(h[j]):
            continue
        hit_stop = lo[j] <= stop if side > 0 else h[j] >= stop
        hit_tgt = h[j] >= tgt if side > 0 else lo[j] <= tgt
        if hit_stop:
            px = min(o[j], stop) if side > 0 else max(o[j], stop)   # a gap through the stop fills at the open
            return side * (px / e - 1) * 1e4
        if hit_tgt:
            px = max(o[j], tgt) if side > 0 else min(o[j], tgt)
            return side * (px / e - 1) * 1e4
    last = c[~np.isnan(c)]
    return side * (last[-1] / e - 1) * 1e4 if len(last) else None


# ---------- the setup ----------
def five_min_closes(c: np.ndarray) -> list[tuple[int, float]]:
    """(index of the last 1-minute bar, close) of each 5-minute candle on the 09:30 grid that has its last bar."""
    out = []
    for k in range(4, len(c), 5):
        if not np.isnan(c[k]):
            out.append((k, c[k]))
    return out


def setups(day: dict, prev: dict, retest: bool = True) -> list[tuple[int, int, int, float]]:
    """[(row, entry bar, side, risk)] for the day. Only bars up to the decision bar are read for each decision."""
    out = []
    for s in range(len(day["syms"])):
        ph, pl = np.nanmax(prev["h"][s]) if np.isfinite(prev["h"][s]).any() else np.nan, \
            np.nanmin(prev["l"][s]) if np.isfinite(prev["l"][s]).any() else np.nan
        if not (ph > 0 and pl > 0):
            continue
        o, h, lo, c = day["o"][s], day["h"][s], day["l"][s], day["c"][s]
        for side, lvl in ((1, ph), (-1, pl)):
            brk = next((k for k, x in five_min_closes(c) if (x > lvl if side > 0 else x < lvl)), None)
            if brk is None:
                continue
            if not retest:
                i = brk + 1
            else:
                i = None
                for m in range(brk + 1, min(brk + 1 + WAIT, len(c))):
                    if np.isnan(c[m]):
                        continue
                    if (c[m] < lvl * (1 - DEAD)) if side > 0 else (c[m] > lvl * (1 + DEAD)):
                        break                                   # closed back through the level: dead for the day
                    touched = lo[m] <= lvl * (1 + ZONE) if side > 0 else h[m] >= lvl * (1 - ZONE)
                    confirm = (c[m] > lvl and c[m] > o[m]) if side > 0 else (c[m] < lvl and c[m] < o[m])
                    if touched and confirm:
                        i = m + 1
                        break
            if i is None or i >= len(o) - 1 or not o[i] > 0:
                continue
            stop_px = lvl * (1 - side * STOP)
            risk = side * (o[i] - stop_px) / o[i]
            if risk < MIN_RISK:
                continue                                       # entry already through its stop, or a too-tight stop
            out.append((s, i, side, risk))
    return out


def bracket_all(o, h, lo, c, i: int, side: int, risk: float) -> np.ndarray:
    """bracket() for every row at once (NaN where the entry bar is missing). Same rules: stop first, gaps fill at the open."""
    e = o[:, i]
    stop, tgt = e * (1 - side * risk), e * (1 + side * RR * risk)
    H_, L_, O_ = h[:, i:], lo[:, i:], o[:, i:]
    hs = (L_ <= stop[:, None]) if side > 0 else (H_ >= stop[:, None])
    ht = (H_ >= tgt[:, None]) if side > 0 else (L_ <= tgt[:, None])
    hs &= ~np.isnan(H_)
    ht &= ~np.isnan(H_)
    big = H_.shape[1] + 1
    fs = np.where(hs.any(1), hs.argmax(1), big)
    ft = np.where(ht.any(1), ht.argmax(1), big)
    rows = np.arange(len(e))
    out = np.full(len(e), np.nan)
    cl = np.where(np.isnan(c), np.nan, c)
    lastc = np.array([r[~np.isnan(r)][-1] if (~np.isnan(r)).any() else np.nan for r in cl])
    stop_first = (fs <= ft) & (fs < big)
    tgt_first = (ft < fs) & (ft < big)
    po_s = O_[rows, np.minimum(fs, big - 2)]
    po_t = O_[rows, np.minimum(ft, big - 2)]
    px_s = np.where(side > 0, np.fmin(po_s, stop), np.fmax(po_s, stop))
    px_t = np.where(side > 0, np.fmax(po_t, tgt), np.fmin(po_t, tgt))
    px = np.where(stop_first, px_s, np.where(tgt_first, px_t, lastc))
    out = side * (px / e - 1) * 1e4
    out[~(e > 0)] = np.nan
    return out


def control_mean(day: dict, s: int, i: int, side: int, risk: float) -> float | None:
    v = bracket_all(day["o"], day["h"], day["l"], day["c"], i, side, risk)
    v = np.delete(v, s)
    v = v[~np.isnan(v)]
    return float(v.mean()) if len(v) >= 20 else None


# ---------- scoring ----------
def score(files, cfg, mode: str = "rule", seed: int = 7, plant_bp: float = 0.0) -> list[dict]:
    """mode: 'rule' (the setup), 'break' (no retest), 'random' (random name/minute/side at the rule's count)."""
    rng = np.random.default_rng(seed)
    rows = []
    prev_raw = H.load_day(files[0])
    for f in files[1:]:
        raw = H.load_day(f)
        day, prev = H.universe(raw, prev_raw, cfg["top_n"], 10.0, 500.0)
        prev_raw = raw
        if len(day["syms"]) < 21:
            continue
        found = setups(day, prev, retest=(mode != "break"))
        if mode == "random":
            n = len(found)
            found = [(int(rng.integers(len(day["syms"]))), int(rng.integers(10, 360)), int(rng.choice([1, -1])), r)
                     for (_s, _i, _sd, r) in found[:n]]
        for s, i, side, risk in found:
            o, h, lo, c = (day[k][s].copy() for k in ("o", "h", "l", "c"))
            if plant_bp:
                bump = 1 + side * plant_bp / 1e4
                for arr in (o, h, lo, c):
                    arr[i + 1:] *= bump                         # a +plant_bp move in the trade's favour after entry
            r = bracket(o, h, lo, c, i, side, risk)
            ctl = control_mean(day, s, i, side, risk)
            if r is None or ctl is None:
                continue
            rows.append({"day": f.stem, "sym": day["syms"][s], "side": side, "ret": r, "ctl": ctl, "lift": r - ctl,
                         "win": r > 0})
    return rows


def day_t(rows: list[dict], key: str) -> float:
    by = {}
    for r in rows:
        by.setdefault(r["day"], []).append(r[key])
    m = np.array([np.mean(v) for v in by.values()])
    if len(m) < 3 or m.std(ddof=1) == 0:
        return float("nan")
    # per-trade mean, day-clustered standard error
    x = np.array([r[key] for r in rows])
    mu = x.mean()
    g = np.array([np.sum(np.array(v) - mu) for v in by.values()])
    se = math.sqrt((g ** 2).sum() * len(g) / (len(g) - 1)) / len(x)
    return float(mu / se) if se > 0 else float("nan")


def summary(rows: list[dict], cost: float, stress: float) -> dict:
    if not rows:
        return {"n": 0}
    days = sorted({r["day"] for r in rows})
    mid = days[len(days) // 2]
    h1, h2 = [r for r in rows if r["day"] < mid], [r for r in rows if r["day"] >= mid]
    mean = lambda rs, k: float(np.mean([r[k] for r in rs])) if rs else float("nan")  # noqa: E731
    return {"n": len(rows), "days": len(days), "win": mean(rows, "win"), "gross": mean(rows, "ret"), "ctl": mean(rows, "ctl"),
            "lift": mean(rows, "lift"), "t": day_t(rows, "lift"), "lift_h": (mean(h1, "lift"), mean(h2, "lift")),
            "net": mean(rows, "ret") - cost, "net_h": (mean(h1, "ret") - cost, mean(h2, "ret") - cost), "net_stress": mean(rows, "ret") - stress,
            "longs": sum(r["side"] > 0 for r in rows), "long_lift": mean([r for r in rows if r["side"] > 0], "lift"),
            "short_lift": mean([r for r in rows if r["side"] < 0], "lift")}


def verdict(s: dict) -> str:
    if s["n"] < 300:
        return "UNDERPOWERED"
    edge = s["lift_h"][0] > 0 and s["lift_h"][1] > 0 and s["t"] >= 2.0
    if not edge:
        return "FAIL"
    return "PROFITABLE" if (s["net_h"][0] > 0 and s["net_h"][1] > 0 and s["net_stress"] > 0) else "EDGE (not profitable after cost)"


def line(name: str, s: dict) -> str:
    if not s["n"]:
        return f"{name:<16} no trades"
    return (f"{name:<16} {s['n']:5d} trades / {s['days']} days  win {100 * s['win']:.0f}%  gross {s['gross']:+.1f} bp  "
            f"control {s['ctl']:+.1f}  LIFT {s['lift']:+.1f} (halves {s['lift_h'][0]:+.1f} / {s['lift_h'][1]:+.1f}, t {s['t']:+.2f})  "
            f"net {s['net']:+.1f} (halves {s['net_h'][0]:+.1f} / {s['net_h'][1]:+.1f})  longs {s['longs']} lift {s['long_lift']:+.1f} / shorts lift {s['short_lift']:+.1f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warehouse", default="sip")
    ap.add_argument("--top-n", type=int, default=400)
    ap.add_argument("--cost-bp", type=float, default=6.0)
    ap.add_argument("--stress-bp", type=float, default=10.0)
    ap.add_argument("--checks", action="store_true", help="run only the no-edge and planted checks")
    a = ap.parse_args(argv)
    wh = H.ROOT / "ai_reports" / "warehouse" / a.warehouse
    files = sorted(wh.glob("*.npz"))
    cfg = {"top_n": a.top_n}
    print(f"{a.warehouse}: {len(files)} sessions {files[0].stem}..{files[-1].stem}, top {a.top_n}, cost {a.cost_bp} bp", flush=True)
    null = summary(score(files, cfg, "random"), a.cost_bp, a.stress_bp)
    plant = summary(score(files, cfg, "rule", plant_bp=10.0), a.cost_bp, a.stress_bp)
    print("CHECKS (must pass before the read):")
    print(line("no-edge random", null))
    print(line("planted +10 bp", plant))
    ok = null["n"] and abs(null["lift"]) <= 2.0 and plant["n"] and plant["lift"] >= 5.0
    print(f"  checks {'OK' if ok else 'FAILED: the read is not valid'}", flush=True)
    if a.checks or not ok:
        return 0 if ok else 1
    rule = summary(score(files, cfg, "rule"), a.cost_bp, a.stress_bp)
    brk = summary(score(files, cfg, "break"), a.cost_bp, a.stress_bp)
    print("\nRESULT")
    print(line("break + retest", rule))
    print(line("break, no retest", brk))
    print(f"\nVERDICT (break + retest, per trade): {verdict(rule)}   (a 1:2 bracket needs a 33% win rate before cost)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
