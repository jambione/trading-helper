"""late_gates_run.run_seed_pass: the seeds of one calibration pass run together, stale outputs are cleared first,
and nothing launches past the stop time."""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import late_gates_run as L  # noqa: E402


def test_seeds_run_in_parallel_and_old_outputs_are_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "OUT", tmp_path)
    monkeypatch.setattr(L, "past_stop", lambda s: False)
    (tmp_path / "2026-10-20-C_1.json").write_text("{}")
    live, peak, seen = [0], [0], []
    lock = threading.Lock()

    def fake(day, var, ov, sha, **kw):
        assert not (tmp_path / f"{day}-{var}.json").exists()
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
            seen.append((var, ov["share"]))
        time.sleep(0.2)
        with lock:
            live[0] -= 1
        return f"{day} {var}: ok"

    monkeypatch.setattr(L, "run_one", fake)
    assert L.run_seed_pass("2026-10-20", [(f"C_{k}", {"share": 0.5}) for k in (1, 2, 3)], "sha", "08:45")
    assert peak[0] == 3 and sorted(seen) == [("C_1", 0.5), ("C_2", 0.5), ("C_3", 0.5)]


def test_nothing_launches_past_the_stop(monkeypatch):
    monkeypatch.setattr(L, "past_stop", lambda s: True)
    monkeypatch.setattr(L, "run_one", lambda *a, **k: (_ for _ in ()).throw(AssertionError("launched")))
    assert L.run_seed_pass("2026-10-20", [("C_1", {})], "sha", "08:45") is False
