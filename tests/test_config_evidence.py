"""scripts/check_config_evidence.py: a bot_config.json commit must name what it rests on."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_config_evidence as C


def test_no_evidence_line_is_refused():
    assert C.evidence_problem("config: flip the square arm\n\nseems better")


def test_repo_path_must_exist(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "r.md").write_text("x")
    assert C.evidence_problem("x\n\nEvidence: docs/r.md", tmp_path) is None
    assert C.evidence_problem("x\n\nEvidence: docs/missing.md", tmp_path)


def test_home_path_and_operator_reason_pass():
    assert C.evidence_problem("x\n\nEvidence: ~/sweeps/slow/REPORT.md") is None
    assert C.evidence_problem("x\n\nevidence: operator: 11/10 wants earlier opens, untested") is None
    assert C.evidence_problem("x\n\nEvidence: operator: ok")           # a reason, not a rubber stamp


def test_every_config_commit_since_the_start_point_names_its_evidence():
    assert C.audit() == []
