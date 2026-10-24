import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import batch2 as B  # noqa: E402
import wr_trend_trade as W  # noqa: E402


def flat(lo=500, hi=960, px=50.0, vol=1000.0, high=None):
    return {m: (px, high if high is not None else px + 0.05, px - 0.05, px, vol) for m in range(lo, hi)}


def lines_all_real(fast, slow):
    W.REAL[id(fast)] = set(fast)                      # every minute is a real line-feed bar in these fixtures
    return fast, slow


def test_t2_stop_lifecycle_and_fill():
    fast, slow = lines_all_real({m: -50.0 for m in range(500, 960)}, {m: -50.0 for m in range(500, 960)})
    for m in (600, 601):
        fast[m], slow[m] = -90.0, -85.0
    for m in range(602, 640):
        fast[m] = -70.0 + 0.5 * (m - 602)
    iexf = flat(high=49.98)
    iexf[600] = (49.9, 50.10, 49.7, 49.8, 1000.0)      # stop 50.11
    iexf[601] = (49.8, 50.00, 49.6, 49.7, 1000.0)      # lower high -> stop moves to 50.01
    sipf = flat(high=49.99)
    sipf[604] = (49.95, 50.03, 49.90, 50.02, 1000.0)   # SIP high 50.03 >= 50.01 + 0.01 -> fill
    tr = B.run_rule("T2", fast, slow, sipf, iexf)
    assert tr and tr[0][0] == 604 and abs(tr[0][1] - 50.01) < 1e-9


def test_t2_touch_without_extra_cent_does_not_fill():
    fast, slow = lines_all_real({m: -50.0 for m in range(500, 960)}, {m: -50.0 for m in range(500, 960)})
    fast[600], slow[600] = -90.0, -85.0
    iexf = flat(high=49.98)
    iexf[600] = (49.9, 50.00, 49.7, 49.8, 1000.0)      # stop 50.01
    sipf = flat(high=50.01)                            # touches the stop, never stop + 1c
    assert B.run_rule("T2", fast, slow, sipf, iexf) == []


def test_t4_deceleration_signal():
    fast, slow = lines_all_real({m: -85.0 for m in range(500, 960)}, {m: -85.0 for m in range(500, 960)})
    fast.update({594: -82.0, 597: -92.0, 600: -93.0})  # s0 = -10, s1 = -1 > s0 + 5
    iexf = flat()
    for m in range(571, 600):
        iexf[m] = (50.2, 50.3, 50.1, 50.2, 1000.0)
    iexf[600] = (50.0, 50.05, 49.95, 50.0, 1000.0)     # close at the 30-minute low
    assert B.sig_T4(fast, slow, iexf, 600, {"free_from": 0}) == 601


def test_t3_blocked_while_oversold_in_last_five_minutes():
    fast, slow = lines_all_real({m: -50.0 for m in range(500, 960)}, {m: -50.0 for m in range(500, 960)})
    for m in range(590, 596):
        fast[m] = -90.0                                # episode ends 595
    fast[598] = -85.0                                  # oversold again at t-2 -> blocked
    fast[599], fast[600] = -60.0, -55.0
    iexf = flat()
    iexf[600] = (49.0, 49.1, 48.0, 49.0, 1000.0)       # a lower low
    assert B.sig_T3(fast, slow, iexf, 600, {"free_from": 0}) is None
    fast[598] = -60.0
    assert B.sig_T3(fast, slow, iexf, 600, {"free_from": 0}) == 601


def test_planted_scales_only_after_entry():
    sipf = {600: (10.0, 10.1, 9.9, 10.0, 5.0), 601: (10.0, 10.1, 9.9, 10.0, 5.0)}
    p = B.planted(sipf, 601)
    assert p[600] == sipf[600] and abs(p[601][3] - 10.01) < 1e-9 and p[601][4] == 5.0


def test_exit_scan_starts_at_the_entry_minute():
    fast, slow = lines_all_real({m: -85.0 for m in range(500, 960)}, {m: -85.0 for m in range(500, 960)})
    fast.update({594: -82.0, 597: -92.0, 600: -93.0, 598: -80.0, 601: -95.0})   # fast(601) < fast(598): exit at 601
    iexf = flat()
    for m in range(571, 600):
        iexf[m] = (50.2, 50.3, 50.1, 50.2, 1000.0)
    iexf[600] = (50.0, 50.05, 49.95, 50.0, 1000.0)
    tr = B.run_rule("T4", fast, slow, flat(), iexf)
    assert tr and tr[0][0] == 601 and tr[0][2] == 602
