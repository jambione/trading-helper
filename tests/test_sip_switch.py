"""SIP month plan S1: one knob picks the live data feed; defaults stay on the free plan."""
import alpaca_api as A
from alpaca.data.enums import DataFeed


def test_feed_knob_defaults_to_iex_and_flips_both_ways():
    assert A.live_data_feed({}) == DataFeed.IEX
    assert A.live_data_feed({"alpaca_bar_feed": "sip"}) == DataFeed.SIP
    assert A.live_data_feed({"alpaca_bar_feed": "IEX"}) == DataFeed.IEX
    assert A.sip_is_realtime({}) is False and A.sip_is_realtime({"ai_sip_realtime": True}) is True


def test_spread_delay_follows_the_realtime_knob(monkeypatch):
    import ai_entry_watch as W
    seen = {}

    def fetch(sym, t_end):
        seen["t_end"] = t_end
        return [(10.0, 10.02)] * 3
    W._SIP_SPREAD_CACHE.clear()
    monkeypatch.setattr(A, "sip_is_realtime", lambda cfg=None: False)
    W.sip_spread_pct("ABC", now=10_000.0, fetch=fetch, record=False)
    assert seen["t_end"] == 10_000.0 - 16 * 60
    W._SIP_SPREAD_CACHE.clear()
    monkeypatch.setattr(A, "sip_is_realtime", lambda cfg=None: True)
    W.sip_spread_pct("ABC", now=10_000.0, fetch=fetch, record=False)
    assert seen["t_end"] == 10_000.0
