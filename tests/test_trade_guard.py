"""
test_trade_guard.py — offline tests for the account-level risk guard.

No network, no Alpaca: TradeGuard is pure local state persisted to a temp file.

Run:
    venv/bin/python -m pytest tests/test_trade_guard.py -q
"""

import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trade_guard import TradeGuard  # noqa: E402


@pytest.fixture
def guard_file(tmp_path):
    return tmp_path / "guard_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Kill switch ───────────────────────────────────────────────────────────────

def test_buys_allowed_by_default(guard_file):
    g = TradeGuard(daily_loss_limit=100, state_file=guard_file)
    ok, reason = g.allow_buy()
    assert ok and reason is None


def test_kill_switch_fires_on_daily_loss(guard_file):
    g = TradeGuard(daily_loss_limit=100, state_file=guard_file)
    g.record_close(-60, _now_iso())
    assert g.allow_buy()[0]            # -60 > -100 — still trading
    g.record_close(-50, _now_iso())    # cumulative -110
    ok, reason = g.allow_buy()
    assert not ok
    assert "loss limit" in reason
    assert g.kill_switch_active()


def test_wins_offset_losses(guard_file):
    g = TradeGuard(daily_loss_limit=100, state_file=guard_file)
    g.record_close(80, _now_iso())
    g.record_close(-150, _now_iso())   # net -70 — under the limit
    assert g.allow_buy()[0]


def test_zero_limit_disables_kill_switch(guard_file):
    g = TradeGuard(daily_loss_limit=0, state_file=guard_file)
    g.record_close(-10_000, _now_iso())
    assert g.allow_buy()[0]


def test_kill_switch_survives_restart(guard_file):
    g = TradeGuard(daily_loss_limit=100, state_file=guard_file)
    g.record_close(-150, _now_iso())
    assert not g.allow_buy()[0]
    # Fresh instance, same file — mid-day restart can't reset the switch
    g2 = TradeGuard(daily_loss_limit=100, state_file=guard_file)
    assert not g2.allow_buy()[0]
    assert g2.state()["realized_pnl_today"] == -150


def test_counters_reset_on_new_day(guard_file):
    g = TradeGuard(daily_loss_limit=100, state_file=guard_file)
    g.record_close(-150, _now_iso())
    assert not g.allow_buy()[0]
    g._date = "2000-01-01"   # simulate a stale date → rollover on next check
    ok, _ = g.allow_buy()
    assert ok
    assert g.state()["realized_pnl_today"] == 0


# ── Max trades per day ────────────────────────────────────────────────────────

def test_max_trades_per_day(guard_file):
    g = TradeGuard(max_trades_per_day=2, state_file=guard_file)
    g.record_close(5, _now_iso())
    assert g.allow_buy()[0]
    g.record_close(5, _now_iso())
    ok, reason = g.allow_buy()
    assert not ok
    assert "max trades" in reason


# ── PDT ───────────────────────────────────────────────────────────────────────

def test_pdt_counts_same_day_round_trips_only(guard_file):
    g = TradeGuard(pdt_protect="block", state_file=guard_file)
    # Overnight hold (bought yesterday) — NOT a day trade
    g.record_close(5, "2000-01-01T15:00:00Z")
    assert g.day_trades_5d() == 0
    # Same-day round trips ARE day trades
    g.record_close(5, _now_iso())
    g.record_close(5, _now_iso())
    assert g.day_trades_5d() == 2
    assert g.allow_buy()[0]
    g.record_close(5, _now_iso())
    ok, reason = g.allow_buy()
    assert not ok
    assert "PDT" in reason


def test_pdt_warn_mode_allows_buy(guard_file):
    g = TradeGuard(pdt_protect="warn", state_file=guard_file)
    for _ in range(4):
        g.record_close(5, _now_iso())
    assert g.day_trades_5d() == 4
    assert g.allow_buy()[0]   # warns but allows


def test_pdt_gate_prefers_broker_count():
    from trade_guard import pdt_gate
    ok, why = pdt_gate(mode="block", broker_daytrade_count=3, equity=8_000)
    assert not ok and why.startswith("pdt_")
    ok, _ = pdt_gate(mode="block", broker_daytrade_count=3, equity=25_000)
    assert ok
    ok, _ = pdt_gate(mode="off", broker_daytrade_count=5, equity=1_000)
    assert ok
    ok, _ = pdt_gate(mode="block", broker_daytrade_count=1,
                     local_day_trades=5, equity=8_000)
    assert ok  # broker count wins


def test_pdt_off_mode(guard_file):
    g = TradeGuard(pdt_protect="off", state_file=guard_file)
    for _ in range(5):
        g.record_close(5, _now_iso())
    assert g.allow_buy()[0]


# ── Observability ─────────────────────────────────────────────────────────────

def test_state_snapshot(guard_file):
    g = TradeGuard(daily_loss_limit=100, max_trades_per_day=10,
                   pdt_protect="warn", state_file=guard_file)
    g.record_close(-25.5, _now_iso())
    s = g.state()
    assert s["realized_pnl_today"] == -25.5
    assert s["trades_today"] == 1
    assert s["day_trades_5d"] == 1
    assert s["kill_switch"] is False
    assert s["pdt_protect"] == "warn"
