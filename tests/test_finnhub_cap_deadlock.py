"""Hitting the subscription cap must not deadlock the Finnhub thread."""
import asyncio
import threading

import finnhub_stream as fs


class _WS:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


def test_drain_at_cap_does_not_deadlock(monkeypatch):
    monkeypatch.setattr(fs, "MAX_WS_SUBSCRIPTIONS", 2)
    state = fs.FinnhubState()
    state.subscribed = {"AAA", "BBB"}          # already at the cap
    monkeypatch.setattr(fs, "FINNHUB_STATE", state)
    while not fs._pending_subs.empty():
        fs._pending_subs.get_nowait()
    fs._pending_subs.put("CCC")                # would overflow -> cap-hit WARN path

    done = threading.Event()

    def run():
        asyncio.run(fs._drain_pending(_WS()))
        done.set()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    assert done.wait(3.0), "_drain_pending deadlocked on the cap-hit log"
    assert "CCC" not in state.subscribed
    assert any("cap" in l["msg"] for l in state.log_lines)
    # And the lock is free afterwards for the main thread's request_subscribe.
    assert state.lock.acquire(timeout=1.0)
    state.lock.release()
