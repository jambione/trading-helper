"""A catalyst screen that peeks at later news is a machine for inventing edges.

Point-in-time correctness is the only thing separating this from a
backtest that discovers the afternoon in the morning, so it is pinned
harder than anything else here.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

cs = pytest.importorskip("catalyst_screen")

T = 1_787_000_000.0


def _n(offset_sec, headline="Something happened"):
    return {"ts": T + offset_sec, "headline": headline}


def test_future_headlines_are_invisible():
    """The single most important property in this file."""
    news = [_n(+1), _n(+600), _n(+86400)]
    f = cs.features_at(news, T)
    assert f["has_news_24h"] is False
    assert f["n_news_24h"] == 0
    assert f["mins_since"] is None


def test_a_headline_at_the_exact_instant_does_not_count():
    """Strictly before. Same-second news is not information we acted on."""
    assert cs.features_at([_n(0)], T)["has_news_24h"] is False


def test_prior_headlines_are_counted_and_timed():
    news = [_n(-3600), _n(-600), _n(+60)]
    f = cs.features_at(news, T)
    assert f["n_news_24h"] == 2
    assert f["mins_since"] == pytest.approx(10.0)


def test_older_than_a_day_does_not_count_as_todays_catalyst():
    f = cs.features_at([_n(-30 * 3600)], T)
    assert f["has_news_24h"] is False
    assert f["n_news_24h"] == 0
    # ...but it is still the most recent headline, and that is knowable
    assert f["mins_since"] == pytest.approx(30 * 60.0)


def test_keyword_tags_read_only_prior_headlines():
    future_bad = [_n(+60, "Announces $50M registered direct offering")]
    assert cs.features_at(future_bad, T)["bearish"] is False
    past_bad = [_n(-60, "Announces $50M registered direct offering")]
    assert cs.features_at(past_bad, T)["bearish"] is True


def test_bearish_words_catch_the_actions_that_matter():
    for h in ("Prices $20M offering", "Announces reverse split",
              "Shares halted for volatility", "Analyst downgrades to sell",
              "Warrant exercise dilutes holders"):
        assert cs.features_at([_n(-60, h)], T)["bearish"] is True, h


def test_bullish_words_catch_theirs():
    for h in ("FDA grants clearance", "Beats Q2 estimates",
              "Wins $40M defense contract", "Phase 3 data positive"):
        assert cs.features_at([_n(-60, h)], T)["bullish"] is True, h


def test_no_news_is_a_distinct_state_not_a_zero():
    f = cs.features_at([], T)
    assert f["has_news_24h"] is False
    assert f["mins_since"] is None
    # a gate keyed on recency must not fire on "never"
    assert cs.GATES["fresh_news_60m"](f) is False
    assert cs.GATES["stale_news_4h+"](f) is False
    assert cs.GATES["no_catalyst_24h"](f) is True


def test_gates_partition_on_presence():
    with_news = cs.features_at([_n(-60)], T)
    without = cs.features_at([], T)
    assert cs.GATES["has_catalyst_24h"](with_news) is True
    assert cs.GATES["no_catalyst_24h"](with_news) is False
    assert cs.GATES["has_catalyst_24h"](without) is False
    assert cs.GATES["no_catalyst_24h"](without) is True


def test_the_baseline_gate_admits_everything():
    assert cs.GATES["all"](cs.features_at([], T)) is True
