"""Name-level arm gates (2026-09-23): SIP spread and the opening gap."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_entry_watch as ew  # noqa: E402
from tests.test_ai_entry_watch import _armable_rec, _last_cfg  # noqa: E402


@pytest.fixture(autouse=True)
def _clear():
    ew._SIP_SPREAD_CACHE.clear()
    ew._GAP_CACHE.clear()
    yield
    ew._SIP_SPREAD_CACHE.clear()
    ew._GAP_CACHE.clear()


def test_spread_is_the_median_of_the_window():
    quotes = [(10.00, 10.01), (10.00, 10.02), (10.00, 10.10)]   # 0.1%, 0.2%, 1.0%
    got = ew.sip_spread_pct("AAA", now=1000.0, fetch=lambda s, t: quotes)
    assert got == pytest.approx(0.2, abs=0.01)


def test_spread_is_cached_for_the_ttl():
    calls = []
    f = lambda s, t: calls.append(t) or [(10.0, 10.01)]
    ew.sip_spread_pct("AAA", now=1000.0, fetch=f)
    ew.sip_spread_pct("AAA", now=1100.0, fetch=f)
    ew.sip_spread_pct("AAA", now=1200.0, fetch=f)
    assert len(calls) == 2              # 1000 fresh, 1100 cached, 1200 expired
    assert calls[0] == 1000.0 - 16 * 60  # asks 16 minutes back


def test_spread_with_no_quotes_is_unknown():
    assert ew.sip_spread_pct("AAA", now=1.0, fetch=lambda s, t: []) is None


def test_gap_is_open_vs_prior_close():
    assert ew.open_gap_pct("AAA", now=1.0, fetch=lambda s: (9.80, 10.00)) == pytest.approx(-2.0)


def _arm(monkeypatch, rec=None, **cfg):
    return ew.should_arm_buy(rec or _armable_rec(), ask=10.0, bid=9.99, cfg=_last_cfg(**cfg))


def test_wide_spread_refuses(monkeypatch):
    monkeypatch.setattr(ew, "sip_spread_pct", lambda s, now=None: 0.35)
    assert _arm(monkeypatch, ai_watch_max_sip_spread_pct=0.20) == (False, "spread_wide")


def test_unknown_spread_refuses(monkeypatch):
    monkeypatch.setattr(ew, "sip_spread_pct", lambda s, now=None: None)
    assert _arm(monkeypatch, ai_watch_max_sip_spread_pct=0.20) == (False, "spread_unknown")


def test_tight_spread_passes_the_gate(monkeypatch):
    monkeypatch.setattr(ew, "sip_spread_pct", lambda s, now=None: 0.05)
    assert _arm(monkeypatch, ai_watch_max_sip_spread_pct=0.20)[1] not in (
        "spread_wide", "spread_unknown")


def test_gap_down_refuses(monkeypatch):
    monkeypatch.setattr(ew, "open_gap_pct", lambda s, now=None: -1.8)
    assert _arm(monkeypatch, ai_watch_gap_down_block_pct=1.0) == (False, "gapped_down")


def test_small_gap_down_passes(monkeypatch):
    monkeypatch.setattr(ew, "open_gap_pct", lambda s, now=None: -0.5)
    assert _arm(monkeypatch, ai_watch_gap_down_block_pct=1.0)[1] not in (
        "gapped_down", "gap_unknown")


def test_both_gates_off_by_default(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not be called when the gate is off")
    monkeypatch.setattr(ew, "sip_spread_pct", boom)
    monkeypatch.setattr(ew, "open_gap_pct", boom)
    assert _arm(monkeypatch)[1] not in ("spread_wide", "gapped_down")


def test_sip_gap_uses_yesterdays_close_not_todays(monkeypatch):
    """Daily bars are stamped 00:00 ET, so a 'before 09:29' query still returns
    TODAY's bar. ACMR 2026-09-23 read -7.46% (today's close) instead of -1.52%."""
    import types
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    bar = lambda ts, o, c: types.SimpleNamespace(timestamp=ts, open=o, close=c)
    yday = bar(datetime(2026, 9, 22, 0, 0, tzinfo=et), 76.0, 75.80)
    today = bar(datetime(2026, 9, 23, 0, 0, tzinfo=et), 74.65, 80.67)
    first_min = bar(datetime(2026, 9, 23, 9, 30, tzinfo=et), 74.65, 74.9)

    class _C:
        def get_stock_bars(self, req):
            day = "Day" in str(getattr(req, "timeframe", ""))
            return types.SimpleNamespace(data={"ACMR": [yday, today] if day else [first_min]})
    monkeypatch.setattr(ew, "_data_client", lambda: _C())
    t = datetime(2026, 9, 23, 11, 0, tzinfo=et).timestamp()
    op, prev = ew._gap_inputs_sip("ACMR", t)
    assert (op, prev) == (74.65, 75.80)


def test_sip_gap_not_served_before_0946(monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(ew, "_data_client", lambda: (_ for _ in ()).throw(AssertionError("no call")))
    t = datetime(2026, 9, 23, 9, 40, tzinfo=ZoneInfo("America/New_York")).timestamp()
    assert ew._gap_inputs_sip("ACMR", t) == (None, None)
