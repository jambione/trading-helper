"""
version.py — current build identifier, shared by the dashboard and the signal
engine so a running process can report exactly which code it is executing.

get_version() returns the short git hash, with a trailing "+" if there are
uncommitted code changes. The always-uncommitted files (signal_engine.env,
signal_state.json) are ignored so the badge isn't permanently "dirty".
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_IGNORE_DIRTY = ("signal_engine.env", "signal_state.json")


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
        porcelain = subprocess.run(
            ["git", "-C", str(_ROOT), "status", "--porcelain"],
            capture_output=True, text=True, timeout=2,
        ).stdout.splitlines()
        dirty = any(
            line and not line.strip().endswith(_IGNORE_DIRTY)
            for line in porcelain
        )
        return rev + ("+" if dirty else "")
    except Exception:
        return "unknown"


if __name__ == "__main__":
    print(get_version())
