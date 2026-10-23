"""nightly.run: process-group timeout / memory cap (the 2026-10-22 orphaned 55 GB exact replay)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import nightly as N  # noqa: E402

# a child that starts a grandchild (like replay_session.py re-executing the booted code) and both sleep
SPAWN = ("import subprocess, sys, time; "
         "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
         "open(sys.argv[1], 'w').write(str(g.pid)); time.sleep(60)")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _grandchild(tmp: Path) -> int:
    for _ in range(50):
        if tmp.exists() and tmp.read_text():
            return int(tmp.read_text())
        time.sleep(0.1)
    raise AssertionError("grandchild pid not written")


def test_parse_mem():
    assert N.parse_mem("55G") == 55 * 1024 and N.parse_mem("1567M") == 1567 and N.parse_mem("476K") == 476 / 1024
    assert N.parse_mem("12M+") == 12 and N.parse_mem("") is None and N.parse_mem("abc") is None


def test_timeout_kills_the_grandchild(tmp_path):
    pidf, log = tmp_path / "g.pid", tmp_path / "n.log"
    rc = N.run([sys.executable, "-c", SPAWN, str(pidf)], log, timeout=1.0, mem_cap_mb=None, poll_s=0.3)
    g = _grandchild(pidf)
    time.sleep(0.5)
    assert rc == N.RC_TIMEOUT and not _alive(g) and "TIMEOUT" in log.read_text()


def test_memory_cap_kills_the_group(tmp_path):
    pidf, log = tmp_path / "g.pid", tmp_path / "n.log"
    rc = N.run([sys.executable, "-c", SPAWN, str(pidf)], log, timeout=60, mem_cap_mb=100,
               footprint=lambda pgid: 7000.0, poll_s=0.3)
    g = _grandchild(pidf)
    time.sleep(0.5)
    assert rc == N.RC_MEMCAP and not _alive(g) and "MEMCAP 7000 MB" in log.read_text()


def test_normal_exit_returns_rc_and_logs_peak(tmp_path):
    log = tmp_path / "n.log"
    rc = N.run([sys.executable, "-c", "import sys; sys.exit(3)"], log, timeout=60, footprint=lambda pgid: 12.0,
               poll_s=0.2)
    assert rc == 3 and "rc 3" in log.read_text()


def test_real_footprint_reads_this_process_group():
    assert N.group_footprint_mb(os.getpgid(os.getpid())) > 0
