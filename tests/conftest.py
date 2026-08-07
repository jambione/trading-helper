"""
conftest.py — suite-wide guards.

Redirect the TradeGuard state file BEFORE any test imports signal_engine
(whose module-level GUARD instance binds the path at import). Without this,
tests that drive log_sell would write fake trades into the real
trade_guard_state.json — and fake losses could trip the real kill switch.
"""

import os
import tempfile

import pytest

os.environ.setdefault(
    "TRADE_GUARD_STATE_FILE",
    os.path.join(tempfile.mkdtemp(prefix="trade_guard_test_"), "trade_guard_state.json"),
)

# Same problem, different file: dashboard binds the benchmark log paths at
# import, so tests that drive a mention burst appended synthetic rows — null
# price, identical timestamps — straight into benchmarks/mention_bursts.jsonl.
# That file is analysis input for threshold work, so a polluted run quietly
# biases whatever it is later used to calibrate.
os.environ.setdefault(
    "BENCHMARK_DIR", tempfile.mkdtemp(prefix="benchmarks_test_"),
)

# Third instance of the same problem: ai_positions / ai_suggest / ai_entry_watch
# bind six paths off ai_paths.resolve_report_dir() at import — events.jsonl,
# outcomes.jsonl, positions/open-bell/SOD/EOD state, token_metrics.jsonl,
# schedule_state.json — and only three were monkeypatched per-test. So a plain
# `pytest` run appended fixture events (SMCI/NVDA, broker_down, $40-$41 zones)
# straight into the live claude_reports/events.jsonl, during trading hours.
# That file is the desk's audit trail and the input to any fill/skip analysis,
# so a polluted run quietly corrupts whatever it is later used to measure.
os.environ.setdefault(
    "AI_REPORT_DIR", tempfile.mkdtemp(prefix="ai_reports_test_"),
)

# Fourth instance, and the first that feeds a live trading decision rather than
# an audit trail. morning_funnel persists the day's average session volumes to
# cache/avg_session_volume.json so a restart does not have to rebuild them with
# the desk's heaviest request. Tests drive avg_session_volumes with synthetic
# frames, so an unredirected run writes fixture baselines into the file the
# running screener reads — and that number is the DENOMINATOR of rvol, which
# gates AI Watch admission. A fake baseline there does not fail loudly; it
# quietly rescales every rvol on the desk. Today only the ET date stamp on the
# file kept a fixture-dated write from being loaded.
os.environ.setdefault(
    "AVG_VOL_CACHE_FILE",
    os.path.join(tempfile.mkdtemp(prefix="avg_vol_test_"),
                 "avg_session_volume.json"),
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


@pytest.fixture(autouse=True)
def _permissive_tradability():
    """Let order-mechanics tests place buys without an asset lookup.

    alpaca_trader.symbol_tradable asks Alpaca whether it will accept an order
    for a symbol, and fails closed. Every test that arms the trader with a mock
    client would otherwise have its buys refused — they assert qty rounding,
    limit prices, TIF and bracket shape, none of which is what the gate is for.
    Same reasoning as _arm_trader disabling _require_protective_exit: the
    policy has its own tests (test_tradable_gate.py, which restores the real
    function) and is not what these are checking.
    """
    import alpaca_trader as _at
    orig = _at.symbol_tradable
    _at.symbol_tradable = lambda ticker: True
    try:
        yield
    finally:
        _at.symbol_tradable = orig
