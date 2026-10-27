#!/usr/bin/env python3
"""lag_breakdown.py (DIAGNOSTIC, no pass/fail): where do the minutes between the %R turn and the desk's fill go?

For every paper BUY fill in the recorded sessions 2026-09-28..2026-10-15 (warehouse days, so no Alpaca calls):
  turn    the lag audit's turn (tools/studies/lag_audit.audit_one on warehouse IEX lines): the first minute after the
          30-minute fast-%R low where fast rose >= 5 points; its close time
  listed  first proposal_ledger row for the symbol that day (any source, any decision): it was on a source list
  seated  start of the arm-check spell that ends in the buy (decision_ledger arm rows, a gap > 120 s ends a spell)
  armed   first arm_ok row in that spell
  fill    the fill time (session_snapshots fills.jsonl)
Stages after the turn: not_listed = listed - turn; listed_not_seated = seated - max(listed, turn);
seated_not_armed = armed - max(seated, turn); armed_to_fill = fill - armed. A stage that ended before the turn is 0.
Also: the arm's refusal reasons while seated after the turn and before the arm.
USAGE (mini): .venv/bin/python tools/studies/lag_breakdown.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicator_confirm_hist as H  # noqa: E402
import lag_audit as LA  # noqa: E402

SNAP = os.path.expanduser("~/session_snapshots")
FIRST, LAST = "2026-09-28", "2026-10-15"
GAP = 120.0
OUT = os.path.expanduser("~/replay_runs/lag_breakdown.json")


def load_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def day_rows(day: str, prev: str) -> list[dict]:
    d = os.path.join(SNAP, day)
    listed: dict[str, float] = {}
    for r in load_jsonl(os.path.join(d, "proposal_ledger.jsonl")):
        s = r.get("symbol")
        if s and (s not in listed or r["ts"] < listed[s]):
            listed[s] = float(r["ts"])
    checks = defaultdict(list)
    for r in load_jsonl(os.path.join(d, "decision_ledger.jsonl")):
        if r.get("stage") == "arm" and r.get("symbol"):
            checks[r["symbol"]].append((float(r["ts"]), bool(r.get("arm_ok")), r.get("arm_why")))
    for v in checks.values():
        v.sort()
    fills = [r for r in load_jsonl(os.path.join(d, "fills.jsonl"))
             if r.get("event") == "fill" and str(r.get("side")).lower() == "buy"]
    iex_t, iex_p = H.load_raw("iex", day), H.load_raw("iex", prev)
    sip_t, sip_p = H.load_raw("sip", day), H.load_raw("sip", prev)
    out = []
    for f in fills:
        sym, tf = f["symbol"], float(f["ts"])
        row = {"day": day, "sym": sym, "fill_ts": tf}
        if sym not in iex_t or sym not in iex_p or sym not in sip_t or sym not in sip_p:
            row["skip"] = "not_in_warehouse"
            out.append(row)
            continue
        bars = {"iex": (iex_p[sym], iex_t[sym]), "sip": (sip_p[sym], sip_t[sym])}
        a = LA.audit_one({"t": tf, "sym": sym, "px": float(f.get("filled_avg_price") or 0)}, None, bars)
        if a.get("skip") or a.get("turn_close_ts") is None:
            row["skip"] = a.get("skip") or "no_turn"
            out.append(row)
            continue
        turn = float(a["turn_close_ts"])
        ch = [c for c in checks.get(sym, []) if c[0] <= tf]
        if not ch:
            row["skip"] = "no_arm_checks"
            out.append(row)
            continue
        k = len(ch) - 1
        while k > 0 and ch[k][0] - ch[k - 1][0] <= GAP:
            k -= 1
        spell = ch[k:]
        seated = spell[0][0]
        armed = next((c[0] for c in spell if c[1]), None)
        if armed is None:
            row["skip"] = "no_arm_ok_in_spell"
            out.append(row)
            continue
        lst = listed.get(sym)
        lst = min(lst, seated) if lst is not None else seated
        row.update({
            "turn_ts": turn, "listed_ts": lst, "seated_ts": seated, "armed_ts": armed,
            "fill_after_turn_s": tf - turn,
            "not_listed": max(0.0, lst - turn),
            "listed_not_seated": max(0.0, seated - max(lst, turn)),
            "seated_not_armed": max(0.0, armed - max(seated, turn)),
            "armed_to_fill": max(0.0, tf - armed),
            "seated_before_turn": seated <= turn,
            "listed_before_turn": lst <= turn,
            "wait_reasons": Counter(c[2] for c in spell if not c[1] and max(seated, turn) <= c[0] < armed),
        })
        out.append(row)
    return out


def main() -> None:
    days = [d for d in sorted(os.listdir(SNAP)) if FIRST <= d <= LAST]
    allwh = sorted(set(H.warehouse_days()))
    rows = []
    for d in days:
        if d not in allwh:
            continue
        prev = allwh[allwh.index(d) - 1]
        rows += day_rows(d, prev)
    ok = [r for r in rows if not r.get("skip")]
    print(f"buys {len(rows)}, broken down {len(ok)}, skipped {dict(Counter(r.get('skip') for r in rows if r.get('skip')))}")
    if not ok:
        return
    pre = [r for r in ok if r["fill_after_turn_s"] >= 0]
    print(f"fills after the turn: {len(pre)} (fills before the detected turn: {len(ok) - len(pre)})")

    def med(k, xs):
        v = [x[k] / 60 for x in xs]
        return f"median {statistics.median(v):5.1f} min  mean {statistics.fmean(v):5.1f}"
    for k in ("fill_after_turn_s", "not_listed", "listed_not_seated", "seated_not_armed", "armed_to_fill"):
        print(f"  {k:20s} {med(k, pre)}")
    tot = sum(r["fill_after_turn_s"] for r in pre)
    for k in ("not_listed", "listed_not_seated", "seated_not_armed", "armed_to_fill"):
        print(f"  share of all post-turn minutes in {k:20s} {sum(r[k] for r in pre) / tot:5.1%}")
    print(f"  listed before the turn {sum(r['listed_before_turn'] for r in pre) / len(pre):.0%}; "
          f"seated before the turn {sum(r['seated_before_turn'] for r in pre) / len(pre):.0%}")
    wr = Counter()
    for r in pre:
        wr.update(r["wait_reasons"])
    tw = sum(wr.values())
    print("  arm refusals while seated after the turn (share of refused checks):")
    for why, n in wr.most_common(12):
        print(f"    {str(why):26s} {n / tw:6.1%}")
    json.dump([{**r, "wait_reasons": dict(r.get("wait_reasons", {}))} for r in rows], open(OUT, "w"), indent=1)


if __name__ == "__main__":
    main()
