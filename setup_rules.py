"""The operator's setup, written once so live and lab cannot disagree.

Stated 2026-08-23, after every marginal gate this lab tested came back at
the null. The reason those tests were uninformative is arithmetic: this
conjunction occurs on **25 of 493 logged name-days (5.1%)**. A 25-sample
effect inside a 493-sample average does not move the average, however
strong it is — so "no gate selects drift" was only ever a statement about
single filters, never about this.

STAGE 1 — which names are playable at all today:

    up >= 10% on the session          momentum is already established
    RVOL >= 5                          and it is being paid for
    a news catalyst within 24h         there is a reason, not just a crowd
    price $2-$20                       where these moves actually happen
    shares outstanding < 10M           the supply constraint. THE mechanism.

Share count is the only *cause* in that list; the other four are readings
of the effect. It is also why RVOL alone measured anti-predictive here —
5x volume on a 500M-share company is liquidity, on a 3M-share company it
is a squeeze, and pooling them hides the second.

STAGE 2 — when to enter and exit a name that qualifies (see setup_screen):

    both %R lines rising together toward overbought is the move
    one line turning down ends it
    RSI entered at the bottom of its oscillation, exited at the top

Stage 2 was previously tested against an UNFILTERED stage-1 universe,
which is a timing rule applied to the wrong names and says nothing about
the rule.

Every threshold is a named default here rather than a literal at the call
site, so the live row, the screen and the tests are provably the same
rule. `unknown` inputs never pass: a missing share count or a missing
RVOL is not evidence of anything, and admitting on absence is how the
thin-tape floor got bypassed by 19 fills with no reading at all.
"""
from __future__ import annotations

from typing import Any

MIN_PCT_CHANGE = 10.0
MIN_RVOL = 5.0
MAX_RVOL = 100.0          # above this the reading is a producer bug
MIN_PRICE = 2.0
MAX_PRICE = 20.0
MAX_SHARES_OUT_M = 10.0   # millions; OUTSTANDING, an upper bound on float
NEWS_WINDOW_MIN = 24 * 60.0


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def evaluate(*, pct_change: Any = None, rvol: Any = None, price: Any = None,
             shares_out_m: Any = None, news_mins_since: Any = None,
             news_n_24h: Any = None,
             max_shares_out_m: float = MAX_SHARES_OUT_M) -> dict:
    """Score one observation against stage 1. Pure; never raises.

    Returns each leg separately as well as the conjunction, because a
    setup that fails on one leg is a different thing from one that fails
    on four, and the per-leg counts are what say which condition is doing
    the filtering.
    """
    pct, rv, px = _f(pct_change), _f(rvol), _f(price)
    so = _f(shares_out_m)
    mins = _f(news_mins_since)
    n24 = _f(news_n_24h)

    legs = {
        "up": pct is not None and pct >= MIN_PCT_CHANGE,
        "rvol": rv is not None and MIN_RVOL <= rv <= MAX_RVOL,
        "price": px is not None and MIN_PRICE <= px <= MAX_PRICE,
        # Either a counted headline in the window, or a recent-enough one.
        "news": bool(
            (n24 is not None and n24 >= 1)
            or (mins is not None and mins <= NEWS_WINDOW_MIN)),
        # None (never looked up) must not pass. See float_feed.is_low_float.
        # The cap is a PARAMETER, not the module constant: the screen has a
        # --max-shares-m flag, and a flag that silently does nothing is the
        # exact defect that let ai_watch_min_pct_change read 50 for weeks
        # while the path that admitted most names ignored it.
        "float": so is not None and so < float(max_shares_out_m),
    }
    legs["ok"] = all(legs.values())
    legs["n_legs"] = sum(1 for k, v in legs.items()
                         if k not in ("ok", "n_legs") and v)
    return legs


def stage2(*, pctr_rising: Any = None, pctr_slow_rising: Any = None,
           pctr_slow_falling: Any = None, cm_rsi: Any = None) -> dict:
    """Timing state for a name that already cleared stage 1.

    `both_rising` is the operator's sweet spot — fast and slow travelling
    toward overbought together. `diverging` is the exit tell: one line has
    turned while the other has not, which is where the gain stops.
    """
    fast = bool(pctr_rising) if pctr_rising is not None else None
    slow = bool(pctr_slow_rising) if pctr_slow_rising is not None else None
    slow_dn = bool(pctr_slow_falling) if pctr_slow_falling is not None else None
    both = (fast and slow) if (fast is not None and slow is not None) else None
    diverging = None
    if fast is not None and slow is not None:
        diverging = bool(fast != slow) or bool(slow_dn and fast)
    return {
        "pctr_both_rising": both,
        "pctr_diverging": diverging,
        "cm_rsi": _f(cm_rsi),
    }
