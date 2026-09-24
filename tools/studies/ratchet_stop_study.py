#!/usr/bin/env python3
"""How effective is the local_trail / peak-give ratchet?

Offline: outcomes.jsonl + runway_bars_cache.pkl. No Alpaca/Finnhub unless
--fetch (forbidden while the desk is open).

Versions (by trade date, from git history):
  era_pre918     : before 2026-09-18 — arm_r≈0.25, give_r 0.35, no peak_give
  era_arm015     : 2026-09-18..22   — arm_r 0.15 (125adee)
  era_arm006     : 2026-09-23 am    — arm_r 0.06 (+0.3%) (dc5bcce)
  era_peak035    : 2026-09-23 pm+   — peak_give_pct 0.35 (d28857b), no_progress off

Counterfactual on 1m bars from entry minute+1 through 15:50 ET:
  actual          — recorded exit (for regret only)
  stop_lob_eod    — initial stop, else leave-OB (%R was ≥ -20 then < -20), else 15:50
  stop_eod        — initial stop else 15:50
  ratchet(arm, give_pct|give_r, peak) — arm after +arm_r MFE; stop=max(prev, last-give, peak*(1-peak_pct/100))
  be_then_lob     — after +0.35R move stop to breakeven; then leave-OB or stop or EOD
"""
from __future__ import annotations

import json
import math
import os
import pickle
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import runway_study as rs  # noqa: E402

ET = ZoneInfo("America/New_York")
CACHE = os.path.join(ROOT, "ai_reports", "runway_bars_cache.pkl")
OUTCOMES = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")
COST_RT = 0.002  # 0.20% round trip in fraction
OB_LEVEL = -20.0  # Williams %R overbought threshold (approx)


def era_of(day: str) -> str:
    if day < "2026-09-18":
        return "era_pre918"
    if day < "2026-09-23":
        return "era_arm015"
    # 23rd: peak_give shipped 16:20; treat whole day as peak for simplicity
    # after midday — use peak for >=23
    return "era_peak035"


def wr_fast(h, l, c, length=21, span=7.0):
    raw = []
    for i in range(len(c)):
        if i + 1 < length:
            raw.append(None)
            continue
        hh = max(h[i - length + 1:i + 1])
        ll = min(l[i - length + 1:i + 1])
        span_px = hh - ll
        raw.append(None if span_px <= 0 else -100.0 * (hh - c[i]) / span_px)
    vals = [v for v in raw if v is not None]
    if not vals:
        return [None] * len(c)
    a = 2.0 / (span + 1.0)
    sm = [vals[0]]
    for v in vals[1:]:
        sm.append(a * v + (1 - a) * sm[-1])
    out = [None] * len(c)
    j = 0
    for i, v in enumerate(raw):
        if v is None:
            continue
        out[i] = sm[j]
        j += 1
    return out


def load_fills(cache):
    fills = []
    with open(OUTCOMES) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            sym = str(r.get("symbol") or "")
            et = r.get("entry_time")
            px = r.get("entry_price")
            if not rs.SYM_RE.match(sym) or not isinstance(et, (int, float)) or not px:
                continue
            day = datetime.fromtimestamp(float(et), ET).date().isoformat()
            if day < "2026-09-01" or day > "2026-09-24":
                continue
            B = cache.get((sym, day))
            if not B:
                continue
            i0 = rs.bars.index_at(B[0], et) if hasattr(rs, "bars") else None
            # runway_study uses bars module
            import bars
            i0 = bars.index_at(B[0], et)
            if i0 < 0 or i0 + 2 >= len(B[0]):
                continue
            ep = float(px)
            sp = float(r.get("stop_price") or 0) or ep * 0.95
            if sp >= ep:
                sp = ep * 0.95
            risk = ep - sp
            fills.append({
                "r": r, "B": B, "i0": i0, "px": ep, "stop": sp, "risk": risk,
                "day": day, "sym": sym, "era": era_of(day),
                "reason": str(r.get("close_reason") or "?"),
                "exit_px": float(r.get("exit_price") or 0) or None,
                "qty": float(r.get("total_qty") or 1) or 1,
                "mfe_r": float(r.get("mfe_r") or 0),
                "mae_r": float(r.get("mae_r") or 0),
                "pl": float(r.get("realized_pl_usd") or 0),
                "R": float(r.get("realized_r_multiple") or 0),
                "hold": float(r.get("hold_sec") or 0),
            })
    return fills


def eod_index(B):
    t = B[0]
    # 15:50 ET
    for i in range(len(t) - 1, -1, -1):
        dt = datetime.fromtimestamp(t[i], ET)
        if dt.hour * 60 + dt.minute <= 15 * 60 + 50:
            return i
    return len(t) - 1


def simulate(B, i0, entry, stop0, risk, *,
             arm_r=None, give_r=None, give_pct=None, peak_give_pct=None,
             use_lob=False, be_after_r=None, eod=True):
    """Return dict exit_i, exit_px, reason, mfe_r, mae_r, hold_bars, path_peak."""
    t, o, h, l, c, v = B
    i_end = eod_index(B) if eod else len(t) - 1
    stop = float(stop0)
    peak = float(entry)
    armed = arm_r is None or arm_r <= 0
    saw_ob = False
    mfe_r = 0.0
    mae_r = 0.0
    be_on = False
    wr = wr_fast(h, l, c) if use_lob or be_after_r is not None else None

    for i in range(i0 + 1, i_end + 1):
        # conservative: test low against prior stop, then raise on this bar's close/high
        if l[i] <= stop:
            # gap through: exit at open if open < stop else stop
            xpx = o[i] if o[i] < stop else stop
            mfe_r = max(mfe_r, (h[i] - entry) / risk if risk else 0)
            mae_r = min(mae_r, (l[i] - entry) / risk if risk else 0)
            return {
                "i": i, "px": xpx, "why": "stop",
                "mfe_r": mfe_r, "mae_r": mae_r,
                "hold_sec": t[i] - t[i0], "peak": peak,
            }
        peak = max(peak, h[i])
        mfe_r = max(mfe_r, (peak - entry) / risk if risk else 0)
        mae_r = min(mae_r, (l[i] - entry) / risk if risk else 0)

        if be_after_r is not None and not be_on and mfe_r + 1e-9 >= be_after_r:
            be_on = True
            stop = max(stop, entry)  # breakeven

        if arm_r is not None and not armed and mfe_r + 1e-9 >= arm_r:
            armed = True

        if armed and (give_r is not None or give_pct is not None or peak_give_pct):
            last = c[i]
            cand = stop
            if give_pct is not None:
                cand = max(cand, last * (1 - give_pct / 100.0))
            if give_r is not None and risk > 0:
                cand = max(cand, last - give_r * risk)
            if peak_give_pct:
                cand = max(cand, peak * (1 - peak_give_pct / 100.0))
            # never above last
            if cand >= last:
                cand = last - 0.01
            stop = max(stop, cand)

        if use_lob and wr is not None and wr[i] is not None:
            if wr[i] >= OB_LEVEL:
                saw_ob = True
            elif saw_ob and wr[i] < OB_LEVEL:
                return {
                    "i": i, "px": c[i], "why": "left_overbought",
                    "mfe_r": mfe_r, "mae_r": mae_r,
                    "hold_sec": t[i] - t[i0], "peak": peak,
                }

    # EOD
    i = i_end
    return {
        "i": i, "px": c[i], "why": "eod",
        "mfe_r": mfe_r, "mae_r": mae_r,
        "hold_sec": t[i] - t[i0], "peak": peak,
    }


def pl_r(entry, exit_px, risk, qty):
    if risk <= 0:
        return 0.0, 0.0
    # subtract half RT cost each way approx on price
    net = (exit_px / entry - 1) - COST_RT
    dollars = net * entry * qty
    R = (exit_px - entry) / risk - COST_RT * entry / risk
    return dollars, R


def capture(mfe_r, R):
    if mfe_r is None or mfe_r <= 0.01:
        return None
    return R / mfe_r


def summarize(rows):
    if not rows:
        return {"n": 0}
    pls = [r["pl"] for r in rows]
    Rs = [r["R"] for r in rows]
    wins = sum(1 for r in rows if r["R"] > 0)
    caps = [r["cap"] for r in rows if r.get("cap") is not None]
    holds = [r["hold"] for r in rows]
    mdds = [r["mae_r"] for r in rows]
    return {
        "n": len(rows),
        "pl": round(sum(pls), 2),
        "R_sum": round(sum(Rs), 2),
        "R_mean": round(statistics.mean(Rs), 3),
        "win": round(wins / len(rows), 3),
        "cap_mean": round(statistics.mean(caps), 3) if caps else None,
        "hold_mean": round(statistics.mean(holds), 1),
        "mae_mean": round(statistics.mean(mdds), 3),
        "saved_n": sum(1 for r in rows if r.get("saved")),
        "saved_pl": round(sum(r["pl"] for r in rows if r.get("saved")), 2),
        "cut_n": sum(1 for r in rows if r.get("cut")),
        "cut_pl": round(sum(r.get("cut_miss_pl") or 0 for r in rows if r.get("cut")), 2),
    }


def tag_saved_cut(sim, B, i0, entry, stop0, risk, qty):
    """saved = ratchet/stop exit, and later path would have hit initial stop
    before recovering; cut = after exit, price reached +0.35R above exit
    before hitting initial stop."""
    if sim["why"] not in ("stop", "ratchet", "peak"):
        # classify trail exits as stop in our sim when shelf hit
        pass
    t, o, h, l, c, v = B
    i_x = sim["i"]
    xpx = sim["px"]
    # look ahead after exit
    hit_init = False
    ran = False
    target = xpx + 0.35 * risk
    for i in range(i_x + 1, eod_index(B) + 1):
        if l[i] <= stop0:
            hit_init = True
            break
        if h[i] >= target:
            ran = True
            break
    saved = sim["why"] in ("stop",) and hit_init is False and False  # redefine below
    # Better definitions per Jonathan:
    # saved: exit was ratchet/shelf AND later would have hit initial stop0
    # cut short: exit was ratchet/shelf AND later reached +0.35R above exit before stop0
    is_trail = sim["why"] in ("stop",) and sim.get("armed_exit")
    # We mark armed_exit when trail was armed at hit
    if not sim.get("armed_exit") and sim["why"] != "stop":
        return False, False, 0.0
    # For stop hits after arming = trail exit
    trail_exit = bool(sim.get("armed_exit"))
    if not trail_exit:
        return False, False, 0.0
    saved = hit_init  # avoided later stop
    cut = ran and not hit_init
    miss = 0.0
    if cut:
        # missed dollars approx to target
        miss = (target - xpx) * qty
    return saved, cut, miss


def run_variant(fills, name, sim_fn):
    rows = []
    for f in fills:
        sim = sim_fn(f)
        if not sim:
            continue
        pl, R = pl_r(f["px"], sim["px"], f["risk"], f["qty"])
        # path MFE for capture uses sim mfe
        cap = capture(sim["mfe_r"], R)
        saved, cut, miss = False, False, 0.0
        if sim.get("armed_exit"):
            t, o, h, l, c, v = f["B"]
            stop0 = f["stop"]
            hit_init = False
            ran = False
            target = sim["px"] + 0.35 * f["risk"]
            for i in range(sim["i"] + 1, eod_index(f["B"]) + 1):
                if l[i] <= stop0:
                    hit_init = True
                    break
                if h[i] >= target:
                    ran = True
                    break
            saved = hit_init
            cut = ran and not hit_init
            if cut:
                miss = (target - sim["px"]) * f["qty"]
        rows.append({
            "pl": pl, "R": R, "cap": cap, "hold": sim["hold_sec"],
            "mae_r": sim["mae_r"], "mfe_r": sim["mfe_r"],
            "why": sim["why"], "day": f["day"], "era": f["era"],
            "reason_live": f["reason"], "saved": saved, "cut": cut,
            "cut_miss_pl": miss, "armed": sim.get("armed_exit", False),
        })
    return rows


def main():
    import bars  # noqa
    rs.bars = bars
    cache = pickle.load(open(CACHE, "rb"))
    fills = load_fills(cache)
    print(f"fills with bars: {len(fills)}", file=sys.stderr)
    by_era = defaultdict(int)
    by_reason = defaultdict(int)
    for f in fills:
        by_era[f["era"]] += 1
        by_reason[f["reason"]] += 1
    print("by era", dict(by_era), file=sys.stderr)
    print("by live reason", dict(by_reason), file=sys.stderr)

    variants = {}

    # (a) actual
    def actual(f):
        if not f["exit_px"]:
            return None
        return {
            "i": f["i0"], "px": f["exit_px"], "why": f["reason"],
            "mfe_r": f["mfe_r"], "mae_r": f["mae_r"],
            "hold_sec": f["hold"], "peak": f["px"], "armed_exit": f["reason"] == "local_trail",
        }
    variants["actual"] = run_variant(fills, "actual", actual)

    # (b) stop + leave-OB + EOD
    def stop_lob_eod(f):
        sim = simulate(f["B"], f["i0"], f["px"], f["stop"], f["risk"],
                       use_lob=True, eod=True)
        return sim
    variants["stop_lob_eod"] = run_variant(fills, "stop_lob_eod", stop_lob_eod)

    # (c) stop + EOD only
    def stop_eod(f):
        return simulate(f["B"], f["i0"], f["px"], f["stop"], f["risk"], eod=True)
    variants["stop_eod"] = run_variant(fills, "stop_eod", stop_eod)

    # (d) ratchet variants
    ratchet_cfgs = [
        ("ratchet_arm015_give035R", dict(arm_r=0.15, give_r=0.35, peak_give_pct=0)),
        ("ratchet_arm025_give035R", dict(arm_r=0.25, give_r=0.35, peak_give_pct=0)),
        ("ratchet_arm035_give035R", dict(arm_r=0.35, give_r=0.35, peak_give_pct=0)),
        ("ratchet_arm006_peak035", dict(arm_r=0.06, give_r=0.35, peak_give_pct=0.35)),  # live-ish
        ("ratchet_arm015_peak035", dict(arm_r=0.15, give_r=0.35, peak_give_pct=0.35)),
        ("ratchet_arm006_peak050", dict(arm_r=0.06, give_r=0.35, peak_give_pct=0.50)),
        ("ratchet_arm015_give050pct", dict(arm_r=0.15, give_pct=0.50, peak_give_pct=0)),
        ("ratchet_arm025_give050pct", dict(arm_r=0.25, give_pct=0.50, peak_give_pct=0)),
    ]

    def make_ratchet(cfg):
        def fn(f):
            sim = simulate(f["B"], f["i0"], f["px"], f["stop"], f["risk"],
                           eod=True, **cfg)
            # mark if trail was armed and exit was stop hit after arm
            # Re-sim track armed: approximate — if mfe reached arm_r and why=stop
            arm = cfg.get("arm_r") or 0
            sim["armed_exit"] = sim["why"] == "stop" and sim["mfe_r"] + 1e-9 >= arm
            if sim["armed_exit"]:
                sim["why"] = "ratchet"
            return sim
        return fn

    for name, cfg in ratchet_cfgs:
        variants[name] = run_variant(fills, name, make_ratchet(cfg))

    # (e) BE after 0.35R then leave-OB
    def be_lob(f):
        sim = simulate(f["B"], f["i0"], f["px"], f["stop"], f["risk"],
                       be_after_r=0.35, use_lob=True, eod=True)
        return sim
    variants["be035_then_lob"] = run_variant(fills, "be035_then_lob", be_lob)

    # live-like: arm 0.06 + peak 0.35 + leave-OB can override? live has lob separate
    def live_like(f):
        sim = simulate(f["B"], f["i0"], f["px"], f["stop"], f["risk"],
                       arm_r=0.06, give_r=0.35, peak_give_pct=0.35,
                       use_lob=True, eod=True)
        arm = 0.06
        if sim["why"] == "stop" and sim["mfe_r"] >= arm:
            sim["armed_exit"] = True
            sim["why"] = "ratchet"
        elif sim["why"] == "left_overbought":
            sim["armed_exit"] = False
        return sim
    variants["live_like_arm006_peak035_lob"] = run_variant(fills, "live_like", live_like)

    train = {d for d in {f["day"] for f in fills} if d <= "2026-09-17"}
    test = {d for d in {f["day"] for f in fills} if d >= "2026-09-18"}

    out = {"n_fills": len(fills), "by_era": dict(by_era), "by_reason": dict(by_reason)}
    tables = {}
    for name, rows in variants.items():
        tables[name] = {
            "all": summarize(rows),
            "train": summarize([r for r in rows if r["day"] in train]),
            "test": summarize([r for r in rows if r["day"] in test]),
            "by_live_reason": {
                reason: summarize([r for r in rows if r["reason_live"] == reason])
                for reason in sorted({r["reason_live"] for r in rows})
            },
        }
    out["variants"] = tables

    # print compact table
    print("\n=== VARIANT TABLE (all / train / test) pl, R_mean, win, cap, saved/cut ===")
    for name, tab in tables.items():
        for split in ("all", "train", "test"):
            s = tab[split]
            if s.get("n", 0) == 0:
                continue
            print(f"{name:36s} {split:5s} n={s['n']:3d} pl={s['pl']:8.1f} "
                  f"Rmean={s['R_mean']:+.3f} win={s['win']:.2f} cap={s['cap_mean']} "
                  f"saved={s['saved_n']}/{s['saved_pl']} cut={s['cut_n']}/{s['cut_pl']}")

    path = os.path.join(ROOT, "ai_reports", "ratchet_stop_study.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
