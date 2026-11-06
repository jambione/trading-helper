from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import disk_check as dc  # noqa: E402


def test_levels():
    assert dc.level(40) == "ok" and dc.level(20) == "WARN" and dc.level(5) == "CRITICAL"


def test_day_rows_and_brief(tmp_path, monkeypatch):
    for d, mb in (("2026-11-04", 3), ("2026-11-05", 5)):
        p = tmp_path / d; p.mkdir()
        (p / "wire.jsonl.gz").write_bytes(b"x" * mb * 1_000_000)
        (p / "prints.jsonl.gz").write_bytes(b"x" * 1_000_000)
    monkeypatch.setattr(dc, "SESS", tmp_path)
    rows = dc.day_rows(5)
    assert [r[0] for r in rows] == ["2026-11-04", "2026-11-05"] and abs(rows[1][1] - 6.0) < 0.01
    text = "\n".join(dc.brief(5))
    assert "Disk and capture size" in text and "2026-11-05" in text
