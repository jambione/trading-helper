"""Daily A vs X paper duel: dual trial → force-flat → R score → winner chance 3.

Rules (operator design):
  • Both models post one top suggestion on AI Watch (A / X).
  • Zone hit → existing entry + mechanical sell logic.
  • Before chance 3, liquidate both duel legs and score **realized R**.
  • Higher R runs chance 3 alone; loser sits out new entries.

State file: ``{report_dir}/duel_state.json``.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ai_paths import resolve_report_dir

ET = ZoneInfo("America/New_York")
DUEL_STATE_PATH = resolve_report_dir() / "duel_state.json"

SOURCE_A = "anthropic"
SOURCE_X = "xai"
_VALID_SOURCES = frozenset({SOURCE_A, SOURCE_X, "claude", "grok"})


def _norm_source(raw: str | None) -> str | None:
    s = str(raw or "").strip().lower()
    if s in ("anthropic", "claude", "a"):
        return SOURCE_A
    if s in ("xai", "grok", "x"):
        return SOURCE_X
    return None


def _parse_hhmm(raw: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        hh, mm = str(raw or "").strip().split(":")
        return int(hh), int(mm)
    except Exception:
        return default


def _et_day_key(now: float | None = None) -> str:
    from datetime import datetime

    t0 = float(now if now is not None else time.time())
    return datetime.fromtimestamp(t0, tz=ET).strftime("%Y-%m-%d")


def _et_hm(now: float | None = None) -> tuple[int, int]:
    from datetime import datetime

    t0 = float(now if now is not None else time.time())
    dt = datetime.fromtimestamp(t0, tz=ET)
    return dt.hour, dt.minute


def _empty_state(day: str) -> dict[str, Any]:
    return {
        "day": day,
        "phase": "trial",  # trial | scored | chance3 | done
        "champions": {},  # source -> record
        "winner": None,
        "trial_liquidated": False,
        "score": {},
        "updated": time.time(),
    }


def load_state(now: float | None = None) -> dict[str, Any]:
    day = _et_day_key(now)
    try:
        raw = json.loads(DUEL_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and str(raw.get("day") or "") == day:
            return raw
    except Exception:
        pass
    return _empty_state(day)


def save_state(state: dict[str, Any]) -> None:
    try:
        DUEL_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        state = dict(state or {})
        state["updated"] = time.time()
        tmp = DUEL_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        tmp.replace(DUEL_STATE_PATH)
    except Exception:
        pass


def duel_enabled(cfg: dict | None) -> bool:
    cfg = cfg if isinstance(cfg, dict) else {}
    return bool(cfg.get("ai_duel_enabled", True))


def trial_end_hm(cfg: dict | None) -> tuple[int, int]:
    cfg = cfg if isinstance(cfg, dict) else {}
    return _parse_hhmm(str(cfg.get("ai_duel_trial_end_time") or "14:15"), (14, 15))


def chance3_hm(cfg: dict | None) -> tuple[int, int]:
    cfg = cfg if isinstance(cfg, dict) else {}
    return _parse_hhmm(str(cfg.get("ai_duel_chance3_time") or "14:30"), (14, 30))


def past_trial_end(cfg: dict | None, now: float | None = None) -> bool:
    h, m = _et_hm(now)
    th, tm = trial_end_hm(cfg)
    return (h, m) >= (th, tm)


def past_chance3_start(cfg: dict | None, now: float | None = None) -> bool:
    h, m = _et_hm(now)
    th, tm = chance3_hm(cfg)
    return (h, m) >= (th, tm)


def _top_row(rows: list[dict]) -> dict | None:
    best = None
    best_sc = None
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        sc = r.get("trending_score", r.get("score"))
        try:
            sc_f = float(sc) if sc is not None else 0.0
        except (TypeError, ValueError):
            sc_f = 0.0
        if best is None or sc_f > (best_sc or -1e9):
            best = r
            best_sc = sc_f
    return best


def register_champion_from_rows(
    rows: list[dict],
    *,
    source: str,
    cfg: dict | None,
    now: float | None = None,
    chance: int | None = None,
) -> dict[str, Any] | None:
    """Take top-scoring row for *source* and register as today's duel champion."""
    if not duel_enabled(cfg):
        return None
    src = _norm_source(source)
    if not src:
        return None
    t0 = float(now if now is not None else time.time())
    state = load_state(t0)
    phase = str(state.get("phase") or "trial")

    # After score, only winner may register a chance-3 champion.
    if phase in ("scored", "chance3"):
        winner = _norm_source(state.get("winner"))
        if not winner or src != winner:
            return None
        if not past_chance3_start(cfg, t0):
            return None
        ch = 3
    elif phase == "done":
        return None
    else:
        # Trial: do not overwrite a champion that already traded / closed.
        prev = (state.get("champions") or {}).get(src) or {}
        if prev.get("status") in ("open", "closed", "submitted"):
            return None
        ch = int(chance or 1)

    top = _top_row(list(rows or []))
    if not top:
        return None
    sym = str(top.get("symbol") or "").upper().strip()
    if not sym:
        return None

    rec = {
        "symbol": sym,
        "source": src,
        "source_mark": "A" if src == SOURCE_A else "X",
        "chance": ch,
        "score": top.get("trending_score", top.get("score")),
        "reason": str(top.get("reason") or "")[:120],
        "status": "watching",
        "entry_price": None,
        "stop_price": None,
        "exit_price": None,
        "realized_r": None,
        "realized_pl": None,
        "registered_ts": t0,
    }
    champs = dict(state.get("champions") or {})
    # Chance 3 replaces only that source's record after trial.
    champs[src] = rec
    state["champions"] = champs
    if ch == 3:
        state["phase"] = "chance3"
    save_state(state)

    # Put on AI Watch with duel metadata.
    try:
        import ai_entry_watch as ew

        st = ew.load_watch()
        if not isinstance(st, dict):
            st = {}
        prev = st.get(sym) if isinstance(st.get(sym), dict) else {}
        prev_status = str(prev.get("status") or "").lower()
        status = prev_status if prev_status in ("submitted", "filled") else "watching"
        st[sym] = {
            **prev,
            "symbol": sym,
            "status": status or "watching",
            "agreement": bool(top.get("agreement")),
            "score": rec["score"],
            "reason": rec["reason"],
            "source": src,
            "duel": True,
            "duel_source": src,
            "duel_chance": ch,
            "duel_day": state.get("day"),
            "source_mark": rec["source_mark"],
            "structure": prev.get("structure"),
            "structure_ts": prev.get("structure_ts", 0.0),
            "last_poll_ts": prev.get("last_poll_ts", 0.0),
            "last_ask": prev.get("last_ask"),
            "updated_ts": t0,
        }
        ew.save_watch(st)
    except Exception:
        pass

    try:
        import ai_positions as cp

        cp.log_event(
            "duel_champion",
            symbol=sym,
            source=src,
            chance=ch,
            score=rec.get("score"),
        )
    except Exception:
        pass
    return rec


def note_entry(
    symbol: str,
    *,
    source: str | None,
    entry_price: float | None,
    stop_price: float | None,
    now: float | None = None,
) -> None:
    """Tag managed entry as the open duel leg for this source."""
    t0 = float(now if now is not None else time.time())
    state = load_state(t0)
    src = _norm_source(source)
    sym = str(symbol or "").upper().strip()
    if not src or not sym:
        return
    champs = dict(state.get("champions") or {})
    rec = champs.get(src)
    if not isinstance(rec, dict):
        return
    if str(rec.get("symbol") or "").upper() != sym:
        return
    rec = dict(rec)
    rec["status"] = "open"
    if entry_price is not None:
        rec["entry_price"] = float(entry_price)
    if stop_price is not None:
        rec["stop_price"] = float(stop_price)
    rec["opened_ts"] = t0
    champs[src] = rec
    state["champions"] = champs
    save_state(state)


def champion_symbols(state: dict | None = None) -> set[str]:
    st = state if isinstance(state, dict) else load_state()
    out: set[str] = set()
    for rec in (st.get("champions") or {}).values():
        if not isinstance(rec, dict):
            continue
        sym = str(rec.get("symbol") or "").upper().strip()
        if sym:
            out.add(sym)
    return out


def allow_entry_for_source(
    cfg: dict | None,
    source: str | None,
    symbol: str | None = None,
    *,
    now: float | None = None,
) -> bool:
    """Whether *source* may open a new paper entry under duel rules."""
    if not duel_enabled(cfg):
        return True
    t0 = float(now if now is not None else time.time())
    state = load_state(t0)
    src = _norm_source(source)
    phase = str(state.get("phase") or "trial")
    sym = str(symbol or "").upper().strip()

    if phase == "done":
        return False

    if phase == "trial":
        if past_trial_end(cfg, t0):
            return False  # waiting for liquidate/score
        if not src:
            # Unknown source: only allow if symbol is a registered champion.
            return bool(sym and sym in champion_symbols(state))
        rec = (state.get("champions") or {}).get(src) or {}
        if not rec:
            return False
        if sym and str(rec.get("symbol") or "").upper() != sym:
            return False
        if rec.get("status") in ("closed",):
            return False
        return True

    # scored / chance3: winner only
    winner = _norm_source(state.get("winner"))
    if not winner:
        return False
    if src and src != winner:
        return False
    if not past_chance3_start(cfg, t0):
        return False
    rec = (state.get("champions") or {}).get(winner) or {}
    if phase == "chance3" and rec.get("chance") == 3:
        if sym and str(rec.get("symbol") or "").upper() != sym:
            return False
    return True


def allow_entry_symbol(cfg: dict | None, symbol: str | None, *, now: float | None = None) -> bool:
    """Symbol may be entered if it is an active duel champion for an allowed source."""
    if not duel_enabled(cfg):
        return True
    sym = str(symbol or "").upper().strip()
    if not sym:
        return False
    t0 = float(now if now is not None else time.time())
    state = load_state(t0)
    for src, rec in (state.get("champions") or {}).items():
        if not isinstance(rec, dict):
            continue
        if str(rec.get("symbol") or "").upper() != sym:
            continue
        return allow_entry_for_source(cfg, src, sym, now=t0)
    return False


def _compute_r(entry: float | None, stop: float | None, exit_px: float | None) -> float | None:
    try:
        e = float(entry) if entry is not None else None
        s = float(stop) if stop is not None else None
        x = float(exit_px) if exit_px is not None else None
    except (TypeError, ValueError):
        return None
    if e is None or s is None or x is None:
        return None
    risk = e - s
    if risk <= 0:
        return None
    return (x - e) / risk


def run_trial_liquidate_and_score(
    cfg: dict | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Force-flat both duel champions, score realized R, pick winner for chance 3."""
    if not duel_enabled(cfg):
        return {"ok": False, "reason": "disabled"}
    t0 = float(now if now is not None else time.time())
    state = load_state(t0)
    if state.get("trial_liquidated") or str(state.get("phase")) not in ("trial",):
        if state.get("trial_liquidated"):
            return {"ok": True, "reason": "already_done", "winner": state.get("winner")}
        return {"ok": False, "reason": "bad_phase", "phase": state.get("phase")}
    if not past_trial_end(cfg, t0):
        return {"ok": False, "reason": "before_trial_end"}

    import alpaca_trader

    print("[ai] DUEL trial end — liquidate A/X champions and score R", flush=True)
    detail = {}
    try:
        detail = alpaca_trader.get_positions_detail() or {}
    except Exception:
        detail = {}

    # Managed state for stops/entries
    managed = {}
    try:
        import ai_positions as cp

        managed = cp._load_state() if hasattr(cp, "_load_state") else {}
    except Exception:
        managed = {}

    champs = dict(state.get("champions") or {})
    score: dict[str, Any] = {}

    for src in (SOURCE_A, SOURCE_X):
        rec = dict(champs.get(src) or {})
        sym = str(rec.get("symbol") or "").upper().strip()
        if not sym:
            score[src] = {"realized_r": 0.0, "symbol": None, "note": "no_champion"}
            continue

        live = detail.get(sym) if isinstance(detail, dict) else None
        mpos = managed.get(sym) if isinstance(managed, dict) else None
        entry = rec.get("entry_price")
        stop = rec.get("stop_price")
        if mpos and isinstance(mpos, dict):
            entry = entry if entry is not None else mpos.get("entry_price")
            stop = stop if stop is not None else mpos.get("stop_price")
            if mpos.get("last_seen_price") is not None and live is None:
                # already flat; use last seen
                pass

        exit_px = None
        if live and isinstance(live, dict):
            exit_px = live.get("current") or live.get("current_price")
            if entry is None:
                entry = live.get("avg_entry")
        elif mpos and isinstance(mpos, dict):
            exit_px = mpos.get("last_seen_price") or mpos.get("entry_price")

        # Close if still open
        closed_ok = True
        if live:
            try:
                out = alpaca_trader.close_out(sym)
                closed_ok = bool(out.get("ok"))
                # refresh exit approx
                if exit_px is None:
                    exit_px = live.get("current")
            except Exception as e:  # noqa: BLE001
                closed_ok = False
                print(f"[ai] duel close {sym} failed: {e}", flush=True)

        r = _compute_r(entry, stop, exit_px)
        # Never filled / no risk basis → 0 R for fair "no trade"
        if r is None:
            r = 0.0
        pl = None
        try:
            if live and live.get("pl") is not None:
                pl = float(live.get("pl"))
            elif entry is not None and exit_px is not None and mpos:
                qty = float(mpos.get("total_qty") or 0)
                if qty:
                    pl = (float(exit_px) - float(entry)) * qty
        except (TypeError, ValueError):
            pl = None

        rec.update({
            "status": "closed",
            "entry_price": entry,
            "stop_price": stop,
            "exit_price": exit_px,
            "realized_r": r,
            "realized_pl": pl,
            "closed_ts": t0,
            "close_ok": closed_ok,
        })
        champs[src] = rec
        score[src] = {
            "symbol": sym,
            "realized_r": r,
            "realized_pl": pl,
            "entry_price": entry,
            "stop_price": stop,
            "exit_price": exit_px,
        }

    # Winner = higher realized R; strict ties → no winner (skip chance 3)
    r_a = float((score.get(SOURCE_A) or {}).get("realized_r") or 0.0)
    r_x = float((score.get(SOURCE_X) or {}).get("realized_r") or 0.0)
    winner = None
    if r_a > r_x:
        winner = SOURCE_A
    elif r_x > r_a:
        winner = SOURCE_X

    state["champions"] = champs
    state["score"] = score
    state["winner"] = winner
    state["trial_liquidated"] = True
    state["phase"] = "scored" if winner else "done"
    state["scored_ts"] = t0
    save_state(state)

    try:
        import ai_positions as cp

        cp.log_event(
            "duel_trial_scored",
            winner=winner,
            r_anthropic=r_a,
            r_xai=r_x,
            score=score,
        )
    except Exception:
        pass

    print(
        f"[ai] DUEL score A_R={r_a:+.3f} X_R={r_x:+.3f} winner={winner or 'tie/none'}",
        flush=True,
    )
    return {
        "ok": True,
        "winner": winner,
        "score": score,
        "r_anthropic": r_a,
        "r_xai": r_x,
    }


def trial_liquidate_due(cfg: dict | None, now: float | None = None) -> bool:
    if not duel_enabled(cfg):
        return False
    t0 = float(now if now is not None else time.time())
    if not past_trial_end(cfg, t0):
        return False
    state = load_state(t0)
    return not bool(state.get("trial_liquidated")) and str(state.get("phase") or "") == "trial"


def public_snapshot(cfg: dict | None = None, now: float | None = None) -> dict[str, Any]:
    """Small dict for dashboard wire / debugging."""
    t0 = float(now if now is not None else time.time())
    st = load_state(t0)
    return {
        "enabled": duel_enabled(cfg),
        "day": st.get("day"),
        "phase": st.get("phase"),
        "winner": st.get("winner"),
        "trial_liquidated": st.get("trial_liquidated"),
        "score": st.get("score") or {},
        "champions": st.get("champions") or {},
        "trial_end": "%02d:%02d" % trial_end_hm(cfg),
        "chance3_start": "%02d:%02d" % chance3_hm(cfg),
    }
