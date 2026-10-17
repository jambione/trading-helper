"""Scratch rule for the entry-cap arm. No replay files."""
import json
from pathlib import Path

from tools.studies.entry_cap_score import day_problem, is_scratch, scratch_counts

DAYS = ("2026-10-06",)


def test_scratch_is_an_exit_within_one_cent_unless_the_peak_cleared_be():
    assert is_scratch({"entry_px": 10.0, "exit_px": 10.0, "peak_px": 10.01})
    assert not is_scratch({"entry_px": 10.0, "exit_px": 10.0, "peak_px": 10.02})
    assert is_scratch({"entry_px": 10.0, "exit_px": 10.01})
    assert not is_scratch({"entry_px": 10.0, "exit_px": 10.02, "peak_px": None})
    assert not is_scratch({"entry_px": 10.0, "exit_px": 9.98})


def test_a_missing_or_empty_day_fails_and_counts_are_per_arm(tmp_path: Path):
    assert day_problem(tmp_path, "2026-10-06") == "missing"
    for arm, rows in (
        ("trail_dc", [{"entry_px": 10.0, "exit_px": 10.0}]),
        ("trail_dc_entrycap", [{"entry_px": 10.0, "exit_px": 11.0, "peak_px": 11.0}]),
    ):
        (tmp_path / f"2026-10-06_{arm}.json").write_text(json.dumps({"closed": rows}))
    assert day_problem(tmp_path, "2026-10-06") is None
    assert scratch_counts(tmp_path, DAYS) == {"trail_dc": 1, "trail_dc_entrycap": 0}
    empty = tmp_path / "empty"
    empty.mkdir()
    for arm in ("trail_dc", "trail_dc_entrycap"):
        (empty / f"2026-10-06_{arm}.json").write_text(json.dumps({"closed": []}))
    assert day_problem(empty, "2026-10-06") == "vacuous"
