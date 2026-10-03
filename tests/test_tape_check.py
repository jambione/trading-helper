"""Paper-vs-tape: a paper passive fill counts as live-equivalent only if real round lots printed through the limit,
totalling our size, while a live order would have rested there. Review 2026-10-03 cases included."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import tape_check as tc  # noqa: E402
import entry_arm_score as es  # noqa: E402
import exit_arm_score as xs  # noqa: E402


def test_verdicts_by_side_and_size():
    pr = [(1.0, 10.02, 300), (2.0, 10.01, 300)]
    assert tc.tape_verdict("buy", 10.02, pr, 100) == "confirmed"      # 300 printed below the buy limit
    assert tc.tape_verdict("buy", 10.01, pr, 100) == "touch"          # only at it
    assert tc.tape_verdict("buy", 10.00, pr, 100) == "none"
    assert tc.tape_verdict("sell", 10.01, pr, 100) == "confirmed"
    assert tc.tape_verdict("sell", 10.02, pr, 100) == "touch"
    assert tc.tape_verdict("sell", 10.03, [], 100) == "none"


def test_too_little_size_through_the_limit_is_only_a_touch():
    pr = [(1.0, 10.05, 5)]                       # one 5-share print cannot fill 100 shares ahead of a queue
    assert tc.tape_verdict("sell", 10.03, pr, 100) == "touch"
    assert tc.tape_verdict("sell", 10.03, pr + [(2.0, 10.04, 100)], 100) == "confirmed"


def test_odd_lots_are_excluded_at_fetch():
    assert "I" in tc.BAD_CONDITIONS


def test_honest_price_crosses_unless_confirmed():
    assert tc.honest_price("buy", "confirmed", 10.00, (9.99, 10.03)) == 10.00
    assert tc.honest_price("buy", "touch", 10.00, (9.99, 10.03)) == 10.03
    assert tc.honest_price("sell", "none", 10.04, (10.00, 10.05)) == 10.00
    assert tc.honest_price("sell", "none", 10.04, None) is None


def _quote(_sym, _t):
    return (10.00, 10.06)


class Spy:
    """prints() that records the window it was asked for."""

    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def __call__(self, sym, t0, t1):
        self.calls.append((t0, t1))
        return [r for r in self.rows if t0 <= r[0] <= t1]


def _xt(**kw):
    xt = {"arm": "mid", "limit": 10.03, "passive_qty": 100, "crossed_qty": 0, "waited_sec": 1.5}
    xt.update(kw)
    return {"symbol": "XYZ", "exit_test": xt}


def test_full_paper_fill_is_checked_over_the_whole_live_rest_not_paper_fill_time():
    spy = Spy([(1004.0, 10.05, 200)])            # the confirming print comes at 4 s; paper filled at 1.5 s
    v, px = xs.tape_honest_exit(_xt(), 1000.0, 10.03, _quote, spy)
    assert v == "confirmed" and px == 10.03
    assert spy.calls == [(1000.0, 1000.0 + xs.CROSS_SEC + xs.BOOK_TICK_SEC)]


def test_exit_paper_only_fill_is_repriced_at_the_bid():
    v, px = xs.tape_honest_exit(_xt(), 1000.0, 10.03, _quote, Spy([(1001.0, 10.03, 500)]))
    assert v == "touch" and px == 10.00


def test_exit_partial_blends_with_the_crossed_leg_and_uses_waited_sec():
    spy = Spy([])
    v, px = xs.tape_honest_exit(_xt(passive_qty=40, crossed_qty=60, crossed_px=10.01, waited_sec=11.0),
                                1000.0, 10.018, _quote, spy)
    assert v == "none" and abs(px - (40 * 10.00 + 60 * 10.01) / 100) < 1e-12
    assert spy.calls == [(1000.0, 1011.0)]


def test_superseded_and_unknown_legs_are_unchecked_not_guessed():
    assert xs.tape_honest_exit(_xt(superseded=True), 1000.0, 10.02, _quote, Spy([])) == ("unchecked", None)
    assert xs.tape_honest_exit(_xt(passive_qty=40, crossed_qty=60), 1000.0, 10.02, _quote,
                               Spy([])) == ("unchecked", None)          # crossed_px unknown
    assert xs.tape_honest_exit(_xt(), 1000.0, 10.03, _quote, lambda s, a, b: None) == ("unchecked", None)
    assert xs.tape_honest_exit(_xt(), 1000.0, 10.03, lambda s, t: None, Spy([])) == ("unchecked", None)


def test_exit_market_arm_is_not_checked():
    assert xs.tape_honest_exit({"symbol": "XYZ", "exit_test": {"arm": "market"}}, 1000.0, 10.0, _quote,
                               Spy([])) == (None, 10.0)


def _et(**kw):
    et = {"arm": "bid", "limit": 10.00, "passive_qty": 100, "crossed_qty": 0, "waited_sec": 1.5}
    et.update(kw)
    return et


def test_entry_window_starts_at_submit_and_covers_the_full_cross_time():
    spy = Spy([(1006.0, 9.99, 200)])
    v, px = es.tape_honest_entry(_et(), "XYZ", 1000.0, 10.00, _quote, spy)
    assert v == "confirmed" and px == 10.00 and spy.calls == [(1000.0, 1000.0 + es.CROSS_SEC)]


def test_entry_paper_only_buy_is_repriced_at_the_ask():
    v, px = es.tape_honest_entry(_et(), "XYZ", 1000.0, 10.00, _quote, Spy([(1001.0, 10.00, 500)]))
    assert v == "touch" and px == 10.06


def test_entry_partial_uses_recorded_leg_prices():
    v, px = es.tape_honest_entry(_et(arm="mid_down", passive_qty=40, crossed_qty=60, crossed_px=10.05,
                                     waited_sec=10.0), "XYZ", 1000.0, 10.03, _quote, Spy([]))
    assert v == "none" and abs(px - (40 * 10.06 + 60 * 10.05) / 100) < 1e-9


def test_entry_partial_backs_out_only_from_a_real_blend():
    # no crossed_px: back it out of the blended fill using the passive leg's own price
    v, px = es.tape_honest_entry(_et(passive_qty=40, crossed_qty=60, fill=10.03, passive_px=10.00,
                                     waited_sec=10.0), "XYZ", 1000.0, 10.03, _quote, Spy([]))
    assert abs(px - (40 * 10.06 + 60 * 10.05) / 100) < 1e-9
    # fill unknown (one leg's price stood in for the blend): unchecked, never a guess
    assert es.tape_honest_entry(_et(passive_qty=40, crossed_qty=60, fill=None, waited_sec=10.0), "XYZ",
                                1000.0, 10.03, _quote, Spy([])) == ("unchecked", None)


def test_entry_control_and_wait_arms_are_not_checked():
    for arm in ("ask", "wait"):
        assert es.tape_honest_entry({"arm": arm, "passive_qty": 100, "limit": 10.0}, "XYZ", 1.0, 10.0,
                                    _quote, Spy([])) == (None, 10.0)
