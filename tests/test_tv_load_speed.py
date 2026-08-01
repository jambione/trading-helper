"""
test_tv_load_speed.py — the TradingView load path: quicker, and never dropping a key.

Two separate concerns, both offline:

  • mac_agent._wait_until — the poll that replaced the fixed sleeps. A load used
    to cost ~4.0s for a 4-character ticker: 2.9s of explicit sleeps, 0.9s of
    pyautogui's hidden global PAUSE (0.1s after each of nine public calls), plus
    AppleScript round-trips — and up to 1.5s more whenever the verify loop had
    to retry. It is now ~0.8s cold, ~0.65s with the browser already on the
    TradingView tab, and ~0.25s when the chart already shows the symbol.
  • DeskHotkeys request coalescing — the reader thread used to run the load
    inline and DROP anything pressed meanwhile, so tapping 3 then 5 left you on
    3 with no sign the 5 registered.

The speed work must not weaken the safety property it sits on: workflow_add_tv
verifies the chart actually shows the requested ticker before pressing Option+W,
because otherwise a keystroke that landed in the wrong window saves the WRONG
symbol to the watchlist. Quicker must not mean looser.

Run:
    .venv/bin/python -m pytest tests/test_tv_load_speed.py -q
"""

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "momentum-monitor"))

mac_agent = pytest.importorskip("mac_agent", reason="mac_agent requires the agent deps")


# ── _wait_until: poll instead of guess ────────────────────────────────────────

def test_a_condition_already_true_costs_no_sleep_at_all():
    """The common case. A fixed sleep pays the full price every time for the
    rare case where the thing has not happened yet."""
    started = time.perf_counter()
    assert mac_agent._wait_until(lambda: True, timeout=5.0) is True
    assert time.perf_counter() - started < 0.02


def test_it_returns_as_soon_as_the_condition_flips():
    calls = {"n": 0}

    def ready():
        calls["n"] += 1
        return calls["n"] >= 3

    started = time.perf_counter()
    assert mac_agent._wait_until(ready, timeout=5.0, poll=0.01) is True
    assert calls["n"] == 3
    assert time.perf_counter() - started < 0.5


def test_it_gives_up_at_the_timeout_rather_than_hanging():
    started = time.perf_counter()
    assert mac_agent._wait_until(lambda: False, timeout=0.15, poll=0.02) is False
    elapsed = time.perf_counter() - started
    assert 0.14 <= elapsed < 0.6


def test_a_raising_predicate_is_treated_as_not_yet_rather_than_crashing():
    """AppleScript reads fail transiently — a browser mid-navigation, a window
    closing. That must not abort the load with a traceback."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("osascript died")
        return True

    assert mac_agent._wait_until(flaky, timeout=1.0, poll=0.01) is True


def test_the_timeouts_are_long_enough_to_be_real_waits():
    """Trimming these into the ground would turn 'verified' into 'assumed', and
    the verification is what stops the wrong symbol reaching the watchlist."""
    assert mac_agent.TV_LOAD_TIMEOUT >= 1.5
    assert mac_agent.TV_FOCUS_TIMEOUT >= 1.0


def test_the_hidden_pyautogui_pause_is_pinned_down():
    """0.1s x nine public calls was ~0.9s per load, invisible in the source."""
    pag = pytest.importorskip("pyautogui")
    assert pag.PAUSE <= 0.03
    assert pag.PAUSE > 0, "some yield between keystrokes is still required"


def test_typing_is_one_call_rather_than_one_per_letter():
    """write() presses each character with _pause=False internally, so it costs
    a single PAUSE instead of one per letter — and the old press-per-letter loop
    added its own 0.05s sleep on top of each."""
    pag = pytest.importorskip("pyautogui")
    assert mac_agent.TV_TYPE_INTERVAL < 0.05
    assert hasattr(pag, "write")


def test_the_tuning_knobs_are_env_overridable_floats():
    """So a slow machine can be tuned without editing the workflow."""
    import importlib
    for name in ("TV_SETTLE_SEC", "TV_TYPE_INTERVAL", "TV_POLL_SEC",
                 "TV_FOCUS_TIMEOUT", "TV_LOAD_TIMEOUT"):
        assert isinstance(getattr(mac_agent, name), float)

    os.environ["TV_SETTLE_SEC"] = "0.99"
    try:
        assert importlib.reload(mac_agent).TV_SETTLE_SEC == pytest.approx(0.99)
    finally:
        del os.environ["TV_SETTLE_SEC"]
        importlib.reload(mac_agent)


# ── DeskHotkeys: latest request wins, nothing is dropped ──────────────────────

@pytest.fixture()
def keys(monkeypatch):
    """A DeskHotkeys whose own reader/loader threads never start, so a test can
    drive _handle_key and _loader by hand.

    Suppressed by faking the platform rather than by patching threading.Thread:
    `desk_hotkeys.threading` IS the threading module, so setattr on it is global
    and would stub out the test's own threads too.
    """
    import desk_hotkeys

    monkeypatch.setattr(desk_hotkeys.sys, "platform", "linux")
    hk = desk_hotkeys.DeskHotkeys()
    assert hk._pending is None and not hk.enabled, "threads should not have started"
    hk.enabled = True
    hk.tv_ok = False                     # _load short-circuits before touching TV
    hk.update(["AAA", "BBB", "CCC"], ["XXX", "YYY"])
    return hk


def test_a_keypress_records_a_request_instead_of_loading_inline(keys):
    keys._handle_key("1")
    assert keys._pending == ("AAA", "")
    assert keys._wake.is_set()


def test_the_newest_keypress_supersedes_an_earlier_one(keys):
    """Tapping 1 then 3 must land on the third row. Queuing both would be
    slower AND finish somewhere you stopped asking for."""
    keys._handle_key("1")
    keys._handle_key("3")
    assert keys._pending == ("CCC", "")


def test_a_keypress_during_a_running_load_is_not_dropped(keys):
    """The old code returned early whenever _busy was set, so the second key
    vanished with no feedback."""
    with keys._lock:
        keys._busy = True
    keys._handle_key("2")
    assert keys._pending == ("BBB", "")


def test_stocktwits_letters_carry_their_tag_through_the_queue(keys):
    keys._handle_key("b")
    assert keys._pending == ("YYY", "ST ")


def test_space_requests_the_top_row(keys):
    keys._handle_key(" ")
    assert keys._pending == ("AAA", "")


def test_an_unmapped_key_requests_nothing(keys):
    keys._handle_key("z")
    assert keys._pending is None


def test_a_mapped_slot_with_no_symbol_reports_rather_than_queueing(keys):
    keys.update([], None)
    keys._handle_key(" ")
    assert keys._pending is None
    assert "no symbol" in keys.status()


def test_the_loader_runs_only_the_most_recent_request(keys):
    loaded: list[str] = []
    keys._load = lambda sym, tag="": loaded.append(sym)

    keys._handle_key("1")
    keys._handle_key("2")
    keys._handle_key("3")

    threading.Thread(target=keys._loader, daemon=True).start()
    deadline = time.time() + 2.0
    while time.time() < deadline and not loaded:
        time.sleep(0.01)

    assert loaded == ["CCC"], f"expected only the newest request, got {loaded}"


def test_a_load_that_raises_does_not_kill_the_loader_thread(keys):
    """The desk must keep taking keys after one bad load."""
    calls = {"n": 0}

    def boom(sym, tag=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("brave went away")

    keys._load = boom
    threading.Thread(target=keys._loader, daemon=True).start()

    keys._handle_key("1")
    deadline = time.time() + 2.0
    while time.time() < deadline and calls["n"] < 1:
        time.sleep(0.01)

    keys._handle_key("2")
    deadline = time.time() + 2.0
    while time.time() < deadline and calls["n"] < 2:
        time.sleep(0.01)

    assert calls["n"] == 2, "the loader stopped after the first failure"
    assert not keys._busy
