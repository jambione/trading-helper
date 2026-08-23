"""Nothing supervises the supervisor.

`./trading start` checks five services before deciding the stack is "fully
up", and the watchdog is not one of them — so that branch returned before
ever reaching the block that launches it. The documented recovery command
therefore could not revive a dead watchdog while the rest of the stack was
alive; it printed "Already running" and exited 0.

Found 2026-08-23 by bouncing the watchdog alone to pick up a code change:
it stopped, `./trading start` reported success, and the desk sat
unsupervised. The failure is silent by construction, which is why it is
pinned here rather than left to the next person to rediscover.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "trading"


@pytest.fixture(scope="module")
def src() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_script_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_the_fully_up_branch_starts_a_missing_watchdog(src):
    """The bug. The early return must not skip the watchdog."""
    i = src.index('echo "Already running.')
    j = src.index("return 0", i)
    branch = src[i:j]
    assert '_running "tools/watchdog.py"' in branch, (
        "the 'Already running' branch must check the watchdog before "
        "returning, or a dead watchdog can never be recovered by ./trading start")
    assert "tools/watchdog.py" in branch and "nohup" in branch


def test_the_watchdog_is_still_started_on_a_cold_start(src):
    """The original launch site must survive; this is an addition, not a move."""
    assert src.count("nohup \"$PY\" -u tools/watchdog.py") == 2


def test_a_started_watchdog_is_always_tracked_in_the_pidfile(src):
    """An untracked pid survives ./trading stop and races the next start."""
    for m in re.finditer(r'nohup "\$PY" -u tools/watchdog\.py[^\n]*\n', src):
        tail = src[m.end():m.end() + 120]
        assert 'echo "$!" >> "$PIDFILE"' in tail, (
            "every watchdog launch must append its pid to $PIDFILE")


def test_stop_still_kills_the_watchdog_first(src):
    """It restarts whatever is down, so it has to die before the things it
    would revive."""
    stop = src[src.index("cmd_stop()"):]
    kill = stop.index('pkill -TERM -f "tools/watchdog.py"')
    others = stop.index("$PIDFILE")
    assert kill < others, "the watchdog must be stopped before the services"
