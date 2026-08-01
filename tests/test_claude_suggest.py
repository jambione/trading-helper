"""Claude suggestions parse + panel columns for the momentum monitor."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from conftest import column_cells  # noqa: E402
import claude_suggest as cs  # noqa: E402
from claude_suggest import (  # noqa: E402
    ClaudeSuggestions,
    _place_qualifying_entries,
    parse_model_text,
    parse_suggestions,
)
from momentum_signal import DEFAULTS, claude_panel  # noqa: E402

T0 = 1753449600.0


def test_parse_suggestions_object():
    rows = parse_suggestions({
        "suggestions": [
            {"symbol": "NVDA", "score": 9.1, "reason": "AI momentum"},
            {"ticker": "ondS", "confidence": 7, "why": "gap up"},
            {"symbol": "$TSLA", "score": 5},
            {"symbol": "TOOLONGSYMBOLXX"},  # >6 chars after normalize
            {"symbol": "12"},               # must start with a letter
            "PLTR",
        ]
    })
    syms = [r["symbol"] for r in rows]
    assert syms == ["NVDA", "ONDS", "TSLA", "PLTR"]
    assert rows[0]["rank"] == 1
    assert rows[0]["trending_score"] == 9.1
    assert rows[0]["reason"] == "AI momentum"
    assert rows[1]["trending_score"] == 7.0
    assert rows[3]["trending_score"] is None


def test_parse_bare_array_and_fences():
    text = """```json
[{"symbol":"AAPL","score":8,"reason":"strong"},{"symbol":"MSFT","score":7}]
```"""
    rows = parse_model_text(text)
    assert [r["symbol"] for r in rows] == ["AAPL", "MSFT"]


def test_parse_trailer_after_research_prose():
    """Research prompt writes a full report; only the final JSON is the panel."""
    text = """
## Macro
Fed is on hold. Liquidity is fine.

## Top ideas
NVDA remains the cleanest AI infra name.

{"macro": "irrelevant intermediate blob"}

{
  "suggestions": [
    {"symbol": "NVDA", "score": 9.0, "reason": "AI infra", "p30": 0.6},
    {"symbol": "AVGO", "score": 8.2, "reason": "ASIC demand"}
  ],
  "disclaimer": "research only"
}
"""
    rows = parse_model_text(text)
    assert [r["symbol"] for r in rows] == ["NVDA", "AVGO"]
    assert rows[0]["p30"] == 0.6


def test_parse_dedupes_symbols():
    rows = parse_suggestions({
        "stocks": [
            {"symbol": "AAA", "score": 1},
            {"symbol": "AAA", "score": 9},
            {"symbol": "BBB"},
        ]
    })
    assert [r["symbol"] for r in rows] == ["AAA", "BBB"]
    assert rows[0]["trending_score"] == 1.0


def test_display_rows_max_price_and_limit():
    gs = ClaudeSuggestions(enrich_quotes=False, max_price=30.0)
    gs.rows = [
        {"symbol": "CHEAP", "rank": 1, "trending_score": 5, "price": 4.0,
         "reason": "a", "is_crypto": False},
        {"symbol": "EXPENSIVE", "rank": 2, "trending_score": 9, "price": 99.0,
         "reason": "b", "is_crypto": False},
        {"symbol": "MID", "rank": 3, "trending_score": 6, "price": 12.0,
         "reason": "c", "is_crypto": False},
    ]
    gs.by_symbol = {r["symbol"]: r for r in gs.rows}
    gs._seeded = True
    shown = gs.display_rows(limit=10)
    assert [r["symbol"] for r in shown] == ["CHEAP", "MID"]


def _gs(rows, **kw):
    gs = ClaudeSuggestions(enrich_quotes=False, max_price=None, **kw)
    gs.rows = rows
    gs.by_symbol = {r["symbol"]: r for r in rows}
    gs.last_ok = T0
    gs._seeded = True
    return gs


def _row(sym="AAAA", **kw):
    row = {
        "symbol": sym, "rank": 1, "trending_score": 8.0,
        "price": 4.20, "pct_change": 5.0, "pct_is_today": True,
        "vol_session": 1_500_000,
        "high_52w": 10.0, "low_52w": 1.0,
        "reason": "gap + volume",
    }
    row.update(kw)
    return row


def _table(gs, cfg=None, price_by_sym=None):
    panel = claude_panel(gs, price_by_sym or {}, limit=10,
                       hotkeys_on=True, cfg=cfg if cfg is not None
                       else DEFAULTS)
    return panel.renderable


def _headers(table):
    return [c.header for c in table.columns]


def test_claude_panel_columns_match_trending_shape():
    heads = _headers(_table(_gs([_row()])))
    # Same market columns as TRENDING, plus Why.
    for required in ("Key", "G#", "Symbol", "Last", "%Chg", "Vol·IEX",
                     "RVOL", "52w lo→hi", "Score", "Why"):
        assert required in heads, heads


def test_claude_panel_shows_reason_and_score():
    table = _table(_gs([_row(reason="AI chip demand", trending_score=9.5)]))
    assert "AI chip demand" in column_cells(table, "Why")[0]
    assert "9.5" in column_cells(table, "Score")[0]


def test_claude_panel_keys_are_k_through_t():
    rows = [_row(sym=f"S{i}", rank=i + 1) for i in range(3)]
    keys = column_cells(_table(_gs(rows)), "Key")
    assert keys == ["K", "L", "M"]


def test_claude_panel_empty_shows_error_in_title():
    """Empty state must not put the error into a table cell (reflows columns)."""
    gs = _gs([])
    gs.error = "no XAI_API_KEY (signal_engine.env)"
    panel = claude_panel(gs, {}, limit=10, hotkeys_on=True, cfg=DEFAULTS)
    assert "XAI_API_KEY" in str(panel.title)
    # Table has no body rows when empty.
    table = panel.renderable
    assert table.row_count == 0


def test_defaults_have_claude_off():
    assert DEFAULTS.get("claude_enabled") is False
    assert DEFAULTS.get("claude_poll", 0) >= 60
    # Desk price filter for Claude suggestions (under $100).
    assert DEFAULTS.get("claude_max_price") == 100.0
    assert DEFAULTS.get("claude_live_search") is True
    assert DEFAULTS.get("claude_backend") == "claude_cli"


def _et(y, m, d, hour):
    from datetime import datetime

    from stocktwits_trending import ET
    return datetime(y, m, d, hour, tzinfo=ET).timestamp()


def test_research_times_parse_from_json_or_env_string():
    from claude_suggest import parse_research_times

    assert parse_research_times(["04:00", "08:30", "13:00"]) == [
        (4, 0), (8, 30), (13, 0)]
    assert parse_research_times("04:00, 8:30 13:00") == [
        (4, 0), (8, 30), (13, 0)]
    # A typo drops that entry rather than taking the panel down.
    assert parse_research_times(["04:00", "nope", "25:00", "12:99"]) == [(4, 0)]


def test_each_scheduled_time_fires_once_per_day():
    """4:00, 8:30 and 13:00 ET each run once — and only once."""
    from claude_suggest import due_slot

    times = [(4, 0), (8, 30), (13, 0)]
    kw = dict(times=times, catchup_min=120)

    # Wed 2026-08-05, 04:00 ET — first slot of the day is due.
    assert due_slot(_et(2026, 8, 5, 4), last_slot="", **kw) == "2026-08-05T04:00"
    # Same slot already claimed → not due again an hour later.
    assert due_slot(_et(2026, 8, 5, 5),
                    last_slot="2026-08-05T04:00", **kw) is None
    # 08:30 is a new slot.
    assert due_slot(_et(2026, 8, 5, 9),
                    last_slot="2026-08-05T04:00", **kw) == "2026-08-05T08:30"
    # Before the first slot there is nothing to run.
    assert due_slot(_et(2026, 8, 5, 2), last_slot="", **kw) is None


def test_missed_slots_run_once_on_current_data_not_replayed():
    """A desk down all morning should run the latest slot, not all three."""
    from claude_suggest import due_slot

    times = [(4, 0), (8, 30), (13, 0)]
    # 13:30 ET with nothing run today → the 13:00 slot, skipping 04:00/08:30.
    assert due_slot(_et(2026, 8, 5, 13), times=times, catchup_min=120,
                    last_slot="") == "2026-08-05T13:00"


def test_stale_slots_expire_rather_than_firing_on_dead_data():
    """Starting the desk at 23:00 must not fire the 13:00 run."""
    from claude_suggest import due_slot

    assert due_slot(_et(2026, 8, 5, 23), times=[(13, 0)],
                    catchup_min=120, last_slot="") is None


def test_weekends_are_skipped():
    from claude_suggest import due_slot

    times = [(4, 0), (8, 30), (13, 0)]
    # Sat 2026-08-08 / Sun 2026-08-09 at 09:00 ET.
    assert due_slot(_et(2026, 8, 8, 9), times=times, last_slot="") is None
    assert due_slot(_et(2026, 8, 9, 9), times=times, last_slot="") is None
    assert due_slot(_et(2026, 8, 9, 9), times=times, weekdays_only=False,
                    last_slot="") == "2026-08-09T08:30"


def test_refresh_off_schedule_reports_the_next_run_and_does_not_poll():
    gs = ClaudeSuggestions(enrich_quotes=False)
    assert gs.refresh(now=_et(2026, 8, 9, 3)) is False   # Sunday 03:00 ET
    assert gs.last_attempt == 0.0
    assert "next research run" in gs.error
    # Sunday 03:00 → next weekday slot is Monday 04:00.
    assert gs.next_run_label(_et(2026, 8, 9, 3)) == "Mon 04:00"


def test_defaults_are_three_scheduled_runs_at_full_depth():
    """Cost is dominated by search fees, not thinking tokens, so effort buys
    cheap depth — spend is cut by running three times a day instead."""
    assert DEFAULTS["claude_effort"] == "xhigh"
    # 11:00 and 13:00 are both inside RTH (04:00 is pre-market prep only) —
    # two real chances a day to actually open a position, not just one.
    assert DEFAULTS["claude_research_times"] == ["04:00", "11:00", "13:00"]
    assert DEFAULTS["claude_research_weekdays_only"] is True


class _StubTrading:
    """Stands in for claude_trading — records whether entry checks were even
    attempted, since the point is to never spend on them pre-market."""

    def __init__(self, ready=True, market_open=True):
        self._ready = ready
        self._market_open = market_open
        self.reset_called = False

    def is_ready(self):
        return self._ready

    def market_is_open(self):
        return self._market_open

    def reset_poll_counters(self):
        self.reset_called = True

    def buys_left_this_poll(self):
        return 3

    def has_open_position(self, sym):
        return False

    def can_open_new_position(self, sym):
        return True

    def _latest_ask(self, sym):
        return 40.5

    def get_account(self):
        return {"ok": True, "equity": 50_000.0}

    def record_external_buy(self, sym, extra):
        pass


class _StubPositions:
    def __init__(self):
        self.evaluate_calls: list[str] = []

    def evaluate_entry(self, sym, ask, equity, **kw):
        self.evaluate_calls.append(sym)
        return {"decision": "BUY"}

    def qualifies_as_entry(self, decision, **kw):
        return False  # never actually place an order in this test

    def place_scaled_entry(self, *a, **kw):
        raise AssertionError("should never be reached")


def _run_entry_gate(monkeypatch, *, ready=True, market_open=True):
    trading = _StubTrading(ready=ready, market_open=market_open)
    positions = _StubPositions()
    monkeypatch.setitem(sys.modules, "claude_trading", trading)
    monkeypatch.setitem(sys.modules, "claude_positions", positions)
    rows = [{"symbol": "NVDA", "trending_score": 9.0, "reason": "test"}]
    _place_qualifying_entries(
        rows, max_price=None, cli_bin=None, timeout=60.0,
        risk_pct=1.0, trade_style="Moderate position", min_reward_risk=3.0,
    )
    return trading, positions


def test_entry_checks_are_skipped_entirely_outside_market_hours(monkeypatch):
    """An entry check is its own full research-depth call — running it
    pre-market only to have the order discarded would spend real money for
    nothing, so the whole loop must not run at all, not just the order."""
    trading, positions = _run_entry_gate(monkeypatch, market_open=False)
    assert positions.evaluate_calls == []
    assert trading.reset_called is False


def test_entry_checks_run_when_market_is_open(monkeypatch):
    trading, positions = _run_entry_gate(monkeypatch, market_open=True)
    assert positions.evaluate_calls == ["NVDA"]
    assert trading.reset_called is True


def test_entry_checks_skipped_when_claude_trading_not_ready(monkeypatch):
    trading, positions = _run_entry_gate(monkeypatch, ready=False,
                                         market_open=True)
    assert positions.evaluate_calls == []


def test_parse_ranked_prose_fallback():
    from claude_suggest import parse_model_text
    text = """
## Final ranking
1. **MU** (highest risk-adj): Extreme undervaluation to structural HBM growth.
2. **VST**: Contracted AI nuclear cash flows.
3. **AVGO**: Diversified AI silicon growth.
"""
    rows = parse_model_text(text)
    assert [r["symbol"] for r in rows] == ["MU", "VST", "AVGO"]


def test_cli_resolver_and_default_backend():
    from claude_suggest import (
        DEFAULT_BACKEND, resolve_grok_cli, resolve_claude_cli,
        cli_available, claude_cli_available,
    )
    assert DEFAULT_BACKEND == "claude_cli"
    path = resolve_grok_cli("grok")
    assert path is None or path.endswith("grok")
    cpath = resolve_claude_cli("claude")
    assert cpath is None or "claude" in cpath
    assert isinstance(cli_available(), bool)
    assert isinstance(claude_cli_available(), bool)


def test_parse_claude_rich_suggestion_shape():
    from claude_suggest import parse_model_text
    text = '''
    {
      "suggestions": [
        {
          "ticker": "TVTX",
          "conviction": "HIGH",
          "thesis": {"one_line": "Filspari revenue story"},
          "last_price_usd": 27.0
        }
      ]
    }
    '''
    rows = parse_model_text(text)
    assert rows[0]["symbol"] == "TVTX"
    assert rows[0]["trending_score"] == 8.5
    assert "Filspari" in rows[0]["reason"]
