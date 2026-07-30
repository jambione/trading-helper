"""Chart feed: the run/skip gate, trend direction, and refusing partial reads."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tv-monitor"))

pytest.importorskip("Quartz", reason="macOS screen reading only")
cv2 = pytest.importorskip("cv2")
pytest.importorskip("pytesseract")

import tv_capture_mac as cap                                  # noqa: E402
import tv_chart_feed as F                                     # noqa: E402


# ── the gate ─────────────────────────────────────────────────────────────────
# The window title tracks the ACTIVE tab, so it answers "is the chart even on
# screen" for ~1ms, against ~33ms to capture and ~200ms for the tesseract axis
# pass. Reading a browser parked on another page could only ever fail.

@pytest.mark.parametrize("title", [
    "CYCU 0.7100 ▲ +162.77% Unnamed",
    "NVDA 204.12 ▼ -1.20% Unnamed",
    "A 5.00 something",
])
def test_chart_titles_accepted(title):
    assert cap.looks_like_chart(title)


@pytest.mark.parametrize("title", [
    "Stop Manifesting (Somebody Call An Ambulance) - YouTube",
    "Gmail",
    "",
    "lowercase 1.00",          # a ticker is upper case
    "TOOLONGSYM 1.00",         # 7 chars, not a ticker
    "NVDA",                    # no price
    "NVDA notaprice",
])
def test_non_chart_titles_rejected(title):
    assert not cap.looks_like_chart(title)


def test_poll_skips_immediately_when_no_chart_tab(monkeypatch):
    """No capture, no tesseract — and a reason that names the real cause."""
    import tv_signal

    monkeypatch.setattr(
        tv_signal, "find_tv_windows",
        lambda: [({"left": 0, "top": 0, "width": 900, "height": 700, "id": 1},
                  "Gmail")])

    feed = F.ChartFeed()
    called = []
    monkeypatch.setattr(feed.cap, "frame", lambda: called.append(1))
    monkeypatch.setattr(tv_signal, "locate_tv_panels",
                        lambda *a, **k: called.append(1))

    assert feed.poll() is False
    assert feed.last_error == "chart tab not in front"
    assert not called, "gated-out poll still did the expensive work"


def test_poll_reports_no_window_when_none_listed(monkeypatch):
    import tv_signal
    monkeypatch.setattr(tv_signal, "find_tv_windows", lambda: [])
    feed = F.ChartFeed()
    assert feed.poll() is False
    assert feed.last_error == "no TradingView window"


def test_tradingview_titled_window_passes_the_gate(monkeypatch):
    """A window titled with the word itself is a chart even without a price."""
    import tv_signal
    seen = []
    monkeypatch.setattr(
        tv_signal, "find_tv_windows",
        lambda: [({"left": 0, "top": 0, "width": 900, "height": 700, "id": 1},
                  "TradingView — Chart")])
    feed = F.ChartFeed()
    monkeypatch.setattr(feed.cap, "frame", lambda: seen.append(1))
    feed.poll()
    assert seen, "gate rejected a TradingView-titled window"


# ── direction ────────────────────────────────────────────────────────────────

def test_direction_none_until_history_exists():
    """None is not flat. Flat is measured; None is 'we have not watched yet'."""
    assert F.direction(None, 2.0) is None
    assert F.arrow(None) == "·"


def test_direction_bands():
    assert F.direction(5.0, 2.0) == F.UP
    assert F.direction(-5.0, 2.0) == F.DOWN
    assert F.direction(1.0, 2.0) == F.FLAT
    assert F.direction(-1.0, 2.0) == F.FLAT
    assert F.direction(2.0, 2.0) == F.FLAT      # boundary is inclusive-flat
    assert F.arrow(F.UP) == "↑"
    assert F.arrow(F.DOWN) == "↓"
    assert F.arrow(F.FLAT) == "→"


# ── partial reads must not become leg counts ─────────────────────────────────

def _primed(**vals):
    feed = F.ChartFeed()
    feed._values = vals
    feed.last_ok = 1e12          # far future so fresh() passes
    return feed


def test_proximity_refuses_when_a_panel_is_missing():
    """A panel that failed is not an indicator that is unlit. A leg count
    cannot express the difference, so it must not be produced."""
    feed = _primed(r=None, pct_w=-50.0, pct_b=-40.0, m=1.0)
    assert feed.proximity(now=1e12) is None
    assert "RSI" in (feed.last_error or "")

    feed = _primed(r=30.0, pct_w=None, pct_b=None, m=1.0)
    assert feed.proximity(now=1e12) is None
    assert "%R" in (feed.last_error or "")

    feed = _primed(r=30.0, pct_w=-50.0, pct_b=-40.0, m=None)
    assert feed.proximity(now=1e12) is None
    assert "MACD" in (feed.last_error or "")


def test_proximity_emits_the_engine_shape_when_complete():
    """buy_circle() consumes this unchanged — the keys are the contract."""
    feed = _primed(r=30.0, pct_w=-50.0, pct_b=-40.0, m=1.0)
    prox = feed.proximity(now=1e12)
    assert prox is not None
    for key in ("strategy", "bars_fetched", "cm_rsi", "cm_ok",
                "pctr", "pctr_slow", "pctr_ok", "macd_ok",
                "proximity_pct", "status"):
        assert key in prox
    assert prox["strategy"] == "three_indicator"
    assert prox["source"] == "chart"


def test_one_pct_line_is_enough_for_the_pct_leg():
    """The %R panel plots two lines and one can render short; a single
    readable line still describes the indicator."""
    feed = _primed(r=30.0, pct_w=None, pct_b=-40.0, m=1.0)
    assert feed.proximity(now=1e12) is not None


def test_stale_feed_reports_nothing():
    feed = _primed(r=30.0, pct_w=-50.0, pct_b=-40.0, m=1.0)
    feed.last_ok = 0.0
    assert feed.proximity(now=1e6) is None
    assert feed.readout(now=1e6) is None


# ── a turn counts as direction of travel ─────────────────────────────────────

def test_turning_up_lights_the_leg():
    """%R turning up off the floor is the cue the strategy is built on — it
    must not have to wait for a sustained climb to register."""
    feed = _primed(r=30.0, pct_w=-90.0, pct_b=-85.0, m=-1.0)
    feed._values["pct_w_hist"] = ([float(-90 - x) for x in range(20)]
                                  + [float(-110 + x * 2) for x in range(20)])
    d = feed.directions()
    assert d["pct_w"] == "turning_up"
    prox = feed.proximity(now=1e12)
    assert prox is not None and prox["pctr_ok"], "a turn up did not light the leg"


def test_turning_down_does_not_light_the_leg():
    feed = _primed(r=30.0, pct_w=-40.0, pct_b=-40.0, m=-1.0)
    for k in ("pct_w_hist", "pct_b_hist"):
        feed._values[k] = ([float(-80 + x * 2) for x in range(20)]
                           + [float(-40 - x) for x in range(20)])
    d = feed.directions()
    assert d["pct_w"] == "turning_down"
    prox = feed.proximity(now=1e12)
    assert prox is not None and not prox["pctr_ok"]


def test_arrows_distinguish_turns_from_trends():
    assert F.arrow("turning_up") == "↗"
    assert F.arrow("turning_down") == "↘"
    assert F.arrow(F.UP) == "↑" and F.arrow(F.DOWN) == "↓"


# ── direction of travel from %R + MACD ───────────────────────────────────────
# A leg count says whether the setup is complete; this says which way it is
# going. They are different questions and a row can be 1-of-3 and building or
# 2-of-3 and rolling over.

def test_agreement_earns_the_strong_glyphs():
    """The doubled glyphs need both agreement AND conviction."""
    p, m = F.FLAT_PCT_HIST * 2, F.FLAT_M_HIST * 2
    assert F.trend_verdict("turning_up", "turning_up", p, m) == "surging"
    assert F.trend_verdict("turning_down", "turning_down", -p, -m) == "sinking"
    assert F.trend_verdict("down", "turning_down", -p, -m) == "sinking"


def test_agreement_alone_is_not_enough_for_the_doubled_glyph():
    """Two indicators barely past flat agree on very little."""
    assert F.trend_verdict("turning_up", "up") == "rising"


def test_one_indicator_moving_is_only_rising():
    assert F.trend_verdict("up", "flat") == "rising"
    assert F.trend_verdict("flat", "down", 0.0, -F.FLAT_M_HIST * 4) == "falling"


def test_a_marginal_macd_alone_calls_nothing():
    """Down-weighting MACD means a lone barely-past-flat move no longer
    clears the threshold, while a lone %R at the same strength does. That is
    the weighting doing its job, not a gap — a decisive MACD alone still
    registers."""
    assert F.trend_verdict("flat", "down") == "mixed"          # marginal
    assert F.trend_verdict("flat", "down",
                           0.0, -F.FLAT_M_HIST * 4) == "falling"   # decisive
    assert F.trend_verdict("up", "flat") == "rising"           # %R, marginal


def test_disagreement_is_reported_as_mixed_not_averaged():
    """%R climbing while MACD rolls over is not 'flat' — it is a conflict,
    and averaging it into a direction neither indicator supports would be a
    claim the chart does not make."""
    assert F.trend_verdict("up", "down") == "mixed"
    assert F.trend_verdict("turning_up", "turning_down") == "mixed"


def test_unreadable_trend_is_unknown():
    assert F.trend_verdict(None, None) == "unknown"


def test_a_turn_outweighs_a_sustained_move():
    """By the time a climb is established the move has largely happened."""
    band = F.FLAT_PCT_HIST
    assert F.shape_score("turning_up", band, band) > F.shape_score("up", band, band)
    assert F.trend_verdict("turning_up", "flat") == "rising"


# ── magnitude ────────────────────────────────────────────────────────────────
# Direction alone treats a crawl and a run as the same thing.

def test_bigger_moves_score_higher():
    band = F.FLAT_PCT_HIST
    weak = F.shape_score("up", band * 1.0, band)
    mid = F.shape_score("up", band * 1.5, band)
    strong = F.shape_score("up", band * 4.0, band)
    assert weak < mid < strong
    assert strong == pytest.approx(2.0), "strength is capped, not unbounded"


def test_same_shapes_different_strength_give_different_verdicts():
    """Both up either way — but a drift is not a run."""
    drift = F.trend_verdict("up", "up", F.FLAT_PCT_HIST, F.FLAT_M_HIST)
    run = F.trend_verdict("up", "up", F.FLAT_PCT_HIST * 3, F.FLAT_M_HIST * 3)
    assert drift == "rising"
    assert run == "surging"


def test_strength_is_normalised_across_indicators():
    """%R and MACD are on different scales; equal strength must score equal."""
    assert (F.shape_score("up", F.FLAT_PCT_HIST * 2, F.FLAT_PCT_HIST)
            == F.shape_score("up", F.FLAT_M_HIST * 2, F.FLAT_M_HIST))


def test_a_loud_indicator_cannot_override_a_conflict():
    """A big %R move against a small MACD move is still disagreement."""
    assert F.trend_verdict("up", "down",
                           F.FLAT_PCT_HIST * 5, -F.FLAT_M_HIST) == "mixed"


def test_one_indicator_alone_cannot_reach_the_doubled_glyph():
    """⇈ means both agree. A single strong mover is only ↗."""
    assert F.trend_verdict("turning_up", "flat",
                           F.FLAT_PCT_HIST * 10, 0.0) == "rising"


# ── %R carries more weight than MACD ─────────────────────────────────────────
# %R Trend Exhaustion is what the strategy is built around — deep, turning up
# off the floor. MACD is confirmation that the turn has follow-through.

def test_pct_r_outweighs_macd():
    """Same shapes, same strength — the verdict depends on which one is loud."""
    P, M = F.FLAT_PCT_HIST, F.FLAT_M_HIST
    pct_led = F.trend_verdict("up", "up", P * 4, M * 1.0)
    macd_led = F.trend_verdict("up", "up", P * 1.0, M * 4)
    assert pct_led == "surging"
    assert macd_led == "rising", "MACD alone should not carry the doubled glyph"


def test_weights_sum_to_two_so_thresholds_keep_meaning():
    """The split re-weights without rescaling: two minimum contributions still
    total 2.0, which is what the rising/surging cut points were tuned against."""
    assert F.PCT_WEIGHT + F.MACD_WEIGHT == pytest.approx(2.0)
    assert F.PCT_WEIGHT > F.MACD_WEIGHT


def test_a_weighted_indicator_still_cannot_reach_the_doubled_glyph_alone():
    """⇈ means the chart agrees with itself. Weighting must not let %R get
    there on its own, however hard it moves."""
    assert F.trend_verdict("up", "flat", F.FLAT_PCT_HIST * 20, 0.0) == "rising"
    assert F.trend_verdict("down", "flat", -F.FLAT_PCT_HIST * 20, 0.0) == "falling"
    assert F.trend_verdict("turning_up", "flat",
                           F.FLAT_PCT_HIST * 20, 0.0) == "rising"


def test_weighting_does_not_override_a_conflict():
    """A loud %R against a quiet MACD stays a conflict, not a %R win."""
    assert F.trend_verdict("up", "down",
                           F.FLAT_PCT_HIST * 20, -F.FLAT_M_HIST) == "mixed"
