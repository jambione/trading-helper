"""Today's position history for the book strip.

Alpaca only reports what is still open, so a name that was bought and sold
this morning disappears from the account the moment it flattens. The strip
rebuilds the day from filled orders (the account connection) and attaches
the desk's entry and exit reasons from the outcome record.

The list is the ET calendar day of the fill. Yesterday's closed trades drop
off at midnight. A position that is still open stays, because it is still
on the account.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Submit time and fill time are not the same stamp. A market buy is seconds
# apart; a limit that rests is still well inside this window.
_ENTRY_MATCH_SEC = 900.0
_EXIT_MATCH_SEC = 600.0
_QTY_EPS = 1e-4
_FILL_CACHE_SEC = 30.0
_MAX_ROWS = 40

# Words the operator already uses. Anything else is shown with the
# underscores turned into spaces so a new reason is still readable.
_LABELS = {
    "oversold_leave": "leave oversold",
    "last_oversold": "leave oversold",
    "presquare": "empty square",
    "empty_square": "empty square",
    "pre_square": "empty square",
    "overbought": "full square",
    "overbought_hot": "full square",
    "mid_rise": "mid rise",
    "heating": "heating",
    "local_trail": "local trail",
    "left_overbought": "left overbought",
    "dead_trade": "dead trade",
    "rsi_dump": "rsi dump",
    "sod_wipe": "start of day",
    "eod_liquidate": "end of day",
    "eod": "end of day",
    "eod_flatten": "end of day",
    "flat_deadline_miss": "flat deadline",
    "stale_data": "stale quote",
    "target_hit": "target",
    "stopped_out": "stop",
    "trailed_out": "trail",
    "flattened": "flatten",
    "time_stop": "time stop",
    "thesis_break": "thesis break",
    "no_progress": "no progress",
    "exh_falling_flatten": "exhaustion falling",
    "fill_through_stop": "through stop",
    "stop_software_full": "stop",
    "unprotected_flatten": "unprotected",
    "unprotected_local": "unprotected",
    "late_hold_stop": "late hold stop",
    "macd_negative": "macd negative",
    "scale_out": "scale out",
}

_fill_cache: dict[str, Any] = {"at": 0.0, "day": "", "rows": []}
_outcome_cache: dict[str, Any] = {"key": None, "rows": []}
_last_err = ""


def clear_caches() -> None:
    """Drop the paced broker-fill cache. Tests call this between cases."""
    _fill_cache["at"] = 0.0
    _fill_cache["day"] = ""
    _fill_cache["rows"] = []
    _outcome_cache["key"] = None
    _outcome_cache["rows"] = []


def et_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=ET).strftime("%Y-%m-%d")


def reason_label(raw: Any) -> str:
    s = str(raw or "").strip().lower().replace("-", "_")
    if not s or s in ("unknown", "none", "ok"):
        return ""
    if s in _LABELS:
        return _LABELS[s]
    if s.startswith("last_"):
        rest = s[5:]
        if rest in _LABELS:
            return _LABELS[rest]
        s = rest
    return s.replace("_", " ")


def for_book(
    now: float | None = None,
    *,
    open_positions: dict | None = None,
    managed: dict | None = None,
    fills: list[dict] | None = None,
    outcomes: list[dict] | None = None,
) -> dict[str, Any]:
    """Payload for the book strip. Never raises into the publisher."""
    global _last_err
    now_f = time.time() if now is None else float(now)
    try:
        if fills is None:
            fills = load_today_fills(now_f)
        if outcomes is None:
            outcomes = load_outcomes()
        out = build_day_history(
            fills,
            outcomes=outcomes,
            open_positions=open_positions,
            managed=managed,
            now=now_f,
        )
        _last_err = ""
        return out
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        if msg != _last_err:
            _last_err = msg
            print(f"[ai] day history failed: {msg}", flush=True)
        return {"day": et_day(now_f), "rows": []}


def load_today_fills(now: float) -> list[dict]:
    """Filled orders for the ET day of *now*.

    The broker call is paced. The local fill ledger is unioned in so a fill
    the desk already recorded is not invisible for the whole pace window,
    and so the strip still has prices when the account call fails.
    """
    day = et_day(now)
    if (
        _fill_cache["day"] == day
        and _fill_cache["rows"] is not None
        and (float(now) - float(_fill_cache["at"])) < _FILL_CACHE_SEC
        and _fill_cache["at"]
    ):
        return list(_fill_cache["rows"])

    raw: list[dict] = []
    try:
        import alpaca_trader
        if alpaca_trader.is_active():
            got = alpaca_trader.get_filled_orders(limit=500, days=2) or []
            if isinstance(got, list):
                raw.extend(got)
    except Exception:
        pass
    try:
        import fill_ledger
        for ev in fill_ledger.read_day(day) or []:
            if isinstance(ev, dict):
                raw.append(ev)
    except Exception:
        pass

    kept = [
        row for row in _dedupe(_normalize_all(raw))
        if et_day(row["time"]) == day
    ]
    _fill_cache["at"] = float(now)
    _fill_cache["day"] = day
    _fill_cache["rows"] = kept
    return list(kept)


def load_outcomes() -> list[dict]:
    """Outcome rows, cached until the file changes.

    A close is written here before the next paced broker pull, so the strip
    can show the exit without waiting on Alpaca.
    """
    path = _outcomes_path()
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        _outcome_cache["key"] = None
        _outcome_cache["rows"] = []
        return []
    if _outcome_cache["key"] == key:
        return list(_outcome_cache["rows"])
    rows: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        rows = []
    _outcome_cache["key"] = key
    _outcome_cache["rows"] = rows
    return list(rows)


def build_day_history(
    fills: list[dict] | None,
    *,
    outcomes: list[dict] | None = None,
    open_positions: dict | None = None,
    managed: dict | None = None,
    now: float | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    """Pair today's buys and sells into positions.

    One position is the shares opened by one or more buys and closed by
    each sell that followed, in order. A second buy after the first lot is
    flat is a new position, not a scale-in.
    """
    now_f = time.time() if now is None else float(now)
    day_s = day or et_day(now_f)
    opens = open_positions if isinstance(open_positions, dict) else {}
    book = managed if isinstance(managed, dict) else {}

    norm = [
        row for row in _dedupe(_normalize_all(fills or []))
        if et_day(row["time"]) == day_s
    ]
    norm.sort(key=lambda r: (r["time"], 0 if r["side"] == "buy" else 1, r["order_id"]))

    lots: list[dict] = []
    open_lot: dict[str, dict] = {}
    parked: list[dict] = []
    for fill in norm:
        sym = fill["symbol"]
        if fill["side"] == "buy":
            lot = open_lot.get(sym)
            if lot is None:
                lot = _new_lot(fill)
                open_lot[sym] = lot
                lots.append(lot)
            else:
                _add_buy(lot, fill)
            continue
        lot = open_lot.get(sym)
        if lot is None:
            parked.append(fill)
            continue
        leftover = _apply_sell(lot, fill)
        if lot["qty_left"] <= _QTY_EPS:
            open_lot.pop(sym, None)
        if leftover > _QTY_EPS:
            extra = dict(fill)
            extra["qty"] = leftover
            parked.append(extra)

    used: set[int] = set()
    today_outcomes = [
        o for o in (outcomes or [])
        if isinstance(o, dict) and _outcome_touches(o, day_s)
    ]
    for lot in lots:
        match = _claim_outcome(lot, today_outcomes, used)
        if match is None:
            continue
        used.add(id(match))
        _apply_outcome(lot, match)

    for outcome in today_outcomes:
        if id(outcome) in used:
            continue
        row = _lot_from_outcome(outcome, day_s)
        if row is not None:
            lots.append(row)
            used.add(id(outcome))

    for fill in parked:
        if _sell_already_shown(lots, fill):
            continue
        lots.append(_lot_from_sell(fill))

    for sym_raw, pos in opens.items():
        if not isinstance(pos, dict):
            continue
        sym = str(sym_raw or "").upper().strip()
        if not sym or _num(pos.get("qty")) is None or float(pos.get("qty") or 0) <= _QTY_EPS:
            continue
        if any(lot["symbol"] == sym and lot["qty_left"] > _QTY_EPS for lot in lots):
            continue
        lots.append(_lot_from_open(sym, pos, book.get(sym)))

    for lot in lots:
        if lot["qty_left"] <= _QTY_EPS or lot.get("entry_reason"):
            continue
        held = book.get(lot["symbol"])
        if isinstance(held, dict):
            lot["entry_reason"] = reason_label(_arm_why(held))

    for lot in lots:
        _finalize_pl(lot, opens.get(lot["symbol"]) if lot["qty_left"] > _QTY_EPS else None)

    lots.sort(key=lambda lot: _activity(lot), reverse=True)
    rows = [_public_row(lot) for lot in lots[:_MAX_ROWS]]
    return {"day": day_s, "rows": rows}


# ── fills ────────────────────────────────────────────────────────────────────


def _outcomes_path() -> Path:
    try:
        import ai_positions as ap
        return Path(ap.OUTCOMES_PATH)
    except Exception:
        return Path("ai_reports") / "outcomes.jsonl"


def _normalize_all(raw_rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        row = _normalize_fill(raw)
        if row is not None:
            out.append(row)
    return out


def _normalize_fill(raw: dict) -> dict | None:
    event = str(raw.get("event") or "").lower()
    if event and event not in ("fill",):
        return None
    side = str(raw.get("side") or "").split(".")[-1].lower()
    if side not in ("buy", "sell"):
        action = str(raw.get("action") or "").upper()
        if action in ("BUY", "SELL"):
            side = action.lower()
        else:
            return None
    qty = _num(raw.get("filled_qty"))
    if qty is None or qty <= _QTY_EPS:
        qty = _num(raw.get("qty"))
    price = _num(raw.get("filled_avg_price"))
    if price is None or price <= 0:
        price = _num(raw.get("price"))
    ts = (
        _parse_time(raw.get("filled_at"))
        or _parse_time(raw.get("time"))
        or _parse_time(raw.get("ts"))
    )
    sym = str(raw.get("symbol") or "").upper().strip()
    if not sym or qty is None or qty <= _QTY_EPS or price is None or price <= 0 or ts is None:
        return None
    return {
        "order_id": str(raw.get("order_id") or raw.get("id") or "").strip(),
        "symbol": sym,
        "side": side,
        "qty": float(qty),
        "price": float(price),
        "time": float(ts),
        "type": str(raw.get("type") or "").split(".")[-1].lower(),
    }


def _dedupe(rows: list[dict]) -> list[dict]:
    """One row per order. A partial that later completes keeps the larger qty."""
    by_id: dict[str, dict] = {}
    loose: list[dict] = []
    for row in rows:
        oid = row.get("order_id") or ""
        if not oid:
            loose.append(row)
            continue
        prev = by_id.get(oid)
        if prev is None or float(row["qty"]) > float(prev["qty"]) + _QTY_EPS:
            by_id[oid] = row
    out = list(by_id.values())
    for row in loose:
        if any(
            other["symbol"] == row["symbol"]
            and other["side"] == row["side"]
            and abs(other["time"] - row["time"]) < 1.0
            and _near_px(other["price"], row["price"])
            for other in out
        ):
            continue
        out.append(row)
    return out


def _new_lot(fill: dict) -> dict:
    return {
        "symbol": fill["symbol"],
        "qty": float(fill["qty"]),
        "qty_left": float(fill["qty"]),
        "entry_price": float(fill["price"]),
        "entry_time": float(fill["time"]),
        "entry_reason": "",
        "exits": [],
        "pl": None,
        "from_outcome": False,
        "outcome_pl": None,
    }


def _add_buy(lot: dict, fill: dict) -> None:
    q0 = float(lot["qty"])
    q1 = float(fill["qty"])
    lot["entry_price"] = (float(lot["entry_price"]) * q0 + float(fill["price"]) * q1) / (q0 + q1)
    lot["qty"] = q0 + q1
    lot["qty_left"] = float(lot["qty_left"]) + q1


def _apply_sell(lot: dict, fill: dict) -> float:
    """Apply a sell to an open lot. Returns shares that did not fit."""
    room = float(lot["qty_left"])
    take = min(float(fill["qty"]), room)
    if take <= _QTY_EPS:
        return float(fill["qty"])
    partial = take + _QTY_EPS < room
    lot["exits"].append({
        "price": float(fill["price"]),
        "time": float(fill["time"]),
        "qty": take,
        "reason": _fill_reason(fill, partial=partial),
    })
    lot["qty_left"] = room - take
    return float(fill["qty"]) - take


def _fill_reason(fill: dict, *, partial: bool) -> str:
    kind = str(fill.get("type") or "")
    if "stop" in kind:
        return "stop"
    if "limit" in kind and partial:
        return "scale out"
    if "limit" in kind:
        return "limit"
    if "market" in kind:
        return "market"
    return ""


# ── outcomes and opens ───────────────────────────────────────────────────────


def _outcome_touches(outcome: dict, day: str) -> bool:
    for key in ("entry_time", "exit_time", "ts"):
        ts = _parse_time(outcome.get(key))
        if ts is not None and et_day(ts) == day:
            return True
    return False


def _claim_outcome(lot: dict, outcomes: list[dict], used: set[int]) -> dict | None:
    best: dict | None = None
    best_score: float | None = None
    for outcome in outcomes:
        if id(outcome) in used:
            continue
        score = _outcome_score(lot, outcome)
        if score is None:
            continue
        if best_score is None or score < best_score:
            best = outcome
            best_score = score
    return best


def _outcome_score(lot: dict, outcome: dict) -> float | None:
    if str(outcome.get("symbol") or "").upper() != lot["symbol"]:
        return None
    ot = _parse_time(outcome.get("entry_time"))
    op = _num(outcome.get("entry_price"))
    lt = _num(lot.get("entry_time"))
    lp = _num(lot.get("entry_price"))
    time_ok = ot is not None and lt is not None and abs(ot - lt) <= _ENTRY_MATCH_SEC
    px_ok = _near_px(op, lp)
    if not time_ok and not px_ok:
        return None
    if lot["exits"] and not time_ok and not any(_exit_matches(ex, outcome) for ex in lot["exits"]):
        return None
    if ot is not None and lt is not None:
        return abs(ot - lt)
    return 0.0


def _apply_outcome(lot: dict, outcome: dict) -> None:
    why = reason_label(outcome.get("arm_why"))
    if why:
        lot["entry_reason"] = why
    _assign_exit_reason(lot, outcome)
    _maybe_add_outcome_exit(lot, outcome)
    if lot.get("outcome_pl") is None:
        lot["outcome_pl"] = _num(outcome.get("realized_pl_usd"))


def _assign_exit_reason(lot: dict, outcome: dict) -> None:
    label = reason_label(outcome.get("close_reason") or outcome.get("closing_reason"))
    if not label or not lot["exits"]:
        return
    best_i: int | None = None
    best: tuple[float, float] | None = None
    xt = _parse_time(outcome.get("exit_time")) or _parse_time(outcome.get("ts"))
    xp = _num(outcome.get("exit_price"))
    for i, ex in enumerate(lot["exits"]):
        if not _exit_matches(ex, outcome):
            continue
        td = abs(float(ex["time"]) - xt) if xt is not None else 1e12
        pd = abs(float(ex["price"]) - xp) if xp is not None else 1e12
        score = (td, pd)
        if best is None or score < best:
            best = score
            best_i = i
    if best_i is not None:
        lot["exits"][best_i]["reason"] = label


def _maybe_add_outcome_exit(lot: dict, outcome: dict) -> None:
    """Desk already closed the lot but the sell fill is not in the account pull yet."""
    if float(lot["qty_left"]) <= _QTY_EPS:
        return
    if any(_exit_matches(ex, outcome) for ex in lot["exits"]):
        return
    price = _num(outcome.get("exit_price"))
    ts = _parse_time(outcome.get("exit_time")) or _parse_time(outcome.get("ts"))
    entry_t = _num(lot.get("entry_time"))
    if price is None or price <= 0 or ts is None:
        return
    if entry_t is not None and ts + 5 < entry_t:
        return
    lot["exits"].append({
        "price": float(price),
        "time": float(ts),
        "qty": float(lot["qty_left"]),
        "reason": reason_label(outcome.get("close_reason") or outcome.get("closing_reason")),
    })
    lot["qty_left"] = 0.0


def _exit_matches(ex: dict, outcome: dict) -> bool:
    xt = _parse_time(outcome.get("exit_time")) or _parse_time(outcome.get("ts"))
    xp = _num(outcome.get("exit_price"))
    if xt is not None and abs(float(ex["time"]) - xt) <= _EXIT_MATCH_SEC:
        return True
    return _near_px(ex.get("price"), xp)


def _lot_from_outcome(outcome: dict, day: str) -> dict | None:
    xt = _parse_time(outcome.get("exit_time")) or _parse_time(outcome.get("ts"))
    xp = _num(outcome.get("exit_price"))
    et = _parse_time(outcome.get("entry_time"))
    ep = _num(outcome.get("entry_price"))
    qty = _num(outcome.get("total_qty")) or 0.0
    exits: list[dict] = []
    if xp is not None and xp > 0 and xt is not None and et_day(xt) == day:
        exits.append({
            "price": float(xp),
            "time": float(xt),
            "qty": float(qty) if qty > _QTY_EPS else None,
            "reason": reason_label(outcome.get("close_reason") or outcome.get("closing_reason")),
        })
    if not exits and not (et is not None and et_day(et) == day):
        return None
    sym = str(outcome.get("symbol") or "").upper().strip()
    if not sym:
        return None
    return {
        "symbol": sym,
        "qty": float(qty) if qty > _QTY_EPS else 0.0,
        "qty_left": 0.0 if exits else float(qty or 0.0),
        "entry_price": float(ep) if ep else None,
        "entry_time": float(et) if et else None,
        "entry_reason": reason_label(outcome.get("arm_why")),
        "exits": exits,
        "pl": None,
        "from_outcome": True,
        "outcome_pl": _num(outcome.get("realized_pl_usd")),
    }


def _lot_from_sell(fill: dict) -> dict:
    return {
        "symbol": fill["symbol"],
        "qty": float(fill["qty"]),
        "qty_left": 0.0,
        "entry_price": None,
        "entry_time": None,
        "entry_reason": "",
        "exits": [{
            "price": float(fill["price"]),
            "time": float(fill["time"]),
            "qty": float(fill["qty"]),
            "reason": _fill_reason(fill, partial=False),
        }],
        "pl": None,
        "from_outcome": False,
        "outcome_pl": None,
    }


def _lot_from_open(sym: str, pos: dict, managed: Any) -> dict:
    held = managed if isinstance(managed, dict) else {}
    qty = float(pos.get("qty") or 0)
    entry = _num(pos.get("avg_entry"))
    if entry is None:
        entry = _num(held.get("entry_price"))
    return {
        "symbol": sym,
        "qty": qty,
        "qty_left": qty,
        "entry_price": float(entry) if entry else None,
        "entry_time": _parse_time(held.get("entry_time")),
        "entry_reason": reason_label(_arm_why(held)),
        "exits": [],
        "pl": _num(pos.get("pl")),
        "from_outcome": False,
        "outcome_pl": None,
    }


def _sell_already_shown(lots: list[dict], fill: dict) -> bool:
    """A parked sell is the same exit an outcome row already drew.

    The outcome stamp and the broker fill are seconds or minutes apart, so a
    two-second compare printed the close twice.
    """
    for lot in lots:
        if lot["symbol"] != fill["symbol"]:
            continue
        for ex in lot["exits"]:
            if (
                _near_px(ex.get("price"), fill.get("price"))
                and abs(float(ex["time"]) - float(fill["time"])) <= _EXIT_MATCH_SEC
            ):
                return True
    return False


def _arm_why(pos: dict) -> str:
    why = pos.get("arm_why")
    if why:
        return str(why)
    feat = pos.get("features") if isinstance(pos.get("features"), dict) else {}
    why = feat.get("arm_why") if isinstance(feat, dict) else None
    if why:
        return str(why)
    state = str(
        pos.get("entry_exhaustion_state")
        or (feat.get("entry_exhaustion_state") if isinstance(feat, dict) else "")
        or ""
    ).strip().lower()
    if state in ("overbought", "overbought_hot"):
        return "overbought"
    return state


def _finalize_pl(lot: dict, open_pos: Any) -> None:
    """Dollar P&L.

    Fill prices win when we have them. The outcome's realized_pl prices the
    whole quantity at the final exit, which is wrong once a scale-out sold
    some shares at a different price. Broker unrealized P&L is only the
    shares still open.
    """
    if lot.get("from_outcome") and lot.get("outcome_pl") is not None and float(lot["qty_left"]) <= _QTY_EPS:
        lot["pl"] = round(float(lot["outcome_pl"]), 2)
        return
    entry = _num(lot.get("entry_price"))
    realized = 0.0
    have = False
    if entry is not None:
        for ex in lot["exits"]:
            px = _num(ex.get("price"))
            q = _num(ex.get("qty"))
            if px is None or q is None:
                continue
            realized += (px - entry) * q
            have = True
    broker = _num(open_pos.get("pl")) if isinstance(open_pos, dict) else None
    if float(lot["qty_left"]) > _QTY_EPS and broker is not None:
        lot["pl"] = round((realized if have else 0.0) + broker, 2)
        return
    if have:
        lot["pl"] = round(realized, 2)
        return
    if lot.get("pl") is not None:
        lot["pl"] = round(float(lot["pl"]), 2)


def _activity(lot: dict) -> float:
    times = [float(lot["entry_time"] or 0)]
    times.extend(float(ex["time"]) for ex in lot["exits"] if ex.get("time"))
    return max(times) if times else 0.0


def _public_row(lot: dict) -> dict[str, Any]:
    status = "open" if float(lot["qty_left"]) > _QTY_EPS else "closed"
    exits = []
    for ex in lot["exits"]:
        item: dict[str, Any] = {
            "price": _round_px(ex.get("price")),
            "time": ex.get("time"),
            "reason": ex.get("reason") or "",
        }
        q = _qty_out(ex.get("qty"))
        if q is not None:
            item["qty"] = q
        exits.append(item)
    return {
        "symbol": lot["symbol"],
        "status": status,
        "qty": _qty_out(lot.get("qty")),
        "entry_price": _round_px(lot.get("entry_price")),
        "entry_time": lot.get("entry_time"),
        "entry_reason": lot.get("entry_reason") or "",
        "exits": exits,
        "pl": None if lot.get("pl") is None else round(float(lot["pl"]), 2),
    }


# ── small helpers ────────────────────────────────────────────────────────────


def _num(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n:  # NaN
        return None
    return n


def _parse_time(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        n = float(raw)
        if n > 1e12:
            n = n / 1000.0
        return n if n > 0 else None
    text = str(raw).strip()
    if not text or text.lower() in ("none", "null"):
        return None
    try:
        n = float(text)
    except ValueError:
        n = None
    else:
        if n > 1e12:
            n = n / 1000.0
        return n if n > 0 else None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _near_px(a: Any, b: Any) -> bool:
    fa, fb = _num(a), _num(b)
    if fa is None or fb is None:
        return False
    return abs(fa - fb) <= max(0.02, abs(fb) * 0.002)


def _round_px(v: Any) -> float | None:
    n = _num(v)
    if n is None or n <= 0:
        return None
    return round(n, 4)


def _qty_out(v: Any) -> int | float | None:
    n = _num(v)
    if n is None or n <= _QTY_EPS:
        return None
    if abs(n - round(n)) < 1e-3:
        return int(round(n))
    return round(n, 4)
