#!/usr/bin/env python3
"""Intraday pairs reversion. Spec: docs/studies/pairs_revert_prereg.json.

When two related stocks drift 2 sigma apart inside a session, short the leader and buy the laggard;
exit when the gap closes, after 60 minutes, or at 15:50. beta and sigma are fit on half A, the call
is half B. Each pair must beat a placebo partner (an unrelated name, same rule).

  .venv/bin/python tools/studies/pairs_revert.py            # inventory and presence
  .venv/bin/python tools/studies/pairs_revert.py --score    # after a result-skeptic READY
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics as st
from collections import defaultdict

import numpy as np

ROOT = os.environ.get("REPO") or os.getcwd()
WH = os.path.join(ROOT, "ai_reports", "warehouse")
PREREG = os.path.join(ROOT, "docs", "studies", "pairs_revert_prereg.json")
RESULT = os.path.join(ROOT, "ai_reports", "warehouse", "pairs_revert_result.md")

FIRST_DAY, LAST_DAY, N_DAYS, CUT = "2026-04-28", "2026-10-15", 119, "2026-07-23"
ANCHOR = 9 * 60 + 45
ENTRY_FROM, ENTRY_TO = 10 * 60, 15 * 60
FLAT = 15 * 60 + 50
CORR_FROM, CORR_TO = ANCHOR, FLAT
ENTER_Z, REARM_Z, QUIET_Z = 2.0, 1.0, 0.5
HOLD = 60
FILL_WAIT = 3
COST_BP, HAIRCUT_BP = 3.0, 6.0
P2_N, P2_TOP, P2_CAP = 40, 300, 3
MIN_PRESENT = 100
T_PASS, T_PROMISING = 2.24, 1.5
MIN_SESSIONS, MIN_TRADES, MAX_DROP = 30, 300, 0.05
DROP_SESSIONS, DROP_PAIRS, DROP_NAMES = 3, 5, 5


def _seed(*parts) -> int:
    return int.from_bytes(hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()[:8], "big")


# ── pure parts (tested) ──────────────────────────────────────────────────────

def spread_at(a: dict, b: dict, beta: float, m: int) -> float | None:
    """d_m from the 09:45 close. Both legs must have printed in minute m and at 09:45. bars: {minute: (o, c)}."""
    a0, b0, am, bm = a.get(ANCHOR), b.get(ANCHOR), a.get(m), b.get(m)
    if a0 is None or b0 is None or am is None or bm is None:
        return None
    if min(a0[1], b0[1], am[1], bm[1]) <= 0:
        return None
    return math.log(am[1] / a0[1]) - beta * math.log(bm[1] / b0[1])


def _fill_open(bars: dict, m: int) -> float | None:
    for k in range(m, m + FILL_WAIT):
        if k in bars and bars[k][0] > 0:
            return bars[k][0]
    return None


def _fill_close(bars: dict, m: int) -> float | None:
    for k in range(m, m - FILL_WAIT, -1):
        if k in bars and bars[k][1] > 0:
            return bars[k][1]
    return None


def pair_return(side: int, beta: float, a_in: float, a_out: float, b_in: float, b_out: float) -> float:
    """bp of gross notional (1 + beta). side +1: A was ahead, so short A and buy beta of B."""
    ra, rb = a_out / a_in - 1.0, b_out / b_in - 1.0
    return (-side * ra + side * beta * rb) / (1.0 + beta) * 1e4


def _close_trade(a, b, beta, side, a_in, b_in, m_exit, reason, m_entry):
    if m_exit >= FLAT:
        a_out, b_out = _fill_close(a, FLAT), _fill_close(b, FLAT)
    else:
        a_out, b_out = _fill_open(a, m_exit), _fill_open(b, m_exit)
    if a_out is None or b_out is None:
        return {"dropped": True, "entry_m": m_entry}
    return {"dropped": False, "entry_m": m_entry, "exit_m": min(m_exit, FLAT), "reason": reason, "side": side,
            "gross": pair_return(side, beta, a_in, a_out, b_in, b_out)}


def simulate(a: dict, b: dict, beta: float, sigma: float) -> list[dict]:
    """One session of one pair. Signals read the close of minute m; fills are the open of m+1."""
    trades, open_t, armed = [], None, True
    for m in range(ENTRY_FROM, FLAT + 1):
        d = spread_at(a, b, beta, m)
        if open_t is not None:
            reason = None
            if m >= FLAT:
                reason = "15:50"
            elif d is not None and d * open_t["sign"] <= 0:
                reason = "cross"
            elif m >= open_t["entry_m"] + HOLD:
                reason = "60 min"
            if reason:
                t = _close_trade(a, b, beta, open_t["side"], open_t["a_in"], open_t["b_in"],
                                 FLAT if reason == "15:50" else m + 1, reason, open_t["entry_m"])
                trades.append(t)
                open_t = None
                armed = False
            continue
        if d is None:
            continue
        if not armed:
            armed = abs(d) < REARM_Z * sigma
            continue
        if ENTRY_FROM <= m <= ENTRY_TO and abs(d) >= ENTER_Z * sigma:
            side = 1 if d > 0 else -1
            a_in, b_in = _fill_open(a, m + 1), _fill_open(b, m + 1)
            if a_in is None or b_in is None:
                trades.append({"dropped": True, "entry_m": m})
                armed = False
                continue
            open_t = {"entry_m": m, "side": side, "sign": side, "a_in": a_in, "b_in": b_in}
    return trades


def quiet_control(a: dict, b: dict, beta: float, sigma: float, trade: dict, key: str) -> float | None:
    """Same pair, a seeded minute in the trade's clock hour with |d| < 0.5 sigma, same side, exit at 60 min or 15:50."""
    hour = trade["entry_m"] // 60
    ok = [m for m in range(max(hour * 60, ENTRY_FROM), min(hour * 60 + 60, ENTRY_TO + 1))
          if (d := spread_at(a, b, beta, m)) is not None and abs(d) < QUIET_Z * sigma]
    if not ok:
        return None
    m = ok[_seed(key, trade["entry_m"], "pairs-revert") % len(ok)]
    a_in, b_in = _fill_open(a, m + 1), _fill_open(b, m + 1)
    if a_in is None or b_in is None:
        return None
    t = _close_trade(a, b, beta, trade["side"], a_in, b_in, min(m + 1 + HOLD, FLAT), "ctrl", m)
    return None if t["dropped"] else t["gross"]


def fit_beta(xa: np.ndarray, xb: np.ndarray) -> float | None:
    ok = np.isfinite(xa) & np.isfinite(xb)
    if ok.sum() < 100:
        return None
    den = float((xb[ok] ** 2).sum())
    return None if den == 0 else float((xa[ok] * xb[ok]).sum() / den)


def pick_p2(names: list[str], corr: np.ndarray, n: int = P2_N, cap: int = P2_CAP) -> list[tuple[str, str, float]]:
    """Greedy by correlation, each name in at most `cap` pairs."""
    iu = np.triu_indices(len(names), 1)
    vals = corr[iu]
    order = np.argsort(-np.nan_to_num(vals, nan=-2.0), kind="stable")
    used: dict[str, int] = defaultdict(int)
    out = []
    for k in order:
        i, j = int(iu[0][k]), int(iu[1][k])
        c = float(vals[k])
        if not math.isfinite(c):
            break
        if used[names[i]] >= cap or used[names[j]] >= cap:
            continue
        out.append((names[i], names[j], c))
        used[names[i]] += 1
        used[names[j]] += 1
        if len(out) == n:
            break
    return out


def day_t(vals: list[float]) -> tuple[float | None, float | None, int]:
    if not vals:
        return None, None, 0
    mu = st.fmean(vals)
    if len(vals) < 3 or st.stdev(vals) == 0:
        return mu, None, len(vals)
    return mu, mu / (st.stdev(vals) / math.sqrt(len(vals))), len(vals)


def session_means(trades: list[dict], key: str = "net") -> dict[str, float]:
    by = defaultdict(list)
    for t in trades:
        by[t["day"]].append(t[key])
    return {d: st.fmean(v) for d, v in by.items()}


def excess(arm: list[dict], placebo: list[dict]) -> tuple[float | None, float | None, int]:
    """Per session where both traded: arm mean net minus placebo mean net. Day-clustered."""
    a, p = session_means(arm), session_means(placebo)
    return day_t([a[d] - p[d] for d in a if d in p])


def concentration(trades: list[dict]) -> dict[str, float | None]:
    """Session-clustered net without the best sessions, the best pairs, and the best names."""
    sm = session_means(trades)
    keep_s = sorted(sm.values())[:-DROP_SESSIONS] if len(sm) > DROP_SESSIONS else []
    by_pair, by_name = defaultdict(float), defaultdict(float)
    for t in trades:
        by_pair[t["pair"]] += t["net"]
        for nm in t["pair"].split("/"):
            by_name[nm] += t["net"]
    top_p = set(sorted(by_pair, key=by_pair.get, reverse=True)[:DROP_PAIRS])
    top_n = set(sorted(by_name, key=by_name.get, reverse=True)[:DROP_NAMES])
    wo_p = [t for t in trades if t["pair"] not in top_p]
    wo_n = [t for t in trades if not (set(t["pair"].split("/")) & top_n)]
    return {"wo_sessions": st.fmean(keep_s) if keep_s else None,
            "wo_pairs": day_t(list(session_means(wo_p).values()))[0],
            "wo_names": day_t(list(session_means(wo_n).values()))[0]}


def word(a: dict, b: dict, comb_t: float | None) -> str:
    """a, b: half stats {sessions, trades, net, net_t, ex, ex_t, ex_n, drop_share, conc}."""
    def pos(x):
        return x is not None and x > 0
    if b["sessions"] < MIN_SESSIONS or b["trades"] < MIN_TRADES or b["drop_share"] > MAX_DROP:
        return "UNDERPOWERED"
    conc_b = all(pos(v) for v in b["conc"].values())
    if (pos(b["net"]) and b["net_t"] is not None and b["net_t"] >= T_PASS and pos(b["ex"])
            and b["ex_t"] is not None and b["ex_t"] >= T_PASS and b["ex_n"] >= MIN_SESSIONS and conc_b):
        return "PASS"
    conc_ab = conc_b and all(pos(v) for v in a["conc"].values())
    if (pos(a["net"]) and pos(b["net"]) and comb_t is not None and comb_t >= T_PROMISING
            and pos(a["ex"]) and pos(b["ex"]) and conc_ab):
        return "PROMISING"
    return "FAIL"


# ── data ─────────────────────────────────────────────────────────────────────

def usable_days() -> list[str]:
    """The same 119 sessions as the path tests: days with IEX and SIP files, the first one dropped."""
    have = {f: {x[:-4] for x in os.listdir(os.path.join(WH, f)) if x.endswith(".npz")}
            for f in ("iex", "sip") if os.path.isdir(os.path.join(WH, f))}
    if len(have) < 2:
        return []
    both = sorted(have["iex"] & have["sip"])
    return [d for d in both[1:] if d <= LAST_DAY]


def load_day(day: str, want: set[str] | None = None) -> dict[str, dict[int, tuple[float, float]]]:
    """SIP {sym: {minute: (open, close)}} for one session; all names, or only `want`."""
    z = np.load(os.path.join(WH, "sip", f"{day}.npz"), allow_pickle=False)
    syms, idx, mins, o, c = z["syms"], z["sym_idx"], z["minute"], z["o"], z["c"]
    keep = np.arange(len(syms)) if want is None else np.array([i for i, s in enumerate(syms) if str(s) in want], int)
    out: dict[str, dict[int, tuple[float, float]]] = {}
    sel = np.isin(idx, keep)
    for i, m, oo, cc in zip(idx[sel], mins[sel], o[sel], c[sel]):
        out.setdefault(str(syms[i]), {})[int(m)] = (float(oo), float(cc))
    return out


def day_dollar_volume(day: str) -> dict[str, float]:
    z = np.load(os.path.join(WH, "sip", f"{day}.npz"), allow_pickle=False)
    dv = np.bincount(z["sym_idx"], weights=z["c"].astype(np.float64) * z["v"].astype(np.float64), minlength=len(z["syms"]))
    return {str(s): float(dv[i]) for i, s in enumerate(z["syms"])}


GRID = list(range(CORR_FROM, CORR_TO + 1, 5))


def five_min_returns(bars: dict) -> np.ndarray:
    """ln returns between 5-minute closes 09:45..15:50; NaN where either close is missing."""
    px = np.array([bars[m][1] if m in bars and bars[m][1] > 0 else np.nan for m in GRID])
    return np.log(px[1:] / px[:-1])


def corr_matrix(x: np.ndarray) -> np.ndarray:
    """Pairwise-complete correlation of the rows of x (names x observations, NaN = missing)."""
    ok = np.isfinite(x).astype(np.float64)
    v = np.nan_to_num(x)
    n = ok @ ok.T
    sx = v @ ok.T
    sxx = (v * v) @ ok.T
    sxy = v @ v.T
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = sxy - sx * sx.T / n
        var_i = sxx - sx * sx / n
        r = cov / np.sqrt(var_i * var_i.T)
    r[n < 100] = np.nan
    np.fill_diagonal(r, np.nan)
    return r


# ── scoring ──────────────────────────────────────────────────────────────────

def fit(days_a: list[str], all_days: list[str], declared: list[list[str]]):
    present = defaultdict(int)
    dv = defaultdict(list)
    for d in all_days:
        dvd = day_dollar_volume(d)
        for s, v in dvd.items():
            present[s] += 1
            if d <= CUT:
                dv[s].append(v)
    med_dv = {s: st.median(v) for s, v in dv.items()}
    every_a = [s for s in med_dv if len(dv[s]) == len(days_a)]
    top = sorted(every_a, key=lambda s: -med_dv[s])[:P2_TOP]
    pool = sorted(s for s in present if present[s] >= MIN_PRESENT and s in med_dv)
    declared_names = {n for p in declared for n in p}
    names = sorted(set(pool) | set(top) | (declared_names & set(present)))
    pos = {s: i for i, s in enumerate(names)}
    blocks = []
    for d in days_a:
        bars = load_day(d, set(names))
        blk = np.full((len(names), len(GRID) - 1), np.nan)
        for s, b in bars.items():
            blk[pos[s]] = five_min_returns(b)
        blocks.append(blk)
    X = np.concatenate(blocks, axis=1)
    R = corr_matrix(X)
    ti = [pos[s] for s in top]
    p2 = pick_p2(top, R[np.ix_(ti, ti)], n=P2_N, cap=P2_CAP)
    return {"present": present, "med_dv": med_dv, "names": names, "pos": pos, "X": X, "R": R, "pool": pool, "p2": p2}


def leg_order(a: str, b: str, med_dv: dict) -> tuple[str, str]:
    return (a, b) if med_dv.get(a, 0) >= med_dv.get(b, 0) else (b, a)


def placebo_for(a: str, F: dict) -> str | None:
    pos, R, X = F["pos"], F["R"], F["X"]
    cands = [s for s in F["pool"] if s != a]
    rs = np.array([R[pos[a], pos[s]] for s in cands])
    fin = np.isfinite(rs)
    if fin.sum() < 10:
        return None
    med = float(np.median(rs[fin]))
    for k in range(10_000):
        s = cands[_seed(a, "placebo", k, "pairs-revert") % len(cands)]
        r = R[pos[a], pos[s]]
        if not math.isfinite(r) or r >= med:
            continue
        beta = fit_beta(X[pos[a]], X[pos[s]])
        if beta is not None and beta > 0:
            return s
    return None


def fit_sigma(a: str, b: str, beta: float, days_a: list[str], cache: dict) -> float | None:
    vals = []
    for d in days_a:
        bars = cache[d]
        if a not in bars or b not in bars:
            continue
        for m in range(ENTRY_FROM, ENTRY_TO + 1):
            x = spread_at(bars[a], bars[b], beta, m)
            if x is not None:
                vals.append(x)
    return float(np.std(vals)) if len(vals) > 1000 else None


def build_pairs(F: dict, declared: list[list[str]], days_a: list[str], cache: dict) -> dict:
    """{arm: [pair spec]}; a spec is {a, b, beta, sigma, placebo: spec or None}."""
    X, pos = F["X"], F["pos"]

    def spec(a, b, order=True):
        if order:
            a, b = leg_order(a, b, F["med_dv"])
        if a not in pos or b not in pos:
            return {"a": a, "b": b, "status": "MISSING"}
        beta = fit_beta(X[pos[a]], X[pos[b]])
        if beta is None or beta <= 0:
            return {"a": a, "b": b, "beta": beta, "status": "beta<=0"}
        sigma = fit_sigma(a, b, beta, days_a, cache)
        if sigma is None or sigma <= 0:
            return {"a": a, "b": b, "beta": beta, "status": "no sigma"}
        return {"a": a, "b": b, "beta": beta, "sigma": sigma, "status": "ok"}

    arms = {"P1_declared": [], "P2_chosen": []}
    for a, b in declared:
        if min(F["present"].get(a, 0), F["present"].get(b, 0)) < MIN_PRESENT:
            arms["P1_declared"].append({"a": a, "b": b, "status": "MISSING"})
            continue
        arms["P1_declared"].append(spec(a, b))
    for a, b, _c in F["p2"]:
        arms["P2_chosen"].append(spec(a, b))
    for arm in arms.values():
        for p in arm:
            if p["status"] != "ok":
                continue
            plc = placebo_for(p["a"], F)
            p["placebo"] = None if plc is None else spec(p["a"], plc, order=False)
    return arms


def run_arm(specs: list[dict], days: list[str], cache_for) -> tuple[list[dict], list[dict], dict]:
    trades, placebo, cnt = [], [], {"dropped": 0, "taken": 0, "ctrl": []}
    for d in days:
        bars = cache_for(d)
        for p in specs:
            if p["status"] != "ok" or p["a"] not in bars or p["b"] not in bars:
                continue
            key = f"{p['a']}/{p['b']}"
            for t in simulate(bars[p["a"]], bars[p["b"]], p["beta"], p["sigma"]):
                if t["dropped"]:
                    cnt["dropped"] += 1
                    continue
                cnt["taken"] += 1
                t.update(day=d, pair=key, net=t["gross"] - COST_BP, net6=t["gross"] - HAIRCUT_BP)
                t["ctrl"] = quiet_control(bars[p["a"]], bars[p["b"]], p["beta"], p["sigma"], t, f"{key}|{d}")
                trades.append(t)
            q = p.get("placebo")
            if q and q.get("status") == "ok" and q["b"] in bars:
                for t in simulate(bars[q["a"]], bars[q["b"]], q["beta"], q["sigma"]):
                    if not t["dropped"]:
                        t.update(day=d, pair=f"{q['a']}/{q['b']}", net=t["gross"] - COST_BP)
                        placebo.append(t)
    return trades, placebo, cnt


def half_stats(trades, placebo, cnt_drop, cnt_taken) -> dict:
    sm = session_means(trades)
    mu, t, n = day_t(list(sm.values()))
    ex_mu, ex_t, ex_n = excess(trades, placebo)
    ctrl = [x["ctrl"] for x in trades if x["ctrl"] is not None]
    holds = [x["exit_m"] - x["entry_m"] for x in trades]
    reasons = defaultdict(int)
    for x in trades:
        reasons[x["reason"]] += 1
    return {"sessions": n, "trades": len(trades), "net": mu, "net_t": t,
            "gross": day_t(list(session_means(trades, "gross").values()))[0],
            "net6": day_t(list(session_means(trades, "net6").values()))[0],
            "placebo_net": day_t(list(session_means(placebo).values()))[0], "placebo_trades": len(placebo),
            "ex": ex_mu, "ex_t": ex_t, "ex_n": ex_n,
            "ctrl_gross": st.fmean(ctrl) if ctrl else None,
            "win": sum(1 for x in trades if x["net"] > 0) / len(trades) if trades else None,
            "hold": st.median(holds) if holds else None, "reasons": dict(reasons),
            "drop_share": cnt_drop / (cnt_drop + cnt_taken) if (cnt_drop + cnt_taken) else 0.0,
            "dropped": cnt_drop, "conc": concentration(trades) if trades else
            {"wo_sessions": None, "wo_pairs": None, "wo_names": None}}


def _f(x, nd=1):
    return "-" if x is None else f"{x:+.{nd}f}"


def presence_table(arms: dict, F: dict) -> list[str]:
    lines = ["## Presence (sessions of 119 with both names) and fits", "",
             "| arm | pair | sessions | beta | 2 sigma bp | placebo partner | status |", "|---|---|---:|---:|---:|---|---|"]
    for arm, specs in arms.items():
        for p in specs:
            n = min(F["present"].get(p["a"], 0), F["present"].get(p["b"], 0))
            two = "-" if p.get("sigma") is None else f"{2 * p['sigma'] * 1e4:.1f}"
            q = p.get("placebo")
            plc = "-" if not q else f"{q['b']} ({q['status']})"
            lines.append(f"| {arm} | {p['a']}/{p['b']} | {n} | {_f(p.get('beta'), 2)} | {two} | {plc} | {p['status']} |")
    return lines + [""]


def report(arms: dict, F: dict, days: list[str]) -> str:
    fit_days = [d for d in days if d <= CUT]
    check = [d for d in days if d > CUT]
    cache: dict = {}

    def cache_for(d):
        if d not in cache:
            cache.clear()
            want = {p[k] for specs in arms.values() for p in specs for k in ("a", "b")}
            want |= {p["placebo"]["b"] for specs in arms.values() for p in specs if p.get("placebo")}
            cache[d] = load_day(d, want)
        return cache[d]

    lines = ["# Pairs reversion (pairs_revert_prereg)", "",
             "Net = gross - 3 bp of gross notional (3 bp round trip per leg). net6 is the 2x haircut. Session-clustered.",
             "excess: per session where the arm and its placebo set both traded, arm mean net minus placebo mean net.",
             "Half A is in-sample for beta, sigma and the P2 pick. The call is half B.", ""]
    words, blocks = [], []
    for arm, specs in arms.items():
        ta, pa, ca = run_arm(specs, fit_days, cache_for)
        tb, pb, cb = run_arm(specs, check, cache_for)
        a = half_stats(ta, pa, ca["dropped"], ca["taken"])
        b = half_stats(tb, pb, cb["dropped"], cb["taken"])
        comb_t = day_t(list(session_means(ta).values()) + list(session_means(tb).values()))[1]
        w = word(a, b, comb_t)
        words.append(f"{arm}: {w}")
        blocks += [f"## {arm}", "", f"**{w}**   combined t {_f(comb_t, 2)}", "",
                   "| half | sessions | trades | gross | net | t | net6 | placebo net (n) | excess (t, sessions) | quiet ctrl gross | win | median hold | exits | dropped |",
                   "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|"]
        for tag, s in (("A", a), ("B", b)):
            win = "-" if s["win"] is None else f"{s['win']:.0%}"
            blocks.append(f"| {tag} | {s['sessions']} | {s['trades']} | {_f(s['gross'])} | {_f(s['net'])} | {_f(s['net_t'], 2)} | "
                          f"{_f(s['net6'])} | {_f(s['placebo_net'])} ({s['placebo_trades']}) | {_f(s['ex'])} ({_f(s['ex_t'], 2)}, {s['ex_n']}) | "
                          f"{_f(s['ctrl_gross'])} | {win} | {s['hold'] if s['hold'] is not None else '-'} | {s['reasons']} | {s['dropped']} |")
        blocks += ["", f"concentration A {a['conc']} B {b['conc']}", "",
                   "Half B by pair:", "", "| pair | trades | gross | net |", "|---|---:|---:|---:|"]
        by = defaultdict(list)
        for t in tb:
            by[t["pair"]].append(t)
        for pk, rr in sorted(by.items(), key=lambda kv: -sum(t["net"] for t in kv[1])):
            blocks.append(f"| {pk} | {len(rr)} | {_f(st.fmean(t['gross'] for t in rr))} | {_f(st.fmean(t['net'] for t in rr))} |")
        blocks.append("")
    lines += [" | ".join(words), ""] + presence_table(arms, F) + blocks
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    days = usable_days()
    cut = days[(len(days) - 1) // 2] if days else None
    print(f"sessions {len(days)} {days[0] if days else '-'}..{days[-1] if days else '-'}; median split {cut}", flush=True)
    if not (len(days) == N_DAYS and days[0] == FIRST_DAY and days[-1] == LAST_DAY and cut == CUT):
        print(f"REFUSING: window differs from the prereg (n {len(days)}, cut {cut})")
        return
    declared = json.load(open(PREREG))["pairs_declared"]
    fit_days = [d for d in days if d <= CUT]
    print("fitting on half A (dollar volume, 5-minute returns, P2 pick)...", flush=True)
    F = fit(fit_days, days, declared)
    need = {n for p in declared for n in p} | {n for p in F["p2"] for n in p[:2]}
    print("loading half A bars for sigma...", flush=True)
    cache = {d: load_day(d, need) for d in fit_days}
    arms = build_pairs(F, declared, fit_days, cache)
    # placebo partners need sigma too
    plc_names = {p["placebo"]["b"] for specs in arms.values() for p in specs if p.get("placebo")}
    if plc_names - need:
        extra = {d: load_day(d, plc_names - need) for d in fit_days}
        for d in fit_days:
            cache[d].update(extra[d])
        arms = build_pairs(F, declared, fit_days, cache)
    print("\n".join(presence_table(arms, F)), flush=True)
    if not a.score:
        print("(presence and fits only; --score after the prereg has a result-skeptic READY)")
        return
    md = report(arms, F, days)
    print(md, flush=True)
    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    with open(RESULT, "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
