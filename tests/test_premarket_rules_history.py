"""premarket_rules_history: detectors, fill/exit realism, costs and the evaluation order."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import premarket_rules_history as pm  # noqa: E402

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 3, 7, 30, 20, tzinfo=ET).timestamp()
M0 = pm.ceil_min(T0)


def bar(k, o, h, l, c, v=5000.0):  # noqa: E741  (k = minutes after M0)
    return (M0 + 60 * k, o, h, l, c, v)


def history(price=10.0, n=70):
    return [(M0 - 60 * (n - i), price, price + 0.02, price - 0.02, price, 3000.0) for i in range(n)]


def test_name_day_filters():
    t = lambda h, m: datetime(2026, 11, 3, h, m, tzinfo=ET).timestamp()  # noqa: E731
    rows = [(t(4, 5), "A"), (t(4, 8), "EARLY"), (t(5, 30), "B"), (t(5, 40), "B"), (t(6, 0), "CARRY"), (t(9, 30), "LATE")]
    prev = [(t(15, 0), "CARRY"), (t(15, 50), "CARRY")]
    out, drops = pm.name_days("2026-11-03", rows, prev)
    syms = [s for s, _ in out]
    assert syms == ["B"] and drops["carryover"] == 1 and drops["out_of_window"] >= 1       # A/EARLY fall before 05:00 or within 10 min of capture start


def test_r1_higher_low_then_break_and_cancel():
    L0 = 9.98
    b = history() + [bar(0, 10.0, 10.30, 9.99, 10.28), bar(1, 10.28, 10.40, 10.2, 10.38),      # run to H 10.40
                     bar(2, 10.38, 10.39, 10.05, 10.10),                                      # pullback confirmed (close <= H - 30% of leg)
                     bar(3, 10.10, 10.20, 10.02, 10.15),                                      # higher low 10.02 > L0
                     bar(4, 10.15, 10.50, 10.12, 10.45)]                                      # breaks 10.40 x 1.0005
    d = pm.detect_r1(b, T0, L0)
    assert d and abs(d["fill_raw"] - 10.40 * 1.0005) < 1e-9
    cancel = b[:-1] + [bar(3, 10.10, 10.55, 9.90, 10.5)]                                       # trigger bar also breaks L0: cancelled
    assert pm.detect_r1(cancel, T0, L0) is None


def test_r2_range_breakout_and_cancel():
    rng = [bar(k, 10.0, 10.10 + 0.002 * k, 9.99, 10.05) for k in range(10)]                    # 10-minute range, high 10.118
    b = history() + rng + [bar(10, 10.05, 10.2, 10.04, 10.19)]
    d = pm.detect_r2(b, T0)
    assert d and d["fill_raw"] >= 10.118 * 1.0005 - 1e-9
    bad = history() + rng + [bar(10, 10.05, 10.2, 9.80, 10.19)]                               # low breaks range low x 0.995 on the trigger bar
    assert pm.detect_r2(bad, T0) is None


def test_exit_time_stop_gap_and_missing_bar():
    base = history() + [bar(0, 10.0, 10.1, 9.98, 10.05)]
    fb = base[-1][0]
    ts = [bar(k, 10.05, 10.1, 10.0, 10.07) for k in range(1, 12)]
    ex = pm.simulate_exit(base + ts, len(base) - 1, 10.0)
    assert ex[2] == "time" and ex[1] == fb + 600 + 60                                         # close of the bar starting fill + 10 min
    gap = base + [bar(1, 9.40, 9.45, 9.30, 9.35)]                                             # gaps below the 5% stop: fills at the open
    ex2 = pm.simulate_exit(gap, len(base) - 1, 10.0)
    assert ex2[2] == "stop" and abs(ex2[0] - 9.40) < 1e-9
    missing = base + [bar(k, 10.05, 10.1, 10.0, 10.07) for k in range(1, 6)] + [bar(13, 10.2, 10.3, 10.1, 10.25)]
    ex3 = pm.simulate_exit(missing, len(base) - 1, 10.0)
    assert ex3[2] == "next_open" and abs(ex3[0] - 10.2) < 1e-9                                # the target bar is absent: next existing bar's OPEN


def test_cost_floor_sparse_exit_and_thin_fill():
    base = history() + [bar(0, 10.0, 10.1, 9.98, 10.05)] + [bar(k, 10.05, 10.1, 10.0, 10.07) for k in range(1, 12)]
    fb = base[len(history())][0]
    tab = {int(fb - 60): (8.0, 50)}                                                           # entry quotes fine: spread 8 bp -> half 4 -> floor 5
    net, why = pm.trade_net(base, len(history()), 10.0, tab)
    assert why == "time"
    # entry 10.0 x (1 + 5/1e4); the exit quotes are missing -> half spread max(entry 5 bp, 50 bp) = 50 bp; exit = close 10.07 x (1 - 0.005); less 3 bp
    exp = ((10.07 * (1 - 0.005)) / (10.0 * (1 + 5e-4)) - 1) * 1e4 - 3.0
    assert abs(net - exp) < 1e-6
    assert pm.trade_net(base, len(history()), 10.0, {})[1] == "no_entry_quotes"
    thin = list(base)
    thin[len(history())] = (fb, 10.0, 10.1, 9.98, 10.05, 50.0)
    assert pm.trade_net(thin, len(history()), 10.0, tab)[1] == "thin_fill"


def test_evaluation_order_underpowered_before_fail():
    import random
    rng = random.Random(1)
    few = [{"d": f"2026-10-{1 + i % 28:02d}", "sym": f"S{i % 10}", "net": rng.gauss(30, 20), "net15": 25.0, "ctrl": 0.0} for i in range(60)]
    assert pm.evaluate(few, "2026-10-15")["status"] == "UNDERPOWERED"
    many = [{"d": f"2026-{10 + i // 400:02d}-{1 + (i // 14) % 28:02d}", "sym": f"S{i % 60}", "net": rng.gauss(25, 30), "net15": 20.0, "ctrl": rng.gauss(0, 5)} for i in range(400)]
    assert pm.evaluate(many, "2026-10-15")["status"] in ("STRONG", "FAIL")
    flat = [{**t, "net": rng.gauss(-5, 30), "net15": -8.0} for t in many]
    assert pm.evaluate(flat, "2026-10-15")["status"] == "FAIL"
