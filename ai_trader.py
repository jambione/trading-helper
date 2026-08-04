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
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_ENV_FILE_KEYS: list = []


def _load_env_file(path: Path) -> None:
    """Parse KEY=VALUE lines into os.environ. Shell environment always wins."""
    if not path.exists():
        return
    loaded = []
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.split(" #", 1)[0].strip()
            if key and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    if loaded:
        _ENV_FILE_KEYS.extend(loaded)
        print(f"[ENV] Loaded {len(loaded)} setting(s) from signal_engine.env",
              flush=True)


# Alpaca credentials live only in signal_engine.env; config.load_config() never
# sees them. Must run before anything imports a broker client.
_load_env_file(ROOT / "signal_engine.env")

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

LOOP_SLEEP = 5.0


def _write_json(path: Path, payload: dict) -> None:
    """Atomic replace — dashboard.py may read this file mid-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
        except Exception:
            os.close(fd)
            raise
        Path(tmp_path).replace(path)
        tmp_path = None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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


def _positions_payload(mode: str, now: float, *, book_owner: str = "") -> dict:
    import alpaca_trader

    error = ""
    positions: dict = {}
    open_orders: list = []
    if alpaca_trader.is_active():
        detail = alpaca_trader.get_positions_detail()
        if detail is None:
            error = "Alpaca position query failed"
        else:
            positions = detail
        open_orders = alpaca_trader.get_open_orders()
    else:
        error = "trader inactive — no Alpaca session"

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

    return {
        "updated": now,
        "mode": mode,
        "book_owner": book_owner,
        "error": error,
        "warnings": warnings,
        "positions": positions,
        "open_orders": open_orders,
        "performance": performance,
        "reconcile": reconcile,
        "recent_events": recent,
        "realized_r_today": day_r,
        "open_risk_pct": open_r,
        "entry_watch": entry_watch,
        # Unified Watch section: watches + open positions with P&L.
        "entry_book": entry_book,
    }


def _cfg(cfg: dict, new_key: str, old_key: str, default=None):
    """Prefer ai_* shared keys; fall back to legacy claude_* names."""
    if new_key in cfg and cfg.get(new_key) is not None:
        return cfg.get(new_key)
    if old_key in cfg and cfg.get(old_key) is not None:
        return cfg.get(old_key)
    return default


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
    claude_on = bool(_cfg(cfg, "ai_trading_enabled", "claude_trading_enabled", False))
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
    trading = bool(_cfg(cfg, "ai_trading_enabled", "claude_trading_enabled", False))
    return AiSuggestions(
        max_price=_cfg(cfg, "ai_max_price", "claude_max_price"),
        quote_interval=float(_cfg(cfg, "ai_quote_poll", "claude_quote_poll", 15.0)),
        volume_interval=float(_cfg(cfg, "ai_volume_poll", "claude_volume_poll", 60.0)),
        avg_days=int(_cfg(cfg, "ai_avg_days", "claude_avg_days", 10)),
        rvol_time_adjusted=bool(
            _cfg(cfg, "ai_rvol_time_adjusted", "claude_rvol_time_adjusted", True)),
        model=cfg.get("claude_model", "sonnet"),
        prompt_file=cfg.get("claude_prompt_file")
        or cfg.get("ai_prompt_file")
        or "ai_prompt.txt",
        request_timeout=float(cfg.get("claude_request_timeout", 600.0)),
        live_search=bool(cfg.get("claude_live_search", True)),
        save_reports=bool(cfg.get("claude_save_reports", True)),
        trading=trading,
        trade_amount=float(_cfg(cfg, "ai_trade_amount", "claude_trade_amount", 1000.0)),
        max_positions=int(_cfg(cfg, "ai_max_positions", "claude_max_positions", 5)),
        max_buys_per_poll=int(
            _cfg(cfg, "ai_max_buys_per_poll", "claude_max_buys_per_poll", 3)),
        max_sells_per_poll=int(
            _cfg(cfg, "ai_max_sells_per_poll", "claude_max_sells_per_poll", 5)),
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
                               ["04:00", "11:00", "13:00"]),
        research_weekdays_only=bool(
            cfg.get("claude_research_weekdays_only", True)),
        research_catchup_min=int(cfg.get("claude_research_catchup_min", 120)),
        risk_pct=float(_cfg(cfg, "ai_risk_pct", "claude_risk_pct", 1.0)),
        trade_style=_cfg(cfg, "ai_trade_style", "claude_trade_style",
                         "Moderate position"),
        min_reward_risk=float(
            _cfg(cfg, "ai_min_reward_risk", "claude_min_reward_risk", 3.0)),
    )


def _build_grok(cfg: dict) -> AiSuggestions:
    """xAI / Grok research source (optional paper trading — preferred owner)."""
    trading = bool(cfg.get("grok_trading_enabled", False))
    # Shared quote/volume/risk knobs; fall back to ai_* / claude_* values.
    return AiSuggestions(
        max_price=cfg.get("grok_max_price",
                          _cfg(cfg, "ai_max_price", "claude_max_price", 100.0)),
        quote_interval=float(_cfg(cfg, "ai_quote_poll", "claude_quote_poll", 15.0)),
        volume_interval=float(_cfg(cfg, "ai_volume_poll", "claude_volume_poll", 60.0)),
        avg_days=int(_cfg(cfg, "ai_avg_days", "claude_avg_days", 10)),
        rvol_time_adjusted=bool(
            _cfg(cfg, "ai_rvol_time_adjusted", "claude_rvol_time_adjusted", True)),
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
        trade_amount=float(_cfg(cfg, "ai_trade_amount", "claude_trade_amount", 1000.0)),
        max_positions=int(_cfg(cfg, "ai_max_positions", "claude_max_positions", 5)),
        max_buys_per_poll=int(
            _cfg(cfg, "ai_max_buys_per_poll", "claude_max_buys_per_poll", 3)),
        max_sells_per_poll=int(
            _cfg(cfg, "ai_max_sells_per_poll", "claude_max_sells_per_poll", 5)),
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
                               ["04:00", "11:00", "13:00"]),
        research_weekdays_only=bool(
            cfg.get("grok_research_weekdays_only", True)),
        research_catchup_min=int(cfg.get("grok_research_catchup_min", 120)),
        risk_pct=float(_cfg(cfg, "ai_risk_pct", "claude_risk_pct", 1.0)),
        trade_style=_cfg(cfg, "ai_trade_style", "claude_trade_style",
                         "Moderate position"),
        min_reward_risk=float(
            _cfg(cfg, "ai_min_reward_risk", "claude_min_reward_risk", 3.0)),
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


def _run_open_bell_entries(book: AiSuggestions, cfg: dict, now: float) -> None:
    """Act on existing ranked ideas after the open — no full research spend.

    Rebuilds the entry-watch queue from the book (structure + WAIT queue for
    the poller). Still calls ``_place_qualifying_entries`` once for names that
    already qualify as BUY (immediate fast path).
    """
    from ai_suggest import _place_qualifying_entries, tag_agreement_on_rows

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
        _cfg(cfg, "ai_positions_poll_sec", "claude_positions_poll_sec", 5.0))
    unconfirmed_ttl = float(cfg.get("ai_entry_unconfirmed_ttl_sec", 900.0))
    watch_poll_sec = float(cfg.get("ai_watch_poll_sec", 20.0) or 20.0)

    last_positions_tick = 0.0
    last_watch_poll = 0.0
    # Edge-detect RTH open→closed for watch expiry (not pre-market closed).
    watch_seen_open = False
    watch_expired_day = ""
    while True:
        t0 = time.time()

        _tick_source(gs_a, CLAUDE_SUGGESTIONS_FILE, SOURCE_ANTHROPIC, t0, "A")
        _tick_source(gs_x, GROK_SUGGESTIONS_FILE, SOURCE_XAI, t0, "X")

        if trading and book is not None and _open_bell_due(cfg, t0):
            try:
                # Refresh quotes on book rows before acting.
                book.refresh_quotes(t0)
                _run_open_bell_entries(book, cfg, t0)
            except Exception as e:  # noqa: BLE001
                print(f"[ai] open_bell failed: {e}", flush=True)
                ai_positions.log_event("open_bell_error", reason=str(e)[:200])
                _mark_open_bell_done(t0)  # avoid tight retry loop on hard fail

        # Entry-watch poller (RTH): independent interval from positions manage.
        if trading and (t0 - last_watch_poll) >= watch_poll_sec:
            last_watch_poll = t0
            try:
                live_cfg = load_config()
                watch_poll_sec = float(
                    live_cfg.get("ai_watch_poll_sec", watch_poll_sec) or watch_poll_sec
                )
                if live_cfg.get("ai_watch_enabled", True):
                    import ai_entry_watch as ew
                    ew.poll_once(cfg=live_cfg, now=t0)
            except Exception as e:  # noqa: BLE001
                print(f"[ai] watch_poll failed: {e}", flush=True)
                try:
                    ai_positions.log_event(
                        "watch_poll_error", reason=str(e)[:200])
                except Exception:
                    pass

        # Expire unfilled watches on open→closed edge (once per ET day).
        if trading:
            try:
                live_cfg = load_config()
                if live_cfg.get("ai_watch_expire_at_close", True):
                    import ai_trading as gt
                    import ai_entry_watch as ew
                    from datetime import datetime
                    from zoneinfo import ZoneInfo

                    market_open = bool(gt.market_is_open())
                    day_key = datetime.fromtimestamp(
                        t0, tz=ZoneInfo("America/New_York")
                    ).strftime("%Y-%m-%d")
                    do_expire, watch_seen_open, watch_expired_day = (
                        ew.should_expire_watches_on_close(
                            market_open=market_open,
                            day_key=day_key,
                            seen_open=watch_seen_open,
                            expired_day=watch_expired_day,
                        )
                    )
                    if do_expire:
                        ew.expire_open_watches(now=t0)
                        ai_positions.log_event(
                            "watch_expire_at_close", day=day_key)
            except Exception as e:  # noqa: BLE001
                print(f"[ai] watch_expire failed: {e}", flush=True)
                try:
                    ai_positions.log_event(
                        "watch_expire_error", reason=str(e)[:200])
                except Exception:
                    pass

        if trading and (t0 - last_positions_tick) >= positions_poll:
            last_positions_tick = t0
            try:
                ai_positions.manage_open_positions(
                    t0, unconfirmed_ttl_sec=unconfirmed_ttl)
            except Exception as e:  # noqa: BLE001
                print(f"[ai] manage_open_positions failed: {e}", flush=True)

        if trading:
            pos = _positions_payload(trading_mode, t0, book_owner=owner)
            _write_json(POSITIONS_FILE, pos)
            _write_json(POSITIONS_FILE_LEGACY, pos)  # one-release alias

        time.sleep(LOOP_SLEEP)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ai] stopped.", flush=True)
