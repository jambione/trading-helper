"""2026-10-16: replay work dirs filled the mini's disk (66 GB). Stage 1 removes its work dir and refuses to start
with too little free space."""
import types

import tools.replay_session as rs


def _args(out="/tmp/x.json"):
    return types.SimpleNamespace(day="2026-10-14", universe=None, allow_held_out=False, fidelity=False, exact=False,
                                 live_book=False, warm_book=False, sha="HEAD", out=out, no_paint=False)


def test_refuses_when_disk_is_low(monkeypatch):
    import shutil
    monkeypatch.setattr(rs, "held_out_refusal", lambda *a: None)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: types.SimpleNamespace(free=5e9))
    monkeypatch.setattr(rs.subprocess, "check_output", lambda *a, **k: "abc12345")
    assert rs.export_and_reexec(_args()) == 3


def test_work_dir_removed_after_run(monkeypatch, tmp_path):
    import shutil
    import tempfile
    monkeypatch.setattr(rs, "held_out_refusal", lambda *a: None)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: types.SimpleNamespace(free=500e9))
    monkeypatch.setattr(rs.subprocess, "check_output", lambda *a, **k: "abc12345")
    made = {}
    real_mkdtemp = tempfile.mkdtemp

    def mk(prefix=""):
        made["dir"] = real_mkdtemp(prefix=prefix, dir=tmp_path)
        return made["dir"]
    monkeypatch.setattr(rs.tempfile, "mkdtemp", mk)
    monkeypatch.setattr(rs, "_run_export", lambda args, sha, work: 0)
    assert rs.export_and_reexec(_args()) == 0
    assert not (tmp_path / made["dir"]).exists()
    monkeypatch.setattr(rs, "_run_export", lambda args, sha, work: 0)
    assert rs.export_and_reexec(_args(out=None)) == 0          # no --out: kept, the result lives inside it
    assert (tmp_path / made["dir"]).exists()


def test_double_check_gates_turn_on_the_bar_feed():
    assert not rs.ob_feed_on({})
    for k in ("ai_double_check_sell_enabled", "ai_no_progress_require_double_check", "ai_trail_exit_trend_hold"):
        assert rs.ob_feed_on({k: True}), k
