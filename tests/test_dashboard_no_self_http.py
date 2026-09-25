"""The dashboard's book paint must not call its own /api/state or fetch gates inline."""
import time

import ai_entry_watch as ew
import dashboard as dash


def _isolate(monkeypatch, started):
    for name in ("dashboard_state", "sip_spread_pct", "open_gap_pct", "rvol_pace_sip",
                 "_ASYNC_GATES_BOUND"):
        monkeypatch.setattr(ew, name, getattr(ew, name))
    monkeypatch.setattr(ew.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: started.append(k)})())


def test_bind_replaces_self_http_and_inline_fetches(monkeypatch):
    started = []
    _isolate(monkeypatch, started)
    monkeypatch.setattr(dash, "_SNAP_CACHE", (1.0, {"tickers": [{"ticker": "DHT"}]}))
    monkeypatch.setattr(ew, "_SIP_SPREAD_CACHE", {"DHT": (0.05, time.time())})
    monkeypatch.setattr(ew, "_GAP_CACHE", {})
    dash._bind_ai_entry_watch_in_process()
    assert ew.dashboard_state() == {"tickers": [{"ticker": "DHT"}]}   # in-process, no HTTP
    assert ew.sip_spread_pct("dht") == 0.05                           # cached value
    assert ew.open_gap_pct("DHT") is None                             # cold: unknown, no fetch
    assert started and started[0].get("name") == "ew-gate-warm"


def test_async_gates_never_call_the_real_fetch_on_read(monkeypatch):
    started = []
    _isolate(monkeypatch, started)
    calls = []
    monkeypatch.setattr(ew, "sip_spread_pct", lambda s, **k: calls.append(s))
    monkeypatch.setattr(ew, "open_gap_pct", lambda s, **k: calls.append(s))
    monkeypatch.setattr(ew, "rvol_pace_sip", lambda s, **k: calls.append(s))
    now = time.time()
    yesterday = now - 86400
    monkeypatch.setattr(ew, "_SIP_SPREAD_CACHE", {"OLD": (0.1, now - 3600), "NEW": (0.07, now)})
    monkeypatch.setattr(ew, "_GAP_CACHE", {"Y": (-2.0, yesterday, "sip"), "T": (-1.5, now, "sip")})
    monkeypatch.setattr(ew, "_RVOL_PACE_CACHE", {})
    assert ew.bind_async_gates() is True
    assert ew.bind_async_gates() is False                 # idempotent: one warm thread
    assert ew.sip_spread_pct("new") == 0.07
    assert ew.sip_spread_pct("OLD") is None               # past max_age: unknown, not stale
    assert ew.open_gap_pct("Y") is None                   # yesterday's gap never serves today
    assert ew.open_gap_pct("T") == -1.5
    assert ew.rvol_pace_sip("COLD") is None
    assert calls == []                                    # reads never block on Alpaca
    assert len(started) == 1 and started[0]["daemon"] is True
