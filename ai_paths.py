"""Shared on-disk paths for the AI research / trading desk.

Prefer ``ai_reports/`` and ``ai_trader.log``; fall back to legacy
``claude_reports/`` / ``claude.log`` for one release so a mixed
MacBook/mini deploy does not lose schedule state or metrics.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

REPORT_DIR_PRIMARY = ROOT / "ai_reports"
REPORT_DIR_LEGACY = ROOT / "claude_reports"

# Escape hatch for tests (and one-off tooling): point the whole report tree at a
# scratch directory. Six module-level paths bind off resolve_report_dir() at
# import — events, outcomes, positions/open-bell/SOD/EOD state, token metrics,
# schedule state, research write-ups — and only three of them were ever
# monkeypatched per-test, so a plain `pytest` run appended fixture rows to the
# live events.jsonl mid-session. Overriding here covers all of them at once.
REPORT_DIR_ENV = "AI_REPORT_DIR"

# Wire files (per-source idea feeds — names stay source-scoped on purpose)
CLAUDE_SUGGESTIONS_FILE = ROOT / "claude_suggestions.json"
GROK_SUGGESTIONS_FILE = ROOT / "grok_suggestions.json"


def resolve_report_dir(*, prefer_primary: bool = True) -> Path:
    """Directory for token metrics, trades, positions state, events.

    Write preference: ``ai_reports`` when it exists or when neither exists.
    If only the legacy tree has data, keep using it until an operator migrates.

    ``AI_REPORT_DIR`` overrides both — see ``REPORT_DIR_ENV``.
    """
    override = os.environ.get(REPORT_DIR_ENV)
    if override:
        return Path(override)
    if prefer_primary and REPORT_DIR_PRIMARY.exists():
        return REPORT_DIR_PRIMARY
    if REPORT_DIR_LEGACY.exists() and not REPORT_DIR_PRIMARY.exists():
        return REPORT_DIR_LEGACY
    return REPORT_DIR_PRIMARY if prefer_primary else REPORT_DIR_LEGACY


def report_file(name: str, *, create_parent: bool = False) -> Path:
    """Path under the active report dir (creates primary parent if requested)."""
    d = resolve_report_dir()
    if create_parent:
        d.mkdir(parents=True, exist_ok=True)
    return d / name


def find_report_file(name: str) -> Path | None:
    """Return first existing path for *name* in primary then legacy dir."""
    override = os.environ.get(REPORT_DIR_ENV)
    dirs = (Path(override),) if override else (REPORT_DIR_PRIMARY, REPORT_DIR_LEGACY)
    for d in dirs:
        p = d / name
        if p.exists():
            return p
    return None


def migrate_report_dir_once() -> Path:
    """Ensure primary dir exists; leave legacy tree intact as archive.

    Does not move files automatically (session-safe). Operators can
    ``mv claude_reports/* ai_reports/`` when both processes are stopped.
    """
    REPORT_DIR_PRIMARY.mkdir(parents=True, exist_ok=True)
    return REPORT_DIR_PRIMARY
