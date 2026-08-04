"""Local desk agent command bus (v1).

Pure dispatch layer used by mac_agent / windows_agent HTTP handlers.
Keeps verb names, validation, and journal I/O out of the platform files so
the contract is testable without pyautogui or a live browser.

See docs/AGENT_COMMAND_BUS.md.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BUS_VERSION = "v1"

# Actions that touch the keyboard / UI — must go through the agent queue.
QUEUED_ACTIONS = frozenset({"load_tv", "add_tv", "add", "add_wb", "focus"})
# Actions that run in the HTTP thread.
SYNC_ACTIONS = frozenset({"journal", "ping"})

ALL_ACTIONS = sorted(QUEUED_ACTIONS | SYNC_ACTIONS)

# load_tv ≡ add_tv for TV chart load; "add" keeps legacy both/wb mode.
ACTION_ALIASES = {
    "tv": "load_tv",
    "load": "load_tv",
    "add-tv": "add_tv",
    "add_to_tv": "add_tv",
    "wb": "add_wb",
    "add-wb": "add_wb",
    "both": "add",
    "focus_tv": "focus",
    "log": "journal",
    "note": "journal",
}

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

ROOT = Path(__file__).resolve().parent
ACTIVE_SYMBOL_PATH = ROOT / "active_symbol.json"
DEFAULT_JOURNAL_PATH = ROOT / "logs" / "agent_actions.jsonl"

_journal_lock = threading.Lock()


def normalize_action(raw: str | None) -> str:
    a = (raw or "").strip().lower().replace(" ", "_")
    return ACTION_ALIASES.get(a, a)


def normalize_symbol(raw: str | None) -> str:
    return (raw or "").strip().upper().lstrip("$")


def validate_symbol(sym: str) -> bool:
    return bool(sym and _TICKER_RE.match(sym))


def list_actions() -> list[dict[str, Any]]:
    """Catalog for GET /v1/actions and /health."""
    catalog = {
        "load_tv": "Load symbol into pinned TradingView tab",
        "add_tv": "Same as load_tv (legacy name for dashboard/toast)",
        "add": "TV load (legacy both-mode queue; Webull retired)",
        "add_wb": "Retired Webull path — no-op",
        "focus": "Publish active_symbol.json and load TradingView",
        "journal": "Append one action/reason line to agent_actions.jsonl",
        "ping": "Liveness check",
    }
    out = []
    for name in ALL_ACTIONS:
        out.append({
            "action": name,
            "queued": name in QUEUED_ACTIONS,
            "needs_symbol": name not in ("ping",),
            "description": catalog.get(name, ""),
        })
    return out


@dataclass
class BusResult:
    ok: bool
    action: str
    symbol: str | None = None
    source: str = ""
    queued: bool = False
    result: str = ""
    message: str = ""
    error: str = ""
    http_status: int = 200
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self, *, agent_version: str = "") -> dict[str, Any]:
        d: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "symbol": self.symbol,
            "source": self.source or None,
            "queued": self.queued,
            "result": self.result,
            "message": self.message or None,
            "bus": BUS_VERSION,
        }
        if self.error:
            d["error"] = self.error
        if agent_version:
            d["version"] = agent_version
        if self.extra:
            d.update(self.extra)
        # Drop null noise for compact clients
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class BusDeps:
    """Platform hooks injected by mac_agent / windows_agent."""

    enqueue: Callable[[str, str], None]
    """enqueue(symbol, mode) where mode is tv|wb|both."""
    publish_focus: Callable[[str, str], None] | None = None
    """publish_focus(symbol, source)."""
    journal_path: Path = DEFAULT_JOURNAL_PATH
    agent_version: str = ""


def publish_focus_file(
    symbol: str,
    source: str = "agent",
    path: Path = ACTIVE_SYMBOL_PATH,
) -> None:
    """Write active_symbol.json for monitor / flow tools."""
    payload = {
        "symbol": symbol.upper(),
        "ts": time.time(),
        "source": source or "agent",
        "platform": "agent_bus",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def append_journal(
    *,
    action: str,
    symbol: str,
    source: str,
    meta: dict[str, Any] | None,
    path: Path = DEFAULT_JOURNAL_PATH,
) -> Path:
    rec = {
        "ts": time.time(),
        "action": action,
        "symbol": symbol,
        "source": source or None,
        "meta": meta or {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _journal_lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    return path


def parse_request(data: dict[str, Any] | None) -> tuple[str, str, str, dict[str, Any]]:
    """Extract (action, symbol, source, meta) from a JSON body or query map."""
    data = data or {}
    action = normalize_action(
        str(data.get("action") or data.get("cmd") or data.get("verb") or "")
    )
    symbol = normalize_symbol(
        str(data.get("symbol") or data.get("ticker") or data.get("sym") or "")
    )
    source = str(data.get("source") or data.get("from") or "").strip()
    meta = data.get("meta")
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        meta = {"value": meta}
    # Promote common top-level fields into meta when useful for journal.
    for key in ("reason", "score", "mode", "note"):
        if key in data and key not in meta:
            meta[key] = data[key]
    return action, symbol, source, meta


def legacy_add_to_action(mode: str) -> str:
    mode = (mode or "both").strip().lower()
    if mode == "tv":
        return "add_tv"
    if mode == "wb":
        return "add_wb"
    return "add"


def dispatch(deps: BusDeps, data: dict[str, Any] | None) -> BusResult:
    """Run one bus request. Never raises for normal validation failures."""
    action, symbol, source, meta = parse_request(data)

    if not action:
        return BusResult(
            ok=False, action="", error="missing action",
            http_status=400, result="error",
        )

    if action not in ALL_ACTIONS and action not in ACTION_ALIASES.values():
        # After normalize, unknown if not in catalog
        if action not in ALL_ACTIONS:
            return BusResult(
                ok=False, action=action, symbol=symbol or None,
                source=source, error=f"unknown action: {action}",
                http_status=400, result="error",
                extra={"known": ALL_ACTIONS},
            )

    if action == "ping":
        return BusResult(
            ok=True, action="ping", source=source,
            result="pong", message="ok", http_status=200,
            extra={"actions": ALL_ACTIONS},
        )

    if action != "ping" and not validate_symbol(symbol):
        return BusResult(
            ok=False, action=action, symbol=symbol or None,
            source=source, error="missing or invalid symbol",
            http_status=400, result="error",
        )

    if action == "journal":
        try:
            path = append_journal(
                action=str(meta.get("kind") or "note"),
                symbol=symbol,
                source=source or "manual",
                meta=meta,
                path=deps.journal_path,
            )
            return BusResult(
                ok=True, action="journal", symbol=symbol, source=source,
                result="logged", message=str(path),
                http_status=200,
            )
        except Exception as e:  # noqa: BLE001
            return BusResult(
                ok=False, action="journal", symbol=symbol, source=source,
                error=str(e)[:200], result="error", http_status=500,
            )

    if action == "add_wb":
        return BusResult(
            ok=True, action="add_wb", symbol=symbol, source=source,
            result="retired", message="Webull path retired — use load_tv / add_tv",
            http_status=200,
        )

    if action == "focus":
        try:
            if deps.publish_focus:
                deps.publish_focus(symbol, source or "agent")
            else:
                publish_focus_file(symbol, source or "agent")
        except Exception as e:  # noqa: BLE001
            return BusResult(
                ok=False, action="focus", symbol=symbol, source=source,
                error=f"focus publish failed: {e}"[:200],
                result="error", http_status=500,
            )
        # Also load TV so focus means "work this name".
        try:
            deps.enqueue(symbol, "tv")
        except Exception as e:  # noqa: BLE001
            return BusResult(
                ok=False, action="focus", symbol=symbol, source=source,
                error=f"enqueue failed: {e}"[:200],
                result="error", http_status=500,
            )
        return BusResult(
            ok=True, action="focus", symbol=symbol, source=source,
            queued=True, result="queued",
            message="focus published + load_tv queued",
            http_status=202,
        )

    # load_tv / add_tv / add → queue
    mode = "tv"
    if action == "add":
        mode = str(meta.get("mode") or "both").strip().lower()
        if mode not in ("tv", "wb", "both"):
            mode = "both"
    elif action in ("load_tv", "add_tv"):
        mode = "tv"

    try:
        deps.enqueue(symbol, mode)
    except Exception as e:  # noqa: BLE001
        return BusResult(
            ok=False, action=action, symbol=symbol, source=source,
            error=f"enqueue failed: {e}"[:200],
            result="error", http_status=500,
        )

    return BusResult(
        ok=True, action=action, symbol=symbol, source=source,
        queued=True, result="queued",
        message=f"queued mode={mode}",
        http_status=202,
        extra={"mode": mode},
    )
