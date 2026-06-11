"""
conftest.py — suite-wide guards.

Redirect the TradeGuard state file BEFORE any test imports signal_engine
(whose module-level GUARD instance binds the path at import). Without this,
tests that drive log_sell would write fake trades into the real
trade_guard_state.json — and fake losses could trip the real kill switch.
"""

import os
import tempfile

os.environ.setdefault(
    "TRADE_GUARD_STATE_FILE",
    os.path.join(tempfile.mkdtemp(prefix="trade_guard_test_"), "trade_guard_state.json"),
)
