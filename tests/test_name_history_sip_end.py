"""The SIP minute fetch must stop 16 minutes before now, same lag as daily bars."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import name_history as NH  # noqa: E402


def test_sip_end_caps_a_future_end_and_keeps_a_past_one():
    now = 1_700_000_000.0
    assert NH.sip_end_ts(now + 3600, now) == now - 16 * 60
    assert NH.sip_end_ts(now - 86400, now) == now - 86400
