"""version.dirty_paths: which working-tree changes make a build read '+'."""
from version import dirty_paths


def test_clean_tree_is_clean():
    assert dirty_paths([]) == []


def test_untracked_reports_do_not_dirty():
    # benchmarks/ output on the mini stamped every build "+" from 2026-09-14.
    assert dirty_paths([
        "?? benchmarks/open_minutes/2026-09-18.json",
        "?? benchmarks/prefer_band/summary.md",
    ]) == []


def test_untracked_python_dirties():
    assert dirty_paths(["?? tools/runway_study.py"]) == ["?? tools/runway_study.py"]


def test_tracked_modification_dirties_even_non_code():
    assert dirty_paths([" M ai_positions.py", " M docs/x.md"]) == [
        " M ai_positions.py", " M docs/x.md"]


def test_always_uncommitted_files_ignored():
    assert dirty_paths([" M signal_engine.env", " M signal_state.json"]) == []
