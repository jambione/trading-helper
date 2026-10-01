#!/usr/bin/env python3
"""Run detect-secrets against git-tracked files using .secrets.baseline.

In CI or locally:
- Returns 0 if no secrets found, or all secrets are audited in .secrets.baseline.
- Returns 0 if detect-secrets-hook only updated line numbers in .secrets.baseline (exit code 3).
- Returns 1 if NEW, un-audited secrets are found.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / ".secrets.baseline"


def main() -> int:
    if not BASELINE.exists():
        print(f"check_secrets: ERROR — {BASELINE} not found.", file=sys.stderr)
        return 1

    try:
        import detect_secrets  # noqa: F401
    except ImportError:
        print(
            "check_secrets: ERROR — detect-secrets is not installed.\n"
            "Run: pip install 'detect-secrets==1.4.0'",
            file=sys.stderr,
        )
        return 1

    out = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    files = [
        f for f in out.decode("utf-8", errors="surrogateescape").split("\0")
        if f and f != ".secrets.baseline" and (ROOT / f).is_file()
    ]
    if not files:
        print("check_secrets: OK — no files to scan.")
        return 0

    # Markdown is scanned without KeywordDetector. That detector is built for
    # code (`secret_key = "..."`) and in prose it flags any mention of a
    # credential's NAME: a brief saying keys load into `ALPACA_SECRET_KEY`
    # failed every push from 2026-09-26 on. Auditing each hit into the
    # baseline never stuck, because the next brief or handoff tripped it
    # again. Every other detector still runs on markdown, so a real key
    # pasted into a doc (AWS key, high-entropy string, private key) fails.
    md = [f for f in files if f.lower().endswith(".md")]
    code = [f for f in files if not f.lower().endswith(".md")]
    worst = 0
    # Each pass may rewrite the baseline's line numbers (exit 3); the next pass
    # would then see an unstaged baseline and fail. Restore it after each.
    original = BASELINE.read_bytes()
    for group, extra in ((code, []), (md, ["--disable-plugin", "KeywordDetector"])):
        if not group:
            continue
        cmd = [
            sys.executable,
            "-m",
            "detect_secrets.pre_commit_hook",
            "--baseline",
            ".secrets.baseline",
            *extra,
            *group,
        ]
        try:
            res = subprocess.run(cmd, cwd=ROOT)
        finally:
            BASELINE.write_bytes(original)
        # 0 = clean pass (all secrets match baseline line numbers exactly, or none found)
        # 3 = baseline file was updated to adjust line numbers of existing audited secrets
        # 1 = new un-audited secret found (or syntax/execution error)
        if res.returncode == 3:
            print("check_secrets: OK — existing audited secrets are safe (line numbers shifted).")
        elif res.returncode != 0:
            worst = res.returncode
    if worst:
        return worst
    print("check_secrets: OK — no new secrets found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
