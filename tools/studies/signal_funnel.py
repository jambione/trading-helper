#!/usr/bin/env python3
"""signal_funnel - where do the desk's own buy signals die? (Argon 11/18: top-400 minute bars give ~4.6 gated pre-square signals
per 10 min, the desk opens ~0.6.)

Read-only audit, no verdict bar (it ranks fixes, it does not ship one). Input: ai_reports/news_source/ledger_signals.json (the BASE
pre-square signals on the top-400 universe, $20-$400, up >= 1.5%, per ledger day). Each signal (sym, minute t; known at the close of
bar t) is followed through that day's live records:

  entered        entry_ok for the symbol from 2 min before to 10 min after the signal
  armed          an arm_ok decision-ledger row in [-2, +5] min, but no entry
  arm_blocked    seated and evaluated in [-2, +5] min, never arm_ok -> the most common arm_why
  seated_quiet   kept (seated) before the signal, but no arm evaluation near it
  refused        on the admission ledger before the signal, never kept -> the most common refusal reason
  never_seen     not on the admission ledger before the signal (screener never offered it)

    .venv/bin/python tools/studies/signal_funnel.py      # -> ai_reports/signal_funnel/result.json
"""
from __future__ import annotations

import gzip
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
ET = ZoneInfo("America/New_York")
SIG = ROOT / "ai_reports" / "news_source" / "ledger_signals.json"
DEC = ROOT / "ai_reports" / "decision_ledger"
LEDGER_DIRS = (ROOT / "ai_reports" / "admit_ledger", Path.home() / "data_archive" / "admit_ledger")
EVENTS = ROOT / "ai_reports" / "events.jsonl"
OUT = ROOT / "ai_reports" / "signal_funnel"
STAGES = ("entered", "armed", "arm_blocked", "seated_quiet", "refused", "never_seen")


def _open(p: Path):
    return gzip.open(p, "rt") if p.suffix == ".gz" else open(p)


def _rows(paths):
    for p in paths:
        if p.exists():
            with _open(p) as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
            return


def classify(sig_ts, adm, dec, ent):
    """adm [(ts, kept, reason)], dec [(ts, arm_ok, arm_why)], ent [ts] for one symbol -> (stage, why)."""
    if any(sig_ts - 120 <= x <= sig_ts + 600 for x in ent):
        return "entered", ""
    near = [d for d in dec if sig_ts - 120 <= d[0] <= sig_ts + 300]
    if any(d[1] is True for d in near):
        return "armed", ""
    before = [a for a in adm if a[0] <= sig_ts + 60]
    kept = any(a[1] for a in before)
    if near:
        return "arm_blocked", Counter(str(d[2]) for d in near).most_common(1)[0][0]
    if kept:
        return "seated_quiet", ""
    if before:
        return "refused", Counter(str(a[2]) for a in before if not a[1]).most_common(1)[0][0]
    return "never_seen", ""


def main():
    sig = json.load(open(SIG))
    days = sorted(sig)
    ent = defaultdict(lambda: defaultdict(list))
    for line in open(EVENTS):
        if '"entry_ok"' not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        d = datetime.fromtimestamp(float(e["ts"]), ET).date().isoformat()
        if d in sig and e.get("symbol"):
            ent[d][e["symbol"]].append(float(e["ts"]))
    tot, why = Counter(), defaultdict(Counter)
    per_day, gated_days = {}, []
    for d in days:
        if not (DEC / f"{d}.jsonl").exists():
            continue
        adm, dec = defaultdict(list), defaultdict(list)
        for r in _rows([p / f"{d}.jsonl" for p in LEDGER_DIRS] + [p / f"{d}.jsonl.gz" for p in LEDGER_DIRS]):
            if r.get("symbol"):
                adm[r["symbol"]].append((float(r["ts"]), bool(r.get("kept")), r.get("reason")))
        for r in _rows([DEC / f"{d}.jsonl"]):
            if r.get("symbol") and r.get("stage") == "arm":
                dec[r["symbol"]].append((float(r["ts"]), r.get("arm_ok"), r.get("arm_why")))
        t0 = datetime.fromisoformat(d + "T09:30").replace(tzinfo=ET).timestamp()
        c = Counter()
        for x in sig[d]["base"]:
            st, w = classify(t0 + (x["t"] + 1) * 60, adm[x["sym"]], dec[x["sym"]], ent[d][x["sym"]])
            c[st] += 1
            if w:
                why[st][w] += 1
        tot.update(c)
        per_day[d] = dict(c)
        gated_days.append(d)
    n = sum(tot.values())
    res = {"days": len(gated_days), "signals": n, "per_10m": round(n / max(1, len(gated_days)) / 346 * 10, 2),
           "stages": {s: {"n": tot[s], "pct": round(100 * tot[s] / max(1, n), 1)} for s in STAGES},
           "top_reasons": {s: dict(why[s].most_common(12)) for s in ("arm_blocked", "refused")},
           "per_day": per_day, "generated": datetime.now(ET).isoformat(timespec="minutes")}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: res[k] for k in ("days", "signals", "per_10m", "stages", "top_reasons")}, indent=1))


if __name__ == "__main__":
    main()
