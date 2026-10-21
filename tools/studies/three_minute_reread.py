#!/usr/bin/env python3
"""Three-minute re-read. Spec: docs/studies/three_minute_reread_prereg.json (as amended).

Do any of the entry rules (PRE, SQ, MID, BOTHUP, OSL, IGN, RSI2T, CON; video setups S3-S7) or the filters H1-H5 on
PRE pick entries whose next THREE minutes beat the same name's later 3 minutes (d) and a random other name's same 3
minutes (x), and pay after a 5 bp round trip? Discovery on the warehouse (2026-05-11 .. 10-15); any discovery PASS is
re-run unchanged on 60 earlier sessions (ai_reports/warehouse_confirm/, fetched with warehouse_fetch.py --out).

Timing (shared with order_flow_pilot): the onset (or setup) bar closes at the start of decision minute m; entry at
the SIP OPEN of m; exit at the SIP close of m+2 (3 one-minute bars). Lag 1 (entry at m+1) is information.

Built on indicator_confirm_hist (pipeline, features, controls' shape, grading, verdicts, null walk) and
early_entry_honest (rules, warm %R and RSI-2 lines, the live-shape CON series).

  (default)  count step: per rule / setup / filter and half: entries, name-days, days, projected MDE from CONTROL
             outcomes only -> count.json
  --null     the binding null (seed tag 'three-min-null', 120 sessions x 2,500 names): every rule, setup and filter
             >= 300 entries per half, every |t| within the per-df Bonferroni bound, H2/H3 investigate flags -> null.json
  --placebo  real data, coin-flip IN/OUT for the five filters -> placebo.json
  --score    discovery verdicts; refuses unless count / null / placebo were written by these exact modules and passed,
             and --skeptic-ready names the result-skeptic READY on this script
  --confirm  confirmation run on the earlier sessions, only for discovery PASS cells (reads result.json)
  --days N / --workers W as indicator_confirm_hist

Runs on the mini after the close (no Alpaca calls).
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
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicator_confirm_hist as H  # noqa: E402

S, E, BS = H.S, H.E, H.BS

# Module-level so every worker process (which re-imports this module) uses the same settings.
H.NULL_SEED_TAG = "three-min-null"        # prereg calibration: fresh seed family for this study's null
CONFIRM_DIR = os.path.join(E.ROOT, "ai_reports", "warehouse_confirm")
if os.environ.get("TMR_SAMPLE") == "confirm":
    H.WH = CONFIRM_DIR                    # H.load_raw reads H.WH at call time

OUT_DIR = os.path.join(E.ROOT, "ai_reports", "three_minute_reread")
RULES = ("PRE", "SQ", "MID", "BOTHUP", "OSL", "IGN", "RSI2T", "CON")
V_SETUPS = H.V_SETUPS
STANDALONE = RULES + V_SETUPS
FILTERS = H.CELLS
HOLD = 3
COST = 0.0005                             # required: the live 0.05% spread cap as a round trip
INFO_COSTS = (0.0003, 0.0010)
INFO_HOLDS = (1, 2, 5)
ONSET_LO, ONSET_HI = S.ARM_START - 1, 15 * 60 + 26    # decision m = onset + 1 in 10:00 .. 15:27
DISCOVERY = ("2026-05-11", "2026-10-15")
CONFIRM_BEFORE = "2026-04-27"
TIME_SPLIT = H.TIME_SPLIT
SAME_TAG, CROSS_TAG = "three-min-v1", "three-min-cross"


# ── rules, controls, outcomes ────────────────────────────────────────────────

def rule_minutes(ser: dict) -> dict[str, list[int]]:
    """Decision minutes (onset + 1, 10:00-15:27, 15-minute refractory on onsets, every RTH minute on the board) for
    each rule, from early_entry_honest's flags on the warm series; CON from the live-shape con_live_series."""
    m, f, s, rsi = ser["minutes"], ser["fast"], ser["slow"], ser["rsi"]
    n = len(m)
    vol_at = dict(zip(m, ser["vol"]))
    flags = {
        "PRE": [E.pre_flag(f, s, i) for i in range(n)],
        "SQ": [E.sq_flag(f, s, i) for i in range(n)],
        "MID": [E.mid_flag(f, s, rsi, i) for i in range(n)],
        "BOTHUP": [E.bothup_flag(f, s, i) for i in range(n)],
        "OSL": [E.os_leave_flag(f, s, i) for i in range(n)],
        "IGN": [E.ign_flag(m, ser["close"], ser["vol"], i, vol_at) for i in range(n)],
        "RSI2T": [E.rsi2_turn_flag(f, rsi, i) for i in range(n)],
    }
    out = {}
    for r, fl in flags.items():
        on, _off = E.fires(m, fl, set(m), start=ONSET_LO, end=ONSET_HI, refract=H.REFRACT)
        out[r] = [x + 1 for x in on]
    cm = ser["con_minutes"]
    on, _off = E.fires(cm, ser["con_flags"], set(cm), start=ONSET_LO, end=ONSET_HI, refract=H.REFRACT)
    out["CON"] = [x + 1 for x in on]
    return out


def _ok(sip: dict, c: int, hold: int = HOLD) -> bool:
    """Entry at the open of c, exit at the close of c + hold - 1, inside RTH."""
    return c + hold - 1 < S.RTH_END and c in sip and (c + hold - 1) in sip


def control_minute(sym: str, day: str, m: int, sip: dict, hold: int = HOLD, lo: int = 1) -> int | None:
    elig = [c for c in range(m + lo, m + S.CTRL_SPAN + 1) if _ok(sip, c, hold)]
    if not elig:
        return None
    return random.Random(H._seed(sym, day, m, SAME_TAG)).choice(elig)


def cross_name(sym: str, day: str, m: int, pool: list[str], sipm, hold: int = HOLD, tries: int = 64) -> str | None:
    """A random OTHER pool name with bars at m and m + hold - 1 (rejection sampling, uniform over eligible names)."""
    if not pool:
        return None
    rng = random.Random(H._seed(sym, day, m, CROSS_TAG))

    def ok(n):
        return n != sym and _ok(sipm(n), m, hold)
    for _ in range(tries):
        n = pool[rng.randrange(len(pool))]
        if ok(n):
            return n
    elig = [n for n in pool if ok(n)]
    return rng.choice(elig) if elig else None


def net(sip: dict, m: int, hold: int = HOLD, lag: int = 0, cost: float = COST) -> float | None:
    return S.net(sip, m, hold=hold, lag=lag, cost=cost)


def excursion(bars: dict, m: int, hold: int = HOLD) -> tuple[float | None, float | None]:
    """Max favourable / adverse excursion over bars m .. m+hold-1 vs the open of m (what an exit could capture)."""
    if m not in bars or not bars[m][0]:
        return None, None
    rows = [bars[k] for k in range(m, m + hold) if k in bars]
    o = bars[m][0]
    return max(r[1] for r in rows) / o - 1, min(r[2] for r in rows) / o - 1


def outcomes(sipm: dict, m: int, c: int | None, xm: dict | None, full: bool, bars: dict | None = None) -> dict:
    o = {"arm": net(sipm, m), "ctrl": net(sipm, c) if c is not None else None,
         "cross": net(xm, m) if xm is not None else None}
    if not full:
        return o
    o["arm_lag1"] = net(sipm, m, lag=1)
    o["ctrl_lag1"] = net(sipm, c, lag=1) if c is not None else None
    for hld in INFO_HOLDS:
        o[f"arm_h{hld}"] = net(sipm, m, hold=hld)
        o[f"ctrl_h{hld}"] = net(sipm, c, hold=hld) if (c is not None and _ok(sipm, c, hld)) else None
    o["mfe"], o["mae"] = excursion(bars or {}, m)
    return o


# ── one session ──────────────────────────────────────────────────────────────

def process_day(args) -> dict:
    """mode: count (control outcomes only), placebo (PRE rows with outcomes), null / score (everything)."""
    k, days, src, mode = args
    day, prev = days[k], days[k - 1]
    prior_days = days[k - H.LOOKBACK:k]
    keep = set(prior_days) | {day}
    full = mode == "score"
    counts: Counter = Counter()
    sip = H.get_raw(src, "sip", day, keep)
    iex = H.get_raw(src, "iex", day, keep)
    p_iex = H.get_raw(src, "iex", prev, keep)
    cache: dict[str, dict] = {}

    def sipm(n):
        if n not in cache:
            cache[n] = H.sip_oc(sip[n]) if n in sip else {}
        return cache[n]

    def row(label, sym, m, pool):
        c = control_minute(sym, day, m, sipm(sym))
        if c is None:
            counts[f"{label}_no_control"] += 1
            return None
        r = {"day": day, "sym": sym, "m": m, "setup": label, "c": c}
        if mode == "count":
            r["ctrl"] = net(sipm(sym), c)
        else:
            x = cross_name(sym, day, m, pool, sipm)
            r["o"] = outcomes(sipm(sym), m, c, sipm(x) if x else None, full,
                              H.bars_dict(sip[sym]) if full else None)
        if full:
            r["early"] = m < TIME_SPLIT
        return r

    rows = []
    if mode != "placebo":
        prior_raw = [(d, H.get_raw(src, "sip", d, keep)) for d in prior_days]
        v_pool = [s for s in sorted(sip) if all(s in r for _d, r in prior_raw)]
        for sym in v_pool:
            ctx = H.v_context(sym, day, sip[sym], [(d, H.session60(src, d, sym, r[sym])) for d, r in prior_raw])
            for setup, m, _sig in H.v_entries(ctx, counts):
                r = row(setup, sym, m, v_pool)
                if r:
                    rows.append(r)
    names = H.sample_names(day, [s for s in iex if s in sip and s in p_iex])
    E.WARM_LINES = True
    for sym in names:
        ser = E.name_day_series(H.frame(day, iex[sym]), H.frame(prev, p_iex[sym][-E.CON_PRIOR_BARS:]))
        ind = S.indicators(H.bars_dict(iex[sym]))
        for rule, ms in rule_minutes(ser).items():
            if mode == "placebo" and rule != "PRE":
                continue
            for m in ms:
                counts[f"{rule}_onsets"] += 1
                r = row(rule, sym, m, names)
                if not r:
                    continue
                if rule == "PRE":
                    f = S.features_at(ind, m)
                    r["sides"] = {cell: S.cell_side(f, cell, m) for cell in FILTERS}
                rows.append(r)
    return {"day": day, "rows": rows, "counts": counts, "sample": len(names)}


def run_days(days: list[str], ks: list[int], src: str, mode: str, workers: int, quiet: bool = False) -> dict:
    rows, counts = [], Counter()
    tasks = [(k, days, src, mode) for k in ks]
    chunk = max(1, math.ceil(len(ks) / max(workers, 1)))
    if workers <= 1:
        results, ex = map(process_day, tasks), None
    else:
        ex = ProcessPoolExecutor(max_workers=workers)
        results = ex.map(process_day, tasks, chunksize=chunk)
    try:
        for res in results:
            rows += res["rows"]
            counts.update(res["counts"])
            if not quiet:
                print(f"  {res['day']}: sample {res['sample']}, entries {len(res['rows'])}", flush=True)
    finally:
        if ex is not None:
            ex.shutdown()
    return {"rows": rows, "counts": counts}


def pre_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["setup"] == "PRE" and "sides" in r]


# ── count step ───────────────────────────────────────────────────────────────

def count_labels(rows: list[dict], labels, half_end: str | None = None) -> dict:
    """Per standalone label and half: entries, name-days, days, projected MDE from control net3 (outcome-blind)."""
    out = {}
    for lab in labels:
        out[lab] = {}
        for half in ("A", "B"):
            ctl, names = defaultdict(list), set()
            for r in rows:
                if r["setup"] == lab and H.half_of(r["day"], half_end) == half and r.get("ctrl") is not None:
                    ctl[r["day"]].append(r["ctrl"])
                    names.add((r["sym"], r["day"]))
            means = [statistics.mean(v) for v in ctl.values()]
            n = sum(len(v) for v in ctl.values())
            se = statistics.stdev(means) / math.sqrt(len(means)) if len(means) >= 2 else None
            tc = _t_crit18(len(means))
            mde = (tc + 0.84) * math.sqrt(2) * se if se is not None and tc is not None else None
            out[lab][half] = {"n": n, "namedays": len(names), "days": len(means), "t_crit": tc,
                              "projected_mde": mde, "scored": n >= H.MIN_ENTRIES and len(means) >= H.MIN_DAYS}
    return out


def count_filters(rows: list[dict]) -> dict:
    """indicator_confirm_hist.count_h, with the projected MDE rescaled from its Bonferroni-5 t_crit to this prereg's
    family of 18 (same SE, outcome-blind)."""
    out = H.count_h(pre_rows(rows))
    for cell in out.values():
        for c in cell.values():
            t5, t18 = c["t_crit"], _t_crit18(c["days_both_sides"])
            if c["projected_mde"] is not None and t5 is not None and t18 is not None:
                c["projected_mde"] *= (t18 + 0.84) / (t5 + 0.84)
            c["t_crit"] = t18
    return out


def _t_crit18(days: int) -> float | None:
    """Prereg multiplicity: one family of 18, one-sided p 0.025 / 18 at df = days - 1."""
    return S.t_ppf(1 - 0.025 / 18, days - 1) if days >= 2 else None


# ── grading ──────────────────────────────────────────────────────────────────

def grade_standalone(rows, label, half, half_end=None):
    g = H.v_grade(rows, label, half, half_end)
    for k in ("d", "x"):
        g[k]["t_crit"] = _t_crit18(g[k]["days"])
    return g


def grade_filter(rows, cell, half, half_end=None, side_fn=None):
    g = H.grade_h(pre_rows(rows), cell, half, half_end, side_fn)
    for k in ("d", "x"):
        g[k]["t_crit"] = _t_crit18(g[k]["days"])
    return g


def checks(rows: list[dict], half_end: str | None, side_fns: dict | None = None, standalone: bool = True) -> list:
    out = []
    for cell in FILTERS:
        for half in ("A", "B"):
            g = grade_filter(rows, cell, half, half_end, (side_fns or {}).get(cell))
            out += [H._check(cell, half, "DiD_d", g["d"]["did"], g["d"]["did_t"], g["d"]["n_in"], g["d"]["days"]),
                    H._check(cell, half, "DiD_x", g["x"]["did"], g["x"]["did_t"], g["x"]["n_in"], g["x"]["days"])]
    for lab in STANDALONE if standalone else ():
        for half in ("A", "B"):
            g = grade_standalone(rows, lab, half, half_end)
            out += [H._check(lab, half, "lift_d", g["d"]["lift"], g["d"]["t"], g["d"]["n"], g["d"]["days"]),
                    H._check(lab, half, "lift_x", g["x"]["lift"], g["x"]["t"], g["x"]["n"], g["x"]["days"])]
    return out


def verdicts(rows: list[dict], count: dict) -> dict:
    graded, halves, final = {}, {}, {}
    for lab in STANDALONE + FILTERS:
        graded[lab], halves[lab] = {}, {}
        for half in ("A", "B"):
            is_f = lab in FILTERS
            g = grade_filter(rows, lab, half) if is_f else grade_standalone(rows, lab, half)
            scored = count["filters" if is_f else "standalone"][lab][half]["scored"]
            if not scored:
                halves[lab][half] = "UNDERPOWERED"
                n_ = g["d"]["n_in"] if is_f else g["d"]["n"]
                g = {"masked": True, "n": n_, "days": g["d"]["days"]}
            else:
                halves[lab][half] = H.h_half_verdict(g, lab) if is_f else H.v_half_verdict(g)
            graded[lab][half] = g
        ub = [((graded[lab][h].get("ub_d"), graded[lab][h].get("ub_x")) if lab in FILTERS else
               (graded[lab][h].get("d", {}).get("ub"), graded[lab][h].get("x", {}).get("ub"))) for h in ("A", "B")]
        final[lab] = H.cell_verdict([halves[lab][h] for h in ("A", "B")], ub)
    return {"graded": graded, "halves": halves, "verdicts": final}


def confirm_half(g: dict, is_filter: bool, k: int) -> str:
    """Prereg confirmation bar: floors; > 0 on d AND x at one-sided p 0.025 / k (df = days - 1); net > 0 at 5 bp;
    name-day > 0; drop-3 names / days >= 0."""
    gd, gx = g["d"], g["x"]
    n_key, est, t_key = ("n_in", "did", "did_t") if is_filter else ("n", "lift", "t")
    if any(x[n_key] < H.MIN_ENTRIES or x["days"] < H.MIN_DAYS or x[t_key] is None for x in (gd, gx)):
        return "UNDERPOWERED"
    tc = lambda x: S.t_ppf(1 - 0.025 / max(k, 1), x["days"] - 1)  # noqa: E731
    net_ = gd["in_net"] if is_filter else gd["net"]
    nd = gd["did_nameday"] if is_filter else gd["lift_nameday"]
    dn, dd = ((gd["did_drop_names"], gd["did_drop_days"]) if is_filter else (gd["drop_names"], gd["drop_days"]))
    ok = (gd[est] > 0 and gd[t_key] >= tc(gd) and gx[est] > 0 and gx[t_key] >= tc(gx) and net_ is not None
          and net_ > 0 and nd is not None and nd > 0 and dn is not None and dn >= 0 and dd is not None and dd >= 0)
    return "PASS" if ok else "NOT_PASSED"


# ── information ──────────────────────────────────────────────────────────────

def info_tables(rows: list[dict], count: dict) -> dict:
    """Information only: lag-1 entry, holds 1/2/5, 3 and 10 bp nets, excursions, time split."""
    def mean_day(vals_by_day):
        return H._day_t({d: statistics.mean(v) for d, v in vals_by_day.items() if v})
    out = {}
    lines = ["| entry | half | lag1 lift d (t) | h1 lift d | h2 lift d | h5 lift d | net3 @3bp | net3 @10bp | "
             "MFE | MAE | before 11:25 lift d | after |", "|" + "---|" * 12]
    for lab in STANDALONE:
        for half in ("A", "B"):
            rs = [r for r in rows if r["setup"] == lab and H.half_of(r["day"]) == half and "o" in r]
            if not count["standalone"][lab][half]["scored"]:
                lines.append(f"| {lab} | {half} | not scored |" + " |" * 9)
                continue

            def lift(a, c, pred=lambda r: True):
                per = defaultdict(list)
                for r in rs:
                    if pred(r) and r["o"].get(a) is not None and r["o"].get(c) is not None:
                        per[r["day"]].append(r["o"][a] - r["o"][c])
                mu, t, _ = mean_day(per)
                return f"{H._bp(mu)} ({H._t(t)})"
            nets = defaultdict(list)
            mfe, mae = defaultdict(list), defaultdict(list)
            for r in rs:
                if r["o"]["arm"] is not None:
                    nets[r["day"]].append(r["o"]["arm"])
                if r["o"].get("mfe") is not None:
                    mfe[r["day"]].append(r["o"]["mfe"])
                    mae[r["day"]].append(r["o"]["mae"])
            n5 = mean_day(nets)[0]
            lines.append(f"| {lab} | {half} | {lift('arm_lag1', 'ctrl_lag1')} | {lift('arm_h1', 'ctrl_h1')} | "
                         f"{lift('arm_h2', 'ctrl_h2')} | {lift('arm_h5', 'ctrl_h5')} | "
                         f"{H._bp(None if n5 is None else n5 + COST - INFO_COSTS[0])} | "
                         f"{H._bp(None if n5 is None else n5 + COST - INFO_COSTS[1])} | {H._bp(mean_day(mfe)[0])} | "
                         f"{H._bp(mean_day(mae)[0])} | {lift('arm', 'ctrl', lambda r: r['early'])} | "
                         f"{lift('arm', 'ctrl', lambda r: not r['early'])} |")
    out["standalone entries"] = lines
    return out


def desk_fills_markout(days: list[str]) -> list[str]:
    """Information: the desk's own paper fills (outcomes.jsonl entry_time, watch path) at 1/2/3/5 minutes, entry at
    the open of the minute after the fill minute, from the warehouse SIP bars (names in the warehouse only)."""
    fills = E.desk_fills(set(days))
    per = {h: defaultdict(list) for h in (1, 2, 3, 5)}
    missing = 0
    by_day = defaultdict(list)
    for (sym, day), tss in fills.items():
        by_day[day] += [(sym, ts) for ts in tss]
    for day, items in sorted(by_day.items()):
        sip = H.load_raw("sip", day)
        for sym, ts in items:
            if sym not in sip:
                missing += 1
                continue
            sm, m = H.sip_oc(sip[sym]), E._et_min(ts)
            for h in per:
                v = net(sm, m, hold=h, lag=1, cost=0.0)
                if v is not None:
                    per[h][day].append(v)
    L = ["| hold | fills | days | gross mean (t) |", "|---|---|---|---|"]
    for h, d in per.items():
        mu, t, nd = H._day_t({k: statistics.mean(v) for k, v in d.items() if v})
        L.append(f"| {h} min | {sum(len(v) for v in d.values())} | {nd} | {H._bp(mu)} ({H._t(t)}) |")
    return L + ["", f"fills on names not in the warehouse: {missing}"]


# ── gates ────────────────────────────────────────────────────────────────────

def script_sha() -> str:
    return hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()


def module_shas() -> dict[str, str]:
    m = H.module_shas()
    m["three_minute_reread"] = script_sha()
    return m


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    with open(p, "w") as f:
        if isinstance(obj, str):
            f.write(obj + "\n")
        else:
            json.dump(obj, f, indent=1, default=str)
    return p


def stamp(obj: dict) -> dict:
    obj["module_sha256"] = module_shas()
    return obj


def assert_discovery(sessions: list[str]) -> None:
    if not sessions or sessions[0] != DISCOVERY[0] or sessions[-1] != DISCOVERY[1]:
        raise SystemExit(f"discovery sessions must be exactly {DISCOVERY[0]} .. {DISCOVERY[1]}; got "
                         f"{sessions[:1]} .. {sessions[-1:]}")


def assert_confirm(days: list[str]) -> None:
    if not days or days[-1] >= CONFIRM_BEFORE:
        raise SystemExit(f"confirmation sessions must all be before {CONFIRM_BEFORE}; got .. {days[-1:]}")


def score_gate(ready: str, sessions: list[str]) -> dict:
    if not ready:
        raise SystemExit("--score needs --skeptic-ready (the result-skeptic READY on this script)")
    objs = {}
    for name in ("count.json", "null.json", "placebo.json"):
        p = os.path.join(OUT_DIR, name)
        if not os.path.exists(p):
            raise SystemExit(f"{name} missing: run its step first")
        obj = json.load(open(p))
        if obj.get("module_sha256") != module_shas():
            raise SystemExit(f"{name} was written by a different script or module version; rerun it")
        if name != "count.json" and not obj.get("ok"):
            raise SystemExit(f"{name} is NOT OK: the study is BLOCKED")
        objs[name] = obj
    nl = objs["null.json"]
    if nl.get("days") != H.NULL_DAYS_REGISTERED or nl.get("seed_tag") != H.NULL_SEED_TAG:
        raise SystemExit("null.json is not the registered 120-session 'three-min-null' run")
    if nl.get("investigate"):
        raise SystemExit(f"null.json flags {nl['investigate']}: investigate the method before any score")
    if objs["count.json"]["scored_sessions"] != sessions:
        raise SystemExit("the session set changed since the count step; rerun the count step")
    return objs["count.json"]


# ── report ───────────────────────────────────────────────────────────────────

def report(res: dict, sessions: list[str], title: str) -> str:
    L = [f"# Three-minute re-read: {title}", "",
         "Prereg: docs/studies/three_minute_reread_prereg.json. Entry at the open of m, exit at the close of m+2; "
         "d = vs the same name's later minute, x = vs a random other name at the same minute; net at 5 bp; bp per trade.",
         "", f"Sessions {sessions[0]} .. {sessions[-1]} ({len(sessions)}).", "",
         "| cell | half | n | days | lift/DiD d | t | lift/DiD x | t | t_crit | 97.5% UB d / x | net | half |",
         "|" + "---|" * 12]
    for lab, hs in res["graded"].items():
        for half, g in hs.items():
            if g.get("masked"):
                L.append(f"| {lab} | {half} | {g['n']} | {g['days']} | not scored |" + " |" * 6
                         + f" {res['halves'][lab][half]} |")
                continue
            d, x = g["d"], g["x"]
            if lab in FILTERS:
                L.append(f"| {lab} | {half} | {d['n_in']} | {d['days']} | {H._bp(d['did'])} | {H._t(d['did_t'])} | "
                         f"{H._bp(x['did'])} | {H._t(x['did_t'])} | {H._t(d['t_crit'])} | {H._bp(g['ub_d'])} / "
                         f"{H._bp(g['ub_x'])} | {H._bp(d['in_net'])} | {res['halves'][lab][half]} |")
            else:
                L.append(f"| {lab} | {half} | {d['n']} | {d['days']} | {H._bp(d['lift'])} | {H._t(d['t'])} | "
                         f"{H._bp(x['lift'])} | {H._t(x['t'])} | {H._t(d['t_crit'])} | {H._bp(d['ub'])} / "
                         f"{H._bp(x['ub'])} | {H._bp(d['net'])} | {res['halves'][lab][half]} |")
    L += ["", "| cell | verdict |", "|---|---|"] + [f"| {c} | **{v}** |" for c, v in res["verdicts"].items()]
    for t_, lines in res.get("info", {}).items():
        L += ["", f"### {t_} (information)", ""] + lines
    return "\n".join(L)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    for flag in ("--null", "--placebo", "--score", "--confirm"):
        g.add_argument(flag, action="store_true")
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--skeptic-ready", default="")
    a = ap.parse_args()
    if a.null:
        days = H.null_days()
        res = run_days(days, list(range(H.LOOKBACK, len(days))), "null", "null", a.workers, quiet=True)
        half_end = days[H.LOOKBACK + H.NULL_DAYS // 2 - 1]
        out = stamp(H.bound_check(checks(res["rows"], half_end)))
        out.update({"days": H.NULL_DAYS, "seed_tag": H.NULL_SEED_TAG, "entries": len(res["rows"]),
                    "half_end": half_end})
        print(f"null: {out['entries']} entries, {out['n_stats']} t values, max |t| {out['max_abs_t']:.2f}, missing "
              f"{out['missing']}, thin {out['thin']}, over {out['over']}, investigate {out['investigate']}: "
              f"{'OK' if out['ok'] else 'NOT OK - the study is BLOCKED (binding)'}")
        print(write("null.json", out))
        return
    if a.confirm:
        if os.environ.get("TMR_SAMPLE") != "confirm":
            raise SystemExit("run --confirm with TMR_SAMPLE=confirm (reads ai_reports/warehouse_confirm/)")
        days = H.warehouse_days()
        assert_confirm(days)
        disc = json.load(open(os.path.join(OUT_DIR, "result.json")))
        cells = [c for c, v in disc["verdicts"].items() if v == "PASS"]
        if not cells:
            print("no discovery PASS: nothing to confirm")
            return
        ks = list(range(H.LOOKBACK, len(days)))
        half_end = days[H.LOOKBACK + (len(ks) - 1) // 2]
        res = run_days(days, ks, "wh", "score", a.workers, quiet=True)
        out = {}
        for c in cells:
            hv = [confirm_half(grade_filter(res["rows"], c, h, half_end) if c in FILTERS
                               else grade_standalone(res["rows"], c, h, half_end), c in FILTERS, len(cells))
                  for h in ("A", "B")]
            out[c] = {"halves": hv, "lead": all(x == "PASS" for x in hv)}
        print(json.dumps(out, indent=1))
        write("confirm.json", stamp({"cells": out, "sessions": [days[k] for k in ks]}))
        return
    days = H.warehouse_days()
    ks = list(range(H.LOOKBACK, len(days)))
    if a.days:
        ks = ks[:a.days]
    sessions = [days[k] for k in ks]
    if not a.days:
        assert_discovery(sessions)
    print(f"warehouse {H.WH}: scored {len(sessions)} sessions {sessions[0]} .. {sessions[-1]}", flush=True)
    if a.placebo:
        res = run_days(days, ks, "wh", "placebo", a.workers, quiet=True)
        fns = {c: (lambda r, c=c: H.coin(r) if r["sides"][c] is not None else None) for c in FILTERS}
        out = stamp(H.bound_check(checks(res["rows"], None, fns, standalone=False)))
        print(f"placebo: {out['n_stats']} t values, max |t| {out['max_abs_t']:.2f}, over {out['over']}, thin "
              f"{out['thin']}: {'OK' if out['ok'] else 'NOT OK'}")
        print(write("placebo_smoke.json" if a.days else "placebo.json", out))
        return
    if a.score:
        count = score_gate(a.skeptic_ready, sessions)
        res = run_days(days, ks, "wh", "score", a.workers, quiet=True)
        out = verdicts(res["rows"], count)
        out["info"] = info_tables(res["rows"], count)
        out["info"]["desk fills (outcomes.jsonl) at 1/2/3/5 minutes"] = desk_fills_markout(sessions)
        md = report(out, sessions, "discovery")
        print(md)
        write("result.md", md)
        write("result.json", stamp({k: v for k, v in out.items() if k != "info"} | {"skeptic_ready": a.skeptic_ready}))
        return
    res = run_days(days, ks, "wh", "count", a.workers)
    cnt = {"standalone": count_labels(res["rows"], STANDALONE), "filters": count_filters(res["rows"])}
    for lab in STANDALONE:
        for half in ("A", "B"):
            c = cnt["standalone"][lab][half]
            print(f"  {lab} {half}: n {c['n']} name-days {c['namedays']} days {c['days']} MDE {H._bp(c['projected_mde'])} "
                  f"bp {'scored' if c['scored'] else 'UNDERPOWERED'}")
    for cell in FILTERS:
        for half in ("A", "B"):
            c = cnt["filters"][cell][half]
            print(f"  {cell} {half}: IN {c['n_in']} OUT {c['n_out']} days {c['days_both_sides']} "
                  f"MDE {H._bp(c['projected_mde'])} bp {'scored' if c['scored'] else 'UNDERPOWERED'}")
    print(f"counts: {dict(res['counts'])}")
    print(write("count_smoke.json" if a.days else "count.json",
                stamp({"scored_sessions": sessions, "counts": dict(res["counts"]), **cnt})))
    print("(count step only; no entry outcome read)")


if __name__ == "__main__":
    main()
