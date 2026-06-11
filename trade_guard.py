"""
trade_guard.py — Account-level risk guard for the signal engine.

The strategies decide WHAT to trade; this module decides whether the account
is allowed to take another trade at all. It is the layer that makes the engine
safe to run unattended:

  • Daily realized-loss kill switch — once today's realized P&L drops below
    -DAILY_LOSS_LIMIT dollars, new buys are blocked for the rest of the ET day.
    Exits are never blocked.
  • Max round-trips per day — optional hard cap on trade count.
  • PDT protection — counts same-day round trips over a rolling 5-business-day
    window. A margin account under $25k is flagged as a pattern day trader at
    4 day trades in 5 business days; "block" mode refuses new buys once 3 are
    used (a scalp entry will almost certainly close the same day and become
    the 4th).

State persists to trade_guard_state.json so a mid-day engine restart can't
reset the kill switch. Daily counters roll over on the ET date change.

Usage (from signal_engine.py):
    guard = TradeGuard(daily_loss_limit=100, max_trades_per_day=0, pdt_protect="warn")
    ok, reason = guard.allow_buy()      # gate before every BUY
    guard.record_close(pnl_dollars, buy_time_iso)   # after every SELL
"""

from __future__ import annotations

import json
import tempfile
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# FINRA pattern-day-trader threshold: 4+ day trades in 5 business days.
# We gate at 3 used because the next scalp will close same-day and become the 4th.
PDT_DAY_TRADE_LIMIT = 3
PDT_WINDOW_DAYS = 5


def _today_et() -> str:
    return datetime.now(timezone.utc).astimezone(_ET).strftime("%Y-%m-%d")


def _et_date_of(iso_utc: Optional[str]) -> Optional[str]:
    """ET calendar date of a UTC ISO timestamp like 2026-06-11T14:30:00Z."""
    if not iso_utc:
        return None
    try:
        dt = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone(_ET).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


class TradeGuard:
    def __init__(self,
                 daily_loss_limit: float = 0.0,
                 max_trades_per_day: int = 0,
                 pdt_protect: str = "warn",
                 state_file: Optional[Path] = None):
        self.daily_loss_limit   = max(0.0, float(daily_loss_limit))
        self.max_trades_per_day = max(0, int(max_trades_per_day))
        self.pdt_protect        = pdt_protect if pdt_protect in ("warn", "block", "off") else "warn"
        # TRADE_GUARD_STATE_FILE env override exists so the test suite can never
        # pollute the real daily P&L / PDT counters (tests/conftest.py sets it).
        self.state_file = state_file or Path(
            os.getenv("TRADE_GUARD_STATE_FILE", "")
            or (Path(__file__).parent / "trade_guard_state.json"))

        # date          : ET day the daily counters belong to
        # realized_pnl  : $ closed P&L today
        # trades_today  : round trips closed today
        # day_trades    : {et_date: same-day-round-trip count} — PDT window
        self._date: str = _today_et()
        self._realized_pnl: float = 0.0
        self._trades_today: int = 0
        self._day_trades: dict[str, int] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self):
        if not self.state_file.exists():
            return
        try:
            s = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return
        self._day_trades = {k: int(v) for k, v in (s.get("day_trades") or {}).items()}
        if s.get("date") == _today_et():
            self._date         = s["date"]
            self._realized_pnl = float(s.get("realized_pnl", 0.0))
            self._trades_today = int(s.get("trades_today", 0))

    def _save(self):
        payload = {
            "date":         self._date,
            "realized_pnl": round(self._realized_pnl, 2),
            "trades_today": self._trades_today,
            "day_trades":   self._day_trades,
        }
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=self.state_file.parent, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            Path(tmp_path).replace(self.state_file)
            tmp_path = None
        except Exception as e:
            print(f"[GUARD] ⚠️  could not write {self.state_file.name}: {e}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

    def _rollover(self):
        """Reset daily counters when the ET date changes; prune the PDT window."""
        today = _today_et()
        if today != self._date:
            self._date         = today
            self._realized_pnl = 0.0
            self._trades_today = 0
        # Keep only recent dates in the PDT map (calendar buffer > 5 business days)
        if len(self._day_trades) > 12:
            for k in sorted(self._day_trades)[:-12]:
                del self._day_trades[k]

    # ── Queries ───────────────────────────────────────────────────────────────

    def day_trades_5d(self) -> int:
        """Same-day round trips across the 5 most recent business days recorded."""
        recent = sorted(self._day_trades)[-PDT_WINDOW_DAYS:]
        return sum(self._day_trades[d] for d in recent)

    def kill_switch_active(self) -> bool:
        self._rollover()
        return (self.daily_loss_limit > 0
                and self._realized_pnl <= -self.daily_loss_limit)

    def allow_buy(self) -> tuple[bool, Optional[str]]:
        """
        (ok, reason) — reason explains the block when ok is False.
        Never blocks exits; callers gate buys only.
        """
        self._rollover()

        if self.kill_switch_active():
            return False, (f"daily loss limit hit (${self._realized_pnl:,.2f} ≤ "
                           f"-${self.daily_loss_limit:,.2f}) — no new buys today")

        if self.max_trades_per_day > 0 and self._trades_today >= self.max_trades_per_day:
            return False, (f"max trades/day reached "
                           f"({self._trades_today}/{self.max_trades_per_day})")

        if self.pdt_protect != "off" and self.day_trades_5d() >= PDT_DAY_TRADE_LIMIT:
            msg = (f"PDT: {self.day_trades_5d()} day trades in 5 business days — "
                   f"next same-day close would flag the account")
            if self.pdt_protect == "block":
                return False, msg
            print(f"[GUARD] ⚠️  {msg} (PDT_PROTECT=warn — buy allowed)")

        return True, None

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_close(self, pnl_dollars: float, buy_time_iso: Optional[str] = None):
        """
        Call after every SELL. Updates the daily P&L, trade count, and — when
        the matching BUY happened on the same ET day — the PDT day-trade count.
        """
        self._rollover()
        self._realized_pnl += float(pnl_dollars)
        self._trades_today += 1

        if _et_date_of(buy_time_iso) == self._date:
            self._day_trades[self._date] = self._day_trades.get(self._date, 0) + 1

        if self.kill_switch_active():
            print(f"[GUARD] 🛑 KILL SWITCH — realized P&L today "
                  f"${self._realized_pnl:,.2f} breached -${self.daily_loss_limit:,.2f}. "
                  f"New buys halted until the next ET day. Exits still allowed.")
        self._save()

    # ── Observability ─────────────────────────────────────────────────────────

    def state(self) -> dict:
        self._rollover()
        return {
            "date":               self._date,
            "realized_pnl_today": round(self._realized_pnl, 2),
            "trades_today":       self._trades_today,
            "day_trades_5d":      self.day_trades_5d(),
            "kill_switch":        self.kill_switch_active(),
            "daily_loss_limit":   self.daily_loss_limit,
            "max_trades_per_day": self.max_trades_per_day,
            "pdt_protect":        self.pdt_protect,
        }
