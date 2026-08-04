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
