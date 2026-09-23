"""
version.py — current build identifier, shared by the dashboard and the signal
engine so a running process can report exactly which code it is executing.

get_version() returns the short git hash, with a trailing "+" if there are
uncommitted code changes. The always-uncommitted files (signal_engine.env,
signal_state.json) are ignored so the badge isn't permanently "dirty".

"Dirty" means the running code may differ from the commit: a tracked file
is modified, or an untracked .py exists (tracked code could import it).
Other untracked files are reports and scratch — benchmarks/ output made
every build on the mini read "+" from 2026-09-14 on while its code matched
HEAD exactly, which made the stamp useless for telling deploys apart.

`python3 version.py --dirty` lists the offending paths and exits 1 when
there are any; scripts/deploy_mini.sh refuses to deploy onto such a tree.
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_IGNORE_DIRTY = ("signal_engine.env", "signal_state.json")

# Product name + semantic version, shown on the dashboard header. Separate
# from get_version()'s git-hash build badge, which tracks *code* identity for
# debugging a stale/mismatched process — this tracks the *product* milestone.
PRODUCT_NAME = "Trader Bro"
PRODUCT_VERSION = "0.8"


def dirty_paths(porcelain: list[str]) -> list[str]:
    """Lines of ``git status --porcelain`` that make the build not match HEAD."""
    out = []
    for line in porcelain:
        if not line or line.strip().endswith(_IGNORE_DIRTY):
            continue
        if line.startswith("??") and not line.rstrip().endswith(".py"):
            continue
        out.append(line)
    return out


def _porcelain() -> list[str]:
    return subprocess.run(
        ["git", "-C", str(_ROOT), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, timeout=2,
    ).stdout.splitlines()


@lru_cache(maxsize=1)
def get_version() -> str:
    """Short git hash of the running code, e.g. '4575bf6' or '4575bf6+'."""
    try:
        rev = subprocess.run(
            ["git", "-C", str(_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
        ).stdout.strip()
        if not rev:
            return "unknown"
        return rev + ("+" if dirty_paths(_porcelain()) else "")
    except Exception:
        return "unknown"


if __name__ == "__main__":
    import sys

    if "--dirty" in sys.argv[1:]:
        bad = dirty_paths(_porcelain())
        for line in bad:
            print(line)
        sys.exit(1 if bad else 0)
    print(get_version())
