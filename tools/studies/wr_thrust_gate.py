#!/usr/bin/env python3
"""wr_thrust_gate - audit of Argon's 11/18 claim that a 5-minute fast %R thrust (wr_d5_fast >= 25) separates good desk entries.
Pass bar: docs/studies/wr_thrust_gate_prereg.json. Read-only. Run on the mini.

    .venv/bin/python tools/studies/wr_thrust_gate.py primary   # warehouse pre-square events, 4/28-9/23 decides
    .venv/bin/python tools/studies/wr_thrust_gate.py audit     # broker trades 9/24-11/18, three wr_d5_fast versions
"""
from __future__ import annotations

import json
import math
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "wr_thrust_gate"
SPLIT = "2026-09-24"
T_FIRST, T_LAST, STEP = 15, 360, 5


def split_stats(rows, thr, key="d5"):
    """rows: [{day, net, d5}] -> kept/skipped means, lift, day-paired t, halves. Pure."""
    rows = [r for r in rows if r.get(key) is not None and np.isfinite(r[key])]
    k = [r for r in rows if r[key] >= thr]
    s = [r for r in rows if r[key] < thr]
    out = {"n": len(rows), "kept_n": len(k), "skip_n": len(s)}
    if not k or not s:
        return out
    mk, ms = float(np.mean([r["net"] for r in k])), float(np.mean([r["net"] for r in s]))
    out.update(kept_mean=round(mk, 2), skip_mean=round(ms, 2), lift=round(mk - ms, 2),
               kept_win=round(100 * float(np.mean([r["net"] > 0 for r in k])), 1),
               skip_win=round(100 * float(np.mean([r["net"] > 0 for r in s])), 1),
               kept_stop=round(100 * float(np.mean([r["net"] <= -35 for r in k])), 1),
               skip_stop=round(100 * float(np.mean([r["net"] <= -35 for r in s])), 1))
    per = defaultdict(lambda: ([], []))
    for r in rows:
        per[r["day"]][0 if r[key] >= thr else 1].append(r["net"])
    diffs = [np.mean(a) - np.mean(b) for a, b in per.values() if a and b]
    if len(diffs) > 2 and np.std(diffs, ddof=1) > 0:
        out["t_day"] = round(float(np.mean(diffs) / (np.std(diffs, ddof=1) / math.sqrt(len(diffs)))), 2)
        out["paired_days"] = len(diffs)
    days = sorted({r["day"] for r in rows})
    h1 = set(days[: len(days) // 2])
    for name, sel in (("half1", lambda r: r["day"] in h1), ("half2", lambda r: r["day"] not in h1)):
        kk = [r["net"] for r in k if sel(r)]
        ss = [r["net"] for r in s if sel(r)]
        out[name + "_lift"] = round(float(np.mean(kk) - np.mean(ss)), 2) if kk and ss else None
    return out


def verdict(st):
    f = []
    if st.get("kept_n", 0) < 500:
        return "UNDERPOWERED", [f"kept {st.get('kept_n', 0)} (need 500)"]
    if st["lift"] < 2.0:
        f.append(f"lift {st['lift']} bp (need +2.0)")
    if (st.get("t_day") or 0) < 2.0:
        f.append(f"t {st.get('t_day')}")
    if not ((st.get("half1_lift") or -1) > 0 and (st.get("half2_lift") or -1) > 0):
        f.append(f"halves {st.get('half1_lift')} / {st.get('half2_lift')}")
    return ("PASS" if not f else "FAIL"), f


def primary():
    from relative_strength_scan import load_day
    from board_structure_family import universe
    from evening_family import WH_SIP, ALL_ETF, minute_grid, presquare_bars, first_true
    from bar_signal_family import wr_smoothed
    from holistic_entry import bracket
    import news_source as ns
    sig_rows, grid_rows, prev = [], [], None
    for fp in sorted(WH_SIP.glob("20??-??-??.npz")):
        day = load_day(fp)
        if prev is not None and day["idx"].get("SPY") is not None:
            for s in universe(day, prev, set(ALL_ETF)):
                i, j = day["idx"][s], prev["idx"].get(s)
                if j is None:
                    continue
                pc = ns.last_close(prev["c"][j])
                if not pc > 0:
                    continue
                o, h, l, c = (day[k][i] for k in "ohlc")
                cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)
                fast = wr_smoothed(hh, ll, cc, n=21, span=7)
                sigs = set(first_true(presquare_bars(cc, hh, ll, off, 21)))
                for t in sorted(set(range(T_FIRST, T_LAST + 1, STEP)) | sigs):
                    if not (T_FIRST <= t <= T_LAST) or not np.isfinite(c[t]):
                        continue
                    if not (20.0 <= c[t] <= 400.0) or (c[t] / pc - 1) * 100 < 1.5:
                        continue
                    g = t + off
                    d5 = fast[g] - fast[g - 5]
                    if not np.isfinite(d5):
                        continue
                    net = bracket(o, h, l, c, t)
                    if net is None:
                        continue
                    rec = {"day": fp.stem, "net": float(net), "d5": float(d5)}
                    if t in sigs:
                        sig_rows.append(rec)
                    if t % STEP == 0:
                        grid_rows.append(rec)
            print(fp.stem, len(sig_rows), len(grid_rows), flush=True)
        prev = day
    pre = [r for r in sig_rows if r["day"] < SPLIT]
    post = [r for r in sig_rows if r["day"] >= SPLIT]
    res = {"primary_presquare_pre_split_d5_25": split_stats(pre, 25.0),
           "info_presquare_pre_split_d5_30": split_stats(pre, 30.0),
           "info_presquare_post_split_d5_25": split_stats(post, 25.0),
           "info_all_moments_d5_25": split_stats(grid_rows, 25.0),
           "info_presquare_all_mean": round(float(np.mean([r["net"] for r in sig_rows])), 2) if sig_rows else None,
           "info_presquare_share_d5_ge_25": round(float(np.mean([r["d5"] >= 25 for r in sig_rows])), 3) if sig_rows else None}
    res["verdict"] = verdict(res["primary_presquare_pre_split_d5_25"])
    res["generated"] = datetime.now(ET).isoformat(timespec="minutes")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "primary.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


def _d5(iex, t, price):
    from tools import sell_double_check as sdc
    rows = sdc.closed_rows([float(x[0]) for x in iex], [float(x[2]) for x in iex], [float(x[3]) for x in iex],
                           [float(x[4]) for x in iex], t)
    if len(rows) < 40:
        return None
    px = rows[-1][2] if price is None else price
    w = sdc.line_window(rows, px, sdc.FAST_LENGTH, sdc.FAST_SPAN, 11)
    return None if not w else float(w[-1] - w[-6])


def audit():
    import flow_exit as fx
    fx.LO, fx.HI = "2026-09-24", "2026-11-18"   # fx.SKIP keeps the desk-off days (11/12, 11/13, 11/16)
    trades = fx.load_trades()
    logged = defaultdict(list)
    for line in open(ROOT / "ai_reports" / "outcomes.jsonl"):
        try:
            o = json.loads(line)
        except ValueError:
            continue
        f = o.get("features") or {}
        if o.get("entry_time") and f.get("wr_d5_fast") is not None:
            logged[o["symbol"]].append((float(o["entry_time"]), float(f["wr_d5_fast"])))
    cache, rows = {}, []
    for tr in trades:
        d, sym, net = tr["day"], tr["symbol"], tr["ret_bp"]   # broker fill to fill: the spread is already paid
        if d not in cache:
            pk = os.path.expanduser(f"~/replay_cache/bars_v2_{d}.pkl")
            cache.clear()
            cache[d] = pickle.load(open(pk, "rb")) if os.path.exists(pk) else {}
        iex = (cache[d].get(sym) or {}).get("iex") or []
        rec = {"day": d, "sym": sym, "net": float(net), "t": tr["t_in"]}
        if iex:
            rec["d5_fill"] = _d5(iex, tr["t_in"], tr["px_in"])
            rec["d5_closed"] = _d5(iex, tr["t_in"], None)
        near = [v for (te, v) in logged.get(sym, []) if abs(te - tr["t_in"]) <= 120]
        rec["d5_logged"] = near[0] if near else None
        rows.append(rec)
    eras = {"early_0924_1014": ("2026-09-24", "2026-10-14"), "clean1_1015_1103": ("2026-10-15", "2026-11-03"),
            "clean2_1105_1118": ("2026-11-05", "2026-11-18"), "all": ("2026-09-24", "2026-11-18")}
    res = {"trades": len(rows)}
    for ver in ("d5_fill", "d5_closed", "d5_logged"):
        for era, (a, b) in eras.items():
            sub = [r for r in rows if a <= r["day"] <= b]
            res[f"{ver}|{era}|25"] = split_stats(sub, 25.0, key=ver)
        res[f"{ver}|all|30"] = split_stats(rows, 30.0, key=ver)
    both = [r for r in rows if r.get("d5_logged") is not None and r.get("d5_fill") is not None]
    if both:
        res["parity_fill_vs_logged"] = {"n": len(both),
                                        "median_abs_diff": round(float(np.median([abs(r["d5_fill"] - r["d5_logged"]) for r in both])), 2),
                                        "same_side_25": round(float(np.mean([(r["d5_fill"] >= 25) == (r["d5_logged"] >= 25) for r in both])), 3)}
    res["generated"] = datetime.now(ET).isoformat(timespec="minutes")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "audit.json").write_text(json.dumps(res, indent=1))
    with open(OUT / "audit_rows.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"primary": primary, "audit": audit}[sys.argv[1] if len(sys.argv) > 1 else "primary"]()
