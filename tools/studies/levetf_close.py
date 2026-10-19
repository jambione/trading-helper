#!/usr/bin/env python3
"""Leveraged-ETF close flow. Spec: docs/studies/levetf_close_prereg.json.

On a big day, does the index or sector ETF keep moving from 15:30 to 15:49 in the
direction of the day? XLP and XLU are the placebo: little leveraged money, same rule.

  TH_DATA_SECRETS=$PWD/secrets_data.json .venv/bin/python tools/studies/levetf_close.py --fetch    # outside RTH
  TH_DATA_SECRETS=$PWD/secrets_data.json .venv/bin/python tools/studies/levetf_close.py --spreads
  .venv/bin/python tools/studies/levetf_close.py --score     # after a result-skeptic READY
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import os
import pickle
import random
import statistics as st
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.environ.get("REPO") or os.getcwd()
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)

ET = ZoneInfo("America/New_York")
LEV = ("SPY", "QQQ", "IWM", "SMH", "XLF", "XBI", "XLE", "DIA")
PLACEBO = ("XLP", "XLU")
SYMS = LEV + PLACEBO
FETCH_FROM, FETCH_TO = "2015-10", "2026-10"
LAST_DAY = "2026-10-16"
FETCH_END = datetime(2026, 10, 17, tzinfo=ET)
A_FROM, A_TO = "2016-03-01", "2020-12-31"
B_FROM, B_TO = "2021-01-04", LAST_DAY
R2_FROM, R2_TO = "2025-09-29", "2026-09-25"
CRASH_FROM, CRASH_TO = "2020-02-01", "2020-04-30"
WARM, PCT = 60, 0.80
SIG_MIN, ENTRY_MIN, EXIT_MIN, INFO_EXIT_MIN = 15 * 60 + 29, 15 * 60 + 30, 15 * 60 + 49, 15 * 60 + 59
SLIP_BP, FLAT_BP = 1.0, 5.0
T_PASS, T_PROMISING, MIN_BIG_SESSIONS = 2.0, 1.5, 150
DROP_TOP, BOOT_N = 10, 2000
OUT = os.path.join(ROOT, "ai_reports", "levetf_bars")
SPR = os.path.join(ROOT, "ai_reports", "levetf_spreads.json")
RESULT = os.path.join(ROOT, "ai_reports", "levetf_close_result.md")
MANIFEST = os.path.join(OUT, "manifest.json")
FETCH_SPAN_S = 86400
# NYSE 13:00 closes, 2015-10..2026-10. The 15:29-15:49 bars on these days are after-hours prints.
EARLY_CLOSE = frozenset({
    "2015-11-27", "2015-12-24", "2016-11-25", "2017-07-03", "2017-11-24", "2018-07-03", "2018-11-23",
    "2018-12-24", "2019-07-03", "2019-11-29", "2019-12-24", "2020-11-27", "2020-12-24", "2021-11-26",
    "2022-11-25", "2023-07-03", "2023-11-24", "2024-07-03", "2024-11-29", "2024-12-24", "2025-07-03",
    "2025-11-28", "2025-12-24"})


# ── pure parts (tested) ──────────────────────────────────────────────────────

def big_flags(abs_r: list[float], warm: int = WARM, pct: float = PCT) -> list[bool | None]:
    """For each session i: None during warm-up, else |r_i| >= the pct quantile of the previous `warm` |r|.
    Only earlier sessions are read."""
    out: list[bool | None] = []
    for i, x in enumerate(abs_r):
        if i < warm:
            out.append(None)
            continue
        past = sorted(abs_r[i - warm:i])
        out.append(x >= _quantile(past, pct))
    return out


def _quantile(sorted_xs: list[float], q: float) -> float:
    """Linear interpolation, the same as numpy's default."""
    pos = (len(sorted_xs) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def trade_bp(bars: dict, side: int, exit_min: int = EXIT_MIN) -> float | None:
    """side x (close of exit_min / open of the 15:30 bar - 1), in bp. bars: {minute: (o, h, l, c)}."""
    a, b = bars.get(ENTRY_MIN), bars.get(exit_min)
    if a is None or b is None or not a[0] or a[0] <= 0:
        return None
    return side * (b[3] / a[0] - 1.0) * 1e4


def day_t(vals: list[float]) -> tuple[float | None, float | None, int]:
    if not vals:
        return None, None, 0
    mu = st.fmean(vals)
    if len(vals) < 3 or st.stdev(vals) == 0:
        return mu, None, len(vals)
    return mu, mu / (st.stdev(vals) / math.sqrt(len(vals))), len(vals)


def half_of(day: str) -> str | None:
    if A_FROM <= day <= A_TO:
        return "A"
    if B_FROM <= day <= B_TO:
        return "R2" if R2_FROM <= day <= R2_TO else "B"
    return None


def session_means(rows: list[dict], key: str = "net") -> dict[str, float]:
    by = defaultdict(list)
    for r in rows:
        by[r["day"]].append(r[key])
    return {d: st.fmean(v) for d, v in by.items()}


def week_boot(day_vals: dict[str, float], n: int = BOOT_N, seed: str = "levetf") -> tuple[float, float] | None:
    """95% interval of the session-mean net, resampling whole ISO weeks with replacement (big days cluster)."""
    weeks = defaultdict(list)
    for d, v in day_vals.items():
        y, w, _ = date.fromisoformat(d).isocalendar()
        weeks[(y, w)].append(v)
    keys = sorted(weeks)
    if len(keys) < 10:
        return None
    rng = random.Random(int.from_bytes(hashlib.sha256(seed.encode()).digest()[:8], "big"))
    means = []
    for _ in range(n):
        pick = [weeks[keys[rng.randrange(len(keys))]] for _ in keys]
        flat = [x for wk in pick for x in wk]
        means.append(st.fmean(flat))
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def without_top(day_vals: dict[str, float], k: int = DROP_TOP) -> float | None:
    keep = sorted(day_vals.values())[:-k] if len(day_vals) > k else []
    return st.fmean(keep) if keep else None


def word(h: dict) -> str:
    """h: per-half stats {"A": {...}, "B": {...}} with net_t, net, excess, lev_minus_placebo, big_sessions,
    boot_lo (B only), net_wo_top, net_wo_crash, comb_t."""
    a, b = h["A"], h["B"]
    if a["big_sessions"] < MIN_BIG_SESSIONS or b["big_sessions"] < MIN_BIG_SESSIONS:
        return "UNDERPOWERED"

    def pos(x):
        return x is not None and x > 0

    conc = all(pos(x[k]) for x in (a, b) for k in ("net_wo_top", "net_wo_crash"))
    excess = pos(a["excess"]) and pos(b["excess"])
    core = (pos(a["net"]) and pos(b["net"]) and a["net_t"] is not None and b["net_t"] is not None
            and a["net_t"] >= T_PASS and b["net_t"] >= T_PASS and excess and conc
            and b["boot_lo"] is not None and b["boot_lo"] > 0)
    if core:
        flow = (pos(a["lev_minus_placebo"]) and pos(b["lev_minus_placebo"])
                and h.get("lmp_t") is not None and h["lmp_t"] >= T_PASS)
        return "PASS" if flow else "MOMENTUM ONLY"
    if pos(a["net"]) and pos(b["net"]) and h["comb_t"] is not None and h["comb_t"] >= T_PROMISING and excess and conc:
        return "PROMISING"
    return "FAIL"


# ── data ─────────────────────────────────────────────────────────────────────

def _months():
    y, m = map(int, FETCH_FROM.split("-"))
    while f"{y:04d}-{m:02d}" <= FETCH_TO:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _data_client():
    """The data-only account, never the desk's key: bars.client() falls back to config/secrets.json without this."""
    path = os.environ.get("TH_DATA_SECRETS", "")
    if not path or not os.path.exists(path):
        sys.exit("REFUSING: set TH_DATA_SECRETS to the data-only account's secrets_data.json")
    import bars as B
    cl = B.client()
    if cl is None:
        sys.exit("REFUSING: the data-only client did not connect")
    return cl


def _manifest() -> dict:
    return json.load(open(MANIFEST)) if os.path.exists(MANIFEST) else {}


def _stamp(name: str) -> None:
    m = _manifest()
    m[name] = time.time()
    json.dump(m, open(MANIFEST, "w"), indent=1)


def fetch_problems() -> list[str]:
    """Every month file present, daily fetched after the last minute month, all within one day (Adjustment.ALL
    back-adjusts relative to the fetch date, so files fetched across an ex-dividend date disagree)."""
    out = []
    names = [f"{y:04d}-{m:02d}.pkl" for y, m in _months()]
    missing = [n for n in names if not os.path.exists(os.path.join(OUT, n))]
    if missing:
        out.append(f"{len(missing)} month files missing, first {missing[0]}")
    man = _manifest()
    if "daily.pkl" not in man or not os.path.exists(os.path.join(OUT, "daily.pkl")):
        out.append("daily.pkl missing")
    stamps = [man[n] for n in names if n in man]
    if len(stamps) < len(names) - len(missing):
        out.append("a month file has no fetch time in the manifest")
    if stamps and "daily.pkl" in man:
        if man["daily.pkl"] < max(stamps):
            out.append("daily.pkl was fetched before the last minute month: rerun --fetch")
        if max(stamps + [man["daily.pkl"]]) - min(stamps) > FETCH_SPAN_S:
            out.append("the files were fetched more than a day apart: delete ai_reports/levetf_bars and refetch")
    return out


def fetch() -> None:
    import edge_fetch as ef
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl = _data_client()
    os.makedirs(OUT, exist_ok=True)
    for y, m in _months():
        p = os.path.join(OUT, f"{y:04d}-{m:02d}.pkl")
        if os.path.exists(p):
            continue
        s = datetime(y, m, 1, tzinfo=ET)
        # The free plan refuses SIP inside the last 15 minutes, so the last month stops the day after LAST_DAY.
        e = min(datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=ET), FETCH_END)
        ef.throttle()
        try:
            bs = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=list(SYMS), timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                adjustment=Adjustment.ALL, feed=DataFeed.SIP,
                start=s.astimezone(timezone.utc), end=e.astimezone(timezone.utc)))
        except Exception as ex:  # noqa: BLE001 - a month that fails is retried on the next run
            print(f"{y}-{m:02d}: {ex}"[:160], flush=True)
            continue
        out: dict = defaultdict(dict)
        for sym, rs in (bs.data or {}).items():
            for r in rs:
                dt = r.timestamp.astimezone(ET)
                mm = dt.hour * 60 + dt.minute
                if 15 * 60 + 25 <= mm < 16 * 60:
                    out[dt.strftime("%Y-%m-%d")].setdefault(sym, {})[mm] = (
                        float(r.open), float(r.high), float(r.low), float(r.close))
        pickle.dump(dict(out), open(p, "wb"))
        _stamp(os.path.basename(p))
        print(f"{y}-{m:02d}: {len(out)} days", flush=True)
    missing = [f"{y:04d}-{m:02d}" for y, m in _months() if not os.path.exists(os.path.join(OUT, f"{y:04d}-{m:02d}.pkl"))]
    if missing:
        print(f"{len(missing)} months failed ({missing[0]}...): rerun --fetch; daily is fetched after the last month")
        return
    # Daily last, so its adjustment matches the minute files.
    ef.throttle()
    bs = cl.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=list(SYMS), timeframe=TimeFrame.Day, adjustment=Adjustment.ALL, feed=DataFeed.SIP,
        start=datetime(2015, 6, 1, tzinfo=timezone.utc), end=FETCH_END.astimezone(timezone.utc)))
    daily = {s: {r.timestamp.astimezone(ET).strftime("%Y-%m-%d"): float(r.close) for r in rs}
             for s, rs in (bs.data or {}).items()}
    pickle.dump(daily, open(os.path.join(OUT, "daily.pkl"), "wb"))
    _stamp("daily.pkl")
    print(f"daily: {', '.join(f'{s} {len(v)}' for s, v in sorted(daily.items()))}", flush=True)
    for p in fetch_problems():
        print(f"PROBLEM: {p}")


def spreads() -> None:
    import edge_fetch as ef
    cl = _data_client()
    store = json.load(open(SPR)) if os.path.exists(SPR) else {}
    bym = defaultdict(list)
    for d in sorted(load_minutes()):
        if d not in EARLY_CLOSE:
            bym[d[:7]].append(d)
    for mo, ds in sorted(bym.items()):
        for d in (ds[0], ds[len(ds) // 2]):
            t = datetime.strptime(d, "%Y-%m-%d").replace(hour=15, minute=31, tzinfo=ET).timestamp()
            ef.fetch_spreads(cl, t, list(SYMS), 5, store)
        json.dump(store, open(SPR, "w"))
        print(f"spreads {mo}", flush=True)


def load_minutes() -> dict:
    D = {}
    for y, m in _months():
        p = os.path.join(OUT, f"{y:04d}-{m:02d}.pkl")
        if os.path.exists(p):
            D.update(pickle.load(open(p, "rb")))
    return D


def month_spread(store: dict) -> dict[tuple[str, str], float]:
    """(sym, YYYY-MM) -> median of that month's sampled SIP spreads, bp. A month with no sample is absent."""
    by = defaultdict(list)
    for k, v in store.items():
        sym, t = k.split("|")
        if v is not None:
            by[(sym, datetime.fromtimestamp(int(t), ET).strftime("%Y-%m"))].append(float(v))
    return {k: st.median(v) for k, v in by.items()}


# ── scoring ──────────────────────────────────────────────────────────────────

def build_rows(minutes: dict, daily: dict, spr: dict) -> tuple[list[dict], dict]:
    cnt = {"dropped_bars": 0, "missing_spread_months": set(), "warm": 0, "early_close": 0}
    rows = []
    for sym in SYMS:
        dcl = daily.get(sym) or {}
        dkeys = sorted(dcl)
        days = sorted(d for d in minutes if sym in minutes[d] and d <= LAST_DAY)
        cnt["early_close"] += sum(1 for d in days if d in EARLY_CLOSE)
        days = [d for d in days if d not in EARLY_CLOSE]
        seq = []
        for d in days:
            k = bisect.bisect_left(dkeys, d)
            bars = minutes[d][sym]
            sig = bars.get(SIG_MIN)
            if k == 0 or sig is None:
                cnt["dropped_bars"] += 1
                continue
            pc = dcl[dkeys[k - 1]]
            if pc <= 0:
                cnt["dropped_bars"] += 1
                continue
            seq.append((d, sig[3] / pc - 1.0, bars))
        flags = big_flags([abs(r) for _, r, _ in seq])
        for (d, r, bars), big in zip(seq, flags):
            if big is None:
                cnt["warm"] += 1
                continue
            side = 1 if r > 0 else -1
            g = trade_bp(bars, side)
            if g is None:
                cnt["dropped_bars"] += 1
                continue
            sp = spr.get((sym, d[:7]))
            if sp is None:
                cnt["missing_spread_months"].add((sym, d[:7]))
                cost = FLAT_BP
            else:
                cost = sp + SLIP_BP
            rows.append({"day": d, "sym": sym, "r": r, "big": big, "side": side, "gross": g, "net": g - cost,
                         "net5": g - FLAT_BP, "long": trade_bp(bars, 1), "info59": trade_bp(bars, side, INFO_EXIT_MIN),
                         "placebo": sym in PLACEBO, "half": half_of(d)})
    cnt["missing_spread_months"] = len(cnt["missing_spread_months"])
    return rows, cnt


def _excess_rows(rows: list[dict]) -> list[dict]:
    """Each big-day trade minus the mean of the same instrument's non-big trades in the same half.
    Per instrument, so a set with more instruments is not diluted differently from one with fewer."""
    base = defaultdict(list)
    for r in rows:
        if not r["big"]:
            base[(r["sym"], r["half"])].append(r["net"])
    mean = {k: st.fmean(v) for k, v in base.items() if v}
    out = []
    for r in rows:
        if r["big"] and (r["sym"], r["half"]) in mean:
            out.append({**r, "ex": r["net"] - mean[(r["sym"], r["half"])]})
    return out


def half_stats(rows: list[dict], half: str) -> dict:
    lev = [r for r in rows if not r["placebo"] and r["half"] == half]
    plc = [r for r in rows if r["placebo"] and r["half"] == half]
    big = [r for r in lev if r["big"]]
    big_day = session_means(big)
    mu, t, n = day_t(list(big_day.values()))
    lev_ex, plc_ex = session_means(_excess_rows(lev), "ex"), session_means(_excess_rows(plc), "ex")
    ex_mu, ex_t, _ = day_t(list(lev_ex.values()))
    pex_mu, _, _ = day_t(list(plc_ex.values()))
    # Flow vs placebo, per session where both sets had a big-day trade.
    lmp = {d: lev_ex[d] - plc_ex[d] for d in lev_ex if d in plc_ex}
    lmp_mu, lmp_t, lmp_n = day_t(list(lmp.values()))
    boot = week_boot(big_day) if half == "B" else None
    no_crash = {d: v for d, v in big_day.items() if not (CRASH_FROM <= d <= CRASH_TO)}
    return {"big_sessions": n, "trades": len(big), "net": mu, "net_t": t,
            "gross": day_t(list(session_means(big, "gross").values()))[0],
            "net5": day_t(list(session_means(big, "net5").values()))[0],
            "win": (sum(1 for r in big if r["net"] > 0) / len(big)) if big else None,
            "all_day": day_t(list(session_means(lev).values()))[0], "excess": ex_mu, "excess_t": ex_t,
            "placebo_excess": pex_mu,
            "lev_minus_placebo": lmp_mu, "lev_minus_placebo_t": lmp_t, "lev_minus_placebo_n": lmp_n, "lmp": lmp,
            "drift": day_t(list(session_means([r for r in big if r["long"] is not None], "long").values()))[0],
            "info59": day_t(list(session_means([r for r in big if r["info59"] is not None], "info59").values()))[0],
            "boot_lo": boot[0] if boot else None, "boot_hi": boot[1] if boot else None,
            "net_wo_top": without_top(big_day), "net_wo_crash": st.fmean(no_crash.values()) if no_crash else None,
            "big_day": big_day}


def _pct(x):
    return "-" if x is None else f"{x:.0%}"


def _f(x, nd=1):
    return "-" if x is None else f"{x:+.{nd}f}"


def report(rows: list[dict], cnt: dict) -> str:
    h = {k: half_stats(rows, k) for k in ("A", "B", "R2")}
    comb = list(h["A"]["big_day"].values()) + list(h["B"]["big_day"].values())
    h["comb_t"] = day_t(comb)[1]
    h["lmp_t"] = day_t(list(h["A"]["lmp"].values()) + list(h["B"]["lmp"].values()))[1]
    w = word(h)
    lines = ["# Leveraged-ETF close flow (levetf_close_prereg)", "",
             f"**{w}**", "",
             "Trade 15:30 open -> 15:49 close in the direction of the day's move to 15:29, on big days only.",
             "Net = gross - (that symbol-month's SIP spread + 1 bp). Session-clustered. R2 is the round-2 window, never in the call.",
             "excess: each big-day trade minus the same ETF's non-big trades in that half. lev - placebo: per session, lev excess minus placebo excess.",
             "", "| half | big sessions | trades | gross | net | t | net 5 bp | win | all-day | excess (t) | placebo excess | lev - placebo | long drift | to 15:59 | boot 95% |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for k in ("A", "B", "R2"):
        s = h[k]
        boot = "-" if s["boot_lo"] is None else f"[{s['boot_lo']:+.1f}, {s['boot_hi']:+.1f}]"
        lines.append(f"| {k} | {s['big_sessions']} | {s['trades']} | {_f(s['gross'])} | {_f(s['net'])} | {_f(s['net_t'], 2)} | "
                     f"{_f(s['net5'])} | {_pct(s['win'])} | {_f(s['all_day'])} | "
                     f"{_f(s['excess'])} ({_f(s['excess_t'], 2)}) | {_f(s['placebo_excess'])} | {_f(s['lev_minus_placebo'])} | "
                     f"{_f(s['drift'])} | {_f(s['info59'])} | {boot} |")
    lines += ["", f"combined t (A + B) {_f(h['comb_t'], 2)}; lev - placebo combined t {_f(h['lmp_t'], 2)} "
              f"(sessions A {h['A']['lev_minus_placebo_n']}, B {h['B']['lev_minus_placebo_n']})",
              f"without the {DROP_TOP} best sessions: A {_f(h['A']['net_wo_top'])} B {_f(h['B']['net_wo_top'])}; "
              f"without 2020-02..04: A {_f(h['A']['net_wo_crash'])} B {_f(h['B']['net_wo_crash'])}", ""]
    for title, key in (("By instrument", "sym"), ("By year", "year"), ("Up vs down days", "dir")):
        lines += [f"## {title} (big days, net, session-clustered within the group)", "", "| group | half | n | net | t |", "|---|---|---:|---:|---:|"]
        groups = defaultdict(list)
        for r in rows:
            if not r["big"] or r["half"] not in ("A", "B", "R2"):
                continue
            g = r["sym"] if key == "sym" else r["day"][:4] if key == "year" else ("up" if r["side"] > 0 else "down")
            groups[(g, r["half"])].append(r)
        for (g, hf), rr in sorted(groups.items()):
            mu, t, n = day_t(list(session_means(rr).values()))
            lines.append(f"| {g} | {hf} | {n} | {_f(mu)} | {_f(t, 2)} |")
        lines.append("")
    lines += [f"early-close sessions dropped (symbol-sessions): {cnt['early_close']}",
              f"dropped for a missing bar or prior close: {cnt['dropped_bars']}; warm-up sessions: {cnt['warm']}; "
              f"symbol-months with no spread sample (charged {FLAT_BP:.0f} bp): {cnt['missing_spread_months']}",
              "Flow size: historical shares outstanding are not available from a free source here. Not printed.", ""]
    return "\n".join(lines)


def coverage(minutes: dict, daily: dict) -> str:
    """Sessions per symbol per year with a 15:29 bar, against SPY's daily calendar."""
    cal = defaultdict(int)
    for d in (daily.get("SPY") or {}):
        if d <= LAST_DAY and d not in EARLY_CLOSE:
            cal[d[:4]] += 1
    have = defaultdict(int)
    for d, by in minutes.items():
        if d in EARLY_CLOSE or d > LAST_DAY:
            continue
        for s, bars in by.items():
            if SIG_MIN in bars:
                have[(s, d[:4])] += 1
    years = sorted(cal)
    lines = ["sessions with a 15:29 bar, by year (SPY calendar first)", "sym  " + " ".join(f"{y:>5}" for y in years),
             "cal  " + " ".join(f"{cal[y]:>5}" for y in years)]
    for s in SYMS:
        lines.append(f"{s:<4} " + " ".join(f"{have[(s, y)]:>5}" for y in years))
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--spreads", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.fetch:
        fetch()
        return
    if a.spreads:
        spreads()
        return
    minutes = load_minutes()
    daily_p = os.path.join(OUT, "daily.pkl")
    days = sorted(minutes)
    print(f"minute sessions {len(days)} {days[0] if days else '-'}..{days[-1] if days else '-'}; "
          f"daily file {'yes' if os.path.exists(daily_p) else 'MISSING'}; spreads file {'yes' if os.path.exists(SPR) else 'MISSING'}", flush=True)
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    problems = fetch_problems()
    if not os.path.exists(SPR):
        problems.append("spreads file missing: run --spreads")
    if days and days[-1] < LAST_DAY:
        problems.append(f"last minute session {days[-1]} is before {LAST_DAY}")
    if problems:
        print("REFUSING: " + "; ".join(problems))
        return
    daily = pickle.load(open(daily_p, "rb"))
    print(coverage(minutes, daily), flush=True)
    spr = month_spread(json.load(open(SPR)))
    rows, cnt = build_rows(minutes, daily, spr)
    md = report(rows, cnt)
    print(md, flush=True)
    with open(RESULT, "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
