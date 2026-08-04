"""Adaptive entry pricing policy."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entry_pricing import (  # noqa: E402
    decide,
    limit_for_style,
    style_from_urgency,
    urgency_score,
)


def test_urgency_higher_when_hot_and_tight():
    cold = urgency_score(
        spread_pct_val=0.8, rvol=0.5, proximity_pct=20, rvol_hot=3.0)
    hot = urgency_score(
        spread_pct_val=0.1, rvol=4.0, proximity_pct=100, rvol_hot=3.0)
    assert hot > cold
    assert 0 <= cold <= 1 and 0 <= hot <= 1


def test_style_bands():
    assert style_from_urgency(0.2) == "passive"
    assert style_from_urgency(0.5) == "fair"
    assert style_from_urgency(0.9) == "join"


def test_limit_for_style_ordering():
    bid, ask = 10.0, 10.10
    passive = limit_for_style("passive", bid, ask)
    fair = limit_for_style("fair", bid, ask)
    join = limit_for_style("join", bid, ask, pad_pct=0.1)
    assert bid <= passive <= fair <= join
    assert join >= ask  # pad above ask


def test_decide_reject_wide_spread():
    d = decide(bid=10.0, ask=10.50, max_spread_pct=1.0)  # 5% wide
    assert not d.ok
    assert "spread" in d.reason


def test_decide_reject_no_quote():
    d = decide(bid=None, ask=None, last=None)
    assert not d.ok


def test_decide_passive_on_cool_tape():
    d = decide(
        bid=20.0, ask=20.02,  # tight 0.1%
        rvol=0.8, proximity_pct=15,
        max_spread_pct=1.0, rvol_hot=3.0,
    )
    assert d.ok
    # Cool setup → not join
    assert d.style in ("passive", "fair")
    assert d.limit_px is not None
    assert 20.0 <= d.limit_px <= 20.05


def test_decide_join_on_hot_buy_zone():
    d = decide(
        bid=5.00, ask=5.01,
        rvol=5.0, proximity_pct=100,
        max_spread_pct=1.0, pad_pct=0.1, pad_max_pct=0.15, rvol_hot=3.0,
    )
    assert d.ok
    assert d.style == "join"
    assert d.limit_px is not None
    assert d.limit_px >= 5.01


def test_decide_max_price():
    d = decide(bid=50.0, ask=50.05, max_price=50.0)
    assert not d.ok
