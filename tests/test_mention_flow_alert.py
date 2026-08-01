"""Alert on the Mentions column crossing into "↑↑".

Two things make this more than "fire when the string is ↑↑":

  * mention_trend() calls ANY rise off a zero base "↑↑" — see
    test_zero_baseline_coming_alive_reads_as_a_strong_rise in
    test_mention_trend.py. On this feed most symbols sit at zero and take one
    mention, so an ungated alert would beep on nearly every row. Hence a level
    floor on the same half the arrow ratioed.
  * The latch therefore has to hold the *alertable* condition, not the arrow.
    Latching the raw arrow would leave a symbol that showed "↑↑" on a trickle
    unable to fire when it later actually took off.
"""
import os
import sys

from conftest import column_cells  # noqa: E402

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "momentum-monitor"))

from momentum_signal import (  # noqa: E402
    DEFAULTS,
    Feed,
    mention_flow_level,
    momentum_table,
)
from symbol_history import SymbolHistory  # noqa: E402

T0 = 1753449600.0
CFG = {**DEFAULTS, "alert_new": False, "alert_burst": False,
       "alert_buy": False, "focus_age_enabled": False,
       "setup_distance_enabled": False}


class SpyAlerter:
    """Records fires instead of beeping. No cooldown — these tests are about
    edge detection, which must not lean on the Alerter's throttle to be
    correct."""

    def __init__(self):
        self.fired: list[tuple[str, str, str]] = []

    def fire(self, kind, sym, detail=""):
        self.fired.append((kind, sym, detail))

    def kinds(self, kind="mflow"):
        return [f for f in self.fired if f[0] == kind]


def _hist(sym, values, field="mention_window"):
    h = SymbolHistory(maxlen=120)
    for i, v in enumerate(values):
        h.push(sym, T0 + i * 2.0, **{field: v})
    return h


def _row(sym="AAAA", window=9, count=47):
    return {"ticker": sym, "mention_window": window, "mention_count": count}


def _run(values, rows=None, cfg=None, hist=None):
    """Drive one detect pass over a feed holding `rows` against `values`."""
    cfg = {**CFG, **(cfg or {})}
    f, a = Feed(cfg), SpyAlerter()
    f.rows = rows if rows is not None else [_row()]
    h = hist if hist is not None else _hist("AAAA", values)
    f.detect_mention_flow(h, T0, a, cfg)
    return f, a, h


# ── the edge itself ─────────────────────────────────────────────────────────

def test_crossing_into_a_double_arrow_fires():
    _, a, _ = _run([1] * 5 + [9] * 5)
    assert [f[1] for f in a.kinds()] == ["AAAA"]


def test_a_single_arrow_does_not_fire():
    """ratio 1.6 — over rise (1.5), under strong (2.25)."""
    _, a, _ = _run([5] * 5 + [8] * 5)
    assert a.kinds() == []


def test_flat_and_falling_do_not_fire():
    for series in ([4] * 10, [10] * 5 + [1] * 5):
        _, a, _ = _run(series)
        assert a.kinds() == []


def test_below_the_sample_floor_nothing_fires():
    """Same shape, too little tape: an arrow off four points is noise."""
    _, a, _ = _run([1, 1, 9, 9])
    assert a.kinds() == []


# ── it fires once, not every poll ───────────────────────────────────────────

def test_holding_at_a_double_arrow_does_not_re_fire():
    cfg = {**CFG}
    f, a = Feed(cfg), SpyAlerter()
    f.rows = [_row()]
    h = _hist("AAAA", [1] * 5 + [9] * 5)
    for _ in range(5):
        f.detect_mention_flow(h, T0, a, cfg)
    assert len(a.kinds()) == 1


def test_dropping_out_and_rising_again_fires_again():
    cfg = {**CFG}
    f, a = Feed(cfg), SpyAlerter()
    f.rows = [_row()]
    f.detect_mention_flow(_hist("AAAA", [1] * 5 + [9] * 5), T0, a, cfg)
    f.detect_mention_flow(_hist("AAAA", [4] * 10), T0, a, cfg)   # back to →
    f.detect_mention_flow(_hist("AAAA", [1] * 5 + [9] * 5), T0, a, cfg)
    assert len(a.kinds()) == 2


def test_a_symbol_that_left_the_feed_can_fire_when_it_returns():
    """push_history prunes its samples, so a held latch would silence it."""
    cfg = {**CFG}
    f, a = Feed(cfg), SpyAlerter()
    rising = [1] * 5 + [9] * 5
    f.rows = [_row()]
    f.detect_mention_flow(_hist("AAAA", rising), T0, a, cfg)
    f.rows = []                                    # dropped off the feed
    f.detect_mention_flow(SymbolHistory(maxlen=120), T0, a, cfg)
    assert "AAAA" not in f.prev_mention_flow
    f.rows = [_row()]
    f.detect_mention_flow(_hist("AAAA", rising), T0, a, cfg)
    assert len(a.kinds()) == 2


# ── the level floor ─────────────────────────────────────────────────────────

def test_one_mention_off_a_zero_base_does_not_beep():
    """mention_trend() reads this as ↑↑ and the cell rightly shows one. It is
    still not worth a sound: on this feed it is the normal state of most rows.
    """
    _, a, _ = _run([0] * 5 + [1] * 5, rows=[_row(window=1, count=1)])
    assert a.kinds() == []


def test_the_same_zero_base_fires_once_a_crowd_actually_shows_up():
    _, a, _ = _run([0] * 5 + [6] * 5, rows=[_row(window=6)])
    assert len(a.kinds()) == 1


def test_a_trickle_that_later_takes_off_still_fires():
    """The latch bug: latching the raw arrow would mark this symbol "already
    ↑↑" on the trickle and swallow the real move that followed."""
    cfg = {**CFG}
    f, a = Feed(cfg), SpyAlerter()
    f.rows = [_row(window=1, count=1)]
    f.detect_mention_flow(_hist("AAAA", [0] * 5 + [1] * 5), T0, a, cfg)
    assert a.kinds() == []
    f.rows = [_row(window=12)]
    f.detect_mention_flow(_hist("AAAA", [0] * 5 + [12] * 5), T0, a, cfg)
    assert len(a.kinds()) == 1


def test_the_floor_is_configurable():
    series = [0] * 5 + [3] * 5
    _, quiet, _ = _run(series, cfg={"mention_flow_alert_min": 5.0})
    _, loud, _ = _run(series, cfg={"mention_flow_alert_min": 2.0})
    assert quiet.kinds() == []
    assert len(loud.kinds()) == 1


def test_a_zero_floor_alerts_on_every_double_arrow():
    _, a, _ = _run([0] * 5 + [1] * 5, rows=[_row(window=1, count=1)],
                   cfg={"mention_flow_alert_min": 0.0})
    assert len(a.kinds()) == 1


def test_level_is_the_mean_of_the_recent_half():
    assert mention_flow_level([1, 1, 1, 1, 4, 6, 8, 10]) == 7.0
    assert mention_flow_level([]) is None
    assert mention_flow_level(None) is None
    assert mention_flow_level([None, None]) is None


# ── flags ───────────────────────────────────────────────────────────────────

def test_the_alert_flag_silences_the_beep_but_keeps_the_journal_edge():
    """Same reasoning as alert_buy: journaling off the alerter's call site
    would mean a flag-off desk silently records nothing."""
    f, a, _ = _run([1] * 5 + [9] * 5, cfg={"alert_mention_flow": False})
    assert a.kinds() == []
    assert [k for k, _ in f.edges] == ["mflow"]


def test_turning_the_trend_feature_off_removes_the_alert_entirely():
    f, a, _ = _run([1] * 5 + [9] * 5, cfg={"mention_trend_enabled": False})
    assert a.kinds() == []
    assert f.edges == []


def test_no_history_is_a_no_op():
    cfg = {**CFG}
    f, a = Feed(cfg), SpyAlerter()
    f.rows = [_row()]
    f.detect_mention_flow(None, T0, a, cfg)
    assert a.kinds() == []
    assert f.edges == []


# ── the alert agrees with the screen ────────────────────────────────────────

def test_the_alert_fires_exactly_when_the_cell_shows_two_arrows():
    """The beep reads the same function the cell renders. If these ever
    disagree, one of them is lying to the desk."""
    cfg = {**CFG}
    for series, window in (([1] * 5 + [9] * 5, 9),      # ↑↑
                           ([5] * 5 + [8] * 5, 8),      # ↑
                           ([4] * 10, 4),               # →
                           ([10] * 5 + [1] * 5, 1)):    # ↓
        f, a = Feed(cfg), SpyAlerter()
        f.rows = [_row(window=window)]
        h = _hist("AAAA", series)
        f.detect_mention_flow(h, T0, a, cfg)
        cell = column_cells(
            momentum_table(f, T0, 0.5, True, cfg=cfg, history=h), "Mentions")[0]
        assert bool(a.kinds()) == ("↑↑" in cell), cell


def test_the_detail_carries_the_level_and_the_price():
    _, a, _ = _run([1] * 5 + [9] * 5,
                   rows=[{"ticker": "AAAA", "mention_window": 9,
                          "mention_count": 47, "price": 3.41,
                          "pct_change": 12.5}])
    detail = a.kinds()[0][2]
    assert "9/window" in detail
    assert "3.41" in detail and "+12.5%" in detail


# ── multiple symbols ────────────────────────────────────────────────────────

def test_each_symbol_latches_independently():
    cfg = {**CFG}
    f, a = Feed(cfg), SpyAlerter()
    f.rows = [_row("AAAA"), _row("BBBB")]
    h = SymbolHistory(maxlen=120)
    for i, v in enumerate([1] * 5 + [9] * 5):
        h.push("AAAA", T0 + i * 2.0, mention_window=v)
        h.push("BBBB", T0 + i * 2.0, mention_window=4)      # flat
    f.detect_mention_flow(h, T0, a, cfg)
    assert [x[1] for x in a.kinds()] == ["AAAA"]
