"""early_path: checkpoint features from synthetic prints, later-green share, group rules, report table."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.studies import early_path as ep  # noqa: E402

T0 = 1_000_000.0
E = 100.0


def _bp(x):
    return E * (1 + x / 1e4)


def test_green_by_checkpoint_and_down_measures():
    prints = [(T0 + 1, _bp(-3)), (T0 + 8, _bp(-12)), (T0 + 25, _bp(+7)), (T0 + 40, _bp(-20))]
    f = ep.path_features(prints, E, T0)
    assert f[5]["green"] is False and abs(f[5]["px_bp"] + 3) < 1e-6
    assert f[10]["green"] is False and abs(f[10]["min_bp"] + 12) < 1e-6
    assert f[30]["green"] is True and f[60]["green"] is True
    assert ep.later_green(prints, E, T0, 10) is True and ep.later_green(prints, E, T0, 30) is False


def test_group_rules_and_table():
    def mk(path, ng, net):
        return {"day": "2026-10-20", "sym": "X", "fill_ts": T0, "entry": E, "net_bp": net, "never_green": ng,
                "prints": path, "feat": ep.path_features(path, E, T0)}
    sliders = [mk([(T0 + 2, _bp(-3)), (T0 + 12, _bp(-9)), (T0 + 20, _bp(-15)), (T0 + 90, _bp(-40))], True, -40.0) for _ in range(12)]
    risers = [mk([(T0 + 2, _bp(2)), (T0 + 6, _bp(8)), (T0 + 60, _bp(15))], False, +12.0) for _ in range(12)]
    rows = sliders + risers
    g = ep._grp(rows, 20, X=10)
    assert len(g) == 12 and all(r["never_green"] for r in g)               # only the sliders: not green and down >= 10 bp by 20 s
    assert len(ep._grp(rows, 20, X=None)) == 12                            # not green by 20 s
    text = "\n".join(ep.table(rows, "TRAIN"))
    assert "T=20s, down 10 bp | 12 |" in text and "| 0% | 100% |" in text   # no flagged trade later turns green; all flagged never-green


def test_signed_flow_tick_rule_and_three_tuples():
    down = [(T0 + 1, 100.0, 10.0), (T0 + 2, 99.9, 50.0), (T0 + 3, 99.9, 40.0), (T0 + 4, 99.8, 100.0)]
    up = [(T0 + 1, 100.0, 10.0), (T0 + 2, 100.1, 50.0), (T0 + 3, 100.1, 40.0), (T0 + 4, 100.2, 100.0)]
    assert ep.signed_flow(down) < -0.9 and ep.signed_flow(up) > 0.9
    assert ep.signed_flow([(T0, 100.0, 0.0), (T0 + 1, 100.1, 0.0)]) is None            # no volume info: no flow
    f = ep.path_features(down, E, T0)
    assert f[5]["flow"] < -0.9 and f[5]["green"] is False
    f2 = ep.path_features([(T0 + 1, 100.0), (T0 + 2, 99.9)], E, T0)                      # old 2-tuples still work
    assert f2[5]["flow"] is None


def test_flow_table_splits_sell_dominated_from_the_rest():
    def mk(path, ng, net):
        return {"day": "2026-10-20", "sym": "X", "fill_ts": T0, "entry": E, "net_bp": net, "never_green": ng, "prints": path,
                "feat": ep.path_features(path, E, T0)}
    sell = [mk([(T0 + 1, 100.0, 10.0), (T0 + 3, 99.9, 90.0), (T0 + 6, 99.8, 90.0)], True, -40.0) for _ in range(12)]
    calm = [mk([(T0 + 1, 100.0, 10.0), (T0 + 3, 100.0, 90.0), (T0 + 6, 100.05, 90.0), (T0 + 40, 100.1, 50.0)], False, 5.0) for _ in range(12)]
    text = "\n".join(ep.flow_table(sell + calm, "TRAIN"))
    assert "| 20s | 12 | 0% | 100% | -40.0 | 12 | 100% | +5.0 |" in text      # calm prints are flat at 5 s (no flow), so the first row with both groups is later


def test_strong_bar_needs_both_halves_and_every_condition():
    def mk(path, ng, net, day):
        return {"day": day, "sym": "X", "fill_ts": T0, "entry": E, "net_bp": net, "never_green": ng, "prints": path,
                "feat": ep.path_features(path, E, T0)}
    slide = [(T0 + 2, _bp(-3)), (T0 + 12, _bp(-12)), (T0 + 25, _bp(-18)), (T0 + 90, _bp(-40))]
    rise = [(T0 + 2, _bp(2)), (T0 + 6, _bp(8)), (T0 + 60, _bp(15))]
    recover = [(T0 + 2, _bp(-3)), (T0 + 12, _bp(-12)), (T0 + 25, _bp(-18)), (T0 + 60, _bp(+9))]   # a false alarm: flagged, then green
    good = [mk(slide, True, -50.0, "2026-10-20") for _ in range(40)] + [mk(rise, False, 12.0, "2026-10-20") for _ in range(60)]
    test_good = [mk(slide, True, -50.0, "2026-11-03") for _ in range(40)] + [mk(rise, False, 12.0, "2026-11-03") for _ in range(60)]
    assert ep.strong_rules(good, test_good)                                    # clean in both halves
    weak_test = [mk(recover, False, 2.0, "2026-11-03") for _ in range(30)] + [mk(slide, True, -50.0, "2026-11-03") for _ in range(10)] + \
                [mk(rise, False, 12.0, "2026-11-03") for _ in range(60)]
    assert not [r for r in ep.strong_rules(good, weak_test) if r[0] == 20 and r[1] == 10]   # 75% false alarms in TEST: not strong
    rare = [mk(slide, True, -50.0, "2026-10-20") for _ in range(5)] + [mk(rise, False, 12.0, "2026-10-20") for _ in range(95)]
    assert not ep.strong_rules(rare, rare)                                     # fires on 5% of trades and under 30 trades: not strong
