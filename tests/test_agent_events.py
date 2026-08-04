"""Event → agent bus routing helpers."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_events import (  # noqa: E402
    ALL_EVENTS,
    ax_symbols,
    build_event_payload,
    detect_ax_new,
    is_ax_row,
    load_event_modes,
    normalize_mode,
    rising_edge,
    should_auto,
    should_toast,
)


def test_normalize_mode():
    assert normalize_mode("toast") == "toast"
    assert normalize_mode("AUTO") == "auto"
    assert normalize_mode("1") == "auto"
    assert normalize_mode("off") == "off"
    assert normalize_mode("nope") == "toast"


def test_load_event_modes_defaults_and_auto_add():
    modes = load_event_modes({}, auto_add=False)
    assert modes["burst"] == "toast"
    assert modes["buy_zone"] == "toast"
    assert modes["ax"] == "toast"

    modes_auto = load_event_modes({}, auto_add=True)
    assert modes_auto["burst"] == "auto"
    assert modes_auto["buy_zone"] == "auto"
    assert modes_auto["ax"] == "toast"  # AX not forced by AUTO_ADD


def test_explicit_env_wins_over_auto_add():
    env = {
        "EVENT_BURST": "off",
        "EVENT_BUY_ZONE": "toast",
        "EVENT_AX": "auto",
        "AUTO_ADD": "1",
    }
    modes = load_event_modes(env)
    assert modes["burst"] == "off"
    assert modes["buy_zone"] == "toast"
    assert modes["ax"] == "auto"


def test_should_toast_auto():
    assert should_toast("toast") and not should_auto("toast")
    assert should_toast("auto") and should_auto("auto")
    assert not should_toast("off") and not should_auto("off")


def test_ax_row_detection():
    assert is_ax_row({"symbol": "SOFI", "agreement": True})
    assert is_ax_row({"symbol": "SMCI", "source_mark": "AX"})
    assert is_ax_row({"symbol": "X", "source": "both"})
    assert not is_ax_row({"symbol": "A", "source_mark": "A"})
    assert ax_symbols([
        {"symbol": "SOFI", "agreement": True},
        {"symbol": "HOOD", "source_mark": "X"},
        {"ticker": "SMCI", "source_mark": "AX"},
    ]) == {"SOFI", "SMCI"}


def test_detect_ax_new_after_prime():
    assert detect_ax_new({"A", "B"}, set(), primed=False) == []
    assert detect_ax_new({"A", "B"}, {"A"}, primed=True) == ["B"]
    assert detect_ax_new({"A"}, {"A", "B"}, primed=True) == []


def test_rising_edge():
    assert rising_edge(True, False) is True
    assert rising_edge(True, True) is False
    assert rising_edge(True, None) is False
    assert rising_edge(False, False) is False


def test_build_event_payload():
    p = build_event_payload(
        "buy_zone", "sofi", source="agent", meta={"price": 16.0})
    assert p["action"] == "focus"
    assert p["symbol"] == "SOFI"
    assert p["meta"]["event"] == "buy_zone"
    assert p["meta"]["price"] == 16.0

    p2 = build_event_payload("burst", "X")
    assert p2["action"] == "load_tv"


def test_all_events_listed():
    assert set(ALL_EVENTS) == {"burst", "buy_zone", "ax"}
