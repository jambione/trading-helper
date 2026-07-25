"""Standalone runner for the real-position feed (trade_bridge/position_feed.py).

Usage:
    python3 scripts/position_feed.py

Keys: ALPACA_API_KEY / ALPACA_SECRET_KEY (env or signal_engine.env).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trade_bridge.position_feed import run  # noqa: E402


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
