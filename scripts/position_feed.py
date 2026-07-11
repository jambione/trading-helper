#!/usr/bin/env python3
"""Standalone runner for the real-position feed (webull_bridge/position_feed.py).

Normally you don't need this: the webull-l2 monitor starts the feed as a
daemon thread and it closes with the monitor. Run this directly only to
test the broker link on its own.

Usage:
    .venv/Scripts/python.exe scripts/position_feed.py
Keys come from WEBULL_APP_KEY/_SECRET env vars or config/webull_bridge.json.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webull_bridge.position_feed import run  # noqa: E402

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
