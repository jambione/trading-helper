#!/usr/bin/env python3
"""Trace why the historical universe misses or delays names the live desk seated.

hist_universe's calibration check (ai_reports/hist_universe/check.json, 2026-10-11) failed:
movers+tight recall 18.6% within 10 min (floor 70%), traded-name recall 34%. Of the recorded
movers names it missed on 10/05-10/09, all 152 were "never surfaced" - no refusal logged - so the
loss is silent. This wraps hist_universe's own functions (no logic change) and records, for each
target name and minute: which candidate lists it was on, what measure_row returned (row / discard
/ None and the numbers it used), and the seed and door-gate outcomes.

Targets per day: recorded movers/tight admissions (ai_reports/admit_range.jsonl) that the
calibration build missed or rebuilt > 10 min late.

    .venv/bin/python tools/studies/hist_universe_trace.py 2026-10-05 2026-10-09   # mini, after 16:15

Writes ai_reports/hist_universe/trace_<day>.json and prints a per-day summary of where each
target fell out. Read-only apart from those files (and hist_universe's own spread cache).
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hist_universe as HU  # noqa: E402

OUT = HU.OUT_DIR
WINDOW = 10.0


def targets_for(day: str) -> dict[str, dict]:
    rec = {k[1]: v for k, v in HU.recorded_admits(day, day).items()}
    calib = os.path.join(OUT, f"calib_{day}.json")
    recon = {a["symbol"]: a for a in json.load(open(calib))["admits"]} if os.path.exists(calib) else {}
    out = {}
    for s, r in rec.items():
        if r.get("source") not in ("movers", "tight"):
            continue
        a = recon.get(s)
        if a is None:
            out[s] = {"recorded_ts": float(r["ts"]), "status": "missing", "recorded": r}
        else:
            d = (a["first_ts"] - float(r["ts"])) / 60.0
            if d > WINDOW:
                out[s] = {"recorded_ts": float(r["ts"]), "status": f"late {d:.0f}m", "recorded": r}
    return out


class Tracer:
    def __init__(self, targets: set[str]):
        self.t = targets
        self.T = None
        self.ev = defaultdict(list)          # sym -> [(T, kind, detail)]

    def note(self, sym, kind, detail=None):
        if sym in self.t:
            self.ev[sym].append((self.T, kind, detail))


def install(tr: Tracer):
    """Wrap hist_universe functions in place; return an undo callable."""
    saved = {}

    def wrap(name, fn):
        saved[name] = getattr(HU, name)
        setattr(HU, name, fn)

    orig_seq = HU.DayMarket.seq

    def seq(self, s, T):
        tr.T = T
        return orig_seq(self, s, T)
    HU.DayMarket.seq = seq

    g0 = HU.gainers_select

    def gainers_select(*a, **k):
        out = g0(*a, **k)
        for s, _p, _px in out:
            tr.note(s, "gainers")
        return out
    wrap("gainers_select", gainers_select)

    a0 = HU.actives_select

    def actives_select(*a, **k):
        out = a0(*a, **k)
        for s in out:
            tr.note(s, "actives")
        return out
    wrap("actives_select", actives_select)

    s0 = HU._scan

    def _scan(mk, T, *a, **k):
        tr.T = T
        out = s0(mk, T, *a, **k)
        for r in out:
            tr.note(r["symbol"], "scan")
        return out
    wrap("_scan", _scan)

    p0 = HU._premarket

    def _premarket(mk, T, *a, **k):
        tr.T = T
        out = p0(mk, T, *a, **k)
        for r in out:
            tr.note(r["symbol"], "premarket")
        return out
    wrap("_premarket", _premarket)

    m0 = HU.measure_row

    def measure_row(sym, pct, px, seq, **k):
        out = m0(sym, pct, px, seq, **k)
        if sym in tr.t:
            if out is None:
                tr.note(sym, "measure:none", {"pct_in": pct, "seq_last": seq[-1] if seq else None,
                                              "seq_prev": seq[-2] if len(seq) > 1 else None})
            elif out.get("_discard"):
                px2 = float(seq[-1][0] or 0) if seq else None
                prev = float(seq[-2][0] or 0) if len(seq) > 1 else None
                tr.note(sym, "measure:discard", {"px": px2, "prev": prev,
                                                 "pct": (px2 / prev - 1) * 100 if px2 and prev else None})
            else:
                tr.note(sym, "measure:row", {k2: out.get(k2) for k2 in ("pct_change", "price", "rvol", "dollar_volume")})
        return out
    wrap("measure_row", measure_row)

    ms0 = HU.movers_seed

    def movers_seed(rows, cfg, **k):
        out = ms0(rows, cfg, **k)
        kept = {r["symbol"] for r in out}
        for r in rows:
            if r["symbol"] in tr.t:
                tr.note(r["symbol"], "seed:kept" if r["symbol"] in kept else "seed:dropped")
        return out
    wrap("movers_seed", movers_seed)

    dg0 = HU.door_gates

    def door_gates(row, cfg, **k):
        why = dg0(row, cfg, **k)
        if row.get("symbol") in tr.t:
            tr.note(row["symbol"], f"door:{why or 'pass'}")
        return why
    wrap("door_gates", door_gates)

    def undo():
        HU.DayMarket.seq = orig_seq
        for n, f in saved.items():
            setattr(HU, n, f)
    return undo


def summarize(day: str, targets: dict, ev: dict) -> dict:
    """Where each target fell out, first event times, and the dominant stage."""
    res = {}
    stage = Counter()
    for s, t in targets.items():
        e = ev.get(s, [])
        kinds = Counter(k for _T, k, _d in e)
        first = {}
        for T, k, _d in e:
            first.setdefault(k, T)
        if not e:
            where = "never a candidate (not on gainers / scan / premarket / actives lists)"
        elif not any(k.startswith("measure") for k in kinds):
            where = "listed but never measured (cut at want + session_max?)"
        elif kinds.get("measure:row", 0) == 0:
            where = "measured, never a row (" + ", ".join(k for k in kinds if k.startswith("measure")) + ")"
        elif not any(k.startswith("seed:kept") for k in kinds):
            where = "row, never seeded"
        elif not any(k == "door:pass" for k in kinds):
            where = "seeded, refused at the door (" + ", ".join(k for k in kinds if k.startswith("door")) + ")"
        else:
            where = "admitted (timing difference)"
        stage[where.split(" (")[0]] += 1
        rec_min = HU.et_min_of(t["recorded_ts"]) if hasattr(HU, "et_min_of") else None
        sample = [d for _T, k, d in e if k in ("measure:discard", "measure:none")][:3]
        res[s] = {"status": t["status"], "recorded_et": t["recorded"].get("ts"), "recorded_pct": t["recorded"].get("pct_change"),
                  "recorded_price": t["recorded"].get("price"), "where": where, "kinds": dict(kinds),
                  "first": {k: HU.hhmm(T) if T is not None else None for k, T in first.items()},
                  "measure_samples": sample, "rec_min": rec_min}
    return {"day": day, "n_targets": len(targets), "stages": dict(stage), "names": res}


class _NoNetwork:
    """Any Alpaca use raises: the trace must run from hist_universe's cache only, so it is safe
    while the live desk is trading."""
    def __getattr__(self, name):
        raise RuntimeError(f"network blocked in trace (attempted {name})")


def main() -> None:
    HU._client = lambda: _NoNetwork()
    days = sys.argv[1:] or ["2026-10-09"]
    if len(days) == 2 and days[0] < days[1]:
        days = HU.trading_days(days[0], days[1])
    for day in days:
        tg = targets_for(day)
        if not tg:
            print(f"{day}: no targets (no calib file or nothing missed)")
            continue
        tr = Tracer(set(tg))
        undo = install(tr)
        try:
            _out, mk = HU.build_day(day, cfg_mode="recorded", use_cache=True, write=False, quiet=True)
        finally:
            undo()
        blocked = len(mk._sq[2]) if hasattr(mk, "_sq") else 0
        summ = summarize(day, tg, tr.ev)
        summ["network_attempts_blocked"] = blocked
        if blocked:
            print(f"{day}: WARNING {blocked} spread lookups needed the network and were blocked - "
                  "this day's trace differs from the calibration build", flush=True)
        json.dump(summ, open(os.path.join(OUT, f"trace_{day}.json"), "w"), indent=1, default=str)
        print(f"{day}: {summ['n_targets']} targets -> {summ['stages']}", flush=True)


if __name__ == "__main__":
    main()
