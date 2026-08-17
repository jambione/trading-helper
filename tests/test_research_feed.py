"""Delayed SIP is research-only; live helpers stay IEX."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import alpaca_api


def test_normalize_research_feed():
    assert alpaca_api.normalize_research_feed("IEX") == "iex"
    assert alpaca_api.normalize_research_feed("sip") == "sip"
    assert alpaca_api.normalize_research_feed("delayed-sip") == "sip"
    assert alpaca_api.normalize_research_feed(None) == "iex"
    with pytest.raises(ValueError):
        alpaca_api.normalize_research_feed("otc")


def test_research_bar_end_sip_lags_fifteen_minutes():
    now = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
    end = alpaca_api.research_bar_end("sip", now=now)
    assert end == now - timedelta(minutes=15)


def test_research_bar_end_iex_is_now():
    now = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
    assert alpaca_api.research_bar_end("iex", now=now) == now


def test_research_bar_end_keeps_older_requested_end():
    now = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
    day_end = datetime(2026, 8, 13, 20, 0, tzinfo=timezone.utc)
    assert alpaca_api.research_bar_end("sip", now=now, requested_end=day_end) == day_end


def test_research_bar_end_clamps_today_sip():
    now = datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
    later = datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)
    assert alpaca_api.research_bar_end("sip", now=now, requested_end=later) == (
        now - timedelta(minutes=15)
    )


def test_live_feed_arg_stays_iex():
    arg = alpaca_api._get_feed_arg({})
    if not arg:
        pytest.skip("alpaca SDK DataFeed not installed")
    assert str(arg["feed"]).upper().endswith("IEX")
