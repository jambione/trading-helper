"""data_drops study: which drops are data drops, and when a later signal counts as lost."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
import data_drops as dd  # noqa: E402

DAY = "2026-11-12"
T0 = dd.session_open(DAY)


def test_drop_class():
    assert dd.drop_class({"reason": "dead_unknown", "block": "stale_quote"}) == "data"
    assert dd.drop_class({"reason": "stale_tape_cap"}) == "data"
    assert dd.drop_class({"reason": "dead_unknown", "block": "exh_falling"}) == "control"
    assert dd.drop_class({"reason": "never_armable", "block": "spread_wide"}) == "control"
    assert dd.drop_class({"reason": "attempt_cap"}) is None


def test_lost_needs_a_signal_in_window_and_no_recovery():
    drop = T0 + 10 * 60 + 5                          # minute 10
    mins = {"A": [5, 30], "B": [100], "C": [20], "D": [25]}
    reseat = {"C": [T0 + 15 * 60]}                   # re-seated before its signal: not lost
    entries = {"D": [T0 + 30 * 60]}                  # desk entered it anyway: not lost
    drops = [(drop, s, "dead_unknown|stale_quote") for s in "ABCD"]
    lost = dd.lost_signals(drops, mins, reseat, entries, DAY)
    assert lost == {("A", 30): "dead_unknown|stale_quote"}     # B's signal is past the 60-min window


def test_signal_counted_once_under_first_drop():
    mins = {"A": [40]}
    drops = [(T0 + 35 * 60, "A", "second"), (T0 + 20 * 60, "A", "first")]
    assert dd.lost_signals(drops, mins, {}, {}, DAY) == {("A", 40): "first"}
