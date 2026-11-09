#!/usr/bin/env python3
"""Sideways conjunction: price pattern + %R + slope, from the current desk functions.

Sideways means the series is traveling between a high and a low.
Rules: docs/studies/flat_conjunction_prereg.json. This file does not recompute
%R or the range. Every feature is ai_entry_watch._range_shape_fields,
ai_entry_watch._wr_slope_fields, or tools.sell_double_check.line_window.

  --score   one read. Refuses unless the canary passes.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import pickle
import random
import statistics
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = os.environ.get("TH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

import ai_entry_watch as ew  # noqa: E402
import ob_observe  # noqa: E402
from tools import sell_double_check as sdc  # noqa: E402

WH = os.path.join(ROOT, "ai_reports", "warehouse", "iex")
CACHE = os.path.expanduser(os.environ.get("REPLAY_CACHE_DIR", "~/replay_cache"))
# Sideways = traveling between a high and a low, not a tight motionless range.
# 0.30 is the published price-efficiency chop bar (sideways_filter TRER, 2026-10-16).
# The other levels are round and fixed before this read. They are not fit.
RNG_MIN_BP = 20.0
ER_MAX = 0.30
RISE_OF_RANGE = 0.5
WR_SPAN_MIN = 15.0
WR_NET_OF_SPAN = 0.5
COST_BP = 4.0
STRESS_BP = 8.0
TOP_N = 40
MIN_PRICE = 20.0
SCORE_UNTIL = "2026-09-23"
GRID = list(range(10 * 60, 15 * 60 + 50 + 1, 5))  # 10:00 .. 15:50
BOOT = 1000
SEED = 1108
T_BAR = 1.96
CFG = {"ai_watch_range_shape_log": True, "ai_watch_wr_slope_log": True}


def code_id() -> str:
    src = (inspect.getsource(ew._range_shape_fields) + inspect.getsource(ew._wr_slope_fields)
           + inspect.getsource(sdc.line_window))
    return hashlib.sha256(src.encode()).hexdigest()[:16]


def _ts(day: str, minute: int) -> float:
    hour, mins = divmod(int(minute), 60)
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, minute=mins, tzinfo=ET).timestamp()


def iex_days() -> list[str]:
    if not os.path.isdir(WH):
        return []
    return sorted(x[:-4] for x in os.listdir(WH) if x.endswith(".npz"))


def load_iex(day: str) -> dict[str, list[tuple]]:
    """symbol -> [(minute, o, h, l, c, v), ...] sorted, RTH minutes only."""
    path = os.path.join(WH, f"{day}.npz")
    z = np.load(path, allow_pickle=False)
    syms, idx, mins = z["syms"], z["sym_idx"], z["minute"]
    order = np.lexsort((mins, idx))
    idx, mins = idx[order], mins[order]
    o, h, l, c, v = z["o"][order], z["h"][order], z["l"][order], z["c"][order], z["v"][order]
    bounds = np.searchsorted(idx, np.arange(len(syms) + 1))
    out: dict[str, list[tuple]] = {}
    for k, s in enumerate(syms):
        a, b = int(bounds[k]), int(bounds[k + 1])
        if b <= a:
            continue
        rows = []
        for i in range(a, b):
            m = int(mins[i])
            if 9 * 60 + 30 <= m < 16 * 60:
                rows.append((m, float(o[i]), float(h[i]), float(l[i]), float(c[i]), float(v[i])))
        if rows:
            out[str(s)] = rows
    return out


def dollar_volume(rows: list[tuple]) -> float:
    return sum(r[4] * r[5] for r in rows)


def first_open(rows: list[tuple]) -> float | None:
    for r in rows:
        if r[0] == 9 * 60 + 30:
            return r[1]
    return rows[0][1] if rows else None


def pick_names(today: dict[str, list[tuple]], prior: dict[str, list[tuple]]) -> list[str]:
    ranked = []
    for sym, rows in prior.items():
        op = first_open(today.get(sym) or [])
        if op is None or op < MIN_PRICE:
            continue
        ranked.append((dollar_volume(rows), sym))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [s for _, s in ranked[:TOP_N]]


def as_bars(day_rows: list[tuple[str, list[tuple]]]) -> list[tuple]:
    """(ts, open, high, low, close, vol) for the live functions."""
    bars = []
    for day, rows in day_rows:
        for m, o, h, l, c, v in rows:
            bars.append((_ts(day, m), o, h, l, c, v))
    return bars


def live_features(bars: list[tuple], price: float, now: float) -> dict | None:
    """Call the current desk functions. None when either reading is missing."""
    ob_observe.bars = lambda _s, rows=bars: rows
    rs = ew._range_shape_fields("X", price, now, CFG)
    wr = ew._wr_slope_fields("X", price, now, CFG)
    need = ("rs_rng15_bp", "rs_rise15_bp", "rs_er15")
    if not rs or not wr or any(rs.get(k) is None for k in need) or wr.get("wr_d10_slow") is None:
        return None
    closed = sdc.closed_rows([b[0] for b in bars], [b[2] for b in bars], [b[3] for b in bars],
                             [b[4] for b in bars], now)
    fw = sdc.line_window(closed, price, sdc.FAST_LENGTH, sdc.FAST_SPAN, 11)
    if not fw:
        return None
    pos = rs.get("rs_pos15")
    return {"rs_rng15_bp": float(rs["rs_rng15_bp"]), "rs_rise15_bp": float(rs["rs_rise15_bp"]),
            "rs_er15": float(rs["rs_er15"]), "rs_pos15": None if pos is None else float(pos),
            "wr_d10_slow": float(wr["wr_d10_slow"]),
            "wr_span": max(fw) - min(fw), "wr_net": abs(fw[-1] - fw[0])}


def is_sideways(feat: dict) -> bool:
    """Price, %R, and the slow slope all say the series is traveling between a high and a low."""
    rng = feat["rs_rng15_bp"]
    price = rng >= RNG_MIN_BP and feat["rs_er15"] <= ER_MAX and abs(feat["rs_rise15_bp"]) <= RISE_OF_RANGE * rng
    span = feat["wr_span"]
    wr = span >= WR_SPAN_MIN and feat["wr_net"] <= WR_NET_OF_SPAN * span
    slope = feat["wr_d10_slow"] <= 0.0
    return bool(price and wr and slope)


def _by_minute(rows: list[tuple]) -> dict[int, tuple]:
    return {r[0]: r for r in rows}


def scan_day(day: str, names: list[str], history: dict[str, list[tuple]], today: dict[str, list[tuple]],
             score: bool) -> list[dict]:
    """One row per scored minute. gross and never-green are attached only in score mode."""
    rows_out = []
    for sym in names:
        seq = history.get(sym)
        if not seq:
            continue
        book = _by_minute(today.get(sym) or [])
        for minute in GRID:
            last = minute - 1
            bar = book.get(last)
            if bar is None or bar[4] <= 0:
                continue
            fwd = [book.get(minute + k) for k in range(0, 5)]
            if any(b is None for b in fwd):
                continue
            feat = live_features(seq, bar[4], _ts(day, minute))
            if feat is None:
                continue
            row = {"d": day, "sym": sym, "m": minute, "sideways": is_sideways(feat), **feat}
            if score:
                entry = bar[4]
                end = fwd[-1][4]
                row["gross"] = (end / entry - 1.0) * 1e4
                row["ng"] = max(b[2] for b in fwd) < entry * 1.0005
            rows_out.append(row)
    return rows_out


def build_history(days: list[str], cache: dict[str, dict]) -> None:
    for d in days:
        if d not in cache:
            cache[d] = load_iex(d)


def iter_windows(days: list[str]):
    """Yield (day, prev1, prev2) once two warmup sessions exist."""
    for i in range(2, len(days)):
        yield days[i], days[i - 1], days[i - 2]


def collect(days: list[str], score: bool, label: str) -> list[dict]:
    cache: dict[str, dict] = {}
    out: list[dict] = []
    windows = list(iter_windows(days))
    for n, (day, prev, prev2) in enumerate(windows, 1):
        build_history([prev2, prev, day], cache)
        names = pick_names(cache[day], cache[prev])
        hist = {s: as_bars([(prev2, cache[prev2].get(s, [])), (prev, cache[prev].get(s, [])),
                            (day, cache[day].get(s, []))]) for s in names}
        got = scan_day(day, names, hist, cache[day], score)
        out.extend(got)
        n_side = sum(1 for r in got if r.get("sideways"))
        print(f"{label} {n}/{len(windows)} {day} names {len(names)} rows {len(got)} sideways {n_side}",
              file=sys.stderr, flush=True)
        cache.pop(prev2, None)
    return out


def _day_t(diffs: list[float]) -> float:
    if len(diffs) < 2:
        return float("nan")
    mu = statistics.fmean(diffs)
    sd = statistics.stdev(diffs)
    if sd == 0:
        return float("inf") if mu > 0 else float("nan")
    return mu / (sd / math.sqrt(len(diffs)))


def _mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else float("nan")


def halves(days: list[str]) -> tuple[set[str], set[str]]:
    mid = len(days) // 2
    return set(days[:mid]), set(days[mid:])


def _daily(rows: list[dict], key: str) -> list[float]:
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["d"], []).append(r)
    diffs = []
    for group in by.values():
        fl = [r[key] for r in group if r["sideways"]]
        ot = [r[key] for r in group if not r["sideways"]]
        if len(fl) < 5 or len(ot) < 5:
            continue
        diffs.append(_mean(ot) - _mean(fl))
    return diffs


def _clip_top(rows: list[dict]) -> list[dict]:
    if not rows:
        return rows
    cap = sorted(r["gross"] for r in rows)[int(len(rows) * 0.99)]
    out = []
    for r in rows:
        c = dict(r)
        c["gross"] = min(r["gross"], cap)
        out.append(c)
    return out


def _boot(rows: list[dict], rng: random.Random) -> dict:
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["d"], []).append(r)
    names = list(by)
    pay, ctl = [], []
    for _ in range(BOOT):
        drawn = [r for d in (rng.choice(names) for _ in names) for r in by[d]]
        ot = [r for r in drawn if not r["sideways"]]
        if not ot:
            continue
        all_m = _mean([r["gross"] for r in drawn])
        pay.append(_mean([r["gross"] for r in ot]) - all_m)
        sk = set()
        for d in {r["d"] for r in drawn}:
            dr = [r for r in drawn if r["d"] == d]
            n_side = sum(1 for r in dr if r["sideways"])
            sk.update(id(r) for r in rng.sample(dr, min(len(dr), n_side)))
        kept = [r for r in drawn if id(r) not in sk]
        if kept:
            ctl.append(_mean([r["gross"] for r in kept]) - all_m)
    def q(v, p):
        if not v:
            return float("nan")
        return sorted(v)[min(len(v) - 1, int(len(v) * p))]
    return {"pay_p5": q(pay, 0.05), "pay_med": q(pay, 0.50), "rand_med": q(ctl, 0.50)}


def summarize(rows: list[dict], day_list: list[str]) -> dict:
    a_days, b_days = halves(day_list)
    def side(days, sideways):
        xs = [r for r in rows if r["d"] in days and r["sideways"] is sideways]
        return {"n": len(xs), "days": len({r["d"] for r in xs}),
                "gross": _mean([r["gross"] for r in xs]),
                "net4": _mean([r["gross"] - COST_BP for r in xs]),
                "net8": _mean([r["gross"] - STRESS_BP for r in xs]),
                "ng": _mean([1.0 if r["ng"] else 0.0 for r in xs])}
    def half_power(days):
        sub = [r for r in rows if r["d"] in days]
        contributing = 0
        by: dict[str, list[dict]] = {}
        for r in sub:
            by.setdefault(r["d"], []).append(r)
        for group in by.values():
            if sum(1 for r in group if r["sideways"]) >= 5 and sum(1 for r in group if not r["sideways"]) >= 5:
                contributing += 1
        return contributing, sum(1 for r in sub if r["sideways"])
    pa, fa = half_power(a_days)
    pb, fb = half_power(b_days)
    powered = pa >= 20 and pb >= 20 and fa >= 200 and fb >= 200
    diffs = {h: _day_t(_daily([r for r in rows if r["d"] in days], "gross"))
             for h, days in (("A", a_days), ("B", b_days))}
    wrows = _clip_top(rows)
    wdiffs = {h: _day_t(_daily([r for r in wrows if r["d"] in days], "gross"))
              for h, days in (("A", a_days), ("B", b_days))}
    separates = all(diffs[h] >= T_BAR and wdiffs[h] > 0 for h in ("A", "B"))
    nets = {h: side(days, False) for h, days in (("A", a_days), ("B", b_days))}
    pays_net = all(nets[h]["net4"] > 0 and nets[h]["net8"] > 0 for h in ("A", "B"))
    boot = _boot(rows, random.Random(SEED))
    pays_boot = boot["pay_p5"] > 0 and boot["pay_med"] > boot["rand_med"]
    if not powered:
        verdict = "UNDERPOWERED"
    elif separates and pays_net and pays_boot:
        verdict = "PROFITABLE"
    elif separates:
        verdict = "EDGE"
    else:
        verdict = "FAIL"
    return {
        "verdict": verdict,
        "powered": powered,
        "half_days": {"A": pa, "B": pb},
        "half_sideways": {"A": fa, "B": fb},
        "sideways": {"A": side(a_days, True), "B": side(b_days, True)},
        "not_sideways": nets,
        "all": {"A": _mean([r["gross"] for r in rows if r["d"] in a_days]),
                "B": _mean([r["gross"] for r in rows if r["d"] in b_days])},
        "day_t": diffs,
        "day_t_winsor": wdiffs,
        "bootstrap": boot,
        "n": len(rows),
        "n_sideways": sum(1 for r in rows if r["sideways"]),
    }


def canary() -> None:
    """Plant +8 bp on minutes that are not sideways and require SEPARATES. A zero plant must not pass."""
    days = [f"2026-01-{d:02d}" for d in range(1, 41)]
    rows = []
    rng = random.Random(1)
    for d in days:
        for k in range(30):
            sideways = k < 10
            gross = rng.uniform(-5, 5) + (0 if sideways else 8)
            rows.append({"d": d, "sideways": sideways, "gross": gross, "ng": gross < 5})
    good = summarize(rows, days)
    if good["verdict"] not in ("EDGE", "PROFITABLE") or good["day_t"]["A"] < T_BAR:
        raise SystemExit(f"canary failed to see a planted +8 bp: {good['verdict']} t={good['day_t']}")
    null_rows = []
    rng = random.Random(2)
    for d in days:
        for k in range(30):
            gross = rng.uniform(-5, 5)
            null_rows.append({"d": d, "sideways": k < 10, "gross": gross, "ng": False})
    bad = summarize(null_rows, days)
    if bad["verdict"] == "PROFITABLE":
        raise SystemExit("canary null plant scored PROFITABLE")
    print(f"canary ok planted {good['verdict']} t={good['day_t']['A']:.2f}/{good['day_t']['B']:.2f} "
          f"null {bad['verdict']}", file=sys.stderr)


def run_score(out_path: str) -> dict:
    canary()
    window = [d for d in iex_days() if d <= SCORE_UNTIL]
    if len(window) < 3:
        raise SystemExit("not enough IEX sessions to score")
    rows = collect(window, score=True, label="score")
    scored_days = [d for d, _, _ in iter_windows(window) if any(r["d"] == d for r in rows)]
    tape = summarize(rows, scored_days)
    desk = desk_info()
    payload = {"code": code_id(), "rule": {"rng_min_bp": RNG_MIN_BP, "er_max": ER_MAX,
               "rise_of_range": RISE_OF_RANGE, "wr_span_min": WR_SPAN_MIN,
               "wr_net_of_span": WR_NET_OF_SPAN, "slow_slope": "wr_d10_slow <= 0"},
               "tape": tape, "desk": desk, "score_days": scored_days}
    with open(out_path, "w") as f:
        json.dump(payload, f)
    print(json.dumps({"verdict": tape["verdict"], "day_t": tape["day_t"], "n": tape["n"],
                      "n_sideways": tape["n_sideways"], "not_sideways": tape["not_sideways"],
                      "sideways": tape["sideways"],
                      "all": tape["all"], "bootstrap": tape["bootstrap"], "desk": desk}, indent=1, default=str))
    return payload


def desk_info() -> dict:
    """November watch entries. Information only: five sessions cannot pass."""
    rows = []
    for day in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"):
        path = os.path.join(ROOT, "ai_reports", "tapes", day, "outcomes.jsonl")
        cache_path = os.path.join(CACHE, f"bars_v2_{day}.pkl")
        if not (os.path.exists(path) and os.path.exists(cache_path)):
            continue
        bars = pickle.load(open(cache_path, "rb"))
        seen = set()
        for line in open(path):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
            if o.get("entry_path") != "watch" or not (isinstance(e, (int, float)) and x and t) or e < 20:
                continue
            key = (o.get("symbol"), round(float(t)))
            if key in seen:
                continue
            seen.add(key)
            iex = (bars.get(o["symbol"]) or {}).get("iex") or []
            if len(iex) < 140:
                continue
            feat = live_features(list(iex), float(e), float(t))
            if feat is None:
                continue
            post = [r for r in iex if float(t) < r[0] <= float(t) + 300]
            if len(post) < 3:
                continue
            rows.append({"d": day, "sideways": is_sideways(feat),
                         "gross": (float(x) - float(e)) / float(e) * 1e4,
                         "ng": max(r[2] for r in post) < float(e) * 1.0005})
    if not rows:
        return {"verdict": "INFORMATION", "n": 0}
    side = [r for r in rows if r["sideways"]]
    rest = [r for r in rows if not r["sideways"]]
    return {"verdict": "INFORMATION", "n": len(rows), "n_sideways": len(side),
            "sideways_gross": _mean([r["gross"] for r in side]),
            "rest_gross": _mean([r["gross"] for r in rest]),
            "sideways_ng": _mean([1.0 if r["ng"] else 0.0 for r in side]),
            "rest_ng": _mean([1.0 if r["ng"] else 0.0 for r in rest]),
            "days": sorted({r["d"] for r in rows})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--out", default="/tmp/flat_conjunction_score.json")
    args = ap.parse_args(argv)
    print(f"code {code_id()}", file=sys.stderr)
    if args.score:
        run_score(args.out)
        return 0
    ap.error("pass --score")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
