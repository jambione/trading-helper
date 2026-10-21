#!/usr/bin/env python3
"""Confirmation indicators on history, powered. Spec: docs/studies/indicator_confirm_hist_prereg.json.

H1-H5: do ADX trend / chop, OBV or A/D confirmation, or the article's confluence pick desk-style pre-square (PRE)
entries on a 400-name liquid sample that beat both the same name's later minute and a random other name at the
same minute, by more than the entries they exclude? V: do the 10/8 video setups S3-S7 (S2 dropped, operator 10/21)
pay as standalone 10-minute entries on every warehouse name with 10 prior SIP sessions?

Data: the warehouse (ai_reports/warehouse/{iex,sip}/<day>.npz: RTH 1-minute bars, split-adjusted, no quotes). The
first 10 sessions are look-back only. Features and verdict code come from indicator_confirm_study (desk study), PRE
from early_entry_honest (warm %R lines, as prescreen_turn), setups from structure_pullback's scanners with the
prereg's warehouse adaptation (R-bound price = SIP close of the bar ending at t, no stale-quote rule, prior close
and low from the prior session's SIP bars).

  (default)  count step: per H cell and V setup and half, entries, name-days, days, projected MDE from CONTROL
             outcomes only, the control's own IN-OUT gap, and the V detector funnels; writes count.json
  --days N   limit to the first N scored sessions (smoke runs; never for the verdict)
  --workers  processes (default 4; nice the run on the mini)

Not written yet (next step): --null, --placebo, --handcheck, --score. Runs on the mini after the close.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bars_structure as BS  # noqa: E402
import early_entry_honest as E  # noqa: E402
import indicator_confirm_study as S  # noqa: E402
import structure_pullback as SP  # noqa: E402

WH = os.path.join(E.ROOT, "ai_reports", "warehouse")
OUT_DIR = os.path.join(E.ROOT, "ai_reports", "indicator_confirm_hist")

SAMPLE = 400
LOOKBACK = 10
HALF_A_END = "2026-07-29"
V_SETUPS = ("S3", "S4", "S5", "S6", "S7")
CELLS = S.CELLS
MIN_ENTRIES, MIN_DAYS, MIN_SIDE = 300, 20, 3
PRE_ONSET_LO, PRE_ONSET_HI = S.ARM_START - 1, S.ARM_END - 1     # decision m = onset + 1 in 10:00-15:30
REFRACT = 15
SAME_TAG, CROSS_TAG, SAMPLE_TAG = "indicator-hist-v1", "indicator-hist-cross", "indicator-hist-sample"


# ── warehouse ────────────────────────────────────────────────────────────────

def warehouse_days() -> list[str]:
    have = [{x[:-4] for x in os.listdir(os.path.join(WH, f)) if x.endswith(".npz")} for f in ("iex", "sip")]
    return sorted(have[0] & have[1])


def load_raw(feed: str, day: str) -> dict[str, np.ndarray]:
    """sym -> array of rows (minute, o, h, l, c, v), sorted by minute, RTH only (the warehouse is RTH-only)."""
    z = np.load(os.path.join(WH, feed, f"{day}.npz"), allow_pickle=False)
    syms, idx, mins = z["syms"], z["sym_idx"], z["minute"]
    rows = np.column_stack([mins.astype(float), z["o"], z["h"], z["l"], z["c"], z["v"]]).astype(float)
    order = np.lexsort((mins, idx))
    idx, rows = idx[order], rows[order]
    bounds = np.searchsorted(idx, np.arange(len(syms) + 1))
    out = {}
    for k, s in enumerate(syms):
        a, b = bounds[k], bounds[k + 1]
        if b > a:
            r = rows[a:b]
            r = r[(r[:, 0] >= S.RTH_START) & (r[:, 0] < S.RTH_END)]
            if len(r):
                out[str(s)] = r
    return out


def bars_dict(r: np.ndarray) -> dict[int, tuple]:
    return {int(x[0]): (x[1], x[2], x[3], x[4], x[5]) for x in r}


def sip_oc(r: np.ndarray) -> dict[int, tuple]:
    """minute -> (open, close), the shape early_entry_honest.markout reads."""
    return {int(x[0]): (x[1], x[4]) for x in r}


def frame(day: str, r: np.ndarray):
    import pandas as pd
    base = pd.Timestamp(day, tz=E.ET)
    t = [(base + pd.Timedelta(minutes=int(m))).tz_convert("UTC") for m in r[:, 0]]
    return pd.DataFrame({"time": t, "open": r[:, 1], "high": r[:, 2], "low": r[:, 3], "close": r[:, 4],
                         "volume": r[:, 5]})


def bs_rows(day: str, r: np.ndarray) -> list:
    """bars_structure 1-minute rows: [start_ts, o, h, l, c, v]."""
    t0 = BS.et_ts(day, 0)
    return [[t0 + 60.0 * x[0], x[1], x[2], x[3], x[4], x[5]] for x in r]


def sample_names(day: str, names: list[str], n: int = SAMPLE) -> list[str]:
    rng = random.Random(int(hashlib.sha256(f"{day}|{SAMPLE_TAG}".encode()).hexdigest()[:16], 16))
    names = sorted(names)
    return names if len(names) <= n else sorted(rng.sample(names, n))


def half_of(day: str) -> str:
    return "A" if day <= HALF_A_END else "B"


# ── PRE entries ──────────────────────────────────────────────────────────────

def pre_minutes(ser: dict) -> list[int]:
    """Decision minutes m = PRE onset bar + 1 in 10:00-15:30, 15-minute refractory on onsets (every minute counts
    as on the board, as prescreen_turn)."""
    m, f, s = ser["minutes"], ser["fast"], ser["slow"]
    flags = [E.pre_flag(f, s, i) for i in range(len(m))]
    onsets, _off = E.fires(m, flags, set(m), start=PRE_ONSET_LO, end=PRE_ONSET_HI, refract=REFRACT)
    return [x + 1 for x in onsets]


def control_minute(sym: str, day: str, m: int, sip: dict, hold: int = S.HOLD) -> int | None:
    """Same eligibility as the desk study's control (decision minute c in m+1 .. m+60, entry c+1, exit c+hold in
    RTH), with this prereg's seed tag."""
    elig = [c for c in range(m + 1, m + S.CTRL_SPAN + 1)
            if c + hold < S.RTH_END and (c + 1) in sip and (c + hold) in sip]
    if not elig:
        return None
    seed = int(hashlib.sha256(f"{sym}|{day}|{m}|{SAME_TAG}".encode()).hexdigest()[:16], 16)
    return random.Random(seed).choice(elig)


# ── V setups on warehouse bars ───────────────────────────────────────────────

class Session60:
    """Per-name 60-minute bars, prior-session low and last close of one session (cached across the 10 days that
    use it as look-back)."""

    def __init__(self, day: str, r: np.ndarray):
        b1 = BS.minute_bars(bs_rows(day, r), day)
        self.b60 = BS.aggregate(b1, day, 60)
        self.low = min(b.l for b in b1) if b1 else None
        self.close = b1[-1].c if b1 else None


_RAW: dict[tuple[str, str], dict] = {}
_S60: dict[tuple[str, str], "Session60"] = {}


def cached_sip(day: str, keep: set[str]) -> dict:
    """Per-process cache of SIP day files; workers get contiguous blocks of days, so each prior session is loaded
    and aggregated once per worker instead of once per scored day. Days outside `keep` are evicted."""
    for key in [k for k in _RAW if k[1] not in keep]:
        del _RAW[key]
    for key in [k for k in _S60 if k[0] not in keep]:
        del _S60[key]
    if ("sip", day) not in _RAW:
        _RAW[("sip", day)] = load_raw("sip", day)
    return _RAW[("sip", day)]


def session60(day: str, sym: str, r: np.ndarray) -> "Session60":
    if (day, sym) not in _S60:
        _S60[(day, sym)] = Session60(day, r)
    return _S60[(day, sym)]


def v_context(sym: str, day: str, today: np.ndarray, prior: list[tuple[str, "Session60"]]):
    """structure_pullback.Ctx on warehouse bars (prereg V adaptation)."""
    b1 = BS.minute_bars(bs_rows(day, today), day)
    series = BS.concat_sessions([s.b60 for _d, s in prior] + [BS.aggregate(b1, day, 60)])
    ends = [b.end for b in b1]

    def entry(t):
        # (a) the R-bound price is the SIP close of the 1-minute bar ending at t (last completed bar by t); (b) no
        # stale-quote rule.
        k = np.searchsorted(ends, t, side="right") - 1
        return (b1[k].c, 0.0) if k >= 0 else None
    _prev_day, prev = prior[-1]
    return SP.Ctx(sym, day, b1, series, [d for d, _s in prior[-5:]], prev.low, prev.close, entry)


def v_entries(ctx, counts: Counter) -> list[tuple[str, int]]:
    """(setup, decision minute m = ET minute of the setup bar's end) for each setup's first entry."""
    out = []
    for s in V_SETUPS:
        sig = SP.SCANNERS[s](ctx, counts)
        if sig and not sig.get("stale"):
            out.append((s, BS.et_hm(sig["t"])))
    return out


# ── one session ──────────────────────────────────────────────────────────────

def day_task(args) -> dict:
    """Everything the count step needs from one scored session: per PRE entry its cell sides and same-name control
    net10; per V entry its control net10; funnels. Reads no entry outcome."""
    k, days = args
    day, prev = days[k], days[k - 1]
    prior_days = days[k - LOOKBACK:k]
    counts: Counter = Counter()
    iex, sip, p_iex = load_raw("iex", day), load_raw("sip", day), load_raw("iex", prev)
    pre_rows = []
    names = sample_names(day, [s for s in iex if s in sip and s in p_iex])
    E.WARM_LINES = True
    for sym in names:
        sipm = sip_oc(sip[sym])
        ser = E.name_day_series(frame(day, iex[sym]), frame(prev, p_iex[sym][-E.CON_PRIOR_BARS:]))
        ind = S.indicators(bars_dict(iex[sym]))
        for m in pre_minutes(ser):
            counts["pre_onsets"] += 1
            c = control_minute(sym, day, m, sipm)
            if c is None:
                counts["pre_no_control"] += 1
                continue
            f = S.features_at(ind, m)
            if not f["ok"]:
                counts["pre_thin_bars"] += 1
            pre_rows.append({"day": day, "sym": sym, "m": m,
                             "sides": {cell: S.cell_side(f, cell, m) for cell in CELLS},
                             "ctrl": S.net(sipm, c)})
    # V: every name with today's SIP bars and all 10 prior SIP sessions
    keep = set(prior_days) | {day}
    prior_raw = [(d, cached_sip(d, keep)) for d in prior_days]
    v_rows = []
    for sym in sorted(sip):
        if any(sym not in r for _d, r in prior_raw):
            counts["v_short_lookback"] += 1
            continue
        counts["v_namedays"] += 1
        ctx = v_context(sym, day, sip[sym], [(d, session60(d, sym, r[sym])) for d, r in prior_raw])
        sipm = None
        for setup, m in v_entries(ctx, counts):
            sipm = sipm or sip_oc(sip[sym])
            c = control_minute(sym, day, m, sipm)
            if c is None:
                counts[f"{setup}_no_control"] += 1
                continue
            v_rows.append({"day": day, "sym": sym, "m": m, "setup": setup, "ctrl": S.net(sipm, c)})
    return {"day": day, "pre": pre_rows, "v": v_rows, "counts": counts, "sample": len(names)}


# ── count step ───────────────────────────────────────────────────────────────

def _day_t(values: dict[str, float]) -> tuple[float | None, float | None, int]:
    xs = list(values.values())
    if len(xs) < 2:
        return (xs[0] if xs else None), None, len(xs)
    mu, se = statistics.mean(xs), statistics.stdev(xs) / math.sqrt(len(xs))
    return mu, (mu / se if se > 0 else None), len(xs)


def count_h(rows: list[dict]) -> dict:
    """Per cell and half: counts, projected MDE = (t_crit + 0.84) x sqrt(2) x the day-clustered SE of per-day
    (IN control mean - OUT control mean), and that control gap's own mean and t."""
    out = {}
    for cell in CELLS:
        out[cell] = {}
        for half in ("A", "B"):
            ctl = {"IN": defaultdict(list), "OUT": defaultdict(list)}
            names = set()
            for r in rows:
                s = r["sides"][cell]
                if half_of(r["day"]) != half or s is None:
                    continue
                if r["ctrl"] is not None:
                    ctl[s][r["day"]].append(r["ctrl"])
                if s == "IN":
                    names.add((r["sym"], r["day"]))
            n_in = sum(len(v) for v in ctl["IN"].values())
            n_out = sum(len(v) for v in ctl["OUT"].values())
            days = [d for d in ctl["IN"] if len(ctl["IN"][d]) >= MIN_SIDE and len(ctl["OUT"][d]) >= MIN_SIDE]
            gap = {d: statistics.mean(ctl["IN"][d]) - statistics.mean(ctl["OUT"][d]) for d in days}
            g_mu, g_t, nd = _day_t(gap)
            se = statistics.stdev(gap.values()) / math.sqrt(nd) if nd >= 2 else None
            tc = S.t_crit(nd)
            mde = (tc + 0.84) * math.sqrt(2) * se if se is not None and tc is not None else None
            out[cell][half] = {"n_in": n_in, "n_out": n_out, "in_namedays": len(names), "days_both_sides": nd,
                               "t_crit": tc, "projected_mde": mde, "ctrl_gap_in_minus_out": g_mu,
                               "ctrl_gap_t": g_t,
                               "scored": n_in >= MIN_ENTRIES and n_out >= MIN_ENTRIES and nd >= MIN_DAYS}
    return out


def count_v(rows: list[dict]) -> dict:
    out = {}
    for setup in V_SETUPS:
        out[setup] = {}
        for half in ("A", "B"):
            ctl = defaultdict(list)
            names = set()
            for r in rows:
                if r["setup"] == setup and half_of(r["day"]) == half and r["ctrl"] is not None:
                    ctl[r["day"]].append(r["ctrl"])
                    names.add((r["sym"], r["day"]))
            means = {d: statistics.mean(v) for d, v in ctl.items()}
            n = sum(len(v) for v in ctl.values())
            se = statistics.stdev(means.values()) / math.sqrt(len(means)) if len(means) >= 2 else None
            tc = S.t_crit(len(means))
            mde = (tc + 0.84) * math.sqrt(2) * se if se is not None and tc is not None else None
            out[setup][half] = {"n": n, "namedays": len(names), "days": len(means), "t_crit": tc,
                                "projected_mde": mde, "scored": n >= MIN_ENTRIES and len(means) >= MIN_DAYS}
    return out


def funnels(counts: Counter) -> dict:
    return {s: {k[len(s) + 1:]: v for k, v in sorted(counts.items()) if k.startswith(s + "_")} for s in V_SETUPS}


def _bp(x) -> str:
    return "-" if x is None else f"{x * 1e4:+.1f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=0, help="limit to the first N scored sessions (smoke run only)")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    days = warehouse_days()
    ks = list(range(LOOKBACK, len(days)))
    if a.days:
        ks = ks[:a.days]
    print(f"warehouse {len(days)} sessions ({days[0]} .. {days[-1]}); scored {len(ks)}: "
          f"{days[ks[0]]} .. {days[ks[-1]]}; half A <= {HALF_A_END}", flush=True)
    pre, v, counts, per_day = [], [], Counter(), {}
    chunk = max(1, math.ceil(len(ks) / a.workers))     # contiguous blocks keep each worker's look-back cache warm
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for res in ex.map(day_task, [(k, days) for k in ks], chunksize=chunk):
            pre += res["pre"]
            v += res["v"]
            counts.update(res["counts"])
            per_day[res["day"]] = {"sample": res["sample"], "pre": len(res["pre"]), "v": len(res["v"])}
            print(f"  {res['day']}: sample {res['sample']}, PRE {len(res['pre'])}, V {len(res['v'])}", flush=True)
    ch, cv, fn = count_h(pre), count_v(v), funnels(counts)
    print(f"counts: { {k: n for k, n in counts.items() if k[:2] not in V_SETUPS} }")
    for cell in CELLS:
        for half in ("A", "B"):
            c = ch[cell][half]
            print(f"  {cell} {half}: IN {c['n_in']} OUT {c['n_out']} IN name-days {c['in_namedays']} "
                  f"days {c['days_both_sides']} MDE {_bp(c['projected_mde'])} bp; control gap IN-OUT "
                  f"{_bp(c['ctrl_gap_in_minus_out'])} bp (t {c['ctrl_gap_t'] if c['ctrl_gap_t'] is None else round(c['ctrl_gap_t'], 2)}) "
                  f"{'scored' if c['scored'] else 'UNDERPOWERED - not scored'}")
    for setup in V_SETUPS:
        for half in ("A", "B"):
            c = cv[setup][half]
            print(f"  {setup} {half}: entries {c['n']} name-days {c['namedays']} days {c['days']} "
                  f"MDE {_bp(c['projected_mde'])} bp {'scored' if c['scored'] else 'UNDERPOWERED - not scored'}")
        print(f"    funnel {fn[setup]}")
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, "count_smoke.json" if a.days else "count.json")
    with open(path, "w") as f:
        json.dump({"script_sha256": hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest(),
                   "scored_sessions": [days[k] for k in ks], "per_day": per_day, "counts": dict(counts),
                   "h": ch, "v": cv, "funnels": fn}, f, indent=1, default=str)
    print(path)
    print("(count step only; no entry outcome read. --null / --placebo / --handcheck / --score not written yet)")


if __name__ == "__main__":
    main()
