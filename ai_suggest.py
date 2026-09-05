"""AI stock suggestions for the desk (multi-source ready).

Runs the research prompt on a slow clock — Claude Code CLI by default, with
the Grok Build CLI and the paid xAI HTTP API as legacy fallbacks — parses a
JSON list of tickers, then re-uses the same Alpaca quote / volume / RVOL path
as the Stocktwits panel so the desk sees comparable columns.

Auth: Claude Code login (default backend); ``XAI_API_KEY`` for the xAI paths.
Prompt: ``momentum-monitor/ai_prompt.txt`` (override via config).

Expected model output (flexible — fences and bare arrays also accepted)::

    {
      "suggestions": [
        {"symbol": "NVDA", "score": 8.5, "reason": "short why"},
        ...
      ]
    }
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Shared quote / volume helpers — same IEX provenance as the ST panel.
from stocktwits_trending import (  # noqa: E402
    ET,
    _alpaca_client,
    apply_look_highlights,
    enrich_with_alpaca,
    price_age,
    row_rvol,
)

# Agent Tools API (web_search / x_search). Legacy chat live-search is 410 Gone.
XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
DEFAULT_XAI_MODEL = "grok-4.5"
DEFAULT_PROMPT_FILE = "ai_prompt.txt"
# Full research + agent tools is slow; default well above chat-only timeouts.
DEFAULT_TIMEOUT = 600.0
from ai_paths import (  # noqa: E402
    CLAUDE_SUGGESTIONS_FILE,
    GROK_SUGGESTIONS_FILE,
    resolve_report_dir,
)

REPORT_DIR = resolve_report_dir()
TOKEN_METRICS_PATH = REPORT_DIR / "token_metrics.jsonl"
SCHEDULE_STATE_PATH = REPORT_DIR / "schedule_state.json"

# Usage from the most recent Claude CLI call (research or repair phase).
last_usage: dict[str, Any] = {}

# Framing only — the research process lives in ai_prompt.txt.
# Efficiency rules live here so every call shares the same budget.
_SYSTEM = (
    "You are a competitive quantitative equity trader on a dual-AI paper desk. "
    "Win condition: max realized R on a trade that starts after this suggestion "
    "and ends before the next research run (desk flats ~10m prior). "
    "Not multi-month stories. "
    "If a RIVAL AI COMPETITION block is present (duel mode only), use it; otherwise "
    "ignore competition framing. "
    "Follow the user's full research process under a strict token budget: "
    "few targeted searches, compact notes, no essays. "
    "Prefer live tool results over memory; re-validate prior-run names with fresh data. "
    "Use X/x.com (x_search or web hits on x.com) for finalist sentiment when tools allow. "
    "HARD: the first character of the final answer must be '{' — the JSON object. "
    "No preamble or tool narration before that brace. No markdown fences. "
    "After JSON, brief process notes only. "
    "suggestions[0] is your single best session idea (champion). US equity tickers only. "
    "Skeptical and data-driven. Never invent tool results, posts, or fill prices."
)

# Default efficiency knobs (overridable via config / call_claude kwargs).
DEFAULT_MAX_TURNS = 8          # caps server-side tool loop (search rounds)
DEFAULT_MAX_OUTPUT_TOKENS = 10000  # includes reasoning on some models — leave headroom
# "web_x" attaches web_search + x_search on the xAI HTTP API path.
# Claude CLI still uses WebSearch/WebFetch only (prompt asks for x.com via web).
DEFAULT_SEARCH_TOOLS = "web_x"   # "web" | "web_x" | "none"
# Backend:
#   claude_cli — Claude Code CLI (claude -p), Anthropic/subscription auth
#   agy / gemini_cli — Antigravity CLI (agy -p), Gemini subscription auth
#   cli / grok_cli — Grok Build CLI (grok.com login)
#   api — paid xAI console HTTP (XAI_API_KEY)
DEFAULT_BACKEND = "claude_cli"
DEFAULT_CLI_BIN = "grok"
DEFAULT_CLAUDE_CLI_BIN = "claude"
DEFAULT_CLAUDE_MODEL = "sonnet"
DEFAULT_AGY_CLI_BIN = "agy"
DEFAULT_AGY_MODEL = "gemini-3.7-flash-high"
# JSON-trailer repair is extraction — cheapest Gemini, no extra tools.
DEFAULT_AGY_REPAIR_MODEL = "gemini-3.7-flash-low"
DEFAULT_AGY_EFFORT = "high"
AGY_BACKENDS = frozenset({"agy", "gemini", "gemini_cli", "antigravity"})
# JSON-trailer repair is trivial extraction — cheapest model, no web tools.
DEFAULT_CLAUDE_REPAIR_MODEL = "haiku"
# Research effort (low|medium|high|xhigh|max). xhigh measurably grounds the
# analysis — dated catalysts and real multiples instead of vague labels — for
# ~$0.06 more per run. Cost is dominated by search fees, not thinking tokens,
# so buying depth here is cheap; cut spend with the poll window instead.
DEFAULT_CLAUDE_EFFORT = "xhigh"

# Fixed ET wall-clock run times rather than an interval. Three RTH-aligned
# slots (~4h apart): pre-open prep, late morning, mid-afternoon.
DEFAULT_RESEARCH_TIMES = ("08:30", "11:30", "14:30")
DEFAULT_RESEARCH_WEEKDAYS_ONLY = True
# A slot missed while the desk was down stays runnable this long. Without a
# bound, starting the desk at 23:00 would fire the 13:00 run on stale data.
DEFAULT_RESEARCH_CATCHUP_MIN = 120
# Module default follows DEFAULT_BACKEND; the xAI paths use DEFAULT_XAI_MODEL.
DEFAULT_MODEL = DEFAULT_CLAUDE_MODEL

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


import desk_core  # noqa: E402

_load_env = desk_core.load_desk_env


def _f(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def api_key() -> str:
    _load_env()
    return (os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY") or "").strip()


def resolve_grok_cli(cli_bin: str | None = None) -> str | None:
    """Path to the Grok Build CLI binary, or None if missing."""
    name = (cli_bin or os.getenv("GROK_CLI_BIN") or DEFAULT_CLI_BIN).strip()
    if not name:
        return None
    p = Path(name)
    if p.is_file() and os.access(p, os.X_OK):
        return str(p)
    # Prefer the standard install location over a random PATH hit.
    home_bin = Path.home() / ".grok" / "bin" / "grok"
    if home_bin.is_file() and os.access(home_bin, os.X_OK):
        return str(home_bin)
    return shutil.which(name)


def resolve_claude_cli(cli_bin: str | None = None) -> str | None:
    """Path to the Claude Code CLI binary, or None if missing."""
    name = (cli_bin or os.getenv("CLAUDE_CLI_BIN") or DEFAULT_CLAUDE_CLI_BIN).strip()
    if not name:
        return None
    p = Path(name)
    if p.is_file() and os.access(p, os.X_OK):
        return str(p)
    home_bin = Path.home() / ".local" / "bin" / "claude"
    if home_bin.is_file() and os.access(home_bin, os.X_OK):
        return str(home_bin)
    return shutil.which(name)


def cli_available(cli_bin: str | None = None) -> bool:
    return resolve_grok_cli(cli_bin) is not None


def claude_cli_available(cli_bin: str | None = None) -> bool:
    return resolve_claude_cli(cli_bin) is not None


def cli_logged_in() -> bool:
    """True when ~/.grok/auth.json has a session (grok.com / OAuth login)."""
    auth = Path.home() / ".grok" / "auth.json"
    if not auth.is_file():
        return False
    try:
        data = json.loads(auth.read_text(encoding="utf-8"))
        return bool(data)
    except Exception:
        return False


# Substrings Claude Code prints when the subscription session is missing.
_CLAUDE_NOT_LOGGED_IN_MARKERS = (
    "not logged in",
    "please run /login",
    "please run claude /login",
    "run /login",
)


def claude_output_looks_logged_out(text: str | None) -> bool:
    """True when CLI stdout/stderr is an auth failure, not real research text."""
    s = (text or "").strip().lower()
    if not s:
        return False
    return any(m in s for m in _CLAUDE_NOT_LOGGED_IN_MARKERS)


def claude_has_api_key() -> bool:
    """ANTHROPIC_API_KEY (or CLAUDE_API_KEY) present — server-style auth."""
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        v = (os.getenv(k) or "").strip()
        if v:
            return True
    return False


def claude_auth_status(cli_bin: str | None = None,
                       *, timeout: float = 15.0) -> dict[str, Any]:
    """Run ``claude auth status`` and return a normalized dict.

    Keys: ok (bool), logged_in (bool), raw (str), error (str), binary (str|None).
    Never raises — callers use this for probes and preflight.
    """
    binary = resolve_claude_cli(cli_bin)
    if not binary:
        return {
            "ok": False,
            "logged_in": False,
            "raw": "",
            "error": "Claude CLI not found",
            "binary": None,
        }
    if claude_has_api_key():
        return {
            "ok": True,
            "logged_in": True,
            "raw": "ANTHROPIC_API_KEY set",
            "error": "",
            "binary": binary,
            "auth_method": "api_key",
        }
    try:
        proc = subprocess.run(
            [binary, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=max(5.0, float(timeout)),
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "logged_in": False,
            "raw": "",
            "error": "claude auth status timed out",
            "binary": binary,
        }
    except OSError as e:
        return {
            "ok": False,
            "logged_in": False,
            "raw": "",
            "error": f"claude auth status failed: {e}",
            "binary": binary,
        }
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    raw = out or err
    logged_in = False
    # Prefer JSON envelope from modern Claude Code.
    try:
        data = json.loads(out) if out else None
        if isinstance(data, dict):
            if "loggedIn" in data:
                logged_in = bool(data.get("loggedIn"))
            elif "logged_in" in data:
                logged_in = bool(data.get("logged_in"))
            return {
                "ok": logged_in,
                "logged_in": logged_in,
                "raw": raw[:500],
                "error": "" if logged_in else "Claude CLI not logged in — run: claude /login",
                "binary": binary,
                "email": data.get("email") or "",
                "auth_method": data.get("authMethod") or data.get("auth_method") or "",
            }
    except json.JSONDecodeError:
        pass
    if claude_output_looks_logged_out(raw):
        logged_in = False
    elif proc.returncode == 0 and raw:
        # Older CLIs: non-empty success without logout markers.
        low = raw.lower()
        if "loggedin" in low.replace(" ", "") and "false" in low:
            logged_in = False
        else:
            logged_in = True
    else:
        logged_in = False
    return {
        "ok": logged_in,
        "logged_in": logged_in,
        "raw": raw[:500],
        "error": "" if logged_in else (
            "Claude CLI not logged in — run: claude /login "
            "(on this machine as the trading user)"
        ),
        "binary": binary,
    }


def claude_cli_logged_in(cli_bin: str | None = None,
                         *, timeout: float = 15.0) -> bool:
    """True when subscription session or ANTHROPIC_API_KEY is available."""
    return bool(claude_auth_status(cli_bin, timeout=timeout).get("logged_in"))


def claude_cli_ready(cli_bin: str | None = None) -> bool:
    """Binary exists and auth works (login session or API key)."""
    if not claude_cli_available(cli_bin):
        return False
    return claude_cli_logged_in(cli_bin)


def is_agy_backend(backend: str | None, cli_bin: str | None = None) -> bool:
    """True when this research slot is Antigravity CLI, not Claude Code.

    Matches an explicit backend name, or a ``claude_cli_bin`` that is ``agy``
    so a partial config flip cannot send Claude flags to the Gemini binary.
    """
    b = (backend or "").strip().lower()
    if b in AGY_BACKENDS:
        return True
    return Path(str(cli_bin or "")).name.lower() == "agy"


def resolve_agy_cli(cli_bin: str | None = None) -> str | None:
    """Path to the Antigravity CLI binary, or None if missing."""
    name = (cli_bin or os.getenv("AGY_CLI_BIN") or DEFAULT_AGY_CLI_BIN).strip()
    if not name:
        return None
    p = Path(name)
    if p.is_file() and os.access(p, os.X_OK):
        return str(p)
    home_bin = Path.home() / ".local" / "bin" / "agy"
    if home_bin.is_file() and os.access(home_bin, os.X_OK):
        return str(home_bin)
    found = shutil.which(name)
    if found:
        return found
    if name != DEFAULT_AGY_CLI_BIN:
        return shutil.which(DEFAULT_AGY_CLI_BIN)
    return None


def agy_cli_available(cli_bin: str | None = None) -> bool:
    return resolve_agy_cli(cli_bin) is not None


_AGY_NOT_LOGGED_IN_MARKERS = (
    "authentication required",
    "authentication failed",
    "not logged into antigravity",
    "please sign in",
    "run 'agy' to log in",
    "run agy to log in",
    "launch the cli without arguments to sign in",
)


def agy_output_looks_logged_out(text: str | None) -> bool:
    s = (text or "").strip().lower()
    if not s:
        return False
    return any(m in s for m in _AGY_NOT_LOGGED_IN_MARKERS)


def _agy_model(model: str | None) -> str:
    m = (model or "").strip()
    low = m.lower()
    if not m or low in ("sonnet", "haiku", "opus") or low.startswith("grok"):
        return DEFAULT_AGY_MODEL
    return m


def _agy_effort(effort: str | None) -> str | None:
    e = (effort or "").strip().lower()
    if not e:
        return DEFAULT_AGY_EFFORT
    if e in ("xhigh", "max", "highest"):
        return "high"
    if e in ("low", "medium", "high"):
        return e
    return DEFAULT_AGY_EFFORT


def _agy_print_timeout(timeout: float) -> str:
    try:
        sec = int(max(30.0, float(timeout)))
    except (TypeError, ValueError):
        sec = 600
    return f"{sec}s"


def agy_auth_status(cli_bin: str | None = None,
                    *, timeout: float = 20.0) -> dict[str, Any]:
    """``agy models`` is the login probe — there is no ``agy auth status``.

    Keys match ``claude_auth_status`` so the trader print path can share shape.
    """
    binary = resolve_agy_cli(cli_bin)
    if not binary:
        return {
            "ok": False,
            "logged_in": False,
            "raw": "",
            "error": "Antigravity CLI not found",
            "binary": None,
        }
    try:
        proc = subprocess.run(
            [binary, "models"],
            capture_output=True,
            text=True,
            timeout=max(5.0, float(timeout)),
            env={**os.environ},
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "logged_in": False,
            "raw": "",
            "error": "agy models timed out",
            "binary": binary,
        }
    except OSError as e:
        return {
            "ok": False,
            "logged_in": False,
            "raw": "",
            "error": f"agy models failed: {e}",
            "binary": binary,
        }
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    raw = out or err
    logged_out = agy_output_looks_logged_out(out) or agy_output_looks_logged_out(err)
    logged_in = proc.returncode == 0 and bool(out) and not logged_out
    return {
        "ok": logged_in,
        "logged_in": logged_in,
        "raw": raw[:500],
        "error": "" if logged_in else (
            "Antigravity CLI not logged in — run: agy   "
            "(from a Terminal on this machine, then restart the stack there)"
        ),
        "binary": binary,
        "auth_method": "session" if logged_in else "",
    }


def agy_cli_logged_in(cli_bin: str | None = None,
                      *, timeout: float = 20.0) -> bool:
    return bool(agy_auth_status(cli_bin, timeout=timeout).get("logged_in"))


def agy_cli_ready(cli_bin: str | None = None) -> bool:
    if not agy_cli_available(cli_bin):
        return False
    return agy_cli_logged_in(cli_bin)


def prompt_path(cfg_name: str | None = None) -> Path:
    name = (cfg_name or DEFAULT_PROMPT_FILE).strip() or DEFAULT_PROMPT_FILE
    p = Path(name)
    if not p.is_absolute():
        p = ROOT / p
    return p


def load_prompt(cfg_name: str | None = None,
                max_price: float | None = None) -> str:
    path = prompt_path(cfg_name)
    if not path.is_file():
        raise FileNotFoundError(f"Claude prompt file missing: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Claude prompt file is empty: {path}")
    # Inject live price ceiling so config changes don't require editing the file.
    if max_price is not None and float(max_price) > 0:
        cap = float(max_price)
        text += (
            f"\n\nCONFIG PRICE CAP (overrides any looser language above): "
            f"every suggested symbol must have last price STRICTLY UNDER "
            f"${cap:g}. Drop any name at or above ${cap:g}.\n"
        )
    return text


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t, count=1, flags=re.I)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _iter_json_blobs(text: str) -> list[Any]:
    """All top-level JSON objects/arrays found in free-form model text."""
    t = _strip_fences(text)
    found: list[Any] = []
    try:
        found.append(json.loads(t))
        return found
    except json.JSONDecodeError:
        pass

    i = 0
    n = len(t)
    while i < n:
        if t[i] not in "{[":
            i += 1
            continue
        opener = t[i]
        closer = "}" if opener == "{" else "]"
        depth = 0
        in_str = False
        esc = False
        for j in range(i, n):
            ch = t[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    try:
                        found.append(json.loads(t[i : j + 1]))
                    except json.JSONDecodeError:
                        pass
                    i = j + 1
                    break
        else:
            break
    return found


def _best_suggestions_payload(text: str) -> Any:
    """Prefer a JSON object that carries a suggestions/stocks list.

    The research prompt produces prose first and a trailer object last. Taking
    the first `{` would often grab an unrelated snippet; taking the last
    suggestions-bearing object matches the instructed trailer.
    """
    blobs = _iter_json_blobs(text)
    if not blobs:
        raise ValueError("no JSON object/array found in model response")
    preferred_keys = ("suggestions", "stocks", "tickers", "symbols", "results")
    for blob in reversed(blobs):
        if isinstance(blob, dict) and any(
            isinstance(blob.get(k), list) for k in preferred_keys
        ):
            return blob
    # Bare array of tickers, or single-symbol object.
    for blob in reversed(blobs):
        if isinstance(blob, list) and blob:
            return blob
        if isinstance(blob, dict) and (blob.get("symbol") or blob.get("ticker")):
            return blob
    return blobs[-1]


# Canonical AI research sources (desk marks + merge provenance).
# Display letters: G = Google AGY, X = xAI (Grok), GX = both agree.
# Legacy names anthropic/claude/A/AX still normalize here.
SOURCE_AGY = "agy"
SOURCE_ANTHROPIC = SOURCE_AGY  # legacy alias — do not use in new code
SOURCE_XAI = "xai"
SOURCE_MARK = {
    SOURCE_AGY: "G",
    SOURCE_XAI: "X",
}
AGREEMENT_MARK = "GX"


def normalize_ai_source(value: str | None) -> str:
    """Map backend / free-form tags to ``agy`` | ``xai`` | ``both`` | ``unknown``."""
    s = (value or "").strip().lower()
    if not s:
        return "unknown"
    if s in ("g", "agy", "google", "gemini", "gemini_cli", "antigravity",
             "a", "anthropic", "claude", "claude_cli"):
        return SOURCE_AGY
    if s in ("x", "xai", "grok", "grok_cli", "cli", "api"):
        # ``cli`` / ``api`` are the historical Grok backends in this module.
        return SOURCE_XAI
    if s in ("both", "ax", "a+x", "xa", "gx", "g+x", "xg", "merged"):
        return "both"
    if "agy" in s or "gemini" in s or "anthropic" in s or s.startswith("claude"):
        return SOURCE_AGY
    if "xai" in s or "grok" in s:
        return SOURCE_XAI
    return "unknown"


def ai_source_mark(value: str | None) -> str:
    """Desk mark: G (Google AGY), X (xAI), GX (agreement), ? (unknown)."""
    src = normalize_ai_source(value)
    if src == "both":
        return AGREEMENT_MARK
    return SOURCE_MARK.get(src, "?")

def source_from_backend(backend: str | None) -> str:
    """Canonical source id for a claude_suggest backend string."""
    b = (backend or DEFAULT_BACKEND).strip().lower()
    if b in ("claude_cli", "claude") or b in AGY_BACKENDS:
        return SOURCE_ANTHROPIC
    if b in ("cli", "grok_cli", "api"):
        return SOURCE_XAI
    return normalize_ai_source(b)


def _row_score(row: dict[str, Any]) -> float | None:
    return _f(row.get("score") if row.get("score") is not None
              else row.get("trending_score"))


def _row_rank(row: dict[str, Any]) -> int:
    try:
        r = int(row.get("rank") or 0)
        return r if r > 0 else 99
    except (TypeError, ValueError):
        return 99


def merge_suggestion_rows(
    anthropic_rows: list[dict[str, Any]] | None,
    xai_rows: list[dict[str, Any]] | None,
    *,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    """Union Google AGY (G) + Grok (X) lists into one ranked desk list.

    Sort key (pre-trade quality, not predicted P&L):
      1. Agreement first (both sources named the symbol) — mark ``GX``
      2. Higher max(score_G, score_X)
      3. Better (lower) min list rank among sources that listed it

    Each output row keeps ``sources`` (list), ``source`` (primary tag),
    ``source_mark`` (``G`` / ``X`` / ``GX``), and combined ``reason`` when both
    spoke. Does not place trades.
    """
    by_sym: dict[str, dict[str, Any]] = {}

    def _ingest(rows: list[dict[str, Any]] | None, forced: str) -> None:
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            sym = str(raw.get("symbol") or raw.get("ticker") or "").upper().strip()
            if not sym:
                continue
            src = normalize_ai_source(raw.get("source") or forced)
            if src == "unknown":
                src = forced
            slot = by_sym.setdefault(sym, {
                "symbol": sym,
                "by_source": {},
            })
            # First hit per source wins (lists are best-first already).
            if src not in slot["by_source"]:
                slot["by_source"][src] = dict(raw)
                slot["by_source"][src]["source"] = src
                slot["by_source"][src]["source_mark"] = SOURCE_MARK.get(src, "?")

    _ingest(anthropic_rows, SOURCE_ANTHROPIC)
    _ingest(xai_rows, SOURCE_XAI)

    out: list[dict[str, Any]] = []
    for sym, bag in by_sym.items():
        parts: dict[str, dict] = bag["by_source"]
        sources = sorted(parts.keys())  # anthropic before xai alphabetically
        a = parts.get(SOURCE_ANTHROPIC)
        x = parts.get(SOURCE_XAI)
        both = a is not None and x is not None

        # Prefer richer primary row: higher score, else better rank, else A then X.
        candidates = [r for r in (a, x) if r is not None]

        def _pick_key(r: dict) -> tuple:
            sc = _row_score(r)
            return (
                sc is not None,
                sc if sc is not None else -1.0,
                -_row_rank(r),
            )

        primary = max(candidates, key=_pick_key)
        merged = dict(primary)
        merged["symbol"] = sym
        merged["sources"] = list(sources)
        if both:
            merged["source"] = "both"
            merged["source_mark"] = AGREEMENT_MARK
        else:
            only = sources[0]
            merged["source"] = only
            merged["source_mark"] = SOURCE_MARK.get(only, "?")

        # Scores / ranks for sort and display
        scores = [s for s in (_row_score(a) if a else None,
                              _row_score(x) if x else None) if s is not None]
        ranks = [_row_rank(r) for r in candidates]
        merged["trending_score"] = max(scores) if scores else _row_score(primary)
        merged["score"] = merged["trending_score"]
        merged["rank_best"] = min(ranks) if ranks else _row_rank(primary)
        if a is not None:
            merged["score_anthropic"] = _row_score(a)
            merged["rank_anthropic"] = _row_rank(a)
        if x is not None:
            merged["score_xai"] = _row_score(x)
            merged["rank_xai"] = _row_rank(x)

        # Combined one-line why
        ra = (str(a.get("reason") or "").strip() if a else "")
        rx = (str(x.get("reason") or "").strip() if x else "")
        if ra and rx and ra != rx:
            merged["reason"] = f"A:{ra[:36]} | X:{rx[:36]}"[:80]
        elif ra:
            merged["reason"] = ra[:80]
        elif rx:
            merged["reason"] = rx[:80]

        # Agreement flag for filters / future entry gates
        merged["agreement"] = both

        out.append(merged)

    def _sort_key(r: dict) -> tuple:
        sc = _row_score(r)
        return (
            0 if r.get("agreement") else 1,
            -(sc if sc is not None else -1.0),
            int(r.get("rank_best") or 99),
            r.get("symbol") or "",
        )

    out.sort(key=_sort_key)
    # Re-number display rank 1..n after merge order
    for i, r in enumerate(out, start=1):
        r["rank"] = i
    if max_rows is not None and max_rows > 0:
        out = out[: int(max_rows)]
    return out


def parse_suggestions(payload: Any,
                      *,
                      source: str | None = SOURCE_ANTHROPIC,
                      ) -> list[dict[str, Any]]:
    """Normalize model JSON → list of suggestion rows (symbol, score, reason).

    ``source`` tags each row for multi-provider provenance (Anthropic vs xAI).
    """
    src = normalize_ai_source(source)
    if src == "unknown":
        src = SOURCE_ANTHROPIC
    items: list[Any]
    if isinstance(payload, dict):
        for key in ("suggestions", "stocks", "tickers", "symbols", "results"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            # Single object with a symbol field.
            if payload.get("symbol") or payload.get("ticker"):
                items = [payload]
            else:
                items = []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, raw in enumerate(items):
        if isinstance(raw, str):
            sym = raw.upper().strip()
            score = None
            reason = ""
        elif isinstance(raw, dict):
            raw_sym = str(
                raw.get("symbol")
                or raw.get("ticker")
                or raw.get("sym")
                or ""
            ).upper().strip()
            # Strip $ prefixes; take first token only when the rest is a
            # parenthetical / name ("NVDA - Nvidia"), not free-form junk.
            raw_sym = raw_sym.lstrip("$")
            sym = raw_sym.split()[0] if raw_sym else ""
            score = _f(raw.get("score") if raw.get("score") is not None
                       else raw.get("confidence") if raw.get("confidence") is not None
                       else raw.get("rank"))
            # Claude sometimes uses conviction labels instead of numeric score.
            if score is None:
                conv = str(raw.get("conviction") or "").strip().upper()
                score = {
                    "HIGH": 8.5, "MED": 7.0, "MEDIUM": 7.0, "LOW": 5.5,
                }.get(conv)
            reason = str(
                raw.get("reason") or raw.get("why")
                or raw.get("thesis") or raw.get("note")
                or raw.get("summary") or ""
            ).strip()
            # Nested thesis.one_line from richer Claude JSON
            if not reason and isinstance(raw.get("thesis"), dict):
                reason = str(
                    raw["thesis"].get("one_line") or raw["thesis"].get("summary") or ""
                ).strip()
            # Extra research fields — stored for journal / future columns.
            p30 = _f(raw.get("p30") or raw.get("prob_30") or raw.get("prob_30pct"))
            p50 = _f(raw.get("p50") or raw.get("prob_50") or raw.get("prob_50pct"))
            p100 = _f(raw.get("p100") or raw.get("prob_100") or raw.get("prob_100pct"))
            position_pct = _f(raw.get("position_pct") or raw.get("allocation_pct"))
            invalidation = str(raw.get("invalidation") or raw.get("stop")
                               or raw.get("stop_loss") or "").strip()
            summary = str(raw.get("summary") or "").strip()
        else:
            continue
        # Max 5 chars for common US equities; allow up to 6 for BRK.B etc.
        if not sym or not _TICKER_RE.match(sym) or len(sym) > 6:
            continue
        if sym in seen:
            continue
        seen.add(sym)
        # Rank is 1-based list order (best first). Score is optional model 0–10.
        out.append({
            "symbol": sym,
            "rank": len(out) + 1,
            "trending_score": score,  # same key as ST so LOOK / panel reuse it
            "reason": reason[:80],
            "summary": summary[:240],
            "p30": p30,
            "p50": p50,
            "p100": p100,
            "position_pct": position_pct,
            "invalidation": invalidation[:80],
            "title": "",
            "instrument_class": "stock",
            "is_equity": True,
            "is_crypto": False,
            "high_52w": None,
            "low_52w": None,
            "avg_vol_consolidated": None,
            "exchange": "",
            "price": None,
            "pct_change": None,
            "vol_session": None,
            "source": src,  # anthropic | xai — desk shows A / X
            "source_mark": SOURCE_MARK.get(src, "?"),
        })
    return out


def _parse_ranked_prose(text: str,
                        *,
                        source: str | None = SOURCE_ANTHROPIC,
                        ) -> list[dict[str, Any]]:
    """Fallback when the model forgets the JSON trailer.

    Only matches **bold tickers** on ranked lines (e.g. ``1. **MU**: …``).
    Plain single letters from markdown (D/F/M in bold formatting noise) are
    rejected by requiring at least 2 characters inside ``**…**``.
    """
    pat = re.compile(
        r"(?m)^\s*(?:\d+[\.\)]\s*|\-\s+)"
        r"\*\*([A-Z]{2,5}(?:\.[A-Z])?)\*\*"
    )
    seen: set[str] = set()
    found: list[str] = []
    for m in pat.finditer(text):
        sym = m.group(1).upper()
        if not _TICKER_RE.match(sym) or len(sym) > 6 or sym in seen:
            continue
        if sym in {"AI", "US", "CEO", "CFO", "ETF", "IPO", "FED", "GDP",
                   "PPA", "PEG", "EPS", "FCF", "HBM", "GPU", "SMR"}:
            continue
        seen.add(sym)
        found.append(sym)
        if len(found) >= 10:
            break
    if not found:
        return []
    return parse_suggestions({
        "suggestions": [
            {"symbol": s, "score": max(5.0, 9.0 - 0.5 * i),
             "reason": "from research ranking"}
            for i, s in enumerate(found)
        ]
    }, source=source)


def parse_model_text(text: str,
                     *,
                     source: str | None = SOURCE_ANTHROPIC,
                     ) -> list[dict[str, Any]]:
    try:
        rows = parse_suggestions(
            _best_suggestions_payload(text), source=source)
        if rows:
            return rows
    except Exception:
        pass
    return _parse_ranked_prose(text, source=source)


def ensure_json_trailer(
    research_text: str,
    *,
    model: str = DEFAULT_XAI_MODEL,
    key: str | None = None,
    timeout: float = 90.0,
) -> str:
    """If research prose has no trailer, ask once more for JSON only (no tools)."""
    if parse_model_text(research_text):
        # Already have rows (JSON or prose fallback) — still try to attach
        # proper JSON when missing so the report is machine-readable.
        try:
            parse_suggestions(_best_suggestions_payload(research_text))
            return research_text  # real JSON present
        except Exception:
            pass

    key = (key if key is not None else api_key()).strip()
    if not key:
        return research_text

    # Only send the tail of the research to keep this call cheap.
    tail = research_text[-6000:] if len(research_text) > 6000 else research_text
    payload = {
        "model": model,
        "instructions": (
            "Extract the ranked US equity ideas from the research. "
            "Respond with ONLY a JSON object, no markdown, no prose. Schema: "
            '{"suggestions":[{"symbol":"TICKER","score":0-10,"reason":"short why"}]}'
        ),
        "input": [
            {
                "role": "user",
                "content": (
                    "Convert this research ranking into the JSON trailer "
                    "(top 5–7 only):\n\n" + tail
                ),
            }
        ],
        "temperature": 0.1,
    }
    try:
        data = _post_responses(payload, key=key, timeout=timeout, retries=2)
        trailer = _extract_responses_text(data).strip()
        if not trailer:
            return research_text
        # Validate before attaching.
        rows = parse_model_text(trailer)
        if not rows:
            return research_text
        return research_text.rstrip() + "\n\n" + trailer + "\n"
    except Exception:
        return research_text


def save_report(text: str, when: float | None = None) -> Path | None:
    """Persist the full prose report (panel only shows the JSON trailer)."""
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.fromtimestamp(
            time.time() if when is None else when
        ).strftime("%Y%m%d_%H%M%S")
        path = REPORT_DIR / f"claude_research_{ts}.md"
        path.write_text(text, encoding="utf-8")
        # Latest pointer for quick open.
        (REPORT_DIR / "latest.md").write_text(text, encoding="utf-8")
        return path
    except Exception:
        return None


def _extract_responses_text(data: dict) -> str:
    """Pull final assistant text from a /v1/responses payload.

    Output items are heterogeneous (reasoning, tool calls, message). We join
    every ``output_text`` chunk from completed message items, in order.
    """
    # Stream path stashes joined deltas when useful.
    st = data.get("_stream_text")
    if isinstance(st, str) and st.strip():
        return st.strip()

    # Convenience fields some SDKs mirror — prefer if present and non-empty.
    for key in ("output_text", "text"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, dict):
            # text.format shape is not the body; skip
            pass

    chunks: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        # Skip pure reasoning / tool-call scaffolding; keep message content.
        itype = item.get("type")
        if itype not in (None, "message"):
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type") or ""
            if ptype not in ("output_text", "text") and "text" not in part:
                continue
            t = part.get("text")
            if isinstance(t, str) and t.strip():
                chunks.append(t)
            elif isinstance(t, dict) and isinstance(t.get("value"), str):
                chunks.append(t["value"])
    text = "\n".join(chunks).strip()
    if not text:
        raise RuntimeError("xAI responses payload had no assistant text")
    return text


def _is_transient_network_error(exc: BaseException) -> bool:
    """Connection drops mid-agent-run are common on long web/X tool sessions."""
    if isinstance(exc, (TimeoutError, ConnectionResetError,
                        http.client.RemoteDisconnected,
                        http.client.IncompleteRead)):
        return True
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, ConnectionResetError,
                               http.client.RemoteDisconnected)):
            return True
        msg = str(reason).lower()
        if any(s in msg for s in (
            "remote end closed",
            "connection reset",
            "connection aborted",
            "timed out",
            "temporarily unavailable",
            "broken pipe",
            "eof occurred",
        )):
            return True
    msg = str(exc).lower()
    return any(s in msg for s in (
        "remote end closed",
        "connection reset",
        "incomplete read",
        "timed out",
    ))


def _friendly_network_error(exc: BaseException) -> str:
    """Short desk-safe message (long urllib dumps break the panel layout)."""
    msg = str(getattr(exc, "reason", None) or exc)
    low = msg.lower()
    if "remote end closed" in low or isinstance(exc, http.client.RemoteDisconnected):
        return "xAI dropped the connection mid-research (retrying next poll)"
    if "timed out" in low or isinstance(exc, TimeoutError):
        return "xAI request timed out (research + search is slow — will retry)"
    if "connection reset" in low:
        return "xAI connection reset (will retry next poll)"
    # Cap length so the empty-state row doesn't explode the table.
    return f"xAI network: {msg[:80]}"


def _post_responses_stream(
    payload: dict[str, Any],
    *,
    key: str,
    timeout: float,
) -> dict:
    """Agentic /v1/responses via SSE stream (httpx).

    xAI recommends streaming for server-side tools. Non-streaming long
    research runs regularly die with RemoteDisconnected after ~3 minutes;
    streaming keeps the socket alive with deltas and returns the full
    ``response.completed`` object.
    """
    try:
        import httpx
    except ImportError as e:
        raise RuntimeError(
            "httpx required for Grok research streaming — pip install httpx"
        ) from e

    body = {**payload, "stream": True}
    text_parts: list[str] = []
    completed: dict[str, Any] | None = None
    last_event = ""

    timeout_cfg = httpx.Timeout(timeout, connect=30.0, write=60.0, pool=30.0)
    with httpx.Client(timeout=timeout_cfg) as client:
        with client.stream(
            "POST",
            XAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "User-Agent": "BrasfieldMomentum/1.0",
            },
            json=body,
        ) as resp:
            if resp.status_code >= 400:
                err_body = ""
                try:
                    err_body = resp.read().decode("utf-8", errors="replace")[:400]
                except Exception:
                    pass
                raise RuntimeError(
                    f"xAI HTTP {resp.status_code}: {err_body or resp.reason_phrase}"
                )
            for line in resp.iter_lines():
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data: "):
                    continue
                data_s = line[6:].strip()
                if data_s == "[DONE]":
                    break
                try:
                    ev = json.loads(data_s)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                et = str(ev.get("type") or "")
                last_event = et
                if et == "response.output_text.delta":
                    delta = ev.get("delta")
                    if isinstance(delta, str):
                        text_parts.append(delta)
                elif et == "response.completed":
                    r = ev.get("response")
                    if isinstance(r, dict):
                        completed = r
                elif et == "error":
                    raise RuntimeError(
                        f"xAI stream error: {ev.get('error') or ev}"[:200]
                    )

    if completed is not None:
        # Ensure extractors can find text even if output shape is odd.
        if text_parts and not completed.get("output_text"):
            completed = dict(completed)
            completed["_stream_text"] = "".join(text_parts)
        return completed

    # No completed event — fall back to joined deltas.
    text = "".join(text_parts).strip()
    if text:
        return {
            "id": None,
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }],
            "_stream_text": text,
            "_last_event": last_event,
        }
    raise RuntimeError(
        f"xAI stream ended without text (last_event={last_event or 'none'})"
    )


def _post_responses(
    payload: dict[str, Any],
    *,
    key: str,
    timeout: float,
    retries: int = 3,
) -> dict:
    """POST /v1/responses.

    * With tools (agentic) → **streamed** via httpx (required for reliability).
    * Without tools → simple non-stream urllib with retries.
    """
    # Agentic tool calls must stream — non-stream drops mid-research.
    if payload.get("tools"):
        last_err: BaseException | None = None
        attempts = max(1, int(retries))
        for attempt in range(attempts):
            try:
                return _post_responses_stream(payload, key=key, timeout=timeout)
            except Exception as e:  # noqa: BLE001
                last_err = e
                if _is_transient_network_error(e) and attempt + 1 < attempts:
                    time.sleep(min(20.0, 2.0 ** attempt * 1.5))
                    continue
                if _is_transient_network_error(e):
                    raise RuntimeError(_friendly_network_error(e)) from e
                raise
        if last_err is not None:
            raise RuntimeError(_friendly_network_error(last_err)) from last_err
        raise RuntimeError("xAI stream failed")

    body = json.dumps({**payload, "stream": False}).encode("utf-8")
    last_err = None
    attempts = max(1, int(retries))

    for attempt in range(attempts):
        req = Request(
            XAI_RESPONSES_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "BrasfieldMomentum/1.0",
                "Connection": "close",
            },
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise RuntimeError("xAI response was not a JSON object")
            return data
        except HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                last_err = e
                time.sleep(min(30.0, 2.0 ** attempt * 1.5))
                continue
            raise RuntimeError(
                f"xAI HTTP {e.code}: {(detail or e.reason)[:160]}"
            ) from e
        except Exception as e:  # noqa: BLE001
            last_err = e
            if _is_transient_network_error(e) and attempt + 1 < attempts:
                time.sleep(min(30.0, 2.0 ** attempt * 1.5))
                continue
            if _is_transient_network_error(e):
                raise RuntimeError(_friendly_network_error(e)) from e
            if isinstance(e, URLError):
                raise RuntimeError(_friendly_network_error(e)) from e
            raise

    if last_err is not None:
        if _is_transient_network_error(last_err):
            raise RuntimeError(_friendly_network_error(last_err)) from last_err
        raise RuntimeError(str(last_err)[:160]) from last_err
    raise RuntimeError("xAI request failed with no error detail")


def _function_calls(data: dict) -> list[dict[str, Any]]:
    """Extract client-side function_call items from a responses payload."""
    out: list[dict[str, Any]] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype in ("function_call", "tool_call", "custom_tool_call"):
            out.append(item)
            continue
        # Some shapes nest under tool_calls on a message.
        for tc in item.get("tool_calls") or []:
            if isinstance(tc, dict):
                out.append(tc)
    return out


def _fc_name(item: dict) -> str:
    if item.get("name"):
        return str(item["name"])
    fn = item.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return ""


def _fc_args(item: dict) -> Any:
    if "arguments" in item:
        return item.get("arguments")
    fn = item.get("function")
    if isinstance(fn, dict):
        return fn.get("arguments")
    return {}


def _fc_call_id(item: dict) -> str:
    return str(item.get("call_id") or item.get("id") or "")


def _run_tool_loop(
    data: dict,
    *,
    key: str,
    model: str,
    timeout: float,
    tools: list[dict[str, Any]],
    trade_exec,
    max_tool_rounds: int,
) -> dict:
    """Resolve client-side function_call items until the model stops calling."""
    response_id = data.get("id")
    for _ in range(max(0, int(max_tool_rounds))):
        status = data.get("status")
        if status and status not in ("completed", "incomplete", "requires_action"):
            err = data.get("error") or status
            raise RuntimeError(f"xAI response status={status}: {err}")

        fcs = _function_calls(data)
        if not fcs or trade_exec is None:
            break

        outputs: list[dict[str, Any]] = []
        for fc in fcs:
            name = _fc_name(fc)
            call_id = _fc_call_id(fc)
            if not name or not call_id:
                continue
            try:
                result = trade_exec(name, _fc_args(fc))
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error": str(e)[:200]}
            outputs.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result),
            })
        if not outputs:
            break

        follow: dict[str, Any] = {
            "model": model,
            "input": outputs,
            "previous_response_id": response_id,
        }
        if tools:
            follow["tools"] = tools
        data = _post_responses(follow, key=key, timeout=timeout)
        response_id = data.get("id") or response_id
    return data


def _research_tools(live_search: bool, search_tools: str) -> list[dict[str, Any]]:
    """Which server-side search tools to attach (each call is billed)."""
    if not live_search:
        return []
    mode = (search_tools or DEFAULT_SEARCH_TOOLS).strip().lower()
    if mode in ("none", "off", "0", "false"):
        return []
    tools: list[dict[str, Any]] = [{"type": "web_search"}]
    if mode in ("web_x", "web+x", "both", "all"):
        tools.append({"type": "x_search"})
    return tools


def parse_research_times(times: Any) -> list[tuple[int, int]]:
    """Normalize ``["04:00", "8:30"]`` → ``[(4, 0), (8, 30)]``, sorted.

    Accepts a comma-separated string too, so the value can come from either
    JSON config or an env var. Unparseable entries are dropped rather than
    raising — a typo should not take the panel down.
    """
    if isinstance(times, str):
        times = [t for t in re.split(r"[,\s]+", times) if t]
    out: set[tuple[int, int]] = set()
    for raw in times or []:
        m = re.fullmatch(r"\s*(\d{1,2})\s*:\s*(\d{2})\s*", str(raw))
        if not m:
            continue
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            out.add((hh, mm))
    return sorted(out)


def due_slot(
    now: float,
    *,
    times: list[tuple[int, int]],
    weekdays_only: bool = DEFAULT_RESEARCH_WEEKDAYS_ONLY,
    catchup_min: int = DEFAULT_RESEARCH_CATCHUP_MIN,
    last_slot: str = "",
) -> str | None:
    """Key of the scheduled run that is due now, or None.

    Returns the *latest* qualifying slot so a desk that was down through two
    slots runs once on current data rather than replaying the backlog.
    """
    et = datetime.fromtimestamp(now, ET)
    if weekdays_only and et.weekday() >= 5:
        return None
    best: str | None = None
    for hh, mm in times:
        slot = et.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if slot > et:
            continue
        if (et - slot).total_seconds() / 60.0 > max(0, int(catchup_min)):
            continue
        best = slot.strftime("%Y-%m-%dT%H:%M")
    # Keys are ISO-ordered, so a string compare also guards a clock that
    # jumped backwards.
    if best is None or best <= (last_slot or ""):
        return None
    return best


def next_slot_label(
    now: float,
    *,
    times: list[tuple[int, int]],
    weekdays_only: bool = DEFAULT_RESEARCH_WEEKDAYS_ONLY,
) -> str:
    """``"Mon 04:00"``-style label for the next scheduled run (desk status)."""
    if not times:
        return ""
    et = datetime.fromtimestamp(now, ET)
    for day in range(0, 8):
        d = et + timedelta(days=day)
        if weekdays_only and d.weekday() >= 5:
            continue
        for hh, mm in times:
            slot = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if slot > et:
                return slot.strftime("%a %H:%M")
    return ""


def _cli_workspace() -> str:
    """Empty, stable working directory for the research CLI session.

    Running from the repo pulls CLAUDE.md, git status, and env info into the
    cached system prompt — ~900 tokens per call that stock research never
    reads, and it also hands a trading repo to an agent whose only job is to
    search the web. A fixed empty directory keeps the cached prefix
    byte-stable across polls.
    """
    ws = Path(tempfile.gettempdir()) / "claude_suggest_workspace"
    try:
        ws.mkdir(parents=True, exist_ok=True)
    except OSError:
        return str(ROOT)
    return str(ws)


def _record_usage(entry: dict[str, Any]) -> None:
    """Append one Claude CLI usage record; keep the latest in ``last_usage``."""
    last_usage.clear()
    last_usage.update(entry)
    try:
        TOKEN_METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with TOKEN_METRICS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def load_token_metrics(
    path: Path | None = None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Load rows from ``token_metrics.jsonl`` (oldest first).

    ``limit`` keeps only the last N rows (still returned oldest→newest).
    """
    p = path or TOKEN_METRICS_PATH
    rows: list[dict[str, Any]] = []
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    if limit is not None and limit > 0 and len(rows) > limit:
        rows = rows[-limit:]
    return rows


def latest_token_usage(path: Path | None = None) -> dict[str, Any]:
    """Most recent usage row, or {} if none."""
    rows = load_token_metrics(path, limit=1)
    return dict(rows[-1]) if rows else {}


def summarize_token_metrics(
    path: Path | None = None,
    *,
    day: str | None = "today",
    since_ts: float | None = None,
) -> dict[str, Any]:
    """Aggregate token_metrics.jsonl for a desk day or a since timestamp.

    ``day``:
      - ``\"today\"`` (default) — calendar day in America/New_York
      - ``\"YYYY-MM-DD\"`` — that ET calendar day
      - ``None`` / ``\"all\"`` — every row (optionally filtered by ``since_ts``)

    Safe for dashboard hot path: one file read, O(n) over a small JSONL.
    """
    rows = load_token_metrics(path)
    day_key = None if day in (None, "", "all") else str(day).strip().lower()
    if day_key == "today":
        day_key = datetime.now(ET).strftime("%Y-%m-%d")

    def _in_window(r: dict[str, Any]) -> bool:
        try:
            ts = float(r.get("ts") or 0)
        except (TypeError, ValueError):
            return False
        if since_ts is not None and ts < float(since_ts):
            return False
        if day_key:
            try:
                d = datetime.fromtimestamp(ts, tz=ET).strftime("%Y-%m-%d")
            except (OSError, OverflowError, ValueError):
                return False
            if d != day_key:
                return False
        return True

    filtered = [r for r in rows if _in_window(r)]
    by_phase: dict[str, dict[str, Any]] = {}
    by_backend: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    total_in = 0
    total_out = 0
    total_cache_read = 0
    total_cache_create = 0

    def _bump(bucket: dict[str, dict[str, Any]], key: str, r: dict[str, Any],
              cost: float, tin: int, tout: int) -> None:
        slot = bucket.setdefault(key, {
            "n": 0, "total_cost_usd": 0.0,
            "input_tokens": 0, "output_tokens": 0,
        })
        slot["n"] += 1
        slot["total_cost_usd"] = round(slot["total_cost_usd"] + cost, 6)
        slot["input_tokens"] += tin
        slot["output_tokens"] += tout

    for r in filtered:
        try:
            cost = float(r.get("total_cost_usd") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        try:
            tin = int(r.get("input_tokens") or 0)
        except (TypeError, ValueError):
            tin = 0
        try:
            tout = int(r.get("output_tokens") or 0)
        except (TypeError, ValueError):
            tout = 0
        try:
            tcr = int(r.get("cache_read_input_tokens") or 0)
        except (TypeError, ValueError):
            tcr = 0
        try:
            tcc = int(r.get("cache_creation_input_tokens") or 0)
        except (TypeError, ValueError):
            tcc = 0
        total_cost += cost
        total_in += tin
        total_out += tout
        total_cache_read += tcr
        total_cache_create += tcc
        phase = str(r.get("phase") or "unknown")
        backend = str(r.get("backend") or "unknown")
        _bump(by_phase, phase, r, cost, tin, tout)
        _bump(by_backend, backend, r, cost, tin, tout)

    last = dict(filtered[-1]) if filtered else latest_token_usage(path)
    # Compact last for API: drop huge optional fields if ever added.
    last_compact = {
        k: last[k]
        for k in (
            "ts", "backend", "phase", "model", "effort", "num_turns",
            "total_cost_usd", "input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens",
            "reasoning_tokens", "total_tokens", "duration_ms",
        )
        if k in last and last[k] is not None
    } if last else {}

    return {
        "day": day_key or "all",
        "path": str(path or TOKEN_METRICS_PATH),
        "count": len(filtered),
        "total_cost_usd": round(total_cost, 6),
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_input_tokens": total_cache_read,
        "cache_creation_input_tokens": total_cache_create,
        "by_phase": by_phase,
        "by_backend": by_backend,
        "last": last_compact,
    }


def call_claude_cli(
    prompt: str,
    *,
    model: str = DEFAULT_CLAUDE_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
    live_search: bool = True,
    cli_bin: str | None = None,
    effort: str | None = None,
    phase: str = "research",
) -> str:
    """Run research via Claude Code CLI headless (``claude -p``).

    Uses your Claude Code login / Anthropic plan (or ANTHROPIC_API_KEY).
    Not free unlimited — subscription/API usage still applies.

    Runs with ``--output-format json`` so per-call token usage and cost land
    in ``ai_reports/token_metrics.jsonl`` (latest also in ``last_usage``).
    """
    binary = resolve_claude_cli(cli_bin)
    if not binary:
        raise RuntimeError(
            "Claude CLI not found — install Claude Code "
            "(https://docs.anthropic.com/en/docs/claude-code) "
            "or set CLAUDE_CLI_BIN"
        )

    # Prompt goes as the -p argv value — macOS ARG_MAX is large (~256k–1M),
    # comfortably above the research prompt + prior-context snippet.
    model_id = model or DEFAULT_CLAUDE_MODEL
    # Don't pass Grok model ids to Claude.
    if str(model_id).lower().startswith("grok"):
        model_id = DEFAULT_CLAUDE_MODEL

    # Research only — no repo edits / shell / subagents.
    blocked = [
        "Edit", "Write", "MultiEdit", "NotebookEdit", "Bash",
        "Agent", "TodoWrite",
    ]
    if not live_search:
        blocked += ["WebSearch", "WebFetch"]

    cmd = [
        binary,
        "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--model", model_id,
        # Keeps cwd / env / git status out of the cached system prefix so it
        # stays byte-identical between polls.
        "--exclude-dynamic-system-prompt-sections",
        "--disallowedTools", *blocked,
    ]
    if live_search:
        # Allow web tools Claude Code exposes when available.
        cmd.extend(["--allowedTools", "WebSearch", "WebFetch"])
    if effort:
        cmd.extend(["--effort", str(effort)])

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(30.0, float(timeout)),
            env={**os.environ},
            cwd=_cli_workspace(),
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Claude CLI timed out after {timeout:.0f}s") from e

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 and not out:
        raise RuntimeError(
            f"Claude CLI exit {proc.returncode}: {(err or 'no output')[:240]}"
        )
    if not out:
        raise RuntimeError(
            f"Claude CLI returned empty output: {(err or 'no stderr')[:240]}"
        )

    # --output-format json → single result envelope with usage + cost.
    # Older CLIs (or a non-JSON stdout) fall back to treating out as the text.
    envelope: dict[str, Any] | None = None
    try:
        maybe = json.loads(out)
        if isinstance(maybe, dict) and "result" in maybe:
            envelope = maybe
    except json.JSONDecodeError:
        envelope = None

    if envelope is None:
        if claude_output_looks_logged_out(out) or claude_output_looks_logged_out(err):
            raise RuntimeError(
                "Claude CLI not logged in — run: claude /login "
                "(on the machine running ai_trader)"
            )
        return out

    text = str(envelope.get("result") or "")
    if claude_output_looks_logged_out(text) or claude_output_looks_logged_out(err):
        raise RuntimeError(
            "Claude CLI not logged in — run: claude /login "
            "(on the machine running ai_trader)"
        )
    usage = envelope.get("usage") or {}
    _record_usage({
        "ts": round(started, 3),
        "backend": "claude_cli",        "phase": phase,
        "model": model_id,
        "effort": effort or "",
        "num_turns": envelope.get("num_turns"),
        "duration_ms": envelope.get("duration_ms"),
        "duration_api_ms": envelope.get("duration_api_ms"),
        "total_cost_usd": envelope.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "prompt_chars": len(prompt),
        "result_chars": len(text),
        "session_id": envelope.get("session_id"),
    })
    if not text.strip():
        raise RuntimeError(
            "Claude CLI returned empty result"
            + (f" ({envelope.get('subtype')})" if envelope.get("is_error") else "")
        )
    return text


def call_agy_cli(
    prompt: str,
    *,
    model: str = DEFAULT_AGY_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
    live_search: bool = True,
    cli_bin: str | None = None,
    effort: str | None = None,
    phase: str = "research",
) -> str:
    """Run research via Antigravity CLI headless (``agy -p``).

    Uses the Google / Gemini subscription login (Keychain on macOS). Same
    session rule as Claude Code: a stack started over SSH cannot read it.

    Does **not** pass ``--dangerously-skip-permissions``. Research runs from
    an empty temp workspace so a tool that would write the trading repo is
    outside the trusted tree and soft-denied in headless mode. Web fetch is
    also Ask-by-default; allow ``read_url(*)`` in
    ``~/.gemini/antigravity-cli/settings.json`` if the run must search live.
    ``live_search`` is accepted for call-shape parity; agy has no Claude-style
    ``--disallowedTools`` switch.
    """
    binary = resolve_agy_cli(cli_bin)
    if not binary:
        raise RuntimeError(
            "Antigravity CLI not found — install agy "
            "(https://antigravity.google/docs/cli/install) "
            "or set AGY_CLI_BIN"
        )

    model_id = _agy_model(model)
    effort_id = _agy_effort(effort)
    cmd = [
        binary,
        "-p", prompt,
        "--output-format", "json",
        "--print-timeout", _agy_print_timeout(timeout),
        "--disable-slash-commands",
        "--model", model_id,
    ]
    if effort_id:
        cmd.extend(["--effort", effort_id])
    _ = live_search  # no CLI flag; permissions.allow read_url is the gate

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(30.0, float(timeout)),
            env={**os.environ},
            cwd=_cli_workspace(),
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Antigravity CLI timed out after {timeout:.0f}s") from e

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if agy_output_looks_logged_out(out) or agy_output_looks_logged_out(err):
        raise RuntimeError(
            "Antigravity CLI not logged in — run: agy   "
            "(on the machine running ai_trader, from a local Terminal)"
        )
    if proc.returncode != 0 and not out:
        raise RuntimeError(
            f"Antigravity CLI exit {proc.returncode}: {(err or 'no output')[:240]}"
        )
    if not out:
        raise RuntimeError(
            f"Antigravity CLI returned empty output: {(err or 'no stderr')[:240]}"
        )

    envelope: dict[str, Any] | None = None
    try:
        maybe = json.loads(out)
        if isinstance(maybe, dict) and (
            "response" in maybe or "status" in maybe or "usage" in maybe
        ):
            envelope = maybe
    except json.JSONDecodeError:
        envelope = None

    if envelope is None:
        return out

    status = str(envelope.get("status") or "").strip().upper()
    text = str(envelope.get("response") or envelope.get("result") or "")
    if status and status != "SUCCESS":
        err_msg = str(envelope.get("error") or status)
        if agy_output_looks_logged_out(err_msg):
            raise RuntimeError(
                "Antigravity CLI not logged in — run: agy   "
                "(on the machine running ai_trader, from a local Terminal)"
            )
        raise RuntimeError(f"Antigravity CLI {status}: {err_msg[:240]}")

    usage = envelope.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    duration_ms = None
    try:
        if envelope.get("duration_seconds") is not None:
            duration_ms = int(round(float(envelope["duration_seconds"]) * 1000))
        else:
            duration_ms = int(round((time.time() - started) * 1000))
    except (TypeError, ValueError):
        duration_ms = None
    _record_usage({
        "ts": round(started, 3),
        "backend": "agy",
        "phase": phase,
        "model": model_id,
        "effort": effort_id or "",
        "num_turns": envelope.get("num_turns"),
        "duration_ms": duration_ms,
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "thinking_tokens": usage.get("thinking_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_tokens")
        or usage.get("cache_read_input_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "prompt_chars": len(prompt),
        "result_chars": len(text),
        "session_id": envelope.get("conversation_id"),
    })
    if not text.strip():
        raise RuntimeError("Antigravity CLI returned empty response")
    return text


def call_grok_cli(
    prompt: str,
    *,
    model: str = DEFAULT_XAI_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
    max_turns: int | None = DEFAULT_MAX_TURNS,
    live_search: bool = True,
    cli_bin: str | None = None,
    phase: str = "research",
) -> str:
    """Run the research prompt via Grok Build CLI headless mode.

    Uses your ``grok login`` / grok.com session (see ~/.grok/auth.json).
    Does **not** require ``XAI_API_KEY`` (console API credits). Subscription /
    free-trial limits of the CLI account still apply — this is not unlimited
    free compute, just a different billing/auth path than the developer API.

    Runs with ``--output-format json`` so per-call token usage and cost land
    in ``ai_reports/token_metrics.jsonl`` (latest also in ``last_usage``),
    same path as the Claude CLI metrics.
    """
    binary = resolve_grok_cli(cli_bin)
    if not binary:
        raise RuntimeError(
            "Grok CLI not found — install from https://x.ai/cli "
            "or set GROK_CLI_BIN to the grok binary"
        )
    if not cli_logged_in():
        raise RuntimeError(
            "Grok CLI not logged in — run: grok login   "
            "(subscription only; do not set XAI_API_KEY for research)"
        )

    model_id = model or DEFAULT_XAI_MODEL
    # Write prompt to a temp file so long research text is sent verbatim.
    fd, tmp_path = tempfile.mkstemp(prefix="grok_prompt_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(prompt)

        cmd = [
            binary,
            "--prompt-file", tmp_path,
            # JSON envelope carries text + usage + total_cost_usd (see metrics).
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
            # Stock research must not edit the repo or spawn subagents.
            "--disallowed-tools",
            "Agent,run_terminal_cmd,search_replace,write,Write,Edit,Bash",
            "-m", model_id,
        ]
        if max_turns is not None and int(max_turns) > 0:
            cmd.extend(["--max-turns", str(int(max_turns))])
        if not live_search:
            cmd.append("--disable-web-search")

        # Subscription path only: never pass console API keys into the Grok CLI
        # subprocess. XAI_API_KEY / GROK_API_KEY bill usage outside SuperGrok.
        env = {**os.environ}
        env.pop("XAI_API_KEY", None)
        env.pop("GROK_API_KEY", None)

        started = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(30.0, float(timeout)),
                env=env,
                cwd=str(ROOT),
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"Grok CLI timed out after {timeout:.0f}s"
            ) from e

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 and not out:
            raise RuntimeError(
                f"Grok CLI exit {proc.returncode}: {(err or 'no output')[:200]}"
            )
        if not out:
            raise RuntimeError(
                f"Grok CLI returned empty output: {(err or 'no stderr')[:200]}"
            )

        # --output-format json → {text, usage, total_cost_usd, num_turns, ...}
        # Fall back to treating stdout as plain research text if not JSON.
        envelope: dict[str, Any] | None = None
        try:
            maybe = json.loads(out)
            if isinstance(maybe, dict) and (
                "text" in maybe or "result" in maybe or "usage" in maybe
            ):
                envelope = maybe
        except json.JSONDecodeError:
            envelope = None

        if envelope is None:
            return out

        text = str(envelope.get("text") or envelope.get("result") or "")
        usage = envelope.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        duration_ms = None
        try:
            duration_ms = int(round((time.time() - started) * 1000))
        except Exception:
            pass
        _record_usage({
            "ts": round(started, 3),
            "backend": "grok_cli",
            "phase": phase,
            "model": model_id,
            "effort": "",
            "num_turns": envelope.get("num_turns"),
            "duration_ms": duration_ms,
            "duration_api_ms": envelope.get("duration_ms") or envelope.get("duration_api_ms"),
            "total_cost_usd": envelope.get("total_cost_usd"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "prompt_chars": len(prompt),
            "result_chars": len(text),
            "session_id": envelope.get("sessionId") or envelope.get("session_id"),
            "request_id": envelope.get("requestId") or envelope.get("request_id"),
            "stop_reason": envelope.get("stopReason") or envelope.get("stop_reason"),
        })
        if not text.strip():
            raise RuntimeError(
                "Grok CLI returned empty text"
                + (f" (stop={envelope.get('stopReason')})" if envelope.get("stopReason") else "")
            )
        return text
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _prior_context_snippet(max_chars: int = 1200) -> str:
    """Optional prior-run snapshot so the model can update, not re-derive everything."""
    latest = REPORT_DIR / "latest.md"
    if not latest.is_file():
        return ""
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    rows = parse_model_text(text)
    if not rows:
        # fall back to a short tail
        tail = text[-max_chars:].strip()
        return f"\n\nPRIOR RUN (context only — re-validate with fresh data):\n{tail}\n"
    lines = [
        "PRIOR RUN top ideas (context only — re-validate with FRESH web/X "
        "search this run; drop if thesis broken or data stale):"
    ]
    for r in rows[:7]:
        lines.append(
            f"- {r.get('symbol')} score={r.get('trending_score')} "
            f"{r.get('reason') or ''}"
        )
    return "\n\n" + "\n".join(lines) + "\n"


# Desk-native snapshot files (hints for research — not a buy list).
RS_RATINGS_FILE = ROOT / "rs_ratings.json"
TRENDING_STOCKS_FILE = ROOT / "trending_stocks.json"
SIGNAL_STATE_FILE = ROOT / "signal_state.json"
DEFAULT_DESK_SNAPSHOT_RS_N = 12
DEFAULT_DESK_SNAPSHOT_TREND_N = 12
DEFAULT_DESK_SNAPSHOT_MOMENTUM_N = 12
DEFAULT_DESK_SNAPSHOT_PEER_N = 7
DEFAULT_DESK_SNAPSHOT_MAX_CHARS = 2200


def _fmt_px(price: Any) -> str:
    try:
        p = float(price)
    except (TypeError, ValueError):
        return "?"
    if p >= 1000:
        return f"{p:.0f}"
    if p >= 100:
        return f"{p:.1f}"
    return f"{p:.2f}"


def _fmt_pct(val: Any) -> str:
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "?"
    # Accept either fraction (0.05) or percent (5.0).
    if abs(v) <= 1.5:
        v *= 100.0
    return f"{v:+.1f}%"


def _fmt_score(val: Any) -> str:
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "?"
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.1f}"


def _under_price_cap(price: Any, max_price: float | None) -> bool:
    if max_price is None or float(max_price) <= 0:
        return True
    try:
        return float(price) < float(max_price)
    except (TypeError, ValueError):
        return False


def _load_json_file(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _rs_leader_lines(
    path: Path = RS_RATINGS_FILE,
    *,
    max_price: float | None = 100.0,
    limit: int = DEFAULT_DESK_SNAPSHOT_RS_N,
    min_rs: float = 80.0,
) -> tuple[list[str], str]:
    """Compact RS board lines + as_of label."""
    data = _load_json_file(path)
    if not isinstance(data, dict):
        return [], ""
    rows = data.get("rows") or []
    if not isinstance(rows, list):
        return [], ""
    as_of = str(data.get("as_of") or data.get("updated") or "")
    scored: list[tuple[float, str]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("ticker") or r.get("symbol") or "").upper().strip()
        if not sym or not _TICKER_RE.match(sym):
            continue
        if not _under_price_cap(r.get("price"), max_price):
            continue
        try:
            rs = float(r.get("rs_rating") if r.get("rs_rating") is not None
                       else r.get("rs_percentile", 0) * 100.0)
        except (TypeError, ValueError):
            continue
        if rs < float(min_rs):
            continue
        px = _fmt_px(r.get("price"))
        r1 = _fmt_pct(r.get("ret_1m"))
        r3 = _fmt_pct(r.get("ret_3m"))
        scored.append((rs, f"{sym} rs={_fmt_score(rs)} px={px} 1m={r1} 3m={r3}"))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [line for _, line in scored[: max(1, int(limit))]], as_of


def _trending_heat_lines(
    path: Path = TRENDING_STOCKS_FILE,
    *,
    max_price: float | None = 100.0,
    limit: int = DEFAULT_DESK_SNAPSHOT_TREND_N,
) -> list[str]:
    data = _load_json_file(path)
    if not isinstance(data, dict):
        return []
    rows = data.get("rows") or []
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if r.get("is_crypto") is True:
            continue
        if r.get("is_equity") is False:
            continue
        sym = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
        if not sym or not _TICKER_RE.match(sym):
            continue
        if not _under_price_cap(r.get("price"), max_price):
            continue
        score = r.get("trending_score", r.get("score"))
        chg = _fmt_pct(r.get("pct_change"))
        rvol = r.get("rvol")
        rvol_s = _fmt_score(rvol) if rvol is not None else "?"
        out.append(
            f"{sym} score={_fmt_score(score)} chg={chg} rvol={rvol_s} "
            f"px={_fmt_px(r.get('price'))}"
        )
        if len(out) >= max(1, int(limit)):
            break
    return out


def _momentum_desk_lines(
    path: Path = SIGNAL_STATE_FILE,
    *,
    max_price: float | None = 100.0,
    limit: int = DEFAULT_DESK_SNAPSHOT_MOMENTUM_N,
) -> list[str]:
    """Active signal-engine / momentum-monitor names for research prompts."""
    data = _load_json_file(path)
    if not isinstance(data, dict):
        return []
    tickers = data.get("tickers") or data.get("active") or {}
    if not isinstance(tickers, dict):
        return []
    scored: list[tuple[float, str]] = []
    for sym, meta in tickers.items():
        s = str(sym or "").upper().strip()
        if not s or not _TICKER_RE.match(s):
            continue
        if not isinstance(meta, dict):
            meta = {}
        px = meta.get("price")
        if px is not None and not _under_price_cap(px, max_price):
            continue
        # Prefer hot / high-proximity names at the top of the snapshot.
        hot = 1.0 if meta.get("is_hot") else 0.0
        try:
            prox = float(meta.get("proximity_pct") or 0.0)
        except (TypeError, ValueError):
            prox = 0.0
        rank = hot * 1000.0 + prox
        status = str(meta.get("status") or "").strip()
        bit = f"{s} px={_fmt_px(px)}"
        if hot:
            bit += " HOT"
        if status:
            bit += f" {status}"
        if prox:
            bit += f" prox={prox:g}%"
        scored.append((rank, bit))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [line for _, line in scored[: max(1, int(limit))]]


def momentum_desk_candidate_rows(
    *,
    path: Path = SIGNAL_STATE_FILE,
    max_n: int = 12,
    max_price: float | None = 100.0,
) -> list[dict]:
    """Synthetic book rows from the live momentum / signal-engine table."""
    data = _load_json_file(path)
    if not isinstance(data, dict):
        return []
    tickers = data.get("tickers") or data.get("active") or {}
    if not isinstance(tickers, dict):
        return []
    scored: list[tuple[float, dict]] = []
    for sym, meta in tickers.items():
        s = str(sym or "").upper().strip()
        if not s or not _TICKER_RE.match(s):
            continue
        if not isinstance(meta, dict):
            meta = {}
        px = meta.get("price")
        if px is not None and not _under_price_cap(px, max_price):
            continue
        hot = bool(meta.get("is_hot"))
        try:
            prox = float(meta.get("proximity_pct") or 0.0)
        except (TypeError, ValueError):
            prox = 0.0
        # Map desk heat into a 6.5–8.5 research-ish score band for ranking only.
        score = 6.5 + min(2.0, (1.0 if hot else 0.0) + prox / 50.0)
        reason = "momentum desk"
        if hot:
            reason = "momentum HOT"
        elif meta.get("status"):
            reason = f"momentum {meta.get('status')}"
        scored.append((score, {
            "symbol": s,
            "trending_score": round(score, 2),
            "score": round(score, 2),
            "reason": reason[:40],
            "agreement": True,
            "source": "momentum",
            "price": px,
        }))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [row for _, row in scored[: max(1, int(max_n))]]


def trending_desk_candidate_rows(
    *,
    path: Path = TRENDING_STOCKS_FILE,
    max_n: int = 8,
    max_price: float | None = 100.0,
) -> list[dict]:
    """Synthetic book rows from Stocktwits heat (under price cap)."""
    data = _load_json_file(path)
    if not isinstance(data, dict):
        return []
    raw = data.get("rows") or []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        if r.get("is_crypto") is True:
            continue
        if r.get("is_equity") is False:
            continue
        sym = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
        if not sym or not _TICKER_RE.match(sym):
            continue
        if not _under_price_cap(r.get("price"), max_price):
            continue
        try:
            score = float(r.get("trending_score", r.get("score") or 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        # Normalize ST heat scores into a 6–8.5 band when they look like raw heat.
        if score > 10:
            score = 6.0 + min(2.5, score / 20.0)
        out.append({
            "symbol": sym,
            "trending_score": round(score, 2),
            "score": round(score, 2),
            "reason": "trending heat",
            "agreement": True,
            "source": "trending",
            "price": r.get("price"),
        })
        if len(out) >= max(1, int(max_n)):
            break
    return out


def _peer_board_lines(
    path: Path,
    *,
    max_price: float | None = 100.0,
    limit: int = DEFAULT_DESK_SNAPSHOT_PEER_N,
) -> list[str]:
    data = _load_json_file(path)
    if not isinstance(data, dict):
        return []
    rows = data.get("rows") or data.get("suggestions") or data.get("items") or []
    if not isinstance(rows, list):
        return []
    out: list[str] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
        if not sym or not _TICKER_RE.match(sym):
            continue
        px = r.get("price")
        if px is not None and not _under_price_cap(px, max_price):
            continue
        score = r.get("trending_score", r.get("score"))
        reason = str(r.get("reason") or "").strip()[:40]
        summary = str(r.get("summary") or "").strip()[:80]
        bit = f"{sym} score={_fmt_score(score)}"
        if reason:
            bit += f" · {reason}"
        if summary and summary != reason:
            bit += f" · {summary}"
        out.append(bit)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _peer_suggestions_path(backend: str | None) -> tuple[Path, str]:
    """Other research wire for this backend + short label."""
    src = source_from_backend(backend)
    if src == SOURCE_XAI:
        return CLAUDE_SUGGESTIONS_FILE, "AGY (G)"
    return GROK_SUGGESTIONS_FILE, "Grok (X)"


def build_rival_duel_snippet(
    *,
    backend: str | None = None,
    max_price: float | None = 100.0,
    peer_path: Path | None = None,
    peer_n: int = 8,
    max_chars: int = 1800,
) -> str:
    """Rival AI board + duel champion for competitive research.

    Injected only when ``ai_duel_enabled`` is true so each model can see the
    other side's top ideas and either beat them on session realized-R or pass.
    When duel is off, returns empty (peer heat still appears in desk snapshot).
    """
    try:
        import ai_duel as duel
        from config import load_config
        if not duel.duel_enabled(load_config()):
            return ""
    except Exception:
        # Fail closed: no competition inject if config/duel unavailable.
        return ""

    p_path, p_label = _peer_suggestions_path(backend)
    if peer_path is not None:
        p_path = peer_path
    cap = float(max_price) if max_price is not None and float(max_price) > 0 else None
    lines: list[str] = []

    # Live duel state (today's registered champions / scores).
    try:
        import ai_duel as duel
        snap = duel.public_snapshot(None)
        if snap.get("enabled"):
            my_src = source_from_backend(backend)
            rival_key = "anthropic" if my_src == SOURCE_XAI else "xai"
            champs = snap.get("champions") or {}
            rival = champs.get(rival_key) if isinstance(champs, dict) else None
            if isinstance(rival, dict) and rival.get("symbol"):
                r_bits = [
                    f"CHAMPION {rival.get('symbol')}",
                    f"mark={rival.get('source_mark') or '?'}",
                    f"status={rival.get('status') or 'watching'}",
                ]
                if rival.get("score") is not None:
                    r_bits.append(f"score={_fmt_score(rival.get('score'))}")
                if rival.get("reason"):
                    r_bits.append(str(rival.get("reason"))[:50])
                if rival.get("realized_r") is not None:
                    try:
                        r_bits.append(f"realized_R={float(rival['realized_r']):+.2f}")
                    except (TypeError, ValueError):
                        pass
                lines.append("Rival duel champion (beat this on session R): " + " · ".join(r_bits))
            sc = snap.get("score") or {}
            if isinstance(sc, dict) and sc:
                bits = []
                for k, label in (("agy", "G"), ("anthropic", "G"), ("xai", "X")):
                    row = sc.get(k) if isinstance(sc.get(k), dict) else None
                    if not row:
                        continue
                    try:
                        rr = row.get("realized_r")
                        bits.append(
                            f"{label}:{row.get('symbol') or '—'} "
                            f"R={float(rr):+.2f}" if rr is not None
                            else f"{label}:{row.get('symbol') or '—'}"
                        )
                    except (TypeError, ValueError):
                        bits.append(f"{label}:{row.get('symbol') or '—'}")
                if bits:
                    lines.append(
                        f"Duel scoreboard phase={snap.get('phase')} "
                        f"winner={snap.get('winner') or 'none'}: "
                        + " | ".join(bits)
                    )
            lines.append(
                f"Duel clock: trial_end={snap.get('trial_end')} "
                f"chance3={snap.get('chance3_start')} ET"
            )
    except Exception:
        pass

    peer = _peer_board_lines(p_path, max_price=cap, limit=peer_n)
    if peer:
        lines.append(
            f"Rival full board — {p_label} (latest research publish, best-first):\n  "
            + "\n  ".join(peer)
        )
    else:
        lines.append(
            f"Rival board — {p_label}: (no published rows yet this session)"
        )

    if len(lines) <= 1 and "no published" in lines[-1]:
        # Nothing useful beyond empty peer.
        if not any("CHAMPION" in x or "scoreboard" in x for x in lines):
            return ""

    body = (
        "\n\nRIVAL AI COMPETITION (you can see the other model — use it):\n"
        "- Your suggestions[0] is your duel champion vs theirs.\n"
        "- Prefer a DIFFERENT symbol than their champion unless you have a "
        "clearly stronger session R:R / catalyst for the same name.\n"
        "- If their idea is weak, say so implicitly by picking a better one "
        "with higher score and tighter invalidation.\n"
        "- Re-validate their names with live search; do not copy blindly.\n"
        + "\n".join(lines)
        + "\n"
    )
    if len(body) > max_chars:
        body = body[: max_chars - 20].rstrip() + "\n…(truncated)\n"
    return body


def build_desk_snapshot_snippet(
    *,
    max_price: float | None = 100.0,
    backend: str | None = None,
    rs_path: Path = RS_RATINGS_FILE,
    trending_path: Path = TRENDING_STOCKS_FILE,
    signal_state_path: Path = SIGNAL_STATE_FILE,
    peer_path: Path | None = None,
    rs_n: int = DEFAULT_DESK_SNAPSHOT_RS_N,
    trend_n: int = DEFAULT_DESK_SNAPSHOT_TREND_N,
    momentum_n: int = DEFAULT_DESK_SNAPSHOT_MOMENTUM_N,
    peer_n: int = DEFAULT_DESK_SNAPSHOT_PEER_N,
    max_chars: int = DEFAULT_DESK_SNAPSHOT_MAX_CHARS,
    min_rs: float = 80.0,
    include_rival: bool = True,
) -> str:
    """Compact desk-native context for research prompts.

    Includes momentum/signal-engine actives, RS leaders, Stocktwits heat, and
    the peer AI board when files exist. Empty string when nothing usable is
    available. Labeled as hints only — the model must still re-validate with
    live search.
    """
    sections: list[str] = []
    cap = float(max_price) if max_price is not None and float(max_price) > 0 else None
    cap_note = f"under ${cap:g}" if cap is not None else "no price cap"

    mom = _momentum_desk_lines(
        signal_state_path, max_price=cap, limit=momentum_n)
    if mom:
        sections.append(
            "Momentum / signal-engine actives (intraday desk heat — prioritize "
            "when thesis + RR work):\n  " + " | ".join(mom)
        )

    rs_lines, as_of = _rs_leader_lines(
        rs_path, max_price=cap, limit=rs_n, min_rs=min_rs)
    if rs_lines:
        head = "RS leaders"
        if as_of:
            head += f" (as_of {as_of})"
        sections.append(head + ":\n  " + " | ".join(rs_lines))

    heat = _trending_heat_lines(trending_path, max_price=cap, limit=trend_n)
    if heat:
        sections.append("Trending heat (Stocktwits):\n  " + " | ".join(heat))

    # Peer board stays in desk snapshot as a short list. Full competitive
    # RIVAL block is appended only when duel mode is on (see build_rival_duel_snippet).
    p_path, p_label = _peer_suggestions_path(backend)
    if peer_path is not None:
        p_path = peer_path
    peer = _peer_board_lines(p_path, max_price=cap, limit=peer_n)
    if peer:
        sections.append(
            f"Other AI source — {p_label} top names (context only; re-validate):\n  "
            + " | ".join(peer)
        )

    body = ""
    if sections:
        body = (
            "\n\nDESK SNAPSHOT (hints only — not a buy list; re-validate with live "
            f"web/X this run; prefer names {cap_note}):\n"
            + "\n".join(sections)
            + "\n"
        )

    if include_rival:
        body += build_rival_duel_snippet(
            backend=backend,
            max_price=max_price,
            peer_path=peer_path,
            peer_n=max(peer_n, 8),
        )

    if not body:
        return ""
    if len(body) > max_chars + 2000:
        # Desk + rival can be larger; hard cap combined inject.
        body = body[: max_chars + 1980].rstrip() + "\n…(truncated)\n"
    return body


def _symbols_from_suggestions_file(path: Path) -> set[str]:
    """Symbols currently published in a source wire file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    rows = data.get("rows") or data.get("suggestions") or data.get("items") or []
    if not isinstance(rows, list):
        return set()
    out: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or r.get("ticker") or "").upper()
        if sym:
            out.add(sym)
    return out


def tag_agreement_on_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark rows with agreement=True when the other research source also lists them.

    Single-source research parses only its own JSON; open-bell / merged desk
    rows already carry agreement from ``merge_suggestion_rows``. This fills
    the gap for post-research entry so ``ai_require_agreement`` works.
    """
    if not rows:
        return rows
    a_syms = _symbols_from_suggestions_file(CLAUDE_SUGGESTIONS_FILE)
    x_syms = _symbols_from_suggestions_file(GROK_SUGGESTIONS_FILE)
    # Also treat co-presence inside *this* batch as partial agreement signal
    # when only one wire file is stale — if both sources appear in row metadata.
    for r in rows:
        sym = str(r.get("symbol") or "").upper()
        if not sym:
            continue
        mark = str(r.get("source_mark") or "").upper()
        if r.get("agreement") is True or mark in ("GX", "AX"):
            r["agreement"] = True
            r["source_mark"] = AGREEMENT_MARK
            continue
        both_wire = sym in a_syms and sym in x_syms
        src = str(r.get("source") or "").lower()
        if both_wire or src in ("both", "ax", "gx", "agy+xai"):
            r["agreement"] = True
            r["source_mark"] = AGREEMENT_MARK
        else:
            r.setdefault("agreement", False)
    return rows


def _entry_runtime_cfg() -> dict[str, Any]:
    """Load bot_config knobs used when entry kwargs are left at defaults."""
    try:
        from config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _place_qualifying_entries(
    rows: list[dict[str, Any]],
    *,
    max_price: float | None,
    cli_bin: str | None,
    timeout: float,
    risk_pct: float,
    trade_style: str,
    min_reward_risk: float,
    model: str | None = None,
    backend: str = "claude_cli",
    max_open_risk_pct: float | None = None,
    daily_loss_limit_r: float | None = None,
    min_score: float = 7.0,
    require_agreement: bool | None = None,
    max_spread_pct: float | None = None,
    min_dollar_volume: float | None = None,
) -> list[dict[str, Any]]:
    """Risk-sized entry checks for ranked ideas that clear the score filter.

    Bracket orders aren't valid outside regular trading hours, so this skips
    the whole per-candidate loop — including the entry-check call itself,
    not just the order placement — when the market is closed. An entry
    check is its own full research-depth call; running it pre-market only to
    have the order discarded afterward would spend real money for nothing.

    ``backend`` / ``model`` select which CLI runs the per-ticker entry check
    (same family as the research source that produced ``rows``).

    Returns a list of structured event dicts (also written via ai_positions).
    """
    events: list[dict[str, Any]] = []
    try:
        import ai_trading as gt
        import ai_positions as cp
        cfg = _entry_runtime_cfg()
        if require_agreement is None:
            require_agreement = bool(cfg.get("ai_require_agreement", False))
        if max_spread_pct is None:
            max_spread_pct = float(
                cfg.get("ai_max_spread_pct", cp.DEFAULT_MAX_SPREAD_PCT))
        if min_dollar_volume is None:
            raw_mdv = cfg.get("ai_min_dollar_volume")
            min_dollar_volume = (
                float(raw_mdv) if raw_mdv is not None else None)
        open_risk_cap = (
            float(max_open_risk_pct)
            if max_open_risk_pct is not None
            else float(cfg.get("ai_max_open_risk_pct",
                               cp.DEFAULT_MAX_OPEN_RISK_PCT))
        )
        day_loss_cap = (
            float(daily_loss_limit_r)
            if daily_loss_limit_r is not None
            else float(cfg.get("ai_daily_loss_limit_r",
                               cp.DEFAULT_DAILY_LOSS_LIMIT_R))
        )
        if not gt.is_ready():
            ev = cp.log_event("entry_skip", reason="trader_not_ready")
            events.append(ev)
            return events
        if not gt.market_is_open():
            ev = cp.log_event("entry_skip", reason="market_closed")
            events.append(ev)
            return events
        gt.reset_poll_counters()
        be = (backend or "claude_cli").strip().lower()
        if be in ("cli", "grok_cli", "grok"):
            entry_model = model or DEFAULT_XAI_MODEL
        elif is_agy_backend(be, cli_bin):
            entry_model = model or DEFAULT_AGY_MODEL
        else:
            entry_model = model or DEFAULT_CLAUDE_MODEL
        work_rows = tag_agreement_on_rows(list(rows))
        for r in work_rows:
            if gt.buys_left_this_poll() <= 0:
                events.append(cp.log_event("entry_skip", reason="buy_cap"))
                break
            # Duel day: only A/X champions (and winner-only after trial score).
            try:
                import ai_duel as duel
                if duel.duel_enabled(cfg):
                    sym_d = str(r.get("symbol") or "").upper().strip()
                    src_d = r.get("source") or (
                        "xai" if be in ("cli", "grok_cli", "grok") else "anthropic"
                    )
                    if not duel.allow_entry_for_source(
                        cfg, str(src_d), sym_d
                    ):
                        events.append(cp.log_event(
                            "entry_skip", symbol=sym_d,
                            reason="duel_not_allowed"))
                        continue
            except Exception:
                pass
            sc = r.get("trending_score", r.get("score"))
            try:
                sc_f = float(sc) if sc is not None else None
            except (TypeError, ValueError):
                sc_f = None
            if sc_f is not None and sc_f < float(min_score):
                events.append(cp.log_event(
                    "entry_skip", symbol=str(r.get("symbol") or ""),
                    reason="low_score", score=sc_f))
                continue
            if require_agreement and not r.get("agreement"):
                events.append(cp.log_event(
                    "entry_skip", symbol=str(r.get("symbol") or ""),
                    reason="no_agreement"))
                continue
            sym = str(r.get("symbol") or "").upper()
            if not sym:
                continue
            if gt.has_open_position(sym):
                events.append(cp.log_event(
                    "entry_skip", symbol=sym, reason="already_held"))
                continue
            if not gt.can_open_new_position(sym):
                events.append(cp.log_event(
                    "entry_skip", symbol=sym, reason="max_positions"))
                continue
            try:
                ask = gt._latest_ask(sym)
            except Exception as e:  # noqa: BLE001
                events.append(cp.log_event(
                    "entry_skip", symbol=sym, reason=f"quote_error:{e}"))
                ask = None
            if ask is None or ask <= 0:
                events.append(cp.log_event(
                    "entry_skip", symbol=sym, reason="no_ask"))
                continue
            bid = None
            try:
                bid = gt._latest_bid(sym)
            except Exception:
                bid = None
            if max_price is not None and ask >= float(max_price):
                events.append(cp.log_event(
                    "entry_skip", symbol=sym, reason="above_max_price",
                    ask=ask, max_price=max_price))
                continue
            dvol = r.get("dollar_volume") or r.get("dollar_vol") or r.get("adv")
            try:
                dvol_f = float(dvol) if dvol is not None else None
            except (TypeError, ValueError):
                dvol_f = None
            acct = gt.get_account()
            equity = float(acct.get("equity") or 0) if acct.get("ok") else 0.0
            ok_gate, gate_reason = cp.pre_entry_gate(
                sym, ask, equity,
                risk_pct=risk_pct,
                max_open_risk_pct=open_risk_cap,
                daily_loss_limit_r=day_loss_cap,
                max_price=max_price,
                score=sc_f,
                min_score=min_score,
                bid=bid,
                max_spread_pct=max_spread_pct,
                min_dollar_volume=min_dollar_volume,
                dollar_volume=dvol_f,
            )
            if not ok_gate:
                events.append(cp.log_event(
                    "entry_skip", symbol=sym, reason=gate_reason))
                continue
            decision = cp.evaluate_entry(
                sym, ask, equity,
                reason=str(r.get("reason") or ""),
                risk_pct=risk_pct, style=trade_style,
                model=entry_model, cli_bin=cli_bin,
                timeout=min(180.0, timeout),
                backend=be,
            )
            # Full decision audit (BUY or WAIT) when persistence enabled.
            if decision is not None and bool(
                    cfg.get("ai_persist_entry_decisions", True)):
                try:
                    events.append(cp.log_entry_decision(
                        sym, decision, reason="structure"))
                except Exception:
                    pass
            if not cp.qualifies_as_entry(decision, min_reward_risk=min_reward_risk):
                dec = decision if isinstance(decision, dict) else {}
                events.append(cp.log_event(
                    "entry_skip", symbol=sym, reason="entry_not_qualified",
                    decision=dec.get("decision"),
                    wait_kind=dec.get("wait_kind"),
                    entry_low=dec.get("entry_low"),
                    entry_high=dec.get("entry_high"),
                    stop_price=dec.get("stop_price"),
                    target_1=dec.get("target_1"),
                    summary=(str(dec.get("summary") or "")[:300] or None),
                ))
                continue
            # Re-check zone price after the (slow) entry CLI call.
            try:
                ask2 = gt._latest_ask(sym) or ask
            except Exception:
                ask2 = ask
            src_place = r.get("source") or (
                "xai" if be in ("cli", "grok_cli", "grok") else "anthropic"
            )
            if isinstance(decision, dict):
                decision = dict(decision)
                decision["source"] = src_place
                decision["duel_source"] = src_place
                # This path never runs the exhaustion gate and stamps no %R —
                # ai_watch_require_exhaustion_data does not reach it. Say so on
                # the row rather than leaving it to be inferred from a null.
                decision["entry_path"] = "suggest"
            result = cp.place_scaled_entry(
                sym, decision, equity, risk_pct=risk_pct, current_ask=ask2,
                duel_source=str(src_place),
            )
            if result.get("ok"):
                gt.record_external_buy(sym, {
                    "reason": str(r.get("reason") or "")[:120],
                    "score": sc, "stop_price": result.get("stop_price"),
                    "target_1": result.get("target_1"),
                })
                events.append({"kind": "entry_ok", "symbol": sym})
            else:
                events.append(cp.log_event(
                    "entry_fail", symbol=sym,
                    reason=str(result.get("error") or "place_failed")[:200],
                ))
    except Exception as e:  # noqa: BLE001
        try:
            import ai_positions as cp
            events.append(cp.log_event(
                "entry_error", reason=str(e)[:200]))
        except Exception:
            events.append({"kind": "entry_error", "reason": str(e)[:200]})
    return events


def call_claude(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
    key: str | None = None,
    live_search: bool = True,
    trading: bool = False,
    max_tool_rounds: int = 8,
    max_turns: int | None = DEFAULT_MAX_TURNS,
    max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS,
    search_tools: str = DEFAULT_SEARCH_TOOLS,
    use_prior_context: bool = True,
    use_desk_snapshot: bool = True,
    backend: str = DEFAULT_BACKEND,
    cli_bin: str | None = None,
    max_price: float | None = None,
    effort: str | None = DEFAULT_CLAUDE_EFFORT,
    # Risk-sized entries (ai_positions.py) — literal defaults here (not
    # imported from claude_positions) to avoid a load-time circular import;
    # kept in sync with claude_positions.DEFAULT_RISK_PCT / DEFAULT_STYLE /
    # DEFAULT_MIN_REWARD_RISK.
    risk_pct: float = 1.0,
    trade_style: str = "Moderate position",
    min_reward_risk: float = 3.0,
) -> str:
    """Run research via Claude Code CLI (default), Grok Build CLI, or xAI HTTP API.

    Phases:
    1. **Research** — bounded search + compact process + JSON first.
    2. **Trade** (optional) — short PAPER tool turn on ranked symbols only
       (HTTP function tools; CLI path skips agentic paper tools and relies
       on a follow-up only when backend=api).

    ``backend``:
    - ``claude_cli`` — ``claude -p …`` (Claude Code / Anthropic)
    - ``agy`` / ``gemini_cli`` — ``agy -p …`` (Gemini / Antigravity login)
    - ``cli`` / ``grok_cli`` — ``grok --prompt-file …`` (grok.com login)
    - ``api`` — ``POST /v1/responses`` with ``XAI_API_KEY`` (paid credits)
    """
    backend = (backend or DEFAULT_BACKEND).strip().lower()
    if backend == "grok_cli":
        backend = "cli"
    if is_agy_backend(backend, cli_bin):
        backend = "agy"
    user_content = prompt
    if use_prior_context:
        user_content = prompt.rstrip() + _prior_context_snippet()
    if use_desk_snapshot:
        user_content = user_content.rstrip() + build_desk_snapshot_snippet(
            max_price=max_price,
            backend=backend,
        )
    if trading and backend in ("cli", "claude_cli", "agy"):
        # Rides this call's existing web-search budget rather than a
        # separate per-position invocation — see ai_positions.py.
        import ai_positions as cp
        user_content += cp.build_holdings_review_snippet()
    # Prepend system efficiency rules for CLI backends (no separate instructions field).
    full_prompt = (
        _SYSTEM
        + "\n\nThis is research for a paper-trading simulation desk only — "
          "not personalized financial advice. Compete for the best session "
          "LONG (~3–4h until the next run); put your champion first in JSON.\n\n"
        + user_content
        if backend in ("cli", "claude_cli", "agy")
        else user_content
    )

    def _cli_research_then_maybe_trade(text: str) -> str:
        if not parse_model_text(text):
            repair_prompt = (
                "Extract ranked US equities from this research for a paper desk. "
                "Respond with ONLY JSON (no markdown fences):\n"
                '{"suggestions":[{"symbol":"TICKER","score":8.0,"reason":"short why"}]}\n\n'
                "Research text:\n" + text[-6000:]
            )
            try:
                if backend == "agy":
                    trailer = call_agy_cli(
                        repair_prompt,
                        model=DEFAULT_AGY_REPAIR_MODEL,
                        timeout=min(180.0, timeout),
                        live_search=False,
                        cli_bin=cli_bin,
                        effort="low",
                        phase="repair",
                    )
                elif backend == "claude_cli":
                    trailer = call_claude_cli(
                        repair_prompt,
                        model=DEFAULT_CLAUDE_REPAIR_MODEL,
                        timeout=min(180.0, timeout),
                        live_search=False,
                        cli_bin=cli_bin,
                        phase="repair",
                    )
                else:
                    trailer = call_grok_cli(
                        repair_prompt,
                        model=model,
                        timeout=min(120.0, timeout),
                        max_turns=2,
                        live_search=False,
                        cli_bin=cli_bin,
                        phase="repair",
                    )
                if parse_model_text(trailer):
                    text = text.rstrip() + "\n\n" + trailer.strip() + "\n"
            except Exception:
                pass
        try:
            rows = parse_model_text(text)
        except Exception:
            rows = []

        # Thesis-break exits for currently held positions — parsed from the
        # same JSON object the ranked ideas came from, no extra call.
        try:
            import ai_positions as cp
            payload = _best_suggestions_payload(text)
            reviews = payload.get("position_reviews") if isinstance(payload, dict) else None
            if reviews:
                cp.apply_position_reviews(reviews)
        except Exception:
            pass

        if rows:
            # Duel champions must register for *both* research sources even when
            # only one owns the paper book (research-only side still competes).
            try:
                cfg = _entry_runtime_cfg()
                import time as _time
                src = (
                    "xai" if str(backend or "").lower() in (
                        "cli", "grok_cli", "grok")
                    or str(model or "").lower().startswith("grok")
                    else "anthropic"
                )
                for r in rows:
                    if isinstance(r, dict) and not r.get("source"):
                        r["source"] = src
                try:
                    import ai_duel as duel
                    if duel.duel_enabled(cfg):
                        rec = duel.register_champion_from_rows(
                            list(rows), source=src, cfg=cfg, now=_time.time())
                        if rec:
                            print(
                                f"[ai] duel champion {rec.get('source_mark')} "
                                f"{rec.get('symbol')} chance={rec.get('chance')} "
                                f"src={src}",
                                flush=True,
                            )
                        else:
                            print(
                                f"[ai] duel champion skip src={src} "
                                f"(blocked phase / no free symbol / disabled path)",
                                flush=True,
                            )
                except Exception as e:  # noqa: BLE001
                    print(f"[ai] duel champion register failed: {e}", flush=True)

                # Watch rebuild only for the trading owner, and only inside the
                # watch window — the same gate _publish_book puts on its own
                # sync. rebuild_watch_from_book is a full re-mirror of the
                # source panels, so outside the window it does not refresh a
                # book, it recreates one. research_times ends at 14:30 but
                # *_research_catchup_min is 120, so a run that starts late (or
                # a restart that finds the slot missed) can land after the
                # 15:50 EOD wipe. On 2026-08-18 a catch-up run at 16:28 seeded
                # 10 fresh "watching" rows two minutes after EOD had cleared
                # them, and they pinned the dashboard watchlist open all
                # evening. Research output still reaches the book: it goes to
                # the suggestions files, which the 09:00 sync mirrors.
                if trading and cfg.get("ai_watch_enabled", True):
                    import ai_entry_watch as ew
                    _watch_now = _time.time()
                    if ew.watch_session_active(cfg, _watch_now):
                        book_rows = tag_agreement_on_rows(list(rows))
                        ew.rebuild_watch_from_book(
                            book_rows, cfg=cfg, now=_watch_now)
                    else:
                        print("[ai] watch rebuild skipped — outside watch "
                              "session (research ran off-window)", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[ai] post-research book/duel hook failed: {e}", flush=True)

        if not trading:
            return text
        if not rows:
            return text
        _place_qualifying_entries(
            rows, max_price=max_price, cli_bin=cli_bin, timeout=timeout,
            risk_pct=risk_pct, trade_style=trade_style,
            min_reward_risk=min_reward_risk,
            model=model, backend=backend,
        )
        return text

    if backend == "claude_cli":
        text = call_claude_cli(
            full_prompt,
            model=model if not str(model).lower().startswith("grok")
            else DEFAULT_CLAUDE_MODEL,
            timeout=timeout,
            live_search=live_search,
            cli_bin=cli_bin,
            effort=effort,
            phase="research",
        )
        return _cli_research_then_maybe_trade(text)

    if backend == "agy":
        text = call_agy_cli(
            full_prompt,
            model=model,
            timeout=timeout,
            live_search=live_search,
            cli_bin=cli_bin,
            effort=effort,
            phase="research",
        )
        return _cli_research_then_maybe_trade(text)

    if backend == "cli":
        text = call_grok_cli(
            full_prompt,
            model=model,
            timeout=timeout,
            max_turns=max_turns,
            live_search=live_search,
            cli_bin=cli_bin,
            phase="research",
        )
        return _cli_research_then_maybe_trade(text)

    # ── API backend (paid console key) ───────────────────────────────────
    key = (key if key is not None else api_key()).strip()
    if not key:
        raise RuntimeError(
            "XAI_API_KEY not set — use claude_backend=claude_cli or cli, "
            "or set a console API key"
        )
    # The module default model tracks the Claude backend; xAI rejects it.
    if not str(model).lower().startswith("grok"):
        model = DEFAULT_XAI_MODEL

    # ── Phase 1: research (search tools only, hard budgets) ──────────────
    research_tools = _research_tools(live_search, search_tools)

    research_payload: dict[str, Any] = {
        "model": model,
        "instructions": _SYSTEM,
        "input": [{"role": "user", "content": user_content}],
        "temperature": 0.3,
        # tools present → _post_responses forces SSE streaming (required)
    }
    if research_tools:
        research_payload["tools"] = research_tools
        if max_turns is not None and int(max_turns) > 0:
            research_payload["max_turns"] = int(max_turns)
    if max_output_tokens is not None and int(max_output_tokens) > 0:
        research_payload["max_output_tokens"] = int(max_output_tokens)

    data = _post_responses(research_payload, key=key, timeout=timeout)
    status = data.get("status")
    if status and status not in ("completed", "incomplete"):
        try:
            text = _extract_responses_text(data)
        except RuntimeError:
            err = data.get("error") or status
            raise RuntimeError(f"xAI response status={status}: {err}") from None
    else:
        text = _extract_responses_text(data)

    # Repair only if panel would be empty (avoid a second paid call when OK).
    if not parse_model_text(text):
        text = ensure_json_trailer(
            text, model=model, key=key, timeout=min(90.0, timeout)
        )

    # Duel champions + thesis reviews (independent of paper-trade phase).
    try:
        rows_api = parse_model_text(text)
    except Exception:
        rows_api = []
    if rows_api:
        try:
            import ai_positions as cp
            payload = _best_suggestions_payload(text)
            reviews = payload.get("position_reviews") if isinstance(payload, dict) else None
            if reviews:
                cp.apply_position_reviews(reviews)
        except Exception:
            pass
        try:
            cfg_api = _entry_runtime_cfg()
            import time as _time
            import ai_duel as duel
            if duel.duel_enabled(cfg_api):
                src_api = (
                    "xai" if str(backend or "").lower() in (
                        "cli", "grok_cli", "grok", "api")
                    or str(model or "").lower().startswith("grok")
                    else "anthropic"
                )
                for r in rows_api:
                    if isinstance(r, dict) and not r.get("source"):
                        r["source"] = src_api
                rec = duel.register_champion_from_rows(
                    list(rows_api), source=src_api, cfg=cfg_api, now=_time.time())
                if rec:
                    print(
                        f"[ai] duel champion {rec.get('source_mark')} "
                        f"{rec.get('symbol')} chance={rec.get('chance')} "
                        f"src={src_api} (api)",
                        flush=True,
                    )
        except Exception as e:  # noqa: BLE001
            print(f"[ai] duel champion register failed (api): {e}", flush=True)

    # ── Phase 2: paper trades (optional, short, no web search) ───────────
    if not trading:
        return text

    trade_exec = None
    trade_tools: list[dict[str, Any]] = []
    try:
        import ai_trading as gt
        if gt.is_ready():
            trade_tools = gt.tool_definitions()
            trade_exec = gt.execute_tool
            gt.reset_poll_counters()
    except Exception:
        trade_exec = None

    if not trade_exec or not trade_tools:
        return text

    # Feed only the ranked list + a tight trade instruction — not the whole
    # research essay — so this turn stays small and finishes.
    try:
        rows = parse_model_text(text)
    except Exception:
        rows = []
    if not rows:
        return text

    rank_lines = []
    for r in rows[:10]:
        rank_lines.append(
            f"- {r.get('symbol')} score={r.get('trending_score')} "
            f"reason={r.get('reason') or ''}"
        )
    trade_prompt = (
        "You already completed research. Here are the ranked US equity ideas "
        "(best first). Place Alpaca PAPER trades only:\n"
        "1) get_account + list_positions\n"
        "2) BUY high-conviction names (prefer score ≥ 7) within cash/caps\n"
        "3) SELL any held name whose thesis is broken\n"
        "Do not re-research. Do not invent fills. Brief confirmation only.\n\n"
        + "\n".join(rank_lines)
    )

    trade_payload: dict[str, Any] = {
        "model": model,
        "instructions": (
            "You are a paper-trading desk agent. Use only the provided tools. "
            "Simulated money only. Keep the reply short."
            + (gt.trading_system_addon() if trade_exec else "")
        ),
        "input": [{"role": "user", "content": trade_prompt}],
        "temperature": 0.2,
        "tools": trade_tools,
        "max_output_tokens": 1500,
    }
    try:
        # Trading turn is short; don't let a drop erase the research text.
        tdata = _post_responses(trade_payload, key=key, timeout=min(timeout, 180.0))
        tdata = _run_tool_loop(
            tdata,
            key=key,
            model=model,
            timeout=min(timeout, 180.0),
            tools=trade_tools,
            trade_exec=trade_exec,
            max_tool_rounds=max_tool_rounds,
        )
        try:
            trade_note = _extract_responses_text(tdata)
            if trade_note.strip():
                text = text.rstrip() + "\n\n## Paper trading\n" + trade_note
        except RuntimeError:
            pass
    except Exception as e:  # noqa: BLE001 — research still valuable
        text = (
            text.rstrip()
            + f"\n\n## Paper trading\n(skipped — {_friendly_network_error(e)})\n"
        )

    return text


def fetch_suggestions(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT,
    key: str | None = None,
    live_search: bool = True,
    trading: bool = False,
) -> list[dict[str, Any]]:
    """One full research poll: call the backend, parse suggestions."""
    text = call_claude(
        prompt, model=model, timeout=timeout, key=key,
        live_search=live_search, trading=trading,
    )
    return parse_model_text(text)


class AiSuggestions:
    """Throttled Claude suggestion list + Alpaca quote/volume enrichment."""

    def __init__(
        self,
        poll_interval: float = 3600.0,
        max_price: float | None = None,
        enrich_quotes: bool = True,
        look_min_abs_chg: float = 3.0,
        look_max: int = 2,
        look_near_high: float = 0.70,
        look_near_low: float = 0.30,
        look_min_rvol: float | None = 1.5,
        quote_interval: float = 15.0,
        volume_interval: float = 60.0,
        avg_days: int = 10,
        rvol_time_adjusted: bool = True,
        model: str = DEFAULT_MODEL,
        prompt_file: str = DEFAULT_PROMPT_FILE,
        request_timeout: float = DEFAULT_TIMEOUT,
        panel_limit: int = 7,
        live_search: bool = True,
        save_reports: bool = True,
        trading: bool = False,
        trade_amount: float = 1000.0,
        max_positions: int = 5,
        slot_equity: float = 250.0,
        max_position_pct: float = 8.0,
        max_buys_per_poll: int = 3,
        max_sells_per_poll: int = 5,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        search_tools: str = DEFAULT_SEARCH_TOOLS,
        use_prior_context: bool = True,
        use_desk_snapshot: bool = True,
        backend: str = DEFAULT_BACKEND,
        cli_bin: str | None = None,
        effort: str = DEFAULT_CLAUDE_EFFORT,
        research_times: Any = DEFAULT_RESEARCH_TIMES,
        research_weekdays_only: bool = DEFAULT_RESEARCH_WEEKDAYS_ONLY,
        research_catchup_min: int = DEFAULT_RESEARCH_CATCHUP_MIN,
        risk_pct: float = 1.0,
        trade_style: str = "Moderate position",
        min_reward_risk: float = 3.0,
    ):
        # Deep research + live search is slow and expensive — default 1 hour.
        self.poll_interval = max(60.0, float(poll_interval))
        self.quote_interval = max(2.0, float(quote_interval))
        self.volume_interval = max(10.0, float(volume_interval))
        self.max_price = float(max_price) if max_price is not None else None
        self.enrich_quotes = bool(enrich_quotes)
        self.look_min_abs_chg = float(look_min_abs_chg)
        self.look_max = int(look_max)
        self.look_near_high = float(look_near_high)
        self.look_near_low = float(look_near_low)
        self.look_min_rvol = (
            float(look_min_rvol) if look_min_rvol is not None else None
        )
        self.avg_days = int(avg_days)
        self.rvol_time_adjusted = bool(rvol_time_adjusted)
        self.model = model or DEFAULT_MODEL
        self.prompt_file = prompt_file or DEFAULT_PROMPT_FILE
        self.request_timeout = float(request_timeout)
        self.panel_limit = max(1, int(panel_limit))
        self.live_search = bool(live_search)
        self.save_reports = bool(save_reports)
        self.trading = bool(trading)
        self.trade_amount = float(trade_amount)
        self.max_positions = int(max_positions)
        self.slot_equity = float(slot_equity)
        self.max_position_pct = float(max_position_pct)
        self.max_buys_per_poll = int(max_buys_per_poll)
        self.max_sells_per_poll = int(max_sells_per_poll)
        self.max_turns = int(max_turns) if max_turns else DEFAULT_MAX_TURNS
        self.max_output_tokens = (
            int(max_output_tokens) if max_output_tokens else DEFAULT_MAX_OUTPUT_TOKENS
        )
        self.search_tools = str(search_tools or DEFAULT_SEARCH_TOOLS)
        self.use_prior_context = bool(use_prior_context)
        self.use_desk_snapshot = bool(use_desk_snapshot)
        self.backend = (backend or DEFAULT_BACKEND).strip().lower()
        self.cli_bin = cli_bin
        self.effort = (effort or DEFAULT_CLAUDE_EFFORT).strip().lower()
        self.research_times = parse_research_times(research_times)
        self.research_weekdays_only = bool(research_weekdays_only)
        self.research_catchup_min = int(research_catchup_min)
        self.risk_pct = float(risk_pct)
        self.trade_style = str(trade_style)
        self.min_reward_risk = float(min_reward_risk)
        # Survives a desk restart so an 08:35 restart doesn't re-run 08:30.
        self._last_slot = self._load_last_slot()
        self.trading_mode = "off"

        if self.trading:
            try:
                import ai_trading as gt
                init_fn = getattr(gt, "init_for_ai", None) or gt.init_for_claude
                self.trading_mode = init_fn(
                    trade_amount=self.trade_amount,
                    max_positions=self.max_positions,
                    max_buys_per_poll=self.max_buys_per_poll,
                    max_sells_per_poll=self.max_sells_per_poll,
                    slot_equity=self.slot_equity,
                    max_position_pct=self.max_position_pct,
                )
            except Exception:
                self.trading_mode = "off"

        self.rows: list[dict[str, Any]] = []
        self.by_symbol: dict[str, dict[str, Any]] = {}
        self.last_ok: float = 0.0
        self.last_attempt: float = 0.0
        self.last_quote_ok: float = 0.0
        self.last_quote_attempt: float = 0.0
        self.last_volume_ok: float = 0.0
        self.last_volume_attempt: float = 0.0
        self.error: str = ""
        self.quotes_error: str = ""
        self.last_report_path: str = ""
        self.last_trades: list[dict[str, Any]] = []
        self.last_usage: dict[str, Any] = {}
        self._seen: set[str] = set()
        self._prev_look: dict[str, bool] = {}
        self._seeded = False
        self._raw_text: str = ""
        # API call runs off the desk render loop — live-search research can
        # take minutes and must never stall the 2s Live refresh.
        self._lock = threading.Lock()
        self._fetching = False

    def _schedule_state_path(self) -> Path:
        """Per-source schedule file so Anthropic and Grok can share the same clock.

        Claude keeps the historical path; Grok uses a sibling file. Sharing one
        last_slot would let whichever source claimed the slot first suppress the
        other for that research window.
        """
        src = source_from_backend(self.backend)
        if src == SOURCE_XAI:
            return REPORT_DIR / "schedule_state_grok.json"
        return SCHEDULE_STATE_PATH

    def _load_last_slot(self) -> str:
        try:
            data = json.loads(self._schedule_state_path().read_text(encoding="utf-8"))
            return str(data.get("last_slot") or "")
        except Exception:
            return ""

    def _save_last_slot(self, slot: str) -> None:
        try:
            path = self._schedule_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"last_slot": slot}), encoding="utf-8")
        except Exception:
            pass

    def next_run_label(self, now: float | None = None) -> str:
        return next_slot_label(
            time.time() if now is None else now,
            times=self.research_times,
            weekdays_only=self.research_weekdays_only,
        )

    def refresh(self, now: float | None = None) -> bool:
        """Kick a background research poll when the interval has elapsed.

        Returns True only when a *new* list was just applied (rare on the
        calling thread — application happens inside the worker). Callers should
        still run ``refresh_quotes`` / ``refresh_volume`` when this returns
        False, same as the Stocktwits panel.
        """
        now = time.time() if now is None else now
        if self._fetching:
            return False

        slot = ""
        if self.research_times:
            due = due_slot(
                now,
                times=self.research_times,
                weekdays_only=self.research_weekdays_only,
                catchup_min=self.research_catchup_min,
                last_slot=self._last_slot,
            )
            if due is None:
                if not self.rows:
                    nxt = self.next_run_label(now)
                    self.error = (
                        f"next research run {nxt} ET" if nxt
                        else "no research times configured"
                    )
                return False
            slot = due
        elif self.last_attempt and (now - self.last_attempt) < self.poll_interval:
            # No schedule configured — fall back to interval polling.
            return False

        if is_agy_backend(self.backend, self.cli_bin):
            if not agy_cli_available(self.cli_bin):
                self.error = "Antigravity CLI missing — install agy"
                self.last_attempt = now
                return False
            if not agy_cli_logged_in(self.cli_bin):
                self.error = (
                    "Antigravity CLI not logged in — run: agy   "
                    "(from a Terminal on this machine, then restart there)"
                )
                self.last_attempt = now
                return False
        elif self.backend in ("claude_cli", "claude"):
            if not claude_cli_available(self.cli_bin):
                self.error = "Claude CLI missing — install Claude Code"
                self.last_attempt = now
                return False
            if not claude_cli_logged_in(self.cli_bin):
                self.error = (
                    "Claude CLI not logged in — run: claude /login "
                    "(or set ANTHROPIC_API_KEY)"
                )
                self.last_attempt = now
                return False
        elif self.backend in ("cli", "grok_cli"):
            if not cli_available(self.cli_bin):
                self.error = "Grok CLI missing — install https://x.ai/cli"
                self.last_attempt = now
                return False
            if not cli_logged_in():
                self.error = "Grok CLI not logged in — run: grok login"
                self.last_attempt = now
                return False
        elif not api_key():
            # Paid HTTP backend only — prefer cli / claude_cli to avoid console bills.
            self.error = (
                "no XAI_API_KEY (paid API) — set grok_backend=cli or claude_backend=claude_cli"
            )
            self.last_attempt = now
            return False

        try:
            prompt = load_prompt(self.prompt_file, max_price=self.max_price)
        except (OSError, ValueError) as e:
            self.error = str(e)
            self.last_attempt = now
            return False

        self.last_attempt = now
        # Claim the slot only once the run actually starts, so a missing CLI
        # or an unreadable prompt above leaves it runnable on the next tick.
        if slot:
            self._last_slot = slot
            self._save_last_slot(slot)
        self._fetching = True
        if not self.rows and not self.error:
            self.error = f"querying AI ({self.backend})…"

        def _worker(prompt_text: str = prompt, started: float = now) -> None:
            try:
                use_trading = self.trading and self.trading_mode == "paper"
                text = call_claude(
                    prompt_text,
                    model=self.model,
                    timeout=self.request_timeout,
                    live_search=self.live_search,
                    trading=use_trading,
                    max_turns=self.max_turns,
                    max_output_tokens=self.max_output_tokens,
                    search_tools=self.search_tools,
                    use_prior_context=self.use_prior_context,
                    use_desk_snapshot=self.use_desk_snapshot,
                    backend=self.backend,
                    cli_bin=self.cli_bin,
                    max_price=self.max_price,
                    effort=self.effort,
                    risk_pct=self.risk_pct,
                    trade_style=self.trade_style,
                    min_reward_risk=self.min_reward_risk,
                )
                rows = parse_model_text(
                    text, source=source_from_backend(self.backend))
                if last_usage:
                    self.last_usage = dict(last_usage)
                # Annotate rows with any paper trades from this poll.
                trades: list[dict[str, Any]] = []
                if use_trading:
                    try:
                        import ai_trading as gt
                        trades = gt.recent_trades(40)
                    except Exception:
                        trades = []
                by_trade: dict[str, list[str]] = {}
                for t in trades:
                    if t.get("ts", 0) < started - 1:
                        continue
                    sym = str(t.get("symbol") or "").upper()
                    if not sym:
                        continue
                    tag = "BOUGHT" if t.get("action") == "buy" and t.get("ok") \
                        else "SOLD" if t.get("action") == "sell" and t.get("ok") \
                        else "BUY?" if t.get("action") == "buy" \
                        else "SELL?"
                    by_trade.setdefault(sym, []).append(tag)
                for r in rows:
                    tags = by_trade.get(r["symbol"]) or []
                    if tags:
                        r["trade_action"] = tags[-1]
                        r["reason"] = (r.get("reason") or "")
                        if tags[-1] not in (r.get("reason") or ""):
                            r["reason"] = f"{tags[-1]} · {r.get('reason') or ''}".strip(" ·")

                report_path = ""
                if self.save_reports and text:
                    saved = save_report(text, when=time.time())
                    if saved is not None:
                        report_path = str(saved)
                        # Append trade summary under the report for the desk.
                        if trades:
                            try:
                                recent = [t for t in trades if t.get("ts", 0) >= started - 1]
                                if recent:
                                    blob = "\n\n## Paper trades this run\n```json\n" \
                                           + json.dumps(recent, indent=2) + "\n```\n"
                                    Path(report_path).write_text(
                                        text + blob, encoding="utf-8")
                                    (REPORT_DIR / "latest.md").write_text(
                                        text + blob, encoding="utf-8")
                            except Exception:
                                pass
                with self._lock:
                    self._raw_text = text
                    self.last_report_path = report_path
                    self.last_trades = [t for t in trades if t.get("ts", 0) >= started - 1]
                    if not rows:
                        self.error = "empty suggestions from model"
                    else:
                        self.rows = rows[
                            : max(self.panel_limit * 2, self.panel_limit)
                        ]
                        self.by_symbol = {r["symbol"]: r for r in self.rows}
                        self.last_ok = time.time()
                        self.error = ""
                if rows and self.enrich_quotes:
                    # Quote on the worker so the first paint has Last/%Chg.
                    self._quote(time.time())
                    self.refresh_volume(time.time())
            except Exception as e:  # noqa: BLE001
                # Keep the panel message short — a full urllib dump wraps across
                # every column and looks like a broken table.
                msg = str(e)
                if "remote end closed" in msg.lower() or "connection" in msg.lower():
                    short = msg if len(msg) <= 90 else (
                        "xAI dropped connection mid-research — retry next poll"
                    )
                else:
                    short = msg[:90]
                with self._lock:
                    self.error = short
            finally:
                self._fetching = False

        threading.Thread(
            target=_worker, daemon=True, name="claude-suggest"
        ).start()
        return False

    def refresh_quotes(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if not self.rows or not self.enrich_quotes:
            return False
        if self.last_quote_attempt and \
                (now - self.last_quote_attempt) < self.quote_interval:
            return False
        self.last_quote_attempt = now
        return self._quote(now)

    def _quote(self, now: float) -> bool:
        self.last_quote_attempt = now
        client = _alpaca_client()
        if client is None:
            self.quotes_error = "no Alpaca keys (signal_engine.env)"
            return False
        self.quotes_error = ""
        with self._lock:
            rows = list(self.rows)
        if not rows:
            return False
        enrich_with_alpaca(
            rows,
            now=now,
            time_adjusted=self.rvol_time_adjusted,
            client=client,
        )
        with self._lock:
            # Only apply if the list is still the same symbols (a new poll
            # may have replaced rows while we were quoting).
            if [r["symbol"] for r in self.rows] == [r["symbol"] for r in rows]:
                for live, fresh in zip(self.rows, rows):
                    live.update(fresh)
                self.by_symbol = {r["symbol"]: r for r in self.rows}
                self.last_quote_ok = now
        return True

    def refresh_volume(self, now: float | None = None, client=None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            rows = list(self.rows)
        if not rows:
            return False
        if self.last_volume_attempt and \
                (now - self.last_volume_attempt) < self.volume_interval:
            return False
        self.last_volume_attempt = now

        client = _alpaca_client() if client is None else client
        if client is None:
            return False
        syms = [r["symbol"] for r in rows if r.get("symbol")]
        if not syms:
            return False
        try:
            import tools.morning_funnel as mf
            cfg = {"funnel_avg_days": self.avg_days}
            now_et = datetime.fromtimestamp(now, ET)
            avg = mf.avg_session_volumes(client, syms, cfg, now_et)
            minutes = mf.fetch_minutes_today(client, syms, cfg, now_et) or {}
        except Exception:
            return False

        for r in rows:
            sym = r.get("symbol")
            df = minutes.get(sym) if sym else None
            try:
                vol = float(df["volume"].sum()) if df is not None \
                    and not df.empty else None
            except Exception:
                vol = None
            r["vol_session"] = vol if (vol or 0) > 0 else None
            r["rvol"], r["rvol_raw"] = row_rvol(
                r, avg.get(sym), now, time_adjusted=self.rvol_time_adjusted)
        with self._lock:
            if [r["symbol"] for r in self.rows] == [r["symbol"] for r in rows]:
                for live, fresh in zip(self.rows, rows):
                    live["vol_session"] = fresh.get("vol_session")
                    live["rvol"] = fresh.get("rvol")
                    live["rvol_raw"] = fresh.get("rvol_raw")
                self.by_symbol = {r["symbol"]: r for r in self.rows}
                self.last_volume_ok = now
        return True

    def quote_age(self, now: float | None = None) -> float | None:
        if not self.last_quote_ok:
            return None
        return max(0.0, (time.time() if now is None else now) - self.last_quote_ok)

    def display_rows(
        self,
        price_by_sym: dict[str, float | None] | None = None,
        limit: int = 10,
        on_change: Callable[[str, str, str], None] | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        price_by_sym = price_by_sym or {}
        now = time.time() if now is None else now
        with self._lock:
            rows_snap = [dict(r) for r in self.rows]
        out: list[dict[str, Any]] = []
        for r in rows_snap:
            sym = r["symbol"]
            age = price_age(r, now)
            if age is not None:
                r["price_age_sec"] = age
            px = r.get("price")
            row = {**r}
            if px is None and sym in price_by_sym:
                px = price_by_sym.get(sym)
                row.pop("price_ts", None)
                row.pop("price_age_sec", None)
            if px is not None and self.max_price is not None and px >= self.max_price:
                continue
            row.update(price=px, price_known=px is not None)
            out.append(row)
            if len(out) >= limit:
                break

        out = apply_look_highlights(
            out,
            min_abs_chg=self.look_min_abs_chg,
            max_looks=self.look_max,
            near_high=self.look_near_high,
            near_low=self.look_near_low,
            min_rvol=self.look_min_rvol,
        )
        for r in out:
            sym = r["symbol"]
            is_new = sym not in self._seen
            if is_new:
                self._seen.add(sym)
            was_look = self._prev_look.get(sym, False)
            look = bool(r.get("look"))
            if self._seeded and on_change is not None:
                if is_new:
                    on_change("claude_new", sym, f"#{r.get('rank') or '?'} claude")
                if look and not was_look:
                    on_change("claude_look", sym, r.get("look_reason") or "")
            self._prev_look[sym] = look
        self._seeded = True
        return out

# Back-compat alias (one release)
AiSuggestions = AiSuggestions
