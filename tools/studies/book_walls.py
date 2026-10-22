#!/usr/bin/env python3
"""Book walls / room to run. Spec: docs/studies/book_walls_prereg.json (as amended).

On the 10/4 Nasdaq mbp-10 entries (random minutes on gappers), is an OPEN ask side (no large resting ask within 15 bp
above the touch) followed by a larger 5-minute rise than a WALLED one, and is bid SUPPORT (a large resting bid within
15 bp below the touch) followed by a larger rise than none? Primary = the name-day fixed-effect lift.

  --features   wall features at each entry's signal from the mbp-10 files (Python 3.9 dbn venv; no outcome)
  (default)    count step: group sizes, mixed name-days, unclassifiable shares, projected MDE from within-name-day
               label permutations (reads outcomes, never the feature-outcome link) -> count.json
  --score      verdicts; refuses without --skeptic-ready, matching module shas and a count step that cleared the
               power bar for at least one hypothesis
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import statistics
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(HERE))
DATA = os.path.join(ROOT, "data", "databento", "xnas_itch_mbp10_2026-06_08")
OUT_DIR = os.path.join(ROOT, "ai_reports", "book_walls")
NS = 1_000_000_000
STALE_S, LAT_S = 30, 0.5
WIN = 0.0015                     # 15 bp beyond the touch
WALL_X, WALL_USD = 4.0, 25_000.0
MIN_PX, MAX_SPREAD = 20.0, 0.0010
HORIZON, INFO_H = 300, (60, 120)
HALF_A_END = "2026-07-15"
LIFT_BAR, COST = 0.0005, 0.0005
N_TESTS = 2
FLOOR_MIXED, FLOOR_DAYS = 100, 20
PERM_TAG, N_PERM, N_PERM_COUNT = "book-walls-perm", 1000, 200
HYP = {"H1": ("OPEN", "WALLED"), "H2": ("SUPPORTED", "UNSUPPORTED")}


def P(*a):
    print(*a, flush=True)


def jload(path: str, default=None):
    return json.load(open(path)) if os.path.exists(path) else default


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def half_of(day: str) -> str:
    return "A" if day <= HALF_A_END else "B"


# ── features (pure on a snapshot; the dbn venv only loads the files) ─────────

def levels(row: dict, side: str) -> list:
    """[(price, size)] of the present levels 0..9 on `side` ('bid' / 'ask'), in book order."""
    out = []
    for k in range(10):
        p, s = row.get(f"{side}_px_{k:02d}"), row.get(f"{side}_sz_{k:02d}")
        if p is None or s is None or not math.isfinite(float(p)) or float(p) <= 0 or float(s) <= 0:
            break
        out.append((float(p), float(s)))
    return out


def side_read(lv: list, touch: float, up: bool, med: float) -> dict:
    """amended (1)-(3): window from the touch out WIN; classified only if the whole window is visible (fewer than 10
    present levels = the whole side is shown, or the 10th level lies at or beyond the edge); wall = size >= WALL_X x the
    snapshot median AND notional >= WALL_USD. `room` = bp from the touch to the first wall (None if none in view)."""
    edge = touch * (1 + WIN) if up else touch * (1 - WIN)
    inside = [(p, s) for p, s in lv if (p <= edge if up else p >= edge)]
    walls = [(p, s) for p, s in inside if s >= WALL_X * med and s * p >= WALL_USD]
    seen = [(p, s) for p, s in lv if s >= WALL_X * med and s * p >= WALL_USD]
    full = len(lv) < 10 or (lv[-1][0] >= edge if up else lv[-1][0] <= edge)
    room = abs(seen[0][0] / touch - 1) if seen else None
    return {"classified": bool(lv) and full, "wall": bool(walls), "room": room,
            "wall_px": walls[0][0] if walls else None, "wall_sz": walls[0][1] if walls else None}


def snapshot_features(row: dict) -> dict | None:
    bids, asks = levels(row, "bid"), levels(row, "ask")
    if not bids or not asks or bids[0][0] >= asks[0][0]:
        return None
    mid = (bids[0][0] + asks[0][0]) / 2
    med = statistics.median([s for _, s in bids + asks])
    return {"mid": mid, "spread": (asks[0][0] - bids[0][0]) / mid, "med": med,
            "ask": side_read(asks, asks[0][0], True, med), "bid": side_read(bids, bids[0][0], False, med)}


def pulled(before: dict | None, now_row: dict) -> bool | None:
    """Information: an ask wall in the window at S - 30 s whose price level is gone or under half its size at S."""
    if not before or not before["ask"]["wall"]:
        return None
    px, sz = before["ask"]["wall_px"], before["ask"]["wall_sz"]
    at = {p: s for p, s in levels(now_row, "ask")}
    return at.get(px, 0.0) < 0.5 * sz


def features_main() -> None:
    import databento as db
    old = json.load(open(os.path.join(DATA, "features.json")))["features"]
    out, drops = {}, Counter()
    for i, (eid, e) in enumerate(sorted(old.items())):
        path = os.path.join(DATA, "mbp10", f"{eid}.dbn.zst")
        if not os.path.exists(path):
            drops["no_file"] += 1
            continue
        df = db.DBNStore.from_file(path).to_df(pretty_ts=False, price_type="float").reset_index()
        ts = df["ts_recv"].values
        S = int(e["t_ns"])
        idx = (ts <= S).nonzero()[0]
        if not len(idx) or ts[idx[-1]] <= S - STALE_S * NS:
            drops["no_or_stale_book"] += 1
            continue
        row = df.iloc[idx[-1]].to_dict()
        f = snapshot_features(row)
        if f is None:
            drops["empty_or_crossed"] += 1
            continue
        j = (ts <= S - STALE_S * NS).nonzero()[0]
        before = snapshot_features(df.iloc[j[-1]].to_dict()) if len(j) else None
        f.update({"day": e["day"], "sym": e["sym"], "t_ns": S, "obi5_dm": e.get("obi5_dm"),
                  "pulled": pulled(before, row), "wall_30s_before": bool(before and before["ask"]["wall"])})
        out[eid] = f
        if (i + 1) % 1000 == 0:
            P(f"  {i + 1}/{len(old)}")
    P(f"features {len(out)}; drops {dict(drops)}")
    write("features.json", {"features": out, "drops": dict(drops), "module_sha256": module_shas()})


# ── groups, outcomes, statistics (desk venv) ─────────────────────────────────

def groups(f: dict) -> dict:
    a, b = f["ask"], f["bid"]
    return {"H1": (("WALLED" if a["wall"] else "OPEN") if a["classified"] else None),
            "H2": (("SUPPORTED" if b["wall"] else "UNSUPPORTED") if b["classified"] else None)}


def in_population(f: dict) -> bool:
    return f["mid"] >= MIN_PX and f["spread"] <= MAX_SPREAD


def outcome(q: dict, sym: str, t_ns: int, h: int) -> float | None:
    k0, k1 = f"{sym}|{t_ns + int(LAT_S * NS)}", f"{sym}|{t_ns + int(LAT_S * NS) + h * NS}"
    if k0 not in q or k1 not in q or not q[k0] or not q[k1]:
        return None
    m0, m1 = (q[k0][0] + q[k0][1]) / 2, (q[k1][0] + q[k1][1]) / 2
    return m1 / m0 - 1 if m0 > 0 else None


def net_outcome(q: dict, sym: str, t_ns: int, h: int) -> float | None:
    k0, k1 = f"{sym}|{t_ns + int(LAT_S * NS)}", f"{sym}|{t_ns + int(LAT_S * NS) + h * NS}"
    if k0 not in q or k1 not in q or not q[k0] or not q[k1] or q[k0][1] <= 0:
        return None
    return q[k1][0] / q[k0][1] - 1


def t_ppf(p: float, df: int) -> float:
    sys.path.insert(0, HERE)
    import indicator_confirm_study as S
    return S.t_ppf(p, df)


def day_stats(means: dict) -> tuple:
    xs = [v for v in means.values() if v is not None]
    if len(xs) < 2:
        return (xs[0] if xs else None), None, len(xs), None
    mu, sd = statistics.mean(xs), statistics.stdev(xs)
    se = sd / math.sqrt(len(xs))
    return mu, (mu / se if se > 0 else None), len(xs), (mu + t_ppf(0.975, len(xs) - 1) * se if se > 0 else None)


def fe_lift(rows: list[dict], hyp: str, key: str = "y", labels: dict | None = None) -> tuple:
    """amended (5): per mixed name-day, mean(favoured) - mean(other); per day the mean over its mixed name-days;
    day-equal. Returns (mu, t, days, ub, n_mixed, per_nameday)."""
    fav, oth = HYP[hyp]
    nd = defaultdict(lambda: defaultdict(list))
    for r in rows:
        g = (labels or {}).get(r["id"], r["g"][hyp])
        if g is not None and r.get(key) is not None:
            nd[(r["day"], r["sym"])][g].append(r[key])
    per_nd = {k: statistics.mean(v[fav]) - statistics.mean(v[oth]) for k, v in nd.items() if v[fav] and v[oth]}
    per_day = defaultdict(list)
    for (d, _s), x in per_nd.items():
        per_day[d].append(x)
    return (*day_stats({d: statistics.mean(v) for d, v in per_day.items()}), len(per_nd), per_nd)


def cs_lift(rows: list[dict], hyp: str, key: str = "y") -> tuple:
    """Information: per day mean(favoured) - mean(other) across all entries (days with >= 3 in each group)."""
    fav, oth = HYP[hyp]
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        g = r["g"][hyp]
        if g is not None and r.get(key) is not None:
            by[r["day"]][g].append(r[key])
    return day_stats({d: statistics.mean(v[fav]) - statistics.mean(v[oth]) for d, v in by.items()
                      if len(v[fav]) >= 3 and len(v[oth]) >= 3})[:3]


def permute(rows: list[dict], hyp: str, n: int, tag: str) -> list[float]:
    """amended (6): labels shuffled within name-day (seeded); the fixed-effect lift of each shuffle."""
    by = defaultdict(list)
    for r in rows:
        if r["g"][hyp] is not None and r.get("y") is not None:
            by[(r["day"], r["sym"])].append(r)
    keys = sorted(by)
    rng = random.Random(int(hashlib.sha256(f"{PERM_TAG}|{hyp}|{tag}".encode()).hexdigest(), 16))
    out = []
    for _ in range(n):
        lab = {}
        for k in keys:
            gs = [r["g"][hyp] for r in by[k]]
            rng.shuffle(gs)
            lab.update({r["id"]: g for r, g in zip(by[k], gs)})
        mu = fe_lift(rows, hyp, labels=lab)[0]
        if mu is not None:
            out.append(mu)
    return out


def quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo = math.floor(k)
    return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (k - lo)


def t_crit(days: int) -> float | None:
    return t_ppf(1 - 0.025 / N_TESTS, days - 1) if days >= 2 else None


def drop3(rows: list[dict], hyp: str, per_nd: dict) -> tuple:
    """Drop the 3 days with the largest day mean, and (separately) the 3 names with the largest summed share."""
    per_day = defaultdict(list)
    share = Counter()
    for (d, s), x in per_nd.items():
        per_day[d].append(x)
        share[s] += x
    dm = {d: statistics.mean(v) for d, v in per_day.items()}
    top_d = set(sorted(dm, key=lambda d: dm[d], reverse=True)[:3])
    top_n = {s for s, _ in share.most_common(3)}
    return (fe_lift([r for r in rows if r["day"] not in top_d], hyp)[0],
            fe_lift([r for r in rows if r["sym"] not in top_n], hyp)[0])


def obi_terciles(rows: list[dict], hyp: str, cuts: tuple) -> list:
    lo, hi = cuts
    out = []
    for f in (lambda v: v < lo, lambda v: lo <= v < hi, lambda v: v >= hi):
        out.append(fe_lift([r for r in rows if r.get("obi5_dm") is not None and f(r["obi5_dm"])], hyp)[0])
    return out


def grade(rows: list[dict], hyp: str, cuts: tuple, n_perm: int, tag: str) -> dict:
    mu, t, days, ub, n_mixed, per_nd = fe_lift(rows, hyp)
    fav = HYP[hyp][0]
    fy = defaultdict(list)
    for r in rows:
        if r["g"][hyp] == fav and r.get("y") is not None:
            fy[r["day"]].append(r["y"])
    fav_net = statistics.mean(statistics.mean(v) for v in fy.values()) - COST if fy else None
    perm = permute(rows, hyp, n_perm, tag)
    dd, dn = drop3(rows, hyp, per_nd)
    return {"lift": mu, "t": t, "days": days, "ub": ub, "mixed_namedays": n_mixed, "t_crit": t_crit(days),
            "fav_gross_minus_cost": fav_net, "perm_p975": quantile(perm, 0.975), "perm_sd":
                statistics.stdev(perm) if len(perm) >= 2 else None, "obi_terciles": obi_terciles(rows, hyp, cuts),
            "drop_days": dd, "drop_names": dn}


def half_verdict(g: dict) -> str:
    if g["t"] is not None and g["t"] <= -2:
        return "FAIL"
    if g["mixed_namedays"] < FLOOR_MIXED or g["days"] < FLOOR_DAYS or g["t"] is None:
        return "UNDERPOWERED"
    ok = (g["lift"] >= LIFT_BAR and g["t"] >= g["t_crit"]
          and g["fav_gross_minus_cost"] is not None and g["fav_gross_minus_cost"] > 0
          and g["perm_p975"] is not None and g["lift"] > g["perm_p975"]
          and all(x is not None and x >= 0 for x in g["obi_terciles"])
          and g["drop_days"] is not None and g["drop_days"] >= 0 and g["drop_names"] is not None and g["drop_names"] >= 0)
    return "PASS" if ok else "NOT_PASSED"


def final(halves: list[str], ubs: list, days: list[int]) -> str:
    if "FAIL" in halves:
        return "FAIL"
    if all(h == "PASS" for h in halves):
        return "PASS (LEAD)"
    if all(n >= 2 for n in days) and all(u is not None and u < LIFT_BAR for u in ubs):
        return "FAIL"
    return "UNDERPOWERED"


def module_shas() -> dict:
    p = os.path.abspath(__file__)
    return {"book_walls": hashlib.sha256(open(p, "rb").read()).hexdigest()}


def load_rows() -> tuple[list[dict], Counter, dict]:
    feats = jload(os.path.join(OUT_DIR, "features.json"))
    if not feats:
        raise SystemExit("run --features (dbn venv) first")
    q = json.load(open(os.path.join(DATA, "sip_quotes.json")))
    rows, st, uncls = [], Counter(), Counter()
    for eid, f in sorted(feats["features"].items()):
        if not in_population(f):
            st["below_$20" if f["mid"] < MIN_PX else "spread_over_10bp"] += 1
            continue
        g = groups(f)
        stratum = "$20-50" if f["mid"] < 50 else "$50+"
        for h in HYP:
            if g[h] is None:
                uncls[f"{h} {stratum}"] += 1
        r = {"id": eid, "day": f["day"], "sym": f["sym"], "half": half_of(f["day"]), "g": g, "obi5_dm": f.get("obi5_dm"),
             "pulled": f.get("pulled"), "room": f["ask"]["room"], "stratum": stratum,
             "y": outcome(q, f["sym"], f["t_ns"], HORIZON), "net": net_outcome(q, f["sym"], f["t_ns"], HORIZON)}
        for h in INFO_H:
            r[f"y{h}"] = outcome(q, f["sym"], f["t_ns"], h)
        if r["y"] is None:
            st["no_outcome"] += 1
        rows.append(r)
    return rows, st, {"unclassifiable": dict(uncls), "strata": dict(Counter(r["stratum"] for r in rows))}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--features", action="store_true")
    g.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    a = ap.parse_args()
    if a.features:
        features_main()
        return
    rows, st, info = load_rows()
    a_rows = [r for r in rows if r["half"] == "A" and r.get("obi5_dm") is not None]
    obis = sorted(r["obi5_dm"] for r in a_rows)
    cuts = (quantile(obis, 1 / 3), quantile(obis, 2 / 3))
    if not a.score:
        cnt = {"drops": dict(st), **info, "obi_cuts": cuts}
        for h in HYP:
            for half in ("A", "B"):
                rs = [r for r in rows if r["half"] == half]
                gc = Counter(r["g"][h] for r in rs if r["g"][h] is not None)
                nd = defaultdict(set)
                for r in rs:
                    if r["g"][h] is not None and r.get("y") is not None:
                        nd[(r["day"], r["sym"])].add(r["g"][h])
                mixed = [k for k, v in nd.items() if len(v) == 2]
                perm = permute(rs, h, N_PERM_COUNT, f"count-{half}")
                sd = statistics.stdev(perm) if len(perm) >= 2 else None
                mdays = len({d for d, _s in mixed})
                tc = t_crit(mdays)
                mde = (tc + 0.84) * sd if (sd and tc) else None
                cnt[f"{h} {half}"] = {"groups": dict(gc), "mixed_namedays": len(mixed), "days_with_mixed": mdays,
                                      "projected_mde": mde,
                                      "power_ok": mde is not None and mde <= LIFT_BAR and len(mixed) >= FLOOR_MIXED
                                      and mdays >= FLOOR_DAYS}
        cnt["module_sha256"] = module_shas()
        P(json.dumps(cnt, indent=1, default=str))
        P(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = jload(os.path.join(OUT_DIR, "count.json"))
    if not cnt or cnt.get("module_sha256") != module_shas():
        raise SystemExit("count.json missing or written by different code")
    res = {"verdicts": {}, "halves": {}, "graded": {}, "info": {}}
    for h in HYP:
        if not all(cnt[f"{h} {x}"]["power_ok"] for x in ("A", "B")):
            res["verdicts"][h] = "UNDERPOWERED (count step; not scored)"
            continue
        gs = {x: grade([r for r in rows if r["half"] == x], h, cuts, N_PERM, x) for x in ("A", "B")}
        res["graded"][h] = gs
        res["halves"][h] = {x: half_verdict(gs[x]) for x in ("A", "B")}
        res["verdicts"][h] = final(list(res["halves"][h].values()), [gs[x]["ub"] for x in ("A", "B")],
                                   [gs[x]["days"] for x in ("A", "B")])
        res["info"][h] = {x: {"cross_sectional": cs_lift([r for r in rows if r["half"] == x], h),
                              **{f"fe_{k}s": fe_lift([r for r in rows if r["half"] == x], h, key=f"y{k}")[:3]
                                 for k in INFO_H},
                              "fe_net": fe_lift([r for r in rows if r["half"] == x], h, key="net")[:3]}
                          for x in ("A", "B")}
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    P(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
