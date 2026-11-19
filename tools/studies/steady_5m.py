#!/usr/bin/env python3
"""steady_5m - a steadier entry than the 1-minute box after the opening thrust: 5-minute signals with 5-minute exits
(operator 11/19). Prereg: docs/studies/steady_5m_prereg.json.

    .venv/bin/python tools/studies/steady_5m.py events
    .venv/bin/python tools/studies/quotes_parallel.py steady_5m all 4      # after 16:05 only
    .venv/bin/python tools/studies/steady_5m.py score
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

from opening_square import summ  # noqa: E402

OUT = ROOT / "ai_reports" / "steady_5m"
DECIDE = ("2026-04-28", "2026-09-23")
CONFIRM = ("2026-09-24", "2026-11-18")
W_LO, W_HI = 59, 329          # 1-minute index of the last minute of 5-minute bars ending 10:30 .. 15:00
STOP, T_OUT, GATE_BP, CAP, EDGE = 0.03, 385, 15.0, 4000, 5.0
SIGNALS = ("BOX5", "TREND5", "LOWHEAT5")


def bars5(o, h, l, c, v):
    """1-minute arrays (390, NaN where no bar) -> 5-minute o/h/l/c/v (78) aligned to 09:30. Pure."""
    def agg(a, f):
        x = a.reshape(78, 5)
        with np.errstate(all="ignore"):
            return f(x)
    first = lambda x: np.array([r[np.isfinite(r)][0] if np.isfinite(r).any() else np.nan for r in x])  # noqa: E731
    last = lambda x: np.array([r[np.isfinite(r)][-1] if np.isfinite(r).any() else np.nan for r in x])  # noqa: E731
    return (agg(o, first), agg(h, lambda x: np.nanmax(x, axis=1)), agg(l, lambda x: np.nanmin(x, axis=1)),
            agg(c, last), agg(np.where(np.isfinite(v), v, 0.0), lambda x: x.sum(axis=1)))


def ffill(a):
    a = a.copy()
    for i in range(1, len(a)):
        if not np.isfinite(a[i]):
            a[i] = a[i - 1]
    return a


def lines5(ph, pl, pc, h5, l5, c5):
    """Prior-day + today 5-minute bars -> today's fast (21, EWM 7) and slow (28, EWM 3) %R. Pure."""
    from bar_signal_family import wr_smoothed
    hh, ll, cc = (ffill(np.concatenate([a, b])) for a, b in ((ph, h5), (pl, l5), (pc, c5)))
    hh = np.where(np.isfinite(hh), hh, cc)
    ll = np.where(np.isfinite(ll), ll, cc)
    n0 = len(pc)
    return wr_smoothed(hh, ll, cc, n=21, span=7)[n0:], wr_smoothed(hh, ll, cc, n=28, span=3)[n0:]


def box5(fast, slow):
    """Bar indexes (0..77) where the 5-minute pre-square is true on this bar and the previous, not the one before. Pure."""
    fp = np.concatenate(([np.nan], fast[:-1]))
    with np.errstate(invalid="ignore"):
        ok = (np.isfinite(fast) & np.isfinite(slow) & np.isfinite(fp) & (fast >= -35) & (slow >= -35)
              & (np.abs(fast - slow) <= 15) & ~((fast >= -20) & (slow >= -20)) & (fast - fp > 0.05))
    return [i for i in range(2, len(ok)) if ok[i] and ok[i - 1] and not ok[i - 2]]


def lowheat5(fast):
    heat = 100.0 + fast
    hp = np.concatenate(([np.nan], heat[:-1]))
    with np.errstate(invalid="ignore"):
        up = np.isfinite(heat) & np.isfinite(hp) & (heat - hp > 0.05)
    return [i for i in range(2, len(heat)) if heat[i] < 40 and up[i] and up[i - 1] and not up[i - 2]]


def trend5(c5, l5, vwap5, prev_c5):
    """First bar where close > rising EMA20 and > VWAP and the last 6 bars' low > the 6 before. Pure."""
    cc = ffill(np.concatenate([prev_c5, c5]))
    ema = np.full(len(cc), np.nan)
    k = 2 / 21
    for i, x in enumerate(cc):
        if np.isfinite(x):
            ema[i] = x if i == 0 or not np.isfinite(ema[i - 1]) else ema[i - 1] + k * (x - ema[i - 1])
    n0 = len(prev_c5)
    e = ema[n0:]
    out, prev_ok = [], False
    for i in range(len(c5)):
        ok = False
        if i >= 12 and np.isfinite(c5[i]) and np.isfinite(e[i]) and np.isfinite(e[i - 3]) and np.isfinite(vwap5[i]):
            lo_a, lo_b = np.nanmin(l5[i - 5:i + 1]), np.nanmin(l5[i - 11:i - 5])
            ok = bool(c5[i] > e[i] and e[i] > e[i - 3] and c5[i] > vwap5[i] and lo_a > lo_b)
        if ok and not prev_ok:
            out.append(i)
        prev_ok = ok
    return out


def vwap_at_bar_end(h, l, c, v):
    """Session VWAP at the end of each 5-minute bar from 1-minute typical price x volume. Pure."""
    tp = (h + l + c) / 3.0
    w = np.where(np.isfinite(tp) & np.isfinite(v), tp * v, 0.0)
    vv = np.where(np.isfinite(tp) & np.isfinite(v), v, 0.0)
    cw, cv = np.cumsum(w), np.cumsum(vv)
    ends = np.arange(4, 390, 5)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(cv[ends] > 0, cw[ends] / cv[ends], np.nan)


def exit5(o, l, c, t, fast):
    """Entry open t+1; -3% stop; else close of the first completed 5-minute bar after entry where fast turns down having
    been >= -20 since entry; else 15:55. Returns (gross bp, exit minute) or None. Pure."""
    if t + 1 > T_OUT:
        return None
    e = o[t + 1]
    if not (np.isfinite(e) and e > 0):
        return None
    sp, seen_ob, last = e * (1 - STOP), False, e
    for m in range(t + 1, T_OUT + 1):
        if np.isfinite(l[m]) and l[m] <= sp:
            return float(((min(sp, o[m]) if np.isfinite(o[m]) else sp) / e - 1) * 1e4), m
        if np.isfinite(c[m]):
            last = c[m]
        if (m + 1) % 5 == 0:
            b = (m + 1) // 5 - 1
            if b >= 1 and np.isfinite(fast[b]):
                if seen_ob and np.isfinite(fast[b - 1]) and fast[b] < fast[b - 1]:
                    return float((last / e - 1) * 1e4), m
                if fast[b] >= -20:
                    seen_ob = True
    return float((last / e - 1) * 1e4), T_OUT


def refract(bars, minutes=30):
    out, last = [], -10 ** 9
    for b in bars:
        if b * 5 - last >= minutes:
            out.append(b)
            last = b * 5
    return out


def events():
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import ALL_ETF, WH_SIP, presquare_bars, minute_grid
    from holistic_entry import bracket, COST
    from ticker_filters import is_levered_etp
    import news_source as ns
    rows, prev = [], None
    for fp in sorted(WH_SIP.glob("20??-??-??.npz")):
        day = load_day(fp)
        if prev is None or not (DECIDE[0] <= fp.stem <= CONFIRM[1]) or day["idx"].get("SPY") is None:
            prev = day
            continue
        for s in universe(day, prev, set(ALL_ETF)):
            if is_levered_etp(s):
                continue
            i, j = day["idx"][s], prev["idx"].get(s)
            if j is None:
                continue
            pc = ns.last_close(prev["c"][j])
            if not pc > 0:
                continue
            o, h, l, c, v = (day[k][i] for k in "ohlcv")
            _, ph5, pl5, pc5, _ = bars5(*(prev[k][j] for k in "ohlcv"))
            o5, h5, l5, c5, v5 = bars5(o, h, l, c, v)
            fast, slow = lines5(ph5, pl5, pc5, h5, l5, c5)
            vw = vwap_at_bar_end(h, l, c, v)

            def ok(b):
                t = b * 5 + 4
                return W_LO <= t <= W_HI and np.isfinite(c5[b]) and 20 <= c5[b] <= 400 and (c5[b] / pc - 1) * 100 >= 1.5

            def add(arm, t, with_bracket=False):
                ex = exit5(o, l, c, t, fast)
                if ex is None:
                    return
                row = {"arm": arm, "day": fp.stem, "sym": s, "t": int(t), "gross": round(ex[0], 2), "exit_m": ex[1]}
                if with_bracket:
                    b_ = bracket(o, h, l, c, t)
                    row["bracket"] = None if b_ is None else round(float(b_) + COST, 2)
                rows.append(row)
            any_sig = False
            for arm, bars in (("BOX5", box5(fast, slow)), ("TREND5", trend5(c5, l5, vw, pc5)), ("LOWHEAT5", lowheat5(fast))):
                for b in refract([b for b in bars if ok(b)]):
                    add(arm, b * 5 + 4)
                    any_sig = True
            if any_sig:
                rb = (W_LO - 4) // 5 + int(hashlib.sha256(f"s5m_r|{fp.stem}|{s}".encode()).hexdigest(), 16) % ((W_HI - W_LO) // 5 + 1)
                if ok(rb):
                    add("RANDOM5", rb * 5 + 4)
            cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)
            lastm = -10 ** 9
            for t in presquare_bars(cc, hh, ll, off, 21):
                if W_LO <= t <= W_HI and t - lastm >= 30 and np.isfinite(c[t]) and 20 <= c[t] <= 400 and (c[t] / pc - 1) * 100 >= 1.5:
                    add("BOX1", t, with_bracket=True)
                    lastm = t
        print(fp.stem, len(rows), flush=True)
        prev = day
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "events.json").write_text(json.dumps(rows))


def sample(rows):
    by = defaultdict(list)
    for r in rows:
        by[(r["arm"], r["day"] <= DECIDE[1])].append(r)
    out = []
    for v in by.values():
        out += sorted(v, key=lambda r: hashlib.sha256(f"s5m|{r['day']}|{r['sym']}|{r['t']}".encode()).hexdigest())[:CAP]
    return out


def plan(which="all"):
    return sorted({("s5m", r["day"], r["sym"], r["t"], 0.0) for r in sample(json.loads((OUT / "events.json").read_text()))})


def verdict(d, cf):
    ok = (d.get("n", 0) >= 300 and (d.get("mean_net") or -99) >= EDGE and (d.get("t_day") or -99) >= 2.5
          and (d.get("half1") or -1) > 0 and (d.get("half2") or -1) > 0 and (cf.get("mean_net") or -1) > 0)
    return ["PASS" if ok else "FAIL", f"net {d.get('mean_net')} (need +{EDGE}) t {d.get('t_day')} (need 2.5) halves "
            f"{d.get('half1')}/{d.get('half2')} n {d.get('n')} confirm {cf.get('mean_net')}"]


def score():
    import news_source as ns
    q = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    traded, cnt, holds = defaultdict(list), defaultdict(lambda: {"noquote": 0, "gated": 0}), defaultdict(list)
    for r in sample(json.loads((OUT / "events.json").read_text())):
        per = "decide" if r["day"] <= DECIDE[1] else "confirm"
        sp = q.get(ns.qkey(r["day"], r["sym"], r["t"]))
        if sp is None:
            cnt[(r["arm"], per)]["noquote"] += 1
            continue
        if sp > GATE_BP:
            cnt[(r["arm"], per)]["gated"] += 1
            continue
        traded[(r["arm"], per)].append({**r, "net": r["gross"] - sp})
        holds[r["arm"]].append(r["exit_m"] - r["t"])
        if r["arm"] == "BOX1" and r.get("bracket") is not None:
            traded[("BOX1_bracket", per)].append({**r, "net": r["bracket"] - sp})
    out = {"|".join(k): summ(v) for k, v in traded.items()}
    out.update({"counts|" + "|".join(k): v for k, v in cnt.items()})
    out["median_hold_min"] = {k: float(np.median(v)) for k, v in holds.items() if v}
    days = len({r["day"] for v in traded.values() for r in v if r["day"] <= DECIDE[1]}) or 1
    out["opens_per_10m_decide"] = {k[0]: round(len(v) / days / 27.0, 3) for k, v in traded.items() if k[1] == "decide"}
    out["VERDICT"] = {s: verdict(out.get(f"{s}|decide", {}), out.get(f"{s}|confirm", {})) for s in SIGNALS}
    r5 = out.get("RANDOM5|decide", {})
    for s in SIGNALS:
        if r5.get("n") and out.get(f"{s}|decide", {}).get("n"):
            out["VERDICT"][s].append(f"lift vs random {round(out[f'{s}|decide']['mean_net'] - r5['mean_net'], 2)}")
    (OUT / "score.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out["VERDICT"], indent=1))
    return out


if __name__ == "__main__":
    {"events": events, "score": score}[sys.argv[1]]()
