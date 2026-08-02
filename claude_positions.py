"""Risk-sized entry decisions and mechanical position management.

``claude_suggest.py`` finds ideas; ``claude_trading.py`` provides the raw
paper-trading primitives. This module is the layer between them: for each
candidate that clears the score filter, Claude runs a per-ticker risk-sized
entry check (exact entry zone / stop / target / time-stop, sized to a fixed
% of account risk) — and from there every mandatory exit rule (hard stop,
scale-out, trailing stop, time stop) is enforced *mechanically*, by real
broker-side orders and local state, not by asking Claude again. Only the
qualitative "did the thesis break" check needs a model, and that rides
inside the existing scheduled research call rather than a separate one —
see ``claude_suggest.py``'s ``position_reviews`` handling.

Why mechanical: a hard stop that "fires immediately, never moves lower" is
only actually true if it is a resting order with the broker. Re-asking an
LLM on some poll cycle makes the stop only as fast as that cycle, and costs
a full research-depth call per position per check.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from claude_suggest import (  # noqa: E402
    DEFAULT_CLAUDE_CLI_BIN,
    DEFAULT_CLAUDE_MODEL,
    _iter_json_blobs,
    call_claude_cli,
)

REPORT_DIR = ROOT / "claude_reports"
POSITIONS_STATE_PATH = REPORT_DIR / "positions_state.json"
OUTCOMES_PATH = REPORT_DIR / "outcomes.jsonl"

# Never more than this much of the account on a single trade's stop distance.
DEFAULT_RISK_PCT = 1.0
DEFAULT_STYLE = "Moderate position"
DEFAULT_MIN_REWARD_RISK = 3.0

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

If decision is "WAIT", set every numeric field to 0 and explain why in "summary".
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
) -> dict[str, Any] | None:
    """Run the per-ticker risk-sized entry check. None on failure or WAIT."""
    prompt = build_entry_prompt(
        ticker, price, account_equity,
        reason=reason, risk_pct=risk_pct, style=style,
    )
    try:
        text = call_claude_cli(
            prompt, model=model, timeout=timeout, live_search=True,
            cli_bin=cli_bin or DEFAULT_CLAUDE_CLI_BIN, phase="entry",
        )
    except Exception:
        return None
    decision = parse_entry_decision(text)
    if not decision or str(decision.get("decision", "")).upper() != "BUY":
        return decision
    return decision


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

def _load_state() -> dict[str, Any]:
    try:
        return json.loads(POSITIONS_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        POSITIONS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        POSITIONS_STATE_PATH.write_text(json.dumps(state, indent=2),
                                        encoding="utf-8")
    except Exception:
        pass


def place_scaled_entry(
    ticker: str,
    decision: dict[str, Any],
    account_equity: float,
    *,
    risk_pct: float = DEFAULT_RISK_PCT,
    current_ask: float | None = None,
) -> dict[str, Any]:
    """Execute a qualifying BUY as two independent broker-side tranches.

    Tranche A (``scale_out_pct``) carries both the stop and the first
    target — it closes itself the moment either level trips, no code
    involved. Tranche B carries the stop only and is meant to ride; once
    tranche A's target fills, ``manage_open_positions`` replaces tranche
    B's stop with breakeven or a trailing stop.
    """
    import alpaca_trader

    # Alpaca rejects bracket orders (and plain market orders) outside regular
    # trading hours — both tranches need one or the other. A pre-market BUY
    # verdict is discarded rather than attempted and failing partway through
    # (which would leave tranche A placed without tranche B, or vice versa).
    if not alpaca_trader.market_is_open():
        return {"ok": False, "error": (
            "market is closed — bracket orders aren't valid outside regular "
            "trading hours; this entry was not queued for the open"
        )}

    ticker = ticker.upper()
    entry_low = float(decision.get("entry_low") or 0)
    entry_high = float(decision.get("entry_high") or entry_low)
    stop_price = float(decision.get("stop_price") or 0)
    target_1 = float(decision.get("target_1") or 0)
    scale_out_pct = max(0.0, min(100.0, float(decision.get("scale_out_pct") or 40)))

    if current_ask is not None and not (entry_low <= current_ask <= max(entry_high, entry_low)):
        return {"ok": False, "error": (
            f"price ${current_ask:.2f} left the entry zone "
            f"${entry_low:.2f}-${entry_high:.2f} before the order could go in"
        )}

    # Size against the price the order will actually fill at, not the zone
    # Claude judged acceptable — current_ask is already validated to fall
    # inside that zone above, so it's the accurate risk basis.
    sizing_entry = current_ask or entry_high or entry_low
    total_qty = alpaca_trader.size_by_risk(
        account_equity, risk_pct, sizing_entry, stop_price)
    if total_qty <= 0:
        return {"ok": False, "error": "risk-sized qty rounded to 0 shares"}

    qty_a = max(1, int(total_qty * scale_out_pct / 100.0))
    qty_b = total_qty - qty_a

    result_a = alpaca_trader.buy_bracket_exact(
        ticker, qty_a, stop_price=stop_price, target_price=target_1)
    result_b = (
        alpaca_trader.buy_bracket_exact(ticker, qty_b, stop_price=stop_price)
        if qty_b > 0 else {"ok": True, "buy_order_id": None, "stop_order_id": None}
    )

    state = _load_state()
    state[ticker] = {
        "qty_a": qty_a,
        "qty_b": qty_b,
        "total_qty": total_qty,
        "entry_price": sizing_entry,
        "tranche_a_order_id": result_a.get("buy_order_id"),
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
        # Set once we've observed the position actually open — guards the
        # closure check below from mistaking "order hasn't filled yet" for
        # "position closed" on the very first tick after entry.
        "entry_confirmed": False,
        "last_seen_price": None,
        "closing_reason": None,
    }
    _save_state(state)

    return {
        "ok": bool(result_a.get("ok")) and bool(result_b.get("ok")),
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
        state[sym]["closing_reason"] = "thesis_break"
        events.append({"ticker": sym, "event": "thesis_break",
                      "reason": r.get("reason"), "ok": bool(out.get("ok"))})
        changed = True
    if changed:
        _save_state(state)
    return events


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
        "exit_price_approx": exit_price,
        "realized_r_multiple": realized_r,
        "realized_pl_usd": realized_pl,
        "close_reason": close_reason,
        "scaled_out": bool(pos.get("tranche_a_filled")),
        "entry_time": entry_time,
        "exit_time": now,
        "hold_days": round((now - entry_time) / 86400.0, 2),
        "reward_risk_planned": pos.get("reward_risk"),
        "summary": pos.get("summary"),
    }
    try:
        OUTCOMES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with OUTCOMES_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(outcome) + "\n")
    except Exception:
        pass
    return outcome


def manage_open_positions(now: float | None = None) -> list[dict[str, Any]]:
    """Desk-tick check, no LLM: closures, tranche-A fills, expired time-stops.

    Cheap enough to run every tick — everything it needs (position detail,
    order status, a breakeven/trailing-stop replacement, a time-boxed close)
    is a plain Alpaca call, not a model call.
    """
    import alpaca_trader

    now = time.time() if now is None else now
    state = _load_state()
    events: list[dict[str, Any]] = []
    changed = False

    # Pass 1: has anything gone fully flat since the last tick? Catches every
    # closing mechanism at once — hard stop, first target then a later
    # trailing/breakeven stop, or an explicit close_out from time-stop or
    # thesis-break above — without needing to track each order leg by hand.
    detail = alpaca_trader.get_positions_detail() or {}
    for ticker, pos in list(state.items()):
        live = detail.get(ticker)
        if live is not None:
            pos["entry_confirmed"] = True
            pos["last_seen_price"] = live.get("current")
            changed = True
            continue
        if not pos.get("entry_confirmed"):
            # Never seen open yet — the entry order likely hasn't filled,
            # not a closure. (If the entry never fills at all, this position
            # stays orphaned in state; not handled here.)
            continue
        exit_price = pos.get("last_seen_price") or pos.get("entry_price")
        reason = pos.get("closing_reason") or _infer_close_reason(pos)
        _record_outcome(ticker, pos, exit_price, reason, now)
        events.append({"ticker": ticker, "event": "closed",
                       "close_reason": reason})
        del state[ticker]
        changed = True

    # Pass 2: only for positions still open — tranche-A fill and time-stop.
    for ticker, pos in list(state.items()):
        # Tranche A fill -> move tranche B's stop to breakeven/trailing.
        if not pos.get("breakeven_done") and pos.get("tranche_a_order_id"):
            order = alpaca_trader.get_order(pos["tranche_a_order_id"])
            if order and order.get("status") == "filled":
                pos["tranche_a_filled"] = True
                if pos.get("qty_b", 0) > 0:
                    trail_pct = pos.get("trail_pct")
                    out = alpaca_trader.replace_stop(
                        ticker, pos.get("tranche_b_stop_order_id"),
                        trail_percent=trail_pct,
                        stop_price=None if trail_pct else pos.get("stop_price"),
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
                alpaca_trader.close_out(ticker)
                pos["closing_reason"] = "time_stop"
                events.append({"ticker": ticker, "event": "time_stop",
                              "age_days": round(age_days, 1)})
                changed = True

    if changed:
        _save_state(state)
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
