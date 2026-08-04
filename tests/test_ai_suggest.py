"""Claude suggestions parse + panel columns for the momentum monitor."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from conftest import column_cells  # noqa: E402
import ai_suggest as cs  # noqa: E402
from ai_suggest import (  # noqa: E402
    AiSuggestions,
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
    assert rows[0]["source"] == "anthropic"
    assert rows[0]["source_mark"] == "A"


def test_source_marks_anthropic_and_xai():
    from ai_suggest import (
        ai_source_mark,
        normalize_ai_source,
        parse_suggestions,
        source_from_backend,
    )
    assert normalize_ai_source("claude_cli") == "anthropic"
    assert normalize_ai_source("grok") == "xai"
    assert ai_source_mark("claude") == "A"
    assert ai_source_mark("xai") == "X"
    assert source_from_backend("cli") == "xai"
    assert source_from_backend("claude_cli") == "anthropic"
    x_rows = parse_suggestions(
        {"suggestions": [{"symbol": "SOFI", "score": 8}]}, source="xai")
    assert x_rows[0]["source"] == "xai"
    assert x_rows[0]["source_mark"] == "X"


def test_merge_suggestion_rows_agreement_first():
    from ai_suggest import merge_suggestion_rows

    a = [
        {"symbol": "ONLYA", "rank": 1, "trending_score": 9.0, "reason": "a-only",
         "source": "anthropic"},
        {"symbol": "BOTH", "rank": 2, "trending_score": 7.0, "reason": "from-a",
         "source": "anthropic"},
    ]
    x = [
        {"symbol": "BOTH", "rank": 1, "trending_score": 8.5, "reason": "from-x",
         "source": "xai"},
        {"symbol": "ONLYX", "rank": 3, "trending_score": 6.0, "reason": "x-only",
         "source": "xai"},
    ]
    merged = merge_suggestion_rows(a, x)
    syms = [r["symbol"] for r in merged]
    # Agreement first, then by max score: BOTH (8.5), ONLYA (9.0)... wait
    # BOTH max score 8.5, ONLYA 9.0 — agreement ranks before score, so BOTH first.
    assert syms[0] == "BOTH"
    assert merged[0]["source_mark"] == "AX"
    assert merged[0]["agreement"] is True
    assert merged[0]["trending_score"] == 8.5  # max of 7.0 and 8.5
    assert "A:" in merged[0]["reason"] and "X:" in merged[0]["reason"]
    # Single-source marks
    by = {r["symbol"]: r for r in merged}
    assert by["ONLYA"]["source_mark"] == "A"
    assert by["ONLYX"]["source_mark"] == "X"
    assert by["ONLYA"]["agreement"] is False


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
    gs = AiSuggestions(enrich_quotes=False, max_price=30.0)
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
    gs = AiSuggestions(enrich_quotes=False, max_price=None, **kw)
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
    # Same market columns as TRENDING, plus Src + Why.
    for required in ("Key", "G#", "Src", "Symbol", "Last", "%Chg", "Vol·IEX",
                     "RVOL", "52w lo→hi", "Score", "Why"):
        assert required in heads, heads


def test_claude_panel_shows_reason_and_score():
    table = _table(_gs([_row(reason="AI chip demand", trending_score=9.5)]))
    assert "AI chip demand" in column_cells(table, "Why")[0]
    assert "9.5" in column_cells(table, "Score")[0]


def test_claude_panel_shows_source_marks():
    rows = [
        _row(sym="AAA", rank=1, source="anthropic", source_mark="A"),
        _row(sym="BBB", rank=2, source="xai", source_mark="X"),
        _row(sym="CCC", rank=3, source="both", source_mark="AX", agreement=True),
    ]
    table = _table(_gs(rows))
    src = column_cells(table, "Src")
    # Markup stripped or raw — accept either plain letter or styled fragment.
    assert any("A" in c for c in src)
    assert any("X" in c for c in src)
    assert any("AX" in c for c in src)
    panel = claude_panel(_gs(rows), {}, limit=10, hotkeys_on=True, cfg=DEFAULTS)
    assert "Anthropic" in str(panel.title)
    assert "xAI" in str(panel.title)
    assert "both" in str(panel.title).lower() or "AX" in str(panel.title)


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
    from config import DEFAULT_CONFIG

    # The monitor only renders: the panel switch and its price filter.
    assert DEFAULTS.get("claude_enabled") is False
    assert DEFAULTS.get("claude_max_price") == 100.0
    # Everything that runs or spends lives with ai_trader.py on the server,
    # and every switch that can place an order ships off.
    assert DEFAULT_CONFIG["claude_live_search"] is True
    assert DEFAULT_CONFIG["claude_backend"] == "claude_cli"
    assert DEFAULT_CONFIG["ai_trader_enabled"] is False
    assert DEFAULT_CONFIG["ai_trading_enabled"] is False
    assert DEFAULT_CONFIG["claude_trader_enabled"] is False
    assert DEFAULT_CONFIG["claude_research_enabled"] is False
    assert DEFAULT_CONFIG["claude_trading_enabled"] is False
    # Grok source stubs (subscription CLI) — research/trading off until wired.
    assert DEFAULT_CONFIG["grok_research_enabled"] is False
    assert DEFAULT_CONFIG["grok_trading_enabled"] is False
    assert DEFAULT_CONFIG["grok_backend"] == "cli"
    assert DEFAULT_CONFIG["grok_max_turns"] == 4


def test_monitor_defaults_cannot_trade():
    """The desk is a renderer. If a trading knob reappears in its config, the
    server and the desk could both manage the same account."""
    for key in ("ai_trading_enabled", "ai_risk_pct", "ai_trade_amount",
                "claude_trading_enabled", "claude_risk_pct",
                "claude_trade_amount", "claude_backend", "claude_model",
                "claude_research_times", "grok_research_enabled",
                "grok_trading_enabled", "grok_backend"):
        assert key not in DEFAULTS, f"{key} belongs to the server trader"


def _et(y, m, d, hour):
    from datetime import datetime

    from stocktwits_trending import ET
    return datetime(y, m, d, hour, tzinfo=ET).timestamp()


def test_research_times_parse_from_json_or_env_string():
    from ai_suggest import parse_research_times

    assert parse_research_times(["04:00", "08:30", "13:00"]) == [
        (4, 0), (8, 30), (13, 0)]
    assert parse_research_times("04:00, 8:30 13:00") == [
        (4, 0), (8, 30), (13, 0)]
    # A typo drops that entry rather than taking the panel down.
    assert parse_research_times(["04:00", "nope", "25:00", "12:99"]) == [(4, 0)]


def test_each_scheduled_time_fires_once_per_day():
    """4:00, 8:30 and 13:00 ET each run once — and only once."""
    from ai_suggest import due_slot

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
    from ai_suggest import due_slot

    times = [(4, 0), (8, 30), (13, 0)]
    # 13:30 ET with nothing run today → the 13:00 slot, skipping 04:00/08:30.
    assert due_slot(_et(2026, 8, 5, 13), times=times, catchup_min=120,
                    last_slot="") == "2026-08-05T13:00"


def test_stale_slots_expire_rather_than_firing_on_dead_data():
    """Starting the desk at 23:00 must not fire the 13:00 run."""
    from ai_suggest import due_slot

    assert due_slot(_et(2026, 8, 5, 23), times=[(13, 0)],
                    catchup_min=120, last_slot="") is None


def test_weekends_are_skipped():
    from ai_suggest import due_slot

    times = [(4, 0), (8, 30), (13, 0)]
    # Sat 2026-08-08 / Sun 2026-08-09 at 09:00 ET.
    assert due_slot(_et(2026, 8, 8, 9), times=times, last_slot="") is None
    assert due_slot(_et(2026, 8, 9, 9), times=times, last_slot="") is None
    assert due_slot(_et(2026, 8, 9, 9), times=times, weekdays_only=False,
                    last_slot="") == "2026-08-09T08:30"


def test_refresh_off_schedule_reports_the_next_run_and_does_not_poll():
    gs = AiSuggestions(enrich_quotes=False)
    assert gs.refresh(now=_et(2026, 8, 9, 3)) is False   # Sunday 03:00 ET
    assert gs.last_attempt == 0.0
    assert "next research run" in gs.error
    # Sunday 03:00 → next weekday slot is Monday 04:00.
    assert gs.next_run_label(_et(2026, 8, 9, 3)) == "Mon 04:00"


def test_research_tools_web_x_and_off():
    """API path attaches x_search only when search_tools requests it."""
    web_only = cs._research_tools(True, "web")
    assert web_only == [{"type": "web_search"}]

    both = cs._research_tools(True, "web_x")
    assert {"type": "web_search"} in both
    assert {"type": "x_search"} in both
    assert len(both) == 2

    assert cs._research_tools(True, "both") == both
    assert cs._research_tools(True, "none") == []
    assert cs._research_tools(False, "web_x") == []
    # Module default prefers web + X for up-to-date social context.
    assert cs.DEFAULT_SEARCH_TOOLS == "web_x"
    assert cs._research_tools(True, "") == both


def test_prompt_requires_freshness_and_x_guidance():
    text = cs.load_prompt("ai_prompt.txt")
    low = text.lower()
    assert "freshness" in low
    assert "x.com" in low or "x_search" in low
    assert "48 hour" in low or "48h" in low or "last 48" in low
    assert "do not re-list" in low or "re-validat" in low


def test_desk_snapshot_rs_trending_and_peer(tmp_path):
    """Snapshot is compact, price-capped, and peer-board aware."""
    rs_path = tmp_path / "rs.json"
    tr_path = tmp_path / "tr.json"
    peer_path = tmp_path / "peer.json"
    sig_path = tmp_path / "signal_state.json"
    rs_path.write_text(json.dumps({
        "as_of": "2026-08-01",
        "rows": [
            {"ticker": "HPE", "rs_rating": 98, "price": 48.2,
             "ret_1m": 0.03, "ret_3m": 0.7},
            {"ticker": "AAPL", "rs_rating": 99, "price": 200.0,
             "ret_1m": 0.01, "ret_3m": 0.1},  # over cap
            {"ticker": "VTRS", "rs_rating": 94, "price": 17.5,
             "ret_1m": -0.02, "ret_3m": 0.2},
            {"ticker": "LOW", "rs_rating": 50, "price": 10.0,
             "ret_1m": 0.0, "ret_3m": 0.0},  # below min_rs
        ],
    }), encoding="utf-8")
    tr_path.write_text(json.dumps({
        "rows": [
            {"symbol": "SOFI", "trending_score": 8.2, "price": 16.0,
             "pct_change": 0.02, "rvol": 1.5, "is_equity": True},
            {"symbol": "BRK.A", "trending_score": 9.0, "price": 500000,
             "pct_change": 0.0, "is_equity": True},  # over cap
            {"symbol": "BTC", "trending_score": 9.0, "price": 1.0,
             "is_crypto": True},
        ],
    }), encoding="utf-8")
    peer_path.write_text(json.dumps({
        "rows": [
            {"symbol": "SMCI", "trending_score": 7.6, "reason": "AI servers"},
            {"symbol": "HOOD", "score": 7.0, "reason": "crypto cycle"},
        ],
    }), encoding="utf-8")
    sig_path.write_text(json.dumps({
        "tickers": {
            "ACHR": {"price": 8.5, "is_hot": True, "proximity_pct": 40,
                     "status": "watching"},
            "NVDA": {"price": 120.0, "is_hot": True},  # over cap
            "GEVO": {"price": 2.1, "is_hot": False, "proximity_pct": 10,
                     "status": "watching"},
        },
    }), encoding="utf-8")

    snap = cs.build_desk_snapshot_snippet(
        max_price=100.0,
        backend="claude_cli",
        rs_path=rs_path,
        trending_path=tr_path,
        signal_state_path=sig_path,
        peer_path=peer_path,
    )
    assert "DESK SNAPSHOT" in snap
    assert "hints only" in snap
    assert "Momentum" in snap
    assert "ACHR" in snap and "HOT" in snap
    assert "GEVO" in snap
    assert "NVDA" not in snap  # price cap on momentum
    assert "HPE" in snap and "VTRS" in snap
    assert "AAPL" not in snap  # price cap
    assert "SOFI" in snap
    assert "BRK.A" not in snap
    assert "SMCI" in snap and "HOOD" in snap
    assert "Grok (X)" in snap  # peer of Claude
    assert len(snap) <= cs.DEFAULT_DESK_SNAPSHOT_MAX_CHARS + 5

    # Grok backend should label Claude as peer.
    snap_x = cs.build_desk_snapshot_snippet(
        max_price=100.0,
        backend="cli",
        rs_path=rs_path,
        trending_path=tr_path,
        signal_state_path=sig_path,
        peer_path=peer_path,
    )
    assert "Claude (A)" in snap_x

    empty = cs.build_desk_snapshot_snippet(
        max_price=100.0,
        rs_path=tmp_path / "missing_rs.json",
        trending_path=tmp_path / "missing_tr.json",
        signal_state_path=tmp_path / "missing_sig.json",
        peer_path=tmp_path / "missing_peer.json",
    )
    assert empty == ""


def test_momentum_and_trending_candidate_rows(tmp_path):
    sig = tmp_path / "signal_state.json"
    tr = tmp_path / "tr.json"
    sig.write_text(json.dumps({
        "tickers": {
            "ACHR": {"price": 9.0, "is_hot": True, "proximity_pct": 50},
            "BIG": {"price": 250.0, "is_hot": True},
        },
    }), encoding="utf-8")
    tr.write_text(json.dumps({
        "rows": [
            {"symbol": "SOFI", "trending_score": 8.0, "price": 18.0, "is_equity": True},
            {"symbol": "BTC", "trending_score": 9.0, "price": 1.0, "is_crypto": True},
        ],
    }), encoding="utf-8")
    mom = cs.momentum_desk_candidate_rows(path=sig, max_n=10, max_price=100.0)
    assert any(r["symbol"] == "ACHR" for r in mom)
    assert all(r["symbol"] != "BIG" for r in mom)
    assert mom[0]["source"] == "momentum"
    heat = cs.trending_desk_candidate_rows(path=tr, max_n=10, max_price=100.0)
    assert any(r["symbol"] == "SOFI" for r in heat)
    assert all(r["symbol"] != "BTC" for r in heat)


def test_defaults_are_three_scheduled_runs_at_full_depth():
    """Cost is dominated by search fees, not thinking tokens, so effort buys
    cheap depth — spend is cut by running three times a day instead."""
    from config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["claude_effort"] == "xhigh"
    # 11:00 and 13:00 are both inside RTH (04:00 is pre-market prep only) —
    # two real chances a day to actually open a position, not just one.
    assert DEFAULT_CONFIG["claude_research_times"] == ["08:25", "11:00", "13:00"]
    assert DEFAULT_CONFIG["claude_research_weekdays_only"] is True


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

    def _latest_bid(self, sym):
        return 40.4

    def get_account(self):
        return {"ok": True, "equity": 50_000.0}

    def record_external_buy(self, sym, extra):
        pass


class _StubPositions:
    DEFAULT_MAX_OPEN_RISK_PCT = 5.0
    DEFAULT_DAILY_LOSS_LIMIT_R = 3.0
    DEFAULT_MAX_SPREAD_PCT = 1.0

    def __init__(self):
        self.evaluate_calls: list[str] = []
        self.events: list[dict] = []

    def log_event(self, kind, **fields):
        row = {"kind": kind, **fields}
        self.events.append(row)
        return row

    def log_entry_decision(self, symbol, decision, *, reason, **extra):
        d = decision if isinstance(decision, dict) else {}
        return self.log_event(
            "entry_decision",
            symbol=symbol,
            reason=reason,
            decision=d.get("decision"),
            wait_kind=d.get("wait_kind"),
            entry_low=d.get("entry_low"),
            entry_high=d.get("entry_high"),
            stop_price=d.get("stop_price"),
            target_1=d.get("target_1"),
            summary=d.get("summary"),
            **extra,
        )

    def pre_entry_gate(self, symbol, ask, equity, **kw):
        return True, ""

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
    monkeypatch.setitem(sys.modules, "ai_trading", trading)
    monkeypatch.setitem(sys.modules, "ai_positions", positions)
    # Legacy module names still imported in some paths
    monkeypatch.setitem(sys.modules, "claude_trading", trading)
    monkeypatch.setitem(sys.modules, "claude_positions", positions)
    # Isolate from live bot_config (e.g. ai_require_agreement=true).
    monkeypatch.setattr(cs, "_entry_runtime_cfg", lambda: {
        "ai_require_agreement": False,
        "ai_max_spread_pct": 1.0,
        "ai_max_open_risk_pct": 6.0,
        "ai_daily_loss_limit_r": 3.0,
    })
    rows = [{"symbol": "NVDA", "trending_score": 9.0, "reason": "test"}]
    _place_qualifying_entries(
        rows, max_price=None, cli_bin=None, timeout=60.0,
        risk_pct=1.0, trade_style="Moderate position", min_reward_risk=3.0,
        require_agreement=False,
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


def test_require_agreement_skips_non_ax(monkeypatch):
    trading = _StubTrading(ready=True, market_open=True)
    positions = _StubPositions()
    monkeypatch.setitem(sys.modules, "ai_trading", trading)
    monkeypatch.setitem(sys.modules, "ai_positions", positions)
    rows = [
        {"symbol": "ONLYX", "trending_score": 9.0, "agreement": False},
        {"symbol": "BOTH", "trending_score": 8.0, "agreement": True},
    ]
    _place_qualifying_entries(
        rows, max_price=None, cli_bin=None, timeout=60.0,
        risk_pct=1.0, trade_style="Moderate position", min_reward_risk=3.0,
        require_agreement=True, max_spread_pct=0,
    )
    assert positions.evaluate_calls == ["BOTH"]
    skip_reasons = [e.get("reason") for e in positions.events]
    assert "no_agreement" in skip_reasons


def test_tag_agreement_on_rows_from_wire_files(tmp_path, monkeypatch):
    from ai_suggest import tag_agreement_on_rows
    import ai_suggest as sug
    a = tmp_path / "claude_suggestions.json"
    x = tmp_path / "grok_suggestions.json"
    a.write_text(json.dumps({"rows": [{"symbol": "AAA"}, {"symbol": "BBB"}]}))
    x.write_text(json.dumps({"rows": [{"symbol": "AAA"}, {"symbol": "CCC"}]}))
    monkeypatch.setattr(sug, "CLAUDE_SUGGESTIONS_FILE", a)
    monkeypatch.setattr(sug, "GROK_SUGGESTIONS_FILE", x)
    rows = tag_agreement_on_rows([
        {"symbol": "AAA", "score": 8},
        {"symbol": "CCC", "score": 7},
    ])
    by = {r["symbol"]: r for r in rows}
    assert by["AAA"]["agreement"] is True
    assert by["CCC"]["agreement"] is False


def test_parse_ranked_prose_fallback():
    from ai_suggest import parse_model_text
    text = """
## Final ranking
1. **MU** (highest risk-adj): Extreme undervaluation to structural HBM growth.
2. **VST**: Contracted AI nuclear cash flows.
3. **AVGO**: Diversified AI silicon growth.
"""
    rows = parse_model_text(text)
    assert [r["symbol"] for r in rows] == ["MU", "VST", "AVGO"]


def test_cli_resolver_and_default_backend():
    from ai_suggest import (
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


def test_claude_output_looks_logged_out():
    from ai_suggest import claude_output_looks_logged_out
    assert claude_output_looks_logged_out("Not logged in · Please run /login")
    assert claude_output_looks_logged_out("Please run /login")
    assert not claude_output_looks_logged_out('{"suggestions":[{"symbol":"SMCI"}]}')
    assert not claude_output_looks_logged_out("")


def test_claude_auth_status_parses_json(monkeypatch):
    from ai_suggest import claude_auth_status
    import ai_suggest as m

    monkeypatch.setattr(m, "resolve_claude_cli", lambda bin=None: "/fake/claude")
    monkeypatch.setattr(m, "claude_has_api_key", lambda: False)

    class _P:
        returncode = 0
        stdout = '{"loggedIn": false, "authMethod": "none"}'
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _P())
    st = claude_auth_status()
    assert st["logged_in"] is False
    assert "login" in (st.get("error") or "").lower()

    class _P2:
        returncode = 0
        stdout = '{"loggedIn": true, "email": "a@b.com", "authMethod": "claude.ai"}'
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _P2())
    st2 = claude_auth_status()
    assert st2["logged_in"] is True
    assert st2.get("email") == "a@b.com"


def test_call_claude_cli_raises_on_not_logged_in(monkeypatch):
    from ai_suggest import call_claude_cli
    import ai_suggest as m
    import pytest

    monkeypatch.setattr(m, "resolve_claude_cli", lambda bin=None: "/fake/claude")

    class _P:
        returncode = 0
        stdout = json.dumps({"result": "Not logged in · Please run /login", "usage": {}})
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: _P())
    with pytest.raises(RuntimeError, match="not logged in"):
        call_claude_cli("test prompt", timeout=30)


def test_parse_claude_rich_suggestion_shape():
    from ai_suggest import parse_model_text
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


def test_summarize_token_metrics_day_and_latest(tmp_path):
    from datetime import datetime
    from ai_suggest import (
        ET,
        latest_token_usage,
        load_token_metrics,
        summarize_token_metrics,
    )

    # Fixed ET day so the test is timezone-stable.
    day = "2026-08-02"
    noon_et = datetime(2026, 8, 2, 12, 0, tzinfo=ET).timestamp()
    other_day = datetime(2026, 8, 1, 12, 0, tzinfo=ET).timestamp()
    path = tmp_path / "token_metrics.jsonl"
    rows = [
        {
            "ts": other_day, "backend": "claude_cli", "phase": "research",
            "total_cost_usd": 0.5, "input_tokens": 100, "output_tokens": 50,
        },
        {
            "ts": noon_et, "backend": "claude_cli", "phase": "research",
            "model": "sonnet", "total_cost_usd": 0.4,
            "input_tokens": 200, "output_tokens": 80,
            "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5,
        },
        {
            "ts": noon_et + 60, "backend": "claude_cli", "phase": "entry",
            "model": "sonnet", "total_cost_usd": 0.2,
            "input_tokens": 50, "output_tokens": 40,
        },
        {
            "ts": noon_et + 120, "backend": "grok_cli", "phase": "research",
            "model": "grok-4.5", "total_cost_usd": 0.15,
            "input_tokens": 1000, "output_tokens": 100,
        },
    ]
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    all_rows = load_token_metrics(path)
    assert len(all_rows) == 4
    last = latest_token_usage(path)
    assert last["phase"] == "research"
    assert last["backend"] == "grok_cli"

    day_sum = summarize_token_metrics(path, day=day)
    assert day_sum["day"] == day
    assert day_sum["count"] == 3
    assert abs(day_sum["total_cost_usd"] - 0.75) < 1e-9
    assert day_sum["by_phase"]["research"]["n"] == 2
    assert day_sum["by_phase"]["entry"]["n"] == 1
    assert day_sum["by_backend"]["claude_cli"]["n"] == 2
    assert day_sum["by_backend"]["grok_cli"]["n"] == 1
    assert day_sum["last"]["backend"] == "grok_cli"

    all_sum = summarize_token_metrics(path, day="all")
    assert all_sum["count"] == 4
    assert abs(all_sum["total_cost_usd"] - 1.25) < 1e-9
