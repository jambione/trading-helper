"""flow_exit study: tick-rule flow and the rule's delta."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "studies"))
import flow_exit as fe  # noqa: E402


def test_signed_flow_tick_rule_and_window():
    ticks = [(99.0, 10.0, 100), (100.5, 10.1, 50), (101, 10.1, 50), (102, 10.0, 200), (200, 9.0, 999)]
    fl, last = fe.signed_flow(ticks, 100.0, 60)
    # 100.5 uptick buy 50, 101 zero tick keeps buy 50, 102 downtick sell 200; 200 s is outside the window
    assert last == 10.0
    assert abs(fl - (100 - 200) / 300) < 1e-9
    assert fe.signed_flow([(50.0, 10.0, 5)], 100.0, 60) == (None, None)


def test_rule_delta_acts_only_when_open_below_and_weak():
    tr = {"t_in": 0.0, "t_out": 300.0, "px_in": 10.0, "ret_bp": -60.0}
    assert fe.rule_delta(tr, -0.5, 9.99, 60, -0.4) == (9.99 / 10 - 1) * 1e4 - fe.EXIT_COST + 60.0
    assert fe.rule_delta(tr, -0.1, 9.99, 60, -0.4) == 0.0                  # flow not weak enough
    assert fe.rule_delta(tr, -0.5, 10.01, 60, -0.4) == 0.0                 # above entry
    assert fe.rule_delta({**tr, "t_out": 30.0}, -0.5, 9.99, 60, -0.4) == 0.0   # already exited
    assert fe.rule_delta(tr, None, 9.99, 60, -0.4, use_flow=False) != 0.0  # plain cut ignores flow
