"""Loader for webull-l2/l2_core.py — the directory name has a hyphen so it
can't be imported as a package; add it to sys.path once and re-export the
pieces the bridge uses. The module is pure stdlib, so this is safe to do
inside the dashboard process.
"""
import sys
from pathlib import Path

_L2_DIR = str(Path(__file__).resolve().parent.parent / "webull-l2")
if _L2_DIR not in sys.path:
    sys.path.insert(0, _L2_DIR)

from l2_core import (  # noqa: E402
    L2Book,
    LongView,
    Signal,
    SignalEngine,
    WallTracker,
    market_bias,
    playbook,
    project_price,
)

__all__ = ["L2Book", "LongView", "Signal", "SignalEngine", "WallTracker",
           "market_bias", "playbook", "project_price"]
