"""Adaptive entry limit pricing policy (pure, no broker I/O).

Maps quote quality + heat into a buy limit style:

  passive — rest near the bid (patient, better price if filled)
  fair    — mid
  join    — join/lift the ask slightly (fill-first)

Used by the momentum desk for policy buys and buy_zone auto-limits.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PricingDecision:
    ok: bool
    style: str  # passive | fair | join | ""
    limit_px: float | None
    urgency: float  # 0..1
    reason: str
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    spread_pct: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "style": self.style,
            "limit_px": self.limit_px,
            "urgency": round(self.urgency, 3),
            "reason": self.reason,
            "bid": self.bid,
            "ask": self.ask,
            "mid": self.mid,
            "spread_pct": (
                round(self.spread_pct, 3) if self.spread_pct is not None else None
            ),
        }


def _f(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x <= 0:  # NaN / non-positive
        return None
    return x


def spread_pct(bid: float, ask: float) -> float | None:
    mid = (bid + ask) / 2.0
    if mid <= 0 or ask < bid:
        return None
    return 100.0 * (ask - bid) / mid


def urgency_score(
    *,
    spread_pct_val: float | None,
    rvol: float | None,
    proximity_pct: float | None,
    rvol_hot: float = 3.0,
    max_spread_pct: float = 1.0,
) -> float:
    """0 = patient, 1 = urgent (join the ask)."""
    # Tight spread → can afford to be more aggressive
    if spread_pct_val is None:
        s_comp = 0.4
    elif max_spread_pct <= 0:
        s_comp = 0.5
    else:
        # 0 at max_spread, 1 at 0 width
        s_comp = max(0.0, min(1.0, 1.0 - (spread_pct_val / max_spread_pct)))

    if rvol is None or rvol_hot <= 0:
        r_comp = 0.35
    else:
        r_comp = max(0.0, min(1.0, float(rvol) / float(rvol_hot)))

    if proximity_pct is None:
        p_comp = 0.5
    else:
        p_comp = max(0.0, min(1.0, float(proximity_pct) / 100.0))

    # Weight heat + setup completion slightly more than spread tightness
    u = 0.30 * s_comp + 0.35 * r_comp + 0.35 * p_comp
    return max(0.0, min(1.0, u))


def style_from_urgency(urgency: float) -> str:
    if urgency < 0.38:
        return "passive"
    if urgency < 0.68:
        return "fair"
    return "join"


def limit_for_style(
    style: str,
    bid: float,
    ask: float,
    *,
    pad_pct: float = 0.1,
) -> float:
    mid = (bid + ask) / 2.0
    pad = max(0.0, float(pad_pct))
    if style == "passive":
        # Slightly inside the bid toward mid when spread allows
        tick = max(0.01, round((ask - bid) * 0.15, 2))
        px = min(bid + tick, mid)
        return round(max(bid, min(px, ask)), 2)
    if style == "fair":
        return round(mid, 2)
    # join
    return round(ask * (1.0 + pad / 100.0), 2)


def decide(
    *,
    bid: float | None,
    ask: float | None,
    last: float | None = None,
    rvol: float | None = None,
    proximity_pct: float | None = None,
    max_spread_pct: float = 1.0,
    pad_pct: float = 0.1,
    pad_max_pct: float = 0.15,
    rvol_hot: float = 3.0,
    max_price: float | None = None,
) -> PricingDecision:
    """Compute a buy limit decision from quotes + heat.

    Rejects missing/inverted quotes, wide spreads, and names at/above max_price.
    """
    b = _f(bid)
    a = _f(ask)
    if b is None and a is None:
        # Fall back to last only for a conservative join attempt
        last_px = _f(last)
        if last_px is None:
            return PricingDecision(
                False, "", None, 0.0, "no_quote")
        a = last_px
        b = last_px
    if a is None:
        a = b
    if b is None:
        b = a
    assert b is not None and a is not None

    if a < b:
        return PricingDecision(
            False, "", None, 0.0, "inverted_quote", bid=b, ask=a)

    if max_price is not None and float(max_price) > 0 and a >= float(max_price):
        return PricingDecision(
            False, "", None, 0.0, f"ask_above_max_{max_price:g}",
            bid=b, ask=a, mid=(b + a) / 2.0)

    spr = spread_pct(b, a)
    msp = float(max_spread_pct) if max_spread_pct is not None else 0.0
    if msp > 0 and spr is not None and spr > msp:
        return PricingDecision(
            False, "", None, 0.0, f"spread_pct_{spr:.2f}>{msp:g}",
            bid=b, ask=a, mid=(b + a) / 2.0, spread_pct=spr)

    urg = urgency_score(
        spread_pct_val=spr,
        rvol=_f(rvol),
        proximity_pct=(
            float(proximity_pct)
            if proximity_pct is not None else None
        ),
        rvol_hot=float(rvol_hot),
        max_spread_pct=msp if msp > 0 else 1.0,
    )
    style = style_from_urgency(urg)
    pad = min(max(0.0, float(pad_pct)), max(0.0, float(pad_max_pct)))
    limit = limit_for_style(style, b, a, pad_pct=pad)

    # Hard clamp: never below bid, never more aggressive than join pad_max
    join_cap = round(a * (1.0 + max(0.0, float(pad_max_pct)) / 100.0), 2)
    limit = round(max(b, min(limit, join_cap)), 2)

    mid = (b + a) / 2.0
    reason = f"{style} urg={urg:.2f}"
    if spr is not None:
        reason += f" spr={spr:.2f}%"
    if rvol is not None:
        try:
            reason += f" rvol={float(rvol):.1f}"
        except (TypeError, ValueError):
            pass
    if proximity_pct is not None:
        try:
            reason += f" prox={float(proximity_pct):.0f}"
        except (TypeError, ValueError):
            pass

    return PricingDecision(
        ok=True,
        style=style,
        limit_px=limit,
        urgency=urg,
        reason=reason,
        bid=b,
        ask=a,
        mid=mid,
        spread_pct=spr,
    )
