#!/usr/bin/env python3
"""Two buy-refusal reasons, scored from the refusal minute.

Spec: docs/studies/refuse_reason_prereg.json. Two graded cells. Do not pass
--score until a result-skeptic READY on that prereg. One process so this can
run beside a warehouse job.

  .venv/bin/python tools/studies/refuse_reason.py
  .venv/bin/python tools/studies/refuse_reason.py --score
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402

REASONS = ("exh_falling", "exh_not_tight")
CUT = "2026-09-24"
COST3, COST10 = 0.0003, 0.0010
# Saturday. The ledger and the cache both have a file. The market was closed.
CLOSED_SATURDAY = "2026-09-05"
PIN = (
    "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
    "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
    "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24",
    "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30",
    "2026-10-01", "2026-10-02",
    "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09",
    "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
)
OUT = os.path.join(E.ROOT, "ai_reports", "early_entry_honest", "refuse_reason_result.md")


def signal_of(row: dict):
    """(symbol, reason, ET minute) or None.

    The first qualifying minute is the signal. arm_ok True is a buy, not a refusal.
    The reason string is exact. The window is the published 09:50-15:30.
    """
    if not isinstance(row, dict):
        return None
    if row.get("stage") not in ("watch", "arm"):
        return None
    if row.get("arm_ok") is True:
        return None
    why = row.get("arm_why")
    if why not in REASONS:
        return None
    sym = row.get("symbol")
    if not isinstance(sym, str) or not sym.strip():
        return None
    try:
        minute = E._et_min(float(row["ts"]))
    except (TypeError, ValueError, KeyError):
        return None
    if not (E.WIN_START <= minute <= E.WIN_END):
        return None
    return sym.strip().upper(), why, minute


def first_signals(rows) -> dict[tuple[str, str], int]:
    """Earliest window minute per symbol per reason. File order does not matter."""
    best: dict[tuple[str, str], int] = {}
    for row in rows:
        hit = signal_of(row)
        if hit is None:
            continue
        sym, why, minute = hit
        key = (sym, why)
        prev = best.get(key)
        if prev is None or minute < prev:
            best[key] = minute
    return best


def read_ledger(path: str) -> dict[tuple[str, str], int]:
    """Stream the day file. The whole ledger is not held."""
    best: dict[tuple[str, str], int] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            hit = signal_of(row)
            if hit is None:
                continue
            sym, why, minute = hit
            key = (sym, why)
            prev = best.get(key)
            if prev is None or minute < prev:
                best[key] = minute
    return best


def fill_gross(sip: dict, signal_min: int):
    """Next minute's SIP open to the SIP close 10 minutes later. None if the bar is missing."""
    g = E.markout(sip, signal_min + 1)
    if g is None or not math.isfinite(g):
        return None
    return g


def board_grosses(board: dict, sips: dict) -> list[float]:
    """Every cached board minute in the window, signal minutes included.

    This is the same-day benchmark. It is not an entry filter, so a later board
    minute does not choose the refusal.
    """
    out = []
    for sym, mins in board.items():
        sip = sips.get(sym)
        if not sip:
            continue
        for m in mins:
            if not (E.WIN_START <= m <= E.WIN_END):
                continue
            g = E.markout(sip, m + 1)
            if g is not None and math.isfinite(g):
                out.append(g)
    return out


def day_mean_t(vals: list[float]):
    if not vals:
        return None, None, 0
    mu = float(statistics.mean(vals))
    if len(vals) < 3:
        return mu, None, len(vals)
    sd = float(statistics.stdev(vals))
    if sd == 0:
        return mu, None, len(vals)
    return mu, mu / (sd / math.sqrt(len(vals))), len(vals)


def _cell():
    return {"gross": [], "later": [], "cross": []}


def score_day(day: str, signals: dict, data: dict) -> dict:
    sips = {}
    for sym, frame in (data.get("sip") or {}).items():
        if frame is None:
            continue
        try:
            sips[str(sym).upper()] = E.sip_map(frame)
        except (TypeError, ValueError, KeyError):
            continue
    board = {str(sym).upper(): mins for sym, mins in (data.get("board") or {}).items()}
    cells = {r: _cell() for r in REASONS}
    dropped = {"no_sip": 0, "no_markout": 0, "no_control": 0}
    board_g = board_grosses(board, sips)
    added = set()
    for (sym, why), sm in signals.items():
        sip = sips.get(sym)
        if sip is None:
            dropped["no_sip"] += 1
            continue
        g = fill_gross(sip, sm)
        if g is None:
            dropped["no_markout"] += 1
            continue
        km = E.control_minute(sym, day, why, sip, board.get(sym) or set(), sm + 1)
        gc = None if km is None else E.markout(sip, km)
        if gc is None or not math.isfinite(gc):
            dropped["no_control"] += 1
            continue
        cells[why]["gross"].append(g)
        cells[why]["later"].append(g - gc)
        # A ledger minute the cache board missed still belongs in the benchmark, once.
        if sm not in (board.get(sym) or ()) and (sym, sm) not in added:
            board_g.append(g)
            added.add((sym, sm))
        xc = E.cross_control(sym, day, why, board, sips, sm)
        if xc is not None and math.isfinite(xc):
            cells[why]["cross"].append(g - xc)
    return {"day": day, "board": board_g, "cells": cells, "dropped": dropped}


def half_rows(per_day: list[dict], dates: list[str], cell: str) -> dict:
    by = {r["day"]: r for r in per_day}
    out = {}
    for label, pred in (
        ("A", lambda d: d <= CUT),
        ("B", lambda d: d > CUT),
        ("ALL", lambda d: True),
    ):
        gross_means, later_means, board_means = [], [], []
        ns = 0
        for d in dates:
            if not pred(d):
                continue
            row = by[d]
            g = row["cells"][cell]["gross"]
            lat = row["cells"][cell]["later"]
            b = row["board"]
            ns += len(g)
            if g:
                gross_means.append(float(statistics.mean(g)))
            if lat:
                later_means.append(float(statistics.mean(lat)))
            if g and b:
                board_means.append(float(statistics.mean(g)) - float(statistics.mean(b)))
        gm, gt, nd = day_mean_t(gross_means)
        lm, lt, _ld = day_mean_t(later_means)
        bm, bt, _bd = day_mean_t(board_means)
        out[label] = {
            "n": ns, "days": nd, "gross": gm, "t_gross": gt,
            "net3": None if gm is None else gm - COST3,
            "net10": None if gm is None else gm - COST10,
            "later": lm, "later_t": lt, "board": bm, "board_t": bt,
        }
    return out


def _net3_t(block: dict):
    """Day-clustered t of (day-mean gross - 3 bp)."""
    if block["gross"] is None or block["t_gross"] in (None, 0):
        return None
    se = block["gross"] / block["t_gross"]
    if se == 0:
        return None
    return (block["gross"] - COST3) / se


def _misses_pass(h: dict) -> bool:
    """A powered half that misses PASS. The board leg has to be positive. Its t is not in the bar."""
    if h["later"] is None or h["later"] < E.LIFT_BAR:
        return True
    if (h["later_t"] or -1) < 2:
        return True
    if h["net10"] is None or h["net10"] <= 0:
        return True
    return h["board"] is None or h["board"] <= 0


def _under(h: dict) -> bool:
    return h["n"] < E.MIN_SIGNALS or h["days"] < E.MIN_DAYS or h["gross"] is None


def verdict(cell: dict) -> str:
    """PASS / PROMISING / FAIL / UNDERPOWERED for one pre-declared reason."""
    a, b = cell["A"], cell["B"]
    if _under(a) or _under(b):
        powered = [h for h in (a, b) if not _under(h)]
        if any(_misses_pass(h) for h in powered):
            return "FAIL"
        return "UNDERPOWERED"
    money = (
        a["later"] is not None and b["later"] is not None
        and a["later"] >= E.LIFT_BAR and b["later"] >= E.LIFT_BAR
        and (a["later_t"] or -1) >= 2 and (b["later_t"] or -1) >= 2
        and a["net10"] > 0 and b["net10"] > 0
        and a["board"] is not None and b["board"] is not None
        and a["board"] > 0 and b["board"] > 0
    )
    if money:
        return "PASS"
    t3 = _net3_t(cell["ALL"])
    promising = (
        a["net3"] is not None and b["net3"] is not None
        and a["net3"] > 0 and b["net3"] > 0
        and (a["later"] or 0) > 0 and (b["later"] or 0) > 0
        and (a["board"] or 0) > 0 and (b["board"] or 0) > 0
        and t3 is not None and t3 >= 1.5
    )
    return "PROMISING" if promising else "FAIL"


def pooled_day(row: dict) -> dict:
    """Information only. A name in both reasons counts in both. Not a third cell."""
    cells = {
        "pooled": {
            "gross": row["cells"]["exh_falling"]["gross"] + row["cells"]["exh_not_tight"]["gross"],
            "later": row["cells"]["exh_falling"]["later"] + row["cells"]["exh_not_tight"]["later"],
            "cross": row["cells"]["exh_falling"]["cross"] + row["cells"]["exh_not_tight"]["cross"],
        }
    }
    return {"day": row["day"], "board": row["board"], "cells": cells}


def _bp(x) -> str:
    return "n/a" if x is None else f"{x * 1e4:+.1f}"


def _t(x) -> str:
    return "n/a" if x is None else f"{x:+.2f}"


def _line(name: str, rows: dict) -> str:
    def cell(h):
        r = rows[h]
        return (f"{_bp(r['gross'])} (t {_t(r['t_gross'])}, n {r['n']}, days {r['days']}, "
                f"net3 {_bp(r['net3'])}, net10 {_bp(r['net10'])}, "
                f"later {_bp(r['later'])} t {_t(r['later_t'])}, "
                f"board {_bp(r['board'])} t {_t(r['board_t'])})")
    return f"{name}: A {cell('A')} | B {cell('B')}"


def _cross_line(per_day: list[dict], dates: list[str], cell: str) -> str:
    by = {r["day"]: r for r in per_day}
    parts = []
    for label, pred in (("A", lambda d: d <= CUT), ("B", lambda d: d > CUT)):
        means = []
        for d in dates:
            if not pred(d):
                continue
            xs = by[d]["cells"][cell]["cross"]
            if xs:
                means.append(float(statistics.mean(xs)))
        mu, t, n = day_mean_t(means)
        parts.append(f"{label} {_bp(mu)} (t {_t(t)}, days {n})")
    return f"{cell} vs another name, same minute: " + " | ".join(parts)


def report(per_day: list[dict], dates: list[str]) -> str:
    lines = [
        "# Refusal reasons, from the refusal minute (refuse_reason_prereg)",
        "",
        f"Pinned {len(dates)} sessions {dates[0]}..{dates[-1]}. Half A through {CUT}.",
        "Two graded cells. A pass is 1 of 2. The pooled line is not graded.",
        "",
    ]
    for reason in REASONS:
        block = half_rows(per_day, dates, reason)
        lines.append(f"**Verdict {reason}:** {verdict(block)}")
        lines.append(_line(reason, block))
        lines.append("")
    wrapped = [pooled_day(r) for r in per_day]
    lines.append("Information only, not graded:")
    lines.append(_line("pooled reasons", half_rows(wrapped, dates, "pooled")))
    for reason in REASONS:
        lines.append(_cross_line(per_day, dates, reason))
    lines.append(_cross_line(wrapped, dates, "pooled"))
    lines.append("")
    bits = []
    for r in per_day:
        d = r["dropped"]
        bits.append(f"{r['day']} {d['no_sip']}/{d['no_markout']}/{d['no_control']}")
    lines.append("Drops by day (no SIP name / no markout / no later control): " + ", ".join(bits) + ".")
    lines.append(f"{CLOSED_SATURDAY} is a Saturday. It is not in the pin.")
    return "\n".join(lines) + "\n"


def scored_days(intersection) -> list[str]:
    """The pin, after the closed Saturday is set aside. Any other difference aborts."""
    got = sorted(set(intersection) - {CLOSED_SATURDAY})
    want = list(PIN)
    if got != want:
        raise SystemExit(
            f"REFUSING: ledger∩cache must be the pinned {len(want)} sessions. "
            f"missing {[d for d in want if d not in got]}; "
            f"extra {[d for d in got if d not in want]}"
        )
    return got


def sip_miss_share(data: dict):
    """Share of cached board names with no SIP frame. None when the board is empty."""
    board = {str(s).upper() for s in (data.get("board") or {})}
    sip = {str(s).upper() for s in (data.get("sip") or {})}
    if not board:
        return None
    return sum(1 for s in board if s not in sip) / len(board)


def require_sip_coverage(day: str, data: dict) -> None:
    """Abort the run. A thin SIP day is not dropped quietly and not scored."""
    share = sip_miss_share(data)
    if share is None or share > E.MAX_MISS:
        shown = "empty board" if share is None else f"{share:.1%}"
        raise SystemExit(f"REFUSING: {day} SIP coverage is {shown} (cap {E.MAX_MISS:.0%})")


def pinned_days() -> list[str]:
    """Ledger days that also have an early-entry cache. Anything but the pin aborts."""
    ledger = set(E.ledger_days())
    cache = []
    if os.path.isdir(E.OUT_DIR):
        cache = [f[:-4] for f in os.listdir(E.OUT_DIR) if f.endswith(".pkl") and len(f) == 14]
    return scored_days(ledger & set(cache))


def _load_day(day: str) -> dict:
    path = os.path.join(E.OUT_DIR, f"{day}.pkl")
    try:
        with open(path, "rb") as f:
            data = pickle.load(f)
    except (OSError, pickle.UnpicklingError, EOFError) as exc:
        raise SystemExit(f"REFUSING: cache {day} did not load ({exc})") from exc
    if not isinstance(data, dict) or data.get("day") not in (None, day):
        raise SystemExit(f"REFUSING: cache {day} is not a day record")
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    dates = pinned_days()
    n_a = sum(1 for d in dates if d <= CUT)
    print(f"sessions {len(dates)} {dates[0]}..{dates[-1]}; A through {CUT} ({n_a}/{len(dates) - n_a})",
          flush=True)
    if not a.score:
        print("inventory only. --score after a result-skeptic READY.", flush=True)
        return
    per = []
    for i, day in enumerate(dates):
        ledger = os.path.join(E.LEDGER_DIR, f"{day}.jsonl")
        signals = read_ledger(ledger)
        data = _load_day(day)
        require_sip_coverage(day, data)
        row = score_day(day, signals, data)
        per.append(row)
        d = row["dropped"]
        n = sum(len(row["cells"][r]["gross"]) for r in REASONS)
        print(f"  {day} ({i + 1}/{len(dates)}) signals {n} "
              f"no_sip {d['no_sip']} no_markout {d['no_markout']} no_control {d['no_control']}",
              flush=True)
    text = report(per, dates)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(text, flush=True)


if __name__ == "__main__":
    main()
