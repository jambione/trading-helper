#!/usr/bin/env python3
"""holistic_entry - outcome-first: does prior price action on 1-, 5- and 10-minute views (plus the day and the market) predict
which moments lead to a desk-style win (+20 bp before -50 bp within 15 min, 6 bp cost)? Pass bar: docs/studies/holistic_entry_prereg.json.
Read-only, warehouse only.

    .venv/bin/python tools/studies/holistic_entry.py build    # -> ai_reports/holistic_entry/rows.npz
    .venv/bin/python tools/studies/holistic_entry.py model    # -> ai_reports/holistic_entry/result.json
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
from relative_strength_scan import load_day  # noqa: E402
from board_structure_family import universe  # noqa: E402
from evening_family import WH_SIP, ALL_ETF, minute_grid, presquare_bars, first_true  # noqa: E402
import news_source as ns  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "holistic_entry"
UP, DOWN, HORIZON, COST = 20.0, 50.0, 15, 6.0
T_FIRST, T_LAST, STEP = 15, 360, 5
FEATS = ["r1", "r3", "r5", "r15", "r30", "slope5", "hl5", "wr5", "dwr5", "r10", "r60", "wr10", "dwr10",
         "pos30", "posday", "below_hod", "since_hod", "since_peak10", "vol15", "upvol15", "vwap_d",
         "gap", "day_pct", "from_open", "spy5", "spy30", "spy_day", "minute"]


def bracket(o, h, l, c, t):
    """Net bp of the +UP/-DOWN bracket entered at open t+1 (stop first on a minute touching both). None if unusable."""
    if t + HORIZON >= 390:
        return None
    e = o[t + 1]
    if not (np.isfinite(e) and e > 0):
        return None
    up, dn = e * (1 + UP / 1e4), e * (1 - DOWN / 1e4)
    for m in range(t + 1, t + HORIZON + 1):
        if np.isfinite(l[m]) and l[m] <= dn:
            return -DOWN - COST
        if np.isfinite(h[m]) and h[m] >= up:
            return UP - COST
    last = c[t + 1:t + HORIZON + 1]
    last = last[np.isfinite(last)]
    return (last[-1] / e - 1) * 1e4 - COST if len(last) else None


def _ret(c, t, k):
    if t - k < 0 or not (np.isfinite(c[t]) and np.isfinite(c[t - k])) or c[t - k] <= 0:
        return np.nan
    return (c[t] / c[t - k] - 1) * 1e4


def _wr(h, l, c, t, n):
    a = max(0, t - n + 1)
    hh, ll = np.nanmax(h[a:t + 1]) if np.isfinite(h[a:t + 1]).any() else np.nan, np.nanmin(l[a:t + 1]) if np.isfinite(l[a:t + 1]).any() else np.nan
    if not (np.isfinite(hh) and np.isfinite(ll)) or hh <= ll or not np.isfinite(c[t]):
        return np.nan
    return (hh - c[t]) / (hh - ll) * -100.0


def features(o, h, l, c, v, t, pc, spy_c, spy_pc):
    """All features at the close of minute t from data up to t. Pure."""
    f = {"r1": _ret(c, t, 1), "r3": _ret(c, t, 3), "r5": _ret(c, t, 5), "r15": _ret(c, t, 15), "r30": _ret(c, t, 30),
         "r10": _ret(c, t, 10), "r60": _ret(c, t, 60)}
    pts = [c[t - 5 * k] for k in range(5, -1, -1) if t - 5 * k >= 0]
    pts = np.array(pts, float)
    f["slope5"] = float(np.polyfit(np.arange(len(pts)), pts, 1)[0] / c[t] * 1e4) if len(pts) >= 4 and np.isfinite(pts).all() and c[t] > 0 else np.nan
    lo_a, lo_b = l[max(0, t - 4):t + 1], l[max(0, t - 9):max(0, t - 4)]
    f["hl5"] = float(np.nanmin(lo_a) > np.nanmin(lo_b)) if np.isfinite(lo_a).any() and np.isfinite(lo_b).any() else np.nan
    f["wr5"], f["wr10"] = _wr(h, l, c, t, 70), _wr(h, l, c, t, 140)
    f["dwr5"] = f["wr5"] - _wr(h, l, c, t - 5, 70) if t >= 5 else np.nan
    f["dwr10"] = f["wr10"] - _wr(h, l, c, t - 10, 140) if t >= 10 else np.nan
    a30 = max(0, t - 29)
    hi30, lo30 = np.nanmax(h[a30:t + 1]), np.nanmin(l[a30:t + 1])
    f["pos30"] = (c[t] - lo30) / (hi30 - lo30) if hi30 > lo30 else np.nan
    hid, lod = np.nanmax(h[:t + 1]), np.nanmin(l[:t + 1])
    f["posday"] = (c[t] - lod) / (hid - lod) if hid > lod else np.nan
    f["below_hod"] = (1 - c[t] / hid) * 1e4 if hid > 0 else np.nan
    f["since_hod"] = float(t - int(np.nanargmax(h[:t + 1])))
    w = h[max(0, t - 9):t + 1]
    f["since_peak10"] = float(len(w) - 1 - int(np.nanargmax(w))) if np.isfinite(w).any() else np.nan
    vv = np.nan_to_num(v[:t + 1])
    day_avg = vv.sum() / (t + 1)
    f["vol15"] = vv[max(0, t - 14):].sum() / 15 / day_avg if day_avg > 0 else np.nan
    seg_c, seg_v = c[max(0, t - 15):t + 1], vv[max(0, t - 15):]
    upv = sum(seg_v[k] for k in range(1, len(seg_c)) if np.isfinite(seg_c[k]) and np.isfinite(seg_c[k - 1]) and seg_c[k] > seg_c[k - 1])
    f["upvol15"] = upv / seg_v[1:].sum() if seg_v[1:].sum() > 0 else np.nan
    tp = np.nan_to_num((h[:t + 1] + l[:t + 1] + c[:t + 1]) / 3)
    f["vwap_d"] = ((c[t] / ((tp * vv).sum() / vv.sum()) - 1) * 1e4) if vv.sum() > 0 else np.nan
    f["gap"] = (o[0] / pc - 1) * 100 if np.isfinite(o[0]) and pc > 0 else np.nan
    f["day_pct"] = (c[t] / pc - 1) * 100 if pc > 0 else np.nan
    f["from_open"] = (c[t] / o[0] - 1) * 100 if np.isfinite(o[0]) and o[0] > 0 else np.nan
    f["spy5"], f["spy30"] = _ret(spy_c, t, 5), _ret(spy_c, t, 30)
    f["spy_day"] = (spy_c[t] / spy_pc - 1) * 100 if np.isfinite(spy_c[t]) and spy_pc > 0 else np.nan
    f["minute"] = float(t)
    return f


def build():
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    X, y, day_s, sig, prev = [], [], [], [], None
    for fp in files:
        day = load_day(fp)
        if prev is not None and day["idx"].get("SPY") is not None and prev["idx"].get("SPY") is not None:
            spy_c, spy_pc = day["c"][day["idx"]["SPY"]], ns.last_close(prev["c"][prev["idx"]["SPY"]])
            n0 = len(y)
            for s in universe(day, prev, set(ALL_ETF)):
                i, j = day["idx"][s], prev["idx"].get(s)
                if j is None:
                    continue
                pc = ns.last_close(prev["c"][j])
                if not pc > 0:
                    continue
                o, h, l, c, v = (day[k][i] for k in "ohlcv")
                cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], c, h, l)
                sigs = set(first_true(presquare_bars(cc, hh, ll, off, 21)))
                for t in sorted(set(range(T_FIRST, T_LAST + 1, STEP)) | sigs):
                    if not (T_FIRST <= t <= T_LAST) or not np.isfinite(c[t]):
                        continue
                    if not (20.0 <= c[t] <= 400.0) or (c[t] / pc - 1) * 100 < 1.5:
                        continue
                    net = bracket(o, h, l, c, t)
                    if net is None:
                        continue
                    f = features(o, h, l, c, v, t, pc, spy_c, spy_pc)
                    X.append([f[k] for k in FEATS])
                    y.append(net)
                    day_s.append(fp.stem)
                    sig.append(1 if t in sigs else 0)
            print(fp.stem, len(y) - n0, len(y), flush=True)
        prev = day
    OUT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT / "rows.npz", X=np.array(X, float), y=np.array(y, float), day=np.array(day_s), sig=np.array(sig, np.int8),
                        feats=np.array(FEATS))


def day_t(vals, days):
    per = defaultdict(list)
    for v, d in zip(vals, days):
        per[d].append(v)
    m = [np.mean(x) for x in per.values()]
    sd = float(np.std(m, ddof=1)) if len(m) > 2 else 0.0
    return (round(float(np.mean(m) / (sd / math.sqrt(len(m)))), 2) if sd > 0 else None), len(per)


def model():
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.inspection import permutation_importance
    z = np.load(OUT / "rows.npz", allow_pickle=True)
    X, y, day, sig, feats = z["X"], z["y"], z["day"], z["sig"], list(z["feats"])
    grid = (day >= "") & ((sig == 0) | True)
    days = sorted(set(day))
    cut = days[int(len(days) * 2 / 3)]
    sampled = np.array([True] * len(y))       # all rows; signal rows are extra minutes, kept for the info cell
    tr = day < cut
    te = ~tr
    m = HistGradientBoostingRegressor(max_iter=200, random_state=0).fit(X[tr], y[tr])
    p_tr, p_te = m.predict(X[tr]), m.predict(X[te])
    thr = float(np.quantile(p_tr, 0.9))
    top = p_te >= thr
    test_days = sorted(set(day[te]))
    yt, dt = y[te], day[te]
    t_top, nd = day_t(yt[top], dt[top])
    half = set(test_days[: len(test_days) // 2])
    h1 = [v for v, d in zip(yt[top], dt[top]) if d in half]
    h2 = [v for v, d in zip(yt[top], dt[top]) if d not in half]
    per10 = top.sum() / max(1, len(test_days)) / 346 * 10
    res_top = {"n": int(top.sum()), "mean_net": round(float(yt[top].mean()), 2) if top.any() else None,
               "win_pct": round(float((yt[top] > 0).mean()) * 100, 1) if top.any() else None,
               "t_day": t_top, "days": nd, "per_10m": round(float(per10), 2),
               "half1_mean": round(float(np.mean(h1)), 2) if h1 else None, "half2_mean": round(float(np.mean(h2)), 2) if h2 else None}
    avg = round(float(yt.mean()), 2)
    fails = []
    if (res_top["mean_net"] or -99) < 3.0:
        fails.append(f"edge: {res_top['mean_net']} bp (need +3.0)")
    if (res_top["mean_net"] or -99) < avg + 5.0:
        fails.append(f"lift: {res_top['mean_net']} vs avg {avg} (need +5.0)")
    if (t_top or 0) < 2.0:
        fails.append(f"significance: t {t_top}")
    if per10 < 1.0:
        fails.append(f"frequency: {per10:.2f} per 10 min (need 1.0)")
    if not (h1 and h2 and np.mean(h1) > 0 and np.mean(h2) > 0):
        fails.append(f"stability: halves {res_top['half1_mean']} / {res_top['half2_mean']}")
    verdict = "PASS" if not fails else "FAIL"
    # information: quintile spreads on train, deciles on test, signal rows, importances
    quint = {}
    for k, name in enumerate(feats):
        col = X[tr, k]
        ok = np.isfinite(col)
        if ok.sum() < 1000:
            continue
        qs = np.quantile(col[ok], [0.2, 0.4, 0.6, 0.8])
        b = np.digitize(col[ok], qs)
        means = [float(y[tr][ok][b == q].mean()) for q in range(5)]
        quint[name] = {"q1": round(means[0], 2), "q5": round(means[4], 2), "spread": round(means[4] - means[0], 2)}
    dec = []
    qs = np.quantile(p_tr, np.linspace(0.1, 0.9, 9))
    bt = np.digitize(p_te, qs)
    for q in range(10):
        sel = bt == q
        dec.append({"decile": q + 1, "n": int(sel.sum()), "mean_net": round(float(yt[sel].mean()), 2) if sel.any() else None,
                    "win_pct": round(float((yt[sel] > 0).mean()) * 100, 1) if sel.any() else None})
    sg = sig[te] == 1
    sig_info = {"n": int(sg.sum()), "mean_net": round(float(yt[sg].mean()), 2) if sg.any() else None,
                "top_decile_share": round(float(top[sg].mean()), 3) if sg.any() else None,
                "top_decile_mean": round(float(yt[sg & top].mean()), 2) if (sg & top).any() else None}
    rng = np.random.default_rng(0)
    idx = rng.choice(np.where(tr)[0], size=min(30000, tr.sum()), replace=False)
    pi = permutation_importance(m, X[idx], y[idx], n_repeats=3, random_state=0)
    imp = sorted(zip(feats, pi.importances_mean), key=lambda z_: -z_[1])[:12]
    res = {"verdict": verdict, "fails": fails, "train_days": len(days) - len(test_days), "test_days": len(test_days), "cut": cut,
           "rows_train": int(tr.sum()), "rows_test": int(te.sum()), "test_avg_net": avg,
           "test_win_pct_all": round(float((yt > 0).mean()) * 100, 1), "top_decile": res_top, "test_deciles": dec,
           "info_signal_minutes_test": sig_info, "info_quintile_spreads_train": dict(sorted(quint.items(), key=lambda kv: -abs(kv[1]["spread"]))),
           "info_importance_train": [(k, round(float(v), 4)) for k, v in imp],
           "generated": datetime.now(ET).isoformat(timespec="minutes")}
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: res[k] for k in ("verdict", "fails", "test_avg_net", "test_win_pct_all", "top_decile", "test_deciles",
                                          "info_signal_minutes_test", "info_importance_train")}, indent=1))
    print("quintile spreads (train):", json.dumps(dict(list(res["info_quintile_spreads_train"].items())[:12])))


if __name__ == "__main__":
    {"build": build, "model": model}[sys.argv[1]]()
