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

import desk_core
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


_parse_hhmm = desk_core.parse_hhmm


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
        "trial_liquidated": False,  # True after final dual window cut
        "score": {},  # last window score
        "windows_scored": [],  # cut keys e.g. "11:20"
        "window_history": [],  # per-window score cards
        "totals": {SOURCE_A: 0.0, SOURCE_X: 0.0},  # sum realized R
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
    except Exception as e:  # noqa: BLE001
        print(f"[ai] duel save_state failed path={DUEL_STATE_PATH}: {e}", flush=True)


def duel_enabled(cfg: dict | None) -> bool:
    """Whether A vs X duel mechanics are on.

    Default is **False** (safe off) when the key is missing — matches
    ``DEFAULT_CONFIG`` / ``bot_config.json``. Opt in with ``ai_duel_enabled: true``.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    return bool(cfg.get("ai_duel_enabled", False))


def close_before_research_min(cfg: dict | None) -> int:
    """Minutes before the next research slot when the duel window force-flats."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        return max(1, int(cfg.get("ai_duel_close_before_research_min", 10) or 10))
    except (TypeError, ValueError):
        return 10


def research_times_hm(cfg: dict | None) -> list[tuple[int, int]]:
    """Scheduled research slots (ET) — shared Claude/Grok list."""
    cfg = cfg if isinstance(cfg, dict) else {}
    raw = (
        cfg.get("claude_research_times")
        or cfg.get("grok_research_times")
        or cfg.get("ai_research_times")
        or ["08:30", "11:30", "14:30"]
    )
    try:
        from ai_suggest import parse_research_times
        times = parse_research_times(raw)
        if times:
            return times
    except Exception:
        pass
    return [(8, 30), (11, 30), (14, 30)]


def chance3_hm(cfg: dict | None) -> tuple[int, int]:
    """Last research slot = winner-only chance 3 (default 14:30)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    times = research_times_hm(cfg)
    if times:
        return times[-1]
    return _parse_hhmm(str(cfg.get("ai_duel_chance3_time") or "14:30"), (14, 30))


def trial_end_hm(cfg: dict | None) -> tuple[int, int]:
    """Final dual-window cut = N minutes before last research (C3)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    # Explicit override still works for ops.
    if cfg.get("ai_duel_trial_end_time"):
        return _parse_hhmm(str(cfg.get("ai_duel_trial_end_time")), (14, 20))
    ch, cm = chance3_hm(cfg)
    total = ch * 60 + cm - close_before_research_min(cfg)
    if total < 0:
        total = 0
    return divmod(total, 60)


def window_cuts(cfg: dict | None) -> list[tuple[str, tuple[int, int], bool]]:
    """Competition close times: (cut_key, (h,m), is_final_dual).

    For research [08:30, 11:30, 14:30] and lead 10m:
      • 11:20 — end window after 08:30 suggestion (mid dual)
      • 14:20 — end window after 11:30 suggestion (final dual → pick C3 winner)
    """
    times = research_times_hm(cfg)
    lead = close_before_research_min(cfg)
    if len(times) < 2:
        th, tm = trial_end_hm(cfg)
        return [(f"{th:02d}:{tm:02d}", (th, tm), True)]
    cuts: list[tuple[str, tuple[int, int], bool]] = []
    for i in range(1, len(times)):
        hh, mm = times[i]
        total = hh * 60 + mm - lead
        if total < 0:
            total = 0
        ch, cm = divmod(int(total), 60)
        key = f"{ch:02d}:{cm:02d}"
        is_final = i == len(times) - 1
        cuts.append((key, (ch, cm), is_final))
    return cuts


def past_trial_end(cfg: dict | None, now: float | None = None) -> bool:
    """True once the final dual window cut time has been reached."""
    h, m = _et_hm(now)
    th, tm = trial_end_hm(cfg)
    return (h, m) >= (th, tm)


def past_chance3_start(cfg: dict | None, now: float | None = None) -> bool:
    h, m = _et_hm(now)
    th, tm = chance3_hm(cfg)
    return (h, m) >= (th, tm)


def pending_window_cut(
    cfg: dict | None,
    now: float | None = None,
) -> tuple[str, tuple[int, int], bool] | None:
    """Next unscored window cut that is at/after its clock time, else None.

    After the regular session (16:05 ET) we still allow catch-up so a late
    restart can close the day once; callers process one cut per tick.
    """
    t0 = float(now if now is not None else time.time())
    state = load_state(t0)
    if str(state.get("phase") or "trial") != "trial":
        return None
    scored = {str(x) for x in (state.get("windows_scored") or [])}
    h, m = _et_hm(t0)
    now_mins = h * 60 + m
    for key, (ch, cm), is_final in window_cuts(cfg):
        if key in scored:
            continue
        if now_mins >= ch * 60 + cm:
            return key, (ch, cm), is_final
    return None


def _ranked_rows(rows: list[dict]) -> list[dict]:
    """Rows sorted by score desc (stable within ties by symbol)."""
    scored: list[tuple[float, str, dict]] = []
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
        scored.append((sc_f, sym, r))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [r for _, _, r in scored]


def _top_row(rows: list[dict]) -> dict | None:
    ranked = _ranked_rows(rows)
    return ranked[0] if ranked else None


def _symbols_claimed_by_others(champs: dict, src: str) -> set[str]:
    """Symbols already registered as champions by a different source."""
    out: set[str] = set()
    for other, rec in (champs or {}).items():
        if _norm_source(other) == src:
            continue
        if not isinstance(rec, dict):
            continue
        sym = str(rec.get("symbol") or "").upper().strip()
        if sym:
            out.add(sym)
    return out


def _upsert_watch_champion(
    *,
    sym: str,
    src: str,
    rec: dict[str, Any],
    top: dict,
    day: str,
    now: float,
) -> None:
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
        "agreement": bool(top.get("agreement")) if top else bool(prev.get("agreement")),
        "score": rec.get("score"),
        "reason": rec.get("reason") or prev.get("reason") or "",
        "source": src,
        "duel": True,
        "duel_source": src,
        "duel_chance": rec.get("chance"),
        "duel_day": day,
        "source_mark": rec.get("source_mark") or ("A" if src == SOURCE_A else "X"),
        "structure": prev.get("structure"),
        "structure_ts": prev.get("structure_ts", 0.0),
        "last_poll_ts": prev.get("last_poll_ts", 0.0),
        "last_ask": prev.get("last_ask"),
        "updated_ts": now,
    }
    ew.save_watch(st)


def reseed_champions_to_watch(*, now: float | None = None) -> int:
    """Re-apply today's duel champions onto the watch file (after SOD wipe)."""
    t0 = float(now if now is not None else time.time())
    state = load_state(t0)
    n = 0
    for src, rec in (state.get("champions") or {}).items():
        if not isinstance(rec, dict):
            continue
        src_n = _norm_source(src)
        sym = str(rec.get("symbol") or "").upper().strip()
        if not src_n or not sym:
            continue
        if str(rec.get("status") or "") in ("closed",):
            continue
        try:
            _upsert_watch_champion(
                sym=sym,
                src=src_n,
                rec=rec,
                top={"agreement": rec.get("agreement"), "reason": rec.get("reason")},
                day=str(state.get("day") or ""),
                now=t0,
            )
            n += 1
        except Exception:
            pass
    return n


def register_champion_from_rows(
    rows: list[dict],
    *,
    source: str,
    cfg: dict | None,
    now: float | None = None,
    chance: int | None = None,
) -> dict[str, Any] | None:
    """Take top-scoring free row for *source* and register as duel champion.

    Will not claim a symbol already taken by the other model (A/X must differ).
    """
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
        # Trial: keep open/submitted legs; allow a NEW champion after a window
        # force-flat (status closed) so the next research→research leg can run.
        prev = (state.get("champions") or {}).get(src) or {}
        if prev.get("status") in ("open", "submitted"):
            return None
        ch = int(chance or max(1, len(state.get("windows_scored") or []) + 1))

    champs = dict(state.get("champions") or {})
    taken = _symbols_claimed_by_others(champs, src)
    top = None
    sym = ""
    for cand in _ranked_rows(list(rows or [])):
        s = str(cand.get("symbol") or "").upper().strip()
        if not s or s in taken:
            continue
        top = cand
        sym = s
        break
    if not top or not sym:
        try:
            import ai_positions as cp
            cp.log_event(
                "duel_champion_skip",
                source=src,
                reason="no_free_symbol",
                taken=sorted(taken),
            )
        except Exception:
            pass
        print(
            f"[ai] duel champion skip src={src} reason=no_free_symbol "
            f"taken={sorted(taken)}",
            flush=True,
        )
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
        "filled": False,
        "registered_ts": t0,
    }
    # Drop prior watch row if this source is switching champions.
    prev_rec = champs.get(src) if isinstance(champs.get(src), dict) else {}
    prev_sym = str(prev_rec.get("symbol") or "").upper().strip()
    champs[src] = rec
    state["champions"] = champs
    if ch == 3:
        state["phase"] = "chance3"
    save_state(state)

    try:
        if prev_sym and prev_sym != sym:
            import ai_entry_watch as ew
            st = ew.load_watch()
            old = st.get(prev_sym) if isinstance(st, dict) else None
            if isinstance(old, dict) and (
                old.get("duel_source") == src or old.get("source") == src
            ):
                # Only drop if still a pure duel watch (not submitted).
                if str(old.get("status") or "") in ("watching", "armed", ""):
                    st.pop(prev_sym, None)
                    ew.save_watch(st)
        _upsert_watch_champion(
            sym=sym,
            src=src,
            rec=rec,
            top=top,
            day=str(state.get("day") or ""),
            now=t0,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[ai] duel watch upsert failed {sym}/{src}: {e}", flush=True)

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
    print(
        f"[ai] duel champion registered {rec['source_mark']} {sym} "
        f"chance={ch} phase={state.get('phase')} path={DUEL_STATE_PATH}",
        flush=True,
    )
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
        # Also match by symbol if source tag missing on the position.
        for k, v in list(champs.items()):
            if isinstance(v, dict) and str(v.get("symbol") or "").upper() == sym:
                src = _norm_source(k) or src
                rec = v
                break
    if not isinstance(rec, dict):
        return
    if str(rec.get("symbol") or "").upper() != sym:
        return
    rec = dict(rec)
    rec["status"] = "open"
    rec["filled"] = True
    if entry_price is not None:
        rec["entry_price"] = float(entry_price)
    if stop_price is not None:
        rec["stop_price"] = float(stop_price)
    rec["opened_ts"] = t0
    champs[src] = rec
    state["champions"] = champs
    save_state(state)


def note_close(
    symbol: str,
    *,
    exit_price: float | None,
    entry_price: float | None = None,
    stop_price: float | None = None,
    source: str | None = None,
    realized_pl: float | None = None,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Freeze realized R when a duel leg closes early (stop/target/time/thesis).

    Idempotent: does not overwrite a closed record that already has realized_r.
    """
    t0 = float(now if now is not None else time.time())
    state = load_state(t0)
    sym = str(symbol or "").upper().strip()
    if not sym:
        return None
    champs = dict(state.get("champions") or {})
    src = _norm_source(source)
    rec = None
    if src and isinstance(champs.get(src), dict):
        rec = champs.get(src)
        if str(rec.get("symbol") or "").upper() != sym:
            rec = None
    if rec is None:
        for k, v in champs.items():
            if isinstance(v, dict) and str(v.get("symbol") or "").upper() == sym:
                src = _norm_source(k)
                rec = v
                break
    if not isinstance(rec, dict) or not src:
        return None
    if rec.get("status") == "closed" and rec.get("realized_r") is not None:
        return rec

    entry = entry_price if entry_price is not None else rec.get("entry_price")
    stop = stop_price if stop_price is not None else rec.get("stop_price")
    exit_px = exit_price if exit_price is not None else rec.get("exit_price")
    r = _compute_r(entry, stop, exit_px)
    if r is None:
        r = 0.0
    rec = dict(rec)
    rec.update({
        "status": "closed",
        "filled": bool(rec.get("filled") or entry is not None),
        "entry_price": entry,
        "stop_price": stop,
        "exit_price": exit_px,
        "realized_r": r,
        "realized_pl": realized_pl,
        "closed_ts": t0,
        "close_ok": True,
    })
    champs[src] = rec
    state["champions"] = champs
    save_state(state)
    try:
        import ai_positions as cp
        cp.log_event(
            "duel_leg_closed",
            symbol=sym,
            source=src,
            realized_r=r,
            exit_price=exit_px,
        )
    except Exception:
        pass
    return rec


def research_allowed_for_source(
    cfg: dict | None,
    source: str | None,
    *,
    now: float | None = None,
) -> bool:
    """Whether this model may run a scheduled research CLI call.

    Both run during the dual trial. After trial score, only the winner may
    run the chance-3 slot (loser saves the third research spend).
    """
    if not duel_enabled(cfg):
        return True
    t0 = float(now if now is not None else time.time())
    # Before chance-3 window: both always research (08:30 / 11:30).
    if not past_chance3_start(cfg, t0):
        return True
    state = load_state(t0)
    phase = str(state.get("phase") or "trial")
    # Trial somehow past C3 clock without score yet — still allow both.
    if phase == "trial":
        return True
    if phase == "done":
        return False
    winner = _norm_source(state.get("winner"))
    if not winner:
        return False
    src = _norm_source(source)
    return bool(src and src == winner)


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
        if past_trial_end(cfg, t0) or pending_window_cut(cfg, t0):
            return False  # in cut window / awaiting liquidate
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


def _score_current_champions(
    state: dict[str, Any],
    *,
    now: float,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, float], dict[str, bool]]:
    """Liquidate open duel legs and return (champs, score, r_by_src, elig_by_src)."""
    import alpaca_trader

    detail = {}
    try:
        detail = alpaca_trader.get_positions_detail() or {}
    except Exception:
        detail = {}
    managed = {}
    try:
        import ai_positions as cp
        managed = cp._load_state() if hasattr(cp, "_load_state") else {}
    except Exception:
        managed = {}

    champs = dict(state.get("champions") or {})
    score: dict[str, Any] = {}
    r_by: dict[str, float] = {SOURCE_A: 0.0, SOURCE_X: 0.0}
    elig_by: dict[str, bool] = {SOURCE_A: False, SOURCE_X: False}

    for src in (SOURCE_A, SOURCE_X):
        rec = dict(champs.get(src) or {})
        sym = str(rec.get("symbol") or "").upper().strip()
        if not sym:
            score[src] = {
                "realized_r": 0.0, "symbol": None, "note": "no_champion",
                "eligible": False,
            }
            continue

        if (
            rec.get("status") == "closed"
            and rec.get("realized_r") is not None
            and not (detail.get(sym) if isinstance(detail, dict) else None)
        ):
            r = float(rec.get("realized_r") or 0.0)
            filled = bool(rec.get("filled"))
            score[src] = {
                "symbol": sym,
                "realized_r": r,
                "realized_pl": rec.get("realized_pl"),
                "entry_price": rec.get("entry_price"),
                "stop_price": rec.get("stop_price"),
                "exit_price": rec.get("exit_price"),
                "eligible": filled,
                "note": "early_close",
            }
            champs[src] = rec
            r_by[src] = r if filled else 0.0
            elig_by[src] = filled
            continue

        live = detail.get(sym) if isinstance(detail, dict) else None
        mpos = managed.get(sym) if isinstance(managed, dict) else None
        entry = rec.get("entry_price")
        stop = rec.get("stop_price")
        if mpos and isinstance(mpos, dict):
            entry = entry if entry is not None else mpos.get("entry_price")
            stop = stop if stop is not None else mpos.get("stop_price")

        exit_px = None
        if live and isinstance(live, dict):
            exit_px = live.get("current") or live.get("current_price")
            if entry is None:
                entry = live.get("avg_entry")
        elif mpos and isinstance(mpos, dict):
            exit_px = mpos.get("last_seen_price") or mpos.get("entry_price")

        closed_ok = True
        if live:
            try:
                out = alpaca_trader.close_out(sym)
                closed_ok = bool(out.get("ok"))
                if exit_px is None:
                    exit_px = live.get("current")
            except Exception as e:  # noqa: BLE001
                closed_ok = False
                print(f"[ai] duel close {sym} failed: {e}", flush=True)

        r = _compute_r(entry, stop, exit_px)
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

        filled = bool(rec.get("filled") or live or entry is not None)
        rec.update({
            "status": "closed",
            "filled": filled,
            "entry_price": entry,
            "stop_price": stop,
            "exit_price": exit_px,
            "realized_r": r,
            "realized_pl": pl,
            "closed_ts": now,
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
            "eligible": filled,
        }
        r_by[src] = r if filled else 0.0
        elig_by[src] = filled

    return champs, score, r_by, elig_by


def run_window_liquidate_and_score(
    cfg: dict | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Force-flat duel legs at a research-boundary cut; accumulate R; maybe pick C3 winner.

    Window rule: trade after suggestion, close ~N minutes before the *next*
    research slot (default 10). Mid cuts keep dual trial open; final cut
    (before last research) selects the chance-3 winner by **sum of window R**.
    """
    if not duel_enabled(cfg):
        return {"ok": False, "reason": "disabled"}
    t0 = float(now if now is not None else time.time())
    state = load_state(t0)
    if str(state.get("phase") or "trial") != "trial":
        if state.get("trial_liquidated"):
            return {"ok": True, "reason": "already_done", "winner": state.get("winner")}
        return {"ok": False, "reason": "bad_phase", "phase": state.get("phase")}

    pending = pending_window_cut(cfg, t0)
    if not pending:
        return {"ok": False, "reason": "no_pending_cut"}
    cut_key, (ch, cm), is_final = pending

    print(
        f"[ai] DUEL window cut {cut_key} — liquidate A/X "
        f"({'FINAL → pick C3 winner' if is_final else 'mid'})",
        flush=True,
    )
    champs, score, r_by, elig_by = _score_current_champions(state, now=t0)

    totals = dict(state.get("totals") or {})
    for src in (SOURCE_A, SOURCE_X):
        try:
            totals[src] = float(totals.get(src) or 0.0) + float(r_by.get(src) or 0.0)
        except (TypeError, ValueError):
            totals[src] = float(r_by.get(src) or 0.0)

    history = list(state.get("window_history") or [])
    history.append({
        "cut": cut_key,
        "ts": t0,
        "score": score,
        "final": is_final,
    })
    scored_keys = list(state.get("windows_scored") or [])
    if cut_key not in scored_keys:
        scored_keys.append(cut_key)

    state["champions"] = champs
    state["score"] = score
    state["totals"] = totals
    state["window_history"] = history
    state["windows_scored"] = scored_keys
    state["last_cut"] = cut_key

    winner = None
    if is_final:
        # Overall winner = higher cumulative R among sides that filled at least once.
        filled_any = {
            SOURCE_A: any(
                bool((w.get("score") or {}).get(SOURCE_A, {}).get("eligible"))
                for w in history if isinstance(w, dict)
            ),
            SOURCE_X: any(
                bool((w.get("score") or {}).get(SOURCE_X, {}).get("eligible"))
                for w in history if isinstance(w, dict)
            ),
        }
        # Prefer explicit elig this window OR any prior
        elig_a = bool(elig_by.get(SOURCE_A) or filled_any[SOURCE_A])
        elig_x = bool(elig_by.get(SOURCE_X) or filled_any[SOURCE_X])
        # Recompute filled_any more carefully from history
        elig_a = False
        elig_x = False
        for w in history:
            sc = (w or {}).get("score") or {}
            if (sc.get(SOURCE_A) or {}).get("eligible"):
                elig_a = True
            if (sc.get(SOURCE_X) or {}).get("eligible"):
                elig_x = True
        ta = float(totals.get(SOURCE_A) or 0.0)
        tx = float(totals.get(SOURCE_X) or 0.0)
        if elig_a and elig_x:
            if ta > tx:
                winner = SOURCE_A
            elif tx > ta:
                winner = SOURCE_X
        elif elig_a and not elig_x:
            winner = SOURCE_A
        elif elig_x and not elig_a:
            winner = SOURCE_X
        state["winner"] = winner
        state["trial_liquidated"] = True
        state["phase"] = "scored" if winner else "done"
        state["scored_ts"] = t0
    else:
        # Mid window: keep dual trial; champions closed so next research can re-seed.
        state["phase"] = "trial"
        state["trial_liquidated"] = False

    save_state(state)

    r_a = float(r_by.get(SOURCE_A) or 0.0)
    r_x = float(r_by.get(SOURCE_X) or 0.0)
    try:
        import ai_positions as cp
        cp.log_event(
            "duel_window_scored",
            cut=cut_key,
            final=is_final,
            winner=winner,
            r_anthropic=r_a,
            r_xai=r_x,
            totals=totals,
            score=score,
        )
    except Exception:
        pass

    print(
        f"[ai] DUEL window {cut_key} A_R={r_a:+.3f} X_R={r_x:+.3f} "
        f"totals A={float(totals.get(SOURCE_A) or 0):+.3f} "
        f"X={float(totals.get(SOURCE_X) or 0):+.3f} "
        f"winner={winner or ('(mid)' if not is_final else 'tie/none')}",
        flush=True,
    )
    return {
        "ok": True,
        "cut": cut_key,
        "final": is_final,
        "winner": winner,
        "score": score,
        "totals": totals,
        "r_anthropic": r_a,
        "r_xai": r_x,
    }


def run_trial_liquidate_and_score(
    cfg: dict | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Back-compat alias for the research-boundary window cut."""
    return run_window_liquidate_and_score(cfg, now=now)


def trial_liquidate_due(cfg: dict | None, now: float | None = None) -> bool:
    """True when a research-boundary competition cut is pending."""
    if not duel_enabled(cfg):
        return False
    return pending_window_cut(cfg, now) is not None


def public_snapshot(cfg: dict | None = None, now: float | None = None) -> dict[str, Any]:
    """Small dict for dashboard wire / debugging."""
    t0 = float(now if now is not None else time.time())
    st = load_state(t0)
    cuts = [k for k, _, _ in window_cuts(cfg)]
    return {
        "enabled": duel_enabled(cfg),
        "day": st.get("day"),
        "phase": st.get("phase"),
        "winner": st.get("winner"),
        "trial_liquidated": st.get("trial_liquidated"),
        "score": st.get("score") or {},
        "totals": st.get("totals") or {},
        "windows_scored": st.get("windows_scored") or [],
        "window_cuts": cuts,
        "close_before_research_min": close_before_research_min(cfg),
        "champions": st.get("champions") or {},
        "trial_end": "%02d:%02d" % trial_end_hm(cfg),
        "chance3_start": "%02d:%02d" % chance3_hm(cfg),
        "horizon": (
            f"trade after suggestion; force-flat "
            f"{close_before_research_min(cfg)}m before next research"
        ),
    }
