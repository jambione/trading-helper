"""Buy-readiness circle: leg counting, the guards, and threshold tuning."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "momentum-monitor"))

from momentum_signal import (  # noqa: E402
    DEFAULTS,
    ChartSymbol,
    Feed,
    buy_circle,
    circle_markup,
    header_panel,
)

T0 = 1753449600.0
CFG = dict(DEFAULTS)


def _row(ticker="ZCMD", **sp):
    """A tracked row with the three-indicator payload the engine publishes."""
    base = {
        "strategy": "three_indicator",
        "bars_fetched": True,
        "cm_rsi": 12.0,
        "cm_ok": False,
        "pctr_ok": False,
        "macd_ok": False,
        "proximity_pct": 0,
        "status": "watching",
        "in_position": False,
    }
    base.update(sp)
    return {"ticker": ticker, "signal_proximity": base}


def _legs(n):
    """A row with `n` of the three legs lit, and the matching buy_pct."""
    keys = ["cm_ok", "pctr_ok", "macd_ok"]
    sp = {k: (i < n) for i, k in enumerate(keys)}
    sp["proximity_pct"] = round(n / 3 * 100)
    if n == 3:
        sp["status"] = "buy_zone"
    elif n == 2:
        sp["status"] = "aligning"
    return _row(**sp)


# ── leg count → colour ───────────────────────────────────────────────────────

def test_three_legs_is_green():
    state, detail = buy_circle(_legs(3), CFG)
    assert state == "go"
    assert detail == "3/3"


def test_two_legs_is_yellow():
    state, detail = buy_circle(_legs(2), CFG)
    assert state == "near"
    assert detail.startswith("2/3")


def test_one_and_zero_legs_are_both_red():
    assert buy_circle(_legs(1), CFG)[0] == "no"
    assert buy_circle(_legs(0), CFG)[0] == "no"


def test_detail_names_the_missing_legs():
    # cm_ok lit, the other two dark.
    assert buy_circle(_legs(1), CFG)[1] == "1/3 %R macd"
    assert buy_circle(_legs(2), CFG)[1] == "2/3 macd"


# ── guards: absence must never render as red ─────────────────────────────────

def test_alert_strategy_buy_zone_is_never_green():
    """STRATEGY_MODE=alert reuses these key names, but proximity_pct is mention
    velocity — a green circle there would have no indicator basis at all."""
    row = _row(strategy="alert", status="buy_zone", proximity_pct=100)
    assert buy_circle(row, CFG) == ("unknown", "wrong mode")


def test_legacy_momentum_shape_is_unknown():
    """The legacy branch emits no `strategy` marker and no cm_ok/pctr_ok."""
    row = {"ticker": "ZCMD",
           "signal_proximity": {"rsi": 22.0, "macd_hist": 0.01,
                                "status": "buy_zone", "proximity_pct": 100}}
    assert buy_circle(row, CFG) == ("unknown", "wrong mode")


def test_missing_and_empty_rows_are_unknown_not_red():
    assert buy_circle(None, CFG) == ("unknown", "untracked")
    assert buy_circle({}, CFG) == ("unknown", "untracked")
    assert buy_circle({"ticker": "ZCMD"}, CFG) == ("unknown", "untracked")


def test_dim_reasons_are_distinguishable():
    """Three causes, three fixes — they must not collapse into one message."""
    reasons = {
        buy_circle(None, CFG)[1],                       # no row at all
        buy_circle(_row(strategy="momentum"), CFG)[1],  # engine wrong mode
        buy_circle(_row(bars_fetched=False), CFG)[1],   # warming up
    }
    assert reasons == {"untracked", "wrong mode", "pending"}


def test_unfetched_bars_are_unknown_not_red():
    assert buy_circle(_row(bars_fetched=False), CFG) == ("unknown", "pending")
    assert buy_circle(_row(cm_rsi=None), CFG) == ("unknown", "pending")


# ── position states ──────────────────────────────────────────────────────────

def test_position_states_get_their_own_colour():
    assert buy_circle(_row(in_position=True), CFG)[0] == "hold"
    assert buy_circle(_row(in_position=True, status="exit_signal"), CFG)[0] == "exit"


def test_buy_zone_status_wins_over_pct():
    """The engine's buy_signal() also requires the legs to align within
    confirm_window, so buy_zone can lead buy_pct. Never disagree with it."""
    row = _row(cm_ok=True, pctr_ok=True, macd_ok=False,
               proximity_pct=67, status="buy_zone")
    assert buy_circle(row, CFG)[0] == "go"


# ── tunability ───────────────────────────────────────────────────────────────

def test_lowering_green_threshold_promotes_two_legs():
    cfg = {**CFG, "buy_circle_green_min": 67.0}
    row = _row(cm_ok=True, pctr_ok=True, proximity_pct=67, status="aligning")
    assert buy_circle(row, CFG)[0] == "near"
    assert buy_circle(row, cfg)[0] == "go"


def test_lowering_yellow_threshold_promotes_one_leg():
    cfg = {**CFG, "buy_circle_yellow_min": 33.0}
    assert buy_circle(_legs(1), CFG)[0] == "no"
    assert buy_circle(_legs(1), cfg)[0] == "near"


def test_zero_threshold_is_honoured_not_defaulted():
    """0.0 is falsy — it must not be swallowed back to the default."""
    cfg = {**CFG, "buy_circle_yellow_min": 0.0}
    assert buy_circle(_legs(0), cfg)[0] == "near"


# ── row lookup ───────────────────────────────────────────────────────────────

def test_row_for_matches_case_insensitively_and_misses_cleanly():
    feed = Feed(CFG)
    feed.rows = [_row("ZCMD"), _row("AAPL")]
    assert feed.row_for("zcmd")["ticker"] == "ZCMD"
    assert feed.row_for("NVDA") is None
    assert feed.row_for(None) is None
    # An untracked charted symbol must go dim, not red.
    assert buy_circle(feed.row_for("NVDA"), CFG)[0] == "unknown"


# ── rendering ────────────────────────────────────────────────────────────────

def test_markup_carries_symbol_and_detail():
    out = circle_markup("near", "2/3 macd", "ZCMD")
    assert "ZCMD" in out and "2/3 macd" in out and "yellow" in out
    # Unknown falls back to the hollow glyph and the label.
    assert "○" in circle_markup("unknown", "", None)


def test_header_renders_circle_without_it_when_absent():
    feed = Feed(CFG)
    feed.rows = [_row("ZCMD")]
    assert header_panel(feed, T0, 0.5, False, None).title is None
    assert header_panel(feed, T0, 0.5, False, "x").title == "x"


# ── symbol source ────────────────────────────────────────────────────────────

class _Hotkeys:
    def __init__(self, sym):
        self._sym = sym

    def focus_symbol(self):
        return self._sym


def test_hotkey_source_never_shells_out(monkeypatch):
    import momentum_signal as ms

    def _boom():
        raise AssertionError("must not read the TV tab in hotkey mode")

    monkeypatch.setattr(ms.desk, "tv_focus_symbol", _boom)
    cs = ChartSymbol({**CFG, "buy_circle_symbol_source": "hotkey"})
    assert cs.get(_Hotkeys("AAPL"), T0) == "AAPL"


def test_chart_source_prefers_tab_and_falls_back(monkeypatch):
    import momentum_signal as ms

    monkeypatch.setattr(ms.desk, "tv_focus_symbol", lambda: "NVDA")
    cs = ChartSymbol(CFG)
    assert cs.get(_Hotkeys("AAPL"), T0) == "NVDA"

    # AppleScript failure / non-Mac → fall back to what we last sent to TV.
    monkeypatch.setattr(ms.desk, "tv_focus_symbol", lambda: None)
    cs2 = ChartSymbol(CFG)
    assert cs2.get(_Hotkeys("AAPL"), T0) == "AAPL"


def test_chart_read_is_cached_within_ttl(monkeypatch):
    import momentum_signal as ms

    calls = []
    monkeypatch.setattr(ms.desk, "tv_focus_symbol",
                        lambda: (calls.append(1), "NVDA")[1])
    cs = ChartSymbol({**CFG, "buy_circle_chart_poll_sec": 10.0})
    cs.get(_Hotkeys(None), T0)
    cs.get(_Hotkeys(None), T0 + 1)
    assert len(calls) == 1          # osascript must not run every repaint
    cs.get(_Hotkeys(None), T0 + 11)
    assert len(calls) == 2


# ── the arrow chip ───────────────────────────────────────────────────────────

def test_arrow_is_symbol_and_glyph_only():
    """The corner carries one verdict. R / %R / MACD with their own arrows are
    in the readout strip underneath, where they can be checked."""
    from momentum_signal import trend_markup
    chip = trend_markup("surging", "QBTS")
    assert chip and "QBTS" in chip and "⇈" in chip
    assert "/3" not in chip and "rsi" not in chip


def test_arrow_glyph_per_state():
    from momentum_signal import trend_markup
    for state, glyph in (("surging", "⇈"), ("rising", "↗"), ("conflict", "⇄"),
                         ("falling", "↘"), ("sinking", "⇊")):
        assert glyph in trend_markup(state, "QBTS")


def test_no_trend_falls_back_to_the_dot():
    """The corner must never be blank: unreadable trend keeps the circle."""
    from momentum_signal import trend_markup
    assert trend_markup(None, "QBTS") is None
    assert trend_markup("unknown", "QBTS") is None


# ── the readout says where its numbers came from ─────────────────────────────
# Twice a desk silently running on the engine was debugged as though it were
# reading the screen — once chasing "pending" that came from bars, once an
# impossible MACD gap. Both sources render plausible-looking values, so a
# fallback is invisible exactly when it matters.

def test_chart_readout_is_tagged():
    from momentum_signal import chart_readout_markup
    out = chart_readout_markup(["R - 43 ↘", "% - -17 ↑", "M - 19.4 ↘"], "chart")
    assert out and "chart" in out and "R - 43 ↘" in out


def test_engine_readout_is_tagged_and_distinguishable():
    from momentum_signal import chart_readout_markup, engine_readout_markup
    row = _row(cm_rsi=22.0, pctr=-45.0, pctr_slow=-30.0, macd_ok=True)
    eng = engine_readout_markup(row)
    chart = chart_readout_markup(["R - 43 ↘"], "chart")
    assert eng and "engine" in eng
    assert "engine" not in chart and "chart" not in eng


def test_engine_readout_carries_its_own_legs():
    from momentum_signal import engine_readout_markup
    out = engine_readout_markup(_row(cm_rsi=22.0, pctr=-45.0, pctr_slow=-30.0,
                                     macd_ok=True))
    assert "22" in out and "-45" in out and "-30" in out


def test_engine_readout_absent_without_a_row():
    """No row is not the same as a row with no values — stay silent."""
    from momentum_signal import engine_readout_markup
    assert engine_readout_markup(None) is None
    assert engine_readout_markup({}) is None


def test_engine_readout_survives_missing_indicators():
    from momentum_signal import engine_readout_markup
    out = engine_readout_markup(_row(cm_rsi=None, pctr=None, pctr_slow=None))
    assert out is not None and "—" in out
