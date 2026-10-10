"""cf_score argument order. The weekend scripts pass days after --vars."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "tools", "studies")]

from cf_score import hoist_days  # noqa: E402


def test_days_after_vars_stay_days():
    argv = ["--dir", "/tmp/cf", "--ref", "base", "--vars", "np_lob", "both_sq", "presq_only",
            "2026-09-24", "2026-09-25"]
    out = hoist_days(argv)
    assert out[:2] == ["2026-09-24", "2026-09-25"]
    assert "2026-09-24" not in out[2:]
    assert out[out.index("--vars") + 1:] == ["np_lob", "both_sq", "presq_only"]


def test_days_already_first_are_unchanged():
    argv = ["2026-10-09", "--ref", "np_lob", "--vars", "np_lob_st"]
    assert hoist_days(argv) == argv
