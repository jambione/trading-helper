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


# Distance the runner gives back from the high water mark, in R — NOT percent.
# A fixed percent trail is a different trade on every name: at 2.5% it is 2.5R
# behind a 1%-wide stop and 0.5R behind a 5%-wide one. Denominated in R it is
# the same trade everywhere. ai_positions uses the same rule for tranche B.
DEFAULT_TRAIL_R = 1.0
# Only rewrite a resting stop when the trail gains at least this much R. The
# monitor's manage tick runs every few seconds and each move is a cancel plus a
# submit, so an ungated trail is a cancel/submit storm for pennies of give.
DEFAULT_TRAIL_STEP_R = 0.10


def trail_stop_level(
    *,
    entry: float,
    peak: float,
    r_per_share: float,
    trail_r: float = DEFAULT_TRAIL_R,
) -> float | None:
    """Where a trailing stop belongs right now: ``max(entry, peak - trail_r*R)``.

    This is the phase model's missing terminal state. ``stop_for_phase("locked")``
    returns ``entry + 1R`` and the caller then marks the position locked, which
    read as "profit captured" but is a stop that never moves again — a name that
    ran to +8R still exited at +1R. Measuring from *peak* instead of from entry
    is what makes the stop follow price up.

    The breakeven floor matters on the way in: until price has run far enough
    that ``peak - trail_r*R`` clears entry, the trail would otherwise sit below
    the entry price and hand back a winner as a loss.

    Returns None when there is no basis to compute one (no entry, no risk).
    """
    try:
        e = float(entry)
        pk = float(peak)
        risk = float(r_per_share)
        give = max(0.0, float(trail_r))
    except (TypeError, ValueError):
        return None
    if e <= 0 or risk <= 0:
        return None
    # The entry floor also covers a peak below entry: peak - give*R is then
    # under entry too, so the max() returns entry either way.
    return round(max(e, pk - give * risk), 2)


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


@dataclass(frozen=True)
class FreeEquitySize:
    """Result of sizing one long off leftover (unoccupied) equity.

    ``qty`` is whole shares. The other fields are for logs / tests.
    ``capped_by`` is a comma-joined list of clamps that reduced qty
    (empty when the raw max(slot, risk) ticket stood).
    """

    qty: int
    risk_qty: int
    notional_qty: int
    free_equity: float
    open_notional: float
    open_count: int
    remaining_slots: int
    target_notional: float
    capped_by: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def position_notional(row: Any) -> float:
    """Live market value if present, else current×qty, else entry×qty."""
    if not isinstance(row, dict):
        return 0.0
    for key in ("mkt_val", "market_value"):
        if key in row and row.get(key) is not None:
            mv = _f(row.get(key), 0.0)
            if mv != 0.0:
                return abs(mv)
    qty = abs(_f(row.get("qty"), 0.0))
    if qty <= 0:
        qty = abs(_f(row.get("total_qty"), 0.0))
    if qty <= 0:
        return 0.0
    for key in ("current", "current_price"):
        px = _f(row.get(key), 0.0)
        if px > 0:
            return qty * px
    for key in ("avg_entry", "avg_entry_price", "entry_price"):
        px = _f(row.get(key), 0.0)
        if px > 0:
            return qty * px
    return 0.0


def open_book_notional(
    positions: Any,
    *,
    skip_symbol: str | None = None,
) -> tuple[float, int]:
    """(open_notional, open_count) from broker or managed-state rows.

    Skips zero-qty rows, the optional ``skip_symbol``, and managed rows
    already marked ``closing_reason``. Count matches how max_positions is
    enforced (one slot per open name).
    """
    if not isinstance(positions, dict):
        return 0.0, 0
    skip = (skip_symbol or "").upper().strip()
    total = 0.0
    count = 0
    for raw_sym, row in positions.items():
        sym = str(raw_sym or "").upper().strip()
        if skip and sym == skip:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("closing_reason"):
            continue
        qty = abs(_f(row.get("qty"), 0.0))
        if qty <= 0:
            qty = abs(_f(row.get("total_qty"), 0.0))
        if qty <= 0:
            continue
        total += position_notional(row)
        count += 1
    return round(max(0.0, total), 4), count


def size_long_from_free_equity(
    *,
    account_equity: float,
    price: float,
    stop: float,
    risk_pct: float,
    open_notional: float,
    open_count: int,
    max_positions: int,
    slot_equity: float = 0.0,
    max_position_pct: float = 0.0,
    cheap: bool = False,
    cheap_pct: float = 0.0,
    max_open_risk_pct: float = 5.0,
    buying_power: float | None = None,
) -> FreeEquitySize:
    """Size a long from leftover equity across remaining slots.

    Rule (small-account first; documented so the 1-share tickets don't
    come back):

    * ``free_equity = max(0, account_equity - open_notional)``
    * ``remaining_slots = max(1, max_positions - open_count)``
    * ``target_notional = free_equity / remaining_slots``  (this is the
      size we want — ~3 shares of a $26 name on a $238 / 3-slot book,
      not the 1 share that 1% risk-of-equity produces)
    * ``qty = max(notional_qty, risk_qty)`` so a large account with a
      tight stop still gets the classic 1%-of-equity ticket
    * then clamp:
      - never more than leftover cash (can't spend occupied capital)
      - on a *small* book (configured % of equity still below one
        ``slot_equity``) never more than the slot notional, except a
        1-share floor when one share fits in free equity
      - on a *large* book, configured ``max_position_pct`` of account
        equity (the 8% name cap)
      - cheap names: ``cheap_pct`` of account equity
      - one trade cannot risk more than ``max_open_risk_pct`` of equity
        (existing book-risk ceiling; 3–4 full slots at ~1R of a 4% stop
        stay well under this)
      - buying power: downsize rather than hard-fail when BP is known

    ``max_position_pct`` is the *configured* cap (8%), not the inflated
    ``limits.max_position_pct`` that equity_book_limits uses as a $slot
    affordability floor on tiny accounts.
    """
    eq = max(0.0, _f(account_equity, 0.0))
    px = _f(price, 0.0)
    stp = _f(stop, 0.0)
    open_n = max(0.0, _f(open_notional, 0.0))
    open_c = max(0, _i(open_count, 0))
    max_pos = max(1, _i(max_positions, 1))
    free = max(0.0, eq - open_n)
    remaining = max(1, max_pos - open_c)
    target = free / float(remaining) if remaining else 0.0

    if px <= 0:
        return FreeEquitySize(
            qty=0, risk_qty=0, notional_qty=0,
            free_equity=round(free, 4), open_notional=round(open_n, 4),
            open_count=open_c, remaining_slots=remaining,
            target_notional=round(target, 4), capped_by="bad_price",
        )

    notional_qty = int(target // px) if target > 0 else 0
    per_share = px - stp
    if eq > 0 and per_share > 0:
        risk_qty = max(0, int((eq * max(0.0, _f(risk_pct, 0.0)) / 100.0) // per_share))
    else:
        risk_qty = 0

    qty = max(notional_qty, risk_qty)
    clamps: list[str] = []

    free_cap = int(free // px) if free > 0 else 0
    if qty > free_cap:
        qty = free_cap
        clamps.append("free_equity")

    slot_eq = _f(slot_equity, 0.0)
    cfg_pct = _f(max_position_pct, 0.0)
    cfg_cap_dollars = eq * cfg_pct / 100.0 if eq > 0 and cfg_pct > 0 else 0.0
    large_account = (
        cfg_cap_dollars > 0
        and (slot_eq <= 0 or cfg_cap_dollars + 1e-9 >= slot_eq)
    )
    if large_account:
        conc_qty = int(cfg_cap_dollars // px)
        if qty > conc_qty:
            qty = max(0, conc_qty)
            clamps.append("concentration")
    else:
        # Small book: slot notional is the size. Risk may not inflate past
        # one slot (a $100 name with 1% risk of $238 would otherwise take
        # 2 shares ≈ 84% of the book). Keep a 1-share floor so a name
        # dearer than one slot can still be bought when leftover cash
        # covers it — same idea as cap_long_qty promoting 0 → 1.
        slot_max = notional_qty
        if slot_max < 1 and free_cap >= 1:
            slot_max = 1
        if slot_max >= 0 and qty > slot_max:
            qty = slot_max
            clamps.append("slot_notional")

    if cheap and cheap_pct > 0 and eq > 0:
        cheap_qty = int((eq * _f(cheap_pct, 0.0) / 100.0) // px)
        if qty > cheap_qty:
            qty = max(0, cheap_qty)
            clamps.append("cheap")

    max_open = _f(max_open_risk_pct, 0.0)
    if max_open > 0 and per_share > 0 and eq > 0:
        risk_ceil = int((eq * max_open / 100.0) // per_share)
        if qty > risk_ceil:
            qty = max(0, risk_ceil)
            clamps.append("open_risk")

    if qty == 0 and free_cap >= 1 and not (cheap and cheap_pct > 0 and int((eq * _f(cheap_pct, 0.0) / 100.0) // px) < 1):
        qty = 1
        clamps.append("min_share")

    if buying_power is not None:
        bp = _f(buying_power, 0.0)
        if bp >= 0:
            bp_qty = int(bp // px) if bp > 0 else 0
            if qty > bp_qty:
                qty = max(0, bp_qty)
                clamps.append("buying_power")

    return FreeEquitySize(
        qty=int(qty),
        risk_qty=int(risk_qty),
        notional_qty=int(notional_qty),
        free_equity=round(free, 4),
        open_notional=round(open_n, 4),
        open_count=open_c,
        remaining_slots=remaining,
        target_notional=round(target, 4),
        capped_by=",".join(clamps),
    )


def dynamic_max_price(equity: float, cfg: dict[str, Any] | None = None) -> float:
    """Float the max_price upper limit based on account equity and risk limits.
    
    If equity is $250, the limit is smaller. If equity is $10k, it 
    scales up, allowing the system to see higher value stocks and open multiple
    positions.
    """
    eq = max(250.0, float(equity if equity else 250.0))
    limits = limits_from_cfg(eq, cfg)
    return max(10.0, limits.dollar_cap)
