"""Risk-sized long plans: tight stop, small size, TP at reward_R.

Pure helpers (no broker I/O). Used by monitor desk bracket path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class LongPlan:
    entry: float
    stop: float
    target: float
    qty: int
    risk_dollars: float
    r_per_share: float
    risk_pct: float
    stop_pct: float
    reward_r: float
    equity: float
    notional: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def plan_long(
    entry: float,
    *,
    equity: float,
    risk_pct: float = 0.35,
    stop_pct: float = 0.40,
    reward_r: float = 2.0,
    max_notional: float | None = None,
) -> LongPlan | None:
    """Build a long plan: stop under entry, size by equity risk, TP at reward_r * R.

    Returns None if prices/sizing are invalid (no trade).
    """
    try:
        entry = float(entry)
        equity = float(equity)
        risk_pct = float(risk_pct)
        stop_pct = float(stop_pct)
        reward_r = float(reward_r)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or equity <= 0 or stop_pct <= 0 or risk_pct <= 0 or reward_r <= 0:
        return None

    stop = round(entry * (1.0 - stop_pct / 100.0), 2)
    if stop <= 0 or stop >= entry:
        return None
    r_ps = entry - stop
    if r_ps <= 0:
        return None
    target = round(entry + reward_r * r_ps, 2)
    risk_dollars = equity * (risk_pct / 100.0)
    qty = int(risk_dollars // r_ps)
    if max_notional is not None and float(max_notional) > 0:
        cap = int(float(max_notional) // entry)
        qty = min(qty, cap)
    if qty < 1:
        return None
    notional = round(qty * entry, 2)
    return LongPlan(
        entry=round(entry, 2),
        stop=stop,
        target=target,
        qty=qty,
        risk_dollars=round(risk_dollars, 4),
        r_per_share=round(r_ps, 4),
        risk_pct=risk_pct,
        stop_pct=stop_pct,
        reward_r=reward_r,
        equity=equity,
        notional=notional,
    )


def unrealized_r(entry: float, r_per_share: float, current: float) -> float | None:
    if r_per_share is None or float(r_per_share) <= 0:
        return None
    try:
        return (float(current) - float(entry)) / float(r_per_share)
    except (TypeError, ValueError):
        return None


def stop_for_phase(
    phase: str,
    *,
    entry: float,
    initial_stop: float,
    r_per_share: float,
) -> float:
    """Next stop price for ratchet phase (raise-only semantics applied by caller)."""
    phase = (phase or "initial").lower()
    if phase == "be":
        return round(float(entry), 2)
    if phase in ("locked", "lock"):
        return round(float(entry) + float(r_per_share), 2)
    return round(float(initial_stop), 2)


def next_phase(unreal_r: float, *, be_at_r: float = 1.0, lock_at_r: float = 2.0) -> str:
    """Phase label from unrealized R."""
    if unreal_r >= float(lock_at_r):
        return "locked"
    if unreal_r >= float(be_at_r):
        return "be"
    return "initial"


def trade_r(realized_pnl: float, risk_dollars: float) -> float | None:
    if risk_dollars is None or float(risk_dollars) <= 0:
        return None
    try:
        return float(realized_pnl) / float(risk_dollars)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class EquityBookLimits:
    """Live-equity concentration and slot count.

    Config ``ai_max_positions`` / ``ai_max_position_pct`` are the *large-account*
    ceilings. Below ``slot_equity × max_positions`` the book loosens so a $250
    account can still buy a whole share; dollar risk still grows with equity
    via ``ai_risk_pct``.
    """

    equity: float
    max_positions: int
    max_position_pct: float
    max_position_pct_cheap: float
    slot_equity: float
    dollar_cap: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(val: Any, default: float) -> float:
    try:
        out = float(val)
    except (TypeError, ValueError):
        return float(default)
    if out != out:  # NaN
        return float(default)
    return out


def _i(val: Any, default: int) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return int(default)


def equity_book_limits(
    equity: float,
    *,
    max_positions: int = 8,
    max_position_pct: float = 8.0,
    max_position_pct_cheap: float = 5.0,
    slot_equity: float = 250.0,
) -> EquityBookLimits:
    """Scale slots and notional cap from live equity.

    * ``max_positions`` = one slot per ``slot_equity``, at least 1, at most the
      configured ceiling. $500 → 1, $1,000 → 2.
    * Dollar cap = ``max(equity × configured %, one slot)``, never more than
      equity. So $250 can use the whole book; at $10k the 8% ceiling binds and
      dollar size keeps growing with equity.

    ``slot_equity <= 0`` disables scaling and returns the configured values.
    """
    eq = max(0.0, _f(equity, 0.0))
    ceiling_pos = max(1, _i(max_positions, 8))
    cfg_pct = max(0.0, _f(max_position_pct, 8.0))
    cfg_cheap = max(0.0, _f(max_position_pct_cheap, 5.0))
    slot = _f(slot_equity, 250.0)

    if slot <= 0 or eq <= 0:
        dollar = eq * cfg_pct / 100.0 if eq > 0 and cfg_pct > 0 else 0.0
        return EquityBookLimits(
            equity=eq,
            max_positions=ceiling_pos,
            max_position_pct=cfg_pct,
            max_position_pct_cheap=cfg_cheap,
            slot_equity=max(0.0, slot),
            dollar_cap=round(dollar, 4),
        )

    slots = int(eq / slot)
    slots = max(1, min(ceiling_pos, slots if slots > 0 else 1))

    configured_dollars = eq * cfg_pct / 100.0
    dollar_cap = min(eq, max(configured_dollars, slot))
    pct = 100.0 * dollar_cap / eq

    return EquityBookLimits(
        equity=eq,
        max_positions=slots,
        max_position_pct=round(pct, 6),
        max_position_pct_cheap=cfg_cheap,
        slot_equity=slot,
        dollar_cap=round(dollar_cap, 4),
    )


def limits_from_cfg(equity: float, cfg: dict[str, Any] | None = None) -> EquityBookLimits:
    """``equity_book_limits`` using desk config keys (defaults match bot_config)."""
    src = cfg if isinstance(cfg, dict) else {}
    return equity_book_limits(
        equity,
        max_positions=_i(src.get("ai_max_positions"), 8),
        max_position_pct=_f(src.get("ai_max_position_pct"), 8.0),
        max_position_pct_cheap=_f(src.get("ai_max_position_pct_cheap"), 5.0),
        slot_equity=_f(src.get("ai_position_slot_equity"), 250.0),
    )


def cap_long_qty(
    qty: int,
    *,
    equity: float,
    price: float,
    max_position_pct: float,
) -> int:
    """Apply the notional cap; allow 1 share when risk-sizing rounded to 0.

    Dollar size still comes from risk-sizing (grows with equity). This only
    stops a tight stop from buying more than ``max_position_pct`` of the book,
    and keeps a $250 account from dying on ``qty rounded to 0``.
    """
    try:
        q = int(qty)
    except (TypeError, ValueError):
        q = 0
    if q < 0:
        q = 0
    eq = _f(equity, 0.0)
    px = _f(price, 0.0)
    pct = _f(max_position_pct, 0.0)
    if eq <= 0 or px <= 0 or pct <= 0:
        return max(0, q)
    cap_qty = int((eq * pct / 100.0) // px)
    if q == 0 and cap_qty >= 1:
        return 1
    if cap_qty < q:
        return max(0, cap_qty)
    return q
