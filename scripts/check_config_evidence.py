#!/usr/bin/env python3
"""A change to config/bot_config.json must say what it rests on (operator 11/10: config flips were outrunning evidence).

Rule: a commit that touches config/bot_config.json carries a line

    Evidence: <path to a sweep REPORT.md / report.json, a harness result, or a study doc>
    Evidence: operator: <who decided and why, when there is no test>

A repo-relative path must exist. "operator:" is allowed on purpose: it does not block a decision, it makes an untested one
visible in the log. Two uses:

    scripts/check_config_evidence.py --commit-msg FILE     # git commit-msg hook (scripts/install_hooks.sh)
    scripts/check_config_evidence.py --audit               # every commit after scripts/config_evidence_since.txt
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = "config/bot_config.json"
SINCE = ROOT / "scripts" / "config_evidence_since.txt"


def evidence_problem(message: str, root: Path = ROOT) -> str | None:
    """None when the message carries acceptable evidence, else what is wrong."""
    found = re.findall(r"(?im)^\s*evidence:\s*(.+?)\s*$", message or "")
    if not found:
        return f"{CONFIG} changed but the commit message has no 'Evidence:' line"
    for ev in found:
        if ev.lower().startswith("operator:"):
            if len(ev.split(":", 1)[1].strip()) >= 8:
                return None
            continue
        path = ev.split()[0]
        if path.startswith(("~", "/")) or (root / path).exists():
            return None
    return "the 'Evidence:' line names no existing repo file, no ~/ or absolute path, and no 'operator: <reason>'"


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False).stdout


def audit() -> list[str]:
    since = SINCE.read_text().split()[0] if SINCE.exists() else ""
    if not since or not _git("rev-parse", "--verify", "--quiet", since).strip():
        return []
    bad = []
    for sha in _git("log", "--format=%H", f"{since}..HEAD", "--", CONFIG).split():
        why = evidence_problem(_git("log", "-1", "--format=%B", sha))
        if why:
            bad.append(f"{sha[:8]} {_git('log', '-1', '--format=%s', sha).strip()[:70]}: {why}")
    return bad


def main(argv: list[str]) -> int:
    if argv[:1] == ["--commit-msg"] and len(argv) == 2:
        if CONFIG not in _git("diff", "--cached", "--name-only").split():
            return 0
        why = evidence_problem(Path(argv[1]).read_text())
        if why:
            print(f"REFUSED: {why}.\nAdd e.g.  Evidence: ~/sweeps/<name>/REPORT.md   or   Evidence: operator: <reason>", file=sys.stderr)
            return 1
        return 0
    if argv[:1] == ["--audit"]:
        bad = audit()
        print("\n".join(bad) if bad else "config evidence: every config commit since the start point names its evidence")
        return 1 if bad else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
