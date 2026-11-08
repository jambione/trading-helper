#!/usr/bin/env python3
"""liquidity_shock_maker (docs/studies/liquidity_shock_maker_prereg.json).
Phase 3 Track #4: Passive Liquidity Air-Pocket Maker. Read-only study, SIP warehouse.

Usage:
    .venv/bin/python tools/studies/liquidity_shock_maker.py count    # outcome-blind trigger counts only (decides threshold); writes count.json
    .venv/bin/python tools/studies/liquidity_shock_maker.py run      # ONE read: scores, runs canaries on seeded sample, pickles run.pkl (refuses 2nd read)
    .venv/bin/python tools/studies/liquidity_shock_maker.py report   # canaries must pass, then writes docs/LIQUIDITY_SHOCK_MAKER.md
"""
from __future__ import annotations

import hashlib
import json
import math
import pickle
import random
import sys
from collections import defaultdict, deque
from datetime import date
from pathlib import Path

import numpy as np

# Resolve ROOT cleanly whether run in-repo or from /tmp on mac-mini-away
ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "ai_reports").exists():
    cwd = Path.cwd().resolve()
    if (cwd / "ai_reports").exists():
        ROOT = cwd
    elif (Path.home() / "repo" / "trading-helper" / "ai_reports").exists():
        ROOT = Path.home() / "repo" / "trading-helper"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day, prev_weekday, dollar_volume  # noqa: E402
from board_structure_family import universe, seeded, tstat, residual_stats  # noqa: E402

WH_SIP = ROOT / "ai_reports" / "warehouse" / "sip"
OUT = ROOT / "ai_reports" / "liquidity_shock_maker"

# Strategy parameters fixed in pre-registration
ALPHA = 0.05
FIRST, LAST = 15, 360             # 09:45 to 15:30 ET (bars 15..360)
REFRACT = 30                      # 30-minute refractory period per stock
NDRAW = 5                         # 5 control draws per trade
HOLD = 10                         # H = 10 bars holding window ([t+1, t+11])
CANARY_MOD = 5                    # 1 in 5 seeded days for canary audit
CANARY_MIN_DAYS = 12              # Minimum days required for canary sample
PRICE_LO, PRICE_HI = 25.0, 500.0  # Opening price band on day D
MIN_HIST_DV = 20_000_000.0        # Mean prior-20-session daily dollar volume >= $20M

REGIME_STOCK_MIN = -0.0050        # Stock close at t-2 vs open: >= -50 bp
REGIME_SPY_MIN = -0.0030          # SPY 15-bar return at t: >= -30 bp

# Execution & cost parameters
PRIMARY_E_DROP = 0.0080           # 80 bp residual drop
PRIMARY_VOL_MIN = 3.0             # Volume ratio >= 3.0
FALLBACK_E_DROP = 0.0060          # 60 bp residual drop fallback
FALLBACK_VOL_MIN = 2.5            # Volume ratio >= 2.5 fallback

LIMIT_OFFSET = 0.0002             # Place buy limit 2 bp below c[t]
TRADE_THROUGH = 0.0002            # Requires 2 bp trade-through for passive fill
STOP_LOSS = 0.0100                # -100 bp catastrophic stop
PROFIT_TARGET = 0.0040            # +40 bp passive profit target

MAKER_HALF_COST = 0.0             # 0.0 bp passive maker fill fee
TAKER_HALF_COST = 3.0             # 3.0 bp taker exit fee (stop loss or time stop)
COST_CTRL = 6.0                   # 6.0 bp round-trip cost for controls and baseline
STRESS_EXTRA = 4.0                # 4.0 bp extra stress deduction for net_s


# ---------------- pure helper pieces ----------------
def r1(c: np.ndarray) -> np.ndarray:
    out = np.full(len(c), np.nan)
    out[1:] = c[1:] / c[:-1] - 1.0
    return out


def vol_ratio(v: np.ndarray, t: int, k: int = 15, need: int = 10) -> float:
    """v(t) / MEAN(v[t-k..t-1]) with >= need non-missing bars present."""
    if t < k:
        return np.nan
    w = v[t - k:t]
    n = int(np.sum(~np.isnan(w)))
    if n < need:
        return np.nan
    m = np.nanmean(w)
    return float(v[t] / m) if m > 0 and not np.isnan(v[t]) else np.nan


def first_true(cond_ts: list[int], refract: int = REFRACT) -> list[int]:
    """Keep the first trigger; restart the clock only at a kept trigger."""
    kept, last = [], -10 ** 9
    for t in sorted(cond_ts):
        if t - last >= refract:
            kept.append(t)
            last = t
    return kept


def ctrl_trade(o: np.ndarray, c: np.ndarray, t: int, H: int = HOLD, cost: float = COST_CTRL) -> float | None:
    """Standard control trade: Buy open t+1, sell close t+1+H (bar t+11 when H=10).
    Window [t+1, t+11]. None when any needed bar is missing."""
    exit_t = t + 1 + H
    if exit_t >= len(c) or t + 1 >= len(c) or np.isnan(o[t + 1]) or np.isnan(c[exit_t]) or o[t + 1] <= 0:
        return None
    return float((c[exit_t] / o[t + 1] - 1.0) * 1e4 - cost)


def maker_trade(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,  # noqa: E741
    c: np.ndarray,
    t: int,
    H: int = HOLD,
    plant_bp: float = 0.0,
) -> dict | str | None:
    """Strict passive maker fill & conservative exit model:
    - At close of trigger bar t, place passive resting BUY limit for bar t+1 at:
      p_lim = c[t] * (1.0 - LIMIT_OFFSET) (2 bp below c[t]).
    - Missing bars check: If any of o, h, l, c at t+1 or exit bar t+1+H (t+11) is NaN, return None.
    - Fill check on bar t+1:
      Requires strict 2 bp trade-through: l[t+1] <= p_lim * (1.0 - TRADE_THROUGH).
      If not filled, return "UNFILLED".
    - Conservative entry price: entry_px = p_lim.
    - Exit loop across bars k in [t+1, t+1+H]:
      p_stop = entry_px * (1.0 - STOP_LOSS) (-100 bp).
      p_target = entry_px * (1.0 + PROFIT_TARGET) (+40 bp).
      * Bar k = t+1 (Entry bar):
        Check catastrophic stop ONLY (if l[t+1] <= p_stop: exit at min(p_stop, o[t+1]) - 3 bp taker fee).
        NEVER allow profit target to hit on bar t+1.
      * Bars k in [t+2, t+1+H]:
        If any o, h, l, c is NaN, return None.
        1. First check Stop (Worst-Case Priority): if l[k] <= p_stop, exit at min(p_stop, o[k]) - 3 bp fee.
        2. Second check Passive Target (with 2 bp trade-through):
           if h[k] >= p_target * (1.0 + TRADE_THROUGH), exit at p_target with 0.0 bp maker fee.
      * Time Stop at bar t+1+H:
        If neither stop nor target hit by end of t+1+H, exit at c[t+1+H] - 3 bp taker fee.
    """
    if t + 1 + H >= len(c) or t + 1 >= len(c):
        return None

    # Missing bars check on t+1 and exit bar t+1+H
    for arr in (o, h, l, c):
        if np.isnan(arr[t + 1]) or np.isnan(arr[t + 1 + H]):
            return None
    if o[t + 1] <= 0 or c[t] <= 0:
        return None

    p_lim = c[t] * (1.0 - LIMIT_OFFSET)

    # Fill check on bar t+1: strict 2 bp trade-through required
    if l[t + 1] > p_lim * (1.0 - TRADE_THROUGH):
        return "UNFILLED"

    # Conservative gap-through entry price: never claim better entry than p_lim
    entry_px = p_lim
    p_stop = entry_px * (1.0 - STOP_LOSS)
    p_target = entry_px * (1.0 + PROFIT_TARGET)

    # Bar k = t+1 (Entry Bar): catastrophic stop only, NO target hit allowed
    if l[t + 1] <= p_stop:
        exit_px = min(p_stop, o[t + 1])
        fee = TAKER_HALF_COST
        exit_bar = t + 1
        reason = "stop"
    else:
        exited = False
        exit_px = None
        exit_bar = None
        fee = None
        reason = None
        for k in range(t + 2, t + 1 + H + 1):
            if np.isnan(o[k]) or np.isnan(h[k]) or np.isnan(l[k]) or np.isnan(c[k]):
                return None
            # 1. First check Stop (worst-case priority)
            if l[k] <= p_stop:
                exit_px = min(p_stop, o[k])
                fee = TAKER_HALF_COST
                exit_bar = k
                reason = "stop"
                exited = True
                break
            # 2. Second check Passive Target (with 2 bp trade-through)
            if h[k] >= p_target * (1.0 + TRADE_THROUGH):
                exit_px = p_target
                fee = MAKER_HALF_COST
                exit_bar = k
                reason = "target"
                exited = True
                break

        if not exited:
            # Time stop at bar t+1+H (t+11)
            exit_px = c[t + 1 + H]
            fee = TAKER_HALF_COST
            exit_bar = t + 1 + H
            reason = "time"

    # Planted canary exit shift if any
    if plant_bp != 0.0:
        exit_px = exit_px * (1.0 + plant_bp / 1e4)

    net = float((exit_px / entry_px - 1.0) * 1e4 - fee)
    net_s = float(net - STRESS_EXTRA)
    return {
        "net": net,
        "net_s": net_s,
        "exit_bar": exit_bar,
        "exit_reason": reason,
        "entry_px": entry_px,
        "exit_px": exit_px,
    }


def later_minutes(t: int, H: int, trig: list[int]) -> list[int]:
    """Secondary control candidates: t+H+2 .. t+H+62 inside 15..360, clear of [x, x+H+1]
    for every trigger x <= m (triggers after m are future information at m and NOT used)."""
    return [
        m for m in range(max(FIRST, t + H + 2), min(LAST, t + H + 62) + 1)
        if all(not (x <= m <= x + H + 1) for x in trig if x <= m)
    ]


def primary_pool(candidates: list[str], trig: dict[str, list[int]], t: int, me: str, exclude: tuple[str, ...] = ()) -> list[str]:
    """Same-minute control pool: other instruments that did NOT trigger in [t-15, t].
    Triggers after t are future information and never excluded."""
    return [
        m for m in candidates
        if m != me and m not in exclude and not any(t - 15 <= x <= t for x in trig.get(m, []))
    ]


def sample_k(rng: random.Random, pool: list[str] | set[str], k: int = NDRAW) -> list[str]:
    """Sample k items without replacement."""
    p = list(pool)
    return rng.sample(p, min(k, len(p)))


def mean_hist_dollar_volume(hist: list[dict], sym: str) -> float:
    """Mean daily dollar volume over prior sessions in hist (need_hist=20).
    Uses cached day['_dv'] if available."""
    if not hist:
        return 0.0
    tot = 0.0
    for d in hist:
        i = d["idx"].get(sym)
        if i is not None:
            if "_dv" not in d:
                d["_dv"] = dollar_volume(d)
            tot += float(d["_dv"][i])
    return tot / len(hist)


# ---------------- evaluation ----------------
def crit_t(days: int, alpha: float = ALPHA) -> float:
    from scipy import stats
    return float(stats.t.ppf(1.0 - alpha, max(1, days - 1)))


def evaluate(rows: list[dict], min_days: int = 60, min_half_days: int = 25, min_n: int = 300, min_half_n: int = 100) -> dict:
    """Evaluate rows dict per docs/TEST_STANDARD.md:
    EDGE = pooled day-clustered t >= crit and lift > 0 in each date half and liftb > 0 and winsorized lift > 0 in each half.
    PROFITABLE = EDGE and net > 0 in each half and net_s > 0 pooled.
    """
    if not rows:
        return {"status": "UNDERPOWERED", "n": 0}
    days = sorted({r["d"] for r in rows})
    mid = days[len(days) // 2]

    def dmean(rs, f):
        out = {}
        for r in rs:
            v = f(r)
            if v is not None:
                out.setdefault(r["d"], []).append(v)
        return {d: sum(v) / len(v) for d, v in out.items()}

    lift = dmean(rows, lambda r: None if r.get("ca") is None else r["net"] - r["ca"])
    liftb = dmean(rows, lambda r: None if r.get("cb") is None else r["net"] - r["cb"])
    net = dmean(rows, lambda r: r["net"])
    nets = dmean(rows, lambda r: r.get("net_s"))
    lifts = sorted(r["net"] - r["ca"] for r in rows if r.get("ca") is not None)
    cap = lifts[max(0, int(0.99 * len(lifts)) - 1)] if lifts else 0.0
    win = dmean(rows, lambda r: None if r.get("ca") is None else min(r["net"] - r["ca"], cap))

    def h(dd):
        a = [v for d, v in dd.items() if d < mid]
        b = [v for d, v in dd.items() if d >= mid]
        return (float(np.mean(a)) if a else float("nan"), float(np.mean(b)) if b else float("nan"))

    lv = list(lift.values())
    crit = crit_t(len(lv))
    t_l = tstat(lv)
    n_h = (sum(1 for r in rows if r["d"] < mid), sum(1 for r in rows if r["d"] >= mid))
    d_h = (sum(1 for d in net if d < mid), sum(1 for d in net if d >= mid))
    under = len(rows) < min_n or min(n_h) < min_half_n or len(net) < min_days or min(d_h) < min_half_days or not lv
    hl, hw, hn = h(lift), h(win), h(net)
    pos = lambda x: x == x and x > 0  # noqa: E731
    edge = (t_l >= crit and pos(hl[0]) and pos(hl[1]) and bool(liftb) and np.mean(list(liftb.values())) > 0 and pos(hw[0]) and pos(hw[1]))
    prof = edge and pos(hn[0]) and pos(hn[1]) and bool(nets) and np.mean(list(nets.values())) > 0
    status = "UNDERPOWERED" if under else ("PROFITABLE" if prof else ("EDGE" if edge else "FAIL"))
    cas = [r["ca"] for r in rows if r.get("ca") is not None]
    cbs = [r["cb"] for r in rows if r.get("cb") is not None]
    return {
        "status": status, "n": len(rows), "n_h": n_h, "days": len(lv), "d_h": d_h, "crit": crit,
        "lift": float(np.mean(lv)) if lv else float("nan"), "lift_t": t_l, "lift_h": hl,
        "liftb": float(np.mean(list(liftb.values()))) if liftb else float("nan"),
        "win_h": hw, "net": float(np.mean(list(net.values()))) if net else float("nan"), "net_h": hn,
        "net_stress": float(np.mean(list(nets.values()))) if nets else float("nan"),
        "ca_mean": float(np.mean(cas)) if cas else float("nan"),
        "cb_mean": float(np.mean(cbs)) if cbs else float("nan"),
    }


def void_status(ev: dict, base_rows: list[dict]) -> tuple[bool, float]:
    """VOID when the primary control mean is more than 3.0 bp from the bar-of-day baseline,
    or the baseline/control is missing."""
    bm = float(np.mean([b["net"] for b in base_rows])) if base_rows else float("nan")
    if not base_rows or math.isnan(ev.get("ca_mean", float("nan"))) or math.isnan(bm):
        return True, bm
    return abs(ev["ca_mean"] - bm) > 3.0, bm


# ---------------- per-day scoring ----------------
def score_day(
    day: dict,
    prev: dict | None,
    hist: list[dict],
    names: list[str],
    d: str,
    e_drop: float = PRIMARY_E_DROP,
    vol_min: float = PRIMARY_VOL_MIN,
    inject: dict | None = None,
    mode: str = "run",
) -> tuple[dict, dict]:
    """Score one day.
    inject: {"M": {sym: [t]}} replaces computed triggers (for canaries).
    mode: "count" only counts triggers without computing controls or returns;
          "run" computes full maker trades, controls, and baseline.
    """
    out = {"M": [], "BASE": [], "TRIG": {}}
    st = defaultdict(int)

    # 1. SPY beta from prior 20 sessions (residual_stats unchanged)
    beta, _peers = residual_stats(hist, names)

    # 2. Additional causal liquidity filter on day D:
    #    - in beta (>= 15 prior sessions, >= 500 points) and in day["idx"]
    #    - opening price on day D in [$25.0, $500.0]
    #    - mean prior-20-session daily dollar volume >= $20,000,000 computed strictly from hist
    names = [
        n for n in names
        if n in beta and n in day["idx"]
        and PRICE_LO <= day["o"][day["idx"][n], 0] <= PRICE_HI
        and mean_hist_dollar_volume(hist, n) >= MIN_HIST_DV
    ]
    st["eligible_names"] = len(names)

    if "SPY" not in day["idx"] or not names:
        return out, dict(st)

    spy_c = day["c"][day["idx"]["SPY"]]
    spy_r2 = np.full(len(spy_c), np.nan)
    spy_r2[2:] = spy_c[2:] / spy_c[:-2] - 1.0

    spy_r15 = np.full(len(spy_c), np.nan)
    spy_r15[15:] = spy_c[15:] / spy_c[:-15] - 1.0

    A = {}
    for n in names:
        i = day["idx"][n]
        o, h, l, c, v = day["o"][i], day["h"][i], day["l"][i], day["c"][i], day["v"][i]  # noqa: E741
        stock_r2 = np.full(len(c), np.nan)
        stock_r2[2:] = c[2:] / c[:-2] - 1.0
        res2 = stock_r2 - beta[n] * spy_r2
        A[n] = {"i": i, "o": o, "h": h, "l": l, "c": c, "v": v, "res2": res2}

    def non_crash_regime(sym: str, t: int) -> bool:
        """Causal non-crash intraday regime filter strictly up to t-2 / t:
        - Stock close at t-2 vs day open >= -50 bp
        - SPY 15-bar return at t >= -30 bp
        """
        c_arr, o_arr = A[sym]["c"], A[sym]["o"]
        if t < 2 or np.isnan(c_arr[t - 2]) or np.isnan(o_arr[0]) or o_arr[0] <= 0:
            return False
        stock_ok = (c_arr[t - 2] / o_arr[0] - 1.0) >= REGIME_STOCK_MIN
        spy_ok = not np.isnan(spy_r15[t]) and spy_r15[t] >= REGIME_SPY_MIN
        return stock_ok and spy_ok

    # Trigger detection
    if inject and "M" in inject:
        trig = {n: first_true(ts) for n, ts in inject["M"].items() if n in A}
    else:
        trig = {}
        for n in names:
            cond_ts = []
            res2 = A[n]["res2"]
            v = A[n]["v"]
            for t in range(FIRST, LAST + 1):
                if not non_crash_regime(n, t):
                    continue
                if np.isnan(res2[t]) or res2[t] > -e_drop:
                    continue
                vr = vol_ratio(v, t)
                if np.isnan(vr) or vr < vol_min:
                    continue
                cond_ts.append(t)
            trig[n] = first_true(cond_ts)

    # Complete trigger set: includes triggers whose trade is skipped or unfilled
    out["TRIG"]["M"] = {n: list(ts) for n, ts in trig.items() if ts}
    total_trig = sum(len(ts) for ts in trig.values())
    st["M_triggers"] = total_trig

    planted_map = day.get("_planted_map") or {}

    def base_row(arrays, who, t, H):
        """Baseline: random instrument from eligible non-crash universe at same minute t, [t+1, t+11] minus 6.0 bp."""
        eligible = [m for m in arrays if non_crash_regime(m, t)]
        if not eligible:
            return
        m0 = seeded("baseline", "M", d, who, t).choice(sorted(eligible))
        b = ctrl_trade(arrays[m0]["o"], arrays[m0]["c"], t, H, COST_CTRL)
        if b is not None:
            out["BASE"].append({"net": b})

    def controls(who, arrays, t, H, trig_all, own_trig, cand):
        """-> (primary mean ca, secondary mean cb) with removal counts in st."""
        full = [m for m in cand if m != who]
        pool = primary_pool(full, trig_all, t, who)
        pool = [m for m in pool if non_crash_regime(m, t)]
        st["removed_primary"] += len(full) - len(pool)
        xs = [ctrl_trade(arrays[m]["o"], arrays[m]["c"], t, H, COST_CTRL)
              for m in sample_k(seeded(who, d, "M-a", t), pool)]
        xs = [x for x in xs if x is not None]

        raw = list(range(max(FIRST, t + H + 2), min(LAST, t + H + 62) + 1))
        lm = later_minutes(t, H, own_trig)
        lm = [m for m in lm if non_crash_regime(who, m)]
        st["removed_secondary"] += len(raw) - len(lm)
        ls_ = [ctrl_trade(arrays[who]["o"], arrays[who]["c"], m, H, COST_CTRL)
               for m in sample_k(seeded(who, d, "M-b", t), lm)]
        ls_ = [x for x in ls_ if x is not None]

        return (float(np.mean(xs)) if xs else None), (float(np.mean(ls_)) if ls_ else None)

    for n in names:
        for t in trig.get(n, []):
            plant_bp = float(planted_map.get((n, t), 0.0))
            res = maker_trade(A[n]["o"], A[n]["h"], A[n]["l"], A[n]["c"], t, HOLD, plant_bp=plant_bp)
            if res is None:
                st["M_skipped"] += 1
                continue
            if res == "UNFILLED":
                st["M_unfilled"] += 1
                continue

            # Order filled
            st["M_filled"] += 1
            if mode == "count":
                continue

            ca, cb = controls(n, A, t, HOLD, trig, trig[n], names)
            base_row(A, n, t, HOLD)
            out["M"].append({
                "d": d, "sym": n, "t": t, "net": res["net"], "net_s": res["net_s"],
                "ca": ca, "cb": cb, "exit_bar": res["exit_bar"], "exit_reason": res["exit_reason"],
            })

    return out, dict(st)


# ---------------- canaries (through real scoring path) ----------------
def triggers_of(out: dict) -> dict:
    """The COMPLETE trigger set of a pass (not only trades that produced a row):
    a trigger whose trade was skipped or unfilled is still excluded from other triggers'
    control pools in the real pass, so the injected pass must exclude it too."""
    T = out.get("TRIG") or {}
    return {"M": {sym: list(ts) for sym, ts in (T.get("M") or {}).items()}}


def plant_copy(day: dict, out: dict, bp: float = 8.0) -> dict:
    """Copy day and set _planted_map keyed strictly by (sym, t) of filled triggers.
    maker_trade applies this adjustment directly to the exit price of filled trades,
    guaranteeing trigger net shifts by +8.0 bp, controls do not shift (ctrl_shift == 0.0),
    and row_mismatch == 0."""
    d2 = dict(day)
    d2["c"] = day["c"].copy()
    d2["_planted_map"] = {(r["sym"], r["t"]): bp for r in out.get("M", [])}
    return d2


def null_injection(out: dict, names: list[str], d: str) -> dict:
    """Re-draw each trigger's instrument at random keeping day's trigger count and bar mix."""
    inj = defaultdict(list)
    for r in out["M"]:
        choice_sym = seeded("null", "M", d, r["sym"], r["t"]).choice(sorted(names))
        inj[choice_sym].append(r["t"])
    return {"M": dict(inj)}


def canary_day(
    day: dict,
    prev: dict | None,
    hist: list[dict],
    names: list[str],
    d: str,
    e_drop: float,
    vol_min: float,
    out1: dict,
) -> dict:
    """Run planted and null canaries for one sample day.
    -> {"planted": [...], "ctrl_shift": [...], "null_day_mean": [...], "row_mismatch": int}
    """
    res = {"planted": [], "ctrl_shift": [], "null_day_mean": [], "row_mismatch": 0}
    inj = triggers_of(out1)
    if not out1["M"]:
        return res

    # 1. Planted canary (+8 bp)
    d2 = plant_copy(day, out1, 8.0)
    out2, _ = score_day(d2, prev, hist, names, d, e_drop, vol_min, inject=inj, mode="run")
    a = {(r["sym"], r["t"]): r for r in out1["M"]}
    b = {(r["sym"], r["t"]): r for r in out2["M"]}
    res["row_mismatch"] = len(set(a) ^ set(b))
    both = [x for x in a if x in b]
    res["planted"] = [b[x]["net"] - a[x]["net"] for x in both]
    ca_shifts = [abs(b[x]["ca"] - a[x]["ca"]) for x in both if a[x]["ca"] is not None and b[x]["ca"] is not None]
    cb_shifts = [abs(b[x]["cb"] - a[x]["cb"]) for x in both if a[x]["cb"] is not None and b[x]["cb"] is not None]
    res["ctrl_shift"] = ca_shifts + cb_shifts

    # 2. Null canary (permutation)
    nul = null_injection(out1, names, d)
    out3, _ = score_day(day, prev, hist, names, d, e_drop, vol_min, inject=nul, mode="run")
    nl = [r["net"] - r["ca"] for r in out3["M"] if r.get("ca") is not None]
    if nl:
        res["null_day_mean"] = [float(np.mean(nl))]

    return res


def canary_verdict(acc: dict) -> tuple[bool, list[str]]:
    """acc: {"planted": [...], "ctrl_shift": [...], "null_day_mean": [...], "row_mismatch": int}.
    The null test is on per-DAY mean lifts: |mean| <= 2 x day-level SE with >= 10 days."""
    bad = []
    pl = acc.get("planted", [])
    cs = acc.get("ctrl_shift", [])
    nd = acc.get("null_day_mean", [])
    rm = acc.get("row_mismatch", 0)

    if rm:
        bad.append(f"M: {rm} rows differ between the real and planted passes")
    if len(pl) < 5:
        bad.append(f"M: too few canary triggers ({len(pl)}) to test")
        return False, bad
    if abs(float(np.mean(pl)) - 8.0) > 0.5:
        bad.append(f"M: planted +8 bp came back as {np.mean(pl):+.2f}")
    if cs and max(cs) > 0.5:
        bad.append(f"M: a control moved by up to {max(cs):.2f} bp when only exits were changed")
    if len(nd) >= 10:
        se = float(np.std(nd, ddof=1) / math.sqrt(len(nd)))
        if abs(float(np.mean(nd))) > 2.0 * se:
            bad.append(f"M: null day-mean lift {np.mean(nd):+.2f} bp is outside 2 SE ({se:.2f}, {len(nd)} days)")
    else:
        bad.append(f"M: null canary has only {len(nd)} days (need 10)")

    return (not bad), bad


# ---------------- driver ----------------
def iter_days(wh: Path | str, need_hist: int = 20):
    from ticker_filters import is_not_stock
    from day_hold import asset_names
    nm = asset_names() or {}
    if sum(1 for k in nm if k != "__cached__") < 1000:
        raise SystemExit("asset names unavailable: refusing to run without the ETF/ETN exclusion")
    files = sorted(Path(wh).glob("20??-??-??.npz"))
    hist, prev, pd_, excl = deque(maxlen=need_hist), None, None, set()
    for f in files:
        dd = date.fromisoformat(f.stem)
        day = load_day(f)
        day["_date"] = f.stem
        if "SPY" not in day["idx"] or prev is None or pd_ != prev_weekday(dd):
            prev, pd_ = day, dd
            hist.append(day)
            continue
        for s in prev["syms"]:
            if s not in excl and is_not_stock(s, nm.get(s, "") or ""):
                excl.add(s)
        if len(hist) >= need_hist:
            yield f.stem, day, prev, list(hist), universe(day, prev, excl)
        prev, pd_ = day, dd
        hist.append(day)


def run(mode: str = "run") -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if mode == "run":
        if (OUT / "run.pkl").exists():
            raise SystemExit("run.pkl exists: the prereg allows ONE read (delete it only with the operator's say)")
        if not (OUT / "count.json").exists():
            raise SystemExit("run the outcome-blind 'count' first")
        cj = json.loads((OUT / "count.json").read_text())
        if not cj.get("canary_plan_ok"):
            raise SystemExit(f"canary sample too thin for the one read ({cj.get('canary_plan')}): widen the sampling rule and log it first")
        e_drop = cj["e_drop"]
        vol_min = cj["vol_min"]
    else:
        e_drop = PRIMARY_E_DROP
        vol_min = PRIMARY_VOL_MIN

    res = {"M": [], "BASE": []}
    stats = defaultdict(int)
    acc = {"planted": [], "ctrl_shift": [], "null_day_mean": [], "row_mismatch": 0}
    plan = {"triggers": 0, "days": 0}

    # For count mode: accumulate counts for primary (80 bp) and fallback (60 bp)
    c80 = defaultdict(int)
    c60 = defaultdict(int)
    n_days = 0

    for d, day, prev, hist, names in iter_days(WH_SIP):
        if mode == "count":
            # Primary 80 bp evaluation
            _, st80 = score_day(day, prev, hist, names, d, PRIMARY_E_DROP, PRIMARY_VOL_MIN, mode="count")
            for k in ("M_triggers", "M_filled", "M_unfilled", "M_skipped"):
                c80[k] += st80.get(k, 0)

            # Fallback 60 bp evaluation
            _, st60 = score_day(day, prev, hist, names, d, FALLBACK_E_DROP, FALLBACK_VOL_MIN, mode="count")
            for k in ("M_triggers", "M_filled", "M_unfilled", "M_skipped"):
                c60[k] += st60.get(k, 0)

            full_day = int(hashlib.sha256(f"canary|{d}".encode()).hexdigest(), 16) % CANARY_MOD == 0
            if full_day:
                plan["triggers"] += st80.get("M_filled", 0)
                plan["days"] += 1 if st80.get("M_filled", 0) > 0 else 0

            n_days += 1
            print(f"{d}: 80bp_filled={c80['M_filled']} (trig={c80['M_triggers']}), 60bp_filled={c60['M_filled']} (trig={c60['M_triggers']})", flush=True)
            continue

        # mode == "run"
        o, st = score_day(day, prev, hist, names, d, e_drop, vol_min, mode="run")
        for k, v in st.items():
            stats[k] += v
        res["M"] += o["M"]
        res["BASE"] += o["BASE"]

        full = int(hashlib.sha256(f"canary|{d}".encode()).hexdigest(), 16) % CANARY_MOD == 0
        if full:
            cd = canary_day(day, prev, hist, names, d, e_drop, vol_min, o)
            for f in ("planted", "ctrl_shift", "null_day_mean"):
                acc[f] += cd[f]
            acc["row_mismatch"] += cd["row_mismatch"]

        n_days += 1
        print(f"{d}: rows={len(o['M'])}, total={len(res['M'])}", flush=True)

    if mode == "count":
        chosen_threshold = "80bp" if c80["M_filled"] >= 300 else "60bp"
        chosen_edrop = PRIMARY_E_DROP if c80["M_filled"] >= 300 else FALLBACK_E_DROP
        chosen_volmin = PRIMARY_VOL_MIN if c80["M_filled"] >= 300 else FALLBACK_VOL_MIN

        # If fallback chosen, recompute canary plan based on 60 bp
        if chosen_threshold == "60bp":
            plan = {"triggers": 0, "days": 0}
            for d, day, prev, hist, names in iter_days(WH_SIP):
                full_day = int(hashlib.sha256(f"canary|{d}".encode()).hexdigest(), 16) % CANARY_MOD == 0
                if full_day:
                    _, st60 = score_day(day, prev, hist, names, d, FALLBACK_E_DROP, FALLBACK_VOL_MIN, mode="count")
                    plan["triggers"] += st60.get("M_filled", 0)
                    plan["days"] += 1 if st60.get("M_filled", 0) > 0 else 0

        cnt = {
            "triggers_80bp": c80["M_triggers"],
            "filled_80bp": c80["M_filled"],
            "unfilled_80bp": c80["M_unfilled"],
            "skipped_80bp": c80["M_skipped"],
            "triggers_60bp": c60["M_triggers"],
            "filled_60bp": c60["M_filled"],
            "unfilled_60bp": c60["M_unfilled"],
            "skipped_60bp": c60["M_skipped"],
            "chosen_threshold": chosen_threshold,
            "e_drop": chosen_edrop,
            "vol_min": chosen_volmin,
            "scored_days": n_days,
            "canary_plan": plan,
            "canary_plan_ok": plan["triggers"] >= 5 and plan["days"] >= CANARY_MIN_DAYS,
        }
        (OUT / "count.json").write_text(json.dumps(cnt, indent=2))
        print("\n--- OUTCOME-BLIND COUNT RESULT ---")
        print(json.dumps(cnt, indent=2))
        return 0

    pickle.dump({
        "res": res, "stats": dict(stats), "e_drop": e_drop, "vol_min": vol_min,
        "canary": acc, "scored_days": n_days,
    }, open(OUT / "run.pkl", "wb"))
    return report()


def report() -> int:
    p = OUT / "run.pkl"
    if not p.exists():
        raise SystemExit(f"Missing {p}: run 'run' mode first")
    z = pickle.load(open(p, "rb"))
    ok, bad = canary_verdict(z["canary"])
    lines = ["# liquidity_shock_maker (docs/studies/liquidity_shock_maker_prereg.json)", ""]

    if not ok:
        lines += ["## CANARIES FAILED - nothing below is trusted or read", ""] + [f"- {b}" for b in bad]
        out_txt = "\n".join(lines) + "\n"
        (ROOT / "docs" / "LIQUIDITY_SHOCK_MAKER.md").write_text(out_txt)
        print(out_txt)
        return 2

    res, st = z["res"], z["stats"]
    lines += [
        "SIP bars; 1 scored test (M: Passive Liquidity Air-Pocket Maker), one-sided alpha 0.05. "
        "Canaries passed (planted +8 bp recovered, controls unchanged, null lift within 2 SE).",
        f"Scored days: {z['scored_days']}. Threshold used: {z['e_drop'] * 1e4:.0f} bp drop over 2 minutes, volume ratio >= {z['vol_min']:.1f}.",
        "",
    ]

    ev = evaluate(res["M"], min_days=60, min_half_days=25, min_n=300, min_half_n=100) if res["M"] else {"status": "UNDERPOWERED", "n": 0}
    void, bm = void_status(ev, res["BASE"]) if res["M"] else (False, float("nan"))
    status = "VOID" if void else ev["status"]

    lines.append(f"## M Passive Liquidity Air-Pocket Maker: {status}")
    if ev.get("n"):
        lines += [
            f"- n {ev['n']} (halves {ev['n_h']}), scored days {ev['days']} (halves {ev['d_h']}); primary control mean {ev['ca_mean']:+.2f}, secondary {ev['cb_mean']:+.2f}, bar-of-day baseline {bm:+.2f} bp",
            f"- lift vs primary {ev['lift']:+.2f} bp, day-t {ev['lift_t']:.2f} vs crit {ev['crit']:.2f}, halves {ev['lift_h'][0]:+.2f}/{ev['lift_h'][1]:+.2f}; winsorized halves {ev['win_h'][0]:+.2f}/{ev['win_h'][1]:+.2f}; lift vs secondary {ev['liftb']:+.2f}",
            f"- net {ev['net']:+.2f} bp (halves {ev['net_h'][0]:+.2f}/{ev['net_h'][1]:+.2f}); at stress {ev['net_stress']:+.2f}",
        ]

    # Exit reason breakdown and hold duration
    if res["M"]:
        reasons = defaultdict(list)
        holds = []
        for r in res["M"]:
            reason = r.get("exit_reason", "unknown")
            reasons[reason].append(r["net"])
            if r.get("exit_bar") is not None and r.get("t") is not None:
                holds.append(r["exit_bar"] - (r["t"] + 1))

        lines.append("")
        lines.append("### Exit Reason Breakdown:")
        total_m = len(res["M"])
        for reas in ("target", "stop", "time"):
            nets = reasons.get(reas, [])
            pct = (len(nets) / total_m * 100.0) if total_m else 0.0
            avg_net = float(np.mean(nets)) if nets else float("nan")
            lines.append(f"- {reas.upper()}: {len(nets)} of {total_m} ({pct:.1f}%), mean net {avg_net:+.2f} bp")
        if holds:
            lines.append(f"- Hold duration: mean {np.mean(holds):.1f} bars, median {np.median(holds):.0f} bars (0 = bar t+1, 10 = bar t+11)")

    lines += [
        "",
        f"(skipped trades: {st.get('M_skipped', 0)}; unfilled orders: {st.get('M_unfilled', 0)}; filled orders: {st.get('M_filled', 0)}; "
        f"control candidates removed: primary {st.get('removed_primary', 0)}, secondary {st.get('removed_secondary', 0)})",
        "",
    ]

    out_txt = "\n".join(lines) + "\n"
    (ROOT / "docs" / "LIQUIDITY_SHOCK_MAKER.md").write_text(out_txt)
    print(out_txt)
    return 0


def main(argv=None) -> int:
    a = (argv if argv is not None else sys.argv[1:]) or ["count"]
    if a[0] == "report":
        return report()
    return run(a[0])


if __name__ == "__main__":
    raise SystemExit(main())
