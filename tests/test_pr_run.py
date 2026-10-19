"""pr_run: the bar that just closed. No market returns."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import pr_run as R  # noqa: E402


def test_the_bar_return_uses_that_bar_only():
    bars = {600: (100.0, 102.0, 99.0, 101.0), 597: (90.0, 91.0, 89.0, 90.0), 601: (50.0, 80.0, 40.0, 10.0)}
    ret1, ret3, rng = R.features(bars, 600)
    assert abs(ret1 - 0.01) < 1e-12
    assert abs(ret3 - (101.0 / 90.0 - 1.0)) < 1e-12
    assert abs(rng - 3.0 / 101.0) < 1e-12
    bare = {600: (100.0, 102.0, 99.0, 101.0)}
    assert R.features(bare, 600)[1] is None


def test_a_later_bar_is_not_read():
    bars = {600: (100.0, 101.0, 99.0, 100.5), 597: (100.0, 100.0, 100.0, 100.0)}
    first = R.features(bars, 600)
    bars[603] = (1.0, 1.0, 1.0, 1.0)
    assert R.features(bars, 600) == first


def test_a_broken_bar_is_dropped():
    assert R.features({600: (0.0, 1.0, 1.0, 1.0)}, 600) is None
    assert R.features({600: (10.0, 9.0, 11.0, 10.0)}, 600) is None
    assert R.features({}, 600) is None


def test_the_cutoff_is_the_feature_only():
    rng = np.random.default_rng(0)
    x = rng.normal(size=5000)
    got = R.cutoffs(x, x, x)
    assert got[0] is not None and got[0][0] < got[0][1]
    assert R.cutoffs(x[:10], x[:10], x[:10]) == (None, None, None)
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "pr_run.py")).read()
    body = src.split("def cutoffs(", 1)[1].split("\ndef ", 1)[0]
    assert "y" not in body.split(":", 1)[0]


def test_the_word_is_the_run_share_against_the_base():
    assert R.describe(0.08, 0.05, 30, 1000, 0.07, 0.05, 30, 1000) == "HIGHER"
    assert R.describe(0.04, 0.05, 30, 1000, 0.03, 0.05, 30, 1000) == "LOWER"
    assert R.describe(0.08, 0.05, 30, 1000, 0.04, 0.05, 30, 1000) == "NOT THE SAME"
    assert R.describe(0.08, 0.05, 29, 1000, 0.08, 0.05, 30, 1000) == "THIN"
    assert R.describe(0.08, 0.05, 30, 999, 0.08, 0.05, 30, 1000) == "THIN"
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "pr_run.py")).read()
    body = src.split("def describe(", 1)[1].split("\ndef ", 1)[0]
    assert "COST" not in body and "0.0003" not in body and "net" not in body


def test_a_name_is_not_dropped_for_the_finished_days_bar_count():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools", "studies", "pr_run.py")).read()
    body = src.split("def day_job(", 1)[1].split("\ndef ", 1)[0]
    assert "< 30" not in body


def test_the_window_and_the_run():
    assert R.HOLD == 3 and R.RUN30 == 0.0030
    assert R.SIG_START == 9 * 60 + 31 and R.SIG_END == 15 * 60 + 56
    assert R.LO == 0.20 and R.HI == 0.80
