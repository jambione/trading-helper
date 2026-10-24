#!/usr/bin/env python3
"""Batch 2, T8: W-neckline power bar on SPY + QQQ. Spec: docs/studies/batch2_prereg.json tests.T8 + amended.T8 (1)-(8).

2-minute RTH bars (ETF cache, 2016-01 .. 2026-10-15, held-out never fetched). Within a session: SWING HIGH = bar j
with high above the 3 bars each side, usable once j+3 has closed before the power bar's first bar; NECKLINE = the most
recent such swing high. W = the two most recent swing lows (low below the 2 bars each side, confirmed before the
power bar's first bar) strictly between the neckline bar and the power bar, both below the neckline, the second no
lower than 0.3% below the first. POWER BAR = 1-3 combined bars (pm_power_bar amended_5: >= 1.5x same-clock n-bar
median range, >= 50% body, green), ending 10:00 .. 15:29, whose first open or combined low <= neckline < close,
whose first open or first bar's low <= A_prev (SMA20 of 2-minute closes through the bar before the window), close >
A_prev, and SMA20 through the window's last bar >= A_prev. Entry next 1-minute open; stop = window low - $0.01;
target 2R; else 15:59. One open trade, <= 3 per day; skips counted. Control: seeded minute in
(end, min(end + 60, 15:30)], same % stop/target. Pass = the anchor_power_bar ETF pass (etf_bars.primary_verdict,
>= 300 long days per half, MDE <= 5 bp, batch t 2.87).

  --canary   plant +10 bp after each entry (time-exit trades must move by +10)
  (default)  count step          --score   verdict (refuses without READY / floors / same code)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from collections import Counter, deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import etf_bars as B  # noqa: E402
import pm_power_bar as P  # noqa: E402

OUT_DIR = os.path.join(B.ROOT, "ai_reports", "batch2_T8")
SYMS = ("SPY", "QQQ")
COST, T_FAMILY, W_TOL, MAX_PER_DAY = 0.0002, 2.87, 0.003, 3
FIRST_END, LAST_END, CTRL_END = 599, 929, 930
STEP = 0.001


def swing_highs(bars2: list, before: int) -> list[int]:
    """Indices j with high above the 3 bars on each side, confirmed (j + 3 closed) before index `before`."""
    out = []
    for j in range(3, before - 3):
        if j + 3 >= before:
            break
        w = bars2[j - 3:j + 4]
        if any(b is None for b in w):
            continue
        if all(w[3][1] > w[i][1] for i in (0, 1, 2, 4, 5, 6)):
            out.append(j)
    return out


def swing_lows(bars2: list, lo: int, before: int) -> list[int]:
    out = []
    for j in range(max(lo + 1, 2), before - 2):
        if j + 2 >= before:
            break
        w = bars2[j - 2:j + 3]
        if any(b is None for b in w):
            continue
        if all(w[2][2] < w[i][2] for i in (0, 1, 3, 4)):
            out.append(j)
    return out


def candidate(bars2: list, sma: list, refs: dict, k: int) -> dict | None:
    """The smallest-n power bar ending at k that meets every T8 condition, or None."""
    if bars2[k] is None or not (FIRST_END <= bars2[k][4] <= LAST_END):
        return None
    for n in range(1, 4):
        cb = P.combine(bars2, k, n)
        if cb is None:
            continue
        first = k - n + 1
        o, h, lo, c, end = cb
        ref = refs[n].ref(k)
        rng = h - lo
        if ref is None or ref <= 0 or rng < 1.5 * ref or c <= o or (c - o) < 0.5 * rng:
            continue
        a_prev = sma[first - 1] if first >= 1 else None
        a_now = sma[k]
        if a_prev is None or a_now is None or a_now < a_prev:
            continue
        if not (min(o, bars2[first][2]) <= a_prev and c > a_prev):
            continue
        highs = swing_highs(bars2, first)
        if not highs:
            continue
        nj = highs[-1]
        neck = bars2[nj][1]
        if not (min(o, lo) <= neck < c):
            continue
        lows = [j for j in swing_lows(bars2, nj, first) if nj < j < first]
        if len(lows) < 2:
            continue
        l1, l2 = bars2[lows[-2]][2], bars2[lows[-1]][2]
        if not (l1 < neck and l2 < neck and l2 >= l1 * (1 - W_TOL)):
            continue
        return {"k": k, "n": n, "end": end, "low": lo, "neck": neck}
    return None


def run_symbol(sym: str, canary: bool = False):
    rows, st = [], Counter()
    refs = {n: B.SizeRef() for n in range(1, 4)}
    closes, restarted = deque(maxlen=20), False
    for day, r_all in B.load(sym).items():
        if not B.in_scope(day):
            st["out_of_scope_day"] += 1
            continue
        if day >= B.B_RESTART and not restarted:
            closes.clear()
            for ref in refs.values():
                ref.reset()
            restarted = True
        if not B.is_full_session(r_all, day):
            st["early_close_dropped"] += 1
            continue
        r = B.rth(r_all)
        bars2 = B.clock_bars(r, 2, 2)
        sma = []
        for b in bars2:
            if b is not None:
                closes.append(b[3])
            sma.append(statistics.mean(closes) if len(closes) == 20 else None)
        busy_until, taken = -1, 0
        for k in range(len(bars2)):
            if taken >= MAX_PER_DAY:
                break
            sig = candidate(bars2, sma, refs, k)
            if sig is None:
                continue
            if sig["end"] < busy_until:
                st["ignored_while_open"] += 1
                continue
            e = B.first_after(r, sig["end"])
            if e is None:
                st["skip_no_entry_bar"] += 1
                continue
            o, stop = float(r[e, 1]), sig["low"] - B.TICK
            tgt = o + 2 * (o - stop)
            if o <= stop:
                st["skip_entry_at_or_below_stop"] += 1
                continue
            res = B.simulate(r, e, 1, stop, tgt)
            sp, tp = (o - stop) / o, 2 * (o - stop) / o
            c1 = B.control(r, sym, day, sig["end"], min(sig["end"] + 60, CTRL_END), 1, sp, tp, f"b2-T8-ctrl|{k}")
            if c1 is None:
                st["drop_no_control_window"] += 1
                continue
            c2 = B.control(r, sym, day, sig["end"], min(sig["end"] + 60, CTRL_END), 1, sp, tp, f"b2-T8-ctrl2|{k}")
            row = {"day": day, "sym": sym, "half": B.half_of(day), "end": sig["end"], "n": sig["n"], **res,
                   "ctrl": c1["gross"], "ctrl2": c2["gross"] if c2 else None}
            if canary and res["why"] == "time":
                rp = r.copy()
                m = rp[:, 0] >= rp[e, 0] + 1
                rp[m, 1:5] *= 1 + STEP
                row["gross_planted"] = B.simulate(rp, e, 1, stop, tgt)["gross"]
            rows.append(row)
            busy_until, taken = res["exit_min"], taken + 1
        for n, ref in refs.items():
            ref.add(P.combos(bars2, n))
    return rows, st


def module_shas():
    d = os.path.dirname(os.path.abspath(__file__))
    return {m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
            for m in ("batch2_etf", "etf_bars", "pm_power_bar")}


def write(name, obj):
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--canary", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    if json.load(open(os.path.join(B.CACHE, "fetch_log.json"))).get("failed"):
        raise SystemExit("fetch has failures")
    rows, st = [], Counter()
    for sym in SYMS:
        rs, s = run_symbol(sym, canary=a.canary)
        rows += rs
        st.update({f"{sym}_{k}": v for k, v in s.items()})
    if any(k.endswith("out_of_scope_day") and v for k, v in st.items()):
        raise SystemExit("held-out or post-window days present in the cache")
    if a.canary:
        g = [x["gross_planted"] - x["gross"] for x in rows if x.get("gross_planted") is not None]
        out = {"n_time_exit": len(g), "delta_bp": round(1e4 * statistics.mean(g), 3) if g else None,
               "pass": bool(bool(g) and abs(statistics.mean(g) - STEP) <= 0.0002), "module_sha256": module_shas()}
        print(json.dumps(out, indent=1))
        write("canary.json", out)
        return
    if not a.score:
        can = json.load(open(os.path.join(OUT_DIR, "canary.json"))) if os.path.exists(os.path.join(OUT_DIR,
                                                                                                    "canary.json")) else {}
        cnt = {"stats": dict(st), "module_sha256": module_shas(), "prereg_commit": a.prereg_commit,
               "canary_ok": can.get("module_sha256") == module_shas() and can.get("pass") is True}
        for h in ("A", "B"):
            hr = [x for x in rows if x["half"] == h]
            m = B.mde(hr)
            days = len({x["day"] for x in hr})
            cnt[h] = {"long_trades": len(hr), "long_days": days, "mde": m,
                      "floors_met": days >= 300 and m is not None and m <= 0.0005}
        write("rows.json", {"rows": rows, "module_sha256": module_shas()})
        print(json.dumps(cnt, indent=1, default=str))
        print(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = json.load(open(os.path.join(OUT_DIR, "count.json")))
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit") or not cnt.get("canary_ok"):
        raise SystemExit("count.json from different code, without --prereg-commit, or without a passing canary")
    if not all(cnt[h]["floors_met"] for h in ("A", "B")):
        raise SystemExit("UNDERPOWERED at the count step: no outcome is read")
    res = {"primary_long": B.primary_verdict(rows, COST, t_batch=T_FAMILY)}
    res["verdict"] = res["primary_long"]["verdict"]
    res["post_hoc"] = "a PASS here is a candidate only: it must repeat on the never-fetched 2026-01-14 .. 04-24 window"
    res["declared"] = ["the canary keeps each trade's original stop and target on time-exit trades, so a trade ending "
                       "within 10 bp of its target can flip to a target exit; the canary is near, not exactly, +10 bp"]
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
