#!/usr/bin/env python3
"""rsi2_colour_timing - does buying at the START of a red RSI-2 segment (RED_START), the END of a green one (GREEN_END),
or on a resting buy-stop at the price that turns the bar red (RED_STOP) catch a run that beats the same minute on other
names and pays the real spread? Operator 11/20 (screenshots). Prereg + amended_1: docs/studies/rsi2_colour_timing_prereg.json.

    .venv/bin/python tools/studies/rsi2_colour_timing.py chart              # 11/19 FIGS + COLD per-bar RSI-2 / colours
    .venv/bin/python tools/studies/rsi2_colour_timing.py build              # CPU: per-day cache (no API)
    .venv/bin/python tools/studies/rsi2_colour_timing.py edge  --colours pine           # EDGE verdicts (gross, no quotes); plain|strict = old diagnostics
    .venv/bin/python tools/studies/rsi2_colour_timing.py run   --colours pine           # + quotes, PROFITABLE, report (amended_3; eth + build first)

The colour mode is decided by the chart match (amended_1 B2) and must be passed explicitly. Quote fetches use the data key
shared with the live dashboard: 'run' refuses to fetch 09:00-16:05 ET.

RSI-2 is ai_entry_watch.cm_rsi_series arithmetic (Wilder alpha 1/2, first delta seeds, down == 0 -> 100, missing minutes
skipped) on the last 220 regular-hours closes of the prior session + today's closes, as live symbol_ohlc feeds it. For the
plain rules (prereg: seeded at 09:30) the prior-session warm-up changes RSI-2 by < 1e-4 after 09:45 (seed weight 0.5^15).
The red trigger price is ai_entry_watch.rsi2_trigger_prices arithmetic (P = C + 9D - U), rounded UP to the cent. Both
equalities are unit-tested against the live functions.
"""
from __future__ import annotations

import hashlib
import json
import math
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

OUT = ROOT / "ai_reports" / "rsi2_colour"
CACHE = OUT / "cache"
OWNQ = OUT / "quotes.json"
SEARCH = ("2026-04-28", "2026-09-23")
CONFIRM = ("2026-09-24", "2026-11-18")
SPLIT = "2026-07-11"                      # halves by calendar date (amended_1 m5)
WARM = 220                                # prior-session RTH bars prepended
T_LO, T_HI = 15, 360                      # signal bar 09:45 .. 15:30
CAP_BARS = 15
CHG_MIN, PX_LO, PX_HI = 1.5, 20.0, 400.0
GATE_BP, CAP_PER_HALF = 15.0, 1500
EDGE_T, MIN_NET, MIN_N, MDE_MAX = 2.5, 2.0, 300, 10.0
STOP_SLIP_BP, STRESS = 5.0, 1.5
TICK = 0.01
RULES = ("RED_START", "GREEN_END", "RED_STOP", "GREEN_CROSS")
STOP_RULES = ("RED_STOP", "GREEN_CROSS")
BUILD_SHA = ""
PINE_KEYS = ("rsi_pine", "U_pine", "D_pine", "s200_pine", "s5_pine", "m4_pine", "m199_pine", "red_pine", "green_pine")
TRAIL_PCT, BE_PCT, FLAT_T, RATCHET_CAP = 0.6, 0.15, 380, 60   # approximate desk ratchet (one_two_three's), 15:50 flat
CAP_BP = 5.0
ETH = OUT / "eth"


# ---------------------------------------------------------------- indicator (pure)

def rsi_state(closes):
    """cm_rsi_series arithmetic over a 1-D array with NaN gaps skipped. -> (rsi, up, down) arrays aligned to closes;
    NaN where the close is NaN or before the first delta. Pure."""
    n = len(closes)
    rsi, U, D = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    prev, up, down, k = None, 0.0, 0.0, 0
    for i in range(n):
        c = closes[i]
        if not np.isfinite(c):
            continue
        if prev is not None:
            d = c - prev
            g, l_ = (d if d > 0 else 0.0), (-d if d < 0 else 0.0)
            if k == 0:
                up, down = g, l_
            else:
                up, down = 0.5 * g + 0.5 * up, 0.5 * l_ + 0.5 * down
            k += 1
            U[i], D[i] = up, down
            rsi[i] = 100.0 if down == 0 else (0.0 if up == 0 else 100.0 - 100.0 / (1.0 + up / down))
        prev = c
    return rsi, U, D


def red_trigger(c_prev, up, down):
    """Close of the next bar at which RSI-2 reaches 90 (rsi2_trigger_prices: c + max(9D - U, 0.01)), rounded UP to
    the cent. Pure."""
    if not (np.isfinite(c_prev) and np.isfinite(up) and np.isfinite(down)):
        return np.nan
    p = c_prev + max(9.0 * down - up, 0.01)
    return math.ceil(round(p * 100, 6)) / 100.0


def sma(x, n):
    """Trailing simple mean over the last n FINITE values ending at each index (NaN until n exist). Pure."""
    out = np.full(len(x), np.nan)
    buf = []
    for i, v in enumerate(x):
        if np.isfinite(v):
            buf.append(v)
            if len(buf) > n:
                buf.pop(0)
        if len(buf) == n and np.isfinite(v):
            out[i] = sum(buf) / n
    return out


def colours(closes_full, warm):
    """closes_full = prior-session warm-up (length warm) + today's 390. -> dict of today's arrays: rsi, U, D,
    red/green plain (>= 90 / <= 10) and strict (panel: red RSI > 90 & c < SMA200 & c > SMA5; green RSI < 10 &
    c > SMA200 & c < SMA5). Pure."""
    rsi, U, D = rsi_state(closes_full)
    s200, s5 = sma(closes_full, 200), sma(closes_full, 5)
    c = closes_full
    with np.errstate(invalid="ignore"):
        red_s = (rsi > 90) & (c < s200) & (c > s5)
        green_s = (rsi < 10) & (c > s200) & (c < s5)
        red_p, green_p = rsi >= 90, rsi <= 10
    sl = slice(warm, None)
    return {"rsi": rsi[sl], "U": U[sl], "D": D[sl], "red_plain": red_p[sl], "green_plain": green_p[sl],
            "red_strict": red_s[sl], "green_strict": green_s[sl]}


# ---------------------------------------------------------------- trades (pure)

def hkey(*p):
    return int(hashlib.sha256("|".join(str(x) for x in p).encode()).hexdigest(), 16)


def px_or(arr, i):
    return float(arr[i]) if 0 <= i < len(arr) and np.isfinite(arr[i]) else np.nan


def exit_for(rule, red, t_from, t, o, c):
    """Exit of a trade whose first eligible exit bar is t_from (RED_START/GREEN_END: t+1; RED_STOP: t). Cap: the close
    of t+15. -> (exit_kind, exit_bar, exit_px): kind 'open' sells o[s+1], 'cap' sells c[t+15]. Pure."""
    cap = t + CAP_BARS
    if rule == "GREEN_END":
        seen = False
        for s in range(t_from, cap + 1):          # GREEN_END: t+1; GREEN_CROSS: the fill bar t (review 11/20)
            if red[s]:
                seen = True
            elif seen:
                return "open", s + 1, px_or(o, s + 1)
    else:
        for s in range(t_from, cap + 1):
            if not red[s]:
                return "open", s + 1, px_or(o, s + 1)
    return "cap", cap, px_or(c, cap)


def price_at(kind, bar, o, c):
    return px_or(o, bar) if kind == "open" else px_or(c, bar)


def passes_filter(cl, pc, i):
    if not (0 <= i < len(cl)) or not np.isfinite(cl[i]) or not pc > 0:
        return False
    return PX_LO <= cl[i] <= PX_HI and (cl[i] / pc - 1) * 100 >= CHG_MIN


def stop_fill(P, o_t, h_t):
    """Buy-stop at P for one bar: fill only if high >= P + 1 tick, at max(P, open) + 1 tick (amended_1 M6). Pure."""
    if not (np.isfinite(P) and np.isfinite(h_t)) or h_t < P + TICK - 1e-9:
        return np.nan
    base = P if not np.isfinite(o_t) else max(P, o_t)
    return base + TICK


def stop_valid_red(P, c_prev, s200_prev, m4_prev, s5_prev):
    """amended_3 RED_STOP validity: close[t-1] < SMA200 and > SMA5 at t-1, and P > mean(closes t-4..t-1) (a close at
    P keeps close > SMA5) and P < SMA200[t-1]. Pure."""
    vals = (P, c_prev, s200_prev, m4_prev, s5_prev)
    if not all(np.isfinite(v) for v in vals):
        return False
    return P > m4_prev and P < s200_prev                   # amended_5: no close[t-1] > SMA5 requirement


def green_cross_trigger(c_prev, up, down, m4_prev, s200_prev):
    """amended_cross_price GREEN_CROSS: the lower of P10 (close at which RSI-2 returns to 10: c + max(D/9 - U, tick))
    and S5 (close reaching the 5-bar mean incl. itself = mean of closes t-4..t-1), each rounded UP to the cent; NaN
    unless P > SMA200[t-1]. Pure."""
    if not all(np.isfinite(v) for v in (c_prev, up, down, m4_prev, s200_prev)):
        return np.nan
    p10 = math.ceil(round((c_prev + max(down / 9.0 - up, TICK)) * 100, 6)) / 100.0
    s5 = math.ceil(round(m4_prev * 100, 6)) / 100.0
    P = min(p10, s5)
    return P if P > s200_prev else np.nan


def pine_day(rth_prev, ext, rth_today):
    """amended_3 series: prior-session RTH closes (390, NaN gaps) + extended-hours closes (in time order) + today's
    RTH closes (390). -> dict of today's 390-arrays: rsi_pine, U_pine, D_pine, s200_pine, s5_pine, m4_pine (mean of
    the 4 closes ending at t), red_pine, green_pine; on a minute with no bar the state carries. Pure."""
    seq, pos = [], np.full(390, -1)
    for v in rth_prev:
        if np.isfinite(v):
            seq.append(float(v))
    for v in ext:
        if np.isfinite(v):
            seq.append(float(v))
    for t, v in enumerate(rth_today):
        if np.isfinite(v):
            seq.append(float(v))
        pos[t] = len(seq) - 1
    x = np.array(seq, float)
    rsi, U, D = pine_rsi(x)
    s200, s5 = sma(x, 200), sma(x, 5)
    m4 = sma(x, 4)
    m199 = sma(x, 199)
    out = {k: np.full(390, np.nan) for k in ("rsi_pine", "U_pine", "D_pine", "s200_pine", "s5_pine", "m4_pine",
                                            "m199_pine")}
    red, green = np.zeros(390, bool), np.zeros(390, bool)
    for t in range(390):
        i = pos[t]
        if i < 0:
            continue
        for k, arr in (("rsi_pine", rsi), ("U_pine", U), ("D_pine", D), ("s200_pine", s200), ("s5_pine", s5),
                       ("m4_pine", m4), ("m199_pine", m199)):
            out[k][t] = arr[i]
        c, r, a, b = x[i], rsi[i], s200[i], s5[i]
        if np.isfinite(r) and np.isfinite(a) and np.isfinite(b):
            red[t] = r > 90 and c < a and c > b
            green[t] = r < 10 and c > a and c < b
    out["red_pine"], out["green_pine"] = red, green
    return out


def cent_above(x):
    """Lowest cent price strictly above x. Pure."""
    return round(math.floor(round(x * 100, 6)) / 100.0 + TICK, 2)


def red_trigger_exact(c_prev, up, down, m4_prev, m199_prev):
    """amended_6/7: the lowest cent price at which bar t closes red - RSI-2 strictly > 90 and close strictly above
    the 5-bar mean incl. itself (> m4) - and whether it is strictly below the 200-bar mean incl. itself (< m199).
    RSI threshold x: if 9D - U >= 0 a rise of 9D - U is needed (x = c + 9D - U); if RSI is already above 90 (U > 9D)
    a fall of up to U/9 - D still keeps it above 90 (x = c - (U/9 - D)). -> (P, below_sma200, rsi_already_hot) or
    (NaN, None, None). Pure."""
    if not all(np.isfinite(v) for v in (c_prev, up, down, m4_prev, m199_prev)):
        return np.nan, None, None
    hot = 9.0 * down - up < 0
    x = c_prev - (up / 9.0 - down) if hot else c_prev + 9.0 * down - up
    P = max(cent_above(x), cent_above(m4_prev))
    return P, P < m199_prev, hot


def ratchet_exit(o, h, l, c, ent_bar, entry, at_open, last=FLAT_T):
    """APPROXIMATE desk exit on 1-minute bars (amended_6 step 1): stop = entry - 0.6%; once the running high reaches
    entry + 0.15% the stop is at least entry; the stop trails the running high - 0.6% (live: give_r 0.12R ~ 0.6% at
    1R ~ 5%, be_at_pct 0.15; the shape is one_two_three's approximation, not the live code). A bar whose low touches
    the stop exits at min(stop, open). An entry AT THE OPEN (at_open) is watched from its own bar; a stop fill inside
    bar t from bar t+1 (its position inside bar t is unknown). The stop is tested before the bar's high updates it.
    Flat at the close of `last` (15:50) or after 60 bars. -> (exit price, exit bar) or (NaN, bar). Pure."""
    stop = entry * (1 - TRAIL_PCT / 100)
    hi = entry
    first = ent_bar if at_open else ent_bar + 1
    end = min(last, ent_bar + RATCHET_CAP, len(c) - 1)
    for b in range(first, end + 1):
        if not all(np.isfinite(x[b]) for x in (o, h, l, c)):
            continue
        if l[b] <= stop:
            return min(stop, o[b]), b
        hi = max(hi, h[b])
        if hi >= entry * (1 + BE_PCT / 100):
            stop = max(stop, entry)
        stop = max(stop, hi * (1 - TRAIL_PCT / 100))
    return px_or(c, end), end


def passes_filter_chg(cl, pc, i, chg_min):
    if not (0 <= i < len(cl)) or not np.isfinite(cl[i]) or not pc > 0:
        return False
    return PX_LO <= cl[i] <= PX_HI and (cl[i] / pc - 1) * 100 >= chg_min


def _outcomes(o, h, l, c, ent, e, at_open):
    """Gross bp for the close of the 5th / 15th bar counting the entry bar, and the approximate ratchet. Pure."""
    rx, xb = ratchet_exit(o, h, l, c, ent, e, at_open)
    return ((px_or(c, ent + 4) / e - 1) * 1e4, (px_or(c, ent + 14) / e - 1) * 1e4, (rx / e - 1) * 1e4, xb)


def _entry_on(rule, o, h, c, t, dist):
    """Same entry type on any name: RED_START buys open[t+1]; stop rules place a same-distance buy-stop above
    close[t-1] for bar t (stop_fill). -> (entry, entry bar, capmiss) or (NaN, None, False). Pure."""
    if rule == "RED_START":
        return px_or(o, t + 1), t + 1, False
    cp = px_or(c, t - 1)
    if not np.isfinite(cp):
        return np.nan, None, False
    P = math.ceil(round(cp * (1 + dist) * 100, 6)) / 100.0
    e = stop_fill(P, px_or(o, t), px_or(h, t))
    return e, t, bool(np.isfinite(o[t]) and o[t] > P * (1 + CAP_BP / 1e4))


def red_screen_name(d, k, chg_min=None):
    """amended_6/7 step 1 for one name-day: RED_STOP (exact trigger, below SMA200), RED_UP (same trigger at or above
    SMA200, information) and RED_START (bar close). A signal is skipped while that rule's trade on the name is open
    (refractory to the ratchet exit bar, and >= 15 bars between entries). -> list of trade dicts. Pure."""
    chg = CHG_MIN if chg_min is None else chg_min
    o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
    pc = d["pc"][k]
    red = d["red_pine"][k]
    U, D, m4, m199 = d["U_pine"][k], d["D_pine"][k], d["m4_pine"][k], d["m199_pine"][k]
    out = []
    free = {"RED_STOP": -1, "RED_UP": -1, "RED_START": -1}       # first bar a new signal may fire
    for t in range(T_LO, T_HI + 1):
        if not bool(red[t - 1]) and passes_filter_chg(c, pc, t - 1, chg):
            P, below, hot = red_trigger_exact(px_or(c, t - 1), U[t - 1], D[t - 1], m4[t - 1], m199[t - 1])
            if np.isfinite(P):
                rule = "RED_STOP" if below else "RED_UP"
                if t >= free[rule]:
                    e = stop_fill(P, px_or(o, t), px_or(h, t))
                    if np.isfinite(e):
                        r = _red_row(rule, d, k, t, t, e, P / c[t - 1] - 1, chg,
                                     capmiss=bool(np.isfinite(o[t]) and o[t] > P * (1 + CAP_BP / 1e4)), hot=hot)
                        free[rule] = max(r["exit_bar"] + 1, t + 15)
                        out.append(r)
        if np.isfinite(c[t]) and bool(red[t]) and not bool(red[t - 1]) and passes_filter_chg(c, pc, t, chg) \
                and t >= free["RED_START"]:
            e = px_or(o, t + 1)
            if np.isfinite(e):
                r = _red_row("RED_START", d, k, t, t + 1, e, None, chg, capmiss=False, hot=None)
                free["RED_START"] = max(r["exit_bar"] + 1, t + 15)
                out.append(r)
    return out


def _red_row(rule, d, k, t, ent, e, dist, chg, capmiss, hot):
    o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
    at_open = rule == "RED_START"
    f5, f15, rt, xb = _outcomes(o, h, l, c, ent, e, at_open)
    r = {"rule": rule, "day": d["day"], "sym": d["syms"][k], "t": t, "ent_bar": ent, "entry": e, "dist": dist,
         "capmiss": capmiss, "hot": hot, "fix5": f5, "fix15": f15, "ratchet": rt, "exit_bar": xb}
    fbar = t if at_open else t - 1
    U_ = len(d["syms"])
    peers = sorted((j for j in range(U_) if j != k and passes_filter_chg(d["c"][j], d["pc"][j], fbar, chg)),
                   key=lambda j: hkey("rsi2red-peer", d["day"], d["syms"][j], t))[:5]
    vals = {"fix5": [], "fix15": [], "ratchet": [], "ratchet_capped": []}
    for j in peers:
        oj, hj, lj, cj = d["o"][j], d["h"][j], d["l"][j], d["c"][j]
        ej, entj, cmj = _entry_on(rule, oj, hj, cj, t, dist)
        if not np.isfinite(ej):
            continue
        a5, a15, art, _ = _outcomes(oj, hj, lj, cj, entj, ej, at_open)
        vals["fix5"].append(a5); vals["fix15"].append(a15); vals["ratchet"].append(art)
        if not cmj:
            vals["ratchet_capped"].append(art)
    for key, v in vals.items():
        v = [x for x in v if np.isfinite(x)]
        r[f"ctl_{key}"] = float(np.mean(v)) if v else np.nan
        base = "ratchet" if key == "ratchet_capped" else key
        ok = v and np.isfinite(r[base]) and not (key == "ratchet_capped" and capmiss)
        r[f"lift_{key}"] = r[base] - r[f"ctl_{key}"] if ok else np.nan
    r["peers"] = len(vals["ratchet"])
    return r


def red_unconditional(d, rows, chg):
    """P2 control: for each signal row of the day, a random filtered (name, minute) with the same entry type
    (same-distance stop or next open) and the same exits. -> list of (rule, ratchet gross). Pure given the hash."""
    out = []
    U_ = len(d["syms"])
    for r in rows:
        rng = np.random.default_rng(hkey("rsi2red-unc", d["day"], r["sym"], r["t"], r["rule"]) % 2 ** 32)
        for _ in range(20):
            j, t = int(rng.integers(0, U_)), int(rng.integers(T_LO, T_HI + 1))
            if passes_filter_chg(d["c"][j], d["pc"][j], t if r["rule"] == "RED_START" else t - 1, chg):
                break
        else:
            continue
        oj, hj, lj, cj = d["o"][j], d["h"][j], d["l"][j], d["c"][j]
        ej, entj, _ = _entry_on(r["rule"], oj, hj, cj, t, r["dist"])
        if np.isfinite(ej):
            g = _outcomes(oj, hj, lj, cj, entj, ej, r["rule"] == "RED_START")[2]
            if np.isfinite(g):
                out.append((r["rule"], g))
    return out


def _stat(rr, key):
    a, b = halves(rr)
    lk = f"lift_{key}"
    both = [r for r in rr if np.isfinite(r.get(lk, np.nan)) and np.isfinite(r.get(key, np.nan))]
    ba, bb = halves(both)
    return {"gross_h1": r2(mean(a, key)), "gross_h2": r2(mean(b, key)),
            "gross_on_lift_rows_h1": r2(mean(ba, key)), "gross_on_lift_rows_h2": r2(mean(bb, key)),
            "lift_h1": r2(mean(ba, lk)), "lift_h2": r2(mean(bb, lk)), "t": r2(day_t(both, lk)),
            "mde_h1": r2(mde(ba, lk)), "mde_h2": r2(mde(bb, lk)), "n_lift_h1": len(ba), "n_lift_h2": len(bb)}


def red_screen():
    """amended_7 step 1 report. Gate to step 2 (written in the prereg before the run): RED_STOP ratchet lift > 0 in
    both halves AND pooled day-t >= 1.0 AND each half's MDE <= 10 bp (else UNDERPOWERED: no step 2)."""
    rows, unc = [], []
    for d in load_days(*SEARCH):
        if "m199_pine" not in d:
            sys.exit("cache built before amended_6 (no m199_pine): rerun build")
        dr = []
        for k in range(len(d["syms"])):
            dr += red_screen_name(d, k)
        rows += dr
        unc += red_unconditional(d, dr, CHG_MIN)
    out = {"prereg": "docs/studies/rsi2_colour_timing_prereg.json amended_7 step 1 (bar-level, ratchet APPROXIMATE)",
           "labels": {"fix5": "close of the 5th bar counting the entry bar", "fix15": "close of the 15th bar",
                      "capmiss": "LOWER BOUND - only opens gapping past P + 5 bp; fast moves through the cap inside a bar are not seen"}}
    for rule in ("RED_STOP", "RED_START", "RED_UP"):
        rr = [r for r in rows if r["rule"] == rule]
        a, b = halves(rr)
        u = [g for rl, g in unc if rl == rule]
        rep_ = {"n": len(rr), "n_h1": len(a), "n_h2": len(b), "no_peer": sum(1 for r in rr if r["peers"] == 0),
                "ratchet_nan": sum(1 for r in rr if not np.isfinite(r["ratchet"]))}
        for key in ("fix5", "fix15", "ratchet", "ratchet_capped"):
            rep_[key] = _stat(rr, key)
        cm, um = mean(rr, "ctl_ratchet"), (float(np.mean(u)) if u else None)
        rep_["peer_mean_ratchet"], rep_["unconditional_mean_ratchet"] = r2(cm), r2(um)
        rep_["peer_vs_unconditional"] = r2(cm - um if cm is not None and um is not None else None)
        rep_["peer_vs_unconditional_t"] = r2(welch_t([r["ctl_ratchet"] for r in rr if np.isfinite(r["ctl_ratchet"])], u))
        if rule != "RED_START":
            rep_["dist_buckets (ratchet)"] = {nm: {"n": len(x), "gross": r2(mean(x, "ratchet")), "lift": r2(mean(x, "lift_ratchet"))}
                                              for nm, x in (("<5bp", [r for r in rr if r["dist"] < 5e-4]),
                                                            ("5-20bp", [r for r in rr if 5e-4 <= r["dist"] < 2e-3]),
                                                            ("20-50bp", [r for r in rr if 2e-3 <= r["dist"] < 5e-3]),
                                                            (">50bp", [r for r in rr if r["dist"] >= 5e-3]))}
            rep_["rsi_already_hot (ratchet)"] = {"n": sum(1 for r in rr if r["hot"]), "lift": r2(mean([r for r in rr if r["hot"]], "lift_ratchet"))}
            rep_["cap_miss_share (lower bound)"] = r2(float(np.mean([r["capmiss"] for r in rr]))) if rr else None
        out[rule] = rep_
    nf = []
    for d in load_days(*SEARCH):                  # information: no +1.5% day filter (amended_5)
        for k in range(len(d["syms"])):
            nf += red_screen_name(d, k, chg_min=-1e9)
    out["no_day_filter (information)"] = {rule: {"n": len(x), **_stat(x, "ratchet")}
                                          for rule in ("RED_STOP", "RED_START", "RED_UP")
                                          for x in [[r for r in nf if r["rule"] == rule]]}
    s_ = out["RED_STOP"]["ratchet"]
    if s_["mde_h1"] is None or s_["mde_h2"] is None or s_["mde_h1"] > 10 or s_["mde_h2"] > 10:
        gate = "UNDERPOWERED - no step 2"
    elif s_["lift_h1"] > 0 and s_["lift_h2"] > 0 and (s_["t"] or 0) >= 1.0:
        gate = "PASS - run the second-level live-exit replay"
    else:
        gate = "STOP - RED_STOP ratchet lift not > 0 in both halves with t >= 1.0"
    out["STEP2_GATE"] = gate
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "red_screen.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


def trades_for_name(rule, d, k, mode, t_lo=None, t_hi=None):
    """All trades of one rule on one name-day (row k of day d) with the refractory. -> list of dicts. Pure."""
    o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
    pc = d["pc"][k]
    red, green = d[f"red_{mode}"][k], d[f"green_{mode}"][k]
    pine = mode == "pine"
    rsi = d["rsi_pine"][k] if pine else d["rsi"][k]
    UU, DD = (d["U_pine"][k], d["D_pine"][k]) if pine else (d["U"][k], d["D"][k])
    s200, m4 = (d["s200_pine"][k], d["m4_pine"][k]) if pine else (None, None)
    out, busy_until, last_entry = [], -1, -10 ** 9
    counts = defaultdict(int)
    if rule == "GREEN_CROSS" and not pine:
        return [], counts                         # GREEN_CROSS exists only on the Pine series (amended_3)
    for t in range(T_LO if t_lo is None else t_lo, (T_HI if t_hi is None else t_hi) + 1):
        if pine and not np.isfinite(c[t]) and rule not in STOP_RULES:
            continue                              # no bar this minute: state carries, nothing fires (amended_3)
        if rule == "RED_START":
            fire = bool(red[t]) and not bool(red[t - 1])
        elif rule == "GREEN_END":
            fire = (not bool(green[t])) and bool(green[t - 1]) and np.isfinite(rsi[t])
            if fire and pine and not (np.isfinite(s200[t]) and c[t] > s200[t]):
                counts["green_end_down"] += 1     # green ended by breaking SMA200: not an entry (amended_3)
                fire = False
        elif rule == "GREEN_CROSS":               # armed at the open of t if completed bar t-1 was green
            fire = bool(green[t - 1]) and np.isfinite(rsi[t - 1])
        else:   # RED_STOP: armed at the open of t if bar t-1 is not red; filter at close[t-1] (M7)
            fire = np.isfinite(rsi[t - 1]) and rsi[t - 1] < 90 and not bool(red[t - 1])
        if not fire:
            continue
        fbar = t - 1 if rule in STOP_RULES else t
        if not passes_filter(c, pc, fbar):
            continue
        if t < busy_until or t < last_entry + 15:
            counts["refractory"] += 1
            continue
        if rule in STOP_RULES:
            if rule == "RED_STOP":
                P = red_trigger(c[t - 1], UU[t - 1], DD[t - 1])
                if pine and not stop_valid_red(P, c[t - 1], s200[t - 1], m4[t - 1], d["s5_pine"][k][t - 1]):
                    counts["sma_invalid"] += 1
                    continue
            else:
                P = green_cross_trigger(c[t - 1], UU[t - 1], DD[t - 1], m4[t - 1], s200[t - 1])
                if not np.isfinite(P):
                    counts["sma_invalid"] += 1
                    continue
            entry = stop_fill(P, o[t], h[t])
            counts["armed"] += 1
            if not np.isfinite(entry):
                continue
            counts["filled"] += 1
            if np.isfinite(c[t]) and c[t] < P:
                counts["failed_pop"] += 1
            kind, ebar, xpx = exit_for("GREEN_END" if rule == "GREEN_CROSS" else rule, red, t, t, o, c)
            ent_bar, dist = t, P / c[t - 1] - 1
            thin = (h[t] - P) / P * 1e4 < 2.0
        else:
            entry = px_or(o, t + 1)
            kind, ebar, xpx = exit_for(rule, red, t + 1, t, o, c)
            ent_bar, dist, thin = t + 1, None, False
        if not (np.isfinite(entry) and np.isfinite(xpx)):
            counts["no_price"] += 1
            continue
        g = (xpx / entry - 1) * 1e4
        out.append({"sym": d["syms"][k], "t": t, "ent_bar": ent_bar, "entry": entry, "kind": kind, "ebar": ebar,
                    "gross": g, "dist": dist, "thin": thin, "wick": wick_pressure(o, h, l, c, fbar + 1 if rule in STOP_RULES else t + 1),
                    "wick3": wick_pressure(o, h, l, c, fbar + 1 if rule in STOP_RULES else t + 1, n=3),
                    "fix5": (px_or(c, ent_bar + 4) / entry - 1) * 1e4 if rule not in STOP_RULES else (px_or(c, t + 5) / entry - 1) * 1e4,
                    "fix15": (px_or(c, ent_bar + 14) / entry - 1) * 1e4 if rule not in STOP_RULES else (px_or(c, t + 15) / entry - 1) * 1e4})
        busy_until = ebar + (1 if kind == "cap" else 0)
        last_entry = t
    return out, counts


def ctl_trade(rule, d, j, row):
    """Gross of the hold-matched control on name j for a signal row (amended_1 B1/B3). RED_START/GREEN_END: buy o[t+1],
    sell at the signal's exit (same bar, same price type). RED_STOP: a buy-stop at the same percent distance above
    c_j[t-1], same fill model, same exit. -> gross bp or NaN (no fill / no price). Pure."""
    o, h, c = d["o"][j], d["h"][j], d["c"][j]
    t = row["t"]
    if rule in STOP_RULES:
        cp = px_or(c, t - 1)
        if not np.isfinite(cp):
            return np.nan
        P = math.ceil(round(cp * (1 + row["dist"]) * 100, 6)) / 100.0
        entry = stop_fill(P, o[t], h[t])
    else:
        entry = px_or(o, t + 1)
    xpx = price_at(row["kind"], row["ebar"], o, c)
    if not (np.isfinite(entry) and np.isfinite(xpx)):
        return np.nan
    return (xpx / entry - 1) * 1e4


def same_name_later(rule, d, k, row):
    """Same name-day, an entry in exit+2 .. exit+62 (hash), held the same number of bars with the same price types
    (amended_1 M5/M8). Dropped if it runs past the session. -> gross or NaN. Pure."""
    o, h, c = d["o"][k], d["h"][k], d["c"][k]
    hold = row["ebar"] - row["ent_bar"]
    first = row["ebar"] + 2
    span = 61
    x = first + hkey("rsi2col-later", d["day"], row["sym"], row["t"]) % span
    if rule in STOP_RULES:
        cp = px_or(c, x - 1)
        if not np.isfinite(cp):
            return np.nan
        P = math.ceil(round(cp * (1 + row["dist"]) * 100, 6)) / 100.0
        entry, ent = stop_fill(P, px_or(o, x), px_or(h, x)), x
    else:
        entry, ent = px_or(o, x), x
    xb = ent + hold
    if xb > 389:
        return None
    xpx = price_at(row["kind"], xb, o, c)
    if not (np.isfinite(entry) and np.isfinite(xpx)):
        return np.nan
    return (xpx / entry - 1) * 1e4


def day_rows(rule, d, mode, t_lo=None, t_hi=None):
    """Every trade of a rule on a day with its controls. -> (rows, counts). Pure."""
    rows, counts = [], defaultdict(int)
    U = len(d["syms"])
    for k in range(U):
        tr, cn = trades_for_name(rule, d, k, mode, t_lo, t_hi)
        for key, v in cn.items():
            counts[key] += v
        for r in tr:
            fbar = r["t"] - 1 if rule in STOP_RULES else r["t"]
            others = [j for j in range(U) if j != k and passes_filter(d["c"][j], d["pc"][j], fbar)]
            others = sorted(others, key=lambda j: hkey("rsi2col-ctl", d["day"], d["syms"][j], r["t"]))[:5]
            cg = [ctl_trade(rule, d, j, r) for j in others]
            cg = [x for x in cg if np.isfinite(x)]
            counts["ctl_tried"] += len(others)
            counts["ctl_filled"] += len(cg)
            r["ctl"] = float(np.mean(cg)) if cg else np.nan
            r["ctl_n"] = len(cg)
            sl = same_name_later(rule, d, k, r)
            if sl is None:
                counts["later_past_close"] += 1
                sl = np.nan
            r["later"] = sl
            spy = (price_at(r["kind"], r["ebar"], d["spy_o"], d["spy_c"]) / px_or(d["spy_o"], r["ent_bar"]) - 1) * 1e4 \
                if rule not in STOP_RULES else (price_at(r["kind"], r["ebar"], d["spy_o"], d["spy_c"]) / px_or(d["spy_c"], r["t"] - 1) - 1) * 1e4
            r["spy"] = spy
            r["day"] = d["day"]
            rows.append(r)
    return rows, counts


def unconditional(rule, d, mode, specs, rng_seed):
    """Random filtered (name, minute) on the day, one per signal spec (hold bars, exit price type, stop distance) drawn
    from this day's signals. Bar-close rules buy o[t+1]; stop rules place the same-distance buy-stop at bar t with the
    same fill model (filter at close[t-1]). Exit price type matches the signal's (price_at). -> list of gross. Pure
    given the seed."""
    rng = np.random.default_rng(rng_seed)
    U = len(d["syms"])
    out = []
    stop = rule in STOP_RULES
    for hold, kind, dist in specs:
        for _ in range(20):
            k = int(rng.integers(0, U))
            t = int(rng.integers(T_LO, T_HI + 1))
            if passes_filter(d["c"][k], d["pc"][k], t - 1 if stop else t):
                break
        else:
            continue
        o, h, c = d["o"][k], d["h"][k], d["c"][k]
        if stop:
            cp = px_or(c, t - 1)
            if not (np.isfinite(cp) and dist is not None):
                continue
            P = math.ceil(round(cp * (1 + dist) * 100, 6)) / 100.0
            e, ent = stop_fill(P, px_or(o, t), px_or(h, t)), t
        else:
            e, ent = px_or(o, t + 1), t + 1
        x = ent + hold
        xp = price_at(kind, x, o, c) if x <= 389 else np.nan
        if np.isfinite(e) and np.isfinite(xp):
            out.append((xp / e - 1) * 1e4)
    return out


# ---------------------------------------------------------------- statistics (pure)

def day_means(rows, key):
    per = defaultdict(list)
    for r in rows:
        v = r[key]
        if v is not None and np.isfinite(v):
            per[r["day"]].append(v)
    return {k: float(np.mean(v)) for k, v in per.items()}


def day_t(rows, key):
    m = list(day_means(rows, key).values())
    if len(m) < 3 or np.std(m, ddof=1) == 0:
        return None
    return float(np.mean(m) / (np.std(m, ddof=1) / math.sqrt(len(m))))


def mde(rows, key):
    m = list(day_means(rows, key).values())
    return float(2.8 * np.std(m, ddof=1) / math.sqrt(len(m))) if len(m) >= 3 else None


def mean(rows, key):
    v = [r[key] for r in rows if r[key] is not None and np.isfinite(r[key])]
    return float(np.mean(v)) if v else None


def welch_t(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 3 or len(b) < 3:
        return None
    se = math.sqrt(a.var(ddof=1) / len(a) + b.var(ddof=1) / len(b))
    return float((a.mean() - b.mean()) / se) if se > 0 else None


def wick_pressure(o, h, l, c, bar, n=5):
    """(sum lower wicks - sum upper wicks) / sum ranges over the n completed bars before `bar` (amended_wick_split).
    None if a bar is missing or ranges sum to 0. Pure."""
    lo_, up_, rg = 0.0, 0.0, 0.0
    for i in range(bar - n, bar):
        if i < 0 or not all(np.isfinite(x[i]) for x in (o, h, l, c)):
            return None
        lo_ += min(o[i], c[i]) - l[i]
        up_ += h[i] - max(o[i], c[i])
        rg += h[i] - l[i]
    return (lo_ - up_) / rg if rg > 0 else None


def winsor(x, top=0.01):
    x = np.asarray(x, float)
    return float(np.minimum(x, np.quantile(x, 1 - top)).mean()) if len(x) else None


def r2(x):
    return None if x is None else round(x, 2)


def halves(rows):
    return [r for r in rows if r["day"] < SPLIT], [r for r in rows if r["day"] >= SPLIT]


def edge_verdict(rows):
    """EDGE on ALL candidates (gross): lift = gross - hold-matched same-minute control; > 0 in both halves and pooled
    day-clustered t >= 2.5 (amended_1 M1/C1). Pure."""
    for r in rows:
        r["lift"] = r["gross"] - r["ctl"] if np.isfinite(r.get("ctl", np.nan)) else np.nan
    a, b = halves(rows)
    la, lb, t = mean(a, "lift"), mean(b, "lift"), day_t(rows, "lift")
    ok = la is not None and lb is not None and la > 0 and lb > 0 and t is not None and t >= EDGE_T
    return ("EDGE" if ok else "NO EDGE"), {"lift_h1": r2(la), "lift_h2": r2(lb), "t": r2(t),
                                           "mde_h1": r2(mde(a, "lift")), "mde_h2": r2(mde(b, "lift"))}


# ---------------------------------------------------------------- build / load (I/O)

def build():
    global BUILD_SHA
    import subprocess
    BUILD_SHA = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import ALL_ETF, WH_SIP
    import news_source as ns
    CACHE.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in WH_SIP.glob("20??-??-??.npz") if p.stem <= CONFIRM[1])
    prev = None
    for fp in files:
        if fp.stem < SEARCH[0] or (CACHE / f"{fp.stem}.npz").exists() or prev is None:
            prev = fp
            continue
        day, pday = load_day(fp), load_day(prev)
        if day["idx"].get("SPY") is None:
            st = json.loads((OUT / "build_stats.json").read_text()) if (OUT / "build_stats.json").exists() else {}
            st[fp.stem] = {"skipped": "no SPY row"}
            (OUT / "build_stats.json").write_text(json.dumps(st))
            prev = fp
            continue
        ef = ETH / f"{fp.stem}.pkl"
        if not ef.exists():
            print(fp.stem, "no ETH file - skipped (run eth first)", flush=True)
            st = json.loads((OUT / "build_stats.json").read_text()) if (OUT / "build_stats.json").exists() else {}
            st[fp.stem] = {"skipped": "no ETH file"}
            (OUT / "build_stats.json").write_text(json.dumps(st))
            prev = fp
            continue
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _Z
        open_ts = _dt.fromisoformat(fp.stem + "T09:30").replace(tzinfo=_Z("America/New_York")).timestamp()
        # the fetch's end is inclusive: the 09:30 bar is RTH t=0 already, so keep extended bars strictly before it
        ext_day = {k: np.array([c for ts, c in v if ts < open_ts], float) for k, v in pickle.load(open(ef, "rb")).items()}
        n_noext = n_noprior = 0
        PN = {kk: [] for kk in PINE_KEYS}
        syms, O, H, L, C, PC, RSI, UU, DD, RP, GP, RS, GS = ([] for _ in range(13))
        for s in universe(day, pday, set(ALL_ETF)):
            i, j = day["idx"][s], pday["idx"].get(s)
            if j is None:
                n_noprior += 1
                continue
            pc = ns.last_close(pday["c"][j])
            if not pc > 0:
                n_noprior += 1
                continue
            warm = pday["c"][j][~np.isnan(pday["c"][j])][-WARM:]
            full = np.concatenate([np.pad(warm, (WARM - len(warm), 0), constant_values=np.nan), day["c"][i]])
            col = colours(full, WARM)
            if s not in ext_day:
                n_noext += 1                       # no extended-hours bars fetched for this name-day: dropped (counted)
                continue
            pine = pine_day(pday["c"][j], ext_day[s], day["c"][i])
            for kk in PINE_KEYS:
                PN[kk].append(pine[kk])
            syms.append(s)
            for lst, arr in ((O, day["o"][i]), (H, day["h"][i]), (L, day["l"][i]), (C, day["c"][i])):
                lst.append(arr)
            PC.append(pc)
            RSI.append(col["rsi"]); UU.append(col["U"]); DD.append(col["D"])
            RP.append(col["red_plain"]); GP.append(col["green_plain"]); RS.append(col["red_strict"]); GS.append(col["green_strict"])
        k = day["idx"]["SPY"]
        np.savez_compressed(CACHE / f"{fp.stem}.npz", syms=np.array(syms), o=np.array(O, np.float64), h=np.array(H),
                            l=np.array(L), c=np.array(C), pc=np.array(PC), rsi=np.array(RSI, np.float32),
                            U=np.array(UU), D=np.array(DD), red_plain=np.array(RP), green_plain=np.array(GP),
                            red_strict=np.array(RS), green_strict=np.array(GS), spy_o=day["o"][k], spy_c=day["c"][k],
                            build_sha=np.array(BUILD_SHA), **{kk: np.array(PN[kk]) for kk in PINE_KEYS})
        print(fp.stem, len(syms), "no_ext", n_noext, flush=True)
        stats = json.loads((OUT / "build_stats.json").read_text()) if (OUT / "build_stats.json").exists() else {}
        stats[fp.stem] = {"names": len(syms), "no_ext": n_noext, "no_prior": n_noprior, "sha": BUILD_SHA}
        (OUT / "build_stats.json").write_text(json.dumps(stats))
        prev = fp


def eth_fetch():
    """amended_3: SIP extended-hours 1-minute closes per universe name-day: prior session 16:00-20:00 and today
    04:00-09:29 ET. -> ai_reports/rsi2_colour/eth/DAY.pkl {sym: [(epoch, close)]}. Resumable; after 16:05 ET only. I/O."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    import time
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import ALL_ETF, WH_SIP
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment
    et = ZoneInfo("America/New_York")
    hm = datetime.now(et).strftime("%H%M")
    if datetime.now(et).weekday() < 5 and "0900" <= hm < "1605":
        sys.exit("market hours - rerun after 16:05 ET (shared data key)")
    k = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    ETH.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in WH_SIP.glob("20??-??-??.npz") if p.stem <= CONFIRM[1])
    failed = []
    for a, b in zip(files, files[1:]):
        if b.stem < SEARCH[0] or (ETH / f"{b.stem}.pkl").exists():
            continue
        day, pday = load_day(b), load_day(a)
        syms = sorted(universe(day, pday, set(ALL_ETF)))
        s0 = datetime.fromisoformat(a.stem + "T16:00").replace(tzinfo=et)
        s1 = datetime.fromisoformat(b.stem + "T09:30").replace(tzinfo=et)
        res = {}
        ok = True
        for i in range(0, len(syms), 100):
            got = None
            for attempt in range(6):
                try:
                    got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + 100], timeframe=TimeFrame.Minute,
                                                             start=s0, end=s1, feed=DataFeed.SIP, adjustment=Adjustment.RAW))
                    break
                except Exception as e:              # noqa: BLE001
                    time.sleep(60 if "429" in str(e) else 5 * (attempt + 1))
            if got is None:
                ok = False
                break
            for sym, rows in (got.data or {}).items():
                res[sym] = [(r.timestamp.timestamp(), float(r.close)) for r in rows]
        if not ok:
            failed.append(b.stem)
            print(b.stem, "FAILED", flush=True)
            continue
        for sym in syms:
            res.setdefault(sym, [])                 # fetched, no extended-hours trades: an empty list, not missing
        pickle.dump(res, open(ETH / f"{b.stem}.pkl", "wb"))
        print(b.stem, len(syms), sum(len(v) for v in res.values()), flush=True)
    print("ETH_DONE failed", failed, flush=True)


def load_days(lo, hi):
    for p in sorted(CACHE.glob("*.npz")):
        if lo <= p.stem <= hi:
            z = np.load(p)
            d = {k: z[k] for k in z.files}
            d["syms"] = [str(s) for s in z["syms"]]
            d["day"] = p.stem
            yield d


def chart(day="2026-11-19", names=(("FIGS", "15:05", "15:30"), ("COLD", "14:25", "15:00"))):
    """Per-bar RSI-2 and colours for the operator's screenshot names (amended_1 B2). Warehouse only, no API."""
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    files = sorted(p.stem for p in WH_SIP.glob("20??-??-??.npz"))
    pday = load_day(WH_SIP / f"{files[files.index(day) - 1]}.npz")
    dd = load_day(WH_SIP / f"{day}.npz")
    for s, a, b in names:
        i, j = dd["idx"][s], pday["idx"][s]
        warm = pday["c"][j][~np.isnan(pday["c"][j])][-WARM:]
        full = np.concatenate([np.pad(warm, (WARM - len(warm), 0), constant_values=np.nan), dd["c"][i]])
        col = colours(full, WARM)
        P = [red_trigger(dd["c"][i][t - 1], col["U"][t - 1], col["D"][t - 1]) if t else np.nan for t in range(390)]
        print(f"== {s} {day}  minute  close  rsi2  plain(R/G)  strict(R/G)  red-trigger-for-this-bar")
        for t in range(390):
            hm = f"{9 + (30 + t) // 60:02d}:{(30 + t) % 60:02d}"
            if a <= hm <= b:
                print(f"{hm}  {dd['c'][i][t]:.2f}  {col['rsi'][t]:6.1f}  "
                      f"{'R' if col['red_plain'][t] else ('G' if col['green_plain'][t] else '.')}  "
                      f"{'R' if col['red_strict'][t] else ('G' if col['green_strict'][t] else '.')}  {P[t]:.2f}")


# ---------------------------------------------------------------- quotes (I/O)

def market_hours_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    hm = datetime.now(ZoneInfo("America/New_York")).strftime("%H%M")
    return "0900" <= hm < "1605"


def qbar(rule, row):
    """Quote key bar: the 60 s before the entry moment. news_source keys t = 'before the open of t+1'."""
    return row["t"] - 1 if rule in STOP_RULES else row["t"]


def spread_lookup():
    import news_source as ns
    main = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    own = json.loads(OWNQ.read_text()) if OWNQ.exists() else {}

    def f(day, sym, t):
        k = ns.qkey(day, sym, t)
        return main[k] if k in main else (own[k] if k in own else "missing")
    return f


def fetch(keys, n=4):
    from multiprocessing import Process
    import news_source as ns
    if market_hours_now():
        print("market hours - stopping before fetch; rerun after 16:05 (quotes so far are kept)", flush=True)
        sys.exit(0)
    main = json.load(open(ns.QUOTES)) if ns.QUOTES.exists() else {}
    plan = [("rsi2col", d, s, t, 0.0) for d, s, t in keys]
    parts = [OUT / f"quotes_part{i}.json" for i in range(n)]
    procs = [Process(target=ns.fetch_quotes, args=(plan[i::n],), kwargs={"path": parts[i], "skip": set(main)})
             for i in range(n)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    own = json.loads(OWNQ.read_text()) if OWNQ.exists() else {}
    for p in parts:
        if p.exists():
            own.update(json.loads(p.read_text()))
    OWNQ.write_text(json.dumps(own))


def draw(rule, rows, cap=CAP_PER_HALF, days=None):
    """Hash-ordered walk, spread gate 15 bp, cap traded per half. -> (traded, missing keys). Pure given the lookup."""
    f = spread_lookup()
    traded, missing = [], []
    for half in halves([r for r in rows if days is None or r["day"] in days]):
        n_ok = 0
        for r in sorted(half, key=lambda r: hkey("rsi2col", rule, r["day"], r["sym"], r["t"])):
            if n_ok >= cap:
                break
            sp = f(r["day"], r["sym"], qbar(rule, r))
            if sp == "missing":
                missing.append((r["day"], r["sym"], qbar(rule, r)))
                n_ok += 1
                continue
            if sp is None or sp > GATE_BP:
                continue
            slip = STOP_SLIP_BP if rule in STOP_RULES else 0.0
            traded.append({**r, "spread": sp, "net": r["gross"] - sp, "stress": r["gross"] - STRESS * sp - slip})
            n_ok += 1
    return traded, missing


def draw_fetched(rule, rows, days=None, rounds=6):
    for _ in range(rounds):
        tr, miss = draw(rule, rows, days=days)
        print(rule, "traded", len(tr), "missing quotes", len(miss), flush=True)
        if not miss:
            return tr
        fetch(sorted(set(miss)))
    print("WARNING: quotes still missing", flush=True)
    return draw(rule, rows, days=days)[0]


# ---------------------------------------------------------------- run

def all_rows(rule, mode, lo, hi, t_lo=None, t_hi=None):
    rows, counts, unc = [], defaultdict(int), []
    n_days = defaultdict(int)
    for d in load_days(lo, hi):
        rr, cn = day_rows(rule, d, mode, t_lo, t_hi)
        rows += rr
        for k, v in cn.items():
            counts[k] += v
        specs = [(r["ebar"] - r["ent_bar"], r["kind"], r["dist"]) for r in rr][:200]
        unc += unconditional(rule, d, mode, specs, hkey("rsi2col-unc", rule, d["day"]) % 2 ** 32)
        half = "h1" if d["day"] < SPLIT else "h2"
        for k, v in cn.items():
            counts[f"{half}_{k}"] += v
        n_days[half] += 1
    counts["sessions_scored_h1"], counts["sessions_scored_h2"] = n_days["h1"], n_days["h2"]
    return rows, dict(counts), unc


def edge_report(rule, rows, counts, unc):
    v, st = edge_verdict(rows)
    a, b = halves(rows)
    ctl_m, unc_m = mean(rows, "ctl"), (float(np.mean(unc)) if unc else None)
    later = [r["gross"] - r["later"] for r in rows if r["later"] is not None and np.isfinite(r["later"])]
    rep = {"verdict": v, **st, "n": len(rows), "n_h1": len(a), "n_h2": len(b), "days": len({r["day"] for r in rows}),
           "gross": r2(mean(rows, "gross")), "gross_h1": r2(mean(a, "gross")), "gross_h2": r2(mean(b, "gross")),
           "control_mean": r2(ctl_m), "unconditional_mean": r2(unc_m),
           "control_vs_unconditional": r2(None if ctl_m is None or unc_m is None else ctl_m - unc_m),
           "same_name_later_lift": r2(float(np.mean(later)) if later else None),
           "spy_hedged_gross": r2(float(np.mean([r["gross"] - r["spy"] for r in rows if np.isfinite(r["spy"])])) if rows else None),
           "fixed_hold_5": r2(mean(rows, "fix5")), "fixed_hold_15": r2(mean(rows, "fix15")),
           "median_hold_bars": float(np.median([r["ebar"] - r["ent_bar"] for r in rows])) if rows else None,
           "win_pct": r2(100 * float(np.mean([r["gross"] > 0 for r in rows]))) if rows else None,
           "counts": counts}
    later_vals = [r["later"] for r in rows if r["later"] is not None and np.isfinite(r["later"])]
    ctl_vals = [r["ctl"] for r in rows if np.isfinite(r.get("ctl", np.nan))]
    rep["later_mean"] = r2(float(np.mean(later_vals)) if later_vals else None)
    rep["later_vs_unconditional"] = r2(rep["later_mean"] - unc_m if later_vals and unc_m is not None else None)
    rep["control_vs_unconditional_t"] = r2(welch_t(ctl_vals, unc))
    rep["later_vs_unconditional_t"] = r2(welch_t(later_vals, unc))
    rep["no_price_share"] = r2(counts.get("no_price", 0) / max(len(rows) + counts.get("no_price", 0), 1))
    # TEST_STANDARD mismatch (amended_1 m1, P2): either control far from the unconditional mean blocks the verdict
    for nm in ("control", "later"):
        dlt, tt = rep[f"{nm}_vs_unconditional"], rep[f"{nm}_vs_unconditional_t"]
        if dlt is not None and (abs(dlt) > 3.0 or (tt is not None and abs(tt) > 2.0)):
            rep["verdict"] = f"BLOCKED ({nm} vs unconditional mismatch: {dlt} bp, t {tt})"
            break
    if rule in STOP_RULES:
        rep["control_fill_rate"] = r2(counts.get("ctl_filled", 0) / counts["ctl_tried"]) if counts.get("ctl_tried") else None
        rep["thin_fill_share"] = r2(float(np.mean([r["thin"] for r in rows]))) if rows else None
    return rep


def wick_split(rows, key="wick"):
    """Lift by wick-pressure bucket (amended_wick_split, information). Pure."""
    out = {}
    for nm, f in (("selling<-0.2", lambda w: w < -0.2), ("neutral", lambda w: -0.2 <= w <= 0.2), ("buying>0.2", lambda w: w > 0.2)):
        rr = [r for r in rows if r.get(key) is not None and f(r[key])]
        out[nm] = {"n": len(rr), "lift": r2(mean(rr, "lift")) if rr else None, "gross": r2(mean(rr, "gross")) if rr else None}
    return out


def cross_vs_close(stop_rows, close_rows):
    """Cross-price fills matched to the bar-close signal on the same name-day and bar (amended_cross_price,
    information): mean gross difference on matched pairs, and the stop fills the bar-close rule never took
    (the colour reverted before the close). Pure."""
    by = {(r["day"], r["sym"], r["t"]): r for r in close_rows}
    pairs = [(r, by[(r["day"], r["sym"], r["t"])]) for r in stop_rows if (r["day"], r["sym"], r["t"]) in by]
    un = [r for r in stop_rows if (r["day"], r["sym"], r["t"]) not in by]
    return {"matched": len(pairs), "cross_minus_close_gross": r2(float(np.mean([a["gross"] - b["gross"] for a, b in pairs])) if pairs else None),
            "unmatched_fills": len(un), "unmatched_gross": r2(mean(un, "gross")) if un else None}


def profitable(rule, rows, edge_rep):
    from early_stop import gross_screen, screen, screen_days
    v, gst = gross_screen([{"day": r["day"], "gross": r["gross"]} for r in rows], pass_mean=MIN_NET)
    if v == "SKIP_QUOTES":
        return {"verdict": "FAIL-gross (no quotes fetched)", "gross_screen": gst}
    sdays = set(screen_days([r["day"] for r in rows]))
    first = draw_fetched(rule, rows, days=sdays)
    sv, sst = screen(first, key="net", pass_mean=MIN_NET)
    if sv == "STOP":
        return {"verdict": "FAIL-early (first-quarter screen)", "gross_screen": gst, "screen": sst}
    tr = draw_fetched(rule, rows)
    a, b = halves(tr)
    na, nb = mean(a, "net"), mean(b, "net")
    sa, sb = mean(a, "stress"), mean(b, "stress")
    wa = winsor([r["net"] for r in a]) if a else None
    wb = winsor([r["net"] for r in b]) if b else None
    ma, mb = mde(a, "net"), mde(b, "net")
    rep = {"n_h1": len(a), "n_h2": len(b), "net_h1": r2(na), "net_h2": r2(nb), "stress_h1": r2(sa), "stress_h2": r2(sb),
           "winsor_h1": r2(wa), "winsor_h2": r2(wb), "mde_h1": r2(ma), "mde_h2": r2(mb), "t_net": r2(day_t(tr, "net")),
           "median_spread": r2(float(np.median([r["spread"] for r in tr]))) if tr else None,
           "gross_screen": gst, "screen": sst}
    if len(a) < MIN_N or len(b) < MIN_N or (ma or 99) > MDE_MAX or (mb or 99) > MDE_MAX:
        rep["verdict"] = "UNDERPOWERED -> UNPROVEN"
    else:
        ok = all(x is not None and x >= MIN_NET for x in (na, nb)) and all(x is not None and x > 0 for x in (sa, sb, wa, wb))
        rep["verdict"] = "PROFITABLE" if ok else "NOT PROFITABLE"
    return rep



# ---------------------------------------------------------------- amended_2: exact Pine colours on the ETH series

def pine_rsi(closes):
    """Pine ta.rsi(close, 2) over bars that exist (NaN entries skipped): change -> up/down, rma(2) seeded with the
    simple mean of the first 2 values, then alpha 1/2; down == 0 -> 100, up == 0 -> 0. -> (rsi, up, down). Pure."""
    n = len(closes)
    rsi, U, D = np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan)
    prev, ups, downs, up, down = None, [], [], None, None
    for i in range(n):
        c = closes[i]
        if not np.isfinite(c):
            continue
        if prev is not None:
            d = c - prev
            g, l_ = max(d, 0.0), max(-d, 0.0)
            if up is None:
                ups.append(g); downs.append(l_)
                if len(ups) == 2:
                    up, down = sum(ups) / 2, sum(downs) / 2
            else:
                up, down = 0.5 * g + 0.5 * up, 0.5 * l_ + 0.5 * down
            if up is not None:
                U[i], D[i] = up, down
                rsi[i] = 100.0 if down == 0 else (0.0 if up == 0 else 100.0 - 100.0 / (1.0 + up / down))
        prev = c
    return rsi, U, D


def pine_colours(closes):
    """amended_2 colour per bar: 'R' red (close < SMA200, close > SMA5, RSI2 > 90), 'G' green (close > SMA200,
    close < SMA5, RSI2 < 10), '.' silver; all on the same bar series. Pure."""
    rsi, _, _ = pine_rsi(closes)
    s200, s5 = sma(closes, 200), sma(closes, 5)
    out = []
    for c, r, a, b in zip(closes, rsi, s200, s5):
        if not (np.isfinite(c) and np.isfinite(r) and np.isfinite(a) and np.isfinite(b)):
            out.append("?")
        elif r > 90 and c < a and c > b:
            out.append("R")
        elif r < 10 and c > a and c < b:
            out.append("G")
        else:
            out.append(".")
    return out, rsi


CHART_REFS = [   # (sym, day, colour, from, to) - operator screenshots (prereg amended_2, amended_references_3)
    ("UNH", "2026-11-20", "G", "06:3?", None), ("MU", "2026-11-20", "R", "07:10", "07:14"),
    ("MU", "2026-11-20", "R", "07:20", "07:21"), ("MU", "2026-11-20", "R", "07:25", "07:28"),
    ("ANT", "2026-11-20", "R", "08:43", "08:44"), ("ANT", "2026-11-20", "R", "08:48", "08:49"),
    ("ANT", "2026-11-20", "R", "08:56", "09:00"), ("ANT", "2026-11-20", "R", "09:03", "09:08"),
    ("INVH", "2026-11-20", "R", "14:37", "14:40"), ("INVH", "2026-11-20", "R", "14:44", "14:46"),
    ("INVH", "2026-11-20", "R", "14:53", "14:54"),
    ("ANT", "2026-11-20", "R", "12:46", "12:54"), ("ANT", "2026-11-20", "R", "12:55", "13:00"),
    ("ANT", "2026-11-20", "R", "13:04", "13:05"), ("ANT", "2026-11-20", "R", "13:16", "13:21"),
    ("ANT", "2026-11-20", "R", "13:24", "13:25"),
    ("SPCX", "2026-11-20", "G", "13:39", "13:41"), ("SPCX", "2026-11-20", "G", "13:57", "13:59"),
    ("SPCX", "2026-11-20", "G", "14:11", "14:12"), ("SPCX", "2026-11-20", "G", "14:21", "14:24"),
]
CHART_WINDOWS = {("FIGS", "2026-11-19"): ("15:05", "15:30"), ("COLD", "2026-11-19"): ("14:25", "15:00"),
                 ("UNH", "2026-11-20"): ("06:25", "06:50"), ("MU", "2026-11-20"): ("06:50", "07:35"),
                 ("ANT", "2026-11-20"): ("08:20", "13:30"), ("INVH", "2026-11-20"): ("14:30", "15:00"),
                 ("SPCX", "2026-11-20"): ("13:30", "14:30")}


def eth_bars(cl, sym, day):
    """SIP 1-minute bars 04:00-20:00 ET for the prior weekday and `day` (Pine's warm-up is all prior bars). -> list of
    (datetime ET, close). I/O."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed, Adjustment
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    d = datetime.fromisoformat(day + "T04:00").replace(tzinfo=ET)
    p = d - timedelta(days=3 if d.weekday() == 0 else 1)
    r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute, start=p,
                                           end=min(d + timedelta(hours=16), datetime.now(ET) - timedelta(minutes=16)), feed=DataFeed.SIP, adjustment=Adjustment.RAW))
    return [(b.timestamp.astimezone(ET), float(b.close)) for b in (r.data or {}).get(sym, [])]


def chart_eth():
    """amended_2 chart match: Pine colours on ETH SIP bars vs the operator's coloured minutes; prints per-bar colours
    in each screenshot window and the match share (>= 90% of the operator's coloured bars required)."""
    from alpaca.data.historical import StockHistoricalDataClient
    k = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    cols = {}
    for (sym, day), (a, b) in CHART_WINDOWS.items():
        bars = eth_bars(cl, sym, day)
        col, rsi = pine_colours(np.array([c for _, c in bars]))
        cols[(sym, day)] = {t.strftime("%H:%M"): (cc, r, c) for (t, c), cc, r in zip(bars, col, rsi)
                            if t.strftime("%Y-%m-%d") == day}
        print(f"== {sym} {day} {a}-{b}: " + " ".join(f"{hm}{v[0]}" for hm, v in sorted(cols[(sym, day)].items())
                                                       if a <= hm <= b))
    hit = tot = 0
    for sym, day, want, a, b in CHART_REFS:
        if b is None:
            continue
        got = [(hm, v[0], round(v[1], 1)) for hm, v in sorted(cols[(sym, day)].items()) if a <= hm <= b]
        h = sum(1 for _, c, _ in got if c == want)
        hit, tot = hit + h, tot + len(got)
        print(f"{sym} {day} {want} {a}-{b}: {h}/{len(got)}  {got}")
    print(f"MATCH {hit}/{tot} = {100 * hit / max(tot, 1):.0f}% (need >= 90%)")

def main(argv):
    cmd = argv[1]
    if cmd == "chart_eth":
        return chart_eth()
    if cmd == "chart":
        return chart()
    if cmd == "eth":
        return eth_fetch()
    if cmd == "red_screen":
        return red_screen()
    if cmd == "build":
        return build()
    mode = argv[argv.index("--colours") + 1] if "--colours" in argv else None
    if mode not in ("plain", "strict", "pine"):
        sys.exit("--colours plain|strict|pine is required (pine = amended_3)")
    if cmd == "run" and mode != "pine":
        sys.exit("run writes a verdict: only the registered mode (--colours pine, amended_3) is allowed")
    OUT.mkdir(parents=True, exist_ok=True)
    import subprocess
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    stats = json.loads((OUT / "build_stats.json").read_text()) if (OUT / "build_stats.json").exists() else {}
    out = {"mode": mode, "prereg": "docs/studies/rsi2_colour_timing_prereg.json (amended_3_sip_colours)", "sha": sha,
           "sessions": {"built": sum(1 for v in stats.values() if "names" in v),
                        "skipped": {k: v["skipped"] for k, v in stats.items() if "skipped" in v},
                        "name_days_no_ext": sum(v.get("no_ext", 0) for v in stats.values())},
           "rules": {}}
    rows_of = {}
    for rule in RULES:
        rows, counts, unc = all_rows(rule, mode, *SEARCH)
        rows_of[rule] = rows
        e = edge_report(rule, rows, counts, unc)
        out["rules"][rule] = {"EDGE": e}
        e["wick_split_5 (information)"] = wick_split(rows)
        e["wick_split_3 (information)"] = wick_split(rows, "wick3")
        o_rows, o_counts, o_unc = all_rows(rule, mode, *SEARCH, t_lo=1, t_hi=60)
        ov, ost = edge_verdict(o_rows)
        osl = {**ost, "n": len(o_rows), "gross": r2(mean(o_rows, "gross"))}
        if cmd == "run" and osl["gross"] is not None and osl["gross"] > 0:
            otr = draw_fetched(rule, o_rows)      # real SIP spread at those minutes (amended_opening_slice)
            osl.update({"traded": len(otr), "net": r2(mean(otr, "net")) if otr else None,
                        "spread": r2(mean(otr, "spread")) if otr else None})
        elif osl["gross"] is not None:
            osl["net"] = "gross <= 0, so net after the spread is lower still (no quotes fetched)"
        e["opening_slice_0930_1030 (information)"] = osl
        print(rule, json.dumps(e), flush=True)
        if cmd == "run":
            p = profitable(rule, rows, e)
            out["rules"][rule]["PROFITABLE"] = p
            out["rules"][rule]["PASS"] = e["verdict"] == "EDGE" and p["verdict"] == "PROFITABLE"
            print(rule, json.dumps(p), flush=True)
        (OUT / f"{cmd}_{mode}.json").write_text(json.dumps(out, indent=1, default=str))
    out["cross_vs_close (information)"] = {"RED_STOP vs RED_START": cross_vs_close(rows_of["RED_STOP"], rows_of["RED_START"]),
                                           "GREEN_CROSS vs GREEN_END": cross_vs_close(rows_of["GREEN_CROSS"], rows_of["GREEN_END"])}
    print("cross_vs_close", json.dumps(out["cross_vs_close (information)"]), flush=True)
    (OUT / f"{cmd}_{mode}.json").write_text(json.dumps(out, indent=1, default=str))
    if cmd == "run":
        for rule in RULES:   # CONFIRM is information only, gross edge (no quotes)
            rows, counts, unc = all_rows(rule, mode, *CONFIRM)
            out["rules"][rule]["confirm_info"] = edge_report(rule, rows, counts, unc)
        (OUT / f"{cmd}_{mode}.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({r: {k: (v.get("verdict") if isinstance(v, dict) else v) for k, v in x.items()}
                      for r, x in out["rules"].items()}, indent=1))


if __name__ == "__main__":
    main(sys.argv)
