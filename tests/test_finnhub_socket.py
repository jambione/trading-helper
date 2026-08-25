"""Dashboard must not steal the engine's one Finnhub connection."""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fh = pytest.importorskip("finnhub_stream")


def test_same_key_is_a_collision(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY_ENGINE", "abc123")
    assert fh.dashboard_ws_collides_with_engine("abc123") is True


def test_distinct_keys_are_not_a_collision(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY_ENGINE", "engine-key")
    assert fh.dashboard_ws_collides_with_engine("dash-key") is False


def test_empty_dashboard_key_is_not_a_collision(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY_ENGINE", "abc123")
    assert fh.dashboard_ws_collides_with_engine("") is False
