#!/usr/bin/env python3
"""Fail if account or login files are tracked. Those must stay off GitHub."""

from __future__ import annotations

import subprocess
import sys

BLOCKED = (
    "users.json",
    "login_log.json",
    "traffic_log.json",
    "config/push_subscriptions.json",
)


def tracked() -> set[str]:
    out = subprocess.check_output(
        ["git", "ls-files", "-z"],
        stderr=subprocess.DEVNULL,
    )
    return {p.decode() for p in out.split(b"\0") if p}


def main() -> int:
    found = sorted(tracked() & set(BLOCKED))
    if not found:
        print("check_no_user_data: OK — no account/login files tracked.")
        return 0
    print("check_no_user_data: ERROR — user data is tracked and would go to GitHub:",
          file=sys.stderr)
    for path in found:
        print(f"  {path}", file=sys.stderr)
    print("\n  Fix: git rm --cached -- " + " ".join(found), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
