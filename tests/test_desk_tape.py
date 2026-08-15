"""Packed tapes are a frozen, reusable slice of the live jsonl logs."""
from __future__ import annotations

import gzip
import json
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import desk_tape  # noqa: E402

_FIXTURE = _ROOT / "tests" / "fixtures" / "sim_2026-08-11"
_DAY = "2026-08-11"


def _unpack(dest: Path) -> None:
    for gz in _FIXTURE.glob("*.jsonl.gz"):
        with gzip.open(gz, "rb") as fi, open(dest / gz.name[:-3], "wb") as fo:
            shutil.copyfileobj(fi, fo)


def test_pack_writes_manifest_and_required_files(tmp_path, monkeypatch):
    _unpack(tmp_path)
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    result = desk_tape.pack([_DAY], dest=tmp_path / "tapes" / _DAY, report_dir=tmp_path)
    assert result["ok"] is True
    dest = Path(result["path"])
    man = json.loads((dest / "manifest.json").read_text())
    assert man["days"] == [_DAY]
    assert man["files"]["shadow.jsonl"]["rows"] > 0
    assert man["files"]["outcomes.jsonl"]["rows"] == 14
    assert (dest / "shadow.jsonl").exists()
    assert (dest / "outcomes.jsonl").exists()


def test_available_days_sees_the_fixture(tmp_path, monkeypatch):
    _unpack(tmp_path)
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    assert _DAY in desk_tape.available_days(tmp_path)


def test_list_tapes_finds_what_pack_wrote(tmp_path, monkeypatch):
    _unpack(tmp_path)
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    desk_tape.pack([_DAY], dest=tmp_path / "tapes" / _DAY, report_dir=tmp_path)
    listed = desk_tape.list_tapes(tmp_path / "tapes")
    assert len(listed) == 1
    assert listed[0]["label"] == _DAY
    assert listed[0]["files"]["outcomes.jsonl"] == 14
