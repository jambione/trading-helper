#!/usr/bin/env python3
"""scout_trade - full size on a later trade in a name only if the day's first trade in it made money?
Pass bar: docs/studies/scout_trade_prereg.json. Read-only. Run on the mini.

    .venv/bin/python tools/studies/scout_trade.py primary   # warehouse pre-square events (4/28-9/23 decides)
    .venv/bin/python tools/studies/scout_trade.py broker    # broker trades 10/15-11/18, $/day by sizing rule
"""
from __future__ import annotations

import json
import math
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
OUT = ROOT / "ai_reports" / "scout_trade"
SPLIT = "2026-09-24"
T_FIRST, T_LAST, HOLD = 15, 360, 16


def label_sequence(events):
    """events: [(t, net)] for one name-day in time order -> [{k, net, first_win, prev_win}] for LATER events. Pure."""
    out = []
    if not events:
        return out
    first_win = events[0][1] > 0
    for k in range(1, len(events)):
        out.append({"k": k, "net": events[k][1], "first_win": first_win, "prev_win": events[k - 1][1] > 0})
    return out


def compare(rows, key):
    """rows: [{day, net, <key>: bool}] -> WIN vs LOSS group stats with day-clustered t and halves. Pure."""
    w = [r for r in rows if r[key]]
    l = [r for r in rows if not r[key]]
    out = {"win_n": len(w), "loss_n": len(l)}
    if not w or not l:
        return out
    out["win_mean"] = round(float(np.mean([r["net"] for r in w])), 2)
    out["loss_mean"] = round(float(np.mean([r["net"] for r in l])), 2)
    out["lift"] = round(out["win_mean"] - out["loss_mean"], 2)
    per = defaultdict(lambda: ([], []))
    for r in rows:
        per[r["day"]][0 if r[key] else 1].append(r["net"])
    diffs = [np.mean(a) - np.mean(b) for a, b in per.values() if a and b]
    if len(diffs) > 2 and np.std(diffs, ddof=1) > 0:
        out["t_day"] = round(float(np.mean(diffs) / (np.std(diffs, ddof=1) / math.sqrt(len(diffs)))), 2)
        out["paired_days"] = len(diffs)
    days = sorted({r["day"] for r in rows})
    h1 = set(days[: len(days) // 2])
    for name, sel in (("half1_lift", lambda r: r["day"] in h1), ("half2_lift", lambda r: r["day"] not in h1)):
        a = [r["net"] for r in w if sel(r)]
        b = [r["net"] for r in l if sel(r)]
        out[name] = round(float(np.mean(a) - np.mean(b)), 2) if a and b else None
    return out


def verdict(st):
    if st.get("win_n", 0) < 500:
        return "UNDERPOWERED", [f"win group {st.get('win_n', 0)} (need 500)"]
    f = []
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
    from holistic_entry import bracket
    import news_source as ns
    rows, firsts, prev = [], [], None
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
                seq, last_t = [], -999
                for t in sorted(first_true(presquare_bars(cc, hh, ll, off, 21))):
                    if not (T_FIRST <= t <= T_LAST) or t < last_t + HOLD or not np.isfinite(c[t]):
                        continue
                    if not (20.0 <= c[t] <= 400.0) or (c[t] / pc - 1) * 100 < 1.5:
                        continue
                    net = bracket(o, h, l, c, t)
                    if net is None:
                        continue
                    seq.append((t, float(net)))
                    last_t = t
                if seq:
                    firsts.append({"day": fp.stem, "net": seq[0][1]})
                for r in label_sequence(seq):
                    rows.append({"day": fp.stem, **r})
            print(fp.stem, len(firsts), len(rows), flush=True)
        prev = day
    pre = [r for r in rows if r["day"] < SPLIT]
    post = [r for r in rows if r["day"] >= SPLIT]
    res = {"primary_pre_split_first_win": compare(pre, "first_win"),
           "info_pre_split_prev_win": compare(pre, "prev_win"),
           "info_post_split_first_win": compare(post, "first_win"),
           "info_first_events_mean": round(float(np.mean([r["net"] for r in firsts])), 2) if firsts else None,
           "info_later_events_mean": round(float(np.mean([r["net"] for r in rows])), 2) if rows else None,
           "info_first_n": len(firsts), "info_later_n": len(rows)}
    res["verdict"] = verdict(res["primary_pre_split_first_win"])
    res["generated"] = datetime.now(ET).isoformat(timespec="minutes")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "primary.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


def broker():
    import flow_exit as fx
    fx.LO, fx.HI = "2026-10-15", "2026-11-18"
    trades = sorted(fx.load_trades(), key=lambda r: r["t_in"])
    first_net, per, later = {}, defaultdict(lambda: defaultdict(float)), []
    for t in trades:
        k = (t["day"], t["symbol"])
        pl = (t["px_out"] - t["px_in"]) * t["qty"]
        one = t["px_out"] - t["px_in"]
        d = per[t["day"]]
        d["as_traded"] += pl
        if k not in first_net:
            first_net[k] = t["ret_bp"]
            d["half_first"] += 0.5 * pl
            d["one_share_first"] += one
            d["scout"] += one
            continue
        d["half_first"] += pl
        d["one_share_first"] += pl
        d["scout"] += pl if first_net[k] > 0 else one
        later.append({"day": t["day"], "net": t["ret_bp"], "first_win": first_net[k] > 0})
    days = sorted(per)
    res = {"days": len(days), "trades": len(trades)}
    for rule in ("as_traded", "half_first", "one_share_first", "scout"):
        v = [per[d][rule] for d in days]
        res[rule] = {"usd_per_day": round(float(np.mean(v)), 2), "days_positive": int(sum(x > 0 for x in v))}
    res["later_trades_by_first_outcome"] = compare(later, "first_win")
    res["generated"] = datetime.now(ET).isoformat(timespec="minutes")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "broker.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"primary": primary, "broker": broker}[sys.argv[1] if len(sys.argv) > 1 else "primary"]()
