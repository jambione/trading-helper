"""Cross-checked IEX quote as a fresh decision price (barebones step 1).

The quote is trusted only when young AND within QUOTE_PRINT_AGREE_BP of a print
no older than QUOTE_PRINT_MAX_AGE_SEC — alone, IEX quotes on thin books ran a
p90 error of 330 bp against the SIP ask (tools/studies/freshness_study.py).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ew = pytest.importorskip("ai_entry_watch")
gt = pytest.importorskip("ai_trading")

NOW = 1_790_000_000.0


@pytest.fixture
def quote(monkeypatch):
    box = {"ask": 50.00, "age": 1.0}
    monkeypatch.setattr(gt, "_cached_quote", lambda s: (box["ask"], box["ask"] - 0.02))
    monkeypatch.setattr(gt, "cached_quote_age_sec", lambda s, now=None: box["age"])
    return box


def test_young_agreeing_quote_is_fresh(quote):
    q = ew.cross_checked_quote("AAA", (50.05, 40.0), {}, NOW)
    assert q == (50.00, "quote", 1.0)


def test_quote_far_from_the_print_is_refused(quote):
    quote["ask"] = 50.25  # 50 bp above a 50.00 print
    assert ew.cross_checked_quote("AAA", (50.00, 20.0), {}, NOW) is None


def test_old_quote_or_old_print_is_refused(quote):
    quote["age"] = ew.QUOTE_MAX_AGE_SEC + 1
    assert ew.cross_checked_quote("AAA", (50.0, 20.0), {}, NOW) is None
    quote["age"] = 1.0
    assert ew.cross_checked_quote("AAA", (50.0, ew.QUOTE_PRINT_MAX_AGE_SEC + 1), {}, NOW) is None
    assert ew.cross_checked_quote("AAA", (50.0, None), {}, NOW) is None
    assert ew.cross_checked_quote("AAA", None, {}, NOW) is None


def test_switch_off_restores_prints_only(quote):
    assert ew.cross_checked_quote("AAA", (50.0, 20.0), {"ai_watch_quote_freshness": False}, NOW) is None


def test_decision_price_prefers_a_fresh_print_then_the_quote(quote, monkeypatch):
    monkeypatch.setattr(ew, "live_print", lambda s: (50.02, 3.0))
    assert ew.decision_price("AAA", {}, NOW) == (50.02, "stream", 3.0)
    monkeypatch.setattr(ew, "live_print", lambda s: (50.02, 25.0))
    assert ew.decision_price("AAA", {}, NOW) == (50.00, "quote", 1.0)
    monkeypatch.setattr(gt, "_latest_ask", lambda s: 50.0)
    assert ew.decision_price("AAA", {"ai_watch_quote_freshness": False}, NOW)[1] == "stale_tape"


def test_gates_accept_a_quote_source():
    assert ew.price_src_fresh("stream") and ew.price_src_fresh("quote")
    assert not ew.price_src_fresh("stale_tape") and not ew.price_src_fresh("rest")
    cfg = {"ai_watch_arm_require_stream_price": True}
    assert ew.stream_price_required_block("quote", cfg) is None
    assert ew.stream_price_required_block("rest", cfg) == "stream_required"
