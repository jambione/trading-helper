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


def column_cells(table, header):
    """Cells of the rich table column with this header — by NAME, not index.

    Roadmap tickets insert optional columns into the momentum table (RVOL
    after Chg%, a sparkline later), which shifts every position after the
    insertion point. Looking columns up positionally makes each addition
    break unrelated tests; looking them up by header does not.
    """
    for col in table.columns:
        if col.header == header:
            return list(col.cells)
    raise AssertionError(
        f"no column {header!r} in {[c.header for c in table.columns]}")
