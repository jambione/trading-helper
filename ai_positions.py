"""Risk-sized entry decisions and mechanical position management.

``ai_suggest.py`` finds ideas; ``ai_trading.py`` provides the raw
paper-trading primitives. This module is the layer between them: for each
candidate that clears the score filter, the book-owner CLI runs a per-ticker
risk-sized entry check (exact entry zone / stop / target / time-stop, sized
to a fixed % of account risk) — and from there every mandatory exit rule
(hard stop, scale-out, trailing stop, time stop) is enforced *mechanically*,
by real broker-side orders and local state, not by asking a model again.
Only the qualitative "did the thesis break" check needs a model, and that
rides inside the existing scheduled research call rather than a separate
one — see ``ai_suggest.py``'s ``position_reviews`` handling.

Why mechanical: a hard stop that "fires immediately, never moves lower" is
only actually true if it is a resting order with the broker. Re-asking an
LLM on some poll cycle makes the stop only as fast as that cycle, and costs
a full research-depth call per position per check.

Safety additions:
- Atomic dual-tranche entry (rollback if leg B fails)
- Unconfirmed entry TTL + broker reconcile
- Structured events.jsonl for skips/fails (no silent pass)
- Portfolio pre-entry gates (daily loss R, open risk %)
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_paths import resolve_report_dir  # noqa: E402
from ai_suggest import (  # noqa: E402
    DEFAULT_CLAUDE_CLI_BIN,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_XAI_MODEL,
    _iter_json_blobs,
    call_claude_cli,
    call_grok_cli,
)

REPORT_DIR = resolve_report_dir()
POSITIONS_STATE_PATH = REPORT_DIR / "positions_state.json"
OUTCOMES_PATH = REPORT_DIR / "outcomes.jsonl"
EVENTS_PATH = REPORT_DIR / "events.jsonl"
# Counterfactual record: what the desk decided, and what price did next,
# whether or not it ever traded. outcomes.jsonl only grows on a fill, and the
# desk can go a whole session without one (2026-08-06: 530 zones, 31 symbols,
# 0 fills) — which leaves every gate unmeasurable. See log_shadow_sample.
SHADOW_PATH = REPORT_DIR / "shadow.jsonl"
# The other arm of every selection question: what admission KEPT OUT. A filter
# cannot be judged from the names it let through alone — if the rejects
# outperform, the gate is destroying value and nothing else on disk would say
# so. apply_inclusion_gate computed this list and discarded it.
REJECTS_PATH = REPORT_DIR / "rejects.jsonl"
OPEN_BELL_STATE_PATH = REPORT_DIR / "open_bell_state.json"
EOD_LIQUIDATE_STATE_PATH = REPORT_DIR / "eod_liquidate_state.json"
SOD_LIQUIDATE_STATE_PATH = REPORT_DIR / "sod_liquidate_state.json"

ET = ZoneInfo("America/New_York")

# Never more than this much of the account on a single trade's stop distance.
DEFAULT_RISK_PCT = 1.0
DEFAULT_STYLE = "Moderate position"
DEFAULT_MIN_REWARD_RISK = 3.0
# Cancel unfilled / unconfirmed entries after this many seconds in RTH.
DEFAULT_UNCONFIRMED_TTL_SEC = 900.0
# Stop new entries when today's realized R from closed AI trades <= -this.
DEFAULT_DAILY_LOSS_LIMIT_R = 3.0
# Cap sum of open risk (entry-stop)*qty as % of equity.
DEFAULT_MAX_OPEN_RISK_PCT = 5.0
# Max bid/ask spread % of mid (0 = disabled).
DEFAULT_MAX_SPREAD_PCT = 1.0
# Keep a small ring of recent events for /api/state.
_EVENT_RING_MAX = 80
_event_lock = threading.Lock()
_recent_events: list[dict[str, Any]] = []
_last_reconcile: dict[str, Any] = {}

_ENTRY_PROMPT_TEMPLATE = """\
You are an elite quantitative trader whose single mandate is to MAXIMIZE PROFIT \
while STRICTLY MINIMIZING LOSS. You have real-time access to price data, news, \
web, and X sentiment.

I am looking at {ticker}.
Current price: ${price:.2f} (look it up if needed)
My total account size: ${account_equity:.2f}
Maximum risk I am willing to lose on this single trade: {risk_pct:g}% of account (or less)
Preferred style: {style}
Research context for this idea: {reason}

Follow this exact process and never break the risk rules:

1. Current snapshot (last 5-14 days)
   - Price action, key MAs (20/50/200), volume, support/resistance
   - Any catalyst, earnings, news, or sharp change in X sentiment

2. Only recommend a BUY if ALL of these are true:
   - Clear edge (technical + fundamental or thematic)
   - Minimum 1:3 reward-to-risk ratio (preferably 1:4 or better)
   - Defined entry zone with high probability of working quickly
   - Suggested position size so that if the stop is hit, I lose no more than \
{risk_pct:g}% of my total account
   - Exact entry price range, exact stop-loss price, and first profit target

3. Sell / Exit rules (these are mandatory and mechanical -- no exceptions)
   - Hard stop-loss: never move it lower. If price hits it, exit 100% immediately.
   - Scale out: Sell 30-50% at the first target (lock in profit), move stop to \
break-even on the rest.
   - Trailing stop: Once in profit, trail using 20-day MA / 2x ATR / previous \
swing low -- whichever locks in more gains while still giving the trade room.
   - Time stop: If the trade has not reached the first target within a defined \
number of days, exit or tighten stop aggressively.
   - Thesis break: Any major negative news, failed breakout, or sentiment \
collapse -> exit immediately regardless of P&L.

Never recommend averaging down. Never recommend a trade with undefined risk. \
Prefer missing a move over taking a low-quality setup. Use the latest live data.

OUTPUT ORDER (critical): FIRST the JSON object only (no fences, no prose before \
it), THEN your blunt one-paragraph reasoning.

This trade is mechanically executed from your JSON, not read by a human before \
the order goes in -- every field must be a concrete number, not a range of \
intent or "see above." A real stop-loss and take-profit order are placed at your \
exact prices; a real trailing-stop order approximates your trail rule as a \
percent. Since your exact trail level cannot be recomputed live, give the \
distance it represents *right now* at current price so the trailing order \
starts at the right place.

JSON schema (required):
{{
  "decision": "BUY" or "WAIT",
  "wait_kind": null or "wait_for_zone" or "wait_setup" or "hard_no",
  "entry_low": 0.0,
  "entry_high": 0.0,
  "stop_price": 0.0,
  "target_1": 0.0,
  "target_2": 0.0,
  "scale_out_pct": 40,
  "trail_method": "20d_ma" or "2x_atr" or "swing_low",
  "trail_pct": 0.0,
  "time_stop_days": 10,
  "reward_risk": 3.0,
  "summary": "one blunt sentence: why this setup maximizes upside while capping downside"
}}

WAIT rules (structured — do NOT zero levels when you have a zone):
- decision "BUY": set wait_kind to null; all entry/stop/target numbers must be concrete.
- decision "WAIT" + pullback/breakout zone you can define: set wait_kind "wait_for_zone"
  and fill entry_low/entry_high, stop_price, target_1 (and target_2/reward_risk when known).
  The desk will arm when price enters the zone — levels must stay real numbers.
- decision "WAIT" + thesis still valid but no clean trigger yet: wait_kind "wait_setup";
  numeric fields may be 0.
- decision "WAIT" + no trade / thesis broken / avoid: wait_kind "hard_no"; numbers may be 0.
- Always explain in "summary".
"""


def build_entry_prompt(
    ticker: str,
    price: float,
    account_equity: float,
    *,
    reason: str = "",
    risk_pct: float = DEFAULT_RISK_PCT,
    style: str = DEFAULT_STYLE,
) -> str:
    return _ENTRY_PROMPT_TEMPLATE.format(
        ticker=ticker.upper(),
        price=float(price),
        account_equity=float(account_equity),
        risk_pct=float(risk_pct),
        style=style,
        reason=(reason or "no additional context")[:200],
    )


def parse_entry_decision(text: str) -> dict[str, Any] | None:
    """Pull the required JSON object out of free-form entry-check text."""
    for blob in reversed(_iter_json_blobs(text)):
        if isinstance(blob, dict) and "decision" in blob:
            return blob
    return None


_WAIT_KINDS = frozenset({"wait_for_zone", "wait_setup", "hard_no"})
# Keyword hard_no only when full levels are absent. Avoid bare "avoid" —
# it matches benign phrasing like "avoid chasing; wait for 27-28.5".
_HARD_NO_MARKERS = (
    "hard_no",
    "hard no",
    "thesis broken",
    "thesis break",
    "no trade",
    "do not trade",
    "invalidated",
    "stay away",
    "avoid trading",
    "avoid this",
)


def normalize_entry_decision(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize entry JSON: decision casing + structured WAIT wait_kind.

    wait_kind values: wait_for_zone | wait_setup | hard_no | None (BUY).
    Inference when WAIT and wait_kind missing/invalid (priority):
    1. explicit wait_kind when valid
    2. full levels (entry_low, stop, target_1 > 0) → wait_for_zone
    3. hard-no keywords in summary → hard_no (only without full levels)
    4. else → wait_setup
    Levels and other fields are preserved.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return None
    d = dict(raw)
    decision = str(d.get("decision", "") or "").strip().upper()
    if decision not in ("BUY", "WAIT"):
        # Keep nonstandard decisions as-is but still pass through fields.
        d["decision"] = decision or d.get("decision")
        return d
    d["decision"] = decision

    if decision == "BUY":
        d["wait_kind"] = None
        return d

    # WAIT — explicit wait_kind wins.
    explicit = d.get("wait_kind")
    if isinstance(explicit, str):
        wk = explicit.strip().lower().replace(" ", "_")
        if wk in _WAIT_KINDS:
            d["wait_kind"] = wk
            return d

    try:
        entry_low = float(d.get("entry_low") or 0)
        stop_price = float(d.get("stop_price") or 0)
        target_1 = float(d.get("target_1") or 0)
    except (TypeError, ValueError):
        entry_low = stop_price = target_1 = 0.0

    has_full_levels = entry_low > 0 and stop_price > 0 and target_1 > 0
    if has_full_levels:
        d["wait_kind"] = "wait_for_zone"
        return d

    summary = str(d.get("summary", "") or "").lower()
    if any(m in summary for m in _HARD_NO_MARKERS):
        d["wait_kind"] = "hard_no"
        return d

    d["wait_kind"] = "wait_setup"
    return d


def evaluate_entry(
    ticker: str,
    price: float,
    account_equity: float,
    *,
    reason: str = "",
    risk_pct: float = DEFAULT_RISK_PCT,
    style: str = DEFAULT_STYLE,
    model: str = DEFAULT_CLAUDE_MODEL,
    cli_bin: str | None = None,
    timeout: float = 180.0,
    backend: str = "claude_cli",
) -> dict[str, Any] | None:
    """Run the per-ticker risk-sized entry check. None on failure.

    Returns normalized decision (BUY or WAIT with wait_kind). Callers use
    qualifies_as_entry to gate order placement — WAIT never qualifies.

    ``backend`` selects which CLI runs the entry call:
    - ``claude_cli`` / ``claude`` → Claude Code CLI
    - ``cli`` / ``grok_cli`` / ``grok`` → Grok Build CLI
    """
    prompt = build_entry_prompt(
        ticker, price, account_equity,
        reason=reason, risk_pct=risk_pct, style=style,
    )
    be = (backend or "claude_cli").strip().lower()
    try:
        if be in ("cli", "grok_cli", "grok"):
            text = call_grok_cli(
                prompt,
                model=model or DEFAULT_XAI_MODEL,
                timeout=timeout,
                max_turns=2,
                live_search=True,
                cli_bin=cli_bin or "grok",
                phase="entry",
            )
        else:
            text = call_claude_cli(
                prompt, model=model, timeout=timeout, live_search=True,
                cli_bin=cli_bin or DEFAULT_CLAUDE_CLI_BIN, phase="entry",
            )
    except Exception:
        return None
    decision = parse_entry_decision(text)
    return normalize_entry_decision(decision)


def qualifies_as_entry(decision: dict[str, Any] | None,
                       *, min_reward_risk: float = DEFAULT_MIN_REWARD_RISK) -> bool:
    """Everything the mechanical order placement needs must be a real number.

    Guards against a BUY verdict whose numbers didn't come through cleanly —
    an undefined-risk trade is exactly what this prompt forbids taking.
    """
    if not decision or str(decision.get("decision", "")).upper() != "BUY":
        return False
    entry_low = decision.get("entry_low") or 0
    stop = decision.get("stop_price") or 0
    target_1 = decision.get("target_1") or 0
    rr = decision.get("reward_risk") or 0
    if entry_low <= 0 or stop <= 0 or target_1 <= 0:
        return False
    if stop >= entry_low:
        return False
    if float(rr) < float(min_reward_risk):
        return False
    return True


# ── Local state: which positions this module is managing ────────────────────

def _normalize_positions_state(raw: Any) -> dict[str, Any]:
    """Accept only {SYMBOL: {fields...}} maps; ignore wire wrappers."""
    if not isinstance(raw, dict):
        return {}
    # Wire file may wrap as {"positions": {...}}
    if "positions" in raw and isinstance(raw["positions"], dict):
        raw = raw["positions"]
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k or not k[0].isalpha():
            continue
        if isinstance(v, dict):
            out[k.upper()] = v
    return out


def _load_state() -> dict[str, Any]:
    """Load managed positions; prefer POSITIONS_STATE_PATH (monkeypatchable)."""
    from ai_paths import REPORT_DIR_LEGACY, REPORT_DIR_PRIMARY, find_report_file

    def _try(path: Path | None) -> dict[str, Any] | None:
        if path is None or not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        return _normalize_positions_state(raw)

    # Primary path always wins when present (incl. empty book).
    if POSITIONS_STATE_PATH.exists():
        got = _try(POSITIONS_STATE_PATH)
        if got is not None:
            return got
        return {}

    # When tests redirect POSITIONS_STATE_PATH outside report dirs, do not
    # leak live book state into an empty fixture.
    try:
        parent = POSITIONS_STATE_PATH.parent.resolve()
        allowed = {
            REPORT_DIR_PRIMARY.resolve(),
            REPORT_DIR_LEGACY.resolve(),
            ROOT.resolve(),
        }
        if parent not in allowed:
            return {}
    except Exception:
        return {}

    for path in (
        find_report_file("positions_state.json"),
        ROOT / "ai_positions_state.json",
        ROOT / "claude_positions_state.json",
    ):
        got = _try(path)
        if got is not None:
            return got
    return {}


def _save_state(state: dict[str, Any]) -> None:
    """Persist managed book to POSITIONS_STATE_PATH (tests may redirect).

    Never write the dashboard wire file ``ai_positions_state.json`` here —
    that path is owned by ``ai_trader._positions_payload`` (updated, entry_book,
    day_pl, …). Mirroring managed {SYM: fields} onto the wire clobbered the
    book every manage cycle and made the AI Watch stamp flip live/stale.
    """
    try:
        POSITIONS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(state, indent=2)
        POSITIONS_STATE_PATH.write_text(text, encoding="utf-8")
    except Exception:
        pass


def log_event(kind: str, **fields: Any) -> dict[str, Any]:
    """Append a structured desk event (skips, fails, reconcile, entries)."""
    row: dict[str, Any] = {"ts": time.time(), "kind": str(kind)}
    for k, v in fields.items():
        if v is not None:
            row[k] = v
    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass
    with _event_lock:
        _recent_events.append(row)
        if len(_recent_events) > _EVENT_RING_MAX:
            del _recent_events[: len(_recent_events) - _EVENT_RING_MAX]
    return row


# Steady-state conditions re-logged on every poll. reconcile_unmanaged fired
# 384 times on 2026-08-06 for one unchanging fact — CELH unmanaged, all day —
# and synth_zone another 2199, so 84% of that day's log was repetition and the
# 20 entry_skip / 7 entry_decision rows that actually explain the session were
# buried under it. A count of 384 measures how often the desk LOOKED, not how
# often anything happened.
_STATE_RELOG_SEC = 900.0
_state_last: dict[tuple[str, Any], dict[str, Any]] = {}


def log_state_event(kind: str, key: Any, *, scope: Any = None,
                    **fields: Any) -> dict[str, Any] | None:
    """Log *kind* when its condition changes; otherwise fold the poll away.

    *scope* separates independent conditions sharing a kind. synth_zone
    interleaves 36 symbols, so a single slot per kind would see every symbol
    as a change and suppress nothing; scoped by symbol, each name's zone is
    compared against its own last one.

    Transitions are the information. A heartbeat every ``_STATE_RELOG_SEC``
    keeps a condition that persists for hours from being invisible to anyone
    reading a window of the log rather than the whole day.

    ``folded`` counts the polls suppressed since this kind last wrote a row —
    on the heartbeat and on the change alike, so the observation count is
    compressed rather than lost. Attributing it to the previous condition
    would need a row nobody wrote; attributing it to the gap between rows is
    both true and what a reader wants to know.

    Returns the written row, or None when the poll was folded.
    """
    slot = (str(kind), scope)
    k = json.dumps(key, sort_keys=True, default=str)
    now = time.time()
    prev = _state_last.get(slot)
    if prev is not None and prev["key"] == k:
        prev["folded"] += 1
        if (now - prev["ts"]) < _STATE_RELOG_SEC:
            return None
    folded = prev["folded"] if prev is not None else 0
    _state_last[slot] = {"key": k, "ts": now, "folded": 0}
    return log_event(kind, folded=folded or None, **fields)


def clear_state_event(kind: str, scope: Any = None) -> None:
    """Forget a condition, so its next occurrence logs as new.

    Without this a condition that clears and later returns identical would be
    read as "unchanged" and silently folded — the recurrence is exactly the
    event worth seeing.
    """
    _state_last.pop((str(kind), scope), None)


def log_shadow_sample(row: dict[str, Any]) -> None:
    """Append one counterfactual sample. Fire-and-forget, never raises.

    Written from the watch poller using the price it already fetched — this
    must never cost an API call, because the desk is already over Alpaca's
    rate limit and the names that can actually trade are the ones being
    starved.

    One row per watched symbol per poll. Forward returns, zone reachability
    and "what would the blocked trade have done" are all derived downstream by
    grouping these (tools/shadow_report.py) rather than tracked in the record,
    so this stays pure append-only logging with no lifecycle hooks to break.
    """
    try:
        SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SHADOW_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def log_reject_sample(row: dict[str, Any]) -> None:
    """Append one rejected-candidate sample. Fire-and-forget, never raises.

    Prices come off the candidate row the screeners already refreshed, so this
    costs no API call — the desk is over Alpaca's rate limit and the names that
    can trade must not lose quota to the ones that were turned away.
    """
    try:
        REJECTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with REJECTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass


def log_entry_decision(
    symbol: str,
    decision: dict[str, Any] | None,
    *,
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    """Persist full entry decision (BUY/WAIT) for desk audit / watch arming.

    Writes kind ``entry_decision`` with decision, wait_kind, levels, and a
    truncated summary. ``decision`` may be None (logs minimal row).
    """
    d = decision if isinstance(decision, dict) else {}
    summary = str(d.get("summary") or "")
    if len(summary) > 300:
        summary = summary[:300]
    fields: dict[str, Any] = {
        "symbol": str(symbol or "").upper(),
        "reason": reason,
        "decision": d.get("decision"),
        "wait_kind": d.get("wait_kind"),
        "entry_low": d.get("entry_low"),
        "entry_high": d.get("entry_high"),
        "stop_price": d.get("stop_price"),
        "target_1": d.get("target_1"),
        "target_2": d.get("target_2"),
        "reward_risk": d.get("reward_risk"),
        "summary": summary or None,
    }
    fields.update(extra)
    return log_event("entry_decision", **fields)


def recent_events(limit: int = 40) -> list[dict[str, Any]]:
    with _event_lock:
        return list(_recent_events[-max(1, int(limit)):])


def last_reconcile() -> dict[str, Any]:
    return dict(_last_reconcile)


def realized_r_today(now: float | None = None) -> float:
    """Sum of realized R multiples for AI outcomes closed today (ET)."""
    now = time.time() if now is None else now
    try:
        day = datetime.fromtimestamp(now, tz=ET).date()
    except (OverflowError, OSError, ValueError):
        # An unrepresentable clock must not raise: this feeds pre_entry_gate,
        # and an exception there fails closed and silently halts all trading.
        day = datetime.fromtimestamp(time.time(), tz=ET).date()
    total = 0.0
    try:
        text = OUTCOMES_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0.0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        r = row.get("realized_r_multiple")
        if r is None:
            continue
        exit_ts = float(row.get("exit_time") or row.get("ts") or 0)
        if not exit_ts:
            continue
        try:
            row_day = datetime.fromtimestamp(exit_ts, tz=ET).date()
        except (OverflowError, OSError, ValueError):
            continue  # corrupt row must not take down the gate
        if row_day != day:
            continue
        total += float(r)
    return total


def open_risk_pct(account_equity: float) -> float:
    """Open risk as % of equity from managed state (entry − stop) × qty."""
    if account_equity <= 0:
        return 0.0
    state = _load_state()
    risk_usd = 0.0
    for pos in state.values():
        if pos.get("closing_reason"):
            continue
        entry = float(pos.get("entry_price") or 0)
        stop = float(pos.get("stop_price") or 0)
        qty = float(pos.get("total_qty") or 0)
        if entry > stop > 0 and qty > 0:
            risk_usd += (entry - stop) * qty
    return 100.0 * risk_usd / account_equity


def pre_entry_gate(
    symbol: str,
    ask: float,
    account_equity: float,
    *,
    risk_pct: float = DEFAULT_RISK_PCT,
    max_open_risk_pct: float = DEFAULT_MAX_OPEN_RISK_PCT,
    daily_loss_limit_r: float = DEFAULT_DAILY_LOSS_LIMIT_R,
    max_price: float | None = None,
    score: float | None = None,
    min_score: float = 7.0,
    now: float | None = None,
    bid: float | None = None,
    max_spread_pct: float | None = DEFAULT_MAX_SPREAD_PCT,
    min_dollar_volume: float | None = None,
    dollar_volume: float | None = None,
) -> tuple[bool, str]:
    """Code-only portfolio/risk veto before spending an entry CLI call.

    Returns ``(ok, reason)``. reason is empty when ok.

    Liquidity/spread: when ``max_spread_pct`` > 0 and both bid and ask are
    known, reject names wider than that % of mid. When ``min_dollar_volume``
    is set and ``dollar_volume`` is known, reject thin names.
    """
    sym = (symbol or "").upper()
    if not sym:
        return False, "invalid_symbol"
    if ask is None or ask <= 0:
        return False, "no_ask"
    if max_price is not None and ask >= float(max_price):
        return False, f"above_max_price_{max_price}"
    if score is not None and float(score) < float(min_score):
        return False, f"score_below_{min_score}"
    if account_equity <= 0:
        return False, "no_equity"

    msp = float(max_spread_pct) if max_spread_pct is not None else 0.0
    if msp > 0 and bid is not None and bid > 0:
        mid = (float(bid) + float(ask)) / 2.0
        if mid <= 0:
            return False, "bad_mid"
        if float(ask) < float(bid):
            return False, "crossed_quote"
        spr = 100.0 * (float(ask) - float(bid)) / mid
        if spr > msp + 1e-12:
            return False, f"spread_pct_{spr:.2f}>{msp:g}"

    if (
        min_dollar_volume is not None
        and float(min_dollar_volume) > 0
        and dollar_volume is not None
    ):
        if float(dollar_volume) < float(min_dollar_volume):
            return False, (
                f"dollar_vol_{float(dollar_volume):.0f}"
                f"<{float(min_dollar_volume):.0f}"
            )

    day_r = realized_r_today(now)
    if day_r <= -abs(float(daily_loss_limit_r)):
        return False, f"daily_loss_limit_r_{day_r:.2f}"

    open_r = open_risk_pct(account_equity)
    # Proposed trade risk ~ risk_pct of equity (by design of size_by_risk).
    if open_r + float(risk_pct) > float(max_open_risk_pct) + 1e-9:
        return False, f"open_risk_pct_{open_r:.2f}+{risk_pct:g}"

    state = _load_state()
    if sym in state and not state[sym].get("closing_reason"):
        return False, "already_managed"

    return True, ""


def _entry_cfg() -> dict[str, Any]:
    """Live config for entry shaping. Local to this module on purpose.

    ai_suggest has a same-named helper, but importing it here would pull the
    research stack into the order path for what is a plain config read — and
    ai_positions is imported by ai_entry_watch on every poll.
    """
    try:
        from config import load_config
        return load_config() or {}
    except Exception:  # noqa: BLE001
        return {}


def _entry_cfg() -> dict:
    """Live config for entry-order shape. Empty dict on any failure."""
    try:
        from config import load_config
        return load_config() or {}
    except Exception:  # noqa: BLE001
        return {}


def _entry_limit_price(
    current_ask: float | None,
    entry_high: float,
    entry_low: float,
) -> float | None:
    """Marketable limit for the entry, capped at the zone top. None = market.

    A market entry fills at whatever the ask is at execution, which quietly
    breaks the sizing contract: size_by_risk sizes off current_ask and the stop
    is derived from that same ask, so a fill above the quote makes real risk
    exceed ai_watch_synth_stop_pct and notional exceed ai_max_position_pct.
    These are thin IEX books on high-RVOL names, where that gap is largest.

    Pad makes it marketable so it still fills in a normal book; the zone cap
    makes "we only ever fill inside the entry zone" structurally true rather
    than best-effort.
    """
    cfg = _entry_cfg()
    if str(cfg.get("ai_entry_order_style", "limit")).lower().strip() != "limit":
        return None
    try:
        ask = float(current_ask or 0)
    except (TypeError, ValueError):
        ask = 0.0
    if ask <= 0:
        return None            # no quote to anchor on — fall back to market
    try:
        pad = max(0.0, float(cfg.get("ai_entry_limit_pad_pct", 0.15) or 0.0)) / 100.0
    except (TypeError, ValueError):
        pad = 0.0015
    top = max(float(entry_high or 0), float(entry_low or 0))
    px = ask * (1.0 + pad)
    if top > 0:
        px = min(px, top)
    px = round(px, 2)
    return px if px > 0 else None


def place_scaled_entry(
    ticker: str,
    decision: dict[str, Any],
    account_equity: float,
    *,
    risk_pct: float = DEFAULT_RISK_PCT,
    current_ask: float | None = None,
    duel_source: str | None = None,
) -> dict[str, Any]:
    """Execute a qualifying BUY as two broker-side tranches (atomic).

    Tranche A (``scale_out_pct``) carries both the stop and the first
    target — it closes itself the moment either level trips, no code
    involved. Tranche B carries the stop only and is meant to ride; once
    tranche A's target fills, ``manage_open_positions`` replaces tranche
    B's stop with breakeven or a trailing stop.

    If tranche A succeeds and B fails, A is cancelled / flattened so the
    book never sits half-armed.
    """
    import alpaca_trader

    # Alpaca rejects bracket orders (and plain market orders) outside regular
    # trading hours — both tranches need one or the other. A pre-market BUY
    # verdict is discarded rather than attempted and failing partway through.
    if not alpaca_trader.market_is_open():
        err = (
            "market is closed — bracket orders aren't valid outside regular "
            "trading hours; this entry was not queued for the open"
        )
        log_event("entry_fail", symbol=ticker, reason=err)
        return {"ok": False, "error": err}

    ticker = ticker.upper()
    entry_low = float(decision.get("entry_low") or 0)
    entry_high = float(decision.get("entry_high") or entry_low)
    stop_price = float(decision.get("stop_price") or 0)
    target_1 = float(decision.get("target_1") or 0)
    scale_out_pct = max(0.0, min(100.0, float(decision.get("scale_out_pct") or 40)))

    if current_ask is not None and not (entry_low <= current_ask <= max(entry_high, entry_low)):
        err = (
            f"price ${current_ask:.2f} left the entry zone "
            f"${entry_low:.2f}-${entry_high:.2f} before the order could go in"
        )
        log_event("entry_fail", symbol=ticker, reason=err)
        return {"ok": False, "error": err}

    # Clear leftover STOP/limit sells (failed prior attempts, orphaned legs)
    # so Alpaca does not reject the new BUY as a wash trade. Cancel is async —
    # wait and re-cancel so pending_cancel legs are gone before we submit.
    def _clear_open(ticker_s: str) -> None:
        for delay in (0.0, 0.35, 0.7):
            if delay:
                time.sleep(delay)
            try:
                alpaca_trader.cancel_open_orders(ticker_s)
            except Exception as e:  # noqa: BLE001
                log_event(
                    "entry_pre_cancel_warn", symbol=ticker_s,
                    reason=str(e)[:200],
                )

    _clear_open(ticker)

    # Size against the price the order will actually fill at, not the zone
    # bound — current_ask is already validated to fall inside that zone.
    sizing_entry = current_ask or entry_high or entry_low
    total_qty = alpaca_trader.size_by_risk(
        account_equity, risk_pct, sizing_entry, stop_price)
    if total_qty <= 0:
        err = "risk-sized qty rounded to 0 shares"
        log_event("entry_fail", symbol=ticker, reason=err)
        return {"ok": False, "error": err}

    # Notional cap. Risk-based sizing says nothing about concentration: a tight
    # stop implies a huge position, and with no cap five names could add up to
    # well over 100% of equity. Belt-and-braces now that the synthetic stop is a
    # fixed 5% of the fill (~20% of equity per name at 1% risk).
    try:
        max_pos_pct = float(_entry_cfg().get("ai_max_position_pct", 25.0) or 0.0)
    except (TypeError, ValueError):
        max_pos_pct = 25.0
    if max_pos_pct > 0 and sizing_entry > 0:
        cap_qty = int((account_equity * max_pos_pct / 100.0) // sizing_entry)
        if cap_qty < total_qty:
            log_event(
                "size_capped", symbol=ticker, risk_qty=total_qty,
                capped_qty=cap_qty, max_position_pct=max_pos_pct,
            )
            total_qty = cap_qty
        if total_qty <= 0:
            err = f"position cap {max_pos_pct:g}% of equity rounds to 0 shares"
            log_event("entry_fail", symbol=ticker, reason=err)
            return {"ok": False, "error": err}

    # Buying power. Nothing checked this anywhere, so an over-sized order came
    # back as a raw Alpaca rejection string in the UI's blocker column.
    bp = _buying_power()
    if bp is not None and total_qty * sizing_entry > bp:
        err = (
            f"insufficient buying power: need ${total_qty * sizing_entry:,.0f}, "
            f"have ${bp:,.0f}"
        )
        log_event("entry_fail", symbol=ticker, reason=err)
        return {"ok": False, "error": err}

    # One OTOCO bracket for full size. Dual market buys (scale-out A+B) race
    # Alpaca wash-trade checks when stop/TP legs from A are still open.
    # Synthetic / desk zones and any decision with scale_out disabled use this
    # path; research can still request a split via scale_out_pct when needed —
    # but default to single bracket for reliability when filling READY names.
    use_single = bool(decision.get("synthetic")) or scale_out_pct >= 99.0
    if use_single:
        qty_a = int(total_qty)
        qty_b = 0
    else:
        qty_a = max(1, int(total_qty * scale_out_pct / 100.0))
        qty_b = total_qty - qty_a

    entry_limit = _entry_limit_price(current_ask, entry_high, entry_low)

    def _place_a():
        if entry_limit is not None:
            return alpaca_trader.buy_limit_bracket(
                ticker, qty_a, limit_price=entry_limit,
                stop_price=stop_price, target_price=target_1)
        return alpaca_trader.buy_bracket_exact(
            ticker, qty_a, stop_price=stop_price, target_price=target_1)

    result_a = _place_a()
    if not result_a.get("ok"):
        err = str(result_a.get("note") or result_a.get("error")
                  or result_a.get("status") or "tranche_a_failed")
        # Wash-trade / leftover legs: clear again and retry once.
        if "wash" in err.lower() or "40310000" in err:
            _clear_open(ticker)
            result_a = _place_a()
            if result_a.get("ok"):
                err = ""
            else:
                err = str(result_a.get("note") or result_a.get("error")
                          or result_a.get("status") or "tranche_a_failed")
        if err:
            log_event("entry_fail", symbol=ticker, reason=err, leg="A")
            return {
                "ok": False, "error": err, "ticker": ticker,
                "tranche_a": result_a, "tranche_b": None,
            }

    if qty_b > 0:
        if entry_limit is not None:
            # No target on the runner leg — it rides until manage_open_positions
            # replaces its stop with breakeven/trailing after A scales out.
            result_b = alpaca_trader.buy_limit_bracket(
                ticker, qty_b, limit_price=entry_limit,
                stop_price=stop_price, target_price=None)
        else:
            result_b = alpaca_trader.buy_bracket_exact(
                ticker, qty_b, stop_price=stop_price)
    else:
        result_b = {"ok": True, "buy_order_id": None, "stop_order_id": None}

    if not result_b.get("ok"):
        # Atomic rollback: do not leave a one-legged position in managed state.
        try:
            alpaca_trader.cancel_open_orders(ticker)
        except Exception as e:  # noqa: BLE001
            log_event("entry_rollback_warn", symbol=ticker,
                      reason=f"cancel_failed:{e}")
        try:
            alpaca_trader.close_out(ticker)
        except Exception as e:  # noqa: BLE001
            log_event("entry_rollback_warn", symbol=ticker,
                      reason=f"close_failed:{e}")
        err = str(result_b.get("note") or result_b.get("error")
                  or result_b.get("status") or "tranche_b_failed")
        log_event(
            "entry_fail", symbol=ticker, reason=err, leg="B",
            rolled_back=True,
        )
        return {
            "ok": False, "error": f"tranche_b_failed_rolled_back:{err}",
            "ticker": ticker, "tranche_a": result_a, "tranche_b": result_b,
            "rolled_back": True,
        }

    state = _load_state()
    src = duel_source or decision.get("duel_source") or decision.get("source")
    state[ticker] = {
        "qty_a": qty_a,
        "qty_b": qty_b,
        "total_qty": total_qty,
        "entry_price": sizing_entry,
        "entry_limit_price": entry_limit,
        "tranche_a_order_id": result_a.get("buy_order_id"),
        # The take-profit leg — NOT the parent buy. "Has tranche A scaled out?"
        # must key off this; the parent fills at entry.
        "tranche_a_target_order_id": result_a.get("target_order_id"),
        "tranche_b_order_id": result_b.get("buy_order_id"),
        "tranche_b_stop_order_id": result_b.get("stop_order_id"),
        "stop_price": stop_price,
        "target_1": target_1,
        "trail_pct": float(decision.get("trail_pct") or 0) or None,
        "time_stop_days": int(decision.get("time_stop_days") or 0) or None,
        "entry_time": time.time(),
        "tranche_a_filled": False,
        "breakeven_done": False,
        "reward_risk": decision.get("reward_risk"),
        "summary": decision.get("summary"),
        # Decision-time feature vector (ai_entry_watch._entry_features), held
        # so the outcome record can land denormalized — features and result on
        # one row. A join against events.jsonl would work until a symbol is
        # entered twice in a session, which happens.
        "features": decision.get("features"),
        # Set once we've observed the position actually open — guards the
        # closure check below from mistaking "order hasn't filled yet" for
        # "position closed" on the very first tick after entry.
        "entry_confirmed": False,
        "last_seen_price": None,
        "closing_reason": None,
        "duel_source": src,
    }
    _save_state(state)
    log_event(
        "entry_ok", symbol=ticker, qty_a=qty_a, qty_b=qty_b,
        stop_price=stop_price, target_1=target_1, entry_price=sizing_entry,
        duel_source=src,
    )
    try:
        import ai_duel as duel

        duel.note_entry(
            ticker,
            source=str(src) if src else None,
            entry_price=sizing_entry,
            stop_price=stop_price,
        )
    except Exception:
        pass

    return {
        "ok": True,
        "ticker": ticker,
        "qty_a": qty_a,
        "qty_b": qty_b,
        "stop_price": stop_price,
        "target_1": target_1,
        "tranche_a": result_a,
        "tranche_b": result_b,
    }


# ── Thesis-break review: rides inside the existing research call ────────────
# Routine risk management (stop/scale-out/trailing/time) never needs a model
# turn once a position is open — only "did the news break the thesis" does,
# and that rides the scheduled research call's own web-search budget instead
# of paying for a separate per-position invocation.

HOLDINGS_REVIEW_SCHEMA_HINT = (
    '  "position_reviews": [\n'
    '    {"symbol": "TICKER", "action": "hold" or "exit", '
    '"reason": "one short phrase"}\n'
    "  ]"
)


def build_holdings_review_snippet() -> str:
    """Prompt addendum reviewing currently Claude-managed positions, or ''.

    Empty when nothing is held — an empty addendum costs nothing extra in
    the shared research call, so this never adds spend when there's nothing
    to review.
    """
    state = _load_state()
    if not state:
        return ""

    import alpaca_trader

    detail = alpaca_trader.get_positions_detail() or {}
    lines = [
        "\nCURRENTLY HELD (paper) — review each for a broken thesis only; "
        "the stop, target, and trailing exit are already mechanically "
        "enforced by resting broker orders, so only flag a name here if "
        "news, a failed setup, or a sentiment collapse means the original "
        "thesis no longer holds:",
    ]
    for sym, pos in state.items():
        live = detail.get(sym) or {}
        lines.append(
            f"- {sym}: entry thesis \"{(pos.get('summary') or '')[:100]}\" | "
            f"stop=${pos.get('stop_price')} target={pos.get('target_1')} | "
            f"current=${live.get('current')} pl%={live.get('plpc')}"
        )
    lines.append(
        "\nAdd a \"position_reviews\" array to the JSON object with one entry "
        'per symbol above: {"symbol", "action": "hold" or "exit", "reason"}. '
        'Only "exit" triggers a sell — default to "hold" unless the thesis '
        "is genuinely broken.\n"
        + HOLDINGS_REVIEW_SCHEMA_HINT
    )
    return "\n".join(lines)


def apply_position_reviews(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mechanically exit anything the thesis-break review flagged.

    Marks the position as closing rather than deleting it immediately —
    ``close_out`` submits an exit order, it doesn't guarantee an instant
    fill, so the realized outcome (and the state cleanup) is finalized by
    ``manage_open_positions`` once the position actually reads flat.
    """
    import alpaca_trader

    state = _load_state()
    events: list[dict[str, Any]] = []
    changed = False
    for r in reviews or []:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").upper()
        action = str(r.get("action") or "").strip().lower()
        if not sym or action != "exit" or sym not in state:
            continue
        if state[sym].get("closing_reason"):
            continue  # already exiting (e.g. a time-stop already fired)
        alpaca_trader.cancel_open_orders(sym)
        out = alpaca_trader.close_out(sym)
        if isinstance(out, dict) and out.get("order_id"):
            state[sym]["close_order_id"] = str(out["order_id"])
        state[sym]["closing_reason"] = "thesis_break"
        events.append({"ticker": sym, "event": "thesis_break",
                      "reason": r.get("reason"), "ok": bool(out.get("ok"))})
        changed = True
    if changed:
        _save_state(state)
    return events


def _entry_cfg() -> dict:
    """Live config for placement-time limits ({} if it can't be loaded)."""
    try:
        from config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _buying_power() -> float | None:
    """Account buying power, or None when it can't be read (then don't gate)."""
    try:
        import ai_trading as gt
        acct = gt.get_account()
    except Exception:
        return None
    if not isinstance(acct, dict) or not acct.get("ok"):
        return None
    try:
        bp = float(acct.get("buying_power"))
    except (TypeError, ValueError):
        return None
    return bp if bp > 0 else None


def _num(v: Any) -> float | None:
    """float(v) or None — never raises on broker payload junk."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sell_signal_defends(state: dict) -> bool:
    """Whether to let a live sell_signal tighten stops. Off = old behaviour."""
    if not state:
        return False
    try:
        from config import load_config
        return bool(load_config().get("ai_sell_signal_breakeven", True))
    except Exception:
        return False


def _engine_indicators() -> dict:
    """symbol -> signal_proximity from the engine, or {} if unreachable.

    Same wire the entry gate reads, so an exit cannot disagree with an entry
    about what the indicators currently say.
    """
    try:
        import ai_entry_watch as ew
        return ew._engine_indicator_map() or {}
    except Exception:
        return {}


def _resting_stop_order_id(ticker: str) -> str | None:
    """The open protective STOP leg for *ticker*, if one is resting.

    A bracket's stop id is not stored at entry (only the take-profit leg is),
    so it has to be found. Passing None to replace_stop would leave the
    original resting and add a second stop for the same shares.
    """
    try:
        import alpaca_trader
        sym = str(ticker or "").upper()
        for o in alpaca_trader.get_open_orders() or []:
            if (o.get("symbol") == sym
                    and str(o.get("side", "")).lower() == "sell"
                    and "stop" in str(o.get("type", "")).lower()):
                return o.get("id") or None
    except Exception:
        pass
    return None


def _order_fill_price(order_id: Any) -> float | None:
    """filled_avg_price for *order_id*, or None when it can't be resolved."""
    if not order_id:
        return None
    try:
        import alpaca_trader
        o = alpaca_trader.get_order(str(order_id)) or {}
    except Exception:  # noqa: BLE001
        return None
    for key in ("filled_avg_price", "filled_avg_price_usd", "avg_fill_price"):
        v = _num(o.get(key))
        if v and v > 0:
            return v
    return None


def _exit_fill_price(pos: dict[str, Any]) -> float | None:
    """Resolve the actual exit fill from whichever protective leg closed it.

    Returns None when it genuinely cannot be resolved — the caller then writes
    realized_r as null rather than inventing one from the last polled price.
    """
    for key in ("tranche_a_target_order_id", "tranche_b_stop_order_id",
                "close_order_id"):
        px = _order_fill_price(pos.get(key))
        if px:
            return px
    return None


def _infer_close_reason(pos: dict[str, Any]) -> str:
    """Best-effort label when nothing explicitly set one.

    Not per-leg order forensics — just what the mechanism implies: if
    tranche A already scaled out, the eventual full close came from
    tranche B's later (breakeven/trailing) stop; if it never scaled out,
    the only way both tranches share is the original hard stop.
    """
    return "trailed_out" if pos.get("tranche_a_filled") else "stopped_out"


def _record_outcome(ticker: str, pos: dict[str, Any], exit_price: float | None,
                    close_reason: str, now: float) -> dict[str, Any]:
    entry_price = pos.get("entry_price") or 0
    stop_price = pos.get("stop_price") or 0
    total_qty = pos.get("total_qty") or 0
    per_share_risk = entry_price - stop_price

    realized_r = None
    realized_pl = None
    if exit_price and entry_price and per_share_risk > 0:
        realized_r = (exit_price - entry_price) / per_share_risk
    if exit_price and entry_price and total_qty:
        realized_pl = (exit_price - entry_price) * total_qty

    entry_time = pos.get("entry_time", now)
    outcome = {
        "ts": now,
        "symbol": ticker,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_1": pos.get("target_1"),
        "total_qty": total_qty,
        # None when the exit fill could not be resolved. Downstream must skip
        # these rather than substitute an estimate — realized_r_today gates new
        # entries via ai_daily_loss_limit_r.
        "exit_price": exit_price,
        "realized_r_multiple": realized_r,
        "realized_pl_usd": realized_pl,
        "close_reason": close_reason,
        "scaled_out": bool(pos.get("tranche_a_filled")),
        "entry_time": entry_time,
        "exit_time": now,
        "hold_days": round((now - entry_time) / 86400.0, 2),
        "reward_risk_planned": pos.get("reward_risk"),
        "summary": pos.get("summary"),
        # Why this trade was taken, alongside how it ended. Without it an
        # outcome is unsliceable: you know the result but not which gate,
        # indicator state, or time of day to attribute it to.
        "features": pos.get("features"),
    }
    try:
        OUTCOMES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUTCOMES_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(outcome) + "\n")
    except Exception:
        pass
    return outcome


def reconcile_broker(now: float | None = None) -> dict[str, Any]:
    """Compare managed state to live Alpaca positions.

    - unmanaged: live positions not in local state (human/engine/other)
    - stale_unconfirmed: managed but never confirmed and past TTL is handled
      separately; here we only report confirmed-managed missing from broker
      as already closed in pass 1 of manage_open_positions
    """
    global _last_reconcile
    import alpaca_trader

    now = time.time() if now is None else now
    state = _load_state()
    detail = alpaca_trader.get_positions_detail() or {}
    managed = {str(k).upper() for k in state.keys()}
    live = {str(k).upper() for k in detail.keys()}
    unmanaged = sorted(live - managed)
    confirmed = {
        k for k, p in state.items()
        if p.get("entry_confirmed") and not p.get("closing_reason")
    }
    missing_live = sorted(confirmed - live)  # should be rare mid-tick
    unconfirmed = sorted(
        k for k, p in state.items() if not p.get("entry_confirmed")
    )
    # ── Safety invariant: every open position must have a resting exit ──────
    # Nothing watched for this before. On 2026-08-06 CELH's bracket was
    # rejected, the caller fell back to a naked buy, and 353 shares — 83% of
    # account equity — sat with no stop for 44 minutes. It was noticed by a
    # human looking at the screen, not by the desk. reconcile_unmanaged fired
    # 384 times that day and aggregated to nothing.
    #
    # Reported as state rather than acted on: placing or cancelling orders
    # from a reconcile pass is how one bug becomes two. The operator (and the
    # EOD flatten) decide.
    unprotected: list[dict[str, Any]] = []
    try:
        open_orders = alpaca_trader.get_open_orders(limit=100) or []
        protected = {
            str(o.get("symbol") or "").upper()
            for o in open_orders
            if str(o.get("side") or "").lower() == "sell"
        }
        equity = alpaca_trader.get_equity() or 0.0
        for sym, p in detail.items():
            s = str(sym).upper()
            if s in protected:
                continue
            try:
                mv = abs(float(p.get("mkt_val") or 0.0))
            except (TypeError, ValueError):
                mv = 0.0
            unprotected.append({
                "symbol": s,
                "mkt_val": round(mv, 2),
                # Concentration is the reason this is urgent rather than
                # untidy: an unstopped position at 83% of equity IS the
                # account.
                "pct_equity": (round(100.0 * mv / equity, 1)
                               if equity > 0 else None),
                "managed": s in managed,
            })
    except Exception:
        pass

    report = {
        "ts": now,
        "unmanaged": unmanaged,
        "unconfirmed": unconfirmed,
        "missing_live_confirmed": missing_live,
        "n_managed": len(managed),
        "n_live": len(live),
        "unprotected": unprotected,
    }
    # Both are steady state — an unmanaged position stays unmanaged until
    # someone acts on it — so they log on transition, not per poll.
    if unmanaged:
        log_state_event("reconcile_unmanaged", sorted(unmanaged),
                        symbols=unmanaged)
    else:
        clear_state_event("reconcile_unmanaged")
    if unprotected:
        log_state_event("position_unprotected",
                        sorted(p["symbol"] for p in unprotected),
                        positions=unprotected)
    else:
        clear_state_event("position_unprotected")
    _last_reconcile = report
    return report


def manage_open_positions(
    now: float | None = None,
    *,
    unconfirmed_ttl_sec: float = DEFAULT_UNCONFIRMED_TTL_SEC,
) -> list[dict[str, Any]]:
    """Desk-tick check, no LLM: closures, tranche-A fills, TTL, time-stops.

    Cheap enough to run every tick — everything it needs (position detail,
    order status, a breakeven/trailing-stop replacement, a time-boxed close)
    is a plain Alpaca call, not a model call.
    """
    import alpaca_trader

    now = time.time() if now is None else now
    state = _load_state()
    events: list[dict[str, Any]] = []
    changed = False
    ttl = max(60.0, float(unconfirmed_ttl_sec))

    # Pass 1: has anything gone fully flat since the last tick? Catches every
    # closing mechanism at once — hard stop, first target then a later
    # trailing/breakeven stop, or an explicit close_out from time-stop or
    # thesis-break above — without needing to track each order leg by hand.
    detail = alpaca_trader.get_positions_detail() or {}
    for ticker, pos in list(state.items()):
        live = detail.get(ticker)
        if live is not None:
            if not pos.get("entry_confirmed"):
                # First sighting: replace the submit-time ask with what the
                # order actually filled at, so realized R is measured against
                # the real basis rather than an estimate.
                fill = _order_fill_price(pos.get("tranche_a_order_id"))
                if fill is None:
                    fill = _num(live.get("avg_entry_price"))
                if fill and fill > 0:
                    pos["entry_price"] = fill
            pos["entry_confirmed"] = True
            pos["last_seen_price"] = live.get("current")
            changed = True
            continue
        if not pos.get("entry_confirmed"):
            # Never seen open yet — may still be working. Expire after TTL.
            age = now - float(pos.get("entry_time") or now)
            # A resting entry LIMIT gets a much shorter leash than a filled but
            # unconfirmed position: if price left the zone the setup is gone,
            # and leaving the order to rest for 15 minutes lets it fill long
            # after the zone has re-anchored away from it. Re-evaluating from
            # current state next poll is strictly better than a stale fill.
            eff_ttl = ttl
            if pos.get("entry_limit_price"):
                try:
                    eff_ttl = min(ttl, float(
                        _entry_cfg().get("ai_entry_limit_ttl_sec", 30.0) or ttl))
                except (TypeError, ValueError):
                    pass
            if age > eff_ttl:
                try:
                    alpaca_trader.cancel_open_orders(ticker)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    alpaca_trader.close_out(ticker)
                except Exception:  # noqa: BLE001
                    pass
                log_event(
                    "entry_unconfirmed_expired", symbol=ticker,
                    age_sec=round(age, 1), ttl_sec=eff_ttl,
                    entry_limit=pos.get("entry_limit_price"),
                )
                events.append({
                    "ticker": ticker, "event": "entry_unconfirmed_expired",
                    "age_sec": round(age, 1),
                })
                del state[ticker]
                changed = True
            continue
        # Real fill first. last_seen_price is up to one poll stale and is never
        # the actual exit print, so an outcome built from it is a plausible
        # wrong number — and realized_r_today feeds the daily-loss gate.
        exit_price = _exit_fill_price(pos)
        reason = pos.get("closing_reason") or _infer_close_reason(pos)
        if exit_price is None:
            # Loud, not silent: the outcome will carry realized_r=None and be
            # excluded from realized_r_today, so the daily-loss gate is running
            # on partial data until whatever closed this is made traceable.
            log_event(
                "exit_fill_unresolved", symbol=ticker, close_reason=reason,
                last_seen_price=pos.get("last_seen_price"),
            )
        _record_outcome(ticker, pos, exit_price, reason, now)
        # Freeze duel R immediately so trial score survives state cleanup.
        try:
            import ai_duel as duel
            duel.note_close(
                ticker,
                exit_price=exit_price,
                entry_price=pos.get("entry_price"),
                stop_price=pos.get("stop_price"),
                source=pos.get("duel_source"),
                now=now,
            )
        except Exception:
            pass
        events.append({"ticker": ticker, "event": "closed",
                       "close_reason": reason})
        del state[ticker]
        changed = True

    # Indicator-driven defence. sell_signal gated four ENTRY checks and
    # nothing on the way out, so the desk would refuse to buy a name the
    # engine had flagged and then hold that same name without comment when the
    # flag turned on afterwards. The three-indicator strategy computes it every
    # scan and no exit path was listening.
    #
    # It tightens rather than closes. The desk has no completed-trade history
    # to say whether this signal beats letting the bracket work, and closing on
    # an unmeasured signal is the kind of confident, fake verdict the reports
    # are careful to avoid. Moving the stop to entry caps the loss at a scratch
    # and keeps the target live if the signal is wrong.
    if _sell_signal_defends(state):
        indicators = _engine_indicators()
        for ticker, pos in list(state.items()):
            if pos.get("sell_signal_stop_done") or pos.get("closing_reason"):
                continue
            sig = indicators.get(ticker)
            if not isinstance(sig, dict) or not sig.get("sell_signal"):
                continue
            entry = _num(pos.get("entry_price"))
            last = _num(pos.get("last_seen_price"))
            cur_stop = _num(pos.get("stop_price"))
            if not entry or not last:
                continue
            # A stop is only a stop while it sits BELOW the market. Underwater,
            # "move it to breakeven" places it above the last print, which
            # Alpaca triggers on receipt — that is a market exit wearing a stop
            # order's name, and it is the opposite of tightening. Both open
            # positions were below entry the moment this was written. Leave the
            # original stop to do its job and record that we saw the flag.
            if last <= entry:
                events.append({"ticker": ticker, "event": "sell_signal_underwater",
                               "entry": entry, "last": last,
                               "stop": cur_stop})
                log_event("sell_signal_underwater", symbol=ticker,
                          entry=entry, last=last, stop=cur_stop)
                pos["sell_signal_stop_done"] = True
                changed = True
                continue
            # Never loosen: a stop already above entry is better than entry.
            if cur_stop is not None and cur_stop >= entry:
                pos["sell_signal_stop_done"] = True
                changed = True
                continue
            out = alpaca_trader.replace_stop(
                ticker, _resting_stop_order_id(ticker), stop_price=entry)
            if isinstance(out, dict) and out.get("ok"):
                pos["stop_price"] = entry
                pos["sell_signal_stop_done"] = True
                changed = True
                events.append({"ticker": ticker, "event": "sell_signal_breakeven",
                               "from_stop": cur_stop, "to_stop": entry,
                               "last": last})
                log_event("sell_signal_breakeven", symbol=ticker,
                          from_stop=cur_stop, to_stop=entry, last=last)

    # Pass 2: only for positions still open — tranche-A fill and time-stop.
    for ticker, pos in list(state.items()):
        # Tranche A fill -> move tranche B's stop to breakeven/trailing.
        # Key off the take-profit leg. Using the parent buy id here meant
        # tranche_a_filled went True within one 5s tick of every entry.
        target_oid = pos.get("tranche_a_target_order_id")
        if not pos.get("breakeven_done") and target_oid:
            order = alpaca_trader.get_order(target_oid)
            if order and order.get("status") == "filled":
                pos["tranche_a_filled"] = True
                if pos.get("qty_b", 0) > 0:
                    trail_pct = pos.get("trail_pct")
                    # Prefer true breakeven when no trail_pct; otherwise trail.
                    be_price = pos.get("entry_price")
                    out = alpaca_trader.replace_stop(
                        ticker, pos.get("tranche_b_stop_order_id"),
                        trail_percent=trail_pct,
                        stop_price=(
                            None if trail_pct
                            else (be_price or pos.get("stop_price"))
                        ),
                    )
                    pos["tranche_b_stop_order_id"] = out.get("order_id")
                    events.append({"ticker": ticker, "event": "scaled_out",
                                   "target_1": pos.get("target_1")})
                pos["breakeven_done"] = True
                changed = True

        # Time stop: first target never hit within the model's own deadline.
        # Guarded on closing_reason so a still-open position (close_out
        # submitted but not yet filled) doesn't re-trigger every tick.
        days = pos.get("time_stop_days")
        if days and not pos.get("tranche_a_filled") and not pos.get("closing_reason"):
            age_days = (now - pos.get("entry_time", now)) / 86400.0
            if age_days > days:
                alpaca_trader.cancel_open_orders(ticker)
                # Keep the closing order id so this exit's fill is resolvable —
                # otherwise the outcome has no price we are willing to trust.
                out = alpaca_trader.close_out(ticker) or {}
                if isinstance(out, dict) and out.get("order_id"):
                    pos["close_order_id"] = str(out["order_id"])
                pos["closing_reason"] = "time_stop"
                events.append({"ticker": ticker, "event": "time_stop",
                              "age_days": round(age_days, 1)})
                changed = True

    if changed:
        _save_state(state)

    try:
        reconcile_broker(now)
    except Exception as e:  # noqa: BLE001
        log_event("reconcile_error", reason=str(e)[:160])

    return events


def performance_summary(since: float | None = None) -> dict[str, Any]:
    """Aggregate ``outcomes.jsonl`` into win rate, realized R, and P&L.

    This is the only place that answers "is the strategy actually working" —
    cost accounting alone can't; you need to know what happened after entry.
    """
    rows: list[dict[str, Any]] = []
    try:
        text = OUTCOMES_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if since is not None and row.get("exit_time", 0) < since:
            continue
        if row.get("realized_r_multiple") is None:
            continue  # no computable risk basis — nothing to grade
        rows.append(row)

    if not rows:
        return {"count": 0}

    wins = [r for r in rows if r["realized_r_multiple"] > 0]
    losses = [r for r in rows if r["realized_r_multiple"] <= 0]
    by_reason: dict[str, int] = {}
    for r in rows:
        reason = r.get("close_reason") or "unknown"
        by_reason[reason] = by_reason.get(reason, 0) + 1

    return {
        "count": len(rows),
        "win_rate": len(wins) / len(rows),
        "avg_r_multiple": sum(r["realized_r_multiple"] for r in rows) / len(rows),
        "avg_r_multiple_wins": (
            sum(r["realized_r_multiple"] for r in wins) / len(wins)
            if wins else None
        ),
        "avg_r_multiple_losses": (
            sum(r["realized_r_multiple"] for r in losses) / len(losses)
            if losses else None
        ),
        "total_realized_pl_usd": sum(
            r["realized_pl_usd"] for r in rows if r.get("realized_pl_usd") is not None
        ),
        "avg_hold_days": sum(r.get("hold_days", 0) for r in rows) / len(rows),
        "by_close_reason": by_reason,
    }


def print_performance_summary(since: float | None = None) -> None:
    s = performance_summary(since=since)
    if s["count"] == 0:
        print("No closed positions yet — nothing to summarize.")
        return
    print(f"Closed positions: {s['count']}")
    print(f"Win rate: {s['win_rate']:.0%}")
    print(f"Avg R-multiple: {s['avg_r_multiple']:+.2f}")
    if s["avg_r_multiple_wins"] is not None:
        print(f"  wins avg: {s['avg_r_multiple_wins']:+.2f}")
    if s["avg_r_multiple_losses"] is not None:
        print(f"  losses avg: {s['avg_r_multiple_losses']:+.2f}")
    print(f"Total realized P&L: ${s['total_realized_pl_usd']:+.2f}")
    print(f"Avg hold: {s['avg_hold_days']:.1f} days")
    print("By close reason:")
    for reason, count in sorted(s["by_close_reason"].items()):
        print(f"  {reason}: {count}")


if __name__ == "__main__":
    print_performance_summary()
