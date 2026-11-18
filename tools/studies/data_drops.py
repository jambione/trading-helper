#!/usr/bin/env python3
"""data_drops - do names the desk drops for DATA reasons go on to give buy signals the desk never takes?
Pass bar: docs/studies/data_drops_prereg.json (written before any lost signal was counted). Read-only; data-only Alpaca keys.

    .venv/bin/python tools/studies/data_drops.py      # -> ai_reports/data_drops/result.json

A drop is a market-hours watch_drop event (ai_reports/events.jsonl). LOST = the first desk pre-square signal on that name within 60 min
of the drop, admitted by the rule in force ($20-$400, up >= 1.5%), with no re-seat (kept admission-ledger row) between the drop and the
signal and no entry_ok from the drop to 10 min after it. BASE = the same signal on the top-400 universe. Both arms are charged the real
SIP spread at entry (news_source.fetch_quotes, shared quote cache) and held to the desk's 15 bp gate.
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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day  # noqa: E402
from board_structure_family import universe  # noqa: E402
from evening_family import WH_SIP, ALL_ETF  # noqa: E402
import news_source as ns  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "data_drops"
EVENTS = ROOT / "ai_reports" / "events.jsonl"
WINDOW, AFTER_ENTRY = 60, 10
DATA_BLOCKS = {"pctr_not_live_alpaca", "pctr_not_live_missing", "stale_quote", "missing_pctr", "spread_unknown",
               "gap_unknown", "no_quote", "await_stream"}
DATA_REASONS = {"stale_tape_cap", "stale_timeout", "no_stream_trade"}


def drop_class(e: dict) -> str | None:
    """'data' / 'control' / None (not a seat drop we score)."""
    r, b = str(e.get("reason") or ""), str(e.get("block") or "")
    if r in DATA_REASONS or (r == "dead_unknown" and b in DATA_BLOCKS):
        return "data"
    if r in ("never_armable", "dead_unknown"):
        return "control"
    return None


def session_open(day: str) -> float:
    return datetime.fromisoformat(day + "T09:30").replace(tzinfo=ET).timestamp()


def lost_signals(drops, sig_minutes, reseat_ts, entry_ts, day):
    """drops [(ts, sym, label)]; sig_minutes {sym: [t,...] admitted signal minutes}; reseat_ts / entry_ts {sym: [ts]}.
    -> {(sym, t): label} - each signal counted once, under the first drop that lost it."""
    t0 = session_open(day)
    out = {}
    for ts, sym, label in sorted(drops):
        m = int((ts - t0) // 60)
        nxt = [t for t in sig_minutes.get(sym, []) if m <= t <= m + WINDOW]
        if not nxt:
            continue
        t = nxt[0]
        sig_ts = t0 + (t + 1) * 60
        if any(ts < r <= sig_ts for r in reseat_ts.get(sym, [])):
            continue
        if any(ts <= x <= sig_ts + AFTER_ENTRY * 60 for x in entry_ts.get(sym, [])):
            continue
        out.setdefault((sym, t), label)
    return out


def load_events(days: set[str]):
    """{day: {"drops": [(ts, sym, cls, label)], "entries": {sym: [ts]}}} for market hours."""
    res = defaultdict(lambda: {"drops": [], "entries": defaultdict(list)})
    for line in open(EVENTS):
        if '"watch_drop"' not in line and '"entry_ok"' not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        t = datetime.fromtimestamp(float(e["ts"]), ET)
        d = t.date().isoformat()
        if d not in days or not (570 <= t.hour * 60 + t.minute < 960) or not e.get("symbol"):
            continue
        if e.get("kind") == "entry_ok":
            res[d]["entries"][e["symbol"]].append(float(e["ts"]))
        elif e.get("kind") == "watch_drop":
            c = drop_class(e)
            if c:
                res[d]["drops"].append((float(e["ts"]), e["symbol"], c, f'{e.get("reason")}|{e.get("block") or ""}'))
    return res


def ledger_kept(day: str) -> dict[str, list[float]] | None:
    for d in ns.LEDGER_DIRS:
        for p in (d / f"{day}.jsonl", d / f"{day}.jsonl.gz"):
            if p.exists():
                op = gzip.open if p.suffix == ".gz" else open
                kept = defaultdict(list)
                with op(p, "rt") as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                        except Exception:  # noqa: BLE001
                            continue
                        if r.get("kept"):
                            kept[r["symbol"]].append(float(r["ts"]))
                return kept
    return None


def main():
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    ledgers = {f.stem: ledger_kept(f.stem) for f in files}
    days = {d for d, k in ledgers.items() if k is not None}
    ev = load_events(days)
    sig = {}                       # news_source.check_plan shape: {day: {"base": [...], "extra": [...]}}
    info = {"lost_by_reason": Counter(), "control_lost": 0, "entries_per_day": {}, "data_drops": 0, "control_drops": 0}
    prev = None
    for f in files:
        d = f.stem
        day = load_day(f)
        if prev is not None and d in ev:
            si, psi = day["idx"].get("SPY"), prev["idx"].get("SPY")
            if si is not None and psi is not None:
                spy_c, spy_pc = day["c"][si], ns.last_close(prev["c"][psi])
                cache = {}

                def sigs(s):
                    if s not in cache:
                        cache[s] = [x for x in (ns.signals_for(day, prev, s, spy_c, spy_pc) or []) if x["base"]]
                    return cache[s]
                base = [{"sym": s, "t": x["t"], "net": x["net"], "base": True}
                        for s in universe(day, prev, set(ALL_ETF)) for x in sigs(s)]
                drops = ev[d]["drops"]
                info["data_drops"] += sum(1 for x in drops if x[2] == "data")
                info["control_drops"] += sum(1 for x in drops if x[2] == "control")
                syms = {x[1] for x in drops}
                mins = {s: [x["t"] for x in sigs(s)] for s in syms}
                nets = {(s, x["t"]): x["net"] for s in syms for x in sigs(s)}
                lost = lost_signals([(ts, s, lab) for ts, s, c, lab in drops if c == "data"], mins, ledgers[d],
                                    ev[d]["entries"], d)
                ctrl = lost_signals([(ts, s, lab) for ts, s, c, lab in drops if c == "control"], mins, ledgers[d],
                                    ev[d]["entries"], d)
                info["lost_by_reason"].update(lost.values())
                info["control_lost"] += len(ctrl)
                info["entries_per_day"][d] = sum(len(v) for v in ev[d]["entries"].values())
                sig[d] = {"base": base, "extra": [{"sym": s, "t": t, "net": nets[(s, t)], "base": True} for (s, t) in lost]}
                print(d, "drops", len(drops), "base", len(base), "lost", len(lost), "ctrl-lost", len(ctrl),
                      "entries", info["entries_per_day"][d], flush=True)
        prev = day
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "signals.json").write_text(json.dumps(sig))
    plan = ns.check_plan(sig)
    quotes = ns.fetch_quotes(plan)
    res = ns.score_spreads(plan, quotes, len(sig), sum(len(r["base"]) for r in sig.values()), min_gated=200, min_share=0.10)
    res["lost"] = res.pop("extra")
    res["lost_share_of_base"] = res.pop("extra_share_of_base")
    res["gated_lost_per_10m"] = res.pop("gated_extra_per_10m")
    n_days = len(sig)
    res["info"] = {"lost_by_reason": dict(info["lost_by_reason"].most_common()),
                   "lost_per_session": round(sum(len(r["extra"]) for r in sig.values()) / max(1, n_days), 2),
                   "entries_per_session": round(sum(info["entries_per_day"].values()) / max(1, n_days), 2),
                   "control_lost_per_session": round(info["control_lost"] / max(1, n_days), 2),
                   "data_drops": info["data_drops"], "control_drops": info["control_drops"]}
    res["generated"] = datetime.now(ET).isoformat(timespec="minutes")
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
