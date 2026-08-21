"""What the live book is allowed to buy.

See docs/PROFIT_REDESIGN.md. The stack (watch, shadow, research, flatten
of leftover scalps) stays up regardless. This module only answers whether
a *new* auto-arm may place.

Partial cfg dicts in unit tests often omit ``desk_product``. Missing is
``scalp_legacy`` so existing arm-geometry tests keep their old path.
``DEFAULT_CONFIG`` and live ``bot_config.json`` set ``observe``.
"""
from __future__ import annotations

from typing import Any

OBSERVE = "observe"
SCALP_LEGACY = "scalp_legacy"
H4_SWING = "h4_swing"
H3_OVERNIGHT = "h3_overnight"

PRODUCTS = frozenset({OBSERVE, SCALP_LEGACY, H4_SWING, H3_OVERNIGHT})

# Reasons should_arm_buy / place_scaled_entry return when the product vetoes.
REASON_OBSERVE = "desk_observe"
REASON_H4_OFF = "h4_not_armed"
REASON_H3_OFF = "h3_not_armed"
REASON_UNKNOWN = "desk_observe"


def product(cfg: dict | None) -> str:
    raw = str((cfg or {}).get("desk_product") or "").strip().lower()
    if not raw:
        return SCALP_LEGACY
    if raw not in PRODUCTS:
        return OBSERVE
    return raw


def h4_paper(cfg: dict | None) -> bool:
    return bool((cfg or {}).get("ai_h4_paper"))


def h3_paper(cfg: dict | None) -> bool:
    return bool((cfg or {}).get("ai_h3_paper"))


def arm_block_reason(cfg: dict | None) -> str | None:
    """None = this product may still run its own gates. Else a refusal code."""
    p = product(cfg)
    if p == OBSERVE:
        return REASON_OBSERVE
    if p == SCALP_LEGACY:
        return None
    if p == H4_SWING:
        return None if h4_paper(cfg) else REASON_H4_OFF
    if p == H3_OVERNIGHT:
        return None if h3_paper(cfg) else REASON_H3_OFF
    return REASON_UNKNOWN


def allows_new_entries(cfg: dict | None) -> bool:
    return arm_block_reason(cfg) is None
