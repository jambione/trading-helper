"""H4 swing — liquid names, multi-day hold, no 86s shelf.

Paper arms only when desk_product=h4_swing and ai_h4_paper is true
(docs/PROFIT_REDESIGN.md). This module is the universe filter, the
position stamp, and the flatten exceptions. It does not place orders.
"""
from __future__ import annotations

from typing import Any

DEFAULT_MIN_PRICE = 10.0
DEFAULT_MIN_DOLLAR_VOL = 5_000_000.0
DEFAULT_MAX_SPREAD_PCT = 0.10  # percent of mid; round trip ≈ 20 bps
DEFAULT_MIN_RS = 80.0
DEFAULT_HOLD_DAYS = 2
DEFAULT_STOP_PCT = 2.0
DEFAULT_HAIRCUT_PCT = 0.20

STRATEGY = "h4_swing"


def _f(v: Any, default: float) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def is_h4_pos(pos: dict | None) -> bool:
    if not isinstance(pos, dict):
        return False
    if pos.get("h4_swing") or pos.get("h4"):
        return True
    return str(pos.get("strategy") or "") == STRATEGY


def held_symbols(state: dict | None) -> set[str]:
    """Tickers whose local state is an H4 swing (must survive EOD/SOD)."""
    out: set[str] = set()
    if not isinstance(state, dict):
        return out
    positions = state.get("positions") if isinstance(state.get("positions"), dict) else state
    if not isinstance(positions, dict):
        return out
    for k, v in positions.items():
        if not isinstance(k, str):
            continue
        if k.lower() in {"positions", "updated", "mode", "error", "ok"}:
            continue
        if is_h4_pos(v if isinstance(v, dict) else None):
            out.add(k.upper())
    return out


def partition_state(state: dict | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split managed state into (keep_h4, drop_other).

    SOD used to ``_save_state({})`` after flattening the broker. H4 names
    must remain in local state or the next poll treats them as unmanaged.
    """
    if not isinstance(state, dict):
        return {}, {}
    keep: dict[str, Any] = {}
    drop: dict[str, Any] = {}
    nested = isinstance(state.get("positions"), dict)
    body = state["positions"] if nested else state
    if not isinstance(body, dict):
        return {}, dict(state)
    for k, v in body.items():
        if is_h4_pos(v if isinstance(v, dict) else None):
            keep[k] = v
        else:
            drop[k] = v
    if nested:
        kept_full = dict(state)
        kept_full["positions"] = keep
        return kept_full, drop
    return keep, drop


def universe_row_ok(row: dict | None, cfg: dict | None = None) -> tuple[bool, str]:
    """Whether an RS (or similar) snapshot row clears H4 liquidity gates."""
    if not isinstance(row, dict):
        return False, "no_row"
    cfg = cfg or {}
    min_px = _f(cfg.get("h4_min_price"), DEFAULT_MIN_PRICE)
    min_dv = _f(cfg.get("h4_min_dollar_vol"), DEFAULT_MIN_DOLLAR_VOL)
    min_rs = _f(cfg.get("h4_min_rs"), DEFAULT_MIN_RS)
    px = _f(row.get("price"), 0.0)
    if px < min_px:
        return False, "h4_price"
    dv = row.get("avg_dollar_vol_50d")
    if dv is None:
        dv = row.get("dollar_volume")
    if _f(dv, 0.0) < min_dv:
        return False, "h4_dollar_vol"
    rs = row.get("rs_rating")
    if rs is not None and _f(rs, 0.0) < min_rs:
        return False, "h4_rs"
    return True, "ok"


def spread_ok(bid: float | None, ask: float | None, cfg: dict | None = None) -> tuple[bool, str]:
    """Live quote gate. Missing quote is a skip, not a free pass."""
    cfg = cfg or {}
    cap = _f(cfg.get("h4_max_spread_pct"), DEFAULT_MAX_SPREAD_PCT)
    if cap <= 0:
        return True, "ok"
    try:
        b, a = float(bid or 0), float(ask or 0)
    except (TypeError, ValueError):
        return False, "h4_no_quote"
    if b <= 0 or a <= 0 or a < b:
        return False, "h4_no_quote"
    mid = (a + b) / 2.0
    if mid <= 0:
        return False, "h4_no_quote"
    pct = 100.0 * (a - b) / mid
    if pct > cap:
        return False, "h4_spread"
    return True, "ok"


def stop_price(entry: float, cfg: dict | None = None) -> float:
    pct = _f((cfg or {}).get("h4_stop_pct"), DEFAULT_STOP_PCT)
    if pct <= 0:
        pct = DEFAULT_STOP_PCT
    return round(float(entry) * (1.0 - pct / 100.0), 6)


def stamp_decision(decision: dict[str, Any], cfg: dict | None = None) -> dict[str, Any]:
    d = dict(decision)
    d["h4_swing"] = True
    d["strategy"] = STRATEGY
    d["entry_path"] = STRATEGY
    d["scale_out_pct"] = 0.0
    d["late_hold"] = False
    entry = float(d.get("entry_low") or d.get("entry_high") or 0)
    if entry > 0 and not d.get("stop_price"):
        d["stop_price"] = stop_price(entry, cfg)
    return d


def should_arm(record: dict | None, *, ask: float, bid: float | None,
               cfg: dict | None = None) -> tuple[bool, str]:
    """H4 paper arm: liquidity + spread, never RSI/heat/86s shelf.

    The momentum watch is the wrong universe. Names that lack dollar-volume
    / RS fields fail closed rather than falling through to the scalp.
    """
    cfg = cfg or {}
    if not isinstance(record, dict):
        return False, "not_watching"
    ok_sp, why = spread_ok(bid, ask, cfg)
    if not ok_sp:
        return False, why
    row = {
        "price": ask,
        "avg_dollar_vol_50d": record.get("avg_dollar_vol_50d")
        or record.get("dollar_volume"),
        "rs_rating": record.get("rs_rating"),
    }
    ok_u, why_u = universe_row_ok(row, cfg)
    if not ok_u:
        return False, why_u
    return True, "h4_last"


def load_rs_rows(path) -> list[dict]:
    try:
        import json
        from pathlib import Path
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return []
    rows = raw.get("rows") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def filter_universe(rows: list[dict], cfg: dict | None = None) -> list[dict]:
    out = []
    for r in rows:
        ok, _why = universe_row_ok(r, cfg)
        if ok:
            out.append(r)
    return out
