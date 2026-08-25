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
import re
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
from learn_stamps import merge_regime, regime_stamp  # noqa: E402

REPORT_DIR = resolve_report_dir()
POSITIONS_STATE_PATH = REPORT_DIR / "positions_state.json"
OUTCOMES_PATH = REPORT_DIR / "outcomes.jsonl"
EVENTS_PATH = REPORT_DIR / "events.jsonl"
# Counterfactual record: what the desk decided, and what price did next,
# whether or not it ever traded. outcomes.jsonl only grows on a fill, and the
# desk can go a whole session without one (2026-08-06: 530 zones, 31 symbols,
# 0 fills) — which leaves every gate unmeasurable. See log_shadow_sample.
SHADOW_PATH = REPORT_DIR / "shadow.jsonl"
# The buy side's counterpart. Deliberately a SEPARATE file: shadow_report keys
# episodes on (symbol, admit_ts), so held-position rows written into
# shadow.jsonl would fold into the admission episode they came from and inflate
# touch%, armed% and every forward return computed off it. The buy-side
# analytics are the ones that work; do not put them at risk to save a file.
POSITION_SHADOW_PATH = REPORT_DIR / "position_shadow.jsonl"
# The other arm of every selection question: what admission KEPT OUT. A filter
# cannot be judged from the names it let through alone — if the rejects
# outperform, the gate is destroying value and nothing else on disk would say
# so. apply_inclusion_gate computed this list and discarded it.
REJECTS_PATH = REPORT_DIR / "rejects.jsonl"
# Exit-side decision log: one row per open position per tick (MAE/MFE, exit_why).
POSITION_SHADOW_PATH = REPORT_DIR / "position_shadow.jsonl"
OPEN_BELL_STATE_PATH = REPORT_DIR / "open_bell_state.json"
EOD_LIQUIDATE_STATE_PATH = REPORT_DIR / "eod_liquidate_state.json"
SOD_LIQUIDATE_STATE_PATH = REPORT_DIR / "sod_liquidate_state.json"

ET = ZoneInfo("America/New_York")

# Never more than this much of the account on a single trade's stop distance.
DEFAULT_RISK_PCT = 1.0
DEFAULT_STYLE = "Moderate position"
DEFAULT_MIN_REWARD_RISK = 0.5
# Cancel unfilled / unconfirmed entries after this many seconds in RTH.
DEFAULT_UNCONFIRMED_TTL_SEC = 900.0
# Stop new entries when today's realized R from closed AI trades <= -this.
DEFAULT_DAILY_LOSS_LIMIT_R = 3.0
DEFAULT_PDT_PROTECT = "off"  # FINRA PDT ended 2026-06-04; leftover paper counts
# Cap sum of open risk (entry-stop)*qty as % of equity.
DEFAULT_MAX_OPEN_RISK_PCT = 5.0
# Max bid/ask spread % of mid (0 = disabled).
DEFAULT_MAX_SPREAD_PCT = 1.0
# Day-scalp dead trade: minutes held with no meaningful MFE.
DEFAULT_DEAD_TRADE_MIN = 22.0
DEFAULT_DEAD_TRADE_MFE_R = 0.10
# Abort a new fill if last is already through the stop or this far
# under the intended entry (FGI/SPAI/TDIC −2R IEX slips).
DEFAULT_FILL_ABORT_R = 0.15
# Never trail tighter than this many dollars under last — unless that
# dollar floor is wider than min_give_max_r × R (cheap last-mode names).
DEFAULT_LOCAL_TRAIL_MIN_GIVE_PX = 0.06
DEFAULT_LOCAL_TRAIL_MIN_GIVE_MAX_R = 0.20
# Runner (tranche B) trail distance, in R — NOT percent. A fixed percent trail
# is a different trade on every name: at 2.5% it is 2.5R behind a 1%-wide stop
# and 0.5R behind a 5%-wide one, so on the tight double-bottom zones the runner
# risked more than tranche A had just banked and a trade that HIT its target
# still closed red. In R it is the same trade everywhere.
DEFAULT_RUNNER_TRAIL_R = 1.0
# Only rewrite the resting stop when the ratchet gains at least this much R —
# the position tick runs every 5s and each move is a cancel + submit.
DEFAULT_RUNNER_STEP_R = 0.10
# Local profit trail (software). Arm at this MFE, then sit peak − give_r
# under the high. Flatten with close_out — no T1 fill required.
DEFAULT_LOCAL_TRAIL_ARM_R = 0.05
DEFAULT_LOCAL_TRAIL_GIVE_R = 0.10
# Legacy fixed-dollar cushion. 0 = use give_r × R (preferred).
DEFAULT_LOCAL_TRAIL_GIVE_PX = 0.0
# How many seconds of tape sit under the median that lifts the shelf. The ring
# guards against one bad print lifting the shelf into the market, and that
# protection is a span of TIME, not a count of samples — sizing it in ticks
# means every change to the tick rate silently retunes the spike guard.
DEFAULT_LOCAL_TRAIL_DAMP_SEC = 2.0
# Trail width as a multiple of the round-trip spread. 0 = off (shipped).
#
# The shelf is a fixed $0.06 behind price while the book it trades against is
# $0.08-0.18 wide, so on 2026-08-21 the quote alternating between bid and ask
# moved the printed price further than the entire cushion: 62% of RTH moments
# had a spread wider than the trail, median 1.91x. Every fill that day raised
# the shelf and then exited 0.5-4 seconds later a few cents under it — stopped
# out by the quote rather than by the market.
#
# A cushion has to be wider than the noise it is meant to survive, and the
# noise floor of any instrument is its own spread. Sizing the give off R or off
# a percent of price cannot know that; both were set with no reference to the
# book. Set from the record ai_max_spread_r was always waiting for, not guessed.
DEFAULT_LOCAL_TRAIL_SPREAD_K = 0.0
# Ceiling on that spread floor, in R. The floor is applied last and outranks
# every other rule, which is right when the book is 0.06R wide and dangerous
# when it is not: RTH spread_r runs p90 5.56R and pre-market median 3.83R
# (tools/spread_coverage.py, 2026-08-17..21), so an uncapped k=1 would park
# the shelf 5.5R under the peak on exactly the names that need a stop most.
# A cushion that wide is not a wide stop, it is no stop — the position then
# rides to the synthetic 1R or to the 15:50 flatten with nothing in between.
# Cap it: past this, the book is too wide to trade around and the answer is
# to refuse the entry (ai_max_spread_r), not to widen the shelf.
DEFAULT_LOCAL_TRAIL_SPREAD_MAX_R = 0.50
# Breakeven floor may not arm until the trade has cleared this many round
# trips. 0 = off (shipped). Sibling of the give knob above and the same
# lesson: be_at_pct is 0.15% of price against a 0.49% book, so the floor
# fired on a third of the spread and parked the stop a cent above entry.
DEFAULT_BE_AT_SPREAD_K = 0.0
# Breakeven is not flat. A shelf parked exactly at the fill scratches — the
# round-trip costs the spread twice and books zero. Park it this many cents
# ABOVE the fill instead, so a name that ran and came back is still green.
# 0 restores the old flat-at-fill behaviour.
DEFAULT_BREAKEVEN_OFFSET_PX = 0.01
# Flatten longs if stream+REST stay dark this long during RTH.
DEFAULT_STALE_DATA_MAX_AGE_SEC = 15.0
# Keep a small ring of recent events for /api/state.
_EVENT_RING_MAX = 80
_event_lock = threading.Lock()
_recent_events: list[dict[str, Any]] = []
_last_reconcile: dict[str, Any] = {}
# Throttle ratchet_invariant_fail so a stuck book does not write 12/min.
_ratchet_fail_last: dict[str, float] = {}
_RATCHET_FAIL_THROTTLE_SEC = 60.0

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
   - First target at least the desk floor (ai_min_reward_risk; typically 0.5R).
     Do not invent a 3:1 first target — the book scales at ~0.6R.
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


# Serialize load→mutate→save. MLTX (2026-08-11) was entry_ok + filled but
# immediately reconcile_unmanaged: RUM and MLTX saved two seconds apart and
# the second write loaded a stale snapshot, dropping the first symbol — and
# with it every mechanical exit (exhaustion, heal, dead-trade).
_state_lock = threading.RLock()


def _load_state() -> dict[str, Any]:
    """Load managed positions; prefer POSITIONS_STATE_PATH (monkeypatchable)."""
    from ai_paths import find_report_file, resolve_report_dir

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
            resolve_report_dir().resolve(),
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

    Atomic replace so a concurrent reader never sees a half-written file.
    Callers that load→mutate→save must hold ``_state_lock`` (see
    ``_update_state``).
    """
    try:
        POSITIONS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(state, indent=2)
        path = POSITIONS_STATE_PATH
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        try:
            # Fallback if replace fails (some test FS stubs).
            POSITIONS_STATE_PATH.write_text(
                json.dumps(state, indent=2), encoding="utf-8")
        except Exception:
            pass


def _update_state(mutator) -> dict[str, Any]:
    """Load → mutator(state) → save under the process-wide state lock.

    ``mutator`` receives the live dict and may mutate it in place. Returns the
    (same) state dict after save. Use this for every book change so two
    near-simultaneous entry_ok paths cannot drop each other.
    """
    with _state_lock:
        state = _load_state()
        mutator(state)
        _save_state(state)
        return state


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

    Regime stamps (edge_mode, git, config_fp) are merged here so every writer
    path is comparable day-over-day without each call site remembering them.
    """
    try:
        payload = merge_regime(row if isinstance(row, dict) else {})
        SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SHADOW_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        pass


def log_reject_sample(row: dict[str, Any]) -> None:
    """Append one rejected-candidate sample. Fire-and-forget, never raises.

    Prices come off the candidate row the screeners already refreshed, so this
    costs no API call — the desk is over Alpaca's rate limit and the names that
    can trade must not lose quota to the ones that were turned away.
    """
    try:
        payload = merge_regime(row if isinstance(row, dict) else {})
        REJECTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with REJECTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
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


_pdt_guard_inst = None


def _pdt_guard():
    """Restart-proof local day-trade counter. Dollar kill switch stays off."""
    global _pdt_guard_inst
    if _pdt_guard_inst is None:
        from trade_guard import TradeGuard
        _pdt_guard_inst = TradeGuard(
            daily_loss_limit=0.0,
            max_trades_per_day=0,
            pdt_protect="block",
        )
    return _pdt_guard_inst


def _note_day_trade(entry_time: float | None, pnl_dollars: float | None) -> None:
    if entry_time is None:
        return
    try:
        buy_iso = datetime.fromtimestamp(
            float(entry_time), tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _pdt_guard().record_close(float(pnl_dollars or 0.0), buy_iso)
    except (OverflowError, OSError, TypeError, ValueError):
        return


def outcomes_coverage(
    *,
    now: float | None = None,
    lookback_sec: float = 172800.0,
) -> dict[str, Any]:
    """Match recent ``entry_ok`` events to outcome rows.

    An entry is covered when an outcome shares its symbol and an ``entry_time``
    within 2s, or (legacy rows) an ``exit_time`` after that entry. Used at
    trader start so a silent write failure cannot leave the daily-loss brake
    reading an empty file.
    """
    now = time.time() if now is None else float(now)
    cutoff = now - max(0.0, float(lookback_sec))
    entries: list[dict[str, Any]] = []
    try:
        text = EVENTS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or "entry_ok" not in line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(row.get("kind") or "") != "entry_ok":
            continue
        ts = float(row.get("ts") or 0)
        if ts < cutoff:
            continue
        sym = str(row.get("symbol") or "").upper()
        if not sym:
            continue
        entries.append({
            "symbol": sym,
            "ts": ts,
            "entry_time": float(row.get("entry_time") or ts),
        })

    outcomes: list[dict[str, Any]] = []
    try:
        otext = OUTCOMES_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        otext = ""
    for line in otext.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        outcomes.append(row)

    uncovered: list[dict[str, Any]] = []
    for e in entries:
        et = e["entry_time"]
        hit = False
        for o in outcomes:
            if str(o.get("symbol") or "").upper() != e["symbol"]:
                continue
            ot = o.get("entry_time")
            if ot is not None:
                try:
                    if abs(float(ot) - et) <= 2.0:
                        hit = True
                        break
                except (TypeError, ValueError):
                    pass
            xt = o.get("exit_time") or o.get("ts")
            if xt is not None:
                try:
                    if float(xt) >= et:
                        hit = True
                        break
                except (TypeError, ValueError):
                    pass
        if not hit:
            uncovered.append(e)
    return {
        "n_entries": len(entries),
        "n_outcomes": len(outcomes),
        "n_uncovered": len(uncovered),
        "uncovered": uncovered,
    }


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
    stop_price: float | None = None,
    max_spread_r: float | None = None,
    pdt_protect: str | None = None,
    broker_daytrade_count: int | None = None,
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

    # The same cost, denominated in the trade's own risk unit. A percent-of-mid
    # cap answers "is this book wide for a $50 stock", which is not the question
    # — the question is what fraction of the money at risk the round trip costs.
    # On a double-bottom zone 1R is under 1% of price, so a spread that reads as
    # negligible against price can be half of 1R.
    #
    # Crossing is paid twice: buy at the ask, sell at the bid.
    if (
        max_spread_r is not None
        and float(max_spread_r) > 0
        and bid is not None
        and float(bid) > 0
        and stop_price is not None
        and 0 < float(stop_price) < float(ask)
    ):
        risk = float(ask) - float(stop_price)
        round_trip_r = 2.0 * (float(ask) - float(bid)) / risk
        if round_trip_r > float(max_spread_r) + 1e-12:
            return False, (
                f"spread_r_{round_trip_r:.2f}>{float(max_spread_r):g}"
            )

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

    mode = pdt_protect
    if mode is None:
        try:
            mode = str(_entry_cfg().get("ai_pdt_protect", DEFAULT_PDT_PROTECT)
                       or DEFAULT_PDT_PROTECT)
        except Exception:
            mode = DEFAULT_PDT_PROTECT
    broker_count = broker_daytrade_count
    broker_eq = account_equity
    if broker_count is None:
        try:
            import alpaca_trader as _at
            st = _at.get_pdt_status()
            if isinstance(st, dict):
                if st.get("daytrade_count") is not None:
                    broker_count = int(st["daytrade_count"])
                if st.get("equity"):
                    broker_eq = float(st["equity"])
        except Exception:
            pass
    from trade_guard import pdt_gate
    pdt_ok, pdt_why = pdt_gate(
        mode=str(mode or "block"),
        broker_daytrade_count=broker_count,
        equity=broker_eq,
        local_day_trades=_pdt_guard().day_trades_5d(),
    )
    if not pdt_ok:
        return False, pdt_why

    open_r = open_risk_pct(account_equity)
    # Proposed trade risk ~ risk_pct of equity (by design of size_by_risk).
    if open_r + float(risk_pct) > float(max_open_risk_pct) + 1e-9:
        return False, f"open_risk_pct_{open_r:.2f}+{risk_pct:g}"

    state = _load_state()
    if sym in state and not state[sym].get("closing_reason"):
        return False, "already_managed"

    return True, ""


def _entry_cfg() -> dict[str, Any]:
    """Live config for entry shaping / order shape. {} on any failure.

    Local to this module on purpose: ai_suggest has a same-named helper, but
    importing it here would pull the research stack into the order path for
    what is a plain config read — and ai_positions is imported by
    ai_entry_watch on every poll.
    """
    try:
        from config import load_config
        return load_config() or {}
    except Exception:  # noqa: BLE001
        return {}


def _entry_limit_price(
    current_ask: float | None,
    entry_high: float,
    entry_low: float,
    *,
    cap_at_zone: bool = True,
    current_bid: float | None = None,
) -> float | None:
    """Marketable limit for the entry, capped at the zone top. None = market.

    A market entry fills at whatever the ask is at execution, which quietly
    breaks the sizing contract: size_by_risk sizes off current_ask and the stop
    is derived from that same ask, so a fill above the quote makes real risk
    exceed ai_watch_synth_stop_pct and notional exceed ai_max_position_pct.
    These are thin IEX books on high-RVOL names, where that gap is largest.

    Pad makes it marketable so it still fills in a normal book; the zone cap
    makes "we only ever fill inside the entry zone" structurally true rather
    than best-effort. Desk clicks skip the cap — the click is a chase.
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
    # Where the limit is anchored. "ask" (default) is marketable and pays the
    # whole book on the way in, which is why every fill opens showing a loss
    # the size of the spread before the market has done anything: FCX filled
    # 76.26 against a 76.22 print, TGTX 56.21 against 56.17.
    #
    # Crossing buys immediacy, and immediacy is worth its price only when the
    # signal actually continues. On this tape it does not — every entry rule
    # screened at chance — so the desk has been paying the spread for a
    # certainty it gains nothing from. Median spread is 0.058R against a median
    # MFE-minus-spread of -0.038R, so "mid" recovers roughly three quarters of
    # the shortfall on its own, and "bid" pays nothing but only fills when
    # someone comes to it.
    #
    # Sizing is unaffected either way: the caller still sizes off current_ask,
    # so a passive anchor can only fill BETTER than the contract assumed.
    anchor = str(cfg.get("ai_entry_limit_anchor", "ask") or "ask").lower().strip()
    try:
        bid = float(current_bid or 0)
    except (TypeError, ValueError):
        bid = 0.0
    if anchor in ("mid", "bid") and bid > 0 and bid < ask:
        # No pad here. The pad exists to make an ask-anchored order marketable;
        # adding it to a passive anchor walks the price straight back to the
        # ask and buys nothing. A passive order is supposed to wait.
        px = (ask + bid) / 2.0 if anchor == "mid" else bid
    else:
        px = ask * (1.0 + pad)
    if cap_at_zone and top > 0:
        px = min(px, top)
    px = round(px, 2)
    return px if px > 0 else None


def desk_click(symbol: str) -> dict[str, Any]:
    """Operator click on the book: flatten if long, else force a buy.

    Bypasses the arm/exhaustion gates. Uses the watch structure when it
    exists; otherwise a synth band under the last print. Zone membership
    is not a veto — the click is the decision.
    """
    import alpaca_trader
    import ai_trading as gt
    import ai_entry_watch as ew

    sym = str(symbol or "").upper().strip()
    if not sym or not sym.replace(".", "").isalnum() or len(sym) > 8:
        return {"ok": False, "error": "invalid symbol", "action": None}

    cfg = _entry_cfg()
    # Dashboard process does not start the paper client. Auto-arms live in
    # the suggest/poller process, so a book click used to see TRADER_MODE=off
    # and report that as "not tradable".
    if not gt.is_ready() or not alpaca_trader.is_active():
        try:
            amt = float(cfg.get("ai_trade_amount", 1000) or 1000)
        except (TypeError, ValueError):
            amt = 1000.0
        try:
            maxp = int(cfg.get("ai_max_positions", 4) or 4)
        except (TypeError, ValueError):
            maxp = 8
        try:
            slot_eq = float(cfg.get("ai_position_slot_equity", 250.0) or 250.0)
        except (TypeError, ValueError):
            slot_eq = 250.0
        try:
            max_pct = float(cfg.get("ai_max_position_pct", 8.0) or 8.0)
        except (TypeError, ValueError):
            max_pct = 8.0
        try:
            mode = gt.init_for_ai(
                trade_amount=amt, max_positions=maxp, extended_hours=True,
                slot_equity=slot_eq, max_position_pct=max_pct)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "action": None,
                    "error": f"trader init failed: {str(e)[:160]}"}
        if mode != "paper" or not alpaca_trader.is_active():
            return {"ok": False, "action": None,
                    "error": "paper trader not ready in this process"}

    if gt.has_open_position(sym):
        try:
            out = alpaca_trader.close_out(sym) or {}
        except Exception as e:  # noqa: BLE001
            log_event("desk_flatten_fail", symbol=sym, reason=str(e)[:200])
            return {"ok": False, "action": "flatten", "error": str(e)[:200]}
        log_event("desk_flatten", symbol=sym, source="book_click")
        ok = bool(out.get("ok", True))
        return {
            "ok": ok,
            "action": "flatten",
            "symbol": sym,
            "error": None if ok else str(out.get("error") or out.get("note") or "flatten failed")[:200],
        }

    # Desk click still tries a limit if the clock is dark / extended.
    # Regular-session auto-arms stay gated in place_scaled_entry.

    rec = (ew.load_watch() or {}).get(sym) or {
        "symbol": sym, "status": "watching", "source": "desk",
    }
    px, _src, _age = ew.decision_price(sym, cfg)
    try:
        ask = float(px or rec.get("last_ask") or 0)
    except (TypeError, ValueError):
        ask = 0.0
    if ask <= 0:
        return {"ok": False, "action": "buy", "error": "no quote"}

    structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else None
    if not ew._structure_usable(structure):
        try:
            sev = ew.ensure_offset_zone_if_needed(rec, ask, cfg, time.time())
            if sev:
                log_event("desk_synth_zone", symbol=sym, **{
                    k: sev.get(k) for k in ("entry_low", "entry_high", "reason")
                    if k in sev
                })
        except Exception:
            pass
        structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else None
    if not ew._structure_usable(structure):
        return {"ok": False, "action": "buy", "error": "no structure"}

    decision = ew._decision_for_place(structure, ask=ask, cfg=cfg)
    decision["entry_path"] = "desk_click"
    decision["desk_force"] = True
    decision["source"] = rec.get("duel_source") or rec.get("source") or "desk"

    try:
        acct = gt.get_account() or {}
        equity = float(acct.get("equity") or acct.get("portfolio_value") or 0)
    except Exception:
        equity = 0.0
    if equity <= 0:
        equity = 10_000.0
    try:
        risk_pct = float(cfg.get("ai_risk_pct", DEFAULT_RISK_PCT) or DEFAULT_RISK_PCT)
    except (TypeError, ValueError):
        risk_pct = DEFAULT_RISK_PCT

    result = place_scaled_entry(
        sym, decision, equity,
        risk_pct=risk_pct,
        current_ask=ask,
        duel_source=str(decision.get("source") or "") or None,
    )
    if isinstance(result, dict) and result.get("ok"):
        log_event("desk_buy", symbol=sym, source="book_click",
                  stop=result.get("stop_price"), ask=ask)
        try:
            gt.record_external_buy(sym, {
                "reason": "desk_click",
                "stop_price": result.get("stop_price"),
                "target_1": result.get("target_1"),
                "source": "desk_click",
            })
        except Exception:
            pass
        return {
            "ok": True, "action": "buy", "symbol": sym,
            "stop_price": result.get("stop_price"),
            "qty": result.get("qty") or result.get("parent_qty"),
        }
    err = ""
    if isinstance(result, dict):
        err = str(result.get("error") or result.get("note") or "place failed")[:200]
    err = err or "place failed"
    log_event("desk_buy_fail", symbol=sym, reason=err, ask=ask)
    return {"ok": False, "action": "buy", "symbol": sym, "error": err}


def place_scaled_entry(
    ticker: str,
    decision: dict[str, Any],
    account_equity: float,
    *,
    risk_pct: float = DEFAULT_RISK_PCT,
    current_ask: float | None = None,
    current_bid: float | None = None,
    duel_source: str | None = None,
) -> dict[str, Any]:
    """Execute a qualifying BUY with stop (+ dual scale-out bookkeeping).

    Option A (day scalp dual): one parent buy for full size with a hard stop.
    After fill, ``manage_open_positions`` rests a partial limit SELL for
    ``scale_out_pct`` at T1; when that banks, the stop on the remainder moves
    to ``max(breakeven, peak − ai_runner_trail_r × R)`` and ratchets up.
    A second protected buy is never submitted (Alpaca wash-trade).

    Day Scalp rule: no entry without a hard stop *and* a sell strategy
    (T1 and/or left_overbought). Bare longs are refused.
    """
    import alpaca_trader

    # Alpaca rejects bracket orders (and plain market orders) outside regular
    # trading hours — both tranches need one or the other. A pre-market BUY
    # verdict is discarded rather than attempted and failing partway through.
    if not alpaca_trader.market_is_open() and not bool(
            decision.get("desk_force") or decision.get("skip_zone")):
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
    cfg = _entry_cfg()
    try:
        import desk_product as _desk_product
        _prod_block = _desk_product.arm_block_reason(cfg)
    except Exception:
        _prod_block = None
    if _prod_block and not bool(decision.get("desk_force")):
        log_event("entry_fail", symbol=ticker, reason=_prod_block)
        return {"ok": False, "error": _prod_block, "action": "buy",
                "symbol": ticker}

    try:
        default_scale = float(cfg.get("ai_watch_synth_scale_out_pct", 50) or 50)
    except (TypeError, ValueError):
        default_scale = 50.0
    scale_out_pct = max(
        0.0, min(100.0, float(decision.get("scale_out_pct") or default_scale)))
    default_trail = _opt_float(cfg.get("ai_watch_synth_trail_pct"), 2.5)
    trail_pct = _opt_float(decision.get("trail_pct"), default_trail)
    # Runner protection is denominated in R, not percent — see
    # _runner_stop_level(). trail_pct is retained for display/telemetry only.
    runner_trail_r = max(0.0, _opt_float(
        cfg.get("ai_runner_trail_r"), DEFAULT_RUNNER_TRAIL_R))

    # Same geometry as should_arm_buy: in-band *or* armable below-zone dip
    # (through the floor toward the stop). Strict [low, high] used to reject
    # the fill a tick after the arm gate had already accepted the dip.
    cfg_zone = _entry_cfg()
    skip_zone = bool(decision.get("desk_force") or decision.get("skip_zone"))
    if not skip_zone:
        try:
            from ai_entry_watch import arm_at_last
            skip_zone = arm_at_last(cfg_zone)
        except Exception:
            pass
    if current_ask is not None and not skip_zone:
        try:
            from ai_entry_watch import ask_triggers_zone, arm_below_max_r
            zone_ok = ask_triggers_zone(
                float(current_ask), entry_low, entry_high,
                stop=stop_price,
                max_below_r=arm_below_max_r(cfg_zone),
                arm_below=bool(cfg_zone.get("ai_watch_arm_below_zone", True)),
            )
        except Exception:
            zone_ok = (
                entry_low <= float(current_ask) <= max(entry_high, entry_low)
            )
        if not zone_ok:
            err = (
                f"price ${float(current_ask):.2f} left the entry zone "
                f"${entry_low:.2f}-${entry_high:.2f} "
                f"(stop ${stop_price:.2f}) before the order could go in"
            )
            log_event("entry_fail", symbol=ticker, reason=err)
            return {"ok": False, "error": err}

    # Size against the price the order will actually fill at, not the zone
    # bound — current_ask is already validated as armable (in or below zone).
    sizing_entry = current_ask or entry_high or entry_low

    risk_px = max(0.0, float(sizing_entry) - float(stop_price or 0))
    if current_ask is not None and fill_already_dead(
        current_ask, sizing_entry, stop_price, risk_px, cfg=cfg,
    ):
        err = (
            f"refused: last ${float(current_ask):.2f} already through "
            f"stop ${float(stop_price):.2f} or ≥"
            f"{_abort_r(cfg):.2f}R under entry"
        )
        log_event("entry_fail", symbol=ticker, reason=err)
        return {"ok": False, "error": err}
    # Tape can already be through while the REST ask we sized on is stale
    # (FGI/SPAI/TDIC). Same abort, against the live print.
    tape_px = _live_tape_px(ticker)
    if tape_px is not None and fill_already_dead(
        tape_px, sizing_entry, stop_price, risk_px, cfg=cfg,
    ):
        err = (
            f"refused: tape ${float(tape_px):.2f} already through "
            f"stop ${float(stop_price):.2f} or ≥"
            f"{_abort_r(cfg):.2f}R under entry"
        )
        log_event("entry_fail", symbol=ticker, reason=err)
        return {"ok": False, "error": err}

    # Capital protection: refuse any entry without a defined stop and sell plan.
    if stop_price <= 0 or target_1 <= 0 or sizing_entry <= 0:
        err = "refused: stop and target_1 required (no naked long / no sell plan)"
        log_event("entry_fail", symbol=ticker, reason=err)
        return {"ok": False, "error": err}
    if stop_price >= sizing_entry:
        # A fill through the planned stop is already dead. Rebasing a new
        # floor under it (08-17 DUOT/SWMR) opened 18 trades that flattened
        # in ~9s. Refuse; do not invent a stop after the thesis is gone.
        err = (
            f"refused: stop ${float(stop_price):.4f} at/above entry "
            f"${float(sizing_entry):.4f} (dead fill)"
        )
        log_event("entry_fail", symbol=ticker, reason=err)
        return {"ok": False, "error": err}
    if target_1 <= sizing_entry:
        err = (
            f"refused: target_1 ${target_1:.4f} must be above entry "
            f"${sizing_entry:.4f} (sell strategy required)"
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

    def _held_qty(ticker_s: str) -> float:
        try:
            pos = alpaca_trader.get_open_positions() or {}
            row = pos.get(ticker_s) or pos.get(ticker_s.upper())
            if isinstance(row, dict):
                return abs(float(row.get("qty") or 0))
        except Exception:
            pass
        return 0.0

    _clear_open(ticker)
    # Refuse a second long while shares (or a residual close) are still live.
    # 2026-08-11: wash-fail path kept calling place while a prior fill sat open,
    # then close_out + re-arm looped on QMCO ~15 times in five minutes.
    held = _held_qty(ticker)
    if held > 0:
        err = f"refused: already holding {held:g} sh (clear before re-entry)"
        log_event("entry_fail", symbol=ticker, reason=err)
        return {"ok": False, "error": err, "ticker": ticker}

    from desk_risk import cap_long_qty, limits_from_cfg

    limits = limits_from_cfg(account_equity, cfg)
    max_pos_pct = float(limits.max_position_pct)
    try:
        cheap_px = float(cfg.get("ai_watch_cheap_price", 5.0) or 0.0)
    except (TypeError, ValueError):
        cheap_px = 5.0
    cheap_pct = float(limits.max_position_pct_cheap)
    if cheap_px > 0 and cheap_pct > 0 and sizing_entry < cheap_px:
        if max_pos_pct <= 0 or cheap_pct < max_pos_pct:
            max_pos_pct = cheap_pct

    risk_qty = alpaca_trader.size_by_risk(
        account_equity, risk_pct, sizing_entry, stop_price)
    total_qty = cap_long_qty(
        risk_qty,
        equity=account_equity,
        price=sizing_entry,
        max_position_pct=max_pos_pct,
    )
    if total_qty != risk_qty:
        log_event(
            "size_capped", symbol=ticker, risk_qty=risk_qty,
            capped_qty=total_qty, max_position_pct=max_pos_pct,
            equity=round(float(account_equity), 2),
            max_positions=limits.max_positions,
        )
    if total_qty <= 0:
        err = (
            f"risk-sized qty rounded to 0 shares"
            if risk_qty <= 0
            else f"position cap {max_pos_pct:g}% of equity rounds to 0 shares"
        )
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

    # Option A day scalp: dual *bookkeeping* on ONE parent buy (full size +
    # stop). Partial T1 is attached after fill so we never submit a second
    # protected buy (Alpaca wash 40310000). scale_out ≥99 or dual off → one
    # leg; optional full-size broker TP when not splitting.
    dual = bool(cfg.get("ai_day_scalp_dual_tranche", True))
    late_hold = bool(decision.get("late_hold"))
    try:
        import desk_product as _desk_product
        import desk_h4 as _desk_h4
        if (_desk_product.product(cfg) == _desk_product.H4_SWING
                and _desk_product.h4_paper(cfg)):
            decision = _desk_h4.stamp_decision(decision, cfg)
    except Exception:
        pass
    if late_hold or bool(decision.get("h4_swing")):
        # Hold products: no T1 scale-out, no 0.10R shelf identity.
        dual = False
        scale_out_pct = 0.0
    logical_dual = (
        dual
        and scale_out_pct < 99.0
        and total_qty >= 2
    )
    if logical_dual:
        qty_a = max(1, int(total_qty * scale_out_pct / 100.0))
        qty_b = int(total_qty) - qty_a
        if qty_b <= 0:
            qty_a = int(total_qty)
            qty_b = 0
            logical_dual = False
    else:
        qty_a = int(total_qty)
        qty_b = 0

    entry_limit = _entry_limit_price(
        current_ask, entry_high, entry_low, cap_at_zone=not skip_zone,
        current_bid=current_bid)
    broker_target = bool(cfg.get("ai_entry_broker_target", False))
    # Dual: stop-only parent; partial T1 after fill. Single: optional full TP.
    place_target = None if logical_dual else (target_1 if broker_target else None)
    t1_attach_pending = bool(logical_dual and broker_target and qty_a > 0)

    # Protective shape: stop-MARKET by default (gap through the trigger
    # still fills). stop-LIMIT is opt-in via ai_stop_use_market=False.
    use_stop_mkt = bool(cfg.get("ai_stop_use_market", True))
    parent_qty = int(total_qty)
    broker_stop = bool(cfg.get("ai_broker_stop_enabled", True))

    def _place_parent():
        if not broker_stop:
            lim = entry_limit if entry_limit is not None else current_ask
            if not lim or lim <= 0:
                return {"ok": False, "status": "no_limit"}
            out = alpaca_trader.buy_limit_at_price(
                ticker, float(lim),
                dollar_amount=float(parent_qty) * float(lim) + 0.01,
                note="local_stop_only") or {}
            if out.get("ok"):
                out["buy_order_id"] = out.get("order_id")
                out["stop_order_id"] = None
                out["target_order_id"] = None
            return out
        if entry_limit is not None:
            return alpaca_trader.buy_limit_bracket(
                ticker, parent_qty, limit_price=entry_limit,
                stop_price=stop_price, target_price=place_target,
                stop_market=use_stop_mkt)
        return alpaca_trader.buy_bracket_exact(
            ticker, parent_qty, stop_price=stop_price, target_price=place_target)

    result_a = _place_parent()
    if not result_a.get("ok"):
        err = str(result_a.get("note") or result_a.get("error")
                  or result_a.get("status") or "tranche_a_failed")
        # Wash-trade / leftover legs: clear again and retry ONCE. On second
        # wash fail, cancel/flatten residual buys and surface wash_trade so
        # the poller can cool the symbol down (not re-arm every 20s).
        wash = "wash" in err.lower() or "40310000" in err
        if wash:
            _clear_open(ticker)
            # If a naked buy filled between cancel and reject, close it.
            if _held_qty(ticker) > 0:
                try:
                    alpaca_trader.close_out(ticker)
                except Exception as e:  # noqa: BLE001
                    log_event("entry_wash_close_warn", symbol=ticker,
                              reason=str(e)[:200])
            result_a = _place_parent()
            if result_a.get("ok"):
                err = ""
                wash = False
            else:
                err = str(result_a.get("note") or result_a.get("error")
                          or result_a.get("status") or "tranche_a_failed")
                wash = "wash" in err.lower() or "40310000" in err
                if wash:
                    _clear_open(ticker)
                    if _held_qty(ticker) > 0:
                        try:
                            alpaca_trader.close_out(ticker)
                        except Exception:
                            pass
                    err = "wash_trade"
        if err:
            log_event("entry_fail", symbol=ticker, reason=err, leg="A")
            return {
                "ok": False, "error": err, "ticker": ticker,
                "tranche_a": result_a, "tranche_b": None,
            }

    # Stop-only or OTOCO parent. Missing leg ids are healed after fill; do not
    # roll back a live buy for an empty echo field.
    if not result_a.get("buy_order_id"):
        err = "refused: buy order id missing after place"
        log_event("entry_fail", symbol=ticker, reason=err, leg="A")
        return {
            "ok": False, "error": err, "ticker": ticker,
            "tranche_a": result_a, "tranche_b": None,
        }
    if place_target and not result_a.get("target_order_id"):
        log_event(
            "entry_warn", symbol=ticker,
            reason="target_order_id_missing_from_response",
            note="OTOCO submitted with target; will reconcile/heal if naked",
        )
    if not result_a.get("stop_order_id"):
        log_event(
            "entry_warn", symbol=ticker,
            reason="stop_order_id_missing_from_response",
            note="heal will attach stop if still unprotected after fill",
        )

    # Runner is bookkeeping only — same parent order / same resting stop.
    result_b = {
        "ok": True,
        "buy_order_id": None,
        "stop_order_id": result_a.get("stop_order_id") if qty_b > 0 else None,
    }

    src = duel_source or decision.get("duel_source") or decision.get("source")
    strategy = (
        decision.get("strategy")
        or ("day_scalp_v0" if decision.get("synthetic") else "research")
    )
    if late_hold:
        strategy = "late_hold"
    h4_swing = bool(decision.get("h4_swing")) or str(strategy or "") == "h4_swing"
    if h4_swing:
        strategy = "h4_swing"
        dual = False
        scale_out_pct = 0.0
        logical_dual = False
    row = {
        "qty_a": qty_a,
        "qty_b": qty_b,
        "total_qty": total_qty,
        "entry_price": sizing_entry,
        "entry_limit_price": entry_limit,
        "tranche_a_order_id": result_a.get("buy_order_id"),
        # The take-profit leg — NOT the parent buy. "Has tranche A scaled out?"
        # must key off this; the parent fills at entry. Dual path attaches this
        # after fill (t1_attach_pending).
        "tranche_a_target_order_id": result_a.get("target_order_id"),
        "tranche_a_stop_order_id": result_a.get("stop_order_id"),
        "tranche_b_order_id": result_b.get("buy_order_id"),
        # Same resting stop as A until scale-out rewrites it for the runner.
        "tranche_b_stop_order_id": result_b.get("stop_order_id"),
        "t1_attach_pending": t1_attach_pending,
        "logical_dual": bool(qty_b > 0),
        "stop_price": stop_price,
        # The R basis, frozen at entry. Every later stop move (breakeven on a
        # sell_signal, the runner ratchet) rewrites stop_price, and R measured
        # against a moving stop is not R: a stop lifted to entry makes
        # entry-stop zero, which silently dropped the trade out of realized_r
        # and therefore out of the daily-loss gate.
        "risk_per_share": max(0.0, sizing_entry - stop_price),
        # Disaster floor — never overwritten when stop_price ratchets.
        "entry_stop_price": stop_price,
        "local_stop_price": (
            stop_price if (late_hold or h4_swing)
            else (initial_local_stop(sizing_entry, risk_px, cfg) or stop_price)
        ),
        "late_hold": late_hold,
        "h4_swing": h4_swing,
        "target_1": target_1,
        "trail_pct": trail_pct,
        "runner_trail_r": runner_trail_r,
        "runner_stop_price": None,
        "peak_price": None,
        "time_stop_days": int(decision.get("time_stop_days") or 0) or None,
        "entry_time": time.time(),
        "tranche_a_filled": False,
        "breakeven_done": False,
        "reward_risk": decision.get("reward_risk"),
        "summary": decision.get("summary"),
        "strategy": strategy,
        # Carried from the zone structure so the outcome row can be sliced by
        # geometry (double_bottom vs pullback_band). "strategy" is too coarse:
        # both are day_scalp_v0.
        "zone_kind": decision.get("zone_kind"),
        # Exhaustion at the moment of entry. The rule that authorised the trade,
        # recorded next to its result — otherwise "bought at 85%" and "bought at
        # 55% and rising" average into one number and the heat floor stays a guess.
        "entry_exhaustion": decision.get("entry_exhaustion"),
        "entry_exhaustion_state": decision.get("entry_exhaustion_state"),
        # Which desk opened this. ai_entry_watch runs the exhaustion gate;
        # ai_suggest does not (it has its own pre-entry / reward-risk stack)
        # and stamps no %R. Both land in one outcomes.jsonl, where they were
        # indistinguishable — so a slice by entry_exhaustion_state silently
        # mixed gated and ungated rows. Named at the source instead.
        "entry_path": decision.get("entry_path") or "unknown",
        # Overbought-only entries: arm means we already tagged the band, so the
        # left_overbought exit is armed from the first position tick. Without
        # this latch a name that rolls under the band before the first poll
        # would hit never_overbought and never sell on exhaustion.
        "exh_was_overbought": (
            str(decision.get("entry_exhaustion_state") or "").lower()
            == "overbought"
            or bool(decision.get("exh_was_overbought"))
        ),
        "scale_out_pct": scale_out_pct,
        # Decision-time feature vector (ai_entry_watch._entry_features), held
        # so the outcome record can land denormalized — features and result on
        # one row. A join against events.jsonl would work until a symbol is
        # entered twice in a session, which happens.
        "features": decision.get("features"),
        # Frozen at open so the outcome can slice realtime vs fallback
        # without a join. Missing on a fill means we cannot score gate 1.
        "pctr_src": (decision.get("features") or {}).get("pctr_src"),
        "cm_rsi_src": (decision.get("features") or {}).get("cm_rsi_src"),
        "bars_age_sec": (decision.get("features") or {}).get("bars_age_sec"),
        # Set once we've observed the position actually open — guards the
        # closure check below from mistaking "order hasn't filled yet" for
        # "position closed" on the very first tick after entry.
        "entry_confirmed": False,
        "last_seen_price": None,
        "closing_reason": None,
        "duel_source": src,
        "mae_r": None,
        "mfe_r": None,
        "sell_signal_stop_done": False,
        # Frozen at open so the outcome row can slice hybrid vs continuation
        # without guessing from calendar dates.
        **regime_stamp(),
    }

    def _put(st: dict[str, Any]) -> None:
        st[ticker] = row

    _update_state(_put)
    log_event(
        "entry_ok", symbol=ticker, qty_a=qty_a, qty_b=qty_b,
        stop_price=stop_price, target_1=target_1, entry_price=sizing_entry,
        duel_source=src, strategy=strategy, dual=bool(qty_b > 0),
        t1_attach_pending=t1_attach_pending,
        parent_qty=parent_qty,
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
        "strategy": strategy,
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


def _opt_float(value: Any, default: float) -> float:
    """float(value), treating only missing/blank/unparseable as *default*.

    ``float(x or default)`` cannot express a deliberate zero: it is how
    ``ai_watch_synth_trail_pct: 0`` ("no trail, use breakeven") silently came
    back as 2.5%, and no value in bot_config.json could turn the trail off.
    Every knob that legitimately accepts 0 must read through here.
    """
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _cfg_flag(key: str, default: bool = True) -> bool:
    """One config read, never raising — these run inside the position tick."""
    try:
        from config import load_config
        return bool(load_config().get(key, default))
    except Exception:
        return bool(default)


def _cfg_all() -> dict:
    """Whole config, never raising — for helpers that take a cfg dict."""
    try:
        from config import load_config
        return load_config() or {}
    except Exception:
        return {}


def _sell_signal_defends(state: dict) -> bool:
    """Whether to let a live sell_signal tighten stops. Off = old behaviour.

    Also off under the exhaustion rules. sell_signal mixes MACD and CM RSI-2
    with %R, so letting it ratchet stops would put two of the indicators the
    operator excluded back in charge of the exit — by a different door than
    the entry gate they were removed from.
    """
    # Under the exhaustion rules this defence is off for names that HAVE a %R
    # reading — sell_signal mixes MACD and CM RSI-2 with %R, and those were
    # excluded deliberately. Names with no reading keep it: they are running on
    # the pre-exhaustion logic, and taking their only indicator defence away
    # while giving them no replacement would leave them worse off than before.
    if _cfg_flag("ai_watch_exhaustion_rules", True):
        return _cfg_flag("ai_watch_exhaustion_fallback", True)
    if not state:
        return False
    return _cfg_flag("ai_sell_signal_breakeven", True)


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


def _is_protective_stop_order(order: dict | None) -> bool:
    """True when *order* is a resting SELL stop that can exit a long.

    A take-profit LIMIT sell is not protection: it only works if price goes
    up. RIOT-style books with an upper limit and no stop were treated as
    protected because ANY sell counted — heal never fired and the position
    sat naked on the downside.
    """
    if not isinstance(order, dict):
        return False
    if str(order.get("side") or "").lower() != "sell":
        return False
    otype = str(order.get("type") or "").lower().replace("-", "_")
    # stop, stop_limit, trailing_stop, stop_loss — not plain "limit".
    return "stop" in otype


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
            if (str(o.get("symbol") or "").upper() == sym
                    and _is_protective_stop_order(o)):
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


def _broker_exit_fill(ticker: str, since_ts: float | None) -> dict | None:
    """The SELL fill that actually closed *ticker*, straight from the broker.

    The desk can only recognise exits it placed itself, which is the minority
    of them: a hand-liquidated position, an EOD flatten, a bracket leg whose id
    was lost to a restart, or anything cancelled and replaced all resolve to
    nothing. On 2026-08-07 that was every trade — four outcomes, four null exit
    prices, four null R. The fills were sitting in the broker's history the
    whole time.

    Returns the fill dict (symbol/qty/type/filled_avg_price/filled_at) or None.
    """
    try:
        import alpaca_trader
        sym = str(ticker or "").upper()
        best = None
        best_ts = -1.0
        for f in alpaca_trader.get_filled_orders(limit=200, days=2) or []:
            if str(f.get("symbol") or "").upper() != sym:
                continue
            if str(f.get("side") or "").lower() != "sell":
                continue
            px = _num(f.get("filled_avg_price"))
            if not px or px <= 0:
                continue
            ts = _fill_ts(f.get("filled_at"))
            # Only fills after this position opened — an earlier round trip in
            # the same name would otherwise price this one.
            if since_ts and ts and ts < float(since_ts):
                continue
            if ts is None or ts > best_ts:
                best, best_ts = f, (ts if ts is not None else best_ts)
        return best
    except Exception:
        return None


def _fill_ts(raw: Any) -> float | None:
    """Epoch seconds from a broker timestamp, or None. Never raises."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _log_position_shadow(ticker: str, pos: dict[str, Any], price: float | None,
                         sig: dict | None, exit_why: str, now: float) -> None:
    """One row per tick per OPEN position — the exit side's decision log.

    The buy side samples every candidate every poll and records why it did not
    arm, which is what makes "should we have bought this?" answerable. The
    moment a position opened it left the telemetry completely: 4099 shadow rows
    on 2026-08-07, every one of them status=watching, none for anything held.
    So there was no way to ask "should we have sold there?" — only a terminal
    outcome row saying what happened, with nothing about the decision to keep
    holding on any of the ticks before it.

    `exit_why` is the mirror of the buy side's `arm_why`: what the exit
    machinery concluded on THIS tick, including "hold". Without the refusals
    the log only shows exits that happened and can never price the ones that
    did not.

    MAE/MFE are carried on the position and stamped here because they cannot be
    reconstructed afterwards — an outcome row knows the entry and the exit and
    nothing about the worst and best prices in between, which is exactly what
    says whether a stop was too tight or a target left money behind.
    """
    try:
        entry = _num(pos.get("entry_price")) or 0.0
        stop = _num(pos.get("stop_price")) or 0.0
        target = _num(pos.get("target_1")) or 0.0
        px = _num(price)
        risk = entry - stop
        sig = sig if isinstance(sig, dict) else {}
        row = {
            "ts": now,
            "symbol": ticker,
            "exit_why": exit_why,
            "price": px,
            "entry_price": entry or None,
            "stop_price": stop or None,
            "target_1": target or None,
            "hold_sec": round(now - float(pos.get("entry_time") or now), 1),
            # Where the trade stands, in the unit the desk sizes on.
            "unrealized_r": ((px - entry) / risk) if (px and entry and risk > 0) else None,
            "unrealized_pct": (100.0 * (px - entry) / entry) if (px and entry) else None,
            "pct_to_stop": (100.0 * (px - stop) / px) if (px and stop) else None,
            "pct_to_target": (100.0 * (target - px) / px) if (px and target) else None,
            # Excursions — unreconstructable after the fact.
            "mae_r": _num(pos.get("mae_r")),
            "mfe_r": _num(pos.get("mfe_r")),
            # THE RATCHET. `stop_price` above is the 5% plan stop; the thing
            # that actually ends these trades is the working shelf, and until
            # 2026-08-23 it was never written down — every ratchet study had
            # to reconstruct it from bars. peak_price is what the shelf
            # trails off, so peak - shelf is the give ACTUALLY in force,
            # which stopped being a constant when give_spread_k went live.
            "local_stop_price": _num(pos.get("local_stop_price")),
            "peak_price": _num(pos.get("peak_price")),
            "runner_stop_price": _num(pos.get("runner_stop_price")),
            "shelf_r": (
                (px - _num(pos.get("local_stop_price"))) / risk
                if (px and risk > 0
                    and _num(pos.get("local_stop_price")) is not None)
                else None),
            "give_r": (
                (_num(pos.get("peak_price")) - _num(pos.get("local_stop_price"))) / risk
                if (risk > 0 and _num(pos.get("peak_price")) is not None
                    and _num(pos.get("local_stop_price")) is not None)
                else None),
            # The book this position crossed on the way in, and will cross on
            # the way out. Present on shadow since 8/21 and absent from the
            # exit side entirely, which is why exit-side spread coverage was 8%.
            "spread_r": _pos_spread_r(pos),
            # GATE 1's mechanism. min_hold_active is the state; min_hold_blocks
            # is how many exits the delay has actually suppressed on this
            # position. Without the second, the experiment has no mechanism.
            "min_hold_active": soft_exit_held_back(pos, now),
            "min_hold_blocks": int(pos.get("min_hold_blocks") or 0),
            "min_hold_last": pos.get("min_hold_last"),
            # The same indicator wire the entry gate reads, so an exit and an
            # entry can never disagree about what the signals said.
            "cm_ok": sig.get("cm_ok"),
            "pctr_ok": sig.get("pctr_ok"),
            "cm_rsi_rising": sig.get("cm_rsi_rising"),
            "sell_signal": sig.get("sell_signal"),
            "proximity_pct": _num(sig.get("proximity_pct")),
            "cm_rsi": _num(sig.get("cm_rsi")),
            "pctr": _num(sig.get("pctr")),
            "has_indicators": bool(sig),
            # Position machinery state.
            "scaled_out": bool(pos.get("tranche_a_filled")),
            "breakeven_done": bool(pos.get("breakeven_done")),
            "sell_signal_stop_done": bool(pos.get("sell_signal_stop_done")),
            "closing_reason": pos.get("closing_reason"),
            "strategy": pos.get("strategy"),
            "source": (pos.get("features") or {}).get("source"),
            "entry_hour_et": _et_hour(now),
            "entry_path": pos.get("entry_path"),
            "edge_mode": pos.get("edge_mode"),
            "git_version": pos.get("git_version"),
            "config_fp": pos.get("config_fp"),
        }
        row = merge_regime(row)
        POSITION_SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with POSITION_SHADOW_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        # Telemetry must never take the position manager down.
        pass


def _risk_basis(pos: dict[str, Any]) -> float:
    """Per-share risk this position's R is measured in. 0 when unknowable.

    Prefers the value frozen at entry. Falls back to entry-stop for positions
    opened before ``risk_per_share`` existed, which is correct only while the
    stop has not been moved — hence the frozen field for everything new.
    """
    frozen = _num(pos.get("risk_per_share"))
    if frozen and frozen > 0:
        return frozen
    entry = _num(pos.get("entry_price")) or 0.0
    stop = _num(pos.get("stop_price")) or 0.0
    return max(0.0, entry - stop)


def _freeze_risk_per_share(pos: dict[str, Any]) -> None:
    """Stamp risk_per_share before any stop raise so R stays well-defined.

    Raising the stop to breakeven/trail without this zeros entry−stop and
    freezes peak/MFE updates (runner trail then never ratchets).
    """
    if _num(pos.get("risk_per_share")):
        return
    entry = _num(pos.get("entry_price"))
    stop = _num(pos.get("stop_price"))
    if entry and stop and entry > stop:
        pos["risk_per_share"] = round(entry - stop, 6)


def _update_excursions(pos: dict[str, Any], price: float | None) -> None:
    """Track worst/best R and the high-water price. Only recorded here."""
    entry = _num(pos.get("entry_price")) or 0.0
    px = _num(price)
    risk = _risk_basis(pos)
    if not (px and entry and risk > 0):
        return
    peak = _num(pos.get("peak_price"))
    pos["peak_price"] = px if peak is None else max(peak, px)
    r = (px - entry) / risk
    mae = _num(pos.get("mae_r"))
    mfe = _num(pos.get("mfe_r"))
    pos["mae_r"] = r if mae is None else min(mae, r)
    pos["mfe_r"] = r if mfe is None else max(mfe, r)


def breakeven_floor(entry: float | None, cfg: dict | None = None) -> float | None:
    """The fill plus ``ai_breakeven_offset_px`` — never the bare fill.

    Every "protect the fill" rule on the desk used ``entry`` itself, which
    books a scratch: the round trip pays the spread twice, so flat on paper is
    red in the account. A penny above the fill is the smallest level that is
    unambiguously green and is still far inside the 0.10R working shelf, so it
    changes which trades scratch, not which trades stop out.
    """
    try:
        e = float(entry or 0)
    except (TypeError, ValueError):
        return None
    if e <= 0:
        return None
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        off = float(
            cfg.get("ai_breakeven_offset_px", DEFAULT_BREAKEVEN_OFFSET_PX)
            if cfg.get("ai_breakeven_offset_px") is not None
            else DEFAULT_BREAKEVEN_OFFSET_PX)
    except (TypeError, ValueError):
        off = DEFAULT_BREAKEVEN_OFFSET_PX
    return round(e + max(0.0, off), 6)


def _runner_stop_level(pos: dict[str, Any]) -> float | None:
    """Where tranche B's stop belongs right now: max(breakeven, peak - nR).

    The breakeven floor is the whole point. Tranche A banks its target at
    ``reward_risk`` R (0.6R on the day-scalp recipe); if the runner is allowed
    to sit below entry after that, a trade that reached its target still closes
    red — which is exactly what the fixed 2.5% trail did on any name whose stop
    was tighter than 2.5%. Once price has run far enough that peak - nR clears
    entry, the trail takes over and ratchets.

    Returns None when there is no basis to compute one (no entry, no risk).
    """
    entry = _num(pos.get("entry_price"))
    risk = _risk_basis(pos)
    if not entry or risk <= 0:
        return None
    peak = _num(pos.get("peak_price")) or entry
    trail_r = _num(pos.get("runner_trail_r"))
    if trail_r is None:
        trail_r = DEFAULT_RUNNER_TRAIL_R
    trail_r = max(0.0, trail_r)
    be = breakeven_floor(entry, _cfg_all()) or float(entry)
    return max(be, float(peak) - trail_r * risk)


def _orig_stop(pos: dict[str, Any]) -> float | None:
    """Hard stop frozen at entry. Falls back to entry − risk."""
    orig = _num(pos.get("entry_stop_price"))
    if orig and orig > 0:
        return orig
    entry = _num(pos.get("entry_price"))
    risk = _risk_basis(pos)
    if entry and risk > 0:
        return entry - risk
    return _num(pos.get("stop_price"))


def _abort_r(cfg: dict | None = None) -> float:
    """Fill-abort threshold in R. Default 0.15."""
    try:
        thru = float(
            (cfg or {}).get("ai_fill_abort_r", DEFAULT_FILL_ABORT_R)
            or DEFAULT_FILL_ABORT_R)
    except (TypeError, ValueError):
        thru = DEFAULT_FILL_ABORT_R
    return max(0.0, float(thru))


def _live_tape_px(symbol: str) -> float | None:
    """Fresh dashboard last, or None. Never invents a print."""
    try:
        import ai_entry_watch as ew
        tape = ew.live_print(symbol)
    except Exception:
        return None
    if tape is None or not tape[0]:
        return None
    try:
        px = float(tape[0])
    except (TypeError, ValueError):
        return None
    return px if px > 0 else None


def fill_already_dead(
    last: float | None,
    entry: float | None,
    stop: float | None,
    risk: float | None,
    cfg: dict | None = None,
) -> bool:
    """True when last is through the stop or ≥ abort_r under *entry*.

    *entry* is the intended limit at confirm (fill vs limit) and the
    sizing ask at place. FGI/SPAI/TDIC opened 2R through on a stale ask.
    """
    try:
        last_f = float(last or 0)
    except (TypeError, ValueError):
        last_f = 0.0
    if last_f <= 0:
        return False
    try:
        stop_f = float(stop or 0)
    except (TypeError, ValueError):
        stop_f = 0.0
    if stop_f > 0 and last_f <= stop_f + 1e-9:
        return True
    try:
        entry_f = float(entry or 0)
    except (TypeError, ValueError):
        entry_f = 0.0
    try:
        risk_f = float(risk or 0)
    except (TypeError, ValueError):
        risk_f = 0.0
    thru = _abort_r(cfg)
    if entry_f > 0 and risk_f > 0 and thru > 0:
        if last_f <= entry_f - thru * risk_f + 1e-9:
            return True
    return False


def local_trail_give_r(mfe_r: float | None, cfg: dict | None = None) -> float:
    """Trail width in R. Default is a constant ``give_r`` (0.10R).

    A wider ``ai_local_trail_give_open_r`` still snaps down to ``give_r``
    after ``tighten_mfe_r``. Absent or non-positive open width, or open
    equal to tight → constant 0.10R.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        tight = float(
            cfg.get("ai_local_trail_give_r", DEFAULT_LOCAL_TRAIL_GIVE_R)
            or DEFAULT_LOCAL_TRAIL_GIVE_R)
    except (TypeError, ValueError):
        tight = DEFAULT_LOCAL_TRAIL_GIVE_R
    tight = max(0.0, float(tight))
    if "ai_local_trail_give_open_r" not in cfg:
        return tight
    try:
        wide = float(cfg.get("ai_local_trail_give_open_r") or 0.0)
    except (TypeError, ValueError):
        wide = 0.0
    if wide <= 0:
        return tight
    if wide < tight:
        wide = tight
    try:
        need = float(cfg.get("ai_local_trail_tighten_mfe_r", 0.25) or 0.0)
    except (TypeError, ValueError):
        need = 0.0
    try:
        mfe = float(mfe_r) if mfe_r is not None else 0.0
    except (TypeError, ValueError):
        mfe = 0.0
    mfe = max(0.0, mfe)
    if mfe > need + 1e-12:
        return tight
    return wide


def _tight_give_clears_entry(
    last: float | None,
    entry: float | None,
    risk: float | None,
    cfg: dict | None,
    spread_r: float | None = None,
) -> bool:
    """True when last − tight give is still above the fill."""
    try:
        last_f = float(last or 0)
        entry_f = float(entry or 0)
        risk_f = float(risk or 0)
    except (TypeError, ValueError):
        return False
    if last_f <= 0 or entry_f <= 0 or risk_f <= 0:
        return False
    tight_cfg = dict(cfg or {})
    tight_cfg["ai_local_trail_give_open_r"] = 0.0
    give = local_trail_give(last_f, risk_f, tight_cfg, mfe_r=99.0,
                            spread_r=spread_r)
    return last_f - give > entry_f + 1e-9


def soft_exit_held_back(pos: dict[str, Any] | None,
                        now: float | None = None,
                        cfg: dict[str, Any] | None = None) -> bool:
    """True while a fill is too young for *indicator* discretionary exits.

    Gates dead-trade and left-overbought only. The ratchet shelf sale is
    NOT held: last through the shelf flattens immediately (BMNR/CRML
    2026-08-25 sat −1% with last already through the shelf because
    min-hold muzzled the sale). Disaster 1R and 15:50 never gated.

    0 = every discretionary exit armed from the first tick.
    """
    try:
        secs = float((cfg if cfg is not None else _cfg_all()).get(
            "ai_exit_min_hold_sec", 0) or 0)
    except (TypeError, ValueError):
        return False
    if secs <= 0 or not isinstance(pos, dict):
        return False
    et = pos.get("entry_time")
    if not et:
        return False              # unknown age is not "young"; fail open
    try:
        age = float(now if now is not None else time.time()) - float(et)
    except (TypeError, ValueError):
        return False
    return age < secs


def _note_min_hold(pos: dict[str, Any] | None, which: str,
                   now: float | None = None) -> None:
    """Record that ``ai_exit_min_hold_sec`` suppressed an exit on this tick.

    Without this, GATE 1 produces a P&L number with no mechanism attached.
    The delay either changed outcomes or it did not, and "how many exits
    did it hold back, on which trades, and what did those trades do next"
    is the only thing that separates a real effect from a week that merely
    traded differently.

    Write-only by construction: it counts and labels, and never returns a
    value anything can branch on.
    """
    if not isinstance(pos, dict):
        return
    try:
        pos["min_hold_blocks"] = int(pos.get("min_hold_blocks") or 0) + 1
    except (TypeError, ValueError):
        pos["min_hold_blocks"] = 1
    pos["min_hold_last"] = str(which)
    pos["min_hold_last_ts"] = float(now if now is not None else time.time())


def _pos_spread_r(pos: dict[str, Any] | None) -> float | None:
    """Round-trip spread this position crossed, as a fraction of R.

    Recorded on the entry features from 2026-08-21. Deliberately the ENTRY
    spread rather than a live one: the trail runs in ai_positions, which has
    no quote of its own, and re-deriving one there would mean a broker call on
    every tick of every name. The book a fill crossed is the honest estimate of
    the book it will cross on the way out, and an old position with no reading
    returns None rather than a guess — the k floor then simply does not apply.
    """
    if not isinstance(pos, dict):
        return None
    for src in (pos.get("features"), pos):
        if not isinstance(src, dict):
            continue
        v = src.get("spread_r")
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


def local_trail_give(
    last: float | None,
    risk: float | None,
    cfg: dict | None = None,
    mfe_r: float | None = None,
    spread_r: float | None = None,
) -> float:
    """Dollar cushion under last for the local R-stop.

    Width is ``local_trail_give_r(mfe) × R``. ``ai_local_trail_give_px`` > 0
    is a legacy fixed dollar and wins only when set. With no usable R, give
    is ``give_r × last / 100`` so the cushion still scales with price.

    ``ai_local_trail_give_spread_k`` > 0 additionally floors the cushion at
    k × the round-trip spread, when *spread_r* is known. That floor is applied
    last, on purpose: it outranks both the percent ceiling and the dollar
    floor, because a cushion narrower than the book is not a stop — the quote
    crossing its own spread trips it without the market moving at all.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        give_px = float(cfg.get("ai_local_trail_give_px") or 0)
    except (TypeError, ValueError):
        give_px = 0.0
    if give_px > 0:
        return max(0.01, give_px)
    give_r = local_trail_give_r(mfe_r, cfg)
    try:
        r = float(risk) if risk is not None else 0.0
    except (TypeError, ValueError):
        r = 0.0
    if r > 0:
        give = give_r * r
    else:
        try:
            last_f = float(last) if last is not None else 0.0
        except (TypeError, ValueError):
            last_f = 0.0
        if last_f > 0 and give_r > 0:
            give = give_r * last_f / 100.0
        else:
            give = 0.01
    # Ceiling, in percent of price. R is the plan's risk unit, not a statement
    # about how close support should sit: a zone that puts the stop 5% under
    # entry (CRSP 2026-08-19, 1R = $2.87 on a $57 name) turns "give 0.10R" into
    # a 29-cent cushion, and the shelf trails far enough behind the print that
    # it is not support at all. This keeps the shelf near the tape on wide-R
    # names and does nothing on tight ones, where give_r × R is already the
    # smaller number. 0 disables it, which is the shipped default.
    try:
        max_pct = float(cfg.get("ai_local_trail_give_max_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        max_pct = 0.0
    if max_pct > 0:
        try:
            px = float(last) if last is not None else 0.0
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            give = min(give, px * max_pct / 100.0)
    try:
        floor = float(cfg.get(
            "ai_local_trail_min_give_px", DEFAULT_LOCAL_TRAIL_MIN_GIVE_PX)
            or 0.0)
    except (TypeError, ValueError):
        floor = DEFAULT_LOCAL_TRAIL_MIN_GIVE_PX
    if floor > 0:
        cap = floor
        if r > 0:
            try:
                max_r = float(cfg.get(
                    "ai_local_trail_min_give_max_r",
                    DEFAULT_LOCAL_TRAIL_MIN_GIVE_MAX_R,
                ) or 0.0)
            except (TypeError, ValueError):
                max_r = DEFAULT_LOCAL_TRAIL_MIN_GIVE_MAX_R
            if max_r > 0:
                cap = min(floor, max_r * r)
        give = max(give, cap)
    # Last, and above every other rule. spread_r is the round trip as a
    # fraction of R, so k × spread_r × R is k spreads in dollars.
    try:
        k = float(cfg.get("ai_local_trail_give_spread_k",
                          DEFAULT_LOCAL_TRAIL_SPREAD_K) or 0.0)
    except (TypeError, ValueError):
        k = DEFAULT_LOCAL_TRAIL_SPREAD_K
    if k > 0 and r > 0 and spread_r is not None:
        try:
            sp = float(spread_r)
        except (TypeError, ValueError):
            sp = 0.0
        if sp > 0:
            floor_r = k * sp
            try:
                cap_r = float(cfg.get("ai_local_trail_give_spread_max_r",
                                      DEFAULT_LOCAL_TRAIL_SPREAD_MAX_R) or 0.0)
            except (TypeError, ValueError):
                cap_r = DEFAULT_LOCAL_TRAIL_SPREAD_MAX_R
            if cap_r > 0:
                floor_r = min(floor_r, cap_r)
            give = max(give, floor_r * r)
    return max(0.01, give)


def initial_local_stop(
    entry: float | None,
    risk: float | None,
    cfg: dict | None = None,
    spread_r: float | None = None,
) -> float | None:
    """Working rstop at fill: entry − 0.10R (give_r), not the 5% plan stop.

    ``ai_local_trail_arm_r`` only delays *raising* the shelf. The live
    rstop is always the 0.10R working shelf from the fill.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        e = float(entry or 0)
    except (TypeError, ValueError):
        return None
    if e <= 0:
        return None
    give = local_trail_give(e, risk, cfg, mfe_r=0.0, spread_r=spread_r)
    want = e - give
    if want >= e:
        want = e - 0.01
    if want <= 0:
        return None
    return round(want, 6)


def _median_px(xs: list[float]) -> float | None:
    ys = sorted(x for x in xs if x > 0)
    if not ys:
        return None
    n = len(ys)
    if n % 2:
        return ys[n // 2]
    return (ys[n // 2 - 1] + ys[n // 2]) / 2.0


def note_trail_print(
    pos: dict[str, Any],
    px: float | None,
    n: int = 3,
) -> float | None:
    """Record a raw print and return the damped last used to raise RSTOP.

    A single IEX spike (SST 08-14) must not lift the shelf. Need two
    prints; raise off the median of the last *n*.
    """
    try:
        p = float(px) if px is not None else 0.0
    except (TypeError, ValueError):
        p = 0.0
    ring: list[float] = []
    for x in pos.get("trail_prints") or []:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if v > 0:
            ring.append(v)
    if p > 0:
        ring.append(p)
        ring = ring[-max(2, int(n)) :]
        pos["trail_prints"] = ring
    if len(ring) < 2:
        pos["trail_last"] = None
        return None
    med = _median_px(ring)
    pos["trail_last"] = med
    return med


def _trail_last_for_stop(pos: dict[str, Any]) -> float | None:
    """Price the shelf raises off. A 1-print ring is not enough to lift."""
    ring = pos.get("trail_prints")
    if isinstance(ring, list) and 0 < len(ring) < 2:
        return None
    last = _num(pos.get("trail_last"))
    if last is not None and last > 0:
        return last
    return _num(pos.get("last_seen_price"))


def local_profit_stop(pos: dict[str, Any], cfg: dict | None = None) -> float | None:
    """Trail just under the damped last: rises as price grows, never lowers.

    ``local_stop = max(prev, last − give, plan floor)``.
    Last is the median of recent prints when present, else ``last_seen_price``.
    Give is a constant 0.10R unless a wider open width is configured.
    ``ai_local_trail_give_px`` > 0 still wins as a fixed dollar.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not bool(cfg.get("ai_local_trail_enabled", True)):
        return None
    last = _trail_last_for_stop(pos)
    risk = _risk_basis(pos)
    entry = _num(pos.get("entry_price"))
    floor = _orig_stop(pos)
    prev = _num(pos.get("local_stop_price"))
    if last is None or last <= 0:
        return prev or floor
    mfe = _num(pos.get("mfe_r"))
    if last is not None and entry and risk and risk > 0:
        from_last = max(0.0, (float(last) - float(entry)) / float(risk))
        mfe = max(float(mfe or 0.0), from_last)
    elif mfe is None:
        mfe = 0.0
    # Breakeven floor. Once the trade has earned ai_local_trail_be_at_r, the
    # shelf never sits under the fill again — the same rule _runner_stop_level
    # already applies to tranche B, where the floor is the whole point: a name
    # that ran and came back must not be handed over as a loss.
    #
    # It has to be computed before the arm gate below, because that gate is
    # exactly where the hole was: under arm_r the function returns the seed,
    # which is entry − give, so a position in profit kept a stop below its own
    # entry (ASST/TEM 2026-08-19, both parked at −0.070R with price above the
    # fill). 0 disables it.
    try:
        be_at = float(cfg.get("ai_local_trail_be_at_r", 0) or 0)
    except (TypeError, ValueError):
        be_at = 0.0
    # ...or a percent of price, whichever comes first. R is the wrong yardstick
    # for this on its own: these zones make 1R about 5% of price, so HOOD ran
    # +0.42% off a 98.20 fill on 2026-08-19 and that was only 0.084R — under a
    # 0.10R floor, which would not have engaged until price moved half a
    # percent. The operator's question is "am I up enough to stop risking the
    # fill", and that is asked in percent, not in a risk unit the zone happened
    # to choose.
    try:
        be_at_pct = float(cfg.get("ai_local_trail_be_at_pct", 0) or 0)
    except (TypeError, ValueError):
        be_at_pct = 0.0
    be_hit = False
    if entry:
        if be_at > 0 and float(mfe) + 1e-9 >= be_at:
            be_hit = True
        if not be_hit and be_at_pct > 0 and risk and float(risk) > 0:
            # mfe is the high-water mark in R, so this is the same excursion
            # expressed against price — no extra state to keep in step.
            gain_pct = float(mfe) * float(risk) / float(entry) * 100.0
            be_hit = gain_pct + 1e-9 >= be_at_pct
    # A gain smaller than the round trip is not a gain, and locking breakeven
    # on it is how a trade dies. be_at_pct is 0.15% of price against a median
    # book of 0.49%, so on 2026-08-21 the floor armed on roughly a third of the
    # spread and slammed the shelf to entry + $0.01 — three tenths of a cent
    # under the market on BKKT. 45% of every shelf raise that day landed
    # exactly there, and the next tick sold it.
    #
    # This is a floor UNDER the other triggers, deliberately not another one
    # beside them: be_at_r and be_at_pct are ORed, so a third OR would arm the
    # floor even earlier, which is the opposite of the problem. Whatever they
    # say, do not protect a fill until it has actually cleared k round trips.
    # 0 = off, and no spread reading means no opinion.
    if be_hit:
        try:
            be_k = float(cfg.get("ai_local_trail_be_at_spread_k",
                                 DEFAULT_BE_AT_SPREAD_K) or 0.0)
        except (TypeError, ValueError):
            be_k = DEFAULT_BE_AT_SPREAD_K
        sp = _pos_spread_r(pos)
        if be_k > 0 and sp is not None and sp > 0:
            if float(mfe) + 1e-9 < be_k * sp:
                be_hit = False
    be_floor = (breakeven_floor(entry, cfg) or float(entry)) if be_hit else None
    try:
        arm_need = float(cfg.get("ai_local_trail_arm_r", 0) or 0)
    except (TypeError, ValueError):
        arm_need = 0.0
    # Same argument as the breakeven floor: on a zone that makes 1R about 5% of
    # price, an R-only arm keeps the shelf frozen through moves that are plainly
    # real. HOOD ran +0.42% off a 98.20 fill and stayed parked because 0.42% was
    # 0.084R against a 0.15R arm — the trail would not have started tracking
    # until price moved three quarters of a percent.
    try:
        arm_pct = float(cfg.get("ai_local_trail_arm_pct", 0) or 0)
    except (TypeError, ValueError):
        arm_pct = 0.0
    armed = arm_need <= 0 or float(mfe) + 1e-9 >= arm_need
    if not armed and arm_pct > 0 and entry and risk and float(risk) > 0:
        gain_pct = float(mfe) * float(risk) / float(entry) * 100.0
        armed = gain_pct + 1e-9 >= arm_pct
    if not armed:
        # Hold the 0.10R working shelf. Do not fall back to the 5% plan
        # stop — that is the disaster floor, not the live rstop.
        seed = initial_local_stop(entry, risk, cfg,
                                  spread_r=_pos_spread_r(pos))
        out = prev or seed or floor
        if be_floor is not None and out is not None:
            out = max(float(out), be_floor)
        return out
    give_mfe = mfe
    if not _tight_give_clears_entry(last, entry, risk, cfg,
                                    spread_r=_pos_spread_r(pos)):
        give_mfe = 0.0
    give = local_trail_give(last, risk, cfg, mfe_r=give_mfe,
                            spread_r=_pos_spread_r(pos))
    want = float(last) - give
    if want >= float(last):
        want = float(last) - 0.01
    if floor is not None:
        want = max(float(floor), want)
    if be_floor is not None:
        want = max(want, be_floor)
    if prev is not None:
        want = max(want, float(prev))
    return round(want, 6)


def _flatten_late_hold_stop(
    ticker: str,
    pos: dict[str, Any],
    trigger: float | None,
    events: list[dict[str, Any]],
    exit_why: dict[str, Any],
) -> tuple[bool, bool]:
    """Hard synth stop only — no 0.10R working shelf, no ratchet.

    Broker stop is off on this desk; without this check a late-hold fill
    would sit naked until dead-trade or 15:50.
    """
    if not pos.get("entry_confirmed") or pos.get("closing_reason"):
        return False, False
    floor = _num(pos.get("entry_stop_price")) or _num(pos.get("stop_price"))
    last = trigger if trigger is not None else _num(pos.get("last_seen_price"))
    if last is None or floor is None or last > floor + 1e-9:
        return False, False
    import alpaca_trader
    alpaca_trader.cancel_open_orders(ticker)
    out = alpaca_trader.close_out(ticker) or {}
    if isinstance(out, dict) and out.get("order_id"):
        pos["close_order_id"] = str(out["order_id"])
    pos["closing_reason"] = "late_hold_stop"
    exit_why[ticker] = "late_hold_stop"
    events.append({
        "ticker": ticker, "event": "late_hold_stop",
        "last": last, "stop": floor,
    })
    log_event("late_hold_stop", symbol=ticker, last=last, stop=floor)
    return True, True


def _shelf_under_print(loc: float | None, last: float | None,
                       pos: dict[str, Any]) -> float | None:
    """Never seed the working shelf at or above the live print.

    A shelf placed above the market is a market-flatten wearing a stop's
    name: the trigger test on the same tick sees price through it and closes
    the position at whatever the tape is, not at the level the shelf claims.
    The point of seeding is to start protecting a name, not to liquidate it
    for having moved while the shelf was stale or absent.

    Introduced (e02473d) for a leftover 5% plan stop, but the identical case
    with no stored shelf at all — a fresh fill, or a position adopted
    mid-move — skipped the check and flattened. The disaster floor still
    catches a name that is genuinely through its risk; that is what it is
    for, and it is a resting order rather than a market exit.
    """
    if loc is None or last is None or last <= 0:
        return loc
    if loc + 1e-9 < last:
        return loc
    give = local_trail_give(last, _risk_basis(pos), _cfg_all(), mfe_r=0.0,
                            spread_r=_pos_spread_r(pos))
    under = round(float(last) - give, 6)
    return under if 0 < under < last else loc


def apply_local_trail(
    ticker: str,
    pos: dict[str, Any],
    trigger: float | None,
    events: list[dict[str, Any]],
    exit_why: dict[str, Any],
) -> tuple[bool, bool]:
    """Raise the software shelf, or flatten if *trigger* prints at/under it.

    Returns ``(changed, closed)``. ``closed`` means the caller must stop
    working this position on this pass — it has been handed to close_out.

    Split out of manage_open_positions so the shelf can run on its own
    cadence. Everything here is the tape and local state; the only broker call
    is the close itself. That is what lets it tick at ai_shelf_tick_sec while
    fills, dead-trade, T1 and EOD stay on the slower book tick, which costs a
    get_positions_detail every time.
    """
    import alpaca_trader

    if pos.get("late_hold") or str(pos.get("strategy") or "") == "late_hold":
        return _flatten_late_hold_stop(ticker, pos, trigger, events, exit_why)
    try:
        import desk_h4 as _desk_h4
        if _desk_h4.is_h4_pos(pos):
            return _flatten_late_hold_stop(
                ticker, pos, trigger, events, exit_why)
    except Exception:
        pass

    if not (
        pos.get("entry_confirmed")
        and not pos.get("closing_reason")
        and _cfg_flag("ai_local_trail_enabled", True)
    ):
        return False, False

    changed = False
    last = _num(pos.get("last_seen_price"))
    floor = _orig_stop(pos)
    loc = _num(pos.get("local_stop_price"))
    seed = initial_local_stop(
        _num(pos.get("entry_price")), _risk_basis(pos), _cfg_all(),
        spread_r=_pos_spread_r(pos))
    if loc is None:
        # No stored shelf: a fresh fill, or a name adopted mid-move. Same
        # hazard as the stale-plan case below and it was missing the same
        # guard — a seed above the print flattens on the very next trigger.
        loc = _shelf_under_print(seed or floor, last, pos)
    elif seed is not None and loc + 1e-9 < seed:
        # Plan-stop leftover (5% floor) is not the working shelf.
        loc = _shelf_under_print(seed, last, pos)
        pos["local_stop_price"] = loc
        changed = True
    if trigger is None:
        trigger = last
    # Hit the *existing* board stop first. Raising on this tick's high
    # and then testing the low would skip a sale through the old shelf.
    # Min-hold does not muzzle this sale: the ratchet is the edge, and a
    # print through the shelf is the exit even inside the 5m RSI window.
    _trail_hit = (trigger is not None and loc is not None
                  and trigger <= loc + 1e-9)
    if _trail_hit:
        last = trigger
        alpaca_trader.cancel_open_orders(ticker)
        out = alpaca_trader.close_out(ticker) or {}
        if isinstance(out, dict) and out.get("order_id"):
            pos["close_order_id"] = str(out["order_id"])
        pos["closing_reason"] = "local_trail"
        exit_why[ticker] = "local_trail"
        events.append({
            "ticker": ticker, "event": "local_trail",
            "last": last, "stop": loc,
            "peak": pos.get("peak_price"),
        })
        log_event(
            "local_trail", symbol=ticker,
            last=last, stop=loc, peak=pos.get("peak_price"),
        )
        return True, True

    want = local_profit_stop(pos, _cfg_all())
    prev_local = _num(pos.get("local_stop_price"))
    if want is not None and (
        prev_local is None or want > prev_local + 1e-9
    ):
        pos["local_stop_price"] = want
        changed = True
        raised = floor is None or want > floor + 1e-9
        if raised and prev_local is not None:
            events.append({
                "ticker": ticker, "event": "local_trail_raised",
                "from_stop": prev_local, "to_stop": want,
                "peak": pos.get("peak_price"),
                "mfe_r": pos.get("mfe_r"),
            })
            log_event(
                "local_trail_raised", symbol=ticker,
                from_stop=prev_local, to_stop=want,
                peak=pos.get("peak_price"), mfe_r=pos.get("mfe_r"),
                give_r=local_trail_give_r(pos.get("mfe_r"), _cfg_all()),
            )
    return changed, False


def local_trail_ring(tick_sec: float, cfg: dict | None = None) -> int:
    """Ring size that holds the shelf's damping window constant in seconds.

    Two prints 3s apart is a 6-second median; two prints 250ms apart is half a
    second of protection against the same spike. Deriving the count from
    ai_local_trail_damp_sec and the actual tick keeps the guard worth the same
    whatever cadence the shelf runs at. Never fewer than two — a one-print ring
    cannot damp anything. Falls back to ai_local_trail_print_ring when the tick
    is unknown, which is what manage_open_positions passes.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        ring = int(cfg.get("ai_local_trail_print_ring", 3) or 3)
    except (TypeError, ValueError):
        ring = 3
    try:
        t = float(tick_sec or 0.0)
    except (TypeError, ValueError):
        t = 0.0
    if t <= 0:
        return max(2, ring)
    try:
        damp = float(
            cfg.get("ai_local_trail_damp_sec", DEFAULT_LOCAL_TRAIL_DAMP_SEC)
            if cfg.get("ai_local_trail_damp_sec") is not None
            else DEFAULT_LOCAL_TRAIL_DAMP_SEC)
    except (TypeError, ValueError):
        damp = DEFAULT_LOCAL_TRAIL_DAMP_SEC
    if damp <= 0:
        return max(2, ring)
    return max(2, min(64, round(damp / t)))


def tick_local_trail(tick_sec: float = 0.0) -> list[dict[str, Any]]:
    """Shelf-only pass: read the tape, ratchet, flatten if it printed through.

    manage_open_positions is the full desk tick — fills, dead-trade, T1, EOD —
    and it costs a get_positions_detail on every call, which is why it runs on
    the book tick. The shelf needs none of that: the freshest print is already
    in process from the stream and the shelf is local state, so this pass does
    no broker round trip at all unless it fires. Running just this part on a
    short cadence is what puts the stop next to the tape instead of a book tick
    behind it.

    MUST be called from the same thread as manage_open_positions. Both do
    load → mutate → save on the state file, so running them concurrently would
    let one pass silently drop the other's writes — and both can call
    close_out. Sequential execution in one thread is the whole safety argument.
    """
    events: list[dict[str, Any]] = []
    exit_why: dict[str, Any] = {}
    if not _cfg_flag("ai_local_trail_enabled", True):
        return events
    cfg = _cfg_all()
    ring = local_trail_ring(tick_sec, cfg)
    changed = False
    with _state_lock:
        state = _load_state()
        for ticker, pos in list(state.items()):
            if not isinstance(pos, dict):
                continue
            if not pos.get("entry_confirmed") or pos.get("closing_reason"):
                continue
            px = _fresh_tape_px(ticker)
            if px is None:
                # No fresh stream print. The book tick owns the REST fallback
                # and the stale-data flatten; this pass simply has nothing to
                # say, and must not ratchet off a stale number.
                continue
            pos["last_seen_price"] = px
            note_trail_print(pos, px, n=ring)
            _update_excursions(pos, px)
            changed = True
            ch, _closed = apply_local_trail(ticker, pos, px, events, exit_why)
            if ch:
                changed = True
        if changed:
            _save_state(state)
    return events


def never_lower_rstop(*levels: float | None) -> float | None:
    """Highest positive stop among *levels*. RSTOP must not print lower."""
    xs: list[float] = []
    for x in levels:
        try:
            v = float(x) if x is not None else 0.0
        except (TypeError, ValueError):
            continue
        if v > 0:
            xs.append(v)
    return max(xs) if xs else None


def _fresh_tape_px(ticker: str) -> float | None:
    """Stream print for *ticker* when it is inside the stale window, else None.

    The positions tick already pulls this for _tick_prints. It is the fastest
    price the desk has — the stream updates continuously, while the broker mark
    on ``live["current"]`` only moves when the positions poll refreshes.
    """
    try:
        import ai_entry_watch as ew
        tape = ew.live_print(ticker)
    except Exception:  # noqa: BLE001
        return None
    if tape is None or not tape[0]:
        return None
    try:
        px = float(tape[0])
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None
    age = tape[1]
    if age is None:
        return px
    try:
        stale = float(
            _cfg_all().get(
                "ai_stale_data_max_age_sec", DEFAULT_STALE_DATA_MAX_AGE_SEC)
            or DEFAULT_STALE_DATA_MAX_AGE_SEC
        )
    except (TypeError, ValueError):
        stale = DEFAULT_STALE_DATA_MAX_AGE_SEC
    try:
        return px if float(age) <= stale else None
    except (TypeError, ValueError):
        return px


def _tick_prints(ticker: str, live: dict | None) -> tuple[float | None, float | None]:
    """This-tick (high, low) from broker mark and dashboard tape.

    High is excursion (MFE). Low is the flatten trigger — any print at
    or below the shelf market-closes. The shelf itself raises off the
    damped last (``note_trail_print``), not this high. Tape is used when
    its age is known and inside the stale window.
    """
    vals: list[float] = []
    broker = _num((live or {}).get("current"))
    if broker is None:
        broker = _num((live or {}).get("current_price"))
    if broker is not None and broker > 0:
        vals.append(float(broker))
    try:
        import ai_entry_watch as ew
        tape = ew.live_print(ticker)
        if tape is not None and tape[0] and float(tape[0]) > 0:
            age = tape[1]
            try:
                stale = float(
                    _cfg_all().get(
                        "ai_stale_data_max_age_sec",
                        DEFAULT_STALE_DATA_MAX_AGE_SEC,
                    ) or DEFAULT_STALE_DATA_MAX_AGE_SEC
                )
            except (TypeError, ValueError):
                stale = DEFAULT_STALE_DATA_MAX_AGE_SEC
            if age is None or float(age) <= stale:
                vals.append(float(tape[0]))
    except Exception:
        pass
    if not vals:
        return None, None
    return max(vals), min(vals)


def _rth_now(now: float) -> bool:
    """Regular session (weekdays 09:30–16:00 ET). Broker clock if available."""
    try:
        import alpaca_trader as _at
        if hasattr(_at, "is_market_open"):
            got = _at.is_market_open()
            if got is not None:
                return bool(got)
    except Exception:
        pass
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(float(now), tz=ZoneInfo("America/New_York"))
        if dt.weekday() >= 5:
            return False
        mins = dt.hour * 60 + dt.minute
        return (9 * 60 + 30) <= mins < (16 * 60)
    except Exception:
        return False


def quote_is_live(symbol: str, cfg: dict | None = None) -> tuple[bool, str]:
    """True when we have a stream print or a REST ask we can trade on."""
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        import ai_entry_watch as ew
        _px, src, age = ew.decision_price(symbol, cfg)
    except Exception:
        return False, "error"
    if src == "stream":
        try:
            max_age = float(cfg.get(
                "ai_stale_data_max_age_sec", DEFAULT_STALE_DATA_MAX_AGE_SEC)
                or DEFAULT_STALE_DATA_MAX_AGE_SEC)
        except (TypeError, ValueError):
            max_age = DEFAULT_STALE_DATA_MAX_AGE_SEC
        if age is not None and age > max_age:
            return False, "stream_old"
        return True, "stream"
    if src == "rest":
        return True, "rest"
    return False, src or "none"


def _infer_t1_fill(pos: dict[str, Any], live_qty: float | None) -> bool:
    """True when broker qty looks like tranche A sold AND price reached T1.

    Qty-only inferred T1 on CRMD 2026-08-13: live half-size (partial entry)
    while the T1 limit was still working below the high. That marked the
    runner scaled, skipped BE, and left the remainder naked.
    """
    if pos.get("tranche_a_filled") or pos.get("closing_reason"):
        return False
    qty_a = int(pos.get("qty_a") or 0)
    qty_b = int(pos.get("qty_b") or 0)
    q = _num(live_qty)
    if qty_a < 1 or qty_b < 1 or q is None or q <= 0:
        return False
    total = qty_a + qty_b
    if not (q <= qty_b + 1 and q < total):
        return False
    t1 = _num(pos.get("target_1"))
    px = _num(pos.get("peak_price")) or _num(pos.get("last_seen_price"))
    if t1 is None or px is None:
        return False
    return px + 1e-6 >= t1


def evaluate_ratchet_invariants(
    state: dict[str, Any],
    detail: dict[str, Any],
    open_orders: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Check dual-tranche books against the broker. Fail closed, one row each.

    After T1 the runner stop must be at or above entry. Before T1 a name
    that is confirmed open must have a resting T1 (or attach still pending
    on this tick). Used by manage_open_positions and tools/ratchet_check.py.
    """
    orders = open_orders or []
    out: list[dict[str, Any]] = []
    for ticker, pos in state.items():
        if not pos.get("entry_confirmed") or pos.get("closing_reason"):
            continue
        qty_a = int(pos.get("qty_a") or 0)
        qty_b = int(pos.get("qty_b") or 0)
        if qty_a < 1 or qty_b < 1:
            continue
        live = detail.get(str(ticker).upper()) or {}
        live_qty = _num(live.get("qty"))
        if live_qty is None:
            continue
        sells = [
            o for o in orders
            if str(o.get("symbol") or "").upper() == str(ticker).upper()
            and str(o.get("side") or "").lower() == "sell"
        ]
        has_t1 = any(not _is_protective_stop_order(o) for o in sells)
        stop_levels = [
            _num(o.get("stop") or o.get("stop_price") or o.get("limit"))
            for o in sells if _is_protective_stop_order(o)
        ]
        stop_levels = [x for x in stop_levels if x]
        best_stop = max(stop_levels) if stop_levels else _num(
            pos.get("runner_stop_price") or pos.get("stop_price"))
        entry = _num(pos.get("entry_price"))
        scaled = bool(pos.get("tranche_a_filled")) or _infer_t1_fill(pos, live_qty)
        row: dict[str, Any] = {
            "symbol": str(ticker).upper(),
            "live_qty": live_qty,
            "qty_a": qty_a,
            "qty_b": qty_b,
            "entry": entry,
            "best_stop": best_stop,
            "has_t1": has_t1,
            "scaled": scaled,
        }
        if scaled:
            if entry and best_stop is not None and best_stop + 1e-6 < entry:
                row.update({"ok": False, "event": "ratchet_stop_below_entry"})
            elif not stop_levels:
                row.update({"ok": False, "event": "ratchet_missing_runner_stop"})
            else:
                row.update({"ok": True, "event": "ratchet_ok"})
        else:
            if has_t1 or pos.get("t1_attach_pending"):
                row.update({"ok": True, "event": "ratchet_ok"})
            else:
                row.update({"ok": False, "event": "ratchet_missing_t1"})
        out.append(row)
    return out


def _et_hour(now: float) -> float | None:
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.fromtimestamp(now, tz=ZoneInfo("America/New_York"))
        return round(dt.hour + dt.minute / 60.0, 2)
    except Exception:
        return None


def resolve_exit(pos: dict[str, Any], ticker: str = "") -> tuple:
    """(exit_price, close_reason) for a position that has gone flat.

    Price and reason are resolved together because they come from the same
    evidence — the order that actually filled. Resolving them apart is how the
    desk ended up labelling exits it had never seen.

    Order of evidence, strongest first:
      1. The desk's own legs. If our take-profit filled it is a target; if our
         stop filled it is a stop-out. No network call, and it is the one case
         where the desk genuinely knows.
      2. The broker's fill history. Covers everything the desk did not place —
         a hand-liquidated position, an EOD flatten, a leg whose id was lost to
         a restart. The order TYPE is the label: stop -> stopped out, limit ->
         target, market -> flattened.
      3. Nothing. Returns (None, "unknown").

    Never guesses. The old code returned "stopped_out" for anything that
    vanished without tranche A scaling out, which was wrong on all four of
    2026-08-07's trades — three hand-liquidated, one closed at the bell, none
    within 2% of its stop — while every exit_price came back null because the
    fills belonged to orders the desk had not placed. A wrong label is worse
    than no label: the scorecard reads it as observed fact.
    """
    px = _order_fill_price(pos.get("tranche_a_target_order_id"))
    if px:
        return px, "target_hit"
    px = _order_fill_price(pos.get("tranche_b_stop_order_id"))
    if px:
        return px, ("trailed_out" if pos.get("tranche_a_filled")
                    else "stopped_out")
    px = _order_fill_price(pos.get("close_order_id"))
    if px:
        return px, "flattened"

    fill = _broker_exit_fill(ticker or pos.get("symbol") or "",
                             pos.get("entry_time"))
    if fill:
        otype = str(fill.get("type") or "").lower()
        price = _num(fill.get("filled_avg_price"))
        if "stop" in otype:
            return price, ("trailed_out" if pos.get("tranche_a_filled")
                           else "stopped_out")
        if "limit" in otype:
            return price, "target_hit"
        if "market" in otype:
            return price, "flattened"
        return price, "unknown"
    return None, "unknown"


def _cfg_float(key: str, default: float) -> float:
    try:
        return float(_entry_cfg().get(key, default) or default)
    except (TypeError, ValueError):
        return float(default)


def _latest_entry_ok_event(symbol: str) -> dict[str, Any] | None:
    """Most recent full entry_ok row for *symbol* from events.jsonl, if any.

    Used to re-home orphans that were filled at the broker after a lost
    positions_state write (MLTX 2026-08-11). Prefers rows that carry
    stop_price / qty so adoption can rebuild a mechanical book.
    """
    sym = str(symbol or "").upper()
    if not sym or not EVENTS_PATH.exists():
        return None
    best: dict[str, Any] | None = None
    try:
        with EVENTS_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                if "entry_ok" not in line or sym not in line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("kind") != "entry_ok":
                    continue
                if str(e.get("symbol") or "").upper() != sym:
                    continue
                # The dual-logged thin row (stop+target only) is not enough.
                if e.get("stop_price") is None and e.get("qty_a") is None:
                    continue
                best = e
    except Exception:
        return best
    return best


def _resting_stop_price(symbol: str) -> float | None:
    """Stop trigger price from a resting stop/stop_limit sell, if any."""
    try:
        import alpaca_trader
        sym = str(symbol or "").upper()
        for o in alpaca_trader.get_open_orders() or []:
            if str(o.get("symbol") or "").upper() != sym:
                continue
            if not _is_protective_stop_order(o):
                continue
            px = _num(o.get("stop") or o.get("stop_price") or o.get("limit"))
            if px and px > 0:
                return px
    except Exception:
        pass
    return None


def _adopt_unmanaged(
    unmanaged: list[str],
    detail: dict[str, Any],
    state: dict[str, Any],
    now: float,
) -> list[dict[str, Any]]:
    """Pull broker-live orphans into the managed book when we can recover levels.

    MLTX was entry_ok + filled and then immediately unmanaged because a concurrent
    state save dropped it. Without a managed row, exhaustion and heal never run.
    Prefer the last entry_ok event; fall back to a resting stop order's trigger.
    Human/manual positions with no desk trail are left alone (still reported
    unmanaged) unless they have a resting stop we can key off.
    """
    events: list[dict[str, Any]] = []
    if not unmanaged or not _cfg_flag("ai_adopt_unmanaged", True):
        return events
    for sym in unmanaged:
        s = str(sym or "").upper()
        if not s or s in state:
            continue
        live = detail.get(s) or detail.get(sym) or {}
        try:
            qty = abs(float(live.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        entry = _num(live.get("avg_entry_price") or live.get("avg_entry")
                     or live.get("entry_price"))
        ok = _latest_entry_ok_event(s)
        stop = _num((ok or {}).get("stop_price")) if ok else None
        if not stop:
            stop = _resting_stop_price(s)
        if not entry:
            entry = _num((ok or {}).get("entry_price"))
        if not entry or entry <= 0:
            log_event("adopt_skipped", symbol=s, reason="no_entry_price")
            continue
        if not stop or stop <= 0 or stop >= entry:
            # No recoverable stop — do not invent one. Heal/flatten path can
            # still act once unprotected if policy wants; adoption needs R.
            log_event("adopt_skipped", symbol=s, reason="no_stop_level",
                      entry=entry)
            continue
        target = _num((ok or {}).get("target_1")) if ok else None
        qty_a_ev = int((ok or {}).get("qty_a") or 0) if ok else 0
        qty_b_ev = int((ok or {}).get("qty_b") or 0) if ok else 0
        if qty_a_ev > 0 and qty_b_ev > 0:
            qty_a, qty_b = qty_a_ev, qty_b_ev
            scaled = qty <= qty_b + 1 and qty < (qty_a + qty_b)
        else:
            qty_a, qty_b = int(qty), 0
            scaled = False
        state[s] = {
            "qty_a": qty_a,
            "qty_b": qty_b,
            "total_qty": int(qty_a + qty_b) if qty_b else int(qty),
            "entry_price": entry,
            "stop_price": stop,
            "risk_per_share": max(0.0, entry - stop),
            "target_1": target,
            "entry_time": float((ok or {}).get("ts") or now),
            "entry_confirmed": True,
            "tranche_a_filled": bool(scaled),
            "breakeven_done": bool(scaled),
            "t1_attach_pending": bool(qty_b > 0 and not scaled),
            "runner_trail_r": DEFAULT_RUNNER_TRAIL_R,
            "closing_reason": None,
            "last_seen_price": _num(live.get("current") or live.get("price")),
            "strategy": str((ok or {}).get("strategy") or "adopted"),
            "duel_source": (ok or {}).get("duel_source"),
            "adopted": True,
            "adopted_ts": now,
            "entry_path": "adopted",
            # Keep the latch so left_overbought can fire immediately: an orphan
            # is a position the desk did not open and has no continuation
            # thesis for, so the fade out of overbought is a real exit for it.
            # evaluate_positions re-enables that exit for adopted rows even
            # when the edge mode disables it globally.
            "exh_was_overbought": True,
            # NOT "overbought" — no %R was ever read here. Asserting a reading
            # we never took poisons every later slice by entry state, and the
            # 2026-08-11 analysis tripped over exactly this row.
            "entry_exhaustion_state": "adopted",
            "mae_r": None,
            "mfe_r": None,
            "sell_signal_stop_done": False,
            **regime_stamp(),
        }
        events.append({"ticker": s, "event": "position_adopted",
                       "stop_price": stop, "entry_price": entry, "qty": qty})
        log_event(
            "position_adopted", symbol=s, stop_price=stop,
            entry_price=entry, qty=qty,
            from_entry_ok=bool(ok),
        )
    return events


def _available_qty_from_error(failed: Any) -> int:
    """Shares Alpaca says are actually free, read off an insufficient-qty refusal.

    The heal asks for the whole position while a resting T1 still holds part of
    it, so Alpaca refuses the protective stop outright (code 40310000) and the
    name stays naked. That is not a rare race: 183 of the 193 heal failures
    through 2026-08-13 were one position retrying the same full-size stop every
    poll, each time told ``requested: 30, available: 8``.

    The refusal already carries the free quantity, so read it from there rather
    than spending another API call on a desk that is at its rate limit. Regex
    rather than json.loads because the broker body is sometimes truncated
    before its closing brace.

    Returns 0 when this is not that refusal, or nothing is free.
    """
    raw = str((failed or {}).get("error") or (failed or {}).get("status") or "")
    if "40310000" not in raw and "insufficient qty" not in raw.lower():
        return 0
    m = re.search(r'"available"\s*:\s*"?(\d+(?:\.\d+)?)', raw)
    if not m:
        m = re.search(r"available:\s*(\d+(?:\.\d+)?)", raw)
    if not m:
        return 0
    try:
        return max(0, int(float(m.group(1))))
    except (TypeError, ValueError):
        return 0


def _restop_at_available(sym: str, stop_price: float, failed: Any) -> dict | None:
    """Re-place a refused protective stop at the quantity that is actually free.

    ``available == 0`` means every share is already held by a resting order, so
    there is nothing to protect and nothing to do — returning None leaves the
    original refusal to be logged rather than inventing a second failure.
    """
    qty = _available_qty_from_error(failed)
    if qty <= 0:
        return None
    import alpaca_trader
    try:
        out = alpaca_trader.place_stop_sell(sym, stop_price, qty=qty) or {}
    except Exception:  # noqa: BLE001
        return None
    if out.get("ok"):
        log_event("unprotected_heal_resized", symbol=sym, qty=qty,
                  stop_price=stop_price)
    return out


def _heal_unprotected(
    unprotected: list[dict[str, Any]], state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Place a resting stop for longs that have no stop protection.

    Prefer a stop over a take-profit limit when only one sell can rest: a TP
    alone is not protection, and Alpaca will often refuse a second full-size
    sell while a limit sits on the book. Cancel non-stop sells first, then
    place the stop. Upside exit stays software-side (exhaustion left_overbought
    / close_out) — that path already cancels resting orders before selling.

    Managed rows and rows we just adopted are healed. True orphans that were
    never adopted (no stop level) are flattened when unprotected so they cannot
    sit naked like CELH/MLTX.

    Never opens new risk — only attaches a stop (or flattens if we cannot).
    """
    import alpaca_trader

    events: list[dict[str, Any]] = []
    if not unprotected or not _cfg_flag("ai_heal_unprotected", True):
        return events
    if not _cfg_flag("ai_broker_stop_enabled", True):
        return events
    for u in unprotected:
        sym = str(u.get("symbol") or "").upper()
        if not sym:
            continue
        # After adoption, managed flag on the probe may still be stale; trust
        # the state dict as source of truth.
        if sym not in state:
            # Unmanaged + unprotected + no adopt → flatten if enabled.
            if not _cfg_flag("ai_flatten_unmanaged_unprotected", True):
                continue
            try:
                alpaca_trader.cancel_open_orders(sym)
            except Exception:  # noqa: BLE001
                pass
            out = alpaca_trader.close_out(sym) or {}
            events.append({"ticker": sym, "event": "unmanaged_unprotected_flatten"})
            log_event(
                "unmanaged_unprotected_flatten", symbol=sym,
                order_id=(out or {}).get("order_id"),
            )
            continue
        pos = state.get(sym) or {}
        stop = _num(pos.get("stop_price"))
        entry = _num(pos.get("entry_price"))
        if not stop or not entry or stop >= entry:
            # No known stop — flatten rather than invent levels.
            try:
                alpaca_trader.cancel_open_orders(sym)
            except Exception:  # noqa: BLE001
                pass
            out = alpaca_trader.close_out(sym) or {}
            if isinstance(out, dict) and out.get("order_id"):
                pos["close_order_id"] = str(out["order_id"])
            pos["closing_reason"] = pos.get("closing_reason") or "unprotected_flatten"
            state[sym] = pos
            events.append({"ticker": sym, "event": "unprotected_flatten"})
            log_event("unprotected_flatten", symbol=sym)
            continue
        # Dual tranche: never cancel T1 to slap a full-size original stop on.
        # That undoes the scale-out book (ABCL 2026-08-12: unprotected_tp_cleared
        # then heal at the entry stop). Place a runner-sized stop only.
        qty_a = int(pos.get("qty_a") or 0)
        qty_b = int(pos.get("qty_b") or 0)
        dual = qty_a > 0 and qty_b > 0
        scaled = bool(pos.get("tranche_a_filled"))
        if dual:
            if scaled:
                want = (
                    _runner_stop_level(pos)
                    or breakeven_floor(pos.get("entry_price"), _cfg_all())
                    or stop
                )
            else:
                want = stop
            last = _num(pos.get("last_seen_price"))
            if want and last is not None and want >= last:
                if not pos.get("tranche_a_target_order_id") and not scaled:
                    pos["t1_attach_pending"] = True
                    state[sym] = pos
                continue
            qty_stop = qty_b
            out: dict[str, Any] = {}
            try:
                out = alpaca_trader.place_stop_sell(sym, want, qty=qty_stop) or {}
            except Exception:  # noqa: BLE001
                out = {}
            if not out.get("ok"):
                out = alpaca_trader.replace_stop(sym, None, stop_price=want) or {}
            if not out.get("ok"):
                out = _restop_at_available(sym, want, out) or out
            if isinstance(out, dict) and out.get("ok"):
                pos["tranche_b_stop_order_id"] = out.get("order_id") or pos.get(
                    "tranche_b_stop_order_id")
                pos["tranche_a_stop_order_id"] = pos["tranche_b_stop_order_id"]
                if scaled:
                    pos["runner_stop_price"] = want
                    pos["stop_price"] = want
                if not pos.get("tranche_a_target_order_id") and not scaled:
                    pos["t1_attach_pending"] = True
                state[sym] = pos
                events.append({"ticker": sym, "event": "unprotected_healed",
                               "stop_price": want, "qty": qty_stop,
                               "kept_t1": True})
                log_event("unprotected_healed", symbol=sym, stop_price=want,
                          qty=qty_stop, kept_t1=True)
            else:
                log_event(
                    "unprotected_heal_failed", symbol=sym,
                    reason=str((out or {}).get("error") or (out or {}).get("status"))[:160],
                )
            continue

        # Single-tranche / adopted: drop take-profit limits so a full-size
        # protective stop can rest. Dual books never reach here.
        had_non_stop_sell = False
        try:
            for o in alpaca_trader.get_open_orders() or []:
                if str(o.get("symbol") or "").upper() != sym:
                    continue
                if str(o.get("side") or "").lower() != "sell":
                    continue
                if not _is_protective_stop_order(o):
                    had_non_stop_sell = True
                    break
            if had_non_stop_sell:
                alpaca_trader.cancel_open_orders(sym)
                log_event(
                    "unprotected_tp_cleared", symbol=sym,
                    note="prefer_stop_over_target",
                )
        except Exception:  # noqa: BLE001
            try:
                alpaca_trader.cancel_open_orders(sym)
            except Exception:  # noqa: BLE001
                pass
        out = alpaca_trader.replace_stop(sym, None, stop_price=stop)
        # replace_stop always sizes to the full open quantity, which is exactly
        # what Alpaca refuses while any of it is held for another order.
        if not (isinstance(out, dict) and out.get("ok")):
            out = _restop_at_available(sym, stop, out) or out
        if isinstance(out, dict) and out.get("ok"):
            pos["tranche_b_stop_order_id"] = out.get("order_id") or pos.get(
                "tranche_b_stop_order_id")
            # Target is no longer resting; software exhaustion owns the upside.
            pos["tranche_a_target_order_id"] = None
            state[sym] = pos
            events.append({"ticker": sym, "event": "unprotected_healed",
                           "stop_price": stop})
            log_event("unprotected_healed", symbol=sym, stop_price=stop)
        else:
            log_event(
                "unprotected_heal_failed", symbol=sym,
                reason=str((out or {}).get("error") or (out or {}).get("status"))[:160],
            )
    return events


def _record_outcome(ticker: str, pos: dict[str, Any], exit_price: float | None,
                    close_reason: str, now: float) -> dict[str, Any]:
    entry_price = pos.get("entry_price") or 0
    stop_price = pos.get("stop_price") or 0
    total_qty = pos.get("total_qty") or 0
    # Against the ENTRY stop, not wherever the stop was moved to. A breakeven
    # or ratcheted stop makes entry-stop zero or negative, which turned
    # realized_r into None and quietly excluded the trade from the daily-loss
    # gate — the more a trade was actively managed, the less it counted.
    per_share_risk = _risk_basis(pos)

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
        "hold_sec": round(now - float(entry_time or now), 1),
        "reward_risk_planned": pos.get("reward_risk"),
        "summary": pos.get("summary"),
        "strategy": pos.get("strategy"),
        # Which geometry drew the zone this trade entered on. A double-bottom
        # fill and a measured-pullback-band fill are different strategies, and
        # averaging them into one realized-R number is what the arm gate in
        # ai_entry_watch originally refused offset zones to prevent. Recorded
        # so performance_summary and the replay tooling can slice by it.
        "zone_kind": pos.get("zone_kind"),
        "entry_exhaustion": pos.get("entry_exhaustion"),
        "entry_exhaustion_state": pos.get("entry_exhaustion_state"),
        # watch | suggest | adopted — see place_scaled_entry. Without it the
        # ledger cannot say which rows the exhaustion gate ever governed.
        "entry_path": pos.get("entry_path") or "unknown",
        "exit_exhaustion": pos.get("last_exhaustion"),
        "mae_r": pos.get("mae_r"),
        "mfe_r": pos.get("mfe_r"),
        # Cost of crossing on the way in, in R. None until a fill is observed
        # against a limit — never estimated from a quote.
        "entry_slippage_r": pos.get("entry_slippage_r"),
        # ...and on the way out. Only meaningful for a shelf exit, where the
        # shelf is the price we intended to get: negative means the fill
        # landed BELOW the shelf, which is the ratchet's real cost and was
        # invisible before 2026-08-23. None for stops, targets and flattens,
        # which have no intended price to miss.
        "exit_shelf_price": _num(pos.get("local_stop_price")),
        "exit_slippage_r": (
            (exit_price - _num(pos.get("local_stop_price"))) / per_share_risk
            if (close_reason == "local_trail" and exit_price
                and per_share_risk > 0
                and _num(pos.get("local_stop_price")) is not None)
            else None),
        "peak_price": _num(pos.get("peak_price")),
        "give_r_at_exit": (
            (_num(pos.get("peak_price")) - _num(pos.get("local_stop_price")))
            / per_share_risk
            if (per_share_risk > 0 and _num(pos.get("peak_price")) is not None
                and _num(pos.get("local_stop_price")) is not None)
            else None),
        # Entry-side reading, carried onto the outcome so cost analysis does
        # not need a join back into shadow. Coverage was 8.2% before this.
        "spread_r": _pos_spread_r(pos),
        # How many discretionary exits ai_exit_min_hold_sec suppressed on this
        # trade. 0 means the delay never bound and the trade is a control.
        "min_hold_blocks": int(pos.get("min_hold_blocks") or 0),
        "min_hold_last": pos.get("min_hold_last"),
        # Why this trade was taken, alongside how it ended. Without it an
        # outcome is unsliceable: you know the result but not which gate,
        # indicator state, or time of day to attribute it to.
        "features": pos.get("features"),
        "pctr_src": pos.get("pctr_src") or (pos.get("features") or {}).get("pctr_src"),
        "cm_rsi_src": (
            pos.get("cm_rsi_src")
            or (pos.get("features") or {}).get("cm_rsi_src")
        ),
        "bars_age_sec": (
            pos.get("bars_age_sec")
            if pos.get("bars_age_sec") is not None
            else (pos.get("features") or {}).get("bars_age_sec")
        ),
        # Book source (trending / momentum / research). 2026-08-11 outcomes
        # lacked this so "did we trade trending?" needed a join against trades.
        "source": (
            pos.get("duel_source")
            or (pos.get("features") or {}).get("source")
            or pos.get("source")
        ),
        "duel_source": pos.get("duel_source"),
        # Prefer stamps frozen on the position at entry; fall back to live
        # regime so adopted/legacy rows still carry something.
        "edge_mode": pos.get("edge_mode"),
        "exit_left_overbought": pos.get("exit_left_overbought"),
        "git_version": pos.get("git_version"),
        "config_fp": pos.get("config_fp"),
        "paper": pos.get("paper"),
        "book_owner": pos.get("book_owner") or pos.get("duel_source"),
    }
    outcome = merge_regime(outcome)
    try:
        OUTCOMES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUTCOMES_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(outcome) + "\n")
    except Exception as e:  # noqa: BLE001
        log_event(
            "outcome_write_failed", symbol=ticker,
            reason=str(e)[:160], close_reason=close_reason,
        )
    _note_day_trade(entry_time, realized_pl)
    return outcome


def reconcile_broker(now: float | None = None) -> dict[str, Any]:
    """Compare managed state to live Alpaca positions.

    - unmanaged: live positions not in local state (human/engine/other)
    - adoption: orphans with a recoverable stop (entry_ok / resting stop)
      are re-homed into managed so exhaustion + heal apply
    - unprotected: live longs without a resting stop → heal or flatten
    """
    global _last_reconcile
    import alpaca_trader

    now = time.time() if now is None else now
    with _state_lock:
        state = _load_state()
        detail = alpaca_trader.get_positions_detail() or {}
        # Normalize detail keys to upper for reliable lookups.
        detail_u = {str(k).upper(): v for k, v in detail.items()}
        managed = {str(k).upper() for k in state.keys()}
        live = set(detail_u.keys())
        unmanaged = sorted(live - managed)

        adopt_events = _adopt_unmanaged(unmanaged, detail_u, state, now)
        if adopt_events:
            managed = {str(k).upper() for k in state.keys()}
            unmanaged = sorted(live - managed)

        confirmed = {
            k for k, p in state.items()
            if p.get("entry_confirmed") and not p.get("closing_reason")
        }
        missing_live = sorted(confirmed - live)
        unconfirmed = sorted(
            k for k, p in state.items() if not p.get("entry_confirmed")
        )
        # ── Safety invariant: every open position must have a resting STOP ──
        # A take-profit LIMIT alone is not protection — it only works if price
        # rises. Treating any SELL as protected left RIOT-style books with an
        # upper limit and no stop, and heal never ran. Stop / stop_limit /
        # trailing_stop count; plain limit does not.
        unprotected: list[dict[str, Any]] = []
        try:
            open_orders = alpaca_trader.get_open_orders(limit=100) or []
            protected = {
                str(o.get("symbol") or "").upper()
                for o in open_orders
                if _is_protective_stop_order(o)
            }
            equity = alpaca_trader.get_equity() or 0.0
            # With ai_broker_stop_enabled off — the desk's deliberate setting —
            # no long ever carries a resting stop, so this fired on every
            # managed position on every reconcile: 62 alarms on 2026-08-21,
            # all of them "managed": true and every one carrying a live
            # software shelf. An alarm that cries wolf 62 times a day is worse
            # than none, because the one real naked position is invisible
            # inside it.
            #
            # The shelf IS the protection in that mode: apply_local_trail
            # market-flattens on any print at or under local_stop_price, on the
            # 0.25s shelf tick. A managed row without one is still genuinely
            # unprotected and still alarms — that is the case worth seeing.
            software_ok = not _cfg_flag("ai_broker_stop_enabled", True)
            for sym, p in detail_u.items():
                s = str(sym).upper()
                if s in protected:
                    continue
                if software_ok:
                    row = state.get(s) if isinstance(state, dict) else None
                    shelf = _num((row or {}).get("local_stop_price"))
                    if row is not None and shelf and shelf > 0:
                        continue
                try:
                    mv = abs(float(p.get("mkt_val") or 0.0))
                except (TypeError, ValueError):
                    mv = 0.0
                unprotected.append({
                    "symbol": s,
                    "mkt_val": round(mv, 2),
                    "pct_equity": (round(100.0 * mv / equity, 1)
                                   if equity > 0 else None),
                    "managed": s in managed,
                })
        except Exception:
            pass

        heal_events: list[dict[str, Any]] = []
        if unprotected:
            heal_events = _heal_unprotected(unprotected, state)

        changed = bool(adopt_events or heal_events)
        if changed:
            _save_state(state)

        report = {
            "ts": now,
            "unmanaged": unmanaged,
            "unconfirmed": unconfirmed,
            "missing_live_confirmed": missing_live,
            "n_managed": len(managed),
            "n_live": len(live),
            "unprotected": unprotected,
        }
        if adopt_events:
            report["adopt_events"] = adopt_events
        if heal_events:
            report["heal_events"] = heal_events

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
    """Desk-tick check, no LLM: closures, scale-out, dead-trade, defense.

    Capital first, profit second: keep a stop and an exhaustion exit on every
    long. Concurrent entry_ok must not drop a symbol (see _state_lock).

    Cheap enough to run every tick — everything it needs (position detail,
    order status, a breakeven/trailing-stop replacement, a time-boxed close)
    is a plain Alpaca call, not a model call.
    """
    import alpaca_trader

    now = time.time() if now is None else now
    events: list[dict[str, Any]] = []
    changed = False
    ttl = max(60.0, float(unconfirmed_ttl_sec))
    flatten_px: dict[str, float] = {}

    detail_raw = alpaca_trader.get_positions_detail() or {}
    detail = {str(k).upper(): v for k, v in detail_raw.items()}

    with _state_lock:
        state = _load_state()
        snapshot_keys = set(state.keys())

    # Pass 1: has anything gone fully flat since the last tick? Catches every
    # closing mechanism at once — hard stop, first target then a later
    # trailing/breakeven stop, or an explicit close_out from time-stop or
    # thesis-break above — without needing to track each order leg by hand.
    for ticker, pos in list(state.items()):
        live = detail.get(str(ticker).upper())
        if live is not None:
            if not pos.get("entry_confirmed"):
                # First sighting: replace the submit-time ask with what the
                # order actually filled at, so realized R is measured against
                # the real basis rather than an estimate.
                fill = _order_fill_price(pos.get("tranche_a_order_id"))
                if fill is None:
                    fill = _num(live.get("avg_entry_price"))
                if fill and fill > 0:
                    # What crossing actually cost, in this trade's own risk
                    # unit — measured against the limit we asked for, before
                    # entry_price is overwritten by the fill.
                    #
                    # This is the honest instrument for the spread question.
                    # A percent-of-mid cap is read off the IEX book, which is a
                    # few percent of the tape and always looks wide, so gating
                    # on it blocks good fills; this measures the price we really
                    # paid. Positive = we paid up. Feed a few days of these into
                    # ai_max_spread_r before turning that gate on.
                    want = _num(pos.get("entry_limit_price"))
                    risk = _num(pos.get("risk_per_share"))
                    if want and want > 0 and risk and risk > 0:
                        pos["entry_slippage_r"] = round((fill - want) / risk, 4)
                    pos["entry_price"] = fill
                    tape = _num(live.get("current")) or fill
                    planned = _num(pos.get("entry_stop_price")) or _num(
                        pos.get("stop_price"))
                    intended = _num(pos.get("entry_limit_price")) or fill
                    risk = _num(pos.get("risk_per_share"))
                    if (
                        fill_already_dead(
                            tape, fill, planned, risk, cfg=_cfg_all())
                        or fill_already_dead(
                            fill, intended, planned, risk, cfg=_cfg_all())
                    ):
                        alpaca_trader.cancel_open_orders(ticker)
                        out = alpaca_trader.close_out(ticker) or {}
                        if isinstance(out, dict) and out.get("order_id"):
                            pos["close_order_id"] = str(out["order_id"])
                        pos["entry_confirmed"] = True
                        pos["closing_reason"] = "fill_through_stop"
                        events.append({
                            "ticker": ticker, "event": "fill_through_stop",
                            "fill": fill, "last": tape, "stop": planned,
                        })
                        log_event(
                            "fill_through_stop", symbol=ticker,
                            fill=fill, last=tape, stop=planned,
                        )
                        changed = True
                        continue
            pos["entry_confirmed"] = True
            # High print ratchets the shelf; low print is the liquidation
            # trigger so a tape dip through TRAIL sells even if the broker
            # mark is still above.
            hi, lo = _tick_prints(ticker, live)
            # Raise the shelf off the freshest print available, not the broker
            # mark. live["current"] only moves when the positions poll refreshes,
            # so the damped median was averaging repeats of a stale number and
            # the shelf trailed price by a poll or more — NKE/TEM 2026-08-19 both
            # carried three identical trail_prints while the tape had moved.
            # The median damping is unchanged; this only changes what it damps.
            raw = _fresh_tape_px(ticker)
            if raw is None:
                raw = _num((live or {}).get("current"))
            if raw is None:
                raw = _num((live or {}).get("current_price"))
            if raw is None:
                raw = lo or hi
            # Ring size is config so the damping/lag trade can be swept. Three
            # prints is up to two extra polls before a new high reaches the
            # shelf; two keeps the "one spike cannot lift it" guarantee, since
            # a lone outlier still has to survive a median against its
            # neighbour, at half the lag.
            note_trail_print(pos, raw, n=local_trail_ring(0.0, _cfg_all()))
            if hi is not None:
                pos["last_seen_price"] = hi
            elif live.get("current") is not None:
                pos["last_seen_price"] = live.get("current")
            if lo is not None:
                flatten_px[str(ticker).upper()] = lo
            _update_excursions(pos, pos.get("last_seen_price") or live.get("current"))
            # Blind book: no stream and no REST for ai_stale_data_max_age_sec
            # during RTH → market flatten. Local trail cannot protect a ghost.
            if (
                pos.get("entry_confirmed")
                and not pos.get("closing_reason")
                and _cfg_flag("ai_stale_data_flatten", True)
                and _rth_now(now)
            ):
                live_ok, live_src = quote_is_live(ticker, _cfg_all())
                if live_ok:
                    pos.pop("stale_since", None)
                    pos["last_live_data_ts"] = now
                else:
                    started = _num(pos.get("stale_since"))
                    if started is None:
                        pos["stale_since"] = now
                        started = now
                    try:
                        stale_max = float(_cfg_all().get(
                            "ai_stale_data_max_age_sec",
                            DEFAULT_STALE_DATA_MAX_AGE_SEC,
                        ) or DEFAULT_STALE_DATA_MAX_AGE_SEC)
                    except (TypeError, ValueError):
                        stale_max = DEFAULT_STALE_DATA_MAX_AGE_SEC
                    if float(now) - float(started) + 1e-9 >= stale_max:
                        alpaca_trader.cancel_open_orders(ticker)
                        out = alpaca_trader.close_out(ticker) or {}
                        if isinstance(out, dict) and out.get("order_id"):
                            pos["close_order_id"] = str(out["order_id"])
                        pos["closing_reason"] = "stale_data"
                        events.append({
                            "ticker": ticker, "event": "stale_data",
                            "src": live_src, "stale_sec": round(
                                float(now) - float(started), 1),
                        })
                        log_event(
                            "stale_data", symbol=ticker, src=live_src,
                            stale_sec=round(float(now) - float(started), 1),
                        )
                        changed = True
                        continue
            if _infer_t1_fill(pos, live.get("qty")):
                pos["tranche_a_filled"] = True
                log_event(
                    "t1_fill_inferred", symbol=ticker,
                    live_qty=live.get("qty"),
                    qty_a=pos.get("qty_a"), qty_b=pos.get("qty_b"),
                )
                events.append({
                    "ticker": ticker, "event": "t1_fill_inferred",
                    "live_qty": live.get("qty"),
                })
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
        exit_price, observed_reason = resolve_exit(pos, ticker)
        # An explicit closing_reason is the desk saying why IT closed this
        # (time_stop, thesis_break, ...) and outranks forensics. Otherwise take
        # what actually filled — including "unknown".
        reason = pos.get("closing_reason") or observed_reason
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
    # It tightens rather than closes. Moving the stop to entry caps the loss
    # at a scratch and keeps the target live if the signal is wrong.
    # One indicator read per tick, shared by the defence below and the exit
    # shadow log — not one per position, against a rate limit four processes
    # already share.
    indicators = _engine_indicators() if state else {}
    exit_why: dict[str, str] = {t: "hold" for t in state}

    if _sell_signal_defends(state):
        exh_on = _cfg_flag("ai_watch_exhaustion_rules", True)
        for ticker, pos in list(state.items()):
            if pos.get("sell_signal_stop_done") or pos.get("closing_reason"):
                continue
            if pos.get("late_hold"):
                continue
            try:
                import desk_h4 as _desk_h4
                if _desk_h4.is_h4_pos(pos):
                    continue
            except Exception:
                pass
            sig = indicators.get(ticker)
            if not isinstance(sig, dict) or not sig.get("sell_signal"):
                continue
            # Per symbol, not per desk: a name WITH a %R reading is governed by
            # the exhaustion rules and must not have MACD/CM RSI-2 moving its
            # stop. A name without one is on the old logic and keeps it.
            if exh_on and sig.get("pctr") is not None:
                continue
            entry = _num(pos.get("entry_price"))
            last = _num(pos.get("last_seen_price"))
            cur_stop = _num(pos.get("stop_price"))
            if not entry or not last:
                continue
            # A stop is only a stop while it sits BELOW the market. Underwater,
            # "move it to breakeven" places it above the last print, which
            # Alpaca triggers on receipt — leave the original stop to work.
            # Tested against the breakeven level itself, not the bare fill:
            # with the penny offset, a print between entry and entry+0.01 is
            # still "underwater" for this purpose.
            be = breakeven_floor(entry, _cfg_all()) or entry
            if last <= be:
                events.append({
                    "ticker": ticker, "event": "sell_signal_underwater",
                    "entry": entry, "last": last, "stop": cur_stop,
                })
                log_event("sell_signal_underwater", symbol=ticker,
                          entry=entry, last=last, stop=cur_stop)
                exit_why[ticker] = "sell_signal_underwater"
                pos["sell_signal_stop_done"] = True
                changed = True
                continue
            # Never loosen: a stop already at or above entry is better than
            # entry. Includes the runner's ratcheted level — that lives in
            # runner_stop_price, not stop_price, so comparing stop_price alone
            # would walk a trailed-up runner back down to breakeven.
            best_stop = max(
                [x for x in (cur_stop, _num(pos.get("runner_stop_price")))
                 if x is not None],
                default=None,
            )
            if best_stop is not None and best_stop >= be:
                pos["sell_signal_stop_done"] = True
                changed = True
                continue
            out = alpaca_trader.replace_stop(
                ticker, _resting_stop_order_id(ticker), stop_price=be)
            if isinstance(out, dict) and out.get("ok"):
                pos["stop_price"] = be
                pos["sell_signal_stop_done"] = True
                changed = True
                events.append({
                    "ticker": ticker, "event": "sell_signal_breakeven",
                    "from_stop": cur_stop, "to_stop": be, "last": last,
                })
                log_event("sell_signal_breakeven", symbol=ticker,
                          from_stop=cur_stop, to_stop=be, last=last)
                exit_why[ticker] = "sell_signal_breakeven"

    # Pass 2: tranche-A fill, dead-trade, day time-stop.
    dead_min = _cfg_float("ai_dead_trade_min", DEFAULT_DEAD_TRADE_MIN)
    dead_mfe = _cfg_float("ai_dead_trade_mfe_r", DEFAULT_DEAD_TRADE_MFE_R)

    step_r = max(0.0, _cfg_float("ai_runner_step_r", DEFAULT_RUNNER_STEP_R))

    for ticker, pos in list(state.items()):
        # Keep the high-water mark current BEFORE the ratchet reads it. The
        # shadow-log block at the bottom also calls this, but it is behind
        # ai_position_shadow_enabled — the runner's stop must not depend on
        # whether logging happens to be on.
        _update_excursions(pos, _num(pos.get("last_seen_price")))

        # Local profit trail: raise a software shelf under the high and
        # market-flatten if last prints through it. Does not wait for T1.
        _ch, _closed = apply_local_trail(
            ticker, pos, flatten_px.get(str(ticker).upper()),
            events, exit_why)
        if _ch:
            changed = True
        if _closed:
            continue

        # Dual profit bank: free shares held by the full-size stop, then either
        # market-scale at/through T1 or rest a partial T1 + stop on the runner.
        # Without free_sell_capacity, Alpaca rejects partials with available:0
        # (held_for_orders = full qty) and winners never bank.
        if (
            pos.get("entry_confirmed")
            and not pos.get("tranche_a_filled")
            and not pos.get("closing_reason")
            and int(pos.get("qty_b") or 0) > 0
        ):
            t1 = _num(pos.get("target_1"))
            last = _num(pos.get("last_seen_price"))
            qty_a = int(pos.get("qty_a") or 0)
            qty_b = int(pos.get("qty_b") or 0)
            stop_px = _num(pos.get("stop_price"))
            through_t1 = (
                t1 is not None and last is not None and last + 1e-9 >= t1
            )

            def _rearm_stop(qty_stop: int, level: float | None) -> dict:
                if not _cfg_flag("ai_broker_stop_enabled", True):
                    return {"ok": False, "error": "broker_stop_off"}
                if level is None or level <= 0 or qty_stop < 1:
                    return {"ok": False, "error": "bad_stop"}
                if last is not None and level >= last:
                    # Never place a stop that would trigger immediately.
                    return {"ok": False, "error": "stop_at_or_above_market"}
                return alpaca_trader.place_stop_sell(
                    ticker, level, qty=qty_stop) or {}

            # Through T1 (or attach-pending market path): free → sell A → stop B.
            if (
                qty_a > 0
                and t1
                and through_t1
                and (
                    pos.get("t1_attach_pending")
                    or not pos.get("tranche_a_target_order_id")
                )
            ):
                alpaca_trader.free_sell_capacity(ticker)
                sold = alpaca_trader.sell_qty_market(ticker, qty_a) or {}
                if sold.get("ok"):
                    pos["tranche_a_filled"] = True
                    pos["t1_attach_pending"] = False
                    pos["tranche_a_target_order_id"] = sold.get("order_id")
                    _freeze_risk_per_share(pos)
                    want = _runner_stop_level(pos)
                    if want is None:
                        want = max(
                            x for x in (
                                _num(pos.get("entry_price")), stop_px
                            ) if x is not None
                        ) if (_num(pos.get("entry_price")) or stop_px) else stop_px
                    rear = _rearm_stop(qty_b, want)
                    if rear.get("ok"):
                        pos["tranche_b_stop_order_id"] = rear.get("order_id")
                        pos["tranche_a_stop_order_id"] = rear.get("order_id")
                        pos["runner_stop_price"] = want
                        pos["stop_price"] = want
                        pos["breakeven_done"] = True
                    log_event(
                        "t1_market_scale", symbol=ticker,
                        qty=qty_a, target=t1, last=last,
                        runner_stop=want,
                        stop_ok=bool(rear.get("ok")),
                    )
                    events.append({
                        "ticker": ticker, "event": "t1_market_scale",
                        "qty": qty_a, "target": t1, "last": last,
                        "runner_stop": want,
                    })
                    exit_why[ticker] = "t1_market_scale"
                    changed = True
                else:
                    # Re-arm original stop so we are never naked after free.
                    if stop_px:
                        rear = _rearm_stop(qty_a + qty_b, stop_px)
                        if rear.get("ok"):
                            pos["tranche_a_stop_order_id"] = rear.get("order_id")
                            pos["tranche_b_stop_order_id"] = rear.get("order_id")
                    pos["t1_attach_cooldown_until"] = now + 90.0
                    log_event(
                        "t1_market_scale_failed", symbol=ticker,
                        error=str(sold.get("error") or sold.get("status"))[:160],
                        retry_in_sec=90,
                    )

            # Not yet at T1: free → partial limit on A + stop on B only.
            # Also retry when heal wiped the target oid (pending may be false).
            elif (
                not pos.get("tranche_a_target_order_id")
                and t1
                and qty_a > 0
                and not through_t1
                and float(pos.get("t1_attach_cooldown_until") or 0) <= now
            ):
                alpaca_trader.free_sell_capacity(ticker)
                att = alpaca_trader.place_limit_sell(ticker, qty_a, t1) or {}
                if att.get("ok") and att.get("order_id"):
                    pos["tranche_a_target_order_id"] = att["order_id"]
                    pos["t1_attach_pending"] = False
                    pos["t1_attach_cooldown_until"] = None
                    # Runner stop only (qty_b). A is protected by software:
                    # if last <= stop, full close_out below.
                    rear = _rearm_stop(qty_b, stop_px)
                    if rear.get("ok"):
                        pos["tranche_b_stop_order_id"] = rear.get("order_id")
                        pos["tranche_a_stop_order_id"] = rear.get("order_id")
                    log_event(
                        "t1_limit_attached", symbol=ticker,
                        qty=att.get("qty", qty_a), target=t1,
                        order_id=att.get("order_id"),
                        runner_stop_ok=bool(rear.get("ok")),
                    )
                    events.append({
                        "ticker": ticker, "event": "t1_limit_attached",
                        "qty": att.get("qty", qty_a), "target": t1,
                        "order_id": att.get("order_id"),
                    })
                    exit_why[ticker] = "t1_limit_attached"
                    changed = True
                else:
                    if stop_px:
                        rear = _rearm_stop(qty_a + qty_b, stop_px)
                        if rear.get("ok"):
                            pos["tranche_a_stop_order_id"] = rear.get("order_id")
                            pos["tranche_b_stop_order_id"] = rear.get("order_id")
                    pos["t1_attach_pending"] = True
                    pos["t1_attach_cooldown_until"] = now + 90.0
                    log_event(
                        "t1_limit_attach_failed", symbol=ticker,
                        error=str(att.get("error") or att.get("status"))[:160],
                        retry_in_sec=90,
                    )

            # Soft protection for the A leg while only B has a broker stop:
            # if price is at/under the planned stop, flatten everything.
            if (
                not pos.get("tranche_a_filled")
                and pos.get("tranche_a_target_order_id")
                and stop_px
                and last is not None
                and last <= stop_px
            ):
                alpaca_trader.free_sell_capacity(ticker)
                out = alpaca_trader.close_out(ticker) or {}
                if isinstance(out, dict) and out.get("order_id"):
                    pos["close_order_id"] = str(out["order_id"])
                pos["closing_reason"] = "stop_software_full"
                exit_why[ticker] = "stop_software_full"
                events.append({
                    "ticker": ticker, "event": "stop_software_full",
                    "last": last, "stop": stop_px,
                })
                log_event("stop_software_full", symbol=ticker,
                          last=last, stop=stop_px)
                changed = True
                continue

        # Tranche A fill -> runner stop to max(breakeven, peak-nR).
        # Key off the take-profit leg (or market scale flag above).
        target_oid = pos.get("tranche_a_target_order_id")
        if not pos.get("breakeven_done") and (
            pos.get("tranche_a_filled")
            or target_oid
        ):
            filled = bool(pos.get("tranche_a_filled"))
            if not filled and target_oid:
                order = alpaca_trader.get_order(target_oid)
                if order and str(order.get("status") or "").lower() == "filled":
                    filled = True
                    pos["tranche_a_filled"] = True
            if filled and not pos.get("breakeven_done"):
                if pos.get("qty_b", 0) > 0:
                    _freeze_risk_per_share(pos)
                    want = _runner_stop_level(pos)
                    last = _num(pos.get("last_seen_price"))
                    stop_oid = (
                        pos.get("tranche_b_stop_order_id")
                        or pos.get("tranche_a_stop_order_id")
                    )
                    if want is None or (last is not None and want >= last):
                        events.append({
                            "ticker": ticker, "event": "runner_stop_skipped",
                            "want": want, "last": last,
                        })
                        log_event("runner_stop_skipped", symbol=ticker,
                                  want=want, last=last)
                    else:
                        # Free any stale full stop, then arm runner size only.
                        alpaca_trader.free_sell_capacity(ticker)
                        out = alpaca_trader.place_stop_sell(
                            ticker, want, qty=int(pos.get("qty_b") or 0),
                        ) or {}
                        if not out.get("ok"):
                            out = alpaca_trader.replace_stop(
                                ticker, stop_oid, stop_price=want,
                            ) or {}
                        if out.get("ok"):
                            pos["tranche_b_stop_order_id"] = out.get("order_id")
                            pos["tranche_a_stop_order_id"] = out.get("order_id")
                            pos["runner_stop_price"] = want
                            pos["stop_price"] = want
                            events.append({
                                "ticker": ticker, "event": "scaled_out",
                                "target_1": pos.get("target_1"),
                                "runner_stop": want,
                                "breakeven": _num(pos.get("entry_price")),
                            })
                            log_event(
                                "runner_stop_set", symbol=ticker,
                                stop=want, entry=pos.get("entry_price"),
                                peak=pos.get("peak_price"),
                                trail_r=pos.get("runner_trail_r"),
                            )
                            exit_why[ticker] = "scaled_out"
                        else:
                            log_event(
                                "runner_stop_failed", symbol=ticker,
                                want=want, error=str(out.get("error"))[:160],
                            )
                pos["breakeven_done"] = True
                changed = True

        if pos.get("closing_reason"):
            continue

        # Ratchet: once the runner is on a breakeven/trail stop, follow price
        # up. Only ever raises, and only when the gain clears step_r so a 5s
        # tick does not turn a fast move into dozens of cancel+submit pairs.
        if (
            pos.get("tranche_a_filled")
            and pos.get("qty_b", 0) > 0
            and pos.get("runner_stop_price") is not None
        ):
            want = _runner_stop_level(pos)
            cur = _num(pos.get("runner_stop_price"))
            last = _num(pos.get("last_seen_price"))
            risk = _risk_basis(pos)
            step = max(0.01, step_r * risk)
            if (
                want is not None
                and cur is not None
                and want >= cur + step
                and (last is None or want < last)
            ):
                stop_oid = (
                    pos.get("tranche_b_stop_order_id")
                    or pos.get("tranche_a_stop_order_id")
                )
                out = alpaca_trader.replace_stop(
                    ticker, stop_oid,
                    stop_price=want,
                ) or {}
                if out.get("ok"):
                    _freeze_risk_per_share(pos)
                    pos["tranche_b_stop_order_id"] = out.get("order_id")
                    pos["tranche_a_stop_order_id"] = out.get("order_id")
                    pos["runner_stop_price"] = want
                    pos["stop_price"] = want
                    changed = True
                    events.append({
                        "ticker": ticker, "event": "runner_stop_raised",
                        "from_stop": cur, "to_stop": want,
                        "peak": pos.get("peak_price"),
                    })
                    log_event("runner_stop_raised", symbol=ticker,
                              from_stop=cur, to_stop=want,
                              peak=pos.get("peak_price"))
                    exit_why[ticker] = "runner_stop_raised"

        # Exhaustion exit (left_overbought) — used only in exhaustion_scalp
        # mode. Under continuation (Option A / default after 2026-08-11) this
        # path is off: hold through overbought and bank via broker T1, runner
        # trail, dead_trade, or stop. See ai_entry_watch.left_overbought_exit_enabled.
        if _cfg_flag("ai_watch_exhaustion_rules", True) and pos.get("entry_confirmed"):
            sig = dict(indicators.get(ticker) or {})
            # Recompute %R against the live price before judging the fade. The
            # engine's copy is 60-120s old and carries no timestamp, so a stale
            # falling flag would otherwise be re-read every 5s and counted as
            # fresh confirmation of a fade that may already have reversed.
            live_px = _num(pos.get("last_seen_price"))
            if live_px:
                try:
                    import ai_entry_watch as _ew
                    got = _ew.live_exhaustion(ticker, live_px, _cfg_all(), now)
                    if got:
                        pctr_l, _ex, _ris, fall_l = got
                        sig["pctr"] = round(pctr_l, 2)
                        sig["pctr_falling"] = bool(fall_l)
                except Exception:
                    pass
            if sig.get("pctr") is not None:
                # Level crossing, evaluated on every position tick (5s) against
                # a live-price %R. No persistence window: the operator wants
                # the sell when the name leaves overbought, not two minutes
                # after it left.
                probe = {"symbol": ticker, "indicator": sig,
                         "exh_was_overbought": bool(pos.get("exh_was_overbought"))}
                try:
                    import ai_entry_watch as _ew2
                    cfg_exh = _cfg_all()
                    if pos.get("adopted"):
                        # An orphan has no continuation thesis — the desk did
                        # not open it and cannot reason about riding it through
                        # overbought. Under continuation / hybrid the global
                        # flag turns left_overbought off, which would leave
                        # adoption's exh_was_overbought latch arming an exit
                        # that can never fire, and the position running on the
                        # broker stop alone (dead_trade cannot catch it once
                        # MFE clears ai_dead_trade_mfe_r). Keep the flatten.
                        cfg_exh = {**cfg_exh, "ai_exit_left_overbought": True}
                    hit, why = _ew2.exhaustion_exit_now(probe, cfg_exh)
                except Exception:
                    hit, why = False, "error"
                # An indicator opinion is a discretionary exit; hold it back
                # with the shelf so the forward test measures one rule, not a
                # shelf delay that left_overbought quietly steps around.
                if hit and soft_exit_held_back(pos, now):
                    hit, why = False, "min_hold"
                    _note_min_hold(pos, "left_overbought", now)
                if pos.get("last_exhaustion") != _num(sig.get("pctr")):
                    pctr_v = _num(sig.get("pctr"))
                    pos["last_exhaustion"] = (
                        None if pctr_v is None else round(100.0 + pctr_v, 1))
                    changed = True
                if probe.get("exh_was_overbought") and not pos.get("exh_was_overbought"):
                    pos["exh_was_overbought"] = True
                    changed = True
                dual = (
                    int(pos.get("qty_a") or 0) > 0
                    and int(pos.get("qty_b") or 0) > 0
                )
                if hit and dual:
                    # Dual book banks via T1 + runner ratchet. Flattening here
                    # was 13/19 closes on 2026-08-12 and killed the raise.
                    log_event(
                        "left_overbought_deferred", symbol=ticker,
                        pctr=sig.get("pctr"),
                        tranche_a_filled=bool(pos.get("tranche_a_filled")),
                    )
                    events.append({
                        "ticker": ticker, "event": "left_overbought_deferred",
                        "pctr": sig.get("pctr"),
                    })
                    exit_why[ticker] = "left_overbought_deferred"
                    hit = False
                if hit:
                    alpaca_trader.cancel_open_orders(ticker)
                    out = alpaca_trader.close_out(ticker) or {}
                    if isinstance(out, dict) and out.get("order_id"):
                        pos["close_order_id"] = str(out["order_id"])
                    pos["closing_reason"] = "left_overbought"
                    exit_why[ticker] = "left_overbought"
                    events.append({
                        "ticker": ticker, "event": "left_overbought",
                        "pctr": sig.get("pctr"),
                    })
                    log_event("left_overbought", symbol=ticker,
                              pctr=sig.get("pctr"))
                    changed = True
                    continue

        # Day-scalp dead trade: no scale-out, never ran (MFE < 0.10R).
        # A shelf already above entry means the trail locked profit — leave it.
        # Sitting at breakeven is not a prove; flatten after the clock.
        _loc = _num(pos.get("local_stop_price"))
        _ent = _num(pos.get("entry_price"))
        profit_locked = (
            _loc is not None and _ent is not None and _loc > _ent + 1e-9
        )
        if (
            dead_min > 0
            and not pos.get("tranche_a_filled")
            and pos.get("entry_confirmed")
            and not profit_locked
        ):
            age_min = (now - float(pos.get("entry_time") or now)) / 60.0
            mfe = _num(pos.get("mfe_r"))
            mfe_ok = mfe is None or mfe < dead_mfe
            # Same split as the shelf sale above: count the held-back case
            # without changing which ticks close a trade.
            _dead_ready = age_min >= dead_min and mfe_ok
            _dead_held = _dead_ready and soft_exit_held_back(pos, now)
            if _dead_held:
                _note_min_hold(pos, "dead_trade", now)
            if _dead_ready and not _dead_held:
                alpaca_trader.cancel_open_orders(ticker)
                out = alpaca_trader.close_out(ticker) or {}
                if isinstance(out, dict) and out.get("order_id"):
                    pos["close_order_id"] = str(out["order_id"])
                pos["closing_reason"] = "dead_trade"
                exit_why[ticker] = "dead_trade"
                events.append({
                    "ticker": ticker, "event": "dead_trade",
                    "age_min": round(age_min, 1),
                    "mfe_r": mfe,
                })
                log_event(
                    "dead_trade", symbol=ticker,
                    age_min=round(age_min, 1), mfe_r=mfe,
                )
                changed = True
                continue

        # Multi-day time stop (research / swing only).
        days = pos.get("time_stop_days")
        if days and not pos.get("tranche_a_filled"):
            age_days = (now - pos.get("entry_time", now)) / 86400.0
            if age_days > days:
                alpaca_trader.cancel_open_orders(ticker)
                out = alpaca_trader.close_out(ticker) or {}
                if isinstance(out, dict) and out.get("order_id"):
                    pos["close_order_id"] = str(out["order_id"])
                pos["closing_reason"] = "time_stop"
                exit_why[ticker] = "time_stop"
                events.append({"ticker": ticker, "event": "time_stop",
                              "age_days": round(age_days, 1)})
                changed = True

    # Exit-side shadow log. Written last so it records the state the tick
    # actually left the position in, including "hold" (decisions NOT taken).
    if bool(_cfg_flag("ai_position_shadow_enabled", True)):
        for ticker, pos in list(state.items()):
            price = _num(pos.get("last_seen_price"))
            _update_excursions(pos, price)
            _log_position_shadow(
                ticker, pos, price, indicators.get(ticker),
                exit_why.get(ticker, "hold"), now,
            )
        if state:
            changed = True     # excursions moved

    if changed:
        with _state_lock:
            # Merge concurrent entry_ok symbols in; our tick wins on keys we
            # still hold or deleted (capital book integrity).
            concurrent = _load_state()
            out = dict(concurrent)
            for k in snapshot_keys:
                if k in state:
                    out[k] = state[k]
                else:
                    out.pop(k, None)
            for k, v in state.items():
                out[k] = v
            _save_state(out)

    try:
        reconcile_broker(now)
    except Exception as e:  # noqa: BLE001
        log_event("reconcile_error", reason=str(e)[:160])

    try:
        orders = alpaca_trader.get_open_orders(limit=100) or []
        inv = evaluate_ratchet_invariants(state, detail, orders)
        for row in inv:
            ev = row.get("event")
            sym = row.get("symbol")
            if row.get("ok"):
                if ev == "ratchet_ok" and row.get("scaled"):
                    # One-shot when the runner is locked; not every tick.
                    key = f"ok:{sym}"
                    if _ratchet_fail_last.get(key) != -1:
                        log_event("ratchet_ok", **{
                            k: row[k] for k in row
                            if k not in ("event",)
                        })
                        events.append({
                            "ticker": sym, "event": "ratchet_ok",
                            "best_stop": row.get("best_stop"),
                            "entry": row.get("entry"),
                        })
                        _ratchet_fail_last[key] = -1
                continue
            key = f"{sym}:{ev}"
            last_log = _ratchet_fail_last.get(key) or 0.0
            if now - last_log < _RATCHET_FAIL_THROTTLE_SEC:
                continue
            _ratchet_fail_last[key] = now
            _ratchet_fail_last.pop(f"ok:{sym}", None)
            log_event(str(ev), **{k: row[k] for k in row if k != "event"})
            events.append({
                "ticker": sym, "event": ev,
                "best_stop": row.get("best_stop"),
                "entry": row.get("entry"),
                "live_qty": row.get("live_qty"),
            })
    except Exception as e:  # noqa: BLE001
        log_event("ratchet_check_error", reason=str(e)[:160])

    return events


def performance_summary(since: float | None = None) -> dict[str, Any]:
    """Aggregate ``outcomes.jsonl`` into win rate, realized R, and P&L.

    This is the only place that answers "is the strategy actually working" —
    cost accounting alone can't; you need to know what happened after entry.
    """
    rows: list[dict[str, Any]] = []
    # Closed trades that could not be graded, kept as a COUNT rather than
    # dropped. Silently skipping them means a day where every exit fill went
    # unresolved reports as "no trades" — and realized_r_today, which gates new
    # entries on ai_daily_loss_limit_r, is running on the same partial set. The
    # scorecard has to be able to say "I cannot grade N of these".
    ungraded: list[str] = []
    unknown_reason = 0
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
        if str(row.get("close_reason") or "") == "unknown":
            unknown_reason += 1
        if row.get("realized_r_multiple") is None:
            ungraded.append(str(row.get("symbol") or "?"))
            continue  # no computable risk basis — nothing to grade
        rows.append(row)

    # Data-quality block, always present so a caller can never read the graded
    # numbers without seeing what they were computed from.
    quality = {
        "ungraded": len(ungraded),
        "ungraded_symbols": ungraded[-8:],
        "unknown_close_reason": unknown_reason,
    }

    if not rows:
        return {"count": 0, **quality}

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
        **quality,
    }


def _print_quality(s: dict[str, Any]) -> None:
    if s.get("ungraded"):
        print(f"⚠  {s['ungraded']} closed trade(s) could NOT be graded — no exit "
              f"fill resolved: {', '.join(s.get('ungraded_symbols') or [])}")
        print("   These are excluded from every number above AND from the "
              "daily-loss gate. Check events.jsonl for exit_fill_unresolved.")
    if s.get("unknown_close_reason"):
        print(f"⚠  {s['unknown_close_reason']} trade(s) closed for an "
              f"unidentified reason (resolve_exit found no matching leg).")


def print_performance_summary(since: float | None = None) -> None:
    s = performance_summary(since=since)
    if s["count"] == 0:
        print("No graded closed positions yet — nothing to summarize.")
        _print_quality(s)
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
    _print_quality(s)


if __name__ == "__main__":
    print_performance_summary()
