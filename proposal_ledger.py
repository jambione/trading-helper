"""Proposal ledger — attributed seed/inclusion decisions (observe-only).

Append-only day-split JSONL of proposal decisions at seed and inclusion
stages, **kept and dropped**, with ``proposer`` (raw, pre-merge) separate
from ``owner`` (post-``_merge_source`` when known).

Fail-open: write errors never raise into trading / sync. Does **not** change
keep/arm/exit. Additive to ``admit_ledger`` (refuse-only uncapped dig).

Volume policy (disk risk): write on first sight or when ``decision`` /
``reason`` changes; heartbeat every ``ai_proposal_ledger_heartbeat_sec``
(default 300) while state is unchanged so dwell stays reconstructible.
Target: hundreds → low thousands of rows per RTH day — not ~356k.

Day files::

    ai_reports/proposal_ledger/YYYY-MM-DD.jsonl   # ET calendar day

Soft-seed paths are out of v1 (follow-up).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from source_norm import normalize_proposer

ET = ZoneInfo("America/New_York")

_lock = threading.Lock()
# Test override — when set, all writes go here (single file, no day-split).
_PATH_OVERRIDE: Path | None = None

# In-process last-write map: (symbol, proposer_norm, stage) -> state.
# Cleared by tests via reset_dedupe_state() and on ET day rollover.
_last: dict[tuple[str, str, str], dict[str, Any]] = {}
# Seed-stage proposers seen this ET day: symbol -> {proposer_norm}. The
# shortlist row carries only the first-wins winner (later sources hit `seen`
# and continue), so inclusion rows cannot name the true proposer on their own.
# This records every proposer at seed stage so inclusion can say who was there.
_seed_proposers: dict[str, set[str]] = {}
# ET day the two maps above belong to. A rollover clears both: otherwise the
# first row of a new day for an unchanged name is suppressed until the
# heartbeat fires, and the scorecard's "first proposal" anchor lands late on a
# heartbeat row. Also bounds memory to one session.
_cur_day: str | None = None


def _report_dir() -> Path:
    try:
        from ai_paths import resolve_report_dir
        return resolve_report_dir()
    except Exception:
        return Path(__file__).resolve().parent / "ai_reports"


def ledger_path_for_ts(ts: float | None = None) -> Path:
    """Day-split path for *ts* (unix). Uses ET calendar date."""
    if _PATH_OVERRIDE is not None:
        return _PATH_OVERRIDE
    t = float(ts if ts is not None else time.time())
    day = datetime.fromtimestamp(t, tz=ET).strftime("%Y-%m-%d")
    return _report_dir() / "proposal_ledger" / f"{day}.jsonl"


def set_ledger_path_for_tests(path: Path | None) -> None:
    """Point writes at a temp file (tests). ``None`` restores day-split."""
    global _PATH_OVERRIDE
    _PATH_OVERRIDE = path


def reset_dedupe_state() -> None:
    """Clear in-process dedupe / attribution state (tests, day rollover)."""
    global _cur_day
    with _lock:
        _last.clear()
        _seed_proposers.clear()
        _cur_day = None


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _regime_fields() -> dict[str, Any]:
    try:
        from learn_stamps import regime_stamp
        st = regime_stamp()
        return {
            "git_version": st.get("git_version"),
            "config_fp": st.get("config_fp"),
        }
    except Exception:
        return {"git_version": None, "config_fp": None}


def _pick_features(row: dict[str, Any] | None, extra: dict[str, Any] | None) -> dict[str, Any]:
    """Compact features from row/extra; omit nulls."""
    src: dict[str, Any] = {}
    if isinstance(row, dict):
        src.update(row)
    if isinstance(extra, dict):
        src.update(extra)

    out: dict[str, Any] = {}

    px = _f(src.get("px"))
    if px is None:
        px = _f(src.get("price"))
    if px is not None:
        out["px"] = px

    pct = _f(src.get("pct_change"))
    if pct is None:
        pct = _f(src.get("pct"))
    if pct is not None:
        out["pct_change"] = pct

    rvol = _f(src.get("rvol"))
    if rvol is not None:
        out["rvol"] = rvol

    rvol_raw = _f(src.get("rvol_raw"))
    if rvol_raw is not None:
        out["rvol_raw"] = rvol_raw

    # admission_filter writes the canonical value onto the row as
    # ``admit_range_pos`` (admission_filter.py:140); its snapshot payload uses
    # the bare ``range_pos``. Read both or the largest inclusion gate
    # (admit_range_pos, ~37% of refuses) is logged without the input that
    # caused it. Explicit None checks — 0.0 is a legitimate range position.
    range_pos = _f(src.get("range_pos"))
    if range_pos is None:
        range_pos = _f(src.get("admit_range_pos"))
    if range_pos is not None:
        out["range_pos"] = range_pos

    float_m = _f(src.get("float_m"))
    if float_m is None:
        float_m = _f(src.get("float"))
    if float_m is not None:
        out["float_m"] = float_m

    spread_r = _f(src.get("spread_r"))
    if spread_r is None:
        spread_r = _f(src.get("spread"))
    if spread_r is not None:
        out["spread_r"] = spread_r

    if "seen_before" in src:
        out["seen_before"] = bool(src.get("seen_before"))

    bucket = src.get("bucket")
    if bucket is not None and str(bucket).strip():
        out["bucket"] = str(bucket).strip()

    return out


def _enabled(cfg: dict[str, Any] | None, stage: str) -> bool:
    c = cfg if isinstance(cfg, dict) else {}
    if not bool(c.get("ai_proposal_ledger_enabled", True)):
        return False
    if stage == "seed":
        return bool(c.get("ai_proposal_ledger_seed", True))
    if stage == "inclusion":
        return bool(c.get("ai_proposal_ledger_inclusion", True))
    return False


def _heartbeat_sec(cfg: dict[str, Any] | None) -> float:
    c = cfg if isinstance(cfg, dict) else {}
    try:
        return max(0.0, float(c.get("ai_proposal_ledger_heartbeat_sec", 300) or 300))
    except (TypeError, ValueError):
        return 300.0


def log_proposal(
    *,
    stage: str,
    symbol: str,
    proposer: str,
    decision: str,
    reason: str | None = None,
    owner: str | None = None,
    row: dict | None = None,
    extra: dict | None = None,
    ts: float | None = None,
    cfg: dict | None = None,
    force_heartbeat: bool = False,
) -> bool:
    """Append one proposal row if first / state-change / heartbeat. Never raises."""
    try:
        stage_s = str(stage or "").strip().lower()
        if stage_s not in ("seed", "inclusion"):
            return False
        if not _enabled(cfg, stage_s):
            return True

        sym = str(symbol or "").upper().strip()
        if not sym:
            return False

        dec = str(decision or "").strip().lower()
        if dec not in ("kept", "dropped"):
            return False

        prop_raw = str(proposer or "").strip().lower()
        if not prop_raw and isinstance(row, dict):
            prop_raw = str(row.get("source") or "").strip().lower()
        prop_norm = normalize_proposer(prop_raw)

        why: str | None
        if dec == "kept":
            why = None
        else:
            why = str(reason or "").strip() or "unknown"

        t = float(ts if ts is not None else time.time())
        day = datetime.fromtimestamp(t, tz=ET).strftime("%Y-%m-%d")
        hb_sec = _heartbeat_sec(cfg)
        key = (sym, prop_norm, stage_s)

        with _lock:
            global _cur_day
            if day != _cur_day:
                _last.clear()
                _seed_proposers.clear()
                _cur_day = day
            if stage_s == "seed":
                # Record before the dedupe decision: a suppressed row still
                # means this proposer named the symbol today.
                _seed_proposers.setdefault(sym, set()).add(prop_norm)
            prev = _last.get(key)
            is_heartbeat = False
            if prev is None:
                should_write = True
            elif str(prev.get("decision")) != dec or prev.get("reason") != why:
                should_write = True
            elif force_heartbeat or (
                hb_sec > 0 and (t - float(prev.get("ts") or 0)) >= hb_sec
            ):
                should_write = True
                is_heartbeat = True
            else:
                should_write = False

            if not should_write:
                return True

            out: dict[str, Any] = {
                "ts": round(t, 2),
                "day": day,
                "stage": stage_s,
                "symbol": sym,
                "proposer": prop_raw or "unknown",
                "proposer_norm": prop_norm,
                "decision": dec,
                "heartbeat": bool(is_heartbeat),
            }
            if stage_s == "inclusion":
                # `proposer` here is the shortlist row's source — the first-wins
                # winner, not necessarily who found the name. Say so, and carry
                # everyone who proposed it at seed so attribution stays
                # recoverable without re-deriving first-wins order.
                seen_props = sorted(_seed_proposers.get(sym, ()))
                if seen_props:
                    out["proposers_seen"] = seen_props
                    out["proposer_collapsed"] = len(seen_props) > 1
            if why is not None:
                out["reason"] = why
            if owner is not None and str(owner).strip():
                out["owner"] = str(owner).strip().lower()
            elif stage_s == "seed":
                out["owner"] = None

            out.update(_pick_features(row, extra))
            out.update(_regime_fields())

            path = ledger_path_for_ts(t)
            line = json.dumps(out, default=str) + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line)

            _last[key] = {"ts": t, "decision": dec, "reason": why}
        return True
    except Exception:
        return False


__all__ = [
    "ledger_path_for_ts",
    "log_proposal",
    "reset_dedupe_state",
    "set_ledger_path_for_tests",
]
