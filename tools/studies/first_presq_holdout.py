#!/usr/bin/env python3
"""first_presq_holdout.py — docs/studies/first_presq_holdout_prereg.json (amended .. amended_4, ready).

Stage 1 (Argon's in-sample warehouse, 2026-04-27..2026-10-15, information only):
  --fidelity   run Argon's own eval_day_idx (tools/studies/first_presq_argon.py, unchanged) on the pinned 120 days and
               compare setup 4 (N, clipped mean, median, H1, H2, drop-3) and setup 1 N with Argon's printed figures.
  --counts     fire COUNTS only: Argon verbatim setup 4 vs our ARGON-causal port (SIP lines), and how many differ.
  --extract    one candidate per name-day (the first square-band minute; Argon's rule cannot fire later) with its
               features on SIP lines (ARGON-causal) and on gridded IEX lines (WINNER), plus the house controls.
               Writes CAND_DIR/<day>.json.
  --grid       the 96 configurations on the IEX candidates: day-equal net after 5 bp, lift_d, lift_x, eligibility,
               WINNER; ARGON's configuration on SIP and IEX; the MDE projections at 300 days per half.

Why one candidate per name-day: Argon's prior_in_today blocks every minute after the first minute in the square band
(fast >= -35, slow >= -35, |fast - slow| <= 15), and every setup-4 condition is read at that minute. So the rule fires
at most once, at that minute, and every grid configuration only changes which candidates pass.

Causal port (amended_4 #3): no whole-day len >= 80, no n-31 future-bar bound; entry SIP open of minute m+1, exit SIP
close of minute m+15. IEX (amended_4 #4): lines on the live grid (2-15 minute gaps flat-filled, filled minute volume 0),
rises and the volume mean by minute; universe and dollar-volume filters on SIP; price band and day-change numerator on
the IEX close; previous close from SIP.
USAGE (mini): .venv/bin/python tools/studies/first_presq_holdout.py --fidelity | --counts | --extract | --grid
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from itertools import product

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]
import indicator_confirm_hist as H  # noqa: E402
from wr_trend_trade import seeded  # noqa: E402

OUT = os.path.expanduser("~/replay_runs/first_presq")
CAND_DIR = os.path.join(OUT, "cand_insample")
FIRST_IN, LAST_IN = "2026-04-27", "2026-10-15"
N_PINNED = 120
SPLIT = "2026-07-29"
HOLD = 15
CTRL_SPAN = 60
RTH_END = 960
COST = 5.0                       # bp, the grade
MDE_K = 3.08                     # (2.24 + 0.84)
# setup1_N: Argon's report table gives 160 for FIRST_PRESQ_morning_vol2x; "N=89" is a stale label inside the script.
ARGON_PRINTED = {"N": 448, "mean": 7.38, "median": 2.88, "H1": 7.46, "H2": 7.17, "drop3": 5.32, "setup1_N": 160}
K4 = "4_FIRST_PRESQ_morning_vol1.5x (09:45-11:30, chg 0.5..5%, vol>=1.5x)"
K1 = "1_FIRST_PRESQ_morning_vol2x (N=89 exact)"

GRID = {
    "fast_rise": (10.0, 15.0),
    "slow_rise": (0.0, 2.0),
    "vol_mult": (1.5, 2.0),
    "window_end": (660, 690, 780),          # 11:00, 11:30, 13:00
    "band": ((0.5, 5.0), (1.0, 4.5)),
    "floor": (20.0, 25.0),
}
ARGON_CFG = {"fast_rise": 15.0, "slow_rise": 2.0, "vol_mult": 1.5, "window_end": 690, "band": (0.5, 5.0), "floor": 20.0}


def pinned_days(wh: str = H.WH) -> list[str]:
    have = [{x[:-4] for x in os.listdir(os.path.join(wh, f)) if x.endswith(".npz")} for f in ("iex", "sip")]
    days = sorted(d for d in have[0] & have[1] if FIRST_IN <= d <= LAST_IN)
    if len(days) != N_PINNED:
        raise SystemExit(f"pinned day list: expected {N_PINNED} files {FIRST_IN}..{LAST_IN}, found {len(days)}")
    return days


def cfg_key(c: dict) -> str:
    """amended_3 r2_8 tie-break string."""
    hh, mm = divmod(int(c["window_end"]), 60)
    return (f"fr{c['fast_rise']:g}-sr{c['slow_rise']:g}-v{c['vol_mult']:g}-w{hh:02d}{mm:02d}"
            f"-c{c['band'][0]:g}-{c['band'][1]:g}-p{c['floor']:g}")


def all_cfgs() -> list[dict]:
    ks = list(GRID)
    return [dict(zip(ks, vals)) for vals in product(*(GRID[k] for k in ks))]


# ── lines (Argon's arithmetic, copied: tools/studies/first_presq_argon.py) ──────────────────────────────────────────
def ema_arr(x, span):
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(x, dtype=np.float64)
    if len(x) == 0:
        return out
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def pctr_series(h, l, c, length, span):
    n = len(c)
    raw = np.full(n, np.nan)
    for i in range(length - 1, n):
        hh = np.max(h[i - length + 1: i + 1])
        ll = np.min(l[i - length + 1: i + 1])
        raw[i] = (hh - c[i]) / (hh - ll) * -100.0 if hh > ll else -50.0
    valid = ~np.isnan(raw)
    if not np.any(valid):
        return raw
    first = int(np.argmax(valid))
    sm = np.full(n, np.nan)
    sm[first:] = ema_arr(raw[first:], span)
    return np.round(sm, 1)


def grid_fill(rows: np.ndarray) -> np.ndarray:
    """Live minute grid: a gap of 2-15 minutes is filled flat at the previous close with volume 0; longer gaps stay."""
    if len(rows) == 0:
        return rows
    out = [rows[0]]
    for r in rows[1:]:
        prev = out[-1]
        gap = int(r[0] - prev[0])
        if 2 <= gap <= 15:
            c = prev[4]
            for k in range(1, gap):
                out.append(np.array([prev[0] + k, c, c, c, c, 0.0]))
        out.append(r)
    return np.array(out)


def in_square(f, s) -> np.ndarray:
    return (f >= -35.0) & (s >= -35.0) & (np.abs(f - s) <= 15.0)


# ── per name-day candidate ──────────────────────────────────────────────────────────────────────────────────────────
def sip_universe(prev_rows, cur_rows) -> bool:
    """Argon's SIP universe, causal: first RTH bar open in [20,150], prev day >= 120 bars and $vol/min >= $100k."""
    if prev_rows is None or len(prev_rows) < 120 or len(cur_rows) == 0:
        return False
    if not (20.0 <= cur_rows[0, 1] <= 150.0):
        return False
    return float(np.mean(prev_rows[:, 4] * prev_rows[:, 5])) >= 100000.0


def candidate(prev_rows, cur_rows, sip_prev_close: float, sip_cur: np.ndarray, *, grid: bool) -> dict | None:
    """The first square-band minute and its setup-4 features on these line bars (SIP raw, or IEX on the live grid).
    Liquidity (dvol15) is always on SIP; day change uses this feed's close over the SIP previous close."""
    if grid:
        prev_rows, cur_rows = grid_fill(prev_rows), grid_fill(cur_rows)
    if prev_rows is None or len(prev_rows) == 0 or len(cur_rows) == 0:
        return None
    tail = prev_rows[-120:]
    comb = np.vstack([tail, cur_rows])
    start = len(tail)
    mins, h, l, c, v = comb[:, 0], comb[:, 2], comb[:, 3], comb[:, 4], comb[:, 5]
    fast = pctr_series(h, l, c, 21, 7)
    slow = pctr_series(h, l, c, 112, 3)
    sq = in_square(fast[start:], slow[start:])
    hits = np.flatnonzero(sq)
    if len(hits) == 0:
        return {"why": "no_square"}
    i = start + int(hits[0])
    m = int(mins[i])
    out = {"minute": m, "i_today": i - start}
    if i < start + 15:
        out["why"] = "before_15_bars"
        return out
    out["overbought"] = bool(fast[i] >= -20.0 and slow[i] >= -20.0)
    if grid:                                   # rises and the volume mean by MINUTE on the grid
        pos = {int(mm): k for k, mm in enumerate(mins)}
        k3, k5 = pos.get(m - 3), pos.get(m - 5)
        if k3 is None or k5 is None:
            out["why"] = "grid_hole"
            return out
        out["fast_rise"] = float(fast[i] - fast[k3])
        out["slow_rise"] = float(slow[i] - slow[k5])
        vols = [float(v[pos[mm]]) if mm in pos else 0.0 for mm in range(m - 15, m)]
    else:                                      # Argon: by bar index
        out["fast_rise"] = float(fast[i] - fast[i - 3])
        out["slow_rise"] = float(slow[i] - slow[i - 5])
        vols = list(v[i - 15:i])
    avg = float(np.mean(vols))
    out["vol_ratio"] = float(v[i]) / avg if avg > 0 else None
    out["close"] = float(c[i])
    out["day_chg"] = (float(c[i]) / sip_prev_close - 1.0) * 100.0 if sip_prev_close > 0 else None
    # SIP dvol15 as Argon's l.109: the 16 SIP bars ending at minute m by INDEX (i-15..i), not a clock window.
    sm = sip_cur[sip_cur[:, 0] <= m][-16:]
    out["dvol15"] = float(np.mean(sm[:, 4] * sm[:, 5])) if len(sm) else 0.0
    return out


def passes(f: dict, cfg: dict) -> bool:
    if not f or f.get("why") or f.get("overbought") or f.get("vol_ratio") is None or f.get("day_chg") is None:
        return False
    lo, hi = cfg["band"]
    return (585 <= f["minute"] <= cfg["window_end"] and cfg["floor"] <= f["close"] <= 150.0
            and f["dvol15"] >= 150000.0 and lo <= f["day_chg"] <= hi
            and f["fast_rise"] >= cfg["fast_rise"] and f["slow_rise"] >= cfg["slow_rise"]
            and f["vol_ratio"] >= cfg["vol_mult"])


# ── fills and controls (house: pitch_open shape, HOLD 15, minute based, SIP) ────────────────────────────────────────
def minute_maps(sip: dict) -> dict:
    return {s: ({int(r[0]): r[1] for r in a}, {int(r[0]): r[4] for r in a}) for s, a in sip.items()}


def gross(mm: dict, sym: str, e: int) -> float | None:
    o, c = mm.get(sym, ({}, {}))
    a, b = o.get(e), c.get(e + HOLD - 1)
    if not a or not b or a <= 0 or b <= 0 or e + HOLD - 1 >= RTH_END:
        return None
    return (b / a - 1.0) * 1e4


def d_draw(mm, sym, day, e, tag):
    o, c = mm.get(sym, ({}, {}))
    cand = [x for x in range(e + 1, e + CTRL_SPAN + 1)
            if o.get(x, 0) > 0 and c.get(x + HOLD - 1, 0) > 0 and x + HOLD - 1 < RTH_END]
    return seeded(sym, day, e, tag).choice(cand) if cand else None


def x_draw(mm, sym, day, e, tag, pool, cache=None):
    """Another SIP symbol (sorted day pool) with an open at e and a close at e+14. cache: e -> valid list."""
    if cache is not None and e in cache:
        valid = cache[e]
    else:
        valid = [s for s in pool if mm[s][0].get(e, 0) > 0 and mm[s][1].get(e + HOLD - 1, 0) > 0]
        if cache is not None:
            cache[e] = valid
    cand = [s for s in valid if s != sym]
    return seeded(sym, day, e, tag).choice(cand) if cand else None


def controls(mm, sym, day, e, pool, cache=None) -> dict:
    out = {}
    for tag in ("fp-d", "fp-d2"):
        c = d_draw(mm, sym, day, e, tag)
        out[tag] = gross(mm, sym, c) if c is not None else None
    for tag in ("fp-x", "fp-x2"):
        s = x_draw(mm, sym, day, e, tag, pool, cache)
        out[tag] = gross(mm, s, e) if s is not None else None
    return out


# ── stage-1 extraction ──────────────────────────────────────────────────────────────────────────────────────────────
def extract_day(args) -> dict:
    day, prev, wh = args
    sip_p, sip_c = H_load(wh, "sip", prev), H_load(wh, "sip", day)
    iex_p, iex_c = H_load(wh, "iex", prev), H_load(wh, "iex", day)
    mm = minute_maps(sip_c)
    pool = sorted(sip_c)
    rows, skips, xcache = [], {}, {}
    for sym in pool:
        cur, pr = sip_c[sym], sip_p.get(sym)
        if not sip_universe(pr, cur):
            continue
        pc = float(pr[-1, 4])
        rec = {"sym": sym}
        for feed, (p_rows, c_rows, g) in (("sip", (pr, cur, False)), ("iex", (iex_p.get(sym), iex_c.get(sym), True))):
            if c_rows is None or p_rows is None:
                rec[feed] = {"why": "no_feed"}
                continue
            rec[feed] = candidate(p_rows, c_rows, pc, cur, grid=g)
        for feed in ("sip", "iex"):
            f = rec.get(feed) or {}
            why = f.get("why") if f else "none"
            skips[f"{feed}:{why or 'candidate'}"] = skips.get(f"{feed}:{why or 'candidate'}", 0) + 1
            if f and not f.get("why"):
                e = int(f["minute"]) + 1
                f["entry_min"] = e
                f["gross"] = gross(mm, sym, e)
                f.update(controls(mm, sym, day, e, pool, xcache))
        rows.append(rec)
    return {"day": day, "rows": rows, "skips": skips}


def H_load(wh, feed, day):
    if wh == H.WH:
        return H.load_raw(feed, day)
    saved = H.WH
    try:
        H.WH = wh
        return H.load_raw(feed, day)
    finally:
        H.WH = saved


# ── statistics ──────────────────────────────────────────────────────────────────────────────────────────────────────
def day_equal(by_day: dict[str, list[float]]) -> dict:
    means = [statistics.fmean(v) for v in by_day.values() if v]
    n = len(means)
    if n < 2:
        return {"n_days": n, "mean": means[0] if means else None, "t": None}
    sd = statistics.stdev(means)
    m = statistics.fmean(means)
    return {"n_days": n, "mean": m, "t": (m / (sd / math.sqrt(n)) if sd > 0 else None)}


def score_cfg(days_rows: list[dict], feed: str, cfg: dict) -> dict:
    net, ld, lx, n = {}, {}, {}, 0
    for dr in days_rows:
        for rec in dr["rows"]:
            f = rec.get(feed)
            if not passes(f, cfg) or f.get("gross") is None:
                continue
            n += 1
            g = f["gross"]
            net.setdefault(dr["day"], []).append(g - COST)
            if f.get("fp-d") is not None:
                ld.setdefault(dr["day"], []).append(g - f["fp-d"])
            if f.get("fp-x") is not None:
                lx.setdefault(dr["day"], []).append(g - f["fp-x"])
    return {"cfg": cfg_key(cfg), "n": n, "days": len(net), "net": day_equal(net),
            "lift_d": day_equal(ld), "lift_x": day_equal(lx)}


def projection(days_rows: list[dict], feed: str, cfg: dict) -> dict:
    """amended_3 r2_5: MDE_proj = 3.08 x sd_in x sqrt(1 / (300 r)); sd_in from control - control2 day means only."""
    dd, dx, with_trade = {}, {}, set()
    for dr in days_rows:
        for rec in dr["rows"]:
            f = rec.get(feed)
            if not passes(f, cfg) or f.get("gross") is None:
                continue
            with_trade.add(dr["day"])
            if f.get("fp-d") is not None and f.get("fp-d2") is not None:
                dd.setdefault(dr["day"], []).append(f["fp-d"] - f["fp-d2"])
            if f.get("fp-x") is not None and f.get("fp-x2") is not None:
                dx.setdefault(dr["day"], []).append(f["fp-x"] - f["fp-x2"])
    sds = [statistics.stdev([statistics.fmean(v) for v in x.values()]) for x in (dd, dx) if len(x) >= 2]
    r = len(with_trade) / max(1, len(days_rows))
    sd = max(sds) if sds else None
    proj = MDE_K * sd * math.sqrt(1.0 / (300 * r)) if sd is not None and r > 0 else None
    return {"sd_in": sd, "r": r, "mde_proj_600": proj, "ok_8bp": bool(proj is not None and proj <= 8.0)}


# ── modes ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def run_fidelity() -> dict:
    import first_presq_argon as A
    days = pinned_days()
    if list(A.days) != days:
        raise SystemExit("Argon's warehouse_days() differs from the pinned 120 files")
    with ProcessPoolExecutor(max_workers=4) as ex:
        res = list(ex.map(A.eval_day_idx, range(1, len(days))))
    rows4 = [r for x in res for r in x[K4]]
    n1 = sum(len(x[K1]) for x in res)
    w15 = np.clip([r[4] for r in rows4], -200.0, 200.0)
    dseen = [r[0] for r in rows4]
    by = {}
    for d_, v in zip(dseen, w15):
        by.setdefault(d_, []).append(v)
    top3 = {d_ for d_, _ in sorted(by.items(), key=lambda x: np.mean(x[1]), reverse=True)[:3]}
    got = {"N": len(rows4), "mean": float(np.mean(w15)), "median": float(np.median([r[4] for r in rows4])),
           "H1": float(np.mean([v for d_, v in zip(dseen, w15) if d_ <= SPLIT])),
           "H2": float(np.mean([v for d_, v in zip(dseen, w15) if d_ > SPLIT])),
           "drop3": float(np.mean([v for d_, v in zip(dseen, w15) if d_ not in top3])), "setup1_N": n1}
    ok = {k: (got[k] == ARGON_PRINTED[k]) if k in ("N", "setup1_N") else abs(got[k] - ARGON_PRINTED[k]) <= 0.05 + 0.005
          for k in ARGON_PRINTED}      # printed to 2 dp: within 0.05 of the rounded print
    out = {"got": got, "argon": ARGON_PRINTED, "ok": ok, "pass": all(ok.values()),
           "fires": sorted((r[0], r[1]) for r in rows4)}
    os.makedirs(OUT, exist_ok=True)
    json.dump(out, open(os.path.join(OUT, "fidelity.json"), "w"), indent=1)
    return out


def load_cands() -> list[dict]:
    return [json.load(open(os.path.join(CAND_DIR, f))) for f in sorted(os.listdir(CAND_DIR)) if f.endswith(".json")]


def run_extract(wh: str = H.WH) -> None:
    days = pinned_days(wh)
    os.makedirs(CAND_DIR, exist_ok=True)
    jobs = [(days[k], days[k - 1], wh) for k in range(1, len(days))]
    with ProcessPoolExecutor(max_workers=4) as ex:
        for res in ex.map(extract_day, jobs):
            json.dump(res, open(os.path.join(CAND_DIR, f"{res['day']}.json"), "w"))


def run_counts() -> dict:
    fid = json.load(open(os.path.join(OUT, "fidelity.json")))
    verb = {tuple(x) for x in fid["fires"]}
    caus = set()
    for dr in load_cands():
        for rec in dr["rows"]:
            f = rec.get("sip")
            if passes(f, ARGON_CFG) and f.get("gross") is not None:
                caus.add((dr["day"], rec["sym"]))
    diff = len(verb ^ caus)
    out = {"verbatim": len(verb), "causal": len(caus), "only_verbatim": len(verb - caus),
           "only_causal": len(caus - verb), "differ_share": diff / max(1, len(verb)),
           "tell_operator": diff / max(1, len(verb)) > 0.05}
    json.dump(out, open(os.path.join(OUT, "counts.json"), "w"), indent=1)
    return out


def run_grid() -> dict:
    fid = json.load(open(os.path.join(OUT, "fidelity.json")))
    ruling = os.path.join(OUT, "operator_fidelity_ruling.json")
    if not fid.get("pass") and not os.path.exists(ruling):
        raise SystemExit("refusing: fidelity did not pass and no operator ruling is recorded (prereg amended_2 / 4)")
    dr = load_cands()
    rows = [score_cfg(dr, "iex", c) | {"_cfg": c} for c in all_cfgs()]

    def elig(r):
        return (r["n"] >= 300 and r["days"] >= 60 and (r["lift_d"]["mean"] or 0) > 0 and (r["lift_x"]["mean"] or 0) > 0)
    el = [r for r in rows if elig(r)]
    el.sort(key=lambda r: (-(r["net"]["mean"] or -1e9), -r["n"], r["cfg"]))
    winner = el[0] if el else None
    argon_sip = score_cfg(dr, "sip", ARGON_CFG)
    argon_iex = score_cfg(dr, "iex", ARGON_CFG)
    out = {
        "eligible": len(el),
        "winner": None if winner is None else {k: v for k, v in winner.items() if k != "_cfg"},
        "runner_up": None if len(el) < 2 else {k: v for k, v in el[1].items() if k != "_cfg"},
        "top10_by_net": [{k: v for k, v in r.items() if k != "_cfg"} for r in el[:10]],
        "argon_sip_causal": argon_sip, "argon_iex": argon_iex,
        "projection": {"ARGON_sip": projection(dr, "sip", ARGON_CFG),
                       "WINNER_iex": None if winner is None else projection(dr, "iex", winner["_cfg"])},
        "fallback_sole_primary_argon": winner is None,
    }
    json.dump(out, open(os.path.join(OUT, "grid.json"), "w"), indent=1, default=str)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    for m in ("fidelity", "counts", "extract", "grid"):
        g.add_argument(f"--{m}", action="store_true")
    a = ap.parse_args()
    if a.fidelity:
        out = run_fidelity()
        out.pop("fires")
    elif a.extract:
        run_extract()
        out = {"written": len(os.listdir(CAND_DIR))}
    elif a.counts:
        out = run_counts()
    else:
        out = run_grid()
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
