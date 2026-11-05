"""AI pick pre-check (operator 11/05, running list row 85 item 2): which of the AI picks would the desk's door refuse?

OBSERVE ONLY. Runs the door's own checks on every pick now on the research boards (agy / xai / seed-rank), WITHOUT the door's
side effects (passes_inclusion consumes reseed flags and notes uptrend times, so it is not called here). Same numbers the door
uses: levered ETP, the source's price band, admit_arm_gates (price band, SIP spread, gap-down), a known float over
ai_watch_max_float_m, and a name that is down on the day (ai_watch_require_uptrend) outside its grace. Unknown never refuses,
exactly as at the door. Writes ai_reports/pick_precheck/<day>.jsonl (one row per pick per change) and
ai_reports/pick_precheck_latest.json (what the dashboard / a backfill step reads).

Nothing here removes a pick or asks a model for more; the point is to count, by reason, how many picks the door refuses so the
backfill ('ask for more, publish only passers') can be built on a measured refusal rate, not a guess.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "ai_reports" / "pick_precheck"
LATEST = ROOT / "ai_reports" / "pick_precheck_latest.json"


def refusal(row: dict, cfg: dict, *, now: float | None = None) -> str:
    """'' when the door would not refuse this pick on KNOWN facts, else the reason. No state is written."""
    import ai_entry_watch as ew
    t = float(now if now is not None else time.time())
    sym = str(row.get("symbol") or "").upper().strip()
    if not sym or not ew._watch_symbol_ok(sym, cfg):
        return "bad_symbol"
    if ew.is_levered_etp(sym):
        return "levered_etp"
    ok, why = ew.admit_arm_gates(row, cfg, now=t)
    if not ok:
        return why
    try:
        max_float = float(cfg.get("ai_watch_max_float_m", 0) or 0)
    except (TypeError, ValueError):
        max_float = 0.0
    if max_float > 0 and str(row.get("source") or "").lower() != "tight":
        try:
            import float_feed
            fl = float_feed.float_shares(sym)
        except Exception:  # noqa: BLE001
            fl = None
        if fl is not None and fl > max_float:
            return "float_too_big"
    if bool(cfg.get("ai_watch_require_uptrend", True)):
        pct = ew._pct_change_value(row.get("pct_change"))
        if pct is not None and pct <= 0 and not ew.uptrend_in_grace(sym, cfg, t):
            return "not_uptrend"
    return ""


def check_boards(cfg: dict, *, now: float | None = None) -> list[dict]:
    """One row per pick: symbol, source, price, pct_change, refused_by ('' = would be admitted)."""
    import ai_entry_watch as ew
    t = float(now if now is not None else time.time())
    desk_rows, tr_by = ew._live_quote_map()
    out: list[dict] = []
    for r in ew.research_candidate_rows():
        s = str(r.get("symbol") or "").upper().strip()
        if not s:
            continue
        live, tr = desk_rows.get(s) or {}, tr_by.get(s) or {}
        def pick(k):
            for src in (live, tr, r):
                if src.get(k) is not None:
                    return src.get(k)
            return None
        row = dict(r)
        row.update({"price": pick("price"), "pct_change": pick("pct_change"), "rvol": pick("rvol")})
        try:
            why = refusal(row, cfg, now=t)
        except Exception as e:  # noqa: BLE001
            why = ""   # a pre-check bug must read as 'not refused', never as a refusal
            row["precheck_error"] = str(e)[:80]
        out.append({"symbol": s, "source": str(r.get("source") or ""), "price": row["price"],
                    "pct_change": row["pct_change"], "refused_by": why, **({"error": row["precheck_error"]} if "precheck_error" in row else {})})
    return out


_LAST_SIG: dict[str, Any] = {"sig": None}


def in_session(t: float) -> bool:
    """09:25-16:00 ET on a weekday. Before the open every pick's day change reads exactly 0.0 (no print yet), which the
    uptrend check would call a refusal for every pick - an artifact, not a verdict (seen on the mini 11/05 05:50)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = datetime.fromtimestamp(t, ZoneInfo("America/New_York"))
    return et.weekday() < 5 and 9 * 60 + 25 <= et.hour * 60 + et.minute < 16 * 60


def tick(cfg: dict, now: float | None = None) -> dict:
    """Observe-only. Writes the ledger when the verdicts change, the latest file always. Never raises."""
    try:
        if not bool((cfg or {}).get("ai_pick_precheck_log", True)):
            return {}
        t = float(now if now is not None else time.time())
        if not in_session(t):
            return {}
        rows = check_boards(cfg, now=t)
        sig = tuple(sorted((r["symbol"], r["refused_by"]) for r in rows))
        refused = [r for r in rows if r["refused_by"]]
        summary = {"ts": t, "n": len(rows), "refused": len(refused),
                   "by_reason": {k: sum(1 for r in refused if r["refused_by"] == k) for k in sorted({r["refused_by"] for r in refused})},
                   "rows": rows}
        LATEST.parent.mkdir(parents=True, exist_ok=True)
        LATEST.write_text(json.dumps(summary, indent=1, default=str))
        if sig != _LAST_SIG["sig"]:
            _LAST_SIG["sig"] = sig
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            day = time.strftime("%Y-%m-%d", time.localtime(t))
            with open(OUT_DIR / f"{day}.jsonl", "a") as fh:
                fh.write(json.dumps(summary, default=str) + "\n")
        return summary
    except Exception:  # noqa: BLE001
        return {}
