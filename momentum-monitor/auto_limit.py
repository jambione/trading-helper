"""Rising-edge buy_zone → adaptive Alpaca limit (feature-flagged).

Default OFF. Paper-only unless auto_limit_live is true.
Pure gate logic is testable without placing orders.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class AutoLimitState:
    prev_status: dict[str, str] = field(default_factory=dict)
    last_fire_ts: dict[str, float] = field(default_factory=dict)
    session_fires: int = 0
    session_day: str = ""  # ET YYYY-MM-DD
    session_halted: bool = False
    halt_reason: str = ""


def session_day_et(now: float | None = None) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ts = time.time() if now is None else now
    return datetime.fromtimestamp(ts, ZoneInfo("America/New_York")).strftime("%Y-%m-%d")


def is_buy_zone_status(status: str | None) -> bool:
    s = (status or "").strip().lower()
    return s in ("buy_zone", "buy")


def rising_buy_zone(sym: str, status: str | None, prev: dict[str, str]) -> bool:
    """True when status newly enters buy_zone (not first unknown observation)."""
    cur = (status or "").strip().lower()
    if not is_buy_zone_status(cur):
        return False
    if sym not in prev:
        # First sighting: record only, do not fire (avoid open-fire on startup)
        return False
    was = (prev.get(sym) or "").strip().lower()
    return not is_buy_zone_status(was)


def gate_auto_limit(
    *,
    enabled: bool,
    live_ok: bool,
    trader_mode: str,
    has_position: bool,
    cooldown_sec: float,
    max_per_session: int,
    last_fire: float | None,
    session_fires: int,
    now: float,
    session_halted: bool = False,
) -> tuple[bool, str]:
    if not enabled:
        return False, "disabled"
    if session_halted:
        return False, "daily_halt"
    mode = (trader_mode or "off").strip().lower()
    if mode == "off":
        return False, "trader_off"
    if mode == "live" and not live_ok:
        return False, "live_blocked"
    if mode not in ("paper", "live"):
        return False, f"bad_mode_{mode}"
    if has_position:
        return False, "already_long"
    if max_per_session > 0 and session_fires >= max_per_session:
        return False, "session_cap"
    if last_fire is not None and cooldown_sec > 0:
        if (now - last_fire) < cooldown_sec:
            return False, "cooldown"
    return True, "ok"


def constructive_setup(
    row: dict,
    *,
    require: bool = True,
    min_proximity_pct: float = 67.0,
    block_sell_signal: bool = True,
    block_dual_pctr_falling: bool = True,
    require_buy_signal: bool = False,
) -> tuple[bool, str]:
    """Trend/setup filter: avoid auto-buying names that look broken or falling.

    Rule (when require=True):
      • signal_proximity present with bars
      • not sell_signal (engine exit / bearish composite)
      • proximity_pct >= min (default 67 — yellow buy-circle or better)
      • not both %R fast and slow falling (exhaustion still diving)
      • optional: buy_signal must be true

    When require=False, always passes (legacy / testing).
    """
    if not require:
        return True, "ok"

    sp = row.get("signal_proximity")
    if not isinstance(sp, dict) or not sp:
        return False, "no_proximity"

    # Prefer engine-backed rows; empty proximity without bars is not tradable.
    if sp.get("bars_fetched") is False:
        return False, "no_bars"

    if block_sell_signal and sp.get("sell_signal") is True:
        return False, "sell_signal"

    if require_buy_signal and sp.get("buy_signal") is not True:
        return False, "no_buy_signal"

    try:
        prox = sp.get("proximity_pct")
        if prox is not None and float(min_proximity_pct) > 0:
            if float(prox) < float(min_proximity_pct):
                return False, f"proximity_{float(prox):.0f}<{float(min_proximity_pct):.0f}"
    except (TypeError, ValueError):
        return False, "bad_proximity"

    if block_dual_pctr_falling:
        # Both %R lines falling → exhaustion still pointing lower; wait for turn.
        if sp.get("pctr_falling") is True and sp.get("pctr_slow_falling") is True:
            return False, "pctr_dual_falling"

    return True, "ok"


def process_rows(
    rows: list[dict],
    state: AutoLimitState,
    *,
    cfg: dict,
    trader_mode: str,
    position_symbols: set[str],
    buy_fn: Callable[[str, dict], str],
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Scan rows for buy_zone edges; fire buy_fn when gates pass.

    buy_fn(symbol, row) -> status string.
    Returns list of event dicts for journal/status.
    """
    now = time.time() if now is None else now
    day = session_day_et(now)
    if state.session_day != day:
        state.session_day = day
        state.session_fires = 0
        state.last_fire_ts.clear()
        state.session_halted = False
        state.halt_reason = ""

    # External halt from desk_book session_R
    if cfg.get("_session_halted"):
        state.session_halted = True
        state.halt_reason = str(cfg.get("_halt_reason") or "daily_halt")

    enabled = bool(cfg.get("auto_limit_enabled", False))
    live_ok = bool(cfg.get("auto_limit_live", False))
    if bool(cfg.get("daily_loss_halt_auto", True)) and state.session_halted:
        # still walk rows to update prev_status, but skip fires
        pass
    signals = cfg.get("auto_limit_signals") or ["buy_zone"]
    if isinstance(signals, str):
        signals = [signals]
    signals = {str(s).lower() for s in signals}
    if "buy_zone" not in signals and "buy" not in signals:
        # v1 only implements buy_zone; other tokens reserved
        return []

    cooldown = float(cfg.get("auto_limit_cooldown_sec", 900))
    max_sess = int(cfg.get("auto_limit_max_per_session", 3))
    max_spread = float(cfg.get("entry_max_spread_pct", 1.0))
    pad_max = float(cfg.get("entry_pad_max_pct", 0.15))
    rvol_hot = float(cfg.get("rvol_hot", 3.0))
    max_price = cfg.get("auto_limit_max_price")
    if max_price is not None:
        try:
            max_price = float(max_price)
        except (TypeError, ValueError):
            max_price = None

    events: list[dict[str, Any]] = []
    # Snapshot prev before updates so multi-row scan is consistent
    prev = dict(state.prev_status)

    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("ticker") or row.get("symbol") or "").upper().strip()
        if not sym:
            continue
        sp = row.get("signal_proximity") or {}
        status = sp.get("status") if isinstance(sp, dict) else None
        status_s = str(status or "")

        edge = rising_buy_zone(sym, status_s, prev)
        state.prev_status[sym] = status_s

        if not edge:
            continue

        ok, why = gate_auto_limit(
            enabled=enabled,
            live_ok=live_ok,
            trader_mode=trader_mode,
            has_position=sym in position_symbols,
            cooldown_sec=cooldown,
            max_per_session=max_sess,
            last_fire=state.last_fire_ts.get(sym),
            session_fires=state.session_fires,
            now=now,
            session_halted=bool(
                state.session_halted
                and bool(cfg.get("daily_loss_halt_auto", True))
            ),
        )
        if not ok:
            events.append({
                "kind": "auto_limit_skip",
                "symbol": sym,
                "reason": why,
            })
            continue

        # Constructive tape: no sell_signal, %R not dual-falling, min proximity
        ok_c, why_c = constructive_setup(
            row,
            require=bool(cfg.get("auto_limit_require_constructive", True)),
            min_proximity_pct=float(
                cfg.get("auto_limit_min_proximity_pct", 67.0)),
            block_sell_signal=bool(
                cfg.get("auto_limit_block_sell_signal", True)),
            block_dual_pctr_falling=bool(
                cfg.get("auto_limit_block_pctr_falling", True)),
            require_buy_signal=bool(
                cfg.get("auto_limit_require_buy_signal", False)),
        )
        if not ok_c:
            events.append({
                "kind": "auto_limit_skip",
                "symbol": sym,
                "reason": why_c,
            })
            continue

        # Attach policy knobs onto a shallow row copy for buy_fn
        row_ctx = dict(row)
        row_ctx["_policy"] = {
            "max_spread_pct": max_spread,
            "pad_max_pct": pad_max,
            "rvol_hot": rvol_hot,
            "max_price": max_price,
        }
        try:
            msg = buy_fn(sym, row_ctx)
        except Exception as e:  # noqa: BLE001
            msg = f"BUY {sym} error {e}"
            events.append({
                "kind": "auto_limit_error",
                "symbol": sym,
                "message": msg,
            })
            continue

        # buy_fn may return str or (str, plan_dict)
        plan = None
        if isinstance(msg, tuple):
            msg, plan = msg[0], (msg[1] if len(msg) > 1 else None)

        state.last_fire_ts[sym] = now
        state.session_fires += 1
        events.append({
            "kind": "auto_limit",
            "symbol": sym,
            "message": str(msg),
            "plan": plan,
        })

    return events
