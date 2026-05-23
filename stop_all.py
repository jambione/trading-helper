#!/usr/bin/env python3
"""Stop the Signal Scanner dashboard and transcriber.

Cleanly terminates all child processes started by start_all.py.
"""
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))


def kill_process(pattern: str, grace_period: int = 2) -> None:
    """Kill processes matching pattern, with SIGTERM then SIGKILL."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
        )
        pids = [p.strip() for p in result.stdout.splitlines() if p.strip()]
        if not pids:
            return

        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except ProcessLookupError:
                pass

        time.sleep(grace_period)

        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception:
        pass


def main() -> None:
    print("Stopping Signal Scanner...\n")

    kill_process("dashboard.py", grace_period=2)
    kill_process("transcribe_action.py", grace_period=1)
    kill_process("start_all.py", grace_period=1)

    print("✓ All processes stopped.")
    sys.exit(0)


if __name__ == "__main__":
    main()
