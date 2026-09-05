"""Shared on-disk paths for the AI research / trading desk.

All desk state lives in ``ai_reports/``: the events and outcomes audit trail,
positions / open-bell / SOD / EOD state, token metrics, schedule state, and the
research write-ups. The old ``claude_reports/`` tree was migrated into it and
the dual-directory resolver retired with it.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

REPORT_DIR = ROOT / "ai_reports"

# Escape hatch for tests (and one-off tooling): point the whole report tree at a
# scratch directory. Six module-level paths bind off resolve_report_dir() at
# import — events, outcomes, positions/open-bell/SOD/EOD state, token metrics,
# schedule state, research write-ups — and only three of them were ever
# monkeypatched per-test, so a plain `pytest` run appended fixture rows to the
# live events.jsonl mid-session. Overriding here covers all of them at once.
REPORT_DIR_ENV = "AI_REPORT_DIR"

# Wire files (per-source idea feeds — names stay source-scoped on purpose).
# Google AGY is the G-side research slot; claude_suggestions.json is a legacy alias.
AGY_SUGGESTIONS_FILE = ROOT / "agy_suggestions.json"
CLAUDE_SUGGESTIONS_FILE = AGY_SUGGESTIONS_FILE  # legacy name
GROK_SUGGESTIONS_FILE = ROOT / "grok_suggestions.json"


def resolve_report_dir() -> Path:
    """Directory for token metrics, trades, positions state, events.

    ``AI_REPORT_DIR`` overrides it — see ``REPORT_DIR_ENV``.
    """
    override = os.environ.get(REPORT_DIR_ENV)
    return Path(override) if override else REPORT_DIR


def report_file(name: str, *, create_parent: bool = False) -> Path:
    """Path under the active report dir (creates the parent if requested)."""
    d = resolve_report_dir()
    if create_parent:
        d.mkdir(parents=True, exist_ok=True)
    return d / name


def find_report_file(name: str) -> Path | None:
    """Path for *name* if it exists, else None."""
    p = resolve_report_dir() / name
    return p if p.exists() else None
