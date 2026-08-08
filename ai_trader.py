#!/usr/bin/env python3
"""Server-side AI research desk (Anthropic + Grok).

Runs scheduled research prompts for one or both sources, turns qualifying
ideas from the *single trading owner* into Alpaca paper brackets, and
enforces stop / scale-out / trailing / time-stop rules mechanically.
Publishes JSON that dashboard.py merges into /api/state:

    claude_suggestions.json   Anthropic ranked ideas (source A)
    grok_suggestions.json     xAI / Grok ranked ideas (source X)
    ai_positions_state.json   open positions, orders, performance
                              (also writes legacy claude_positions_state.json)

The dashboard merges both into ai_suggestions (A / X / AX). Exactly one
source may place orders (``ai_trading_enabled`` / ``claude_trading_enabled``
vs ``grok_trading_enabled``). Prefer Grok as the trading CLI; Anthropic as
research-only. If both flags are true, Grok wins and Claude trading is
forced off for that process.

The momentum monitor is a renderer only — it reads /api/state and originates
nothing. Only entry evaluation and thesis-break review call a model; open
positions are managed in ai_positions.manage_open_positions().

    python3 ai_trader.py
"""
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import desk_core  # noqa: E402

_ENV_FILE_KEYS: list = []

# Alpaca credentials live only in signal_engine.env; config.load_config() never
# sees them. Must run before anything imports a broker client.
_loaded_env_keys = desk_core.load_env_file(ROOT / "signal_engine.env")
if _loaded_env_keys:
    _ENV_FILE_KEYS.extend(_loaded_env_keys)
    print(f"[ENV] Loaded {len(_loaded_env_keys)} setting(s) from signal_engine.env",
          flush=True)

import ai_positions  # noqa: E402
from ai_suggest import (  # noqa: E402
    SOURCE_ANTHROPIC,
    SOURCE_MARK,
    SOURCE_XAI,
    AiSuggestions,
    source_from_backend,
)
from config import load_config  # noqa: E402

# Per-source idea files. Dashboard merges both into ai_suggestions.
CLAUDE_SUGGESTIONS_FILE = ROOT / "claude_suggestions.json"
GROK_SUGGESTIONS_FILE = ROOT / "grok_suggestions.json"
# Back-compat alias used by _manual_research_run.py
SUGGESTIONS_FILE = CLAUDE_SUGGESTIONS_FILE
# Shared trading book — prefer new name; keep legacy path in sync one release.
POSITIONS_FILE = ROOT / "ai_positions_state.json"
POSITIONS_FILE_LEGACY = ROOT / "claude_positions_state.json"
# Sticky open positions across a failed Alpaca poll so the dashboard book
# does not flash empty when get_positions_detail() returns None once.
_last_good_positions: dict = {}
# Sticky account day P&L snapshot (equity / day_pl) for the same reason.
_last_good_account: dict = {}

LOOP_SLEEP = 5.0


_write_json = desk_core.write_json_atomic


def _read_json(path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _hydrate_suggestions(gs: AiSuggestions, path: Path) -> int:
    """Load last published ideas so a restart / off-slot loop does not blank the UI."""
    data = _read_json(path)
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    if not rows:
        return 0
    gs.rows = list(rows)
    gs.by_symbol = {
        str(r.get("symbol") or "").upper(): r
        for r in gs.rows
        if r.get("symbol")
    }
    try:
        gs.last_ok = float(data.get("last_ok") or 0) or 0.0
    except (TypeError, ValueError):
        gs.last_ok = 0.0
    report = data.get("last_report_path") or ""
    if report:
        gs.last_report_path = str(report)
    # Schedule-only notices are not failures once we have displayable rows.
    err = str(gs.error or data.get("error") or "")
    if err.lower().startswith("next research") or err == "no research times configured":
        gs.error = ""
    elif not gs.error and data.get("error"):
        soft = str(data.get("error") or "")
        if not soft.lower().startswith("next research"):
            gs.error = soft
    return len(gs.rows)


def _seed_duel_from_book(
    gs: AiSuggestions | None,
    source: str,
    cfg: dict,
    *,
    now: float | None = None,
) -> None:
    """Register duel champion from hydrated/published rows if missing.

    Research-only sources used to skip the trade hook entirely, so A never
    latched a champion. On restart we re-seed from the last idea JSON so the
    dual trial can resume without waiting for the next research slot.
    """
    if gs is None or not getattr(gs, "rows", None):
        return
    try:
        import ai_duel as duel
        if not duel.duel_enabled(cfg):
            return
        t0 = float(now if now is not None else time.time())
        st = duel.load_state(t0)
        # Do not invent late champions after the dual window is over when the
        # day never latched a state file (stale books after the close).
        if duel.past_trial_end(cfg, t0) and not (st.get("champions") or {}):
            return
        src = duel._norm_source(source)  # noqa: SLF001
        if not src:
            return
        prev = (st.get("champions") or {}).get(src) or {}
        # Keep an open/submitted/watching leg; only fill a missing or closed slot.
        if isinstance(prev, dict) and prev.get("status") in (
            "open", "submitted", "watching",
        ):
            if prev.get("symbol"):
                return
        rows = list(gs.rows or [])
        for r in rows:
            if isinstance(r, dict) and not r.get("source"):
                r["source"] = src
        rec = duel.register_champion_from_rows(
            rows, source=src, cfg=cfg, now=t0)
        if rec:
            print(
                f"[ai] duel seed from book {rec.get('source_mark')} "
                f"{rec.get('symbol')} src={src}",
                flush=True,
            )
    except Exception as e:  # noqa: BLE001
        print(f"[ai] duel seed from book failed ({source}): {e}", flush=True)


def _suggestions_payload(
    gs: AiSuggestions,
    now: float,
    *,
    path: Path,
    source: str,
) -> dict:
    """Keys the desk AI panel / merge path consume.

    While waiting for the next research slot the in-memory list is often empty
    after a process restart. Prefer last published rows so dashboard/monitor
    keep showing the prior research until a new run replaces them.
    """
    rows = list(gs.rows)
    last_ok = gs.last_ok
    report = gs.last_report_path
    error = gs.error or ""
    src = normalize_source(source, gs.backend)
    mark = SOURCE_MARK.get(src, "?")

    if not rows and not last_ok:
        prev = _read_json(path)
        prev_rows = prev.get("rows") if isinstance(prev.get("rows"), list) else []
        try:
            prev_ok = float(prev.get("last_ok") or 0) or 0.0
        except (TypeError, ValueError):
            prev_ok = 0.0
        if prev_rows and prev_ok:
            rows = list(prev_rows)
            last_ok = prev_ok
            report = prev.get("last_report_path") or report

    # Tag provenance for the merge path (A / X / AX).
    tagged = []
    for r in rows:
        row = dict(r)
        row["source"] = src
        row["source_mark"] = mark
        tagged.append(row)
    rows = tagged

    if rows and (
        error.lower().startswith("next research")
        or error == "no research times configured"
    ):
        error = ""

    # Prefer in-memory last_usage (just finished a call); else latest metrics row
    # so a restart still surfaces cost on the desk without re-running research.
    usage = dict(getattr(gs, "last_usage", None) or {})
    if not usage:
        try:
            from ai_suggest import latest_token_usage
            usage = latest_token_usage()
            # Only attach if it matches this source's backend family.
            backend = str(usage.get("backend") or "")
            if src == SOURCE_ANTHROPIC and "grok" in backend:
                usage = {}
            elif src == SOURCE_XAI and "claude" in backend:
                usage = {}
        except Exception:
            usage = {}
    last_usage_out = None
    if usage:
        last_usage_out = {
            k: usage[k]
            for k in (
                "ts", "backend", "phase", "model", "effort", "num_turns",
                "total_cost_usd", "input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens",
                "reasoning_tokens", "total_tokens", "duration_ms",
            )
            if k in usage and usage[k] is not None
        }

    return {
        "updated": now,
        "last_ok": last_ok,
        "error": error,
        "quotes_error": gs.quotes_error,
        "last_quote_ok": gs.last_quote_ok,
        "model": gs.model,
        "backend": gs.backend,
        "source": src,
        "trading": gs.trading,
        "trading_mode": gs.trading_mode,
        "max_price": gs.max_price,
        "next_run_label": gs.next_run_label(now),
        "last_report_path": report,
        # Surface paper fills for whichever source owns trading.
        "last_trades": gs.last_trades if gs.trading else [],
        "last_usage": last_usage_out or {},
        "rows": rows,
    }


def normalize_source(source: str | None, backend: str | None) -> str:
    src = (source or "").strip().lower()
    if src in (SOURCE_ANTHROPIC, SOURCE_XAI):
        return src
    from_backend = source_from_backend(backend)
    if from_backend in (SOURCE_ANTHROPIC, SOURCE_XAI):
        return from_backend
    return SOURCE_ANTHROPIC


def _watch_source_error() -> str:
    """Last candidate-feed fetch error ('' when healthy)."""
    try:
        import ai_entry_watch as ew
        return ew.dashboard_error()
    except Exception:
        return ""


def _watch_rejected(limit: int = 12) -> list[dict]:
    """Names the inclusion gate turned away this sync, with the reason."""
    try:
        import ai_entry_watch as ew
        return ew.last_rejected()[:limit]
    except Exception:
        return []


def _positions_payload(
    mode: str,
    now: float,
    *,
    book_owner: str = "",
    watch_poll_sec: float | None = None,
) -> dict:
    import alpaca_trader

    error = ""
    positions: dict = {}
    open_orders: list = []
    account: dict = {}
    if alpaca_trader.is_active():
        detail = alpaca_trader.get_positions_detail()
        if detail is None:
            # Keep last good book so the AI Watch OPEN row does not blink out
            # on a single failed broker poll (dashboard still shows error).
            error = "Alpaca position query failed"
            positions = dict(_last_good_positions)
        else:
            positions = detail if isinstance(detail, dict) else {}
            _last_good_positions.clear()
            _last_good_positions.update(positions)
        open_orders = alpaca_trader.get_open_orders()
        try:
            day_snap = alpaca_trader.get_account_day_pl()
            if isinstance(day_snap, dict):
                account = day_snap
            elif _last_good_account:
                account = dict(_last_good_account)
        except Exception:
            account = dict(_last_good_account) if _last_good_account else {}
        if account:
            _last_good_account.clear()
            _last_good_account.update(account)
    else:
        error = "trader inactive — no Alpaca session"
        # Trader off is authoritative flat — do not sticky phantom positions.
        _last_good_positions.clear()
        _last_good_account.clear()

    try:
        performance = ai_positions.performance_summary()
    except Exception as e:  # noqa: BLE001
        performance = {"count": 0}
        error = error or f"performance_summary failed: {e}"

    reconcile = {}
    try:
        reconcile = ai_positions.last_reconcile() or {}
    except Exception:
        reconcile = {}
    recent = []
    try:
        recent = ai_positions.recent_events(40)
    except Exception:
        recent = []

    day_r = 0.0
    open_r = 0.0
    try:
        day_r = ai_positions.realized_r_today(now)
        # equity from first position detail if available — else 0
        import ai_trading as gt
        if gt.is_ready():
            acct = gt.get_account()
            eq = float(acct.get("equity") or 0) if acct.get("ok") else 0.0
            if eq > 0:
                open_r = ai_positions.open_risk_pct(eq)
    except Exception:
        pass

    warnings: list[str] = []
    if reconcile.get("unmanaged"):
        warnings.append(
            "unmanaged_positions:" + ",".join(reconcile["unmanaged"][:8])
        )
    if reconcile.get("unconfirmed"):
        warnings.append(
            "unconfirmed_entries:" + ",".join(reconcile["unconfirmed"][:8])
        )
    # Contradictory settings surface here rather than silently misbehaving —
    # e.g. ai_min_reward_risk above ai_watch_synth_rr makes every synthetic
    # zone self-block, which looks identical to "nothing is ready".
    try:
        from config import load_config, validate_ai_config
        for problem in validate_ai_config(load_config()):
            warnings.append("config:" + problem)
    except Exception:
        pass

    entry_watch: list = []
    entry_book: list = []
    try:
        import ai_entry_watch as ew

        entry_watch = ew.public_snapshot()
        entry_book = ew.book_table_rows(
            positions=positions if isinstance(positions, dict) else {},
            watch_rows=entry_watch,
        )
    except Exception:
        entry_watch = []
        entry_book = []

    n_mom = sum(
        1 for r in entry_book
        if str(r.get("source") or "").lower() in ("momentum", "mom")
    )
    n_st = sum(
        1 for r in entry_book
        if str(r.get("source") or "").lower() in ("trending", "st", "stocktwits")
    )
    n_res = sum(
        1 for r in entry_book
        if str(r.get("source") or "").lower() not in (
            "momentum", "mom", "trending", "st", "stocktwits", "position",
        )
    )
    n_open = sum(1 for r in entry_book if r.get("phase") == "open" or r.get("is_position"))
    n_ready = sum(1 for r in entry_book if r.get("phase") == "ready" or r.get("ready"))

    poll_sec = watch_poll_sec
    if poll_sec is None:
        try:
            poll_sec = float(load_config().get("ai_watch_poll_sec", 20.0) or 20.0)
        except Exception:
            poll_sec = 20.0

    # Prefer broker day P&L; fall back to open unrealized + AI realized when
    # account snapshot is missing so the header still has a number.
    day_pl = account.get("day_pl") if isinstance(account, dict) else None
    day_pl_pct = account.get("day_pl_pct") if isinstance(account, dict) else None
    if day_pl is None and isinstance(positions, dict) and positions:
        try:
            day_pl = sum(
                float(p.get("pl") or 0)
                for p in positions.values()
                if isinstance(p, dict)
            )
        except (TypeError, ValueError):
            day_pl = None

    return {
        "updated": now,
        "mode": mode,
        "book_owner": book_owner,
        "error": error,
        "warnings": warnings,
        "positions": positions,
        "open_orders": open_orders,
        # Full Alpaca account day P&L (equity − last_equity) for AI Watch header.
        "account": account if isinstance(account, dict) else {},
        "day_pl": day_pl,
        "day_pl_pct": day_pl_pct,
        "performance": performance,
        "reconcile": reconcile,
        "recent_events": recent,
        "realized_r_today": day_r,
        "open_risk_pct": open_r,
        "entry_watch": entry_watch,
        # Unified Watch section: watches + open positions with P&L.
        "entry_book": entry_book,
        # Operator-facing liveness for the AI Watch column stamp.
        "watch_meta": {
            "updated": now,
            "poll_sec": float(poll_sec or 20.0),
            "n_book": len(entry_book),
            "n_momentum": n_mom,
            "n_trending": n_st,
            "n_research": n_res,
            "n_open": n_open,
            "n_ready": n_ready,
            "day_pl": day_pl,
            "day_pl_pct": day_pl_pct,
            "equity": (account or {}).get("equity") if isinstance(account, dict) else None,
            # Why the book looks the way it does. Without these an empty book
            # from a dead dashboard is indistinguishable from "nothing
            # qualified", which is exactly how momentum silently contributed
            # zero candidates for a whole session.
            "source_error": _watch_source_error(),
            "rejected": _watch_rejected(),
        },
        "duel": _duel_public(),
    }


def _duel_public() -> dict:
    try:
        import ai_duel as duel
        from config import load_config
        return duel.public_snapshot(load_config())
    except Exception:
        return {}


def _cfg(cfg: dict, key: str, default=None):
    """Config read where an explicit null counts as unset, not as a value."""
    v = cfg.get(key)
    return default if v is None else v


def resolve_trading_source(cfg: dict) -> str:
    """Single book owner: ``grok`` | ``claude`` | ``off``.

    Prefer explicit ``ai_trading_source``. Legacy boolean flags still work:
    if both Claude and Grok trading flags are true, Grok wins.
    """
    raw = str(cfg.get("ai_trading_source") or "").strip().lower()
    if raw in ("grok", "claude", "off", "none", "false", "0"):
        if raw in ("none", "false", "0"):
            return "off"
        return raw
    grok_on = bool(cfg.get("grok_trading_enabled", False))
    claude_on = bool(_cfg(cfg, "ai_trading_enabled", False))
    if grok_on and claude_on:
        return "grok"
    if grok_on:
        return "grok"
    if claude_on:
        return "claude"
    return "off"


def apply_trading_source(cfg: dict, source: str) -> dict:
    """Return a config copy with per-source trading flags aligned to ``source``."""
    out = dict(cfg)
    src = (source or "off").strip().lower()
    out["ai_trading_source"] = src if src in ("grok", "claude", "off") else "off"
    if src == "grok":
        out["grok_trading_enabled"] = True
        out["ai_trading_enabled"] = False
        out["claude_trading_enabled"] = False
    elif src == "claude":
        out["grok_trading_enabled"] = False
        out["ai_trading_enabled"] = True
        out["claude_trading_enabled"] = True
    else:
        out["grok_trading_enabled"] = False
        out["ai_trading_enabled"] = False
        out["claude_trading_enabled"] = False
    return out


def _build_suggestions(cfg: dict) -> AiSuggestions:
    """Anthropic research source (optional paper trading)."""
    trading = bool(_cfg(cfg, "ai_trading_enabled", False))
    return AiSuggestions(
        max_price=_cfg(cfg, "ai_max_price"),
        quote_interval=float(_cfg(cfg, "ai_quote_poll", 15.0)),
        volume_interval=float(_cfg(cfg, "ai_volume_poll", 60.0)),
        avg_days=int(_cfg(cfg, "ai_avg_days", 10)),
        rvol_time_adjusted=bool(
            _cfg(cfg, "ai_rvol_time_adjusted", True)),
        model=cfg.get("claude_model", "sonnet"),
        prompt_file=cfg.get("claude_prompt_file")
        or cfg.get("ai_prompt_file")
        or "ai_prompt.txt",
        request_timeout=float(cfg.get("claude_request_timeout", 600.0)),
        live_search=bool(cfg.get("claude_live_search", True)),
        save_reports=bool(cfg.get("claude_save_reports", True)),
        trading=trading,
        trade_amount=float(_cfg(cfg, "ai_trade_amount", 1000.0)),
        max_positions=int(_cfg(cfg, "ai_max_positions", 5)),
        max_buys_per_poll=int(
            _cfg(cfg, "ai_max_buys_per_poll", 3)),
        max_sells_per_poll=int(
            _cfg(cfg, "ai_max_sells_per_poll", 5)),
        max_turns=int(cfg.get("claude_max_turns", 8)),
        max_output_tokens=int(cfg.get("claude_max_output_tokens", 10000)),
        search_tools=cfg.get("claude_search_tools", "web_x"),
        use_prior_context=bool(cfg.get("claude_use_prior_context", True)),
        use_desk_snapshot=bool(cfg.get(
            "claude_use_desk_snapshot",
            cfg.get("ai_use_desk_snapshot", True))),
        backend=cfg.get("claude_backend", "claude_cli"),
        cli_bin=cfg.get("claude_cli_bin", "claude"),
        effort=cfg.get("claude_effort", "xhigh"),
        research_times=cfg.get("claude_research_times",
                               ["08:30", "11:30", "14:30"]),
        research_weekdays_only=bool(
            cfg.get("claude_research_weekdays_only", True)),
        research_catchup_min=int(cfg.get("claude_research_catchup_min", 120)),
        risk_pct=float(_cfg(cfg, "ai_risk_pct", 1.0)),
        trade_style=_cfg(cfg, "ai_trade_style", "Moderate position"),
        min_reward_risk=float(
            _cfg(cfg, "ai_min_reward_risk", 3.0)),
    )


def _build_grok(cfg: dict) -> AiSuggestions:
    """xAI / Grok research source (optional paper trading — preferred owner)."""
    trading = bool(cfg.get("grok_trading_enabled", False))
    # Shared quote/volume/risk knobs; fall back to ai_* / claude_* values.
    return AiSuggestions(
        max_price=cfg.get("grok_max_price",
                          _cfg(cfg, "ai_max_price", 100.0)),
        quote_interval=float(_cfg(cfg, "ai_quote_poll", 15.0)),
        volume_interval=float(_cfg(cfg, "ai_volume_poll", 60.0)),
        avg_days=int(_cfg(cfg, "ai_avg_days", 10)),
        rvol_time_adjusted=bool(
            _cfg(cfg, "ai_rvol_time_adjusted", True)),
        model=cfg.get("grok_model", "grok-4.5"),
        prompt_file=cfg.get("grok_prompt_file")
        or cfg.get("claude_prompt_file")
        or cfg.get("ai_prompt_file")
        or "ai_prompt.txt",
        request_timeout=float(cfg.get("grok_request_timeout",
                                      cfg.get("claude_request_timeout", 600.0))),
        live_search=bool(cfg.get("grok_live_search", True)),
        save_reports=bool(cfg.get("grok_save_reports", True)),
        trading=trading,
        trade_amount=float(_cfg(cfg, "ai_trade_amount", 1000.0)),
        max_positions=int(_cfg(cfg, "ai_max_positions", 5)),
        max_buys_per_poll=int(
            _cfg(cfg, "ai_max_buys_per_poll", 3)),
        max_sells_per_poll=int(
            _cfg(cfg, "ai_max_sells_per_poll", 5)),
        max_turns=int(cfg.get("grok_max_turns", 4)),
        search_tools=cfg.get("grok_search_tools",
                             cfg.get("claude_search_tools", "web_x")),
        use_prior_context=bool(cfg.get("grok_use_prior_context", False)),
        use_desk_snapshot=bool(cfg.get(
            "grok_use_desk_snapshot",
            cfg.get("ai_use_desk_snapshot", True))),
        backend=cfg.get("grok_backend", "cli"),
        cli_bin=cfg.get("grok_cli_bin", "grok"),
        research_times=cfg.get("grok_research_times",
                               ["08:30", "11:30", "14:30"]),
        research_weekdays_only=bool(
            cfg.get("grok_research_weekdays_only", True)),
        research_catchup_min=int(cfg.get("grok_research_catchup_min", 120)),
        risk_pct=float(_cfg(cfg, "ai_risk_pct", 1.0)),
        trade_style=_cfg(cfg, "ai_trade_style", "Moderate position"),
        min_reward_risk=float(
            _cfg(cfg, "ai_min_reward_risk", 3.0)),
    )


def _tick_source(
    gs: AiSuggestions | None,
    path: Path,
    source: str,
    now: float,
    label: str,
) -> None:
    if gs is None:
        return
    # After duel score, only the winner spends the chance-3 research slot.
    try:
        import ai_duel as duel
        from config import load_config
        if not duel.research_allowed_for_source(load_config(), source, now=now):
            gs.refresh_quotes(now)
            gs.refresh_volume(now)
            _write_json(path, _suggestions_payload(gs, now, path=path, source=source))
            return
    except Exception:
        pass
    if not gs.refresh(now):
        gs.refresh_quotes(now)
        gs.refresh_volume(now)
    _write_json(path, _suggestions_payload(gs, now, path=path, source=source))


def _open_bell_due(cfg: dict, now: float) -> bool:
    """True once per ET day after configured open-bell time (default 09:35)."""
    if not bool(cfg.get("ai_open_bell_enabled", True)):
        return False
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    dt = datetime.fromtimestamp(now, tz=et)
    if dt.weekday() >= 5:
        return False
    raw = str(cfg.get("ai_open_bell_time") or "09:35").strip()
    try:
        hh, mm = raw.split(":")
        bell_h, bell_m = int(hh), int(mm)
    except Exception:
        bell_h, bell_m = 9, 35
    if (dt.hour, dt.minute) < (bell_h, bell_m):
        return False
    day_key = dt.strftime("%Y-%m-%d")
    try:
        prev = json.loads(ai_positions.OPEN_BELL_STATE_PATH.read_text(
            encoding="utf-8"))
        if str(prev.get("last_day") or "") == day_key:
            return False
    except Exception:
        pass
    return True


def _mark_open_bell_done(now: float) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    day_key = datetime.fromtimestamp(now, tz=et).strftime("%Y-%m-%d")
    try:
        path = ai_positions.OPEN_BELL_STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_day": day_key, "ts": now}),
                        encoding="utf-8")
    except Exception:
        pass


def _parse_hhmm(raw: str, default: tuple[int, int] = (15, 50)) -> tuple[int, int]:
    return desk_core.parse_hhmm(raw, default)


def _eod_liquidate_due(cfg: dict, now: float) -> bool:
    """True once per ET weekday at/after configured EOD liquidate time."""
    if not bool(cfg.get("ai_eod_liquidate_enabled", True)):
        return False
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    dt = datetime.fromtimestamp(now, tz=et)
    if dt.weekday() >= 5:
        return False
    bell_h, bell_m = _parse_hhmm(
        str(cfg.get("ai_eod_liquidate_time") or "15:50"), (15, 50))
    if (dt.hour, dt.minute) < (bell_h, bell_m):
        return False
    day_key = dt.strftime("%Y-%m-%d")
    try:
        prev = json.loads(ai_positions.EOD_LIQUIDATE_STATE_PATH.read_text(
            encoding="utf-8"))
        if str(prev.get("last_day") or "") == day_key:
            return False
    except Exception:
        pass
    return True


def _mark_eod_liquidate_done(now: float, result: dict | None = None) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    day_key = datetime.fromtimestamp(now, tz=et).strftime("%Y-%m-%d")
    try:
        path = ai_positions.EOD_LIQUIDATE_STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_day": day_key,
            "ts": now,
            "result": result if isinstance(result, dict) else {},
        }
        path.write_text(json.dumps(payload, indent=2, default=str),
                        encoding="utf-8")
    except Exception:
        pass


def _run_eod_liquidate(cfg: dict, now: float) -> dict:
    """Cancel all open orders and close all positions (once per day)."""
    import alpaca_trader

    print("[ai] EOD liquidate — cancel open orders + close all positions",
          flush=True)
    try:
        result = alpaca_trader.liquidate_all()
    except Exception as e:  # noqa: BLE001
        result = {
            "ok": False, "canceled": 0, "closed": 0,
            "symbols": [], "errors": [str(e)],
        }
        print(f"[ai] EOD liquidate failed: {e}", flush=True)
    try:
        ai_positions.log_event(
            "eod_liquidate",
            ok=bool(result.get("ok")),
            canceled=result.get("canceled"),
            closed=result.get("closed"),
            symbols=result.get("symbols") or [],
            errors=(result.get("errors") or [])[:8],
        )
    except Exception:
        pass
    # Wipe AI Watch entirely — expire alone leaves names that reseed from Mom/ST.
    if cfg.get("ai_watch_enabled", True):
        try:
            import ai_entry_watch as ew
            ew.clear_watch_book(now=now)
            print("[ai] EOD liquidate — AI Watch cleared", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[ai] EOD clear watch failed: {e}", flush=True)
    # Only stamp the day done when the book is actually flat. The stamp is what
    # makes _eod_liquidate_due return False for the rest of the day, so writing
    # it after a failed flatten retires the one mechanism that would try again.
    # On 2026-08-07 a pending-cancel race left USAR open with its bracket
    # already cancelled, liquidate_all still reported ok, the day was stamped,
    # and the position was heading into the weekend naked with no stop.
    still_open = (result or {}).get("still_open") or []
    if result.get("ok") and not still_open:
        _mark_eod_liquidate_done(now, result)
    else:
        print(f"[ai] ⛔ EOD liquidate did NOT flatten: still_open={still_open} "
              f"— leaving the day unstamped so it retries", flush=True)
        try:
            ai_positions.log_event(
                "eod_liquidate_incomplete",
                still_open=still_open,
                errors=(result.get("errors") or [])[:8],
            )
        except Exception:
            pass
    return result if isinstance(result, dict) else {}


def _sod_liquidate_due(cfg: dict, now: float, *, market_open: bool) -> bool:
    """True once per ET weekday at first RTH open (before any new entries)."""
    if not bool(cfg.get("ai_sod_liquidate_enabled", True)):
        return False
    if not market_open:
        return False
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    dt = datetime.fromtimestamp(now, tz=et)
    if dt.weekday() >= 5:
        return False
    # Don't run SOD flatten in the EOD window.
    if _eod_liquidate_due(cfg, now) or (
        bool(cfg.get("ai_eod_liquidate_enabled", True))
        and (dt.hour, dt.minute) >= _parse_hhmm(
            str(cfg.get("ai_eod_liquidate_time") or "15:50"), (15, 50))
    ):
        return False
    day_key = dt.strftime("%Y-%m-%d")
    try:
        prev = json.loads(ai_positions.SOD_LIQUIDATE_STATE_PATH.read_text(
            encoding="utf-8"))
        if str(prev.get("last_day") or "") == day_key:
            return False
    except Exception:
        pass
    return True


def _mark_sod_liquidate_done(now: float, result: dict | None = None) -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    day_key = datetime.fromtimestamp(now, tz=et).strftime("%Y-%m-%d")
    try:
        path = ai_positions.SOD_LIQUIDATE_STATE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_day": day_key,
            "ts": now,
            "result": result if isinstance(result, dict) else {},
        }
        path.write_text(json.dumps(payload, indent=2, default=str),
                        encoding="utf-8")
    except Exception:
        pass


def _run_sod_liquidate(cfg: dict, now: float) -> dict:
    """Start-of-day flatten: wipe broker book before any new paper entries."""
    import alpaca_trader

    print("[ai] SOD liquidate — flatten overnight book before new trades",
          flush=True)
    try:
        result = alpaca_trader.liquidate_all()
    except Exception as e:  # noqa: BLE001
        result = {
            "ok": False, "canceled": 0, "closed": 0,
            "symbols": [], "errors": [str(e)],
        }
        print(f"[ai] SOD liquidate failed: {e}", flush=True)
    try:
        ai_positions.log_event(
            "sod_liquidate",
            ok=bool(result.get("ok")),
            canceled=result.get("canceled"),
            closed=result.get("closed"),
            symbols=result.get("symbols") or [],
            errors=(result.get("errors") or [])[:8],
        )
    except Exception:
        pass
    # Fresh session: empty watch queue; reseed duel champions + panels after.
    if cfg.get("ai_watch_enabled", True):
        try:
            import ai_entry_watch as ew
            ew.clear_watch_book(now=now)
            print("[ai] SOD liquidate — AI Watch cleared for reseed", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[ai] SOD clear watch failed: {e}", flush=True)
    # Drop local managed state so we do not inherit overnight book metadata.
    try:
        ai_positions._save_state({})
    except Exception:
        pass
    # Re-apply today's A/X duel champions so SOD wipe does not blank the book
    # until the next research run.
    try:
        import ai_duel as duel
        n = duel.reseed_champions_to_watch(now=now)
        if n:
            print(f"[ai] SOD liquidate — reseeded {n} duel champion(s)",
                  flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[ai] SOD duel reseed failed: {e}", flush=True)
    # Only latch the day when flatten succeeded so a failed call retries
    # and trading stays blocked (trading_hours_active requires SOD done).
    if result.get("ok"):
        _mark_sod_liquidate_done(now, result)
    else:
        print("[ai] SOD liquidate not latched (will retry) — no new entries yet",
              flush=True)
    return result if isinstance(result, dict) else {}


def _run_open_bell_entries(book: AiSuggestions, cfg: dict, now: float) -> None:
    """Act on existing ranked ideas after the open — no full research spend.

    Rebuilds the entry-watch queue from the book (structure + WAIT queue for
    the poller). Still calls ``_place_qualifying_entries`` once for names that
    already qualify as BUY (immediate fast path).
    """
    from ai_suggest import _place_qualifying_entries, tag_agreement_on_rows
    import ai_entry_watch as ew

    # Never open-bell buy before morning flatten has finished for the day.
    if not ew.sod_liquidate_done(cfg, now):
        ai_positions.log_event("open_bell_skip", reason="awaiting_sod_liquidate")
        print("[ai] open_bell deferred — SOD liquidate not done yet", flush=True)
        return

    rows = list(book.rows or [])
    if not rows:
        ai_positions.log_event("open_bell_skip", reason="no_rows")
        return
    ai_positions.log_event(
        "open_bell_start", n_rows=len(rows), backend=book.backend)
    # Structure + queue: watch owns WAIT timing; poller buys later.
    if cfg.get("ai_watch_enabled", True):
        try:
            import ai_entry_watch as ew
            book_rows = tag_agreement_on_rows(rows)
            ew.rebuild_watch_from_book(book_rows, cfg=cfg, now=now)
        except Exception:
            pass
    # Immediate BUY fast path for names that already qualify.
    _place_qualifying_entries(
        rows,
        max_price=book.max_price,
        cli_bin=book.cli_bin,
        timeout=min(180.0, float(book.request_timeout)),
        risk_pct=float(book.risk_pct),
        trade_style=str(book.trade_style),
        min_reward_risk=float(book.min_reward_risk),
        model=book.model,
        backend=book.backend,
        max_open_risk_pct=float(cfg.get("ai_max_open_risk_pct", 5.0)),
        daily_loss_limit_r=float(cfg.get("ai_daily_loss_limit_r", 3.0)),
        require_agreement=bool(cfg.get("ai_require_agreement", False)),
        max_spread_pct=float(cfg.get("ai_max_spread_pct", 1.0)),
        min_dollar_volume=(
            float(cfg["ai_min_dollar_volume"])
            if cfg.get("ai_min_dollar_volume") not in (None, "", 0, 0.0)
            else None
        ),
    )
    _mark_open_bell_done(now)
    ai_positions.log_event("open_bell_done")


def main() -> None:
    cfg = load_config()

    claude_on = bool(cfg.get("claude_research_enabled", False))
    grok_on = bool(cfg.get("grok_research_enabled", False))

    if not claude_on and not grok_on:
        print("[ai] both claude_research_enabled and grok_research_enabled "
              "are false — nothing to run.", flush=True)
        return

    source = resolve_trading_source(cfg)
    cfg = apply_trading_source(cfg, source)
    if source != "off":
        print(f"[ai] trading_source={source} "
              f"(legacy dual flags coerced to single owner)", flush=True)
    else:
        print("[ai] trading_source=off — research only", flush=True)

    gs_a: AiSuggestions | None = None
    gs_x: AiSuggestions | None = None

    if claude_on:
        gs_a = _build_suggestions(cfg)
        n = _hydrate_suggestions(gs_a, CLAUDE_SUGGESTIONS_FILE)
        print(f"[ai] anthropic backend={gs_a.backend} model={gs_a.model} "
              f"trading={gs_a.trading} mode={gs_a.trading_mode}", flush=True)
        print(f"[ai] anthropic research times "
              f"{gs_a.research_times or '(interval)'} ET — "
              f"next {gs_a.next_run_label() or 'n/a'}", flush=True)
        if n:
            print(f"[ai] hydrated {n} Anthropic idea(s) from "
                  f"{CLAUDE_SUGGESTIONS_FILE.name}", flush=True)
        if gs_a.backend in ("claude_cli", "claude"):
            try:
                from ai_suggest import claude_auth_status
                auth = claude_auth_status(gs_a.cli_bin)
                if auth.get("logged_in"):
                    how = auth.get("auth_method") or "session"
                    email = auth.get("email") or ""
                    extra = f" email={email}" if email else ""
                    print(f"[ai] claude_auth=ok method={how}{extra}",
                          flush=True)
                else:
                    print(
                        "[ai] WARNING: claude_auth=fail — "
                        f"{auth.get('error') or 'not logged in'}. "
                        "Anthropic research will skip until: claude /login "
                        "(on this machine) or ANTHROPIC_API_KEY is set.",
                        flush=True,
                    )
            except Exception as e:  # noqa: BLE001
                print(f"[ai] WARNING: claude_auth probe failed: {e}",
                      flush=True)
        if gs_a.trading and gs_a.trading_mode == "off":
            print("[ai] WARNING: Claude trading requested but no Alpaca "
                  "session — check signal_engine.env", flush=True)
    else:
        print("[ai] Anthropic research off (claude_research_enabled=false)",
              flush=True)

    if grok_on:
        gs_x = _build_grok(cfg)
        n = _hydrate_suggestions(gs_x, GROK_SUGGESTIONS_FILE)
        print(f"[ai] grok backend={gs_x.backend} model={gs_x.model} "
              f"trading={gs_x.trading} mode={gs_x.trading_mode}", flush=True)
        print(f"[ai] grok research times "
              f"{gs_x.research_times or '(interval)'} ET — "
              f"next {gs_x.next_run_label() or 'n/a'}", flush=True)
        if n:
            print(f"[ai] hydrated {n} Grok idea(s) from "
                  f"{GROK_SUGGESTIONS_FILE.name}", flush=True)
        if gs_x.trading and gs_x.trading_mode == "off":
            print("[ai] WARNING: Grok trading requested but no Alpaca "
                  "session — check signal_engine.env", flush=True)
    else:
        print("[ai] Grok research off (grok_research_enabled=false)", flush=True)

    # Recover A/X duel champions from last research books (research-only side
    # never went through the old trading-only register hook).
    try:
        live_cfg = load_config()
        _seed_duel_from_book(gs_a, SOURCE_ANTHROPIC, live_cfg)
        _seed_duel_from_book(gs_x, SOURCE_XAI, live_cfg)
    except Exception as e:  # noqa: BLE001
        print(f"[ai] duel startup seed failed: {e}", flush=True)

    # Single book owner from resolved source.
    book = None
    if source == "grok" and gs_x is not None and gs_x.trading:
        book = gs_x
    elif source == "claude" and gs_a is not None and gs_a.trading:
        book = gs_a
    trading = book is not None
    trading_mode = book.trading_mode if book is not None else "off"
    owner = source if trading else "none"
    print(f"[ai] book_owner={owner} trading={trading} mode={trading_mode}",
          flush=True)

    positions_poll = float(
        _cfg(cfg, "ai_positions_poll_sec", 5.0))
    unconfirmed_ttl = float(cfg.get("ai_entry_unconfirmed_ttl_sec", 900.0))
    watch_poll_sec = float(cfg.get("ai_watch_poll_sec", 20.0) or 20.0)

    # Shared mutables for the book thread (main thread only reads research).
    book_state = {
        "watch_poll_sec": watch_poll_sec,
        "positions_poll": positions_poll,
        "watch_seen_open": False,
        "watch_expired_day": "",
        "last_watch_poll": 0.0,
        "last_positions_tick": 0.0,
    }
    book_lock = threading.Lock()

    def _publish_book(now: float) -> None:
        """Seed desk heat, drop fallen Mom/ST, write dashboard wire."""
        if not trading:
            return
        try:
            live_cfg = load_config()
            if live_cfg.get("ai_watch_enabled", True):
                import ai_entry_watch as ew
                # Watch window: 09:00 ET → EOD liquidate. Outside that range
                # do not reseed (keeps pre-open empty; post-EOD stays cleared).
                if ew.watch_session_active(live_cfg, now):
                    # Full rebuild from live Momentum + Trending only
                    # (no accumulated orphans from prior sessions/research runs).
                    ew.sync_watch_from_source_panels(live_cfg, now=now)
        except Exception as e:  # noqa: BLE001
            print(f"[ai] watch sync failed: {e}", flush=True)
        try:
            with book_lock:
                wps = float(book_state["watch_poll_sec"] or 20.0)
            pos = _positions_payload(
                trading_mode, now, book_owner=owner,
                watch_poll_sec=wps,
            )
            _write_json(POSITIONS_FILE, pos)
            _write_json(POSITIONS_FILE_LEGACY, pos)
        except Exception as e:  # noqa: BLE001
            print(f"[ai] positions publish failed: {e}", flush=True)

    watch_poll_busy = {"on": False}

    def _run_watch_poll(cfg_snap: dict, t0: float) -> None:
        """Structure/arm/buy recheck — may call CLI; must not block publish."""
        try:
            import ai_entry_watch as ew
            ew.poll_once(cfg=cfg_snap, now=t0)
        except Exception as e:  # noqa: BLE001
            print(f"[ai] watch_poll failed: {e}", flush=True)
            try:
                ai_positions.log_event(
                    "watch_poll_error", reason=str(e)[:200])
            except Exception:
                pass
        finally:
            watch_poll_busy["on"] = False

    def _book_maintenance_loop() -> None:
        """Keep AI Watch live even while the main thread is blocked on research.

        Research CLI and structure evaluation can run for minutes. This loop
        always publishes the wire on a short cadence; heavy poll_once work
        runs on a *nested* daemon so publish never freezes.
        """
        if not trading:
            return
        print("[ai] book maintenance thread started "
              f"(publish ~{positions_poll}s, watch poll ~{watch_poll_sec}s)",
              flush=True)
        while True:
            t0 = time.time()
            try:
                live_cfg = load_config()
                wps = float(
                    live_cfg.get("ai_watch_poll_sec", book_state["watch_poll_sec"])
                    or book_state["watch_poll_sec"]
                    or 20.0
                )
                pps = float(
                    live_cfg.get(
                        "ai_positions_poll_sec",
                        book_state["positions_poll"],
                    )
                    or book_state["positions_poll"]
                    or 5.0
                )
                with book_lock:
                    book_state["watch_poll_sec"] = wps
                    book_state["positions_poll"] = pps

                # Start of RTH: liquidate everything once before any new entries.
                try:
                    import ai_trading as gt
                    mkt_open = bool(gt.market_is_open())
                except Exception:
                    mkt_open = False
                try:
                    if _sod_liquidate_due(live_cfg, t0, market_open=mkt_open):
                        _run_sod_liquidate(live_cfg, t0)
                        _publish_book(time.time())
                except Exception as e:  # noqa: BLE001
                    print(f"[ai] sod_liquidate failed: {e}", flush=True)

                # A vs X duel: force-flat both champions, score realized R, pick winner.
                try:
                    import ai_duel as duel
                    if duel.trial_liquidate_due(live_cfg, t0):
                        duel.run_trial_liquidate_and_score(live_cfg, t0)
                        _publish_book(time.time())
                except Exception as e:  # noqa: BLE001
                    print(f"[ai] duel trial liquidate failed: {e}", flush=True)

                # Fast path: seed/prune/publish so UI never goes stale.
                _publish_book(t0)

                if (t0 - book_state["last_positions_tick"]) >= pps:
                    book_state["last_positions_tick"] = t0
                    try:
                        ai_positions.manage_open_positions(
                            t0, unconfirmed_ttl_sec=unconfirmed_ttl)
                    except Exception as e:  # noqa: BLE001
                        print(f"[ai] manage_open_positions failed: {e}",
                              flush=True)

                # 15:50 ET (configurable): cancel open orders + flatten positions.
                try:
                    if _eod_liquidate_due(live_cfg, t0):
                        _run_eod_liquidate(live_cfg, t0)
                        _publish_book(time.time())
                except Exception as e:  # noqa: BLE001
                    print(f"[ai] eod_liquidate failed: {e}", flush=True)

                if live_cfg.get("ai_watch_expire_at_close", True):
                    try:
                        import ai_trading as gt
                        import ai_entry_watch as ew
                        from datetime import datetime
                        from zoneinfo import ZoneInfo

                        market_open = bool(gt.market_is_open())
                        day_key = datetime.fromtimestamp(
                            t0, tz=ZoneInfo("America/New_York")
                        ).strftime("%Y-%m-%d")
                        do_expire, seen, exp_day = (
                            ew.should_expire_watches_on_close(
                                market_open=market_open,
                                day_key=day_key,
                                seen_open=bool(book_state["watch_seen_open"]),
                                expired_day=str(
                                    book_state["watch_expired_day"] or ""),
                            )
                        )
                        book_state["watch_seen_open"] = seen
                        book_state["watch_expired_day"] = exp_day
                        if do_expire:
                            ew.expire_open_watches(now=t0)
                            ai_positions.log_event(
                                "watch_expire_at_close", day=day_key)
                    except Exception as e:  # noqa: BLE001
                        print(f"[ai] watch_expire failed: {e}", flush=True)

                # Heavy recheck: fire-and-forget so structure CLI cannot stall publish.
                if live_cfg.get("ai_watch_enabled", True):
                    if (
                        (t0 - book_state["last_watch_poll"]) >= wps
                        and not watch_poll_busy["on"]
                    ):
                        book_state["last_watch_poll"] = t0
                        watch_poll_busy["on"] = True
                        threading.Thread(
                            target=_run_watch_poll,
                            args=(dict(live_cfg), t0),
                            name="ai-watch-poll",
                            daemon=True,
                        ).start()
            except Exception as e:  # noqa: BLE001
                print(f"[ai] book thread error: {e}", flush=True)
            time.sleep(2.0)

    if trading:
        # Immediate publish + background book loop (independent of research).
        _publish_book(time.time())
        threading.Thread(
            target=_book_maintenance_loop,
            name="ai-book-maintenance",
            daemon=True,
        ).start()
    else:
        print("[ai] book maintenance off (no trading owner)", flush=True)

    # Main thread: research + open-bell only (may block on CLI for minutes).
    while True:
        t0 = time.time()

        _tick_source(gs_a, CLAUDE_SUGGESTIONS_FILE, SOURCE_ANTHROPIC, t0, "A")
        _tick_source(gs_x, GROK_SUGGESTIONS_FILE, SOURCE_XAI, t0, "X")

        if trading and book is not None and _open_bell_due(cfg, t0):
            try:
                book.refresh_quotes(t0)
                _run_open_bell_entries(book, cfg, t0)
            except Exception as e:  # noqa: BLE001
                print(f"[ai] open_bell failed: {e}", flush=True)
                ai_positions.log_event("open_bell_error", reason=str(e)[:200])
                _mark_open_bell_done(t0)
            try:
                _publish_book(time.time())
            except Exception:
                pass

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ai] stopped.", flush=True)
