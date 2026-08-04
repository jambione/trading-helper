"""Event → desk-agent routing (burst / buy_zone / AI AX).

Config modes per event:
  off   — ignore
  toast — notify only (user click fires the bus)
  auto  — notify + immediately queue bus action (focus/load_tv)

Used by mac_agent alert listener and shared tests. Dashboard JS mirrors the
same modes via localStorage + the Auto-Add toggle.
"""
from __future__ import annotations

import os
from typing import Any, Iterable

EVENT_BURST = "burst"
EVENT_BUY_ZONE = "buy_zone"
EVENT_AX = "ax"

ALL_EVENTS = (EVENT_BURST, EVENT_BUY_ZONE, EVENT_AX)

MODES = frozenset({"off", "toast", "auto"})

# Default bus action for each event (click or auto).
DEFAULT_ACTIONS = {
    EVENT_BURST: "load_tv",
    EVENT_BUY_ZONE: "focus",
    EVENT_AX: "focus",
}

_ENV_KEYS = {
    EVENT_BURST: "EVENT_BURST",
    EVENT_BUY_ZONE: "EVENT_BUY_ZONE",
    EVENT_AX: "EVENT_AX",
}


def normalize_mode(raw: str | None, *, default: str = "toast") -> str:
    m = (raw or default).strip().lower()
    if m in ("1", "true", "yes", "on"):
        return "auto"
    if m in ("0", "false", "no"):
        return "off"
    if m in ("notify", "toast_only", "click"):
        return "toast"
    if m in ("queue", "autoload", "handsfree"):
        return "auto"
    return m if m in MODES else default


def load_event_modes(
    env: dict[str, str] | None = None,
    *,
    auto_add: bool | None = None,
) -> dict[str, str]:
    """Resolve per-event modes from env.

    ``AUTO_ADD=1`` (legacy) upgrades burst + buy_zone to *auto* when those
    keys are unset or still at default toast. Explicit EVENT_* always win.
    """
    e = env if env is not None else os.environ
    if auto_add is None:
        auto_add = str(e.get("AUTO_ADD", "0")).strip().lower() in (
            "1", "true", "yes", "on",
        )

    modes: dict[str, str] = {}
    for ev, key in _ENV_KEYS.items():
        raw = e.get(key)
        if raw is None or str(raw).strip() == "":
            modes[ev] = "auto" if auto_add and ev != EVENT_AX else "toast"
        else:
            modes[ev] = normalize_mode(str(raw), default="toast")

    # Legacy: AUTO_ADD only forces burst/buy when EVENT_* not explicitly set
    # (handled above). AX stays toast unless EVENT_AX=auto.
    return modes


def should_toast(mode: str) -> bool:
    return normalize_mode(mode) in ("toast", "auto")


def should_auto(mode: str) -> bool:
    return normalize_mode(mode) == "auto"


def bus_action_for(event: str) -> str:
    return DEFAULT_ACTIONS.get(event, "load_tv")


def is_ax_row(row: dict[str, Any] | None) -> bool:
    if not row or not isinstance(row, dict):
        return False
    if row.get("agreement") is True:
        return True
    mark = str(row.get("source_mark") or "").upper()
    if mark == "AX":
        return True
    src = str(row.get("source") or "").lower()
    return src in ("both", "ax", "agreement")


def ax_symbols(rows: Iterable[dict[str, Any]] | None) -> set[str]:
    out: set[str] = set()
    for r in rows or []:
        if not is_ax_row(r):
            continue
        sym = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
        if sym:
            out.add(sym)
    return out


def ax_rows_by_symbol(rows: Iterable[dict[str, Any]] | None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows or []:
        if not is_ax_row(r):
            continue
        sym = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
        if sym:
            out[sym] = r
    return out


def rising_edge(now: bool, prev: bool | None) -> bool:
    """True on first observation of True, or False→True.

    ``prev is None`` means first poll for this key — treat True as rising so
    we don't miss a burst already active when the agent starts *if* you want
    that. For agent cold-start we pass prev=False after priming with current
    state so we only fire on transitions. Callers choose priming policy.
    """
    if not now:
        return False
    if prev is None:
        return False  # primed unknown → wait for transition
    return prev is False


def detect_ax_new(
    current_ax: set[str],
    previous_ax: set[str],
    *,
    primed: bool,
) -> list[str]:
    """Symbols newly marked AX since last poll. Empty until primed."""
    if not primed:
        return []
    return sorted(current_ax - previous_ax)


def build_event_payload(
    event: str,
    symbol: str,
    *,
    source: str | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Body for POST /v1/action."""
    action = bus_action_for(event)
    body: dict[str, Any] = {
        "action": action,
        "symbol": symbol.upper().strip(),
        "source": source or event,
        "meta": {
            "event": event,
            **(meta or {}),
        },
    }
    return body
