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
