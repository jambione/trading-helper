import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TOOL = Path(__file__).resolve().parents[1] / "tools" / "studies" / "day_review.py"


def _run(tmp_path, day):
    return subprocess.run([sys.executable, str(TOOL), day], cwd=tmp_path, capture_output=True,
                          text=True, env={"REPO": str(tmp_path), "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})


def test_review_writes_a_file_and_holds_settings(tmp_path):
    rep = tmp_path / "ai_reports"
    rep.mkdir()
    t = datetime(2026, 10, 8, 15, 10, tzinfo=ET).timestamp()
    rows = [{"symbol": "AAA", "entry_time": t + i * 60, "entry_price": 100.0, "exit_price": 100.0 + d,
             "stop_price": 95.0, "mfe_r": 0.02, "total_qty": 2, "realized_pl_usd": 2 * d, "hold_sec": 90,
             "close_reason": "local_trail", "duel_source": "tight"} for i, d in enumerate((-0.3, -0.2, 0.1))]
    (rep / "outcomes.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    out = _run(tmp_path, "2026-10-08")
    assert out.returncode == 0, out.stderr
    text = (rep / "day_review" / "2026-10-08.md").read_text()
    assert "| tight | 3 |" in text
    assert "No gate is due: hold settings." in text
    assert "Late session (15:00+) tight trades: n 3" in text


def test_a_due_gate_is_named_not_decided(tmp_path):
    (tmp_path / "ai_reports").mkdir()
    out = _run(tmp_path, "2026-10-15")
    assert out.returncode == 0, out.stderr
    assert "G2 (cost arms" in out.stdout and "read their pre-registered verdicts" in out.stdout
    assert "No closed round trips" in out.stdout
