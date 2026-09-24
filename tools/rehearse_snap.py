#!/usr/bin/env python3
"""rehearse_snap.py — drive pass-bar metrics from a session snapshot archive.

Companion to rehearse_open (ledger-observed) and rehearse_whatif (ledger
counterfactuals). Reads ``state_snapshots.jsonl.gz`` records of the form
``{ts, file, mtime, sha, data}`` plus optional copied ledgers/fills in the
same directory.

Layers
  observed   book / armable / data-blocked from entry_watch_state +
             decision_ledger (or seat block_code on the snapshot row)
  concurrency  share of the session with >=1 / >=2 positions open, from
               ai_positions_state snapshots (or fills.jsonl open/close)
  simulate   optional: re-stamp one-price ages from signal_state.rt_* and
             re-bucket seats (used to score freshness fixes offline)

No live state is written. Truncated gzip (writer killed mid-flush) is tolerated.

USAGE
    .venv/bin/python tools/rehearse_snap.py \\
        --snapshots ~/session_snapshots/2026-09-24 --start 13:10 --end 16:00 --step 10
    .venv/bin/python tools/rehearse_snap.py --snapshots DIR --simulate-one-clock
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import rehearse_open as ro  # noqa: E402

ET = ZoneInfo("America/New_York")

WATCH_FILES = (
    "ai_reports/entry_watch_state.json",
    "entry_watch_state.json",
)
POS_FILES = (
    "ai_positions_state.json",
    "ai_reports/positions_state.json",
)
FUNNEL_FILES = (
    "ai_reports/admit_funnel.json",
    "admit_funnel.json",
)
SIGNAL_FILES = ("signal_state.json",)


def _open_jsonl_gz(path: str):
    """Yield parsed JSON objects; tolerate truncated gzip trailers."""
    if not os.path.exists(path):
        return
    opener = gzip.open if path.endswith(".gz") else open
    try:
        with opener(path, "rt", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
    except EOFError:
        return
    except OSError:
        return


def index_snapshots(path: str) -> dict[str, list[tuple[float, dict]]]:
    """file -> sorted [(ts, data), ...] for known desk state files."""
    out: dict[str, list[tuple[float, dict]]] = collections.defaultdict(list)
    for r in _open_jsonl_gz(path):
        fn = r.get("file")
        ts = r.get("ts")
        data = r.get("data")
        if not isinstance(fn, str) or not isinstance(ts, (int, float)):
            continue
        if not isinstance(data, dict):
            continue
        out[fn].append((float(ts), data))
    for fn in out:
        out[fn].sort(key=lambda x: x[0])
    return out


def _latest_at(series: list[tuple[float, dict]], t: float) -> dict | None:
    if not series:
        return None
    lo, hi = 0, len(series) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if series[mid][0] <= t:
            best = series[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _series(idx: dict, names: tuple[str, ...]) -> list[tuple[float, dict]]:
    for n in names:
        if n in idx and idx[n]:
            return idx[n]
    return []


def _watch_rows(data: dict | None) -> dict[str, dict]:
    if not isinstance(data, dict):
        return {}
    # Flat {SYM: row} (live entry_watch_state shape).
    if data and all(isinstance(v, dict) for v in data.values()):
        sample = next(iter(data.values()))
        if any(k in sample for k in ("status", "block_code", "symbol", "last_ask", "pctr")):
            return {str(k).upper(): v for k, v in data.items() if isinstance(v, dict)}
    for key in ("watches", "watch", "rows", "book"):
        w = data.get(key)
        if isinstance(w, dict):
            return {str(k).upper(): v for k, v in w.items() if isinstance(v, dict)}
        if isinstance(w, list):
            out = {}
            for row in w:
                if not isinstance(row, dict):
                    continue
                sym = str(row.get("symbol") or row.get("ticker") or "").upper()
                if sym:
                    out[sym] = row
            return out
    return {}


def _n_open(pos: dict | None) -> int:
    if not isinstance(pos, dict):
        return 0
    try:
        if pos.get("n_open") is not None:
            return max(0, int(pos["n_open"]))
    except (TypeError, ValueError):
        pass
    positions = pos.get("positions")
    if isinstance(positions, dict):
        n = 0
        for row in positions.values():
            if not isinstance(row, dict):
                continue
            try:
                qty = float(row.get("qty") or row.get("quantity") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            if qty > 0 and not row.get("closing") and not row.get("closed"):
                n += 1
        return n
    if isinstance(positions, list):
        return sum(1 for row in positions if isinstance(row, dict)
                   and float(row.get("qty") or 0) > 0)
    return 0


def _bucket_row(row: dict, *, simulate_one_clock: bool = False,
                signal: dict | None = None, now: float | None = None,
                decision_ceiling: float = 15.0) -> str:
    """Map a seated watch row to rehearse_open buckets."""
    why = str(row.get("arm_why") or row.get("block_code") or row.get("why") or "")
    ok = bool(row.get("arm_ok") or row.get("status") in ("armed", "submitted", "filled"))
    if simulate_one_clock and signal and now is not None:
        sym = str(row.get("symbol") or "").upper()
        # Prefer explicit symbol key on flat maps.
        tickers = (signal.get("tickers") or signal) if isinstance(signal, dict) else {}
        sp = tickers.get(sym) if isinstance(tickers, dict) else None
        if isinstance(sp, dict):
            try:
                age = float(sp.get("rt_price_age_sec"))
            except (TypeError, ValueError):
                age = None
            if age is not None and age <= decision_ceiling:
                # Young engine print clears tape-data blocks for scoring.
                if why in ro.DATA or why in ("stale_quote", "stale_tape", "tape_only"):
                    why = "wait_mid_rise"
                    ok = False
    return ro.bucket(why, ok=ok)


def concurrency_from_fills(fills_path: str, start: str, end: str, day: str) -> dict:
    """Build open-count time series from fills.jsonl buy/sell events."""
    t0, t1 = ro._day_bounds(day)
    m0 = int(start[:2]) * 60 + int(start[3:])
    m1 = int(end[:2]) * 60 + int(end[3:])
    events: list[tuple[float, str, str]] = []
    if not os.path.exists(fills_path):
        return {"pct_ge1": None, "pct_ge2": None, "avg_open": None, "max_open": None,
                "samples": 0}
    with open(fills_path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            ts = e.get("ts") or e.get("observed_ts")
            if not isinstance(ts, (int, float)) or not (t0 <= ts < t1):
                continue
            sym = str(e.get("symbol") or "").upper()
            if not sym:
                continue
            ev = str(e.get("event") or "").lower()
            side = str(e.get("side") or e.get("action") or "").lower()
            if ev == "fill" and side in ("buy", "sell"):
                events.append((float(ts), sym, side))
            elif ev == "submit" and side in ("buy", "sell"):
                # Prefer fills; submit alone is ignored when fills exist.
                pass
    events.sort()
    open_syms: set[str] = set()
    # Sample every minute in the window.
    samples = []
    ev_i = 0
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    for m in range(m0, m1):
        t = d0.timestamp() + m * 60
        while ev_i < len(events) and events[ev_i][0] <= t:
            _, sym, side = events[ev_i]
            if side == "buy":
                open_syms.add(sym)
            elif side == "sell":
                open_syms.discard(sym)
            ev_i += 1
        samples.append(len(open_syms))
    if not samples:
        return {"pct_ge1": None, "pct_ge2": None, "avg_open": None, "max_open": None,
                "samples": 0}
    n = len(samples)
    return {
        "pct_ge1": round(sum(1 for x in samples if x >= 1) / n, 4),
        "pct_ge2": round(sum(1 for x in samples if x >= 2) / n, 4),
        "avg_open": round(sum(samples) / n, 3),
        "max_open": max(samples),
        "samples": n,
    }


def replay_snapshots(
    snap_dir: str,
    day: str,
    start: str,
    end: str,
    step: int = 10,
    *,
    simulate_one_clock: bool = False,
    decision_ceiling: float = 15.0,
) -> dict:
    gz = os.path.join(snap_dir, "state_snapshots.jsonl.gz")
    if not os.path.exists(gz):
        gz = os.path.join(snap_dir, "state_snapshots.jsonl")
    idx = index_snapshots(gz)
    watches = _series(idx, WATCH_FILES)
    positions = _series(idx, POS_FILES)
    funnels = _series(idx, FUNNEL_FILES)
    signals = _series(idx, SIGNAL_FILES)

    m_start = int(start[:2]) * 60 + int(start[3:])
    m_end = int(end[:2]) * 60 + int(end[3:])
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    slots = list(range(m_start, m_end, step))
    rows = []
    open_samples: list[int] = []

    for s in slots:
        t = d0.timestamp() + s * 60
        # Sample every minute inside the slot for concurrency.
        for mm in range(s, min(s + step, m_end)):
            tt = d0.timestamp() + mm * 60
            open_samples.append(_n_open(_latest_at(positions, tt)))

        wdata = _latest_at(watches, t)
        fdata = _latest_at(funnels, t)
        sig = _latest_at(signals, t) if simulate_one_clock else None
        book_map = _watch_rows(wdata)
        if not book_map and isinstance(fdata, dict):
            book_map = {str(x).upper(): {} for x in (fdata.get("kept_symbols") or [])}

        bk = collections.Counter()
        reasons = collections.Counter()
        for sym, row in book_map.items():
            row = dict(row)
            row.setdefault("symbol", sym)
            b = _bucket_row(
                row,
                simulate_one_clock=simulate_one_clock,
                signal=sig,
                now=t,
                decision_ceiling=decision_ceiling,
            )
            bk[b] += 1
            why = str(row.get("arm_why") or row.get("block_code") or "")
            if b not in ("armable",) and why:
                reasons[why] += 1

        n_open = _n_open(_latest_at(positions, t))
        rows.append({
            "t": ro._hhmm(s),
            "book": len(book_map),
            "armable": bk["armable"],
            "gate": bk["gate"],
            "data": bk["data"],
            "other": bk["other"],
            "unjudged": bk["unjudged"],
            "cand": (int(fdata.get("n_candidates"))
                     if isinstance(fdata, dict) and fdata.get("n_candidates") is not None
                     else None),
            "n_open": n_open,
            "top_blocks": reasons.most_common(3),
            "names": sorted(book_map),
        })

    conc = {}
    if open_samples:
        n = len(open_samples)
        conc = {
            "pct_ge1": round(sum(1 for x in open_samples if x >= 1) / n, 4),
            "pct_ge2": round(sum(1 for x in open_samples if x >= 2) / n, 4),
            "avg_open": round(sum(open_samples) / n, 3),
            "max_open": max(open_samples),
            "samples": n,
        }
    else:
        fills = os.path.join(snap_dir, "fills.jsonl")
        conc = concurrency_from_fills(fills, start, end, day)

    # Trade-path events from the live reports tree when available.
    events: list = []
    reports = os.path.join(ROOT, "ai_reports")
    if os.path.exists(os.path.join(reports, "events.jsonl")):
        try:
            events = (ro.replay(day, start, end, step, reports).get("events") or [])
        except Exception:  # noqa: BLE001
            events = []

    return {
        "day": day,
        "start": start,
        "end": end,
        "step": step,
        "source": "snapshots",
        "simulate_one_clock": simulate_one_clock,
        "rows": rows,
        "events": events,
        "concurrency": conc,
        "snap_files": sorted(idx.keys()),
    }


def criteria_with_concurrency(res: dict) -> list[tuple[str, bool, str]]:
    """Pass bars including concurrency headline metrics."""
    out = list(ro.criteria(res))
    c = res.get("concurrency") or {}
    pct1, pct2 = c.get("pct_ge1"), c.get("pct_ge2")
    if pct1 is not None:
        out.append((">=1 open for >=80% of session", pct1 >= 0.80,
                    f"{pct1:.0%} (avg {c.get('avg_open')} max {c.get('max_open')})"))
    if pct2 is not None:
        out.append((">=2 open for >=50% of session", pct2 >= 0.50,
                    f"{pct2:.0%}"))
    return out


def render(res: dict) -> str:
    mode = "simulate-one-clock" if res.get("simulate_one_clock") else "observed"
    L = [f"SNAP REHEARSAL {res['day']} {res['start']}-{res['end']} "
         f"(step {res['step']}m, {mode})", ""]
    L.append(f"{'time':>5} {'book':>4} {'arm':>4} {'gate':>4} {'data':>4} "
             f"{'oth':>4} {'unj':>4} {'open':>4}  top blocks")
    for r in res["rows"]:
        blocks = ", ".join(f"{k}:{n}" for k, n in (r.get("top_blocks") or []))
        L.append(f"{r['t']:>5} {r['book']:>4} {r['armable']:>4} {r['gate']:>4} "
                 f"{r['data']:>4} {r['other']:>4} {r['unjudged']:>4} "
                 f"{str(r.get('n_open') if r.get('n_open') is not None else '-'):>4}  "
                 f"{blocks or '-'}")
    c = res.get("concurrency") or {}
    L.append("")
    L.append(f"Concurrency: ge1={c.get('pct_ge1')} ge2={c.get('pct_ge2')} "
             f"avg={c.get('avg_open')} max={c.get('max_open')} n={c.get('samples')}")
    L.append("")
    L.append("Pass criteria:")
    for name, ok, detail in criteria_with_concurrency(res):
        L.append(f"  [{'PASS' if ok else 'FAIL'}] {name:<40} {detail}")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", required=True, help="session snapshot directory")
    ap.add_argument("--day", default=None)
    ap.add_argument("--start", default="09:40")
    ap.add_argument("--end", default="15:50")
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--simulate-one-clock", action="store_true")
    ap.add_argument("--decision-ceiling", type=float, default=15.0)
    ap.add_argument("--json", help="write result JSON here")
    args = ap.parse_args()
    day = args.day
    if not day:
        # Infer from directory name …/YYYY-MM-DD
        day = os.path.basename(os.path.abspath(args.snapshots.rstrip("/")))
    res = replay_snapshots(
        args.snapshots, day, args.start, args.end, args.step,
        simulate_one_clock=args.simulate_one_clock,
        decision_ceiling=args.decision_ceiling,
    )
    print(render(res))
    if args.json:
        with open(args.json, "w") as f:
            json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
