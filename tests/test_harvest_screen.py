"""Stomp vs harvest split — no Alpaca."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import harvest_screen as hs  # noqa: E402


def test_bucket_and_inside_book():
    assert hs.bucket({"hold_sec": 3}) == "stomp"
    assert hs.bucket({"hold_sec": 20}) == "fast"
    assert hs.bucket({"hold_sec": 86}) == "harvest"
    assert hs.inside_book({"features": {"spread_r": 1.0}}, 0.10) == "inside"
    assert hs.inside_book({"features": {"spread_r": 0.04}}, 0.10) == "outside"
    assert hs.inside_book({}, 0.10) == "unk"


def test_summarize_stomp_zero_wins():
    rows = [
        {"hold_sec": 4, "realized_r_multiple": -0.03, "mfe_r": 0.0},
        {"hold_sec": 5, "realized_r_multiple": -0.04, "mfe_r": -0.01},
        {"hold_sec": 90, "realized_r_multiple": 0.2, "mfe_r": 0.3},
    ]
    st = hs.summarize([r for r in rows if hs.bucket(r) == "stomp"])
    assert st["n"] == 2
    assert st["win"] == 0
    h = hs.summarize([r for r in rows if hs.bucket(r) == "harvest"])
    assert h["win"] == 1.0
