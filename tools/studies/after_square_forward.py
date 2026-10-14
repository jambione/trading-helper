#!/usr/bin/env python3
"""Forward score of the pre-square-after-a-square refusal. Spec: docs/studies/presquare_after_square_prereg.json.

Per forward session (>= FORWARD_START, early_entry_honest cache + decision_ledger):
  refused  onsets of arm_why 'presquare_after_square' (first per symbol, 15-min refractory)
  taken    onsets of arm_ok True / arm_why 'last_presquare' (same refractory)
Each scored as the harness markout: SIP open of the next minute to the SIP close 9 minutes later, 10 bp round trip.
USAGE (mini): .venv/bin/python tools/studies/after_square_forward.py
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402
import limit_below_study as L  # noqa: E402

FORWARD_START = "2026-10-15"
READ_AT = 10
REFRACT = 15
COST = 0.0010
MIN_REFUSED = 40


def onsets(rows: list[tuple[float, str]], refract_min: int = REFRACT) -> list[tuple[str, int]]:
    """rows: (ts, sym) sorted by ts -> [(sym, ET minute)] keeping the first per symbol per refractory window."""
    last: dict[str, int] = {}
    out = []
    for ts, sym in rows:
        m = E._et_min(ts)
        if sym in last and m - last[sym] < refract_min:
            continue
        last[sym] = m
        out.append((sym, m))
    return out


def ledger_rows(day: str):
    ref, tak = [], []
    p = os.path.join(E.LEDGER_DIR, f"{day}.jsonl")
    if not os.path.exists(p):
        return ref, tak
    for line in open(p, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("stage") != "arm":
            continue
        if r.get("arm_why") == "presquare_after_square":
            ref.append((float(r["ts"]), str(r["symbol"]).upper()))
        elif r.get("arm_ok") is True and r.get("arm_why") == "last_presquare":
            tak.append((float(r["ts"]), str(r["symbol"]).upper()))
    return sorted(ref), sorted(tak)


def main() -> None:
    datas, _ex = E.load_days()
    fwd = sorted(d["day"] for d in datas if d["day"] >= FORWARD_START)
    print(f"forward sessions {len(fwd)}")
    by = {d["day"]: d for d in datas}
    res = {"refused": defaultdict(list), "taken": defaultdict(list)}
    for day in fwd[:READ_AT]:
        ref, tak = ledger_rows(day)
        for name, rows in (("refused", ref), ("taken", tak)):
            for sym, m in onsets(rows):
                fr = by[day]["sip"].get(sym)
                if fr is None:
                    continue
                mk = L.market_trade(L.ohlc_map(fr), m)
                if mk is not None:
                    res[name][day].append(mk - COST)
    n_ref = sum(len(v) for v in res["refused"].values())
    print({k: (sum(len(v) for v in d.values()), E.day_equal(d)[0]) for k, d in res.items()})
    if len(fwd) < READ_AT:
        print(f"NOT DUE: {len(fwd)} < {READ_AT} sessions (counts above are information)")
        return
    diff = {d: [sum(res["taken"][d]) / len(res["taken"][d]) - sum(v) / len(v)]
            for d, v in res["refused"].items() if v and res["taken"].get(d)}
    mu, t, _n, nd = E.day_equal(diff)
    ok = n_ref >= MIN_REFUSED and nd >= 6 and mu is not None and t is not None
    verdict = ("UNDERPOWERED" if not ok else "KEEP" if mu >= 0.0010 and t >= 2.0 else
               "DROP" if mu <= 0 else "NO EVIDENCE")
    print(f"taken - refused: {None if mu is None else round(mu * 1e4, 1)} bp/day (t {t}, {nd} days, {n_ref} refused): {verdict}")


if __name__ == "__main__":
    main()
