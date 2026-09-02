"""
test_alpaca_429_backoff.py — Alpaca IEX 429 / throttle helpers.

Covers:
  - Retry-After honored when present
  - exponential+jitter path when absent
  - process throttle spaces requests
  - signal_engine.fetch_bars backs off on mocked 429 then succeeds

Run:
    venv/bin/python -m pytest tests/test_alpaca_429_backoff.py -q
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alpaca_api as aa


@pytest.fixture(autouse=True)
def _reset_throttle():
    aa.reset_alpaca_throttle_for_tests()
    # Keep tests fast / deterministic — no env bleed from a live desk.
    old = aa._ALPACA_MIN_INTERVAL_S
    aa._ALPACA_MIN_INTERVAL_S = 0.05
    yield
    aa._ALPACA_MIN_INTERVAL_S = old
    aa.reset_alpaca_throttle_for_tests()


def test_parse_retry_after_seconds():
    assert aa.parse_retry_after({"Retry-After": "2"}) == 2.0
    assert aa.parse_retry_after({"retry-after": "1.5"}) == 1.5
    assert aa.parse_retry_after({}) is None
    assert aa.parse_retry_after(None) is None


def test_backoff_prefers_retry_after(monkeypatch):
    monkeypatch.setattr(aa.random, "uniform", lambda a, b: 0.0)
    wait = aa.backoff_seconds(0, base_wait=1.0, retry_after=3.0)
    assert wait == 3.0


def test_backoff_exponential_with_jitter(monkeypatch):
    # Full jitter draws uniform(0, base*2^attempt); pin the draw.
    monkeypatch.setattr(aa.random, "uniform", lambda a, b: b)
    assert aa.backoff_seconds(0, base_wait=1.0) == 1.0
    assert aa.backoff_seconds(2, base_wait=1.0) == 4.0


def test_throttle_spaces_requests():
    t0 = time.monotonic()
    aa.throttle_alpaca_request(0.05)
    aa.throttle_alpaca_request(0.05)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.04


def test_retry_with_backoff_honors_429_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(aa.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(aa.random, "uniform", lambda a, b: 0.0)
    # Disable throttle sleep so only the decorator backoff is measured.
    aa._ALPACA_MIN_INTERVAL_S = 0.0

    calls = {"n": 0}

    class _Resp:
        headers = {"Retry-After": "2"}

    @aa.retry_with_backoff(max_retries=3, base_wait=1.0)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            exc = Exception("429 Too Many Requests")
            exc.response = _Resp()  # type: ignore[attr-defined]
            raise exc
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 2
    assert sleeps and abs(sleeps[0] - 2.0) < 1e-6


def test_fetch_bars_backoff_on_429(monkeypatch):
    se = pytest.importorskip("signal_engine", reason="signal_engine requires pandas")
    sleeps = []
    monkeypatch.setattr(se.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(aa.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(aa.random, "uniform", lambda a, b: 0.0)
    aa._ALPACA_MIN_INTERVAL_S = 0.0
    se.ALPACA_BAR_MAX_RETRIES = 3

    # Build a minimal bars payload that clears MACD_SLOW+MACD_SIG+5.
    n = se.MACD_SLOW + se.MACD_SIG + 5
    bars = []
    for i in range(n):
        bars.append({
            "t": f"2026-09-02T14:{i:02d}:00Z",
            "o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05, "v": 1000,
        })

    class FakeResp:
        def __init__(self, status_code, payload=None, headers=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.headers = headers or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                err = se.requests.HTTPError(f"{self.status_code}")
                err.response = self
                raise err

        def json(self):
            return self._payload

    sequence = [
        FakeResp(429, headers={"Retry-After": "1"}),
        FakeResp(200, payload={"bars": bars}),
    ]

    def fake_get(*args, **kwargs):
        return sequence.pop(0)

    monkeypatch.setattr(se.requests, "get", fake_get)
    df = se.fetch_bars("TEST", "k", "s", count=n, lookback_days=5)
    assert df is not None
    assert len(df) >= n
    assert sleeps and sleeps[0] >= 1.0
