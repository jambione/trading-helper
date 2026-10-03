"""Paper-vs-tape: a paper passive fill counts as live-equivalent only if the real tape printed through the limit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import tape_check as tc  # noqa: E402
import entry_arm_score as es  # noqa: E402
import exit_arm_score as xs  # noqa: E402


def test_verdicts_by_side():
    pr = [(1.0, 10.02), (2.0, 10.01)]
    assert tc.tape_verdict("buy", 10.02, pr) == "confirmed"      # printed below the buy limit
    assert tc.tape_verdict("buy", 10.01, pr) == "touch"          # only at it
    assert tc.tape_verdict("buy", 10.00, pr) == "none"
    assert tc.tape_verdict("sell", 10.01, pr) == "confirmed"     # printed above the sell limit
    assert tc.tape_verdict("sell", 10.02, pr) == "touch"
    assert tc.tape_verdict("sell", 10.03, pr) == "none"
    assert tc.tape_verdict("sell", 10.03, []) == "none"


def test_honest_price_crosses_unless_confirmed():
    assert tc.honest_price("buy", "confirmed", 10.00, (9.99, 10.03)) == 10.00
    assert tc.honest_price("buy", "touch", 10.00, (9.99, 10.03)) == 10.03     # a touch is not a fill
    assert tc.honest_price("sell", "none", 10.04, (10.00, 10.05)) == 10.00
    assert tc.honest_price("sell", "none", 10.04, None) is None


def _quote(_sym, _t):
    return (10.00, 10.06)


def test_exit_confirmed_passive_fill_keeps_its_price():
    r = {"symbol": "XYZ", "exit_test": {"arm": "mid", "limit": 10.03, "passive_qty": 100, "crossed_qty": 0,
                                        "waited_sec": 4.0}}
    v, px = xs.tape_honest_exit(r, 1000.0, 10.03, _quote, lambda s, a, b: [(1001.0, 10.04)])
    assert v == "confirmed" and px == 10.03


def test_exit_paper_only_fill_is_repriced_at_the_bid():
    r = {"symbol": "XYZ", "exit_test": {"arm": "mid", "limit": 10.03, "passive_qty": 100, "crossed_qty": 0,
                                        "waited_sec": 4.0}}
    v, px = xs.tape_honest_exit(r, 1000.0, 10.03, _quote, lambda s, a, b: [(1001.0, 10.03)])
    assert v == "touch" and px == 10.00


def test_exit_partial_blends_with_the_crossed_leg():
    r = {"symbol": "XYZ", "exit_test": {"arm": "mid", "limit": 10.03, "passive_qty": 40, "crossed_qty": 60,
                                        "crossed_px": 10.01, "waited_sec": 10.0}}
    v, px = xs.tape_honest_exit(r, 1000.0, 10.018, _quote, lambda s, a, b: [])
    assert v == "none" and abs(px - (40 * 10.00 + 60 * 10.01) / 100) < 1e-12


def test_exit_market_arm_and_missing_tape_are_untouched():
    r = {"symbol": "XYZ", "exit_test": {"arm": "market"}}
    assert xs.tape_honest_exit(r, 1000.0, 10.0, _quote, lambda s, a, b: []) == (None, 10.0)
    r = {"symbol": "XYZ", "exit_test": {"arm": "mid", "limit": 10.03, "passive_qty": 100}}
    assert xs.tape_honest_exit(r, 1000.0, 10.03, _quote, lambda s, a, b: None) == (None, 10.03)


def test_entry_paper_only_buy_is_repriced_at_the_ask():
    et = {"arm": "bid", "limit": 10.00, "passive_qty": 100, "crossed_qty": 0, "waited_sec": 3.0}
    v, px = es.tape_honest_entry(et, "XYZ", 1000.0, 10.00, _quote, lambda s, a, b: [(1001.0, 10.00)])
    assert v == "touch" and px == 10.06
    v, px = es.tape_honest_entry(et, "XYZ", 1000.0, 10.00, _quote, lambda s, a, b: [(1001.0, 9.99)])
    assert v == "confirmed" and px == 10.00


def test_entry_partial_backs_out_the_crossed_price():
    # 40 at the 10.00 limit + 60 crossed at 10.05 -> blended 10.03; tape never went below 10.00
    et = {"arm": "mid_down", "limit": 10.00, "passive_qty": 40, "crossed_qty": 60, "fill": 10.03,
          "waited_sec": 10.0}
    v, px = es.tape_honest_entry(et, "XYZ", 1000.0, 10.03, _quote, lambda s, a, b: [])
    assert v == "none" and abs(px - (40 * 10.06 + 60 * 10.05) / 100) < 1e-9


def test_entry_control_and_wait_arms_are_not_checked():
    for arm in ("ask", "wait"):
        assert es.tape_honest_entry({"arm": arm, "passive_qty": 100, "limit": 10.0}, "XYZ", 1.0, 10.0,
                                    _quote, lambda s, a, b: []) == (None, 10.0)
