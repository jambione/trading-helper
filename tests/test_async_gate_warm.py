"""The async gate warmer fetches only inputs a gate actually read, spread first."""
import time

import ai_entry_watch as ew


def test_warmer_calls_only_inputs_that_were_read(monkeypatch):
    calls = []
    for name in ("sip_spread_pct", "open_gap_pct", "rvol_pace_sip", "day_high_iex", "vol_now_iex"):
        monkeypatch.setattr(ew, name, (lambda n: (lambda s, *a, **k: calls.append((n, s))))(name))
    monkeypatch.setattr(ew, "_ASYNC_GATES_BOUND", False)
    started = {}

    class _T:
        def __init__(self, target=None, **k):
            started["fn"] = target

        def start(self):
            pass

    monkeypatch.setattr(ew.threading, "Thread", _T)
    assert ew.bind_async_gates(idle_sec=0.0)
    # Only the spread and volume gates read; the room-below-HOD gate is off.
    ew.sip_spread_pct("AAA")
    ew.sip_spread_pct("BBB")
    ew.vol_now_iex("AAA")

    def one_pass():                       # run the warm loop exactly once
        def stop(_s):
            raise SystemExit
        monkeypatch.setattr(ew.time, "sleep", stop)
        try:
            started["fn"]()
        except SystemExit:
            pass

    one_pass()
    assert ("day_high_iex", "AAA") not in calls and all(n != "open_gap_pct" for n, _ in calls)
    assert calls[:2] == [("sip_spread_pct", "AAA"), ("sip_spread_pct", "BBB")]   # spread first
    assert ("vol_now_iex", "AAA") in calls
