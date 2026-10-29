"""square_relax runner: the amended_harness_switch harness (late_gates flags, source-label staleness as live)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import late_gates_run as L  # noqa: E402
import square_relax_run as R  # noqa: E402


def test_harness_is_late_gates_with_source_label_staleness():
    R.use_study_globals()
    assert L.FLAGS is R.FLAGS and "--live-staleness" in R.FLAGS and "--arm-age-from-prints" not in R.FLAGS
    assert L.PIN["ai_watch_arm_age_mode"] == "src" and L.PIN["ai_watch_arm_max_age_sec"] == 0.0
    assert L.PIN["ai_watch_stale_rand_release_share"] == 0.0
    assert L.PIN["ai_watch_slope_now_gate"] is False and L.PIN["ai_watch_tape_gap_mult"] == 1.5
