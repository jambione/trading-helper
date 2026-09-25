"""Held-position REST quote refresh must not block the book thread."""
import ai_positions as cp


def test_held_quote_refresh_never_blocks_the_book(monkeypatch):
    """2026-09-25: inline REST refresh slept through 429 retries, book froze 10-20 s."""
    import threading as _th
    import time
    import ai_trading as gt
    gate = _th.Event()
    primed = []

    def slow_prime(syms):
        gate.wait(5)
        primed.append(list(syms))
        return len(syms)

    monkeypatch.setattr(gt, "prime_quotes", slow_prime)
    monkeypatch.setattr(cp, "_QUOTE_REFRESH", {"busy": False, "last": 0.0, "want": set()})
    import ai_entry_watch as ew
    monkeypatch.setattr(ew, "dashboard_state", lambda **k: {})
    t0 = time.time()
    assert cp.refresh_open_position_quotes(["ffbc"]) == 1
    assert cp.refresh_open_position_quotes(["WTTR"]) == 1      # queued, no second thread
    assert time.time() - t0 < 0.5                               # returned while the fetch hangs
    gate.set()
    for _ in range(100):
        if not cp._QUOTE_REFRESH["busy"]:
            break
        time.sleep(0.01)
    assert primed == [["FFBC"]]
    assert cp._QUOTE_REFRESH["want"] == {"WTTR"}               # rides the next pass
