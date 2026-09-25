"""The dashboard's book paint must not call its own /api/state or fetch gates inline."""
import time

import ai_entry_watch as ew
import dashboard as dash


def test_bind_replaces_self_http_and_inline_fetches(monkeypatch):
    for name in ("dashboard_state", "sip_spread_pct", "open_gap_pct", "rvol_pace_sip"):
        monkeypatch.setattr(ew, name, getattr(ew, name))
    started = []
    monkeypatch.setattr(dash.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: started.append(k)})())
    monkeypatch.setattr(dash, "_SNAP_CACHE", (1.0, {"tickers": [{"ticker": "DHT"}]}))
    monkeypatch.setattr(ew, "_SIP_SPREAD_CACHE", {"DHT": (0.05, time.time())})
    monkeypatch.setattr(ew, "_GAP_CACHE", {})
    dash._bind_ai_entry_watch_in_process()
    assert ew.dashboard_state() == {"tickers": [{"ticker": "DHT"}]}   # in-process, no HTTP
    assert ew.sip_spread_pct("dht") == 0.05                           # cached value
    assert ew.open_gap_pct("DHT") is None                             # cold: unknown, no fetch
    assert started and started[0].get("name") == "ew-gate-warm"
