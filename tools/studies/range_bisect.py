#!/usr/bin/env python3
"""Enter only the lower half of a between-levels range. Live desk functions.

Rules: docs/studies/range_bisect_prereg.json. The sideways definition is
flat_conjunction.is_sideways and is not refit here. Position is
ai_entry_watch._range_shape_fields rs_pos15 (0 at the low, 1 at the high).

  --score   one read. Refuses unless the canary passes.
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import random
import sys

ROOT = os.environ.get("TH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flat_conjunction as fc  # noqa: E402

MID = 0.5


def is_lower(feat: dict) -> bool:
    """Sideways, and the live price is between the midpoint and the low."""
    pos = feat.get("rs_pos15")
    return bool(fc.is_sideways(feat) and pos is not None and float(pos) <= MID)


def is_upper(feat: dict) -> bool:
    """The other half of the same range. Not an entry."""
    pos = feat.get("rs_pos15")
    return bool(fc.is_sideways(feat) and pos is not None and float(pos) > MID)


def mark(rows: list[dict]) -> list[dict]:
    """Tag the lower and upper half. Full feature rows use the live rule.

    Scorer rows that already carry sideways and rs_pos15, and not the range
    fields, are tagged from those two values. That is the canary path.
    """
    out = []
    for r in rows:
        c = dict(r)
        pos = r.get("rs_pos15")
        if "rs_rng15_bp" in r:
            c["lower"] = is_lower(r)
            c["upper"] = is_upper(r)
        else:
            side = bool(r.get("sideways"))
            c["lower"] = bool(side and pos is not None and float(pos) <= MID)
            c["upper"] = bool(side and pos is not None and float(pos) > MID)
        out.append(c)
    return out


def _daily(rows: list[dict]) -> list[float]:
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["d"], []).append(r)
    diffs = []
    for group in by.values():
        lo = [r["gross"] for r in group if r["lower"]]
        up = [r["gross"] for r in group if r["upper"]]
        if len(lo) < 5 or len(up) < 5:
            continue
        diffs.append(fc._mean(lo) - fc._mean(up))
    return diffs


def _boot(rows: list[dict], rng: random.Random) -> dict:
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["d"], []).append(r)
    names = list(by)
    pay, ctl = [], []
    for _ in range(fc.BOOT):
        drawn = [r for d in (rng.choice(names) for _ in names) for r in by[d]]
        lo = [r for r in drawn if r["lower"]]
        if not lo:
            continue
        all_m = fc._mean([r["gross"] for r in drawn])
        pay.append(fc._mean([r["gross"] for r in lo]) - all_m)
        sk = set()
        for d in {r["d"] for r in drawn}:
            dr = [r for r in drawn if r["d"] == d]
            n_lo = sum(1 for r in dr if r["lower"])
            sk.update(id(r) for r in rng.sample(dr, min(len(dr), n_lo)))
        kept = [r for r in drawn if id(r) not in sk]
        if kept:
            ctl.append(fc._mean([r["gross"] for r in kept]) - all_m)

    def q(v, p):
        if not v:
            return float("nan")
        return sorted(v)[min(len(v) - 1, int(len(v) * p))]

    return {"pay_p5": q(pay, 0.05), "pay_med": q(pay, 0.50), "rand_med": q(ctl, 0.50)}


def _side(rows: list[dict], days: set[str], key: str) -> dict:
    xs = [r for r in rows if r["d"] in days and r[key]]
    return {"n": len(xs), "days": len({r["d"] for r in xs}),
            "gross": fc._mean([r["gross"] for r in xs]),
            "net4": fc._mean([r["gross"] - fc.COST_BP for r in xs]),
            "net8": fc._mean([r["gross"] - fc.STRESS_BP for r in xs]),
            "ng": fc._mean([1.0 if r["ng"] else 0.0 for r in xs])}


def summarize(rows: list[dict], day_list: list[str]) -> dict:
    a_days, b_days = fc.halves(day_list)

    def half_power(days):
        sub = [r for r in rows if r["d"] in days]
        by: dict[str, list[dict]] = {}
        for r in sub:
            by.setdefault(r["d"], []).append(r)
        contributing = sum(1 for g in by.values()
                           if sum(1 for r in g if r["lower"]) >= 5 and sum(1 for r in g if r["upper"]) >= 5)
        return contributing, sum(1 for r in sub if r["lower"])

    pa, fa = half_power(a_days)
    pb, fb = half_power(b_days)
    powered = pa >= 20 and pb >= 20 and fa >= 200 and fb >= 200
    diffs = {h: fc._day_t(_daily([r for r in rows if r["d"] in days]))
             for h, days in (("A", a_days), ("B", b_days))}
    wrows = fc._clip_top(rows)
    wdiffs = {h: fc._day_t(_daily([r for r in wrows if r["d"] in days]))
              for h, days in (("A", a_days), ("B", b_days))}
    lower = {h: _side(rows, days, "lower") for h, days in (("A", a_days), ("B", b_days))}
    upper = {h: _side(rows, days, "upper") for h, days in (("A", a_days), ("B", b_days))}
    all_m = {h: fc._mean([r["gross"] for r in rows if r["d"] in days]) for h, days in (("A", a_days), ("B", b_days))}
    separates = all(diffs[h] >= fc.T_BAR and wdiffs[h] > 0 for h in ("A", "B"))
    beats_all = all(lower[h]["gross"] > all_m[h] for h in ("A", "B"))
    pays_net = all(lower[h]["net4"] > 0 and lower[h]["net8"] > 0 for h in ("A", "B"))
    boot = _boot(rows, random.Random(fc.SEED))
    pays_boot = boot["pay_p5"] > 0 and boot["pay_med"] > boot["rand_med"]
    if not powered:
        verdict = "UNDERPOWERED"
    elif separates and beats_all and pays_net and pays_boot:
        verdict = "PROFITABLE"
    elif separates and beats_all:
        verdict = "EDGE"
    else:
        verdict = "FAIL"
    return {
        "verdict": verdict, "powered": powered,
        "half_days": {"A": pa, "B": pb}, "half_lower": {"A": fa, "B": fb},
        "lower": lower, "upper": upper, "all": all_m,
        "day_t": diffs, "day_t_winsor": wdiffs, "bootstrap": boot,
        "n": len(rows), "n_lower": sum(1 for r in rows if r["lower"]),
        "n_upper": sum(1 for r in rows if r["upper"]),
    }


def canary() -> None:
    """A planted gap on the lower half must SEPARATE. A zero plant must not be profitable."""
    days = [f"2026-01-{d:02d}" for d in range(1, 41)]
    rows = []
    rng = random.Random(1)
    for d in days:
        for k in range(30):
            if k < 10:
                pos, gross_add = 0.2, 8.0
            elif k < 20:
                pos, gross_add = 0.8, 0.0
            else:
                pos, gross_add = 0.2, 0.0
            gross = rng.uniform(-5, 5) + gross_add
            rows.append({"d": d, "sideways": k < 20, "rs_pos15": pos, "gross": gross, "ng": False})
    good = summarize(mark(rows), days)
    if good["verdict"] not in ("EDGE", "PROFITABLE") or good["day_t"]["A"] < fc.T_BAR:
        raise SystemExit(f"canary failed to see a planted lower-half gap: {good['verdict']} t={good['day_t']}")
    null_rows = []
    rng = random.Random(2)
    for d in days:
        for k in range(30):
            pos = 0.2 if k < 10 else 0.8
            null_rows.append({"d": d, "sideways": k < 20, "rs_pos15": pos,
                              "gross": rng.uniform(-5, 5), "ng": False})
    bad = summarize(mark(null_rows), days)
    if bad["verdict"] == "PROFITABLE":
        raise SystemExit("canary null plant scored PROFITABLE")
    print(f"canary ok planted {good['verdict']} t={good['day_t']['A']:.2f}/{good['day_t']['B']:.2f} "
          f"null {bad['verdict']}", file=sys.stderr)


def desk_info() -> dict:
    """November watch entries. Information only."""
    rows = []
    for day in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"):
        path = os.path.join(fc.ROOT, "ai_reports", "tapes", day, "outcomes.jsonl")
        cache_path = os.path.join(fc.CACHE, f"bars_v2_{day}.pkl")
        if not (os.path.exists(path) and os.path.exists(cache_path)):
            continue
        bars = pickle.load(open(cache_path, "rb"))
        seen = set()
        for line in open(path):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
            if o.get("entry_path") != "watch" or not (isinstance(e, (int, float)) and x and t) or e < 20:
                continue
            key = (o.get("symbol"), round(float(t)))
            if key in seen:
                continue
            seen.add(key)
            iex = (bars.get(o["symbol"]) or {}).get("iex") or []
            if len(iex) < 140:
                continue
            feat = fc.live_features(list(iex), float(e), float(t))
            if feat is None or feat.get("rs_pos15") is None:
                continue
            post = [r for r in iex if float(t) < r[0] <= float(t) + 300]
            if len(post) < 3:
                continue
            rows.append({"d": day, "lower": is_lower(feat), "upper": is_upper(feat),
                         "gross": (float(x) - float(e)) / float(e) * 1e4,
                         "ng": max(r[2] for r in post) < float(e) * 1.0005})
    if not rows:
        return {"verdict": "INFORMATION", "n": 0}
    lo = [r for r in rows if r["lower"]]
    up = [r for r in rows if r["upper"]]
    return {"verdict": "INFORMATION", "n": len(rows), "n_lower": len(lo), "n_upper": len(up),
            "lower_gross": fc._mean([r["gross"] for r in lo]),
            "upper_gross": fc._mean([r["gross"] for r in up]),
            "days": sorted({r["d"] for r in rows})}


def run_score(out_path: str) -> dict:
    canary()
    window = [d for d in fc.iex_days() if d <= fc.SCORE_UNTIL]
    if len(window) < 3:
        raise SystemExit("not enough IEX sessions to score")
    rows = mark(fc.collect(window, score=True, label="bisect"))
    scored_days = [d for d, _, _ in fc.iter_windows(window) if any(r["d"] == d for r in rows)]
    tape = summarize(rows, scored_days)
    desk = desk_info()
    payload = {"code": fc.code_id(), "mid": MID, "tape": tape, "desk": desk, "score_days": scored_days}
    with open(out_path, "w") as f:
        json.dump(payload, f)
    print(json.dumps({"verdict": tape["verdict"], "day_t": tape["day_t"], "n": tape["n"],
                      "n_lower": tape["n_lower"], "n_upper": tape["n_upper"],
                      "lower": tape["lower"], "upper": tape["upper"], "all": tape["all"],
                      "bootstrap": tape["bootstrap"], "desk": desk}, indent=1, default=str))
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--out", default="/tmp/range_bisect_score.json")
    args = ap.parse_args(argv)
    print(f"code {fc.code_id()} mid {MID}", file=sys.stderr)
    if args.score:
        run_score(args.out)
        return 0
    ap.error("pass --score")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
