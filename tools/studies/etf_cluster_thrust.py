#!/usr/bin/env python3
"""etf_cluster_thrust.py (docs/studies/etf_cluster_thrust_prereg.json). Read-only; SIP warehouses.

Tests Constituent Cluster Thrust on Sector ETFs (Grok Path 3 / Test Standard 11/07).
Hypotheses:
  K2_H3: >= 2 mapped constituents thrust -> buy sector ETF at next open, hold H=3 minutes.
  K3_H3: >= 3 mapped constituents thrust -> buy sector ETF at next open, hold H=3 minutes.
  K3_H5: >= 3 mapped constituents thrust -> buy sector ETF at next open, hold H=5 minutes.

Modes:
  .venv/bin/python tools/studies/etf_cluster_thrust.py count    # Outcome-blind trigger counts
  .venv/bin/python tools/studies/etf_cluster_thrust.py run      # Single read with canaries -> run.pkl
  .venv/bin/python tools/studies/etf_cluster_thrust.py report   # Markdown summary
"""
from __future__ import annotations

import hashlib
import json
import math
import pickle
import sys
from collections import defaultdict, deque
from datetime import date
from pathlib import Path

import numpy as np

try:
    import scipy.stats as stats
except ImportError:
    stats = None

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day, prev_weekday, dollar_volume  # noqa: E402
from board_structure_family import universe, seeded, tstat, residual_stats  # noqa: E402

WH_SIP = ROOT / "ai_reports" / "warehouse" / "sip"
WH_ETF = ROOT / "ai_reports" / "warehouse" / "etf"
OUT = ROOT / "ai_reports" / "etf_cluster_thrust"
PREREG = ROOT / "docs" / "studies" / "etf_cluster_thrust_prereg.json"

FIRST, LAST = 15, 360  # 09:45 to 15:30 (bar 0 = 09:30)
REFRACT = 30
NDRAW = 5
CANARY_MOD = 5
ALPHA = 0.05 / 3  # 3 scored tests
SECTOR = ["SMH", "SOXX", "IBIT", "IGV", "XBI", "URA", "XLE", "XLF", "ARKK", "QQQ"]
ALL_ETF = SECTOR + ["IWM", "SPY"]
KEYS = ("K2_H3", "K3_H3", "K3_H5")
HYP_CONFIG = {
    "K2_H3": {"K": 2, "H": 3},
    "K3_H3": {"K": 3, "H": 3},
    "K3_H5": {"K": 3, "H": 5},
}


# ---------------- pure utilities ----------------
def r1(c: np.ndarray) -> np.ndarray:
    out = np.full(len(c), np.nan)
    out[1:] = c[1:] / c[:-1] - 1
    return out


def vol_ratio(v: np.ndarray, t: int, k: int = 15, need: int = 10) -> float:
    if t < k:
        return np.nan
    w = v[t - k:t]
    n = int(np.sum(~np.isnan(w)))
    if n < need:
        return np.nan
    m = np.nanmean(w)
    return float(v[t] / m) if m > 0 and not np.isnan(v[t]) else np.nan


def trade(o: np.ndarray, c: np.ndarray, t: int, H: int, cost: float) -> float | None:
    """Buy open t+1, sell close t+H. Return net bp."""
    if t + H >= 390 or t + 1 >= 390 or np.isnan(o[t + 1]) or np.isnan(c[t + H]) or o[t + 1] <= 0:
        return None
    return float((c[t + H] / o[t + 1] - 1) * 1e4 - cost)


def first_true(cond_ts: list[int], refract: int = REFRACT) -> list[int]:
    kept, last = [], -10 ** 9
    for t in sorted(cond_ts):
        if t - last >= refract:
            kept.append(t)
            last = t
    return kept


def residuals(hist: list[dict], names: list[str], beta: dict[str, float]) -> dict[str, np.ndarray]:
    R, S = {n: [] for n in names}, []
    for d in hist:
        si = d["idx"].get("SPY")
        if si is None:
            return {}
        spy = d["c"][si]
        S.append(spy[6:] / spy[5:-1] - 1)
        for n in names:
            i = d["idx"].get(n)
            R[n].append(np.full(384, np.nan) if i is None else d["c"][i][6:] / d["c"][i][5:-1] - 1)
    if not S:
        return {}
    S = np.concatenate(S)
    return {n: np.concatenate(R[n]) - beta[n] * S for n in names if n in beta}


def etf_betas(hist: list[dict], names: list[str]) -> dict[str, float]:
    S, R = [], {n: [] for n in names}
    for d in hist:
        si = d["idx"].get("SPY")
        if si is None:
            continue
        spy = d["c"][si]
        S.append(spy[6:] / spy[5:-1] - 1)
        for n in names:
            i = d["idx"].get(n)
            R[n].append(np.full(384, np.nan) if i is None else d["c"][i][6:] / d["c"][i][5:-1] - 1)
    if not S:
        return {}
    S = np.concatenate(S)
    out = {}
    for n in names:
        x = np.concatenate(R[n])
        m = ~np.isnan(x) & ~np.isnan(S)
        if m.sum() >= 500 and np.dot(S[m], S[m]) > 0:
            out[n] = float(np.dot(x[m], S[m]) / np.dot(S[m], S[m]))
    return out


def corr(a: np.ndarray, b: np.ndarray) -> float:
    m = ~np.isnan(a) & ~np.isnan(b)
    if m.sum() < 300:
        return np.nan
    x, y = a[m], b[m]
    sx, sy = x.std(), y.std()
    return float(((x - x.mean()) * (y - y.mean())).mean() / (sx * sy)) if sx > 0 and sy > 0 else np.nan


def crit_t(n_days: int) -> float:
    if n_days <= 1:
        return float("nan")
    if stats is not None:
        return float(stats.t.ppf(1 - ALPHA, n_days - 1))
    # Approximation if scipy is missing
    return 2.14 if n_days >= 60 else 2.30


def load_costs() -> dict[str, dict[str, float]]:
    p = WH_ETF / "cost.json"
    if not p.exists():
        # Fallback realistic defaults
        return {
            "SPY": {"rt_bp": 1.27, "stress_bp": 2.53},
            "QQQ": {"rt_bp": 1.41, "stress_bp": 2.82},
            "SMH": {"rt_bp": 3.68, "stress_bp": 7.37},
            "SOXX": {"rt_bp": 3.71, "stress_bp": 7.42},
            "IBIT": {"rt_bp": 3.53, "stress_bp": 7.06},
            "IGV": {"rt_bp": 2.14, "stress_bp": 4.28},
            "XBI": {"rt_bp": 3.28, "stress_bp": 6.55},
            "URA": {"rt_bp": 5.53, "stress_bp": 11.06},
            "XLE": {"rt_bp": 2.64, "stress_bp": 5.29},
            "XLF": {"rt_bp": 2.82, "stress_bp": 5.63},
            "ARKK": {"rt_bp": 2.32, "stress_bp": 4.65},
            "IWM": {"rt_bp": 1.66, "stress_bp": 3.32},
        }
    c = json.loads(p.read_text())
    return {x: {"rt_bp": float(c[x]["rt_bp"]), "stress_bp": float(c[x]["stress_bp"])} for x in ALL_ETF if x in c}


def is_not_stock(sym: str, desc: str) -> bool:
    d = (desc or "").upper()
    return any(w in d for w in (" ETF", " ETN", " FUND", " TRUST", " INDEX", " INVERSE", " 2X", " 3X")) or sym in ALL_ETF


def iter_days(wh: Path, need_hist: int = 20):
    from day_hold import asset_names
    nm = asset_names() or {}
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


def etf_context(etf_files: dict[str, Path], cache: dict[str, dict], d: str, hist: list[dict]):
    if d not in etf_files or any(h["_date"] not in etf_files for h in hist):
        return None, None

    def get(s):
        if s not in cache:
            cache[s] = load_day(etf_files[s])
        return cache[s]
    return get(d), [get(h["_date"]) for h in hist]


# ---------------- scoring logic ----------------
def score_day(day, prev, hist, names, d, etf_day, etf_hist, costs, mode="count", inject=None):
    st = defaultdict(int)
    out = {"TRIG": {k: {} for k in KEYS}, **{k: [] for k in KEYS}, "BASE": []}
    if etf_day is None or etf_hist is None or len(etf_hist) != len(hist):
        st["etf_missing_days"] += 1
        return out, st

    # Betas and residuals
    beta, _ = residual_stats(hist, names)
    ebeta = etf_betas(etf_hist, [x for x in ALL_ETF if x != "SPY"])
    ebeta["SPY"] = 1.0

    spy_c = day["c"][day["idx"]["SPY"]]
    spy_r1 = r1(spy_c)

    # Stock 1-minute residuals and volume ratio
    # Threshold: stock res1 >= +15 bp, vol_ratio >= 2.0
    stock_surging = {}
    for n in names:
        idx = day["idx"].get(n)
        if idx is None or n not in beta:
            continue
        c = day["c"][idx]
        v = day["v"][idx]
        sr1 = r1(c)
        with np.errstate(invalid="ignore"):
            sres1 = sr1 - beta[n] * spy_r1
        surges = set()
        for t in range(FIRST, LAST + 1):
            if not np.isnan(sres1[t]) and sres1[t] >= 0.0015 and vol_ratio(v, t) >= 2.0:
                surges.add(t)
        if surges:
            stock_surging[n] = surges

    # Stock-to-ETF mapping via prior 20 sessions residual correlation
    sres_hist = residuals(hist, names, beta)
    eres_hist = residuals(etf_hist, [x for x in SECTOR if x in ebeta], ebeta)
    mapped = {}
    for n in names:
        if n not in sres_hist:
            continue
        best, bc = None, 0.30
        for x in SECTOR:
            if x in eres_hist:
                cr = corr(sres_hist[n], eres_hist[x])
                if not np.isnan(cr) and cr >= bc:
                    best, bc = x, cr
        if best:
            mapped[n] = best

    st["mapped_stocks"] += len(mapped)

    # Invert mapping: ETF -> list of mapped stocks
    etf_constituents = defaultdict(list)
    for n, x in mapped.items():
        etf_constituents[x].append(n)

    # Candidate ETF data
    E = {}
    for x in ALL_ETF:
        k = etf_day["idx"].get(x)
        if k is not None:
            E[x] = {"o": etf_day["o"][k], "c": etf_day["c"][k]}

    # Find cluster events at each minute t for each ETF
    # Group triggers by hypothesis K
    for hyp in KEYS:
        K = HYP_CONFIG[hyp]["K"]
        H = HYP_CONFIG[hyp]["H"]

        if inject and hyp in inject:
            raw_trig = inject[hyp]
        else:
            raw_trig = defaultdict(list)
            for x in SECTOR:
                if x not in E:
                    continue
                constits = etf_constituents.get(x, [])
                if len(constits) < K:
                    continue
                for t in range(FIRST, LAST + 1):
                    # Count how many mapped constituents surged at minute t
                    k_count = sum(1 for n in constits if n in stock_surging and t in stock_surging[n])
                    if k_count >= K:
                        raw_trig[x].append(t)

        # Refractory filtering: 30 minutes
        trig = {x: first_true(ts) for x, ts in raw_trig.items() if ts}
        # De-duplicate SMH and SOXX if both trigger at same minute
        if "SMH" in trig and "SOXX" in trig:
            trig["SOXX"] = [t for t in trig["SOXX"] if t not in set(trig["SMH"])]

        out["TRIG"][hyp] = {x: list(ts) for x, ts in trig.items() if ts}

        if mode == "count":
            # In count mode, simply record trigger counts without touching forward prices
            for x, ts in trig.items():
                st[f"{hyp}_count"] += len(ts)
            continue

        # Run mode: execute trades & controls
        for x, ts in trig.items():
            rt = costs.get(x, {}).get("rt_bp", 2.0)
            stres = costs.get(x, {}).get("stress_bp", 4.0)
            for t in ts:
                net = trade(E[x]["o"], E[x]["c"], t, H, rt)
                if net is None:
                    st[f"{hyp}_skipped"] += 1
                    continue

                # Primary control: 5 seeded other ETFs at same minute t
                # Exclude ETFs that triggered in [t-15, t]
                dup = {"SMH": "SOXX", "SOXX": "SMH"}.get(x)
                exclude_etfs = {x}
                if dup:
                    exclude_etfs.add(dup)
                pool_ca = [
                    other for other in ALL_ETF
                    if other in E and other not in exclude_etfs
                    and not any(t - 15 <= pt <= t for pt in trig.get(other, []))
                ]
                ca_net = None
                if pool_ca:
                    rng_ca = seeded("ca", hyp, d, x, t)
                    k_draw = min(NDRAW, len(pool_ca))
                    chosen = rng_ca.sample(pool_ca, k_draw)
                    vals = [
                        trade(E[o]["o"], E[o]["c"], t, H, costs.get(o, {}).get("rt_bp", 2.0))
                        for o in chosen
                    ]
                    valid = [v for v in vals if v is not None]
                    if valid:
                        ca_net = float(np.mean(valid))

                # Secondary control: 5 seeded later minutes on same ETF
                pool_cb = [
                    m for m in range(max(FIRST, t + H + 2), min(LAST, t + H + 62) + 1)
                    if all(not (pt <= m <= pt + H + 1) for pt in ts if pt <= m)
                ]
                cb_net = None
                if pool_cb:
                    rng_cb = seeded("cb", hyp, d, x, t)
                    k_draw = min(NDRAW, len(pool_cb))
                    chosen_m = rng_cb.sample(pool_cb, k_draw)
                    vals = [trade(E[x]["o"], E[x]["c"], m, H, rt) for m in chosen_m]
                    valid = [v for v in vals if v is not None]
                    if valid:
                        cb_net = float(np.mean(valid))

                out[hyp].append({
                    "d": d, "sym": x, "t": t, "net": net,
                    "net_s": net + rt - stres,
                    "ca": ca_net, "cb": cb_net
                })

                # Baseline row
                rng_base = seeded("baseline", hyp, d, x, t)
                b_etf = rng_base.choice(ALL_ETF)
                if b_etf in E:
                    b_net = trade(E[b_etf]["o"], E[b_etf]["c"], t, H, costs.get(b_etf, {}).get("rt_bp", 2.0))
                    if b_net is not None:
                        out["BASE"].append({"key": hyp, "d": d, "sym": b_etf, "t": t, "net": b_net})

    return out, st


# ---------------- evaluation ----------------
def evaluate(rows, min_days=60, min_half=25, min_n=300, min_half_n=100):
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

    under = len(rows) < min_n or min(n_h) < min_half_n or len(net) < min_days or min(d_h) < min_half or not lv
    hl, hw, hn = h(lift), h(win), h(net)
    pos = lambda x: x == x and x > 0
    edge = (t_l >= crit and pos(hl[0]) and pos(hl[1]) and bool(liftb) and np.mean(list(liftb.values())) > 0 and pos(hw[0]) and pos(hw[1]))
    prof = edge and pos(hn[0]) and pos(hn[1]) and bool(nets) and np.mean(list(nets.values())) > 0
    status = "UNDERPOWERED" if under else ("PROFITABLE" if prof else ("EDGE" if edge else "FAIL"))

    cas = [r["ca"] for r in rows if r.get("ca") is not None]
    cbs = [r["cb"] for r in rows if r.get("cb") is not None]
    return {
        "status": status, "n": len(rows), "n_h": n_h, "days": len(lv), "d_h": d_h, "crit": crit,
        "lift": float(np.mean(lv)) if lv else float("nan"), "lift_t": t_l, "lift_h": hl,
        "liftb": float(np.mean(list(liftb.values()))) if liftb else float("nan"),
        "win_h": hw, "net": float(np.mean(list(net.values()))), "net_h": hn,
        "net_stress": float(np.mean(list(nets.values()))) if nets else float("nan"),
        "ca_mean": float(np.mean(cas)) if cas else float("nan"), "cb_mean": float(np.mean(cbs)) if cbs else float("nan")
    }


def report():
    out_pkl = OUT / "run.pkl"
    if not out_pkl.exists():
        raise SystemExit("run.pkl does not exist: run the study first")
    z = pickle.load(open(out_pkl, "rb"))
    res, stats, scored_days = z["res"], z["stats"], z["scored_days"]

    lines = [
        "# ETF Cluster Thrust (docs/studies/etf_cluster_thrust_prereg.json)",
        "",
        "Tests whether a synchronized momentum thrust in sector constituents (>= 2 or >= 3 stocks with res1 >= +15 bp, vol_ratio >= 2.0) ",
        "predicts a forward impulse in the liquid sector ETF (QQQ, SMH, SOXX, XBI, etc.) over H=3 or H=5 minutes.",
        f"Standard: docs/TEST_STANDARD.md. 120-session SIP warehouse sample (2026-04-27 .. 2026-10-15), {scored_days} scored sessions.",
        "",
        "## Summary of Verdicts",
        "",
        "| Hypothesis | K (Stocks) | Hold (min) | Status | Triggers | Lift vs Control | Day t-stat (crit) | Net (after ETF spread) | Stress Net |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for k in KEYS:
        ev = evaluate(res[k])
        crit_str = f"{ev['crit']:.2f}" if not math.isnan(ev.get("crit", float("nan"))) else "N/A"
        t_str = f"{ev['lift_t']:.2f}" if not math.isnan(ev.get("lift_t", float("nan"))) else "N/A"
        lift_str = f"{ev['lift']:+.2f} bp" if not math.isnan(ev.get("lift", float("nan"))) else "N/A"
        net_str = f"{ev['net']:+.2f} bp" if not math.isnan(ev.get("net", float("nan"))) else "N/A"
        stress_str = f"{ev['net_stress']:+.2f} bp" if not math.isnan(ev.get("net_stress", float("nan"))) else "N/A"
        lines.append(f"| {k} | {HYP_CONFIG[k]['K']} | {HYP_CONFIG[k]['H']} | **{ev['status']}** | {ev['n']} | {lift_str} | {t_str} ({crit_str}) | {net_str} | {stress_str} |")

    lines.append("")
    lines.append("## Detailed Hypothesis Breakdown")
    lines.append("")

    for k in KEYS:
        ev = evaluate(res[k])
        lines.append(f"### {k}: {ev['status']}")
        if ev.get("n"):
            lines.append(f"- **Sample size:** n = {ev['n']} (halves: {ev['n_h'][0]} / {ev['n_h'][1]}), scored days = {ev['days']} (halves: {ev['d_h'][0]} / {ev['d_h'][1]})")
            lines.append(f"- **Primary Control Lift:** {ev['lift']:+.2f} bp (t = {ev['lift_t']:.2f} vs critical {ev['crit']:.2f}), halves: {ev['lift_h'][0]:+.2f} bp / {ev['lift_h'][1]:+.2f} bp")
            lines.append(f"- **Winsorized Lift (99% clip):** halves: {ev['win_h'][0]:+.2f} bp / {ev['win_h'][1]:+.2f} bp")
            lines.append(f"- **Secondary Control Lift (Later-only):** {ev['liftb']:+.2f} bp")
            lines.append(f"- **Net Return (after ETF spread):** {ev['net']:+.2f} bp (halves: {ev['net_h'][0]:+.2f} bp / {ev['net_h'][1]:+.2f} bp), at 2x stress: {ev['net_stress']:+.2f} bp")
            lines.append(f"- **Control Means:** Primary control = {ev['ca_mean']:+.2f} bp, Secondary control = {ev['cb_mean']:+.2f} bp")
        lines.append("")

    report_path = ROOT / "docs" / "ETF_CLUSTER_THRUST.md"
    report_path.write_text("\n".join(lines) + "\n")
    print(f"Wrote report to {report_path}")
    print("\n".join(lines))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "count"
    OUT.mkdir(parents=True, exist_ok=True)

    if mode == "report":
        return report()

    if mode == "run":
        if (OUT / "run.pkl").exists():
            raise SystemExit("run.pkl exists: the prereg allows ONE read (delete it only with operator approval)")
        if not (OUT / "count.json").exists():
            raise SystemExit("run the outcome-blind 'count' first")

    costs = load_costs()
    etf_files = {p.stem: p for p in WH_ETF.glob("20??-??-??.npz")}
    etf_cache = {}

    counts = defaultdict(int)
    stats = defaultdict(int)
    res = {k: [] for k in KEYS + ("BASE",)}

    print(f"Running etf_cluster_thrust in mode: {mode}")
    n_days = 0
    for d, day, prev, hist, names in iter_days(WH_SIP):
        ed, eh = etf_context(etf_files, etf_cache, d, hist)
        o, st = score_day(day, prev, hist, names, d, ed, eh, costs, mode=mode)
        for k, v in st.items():
            stats[k] += v
        for k in KEYS:
            counts[k] += len(o["TRIG"][k]) if mode == "count" else len(o[k])
            if mode == "run":
                res[k] += o[k]
        if mode == "run":
            res["BASE"] += o["BASE"]

        n_days += 1
        if n_days % 10 == 0:
            print(f"Day {n_days}: {d} counts={dict(counts)}", flush=True)

    print(f"Total days: {n_days}")
    print(f"Counts: {dict(counts)}")

    if mode == "count":
        cj = {
            "status": "COUNT_COMPLETE",
            "scored_days": n_days,
            "counts": dict(counts),
            "stats": dict(stats),
            "power_ok": {k: counts[k] >= 300 for k in KEYS}
        }
        (OUT / "count.json").write_text(json.dumps(cj, indent=2))
        print("Wrote count.json successfully.")

    elif mode == "run":
        out_pkl = OUT / "run.pkl"
        pickle.dump({"res": res, "stats": dict(stats), "scored_days": n_days}, open(out_pkl, "wb"))
        print(f"Wrote {out_pkl}")
        report()


if __name__ == "__main__":
    main()
