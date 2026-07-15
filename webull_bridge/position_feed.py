"""Real-position feed: publishes broker holdings for the monitors.

Polls WebullBroker.positions() and writes position_state.json at the repo
root (same drop-file pattern as l2_state.json). The webull-l2 monitor uses
it to anchor exit signals (take-profit / trail / stop) to the actual fill
price instead of a virtual paper entry, and the TradingView monitor uses
it to switch the master verdict from entry-mode to exit-mode.

Read-only: only calls positions(); never places or cancels orders.

Runs two ways:
  - embedded: the L2 monitor calls start_daemon(), so the feed lives and
    dies with the monitor window (no separate process to forget)
  - standalone: scripts/position_feed.py, for testing the broker link
"""
import asyncio
import json
import logging
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "position_state.json"


def have_keys(cfg: dict) -> bool:
    return bool(cfg.get("webull_app_key") and cfg.get("webull_app_secret"))


def write_state(positions: list, ok: bool, error: str = "") -> None:
    # tmp + replace so a monitor never reads a half-written file
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({
        "ts": time.time(), "ok": ok, "error": error,
        "positions": [{"symbol": p.symbol, "qty": p.qty,
                       "avg_price": p.avg_price,
                       "last_price": p.last_price} for p in positions],
    }), encoding="utf-8")
    tmp.replace(STATE_FILE)


async def run(cfg: dict | None = None, quiet: bool = False) -> None:
    from webull_bridge.config import load_config
    from webull_bridge.providers.webull import WebullBroker

    cfg = cfg or load_config()
    if not have_keys(cfg):
        if not quiet:
            print("position feed: no Webull keys configured - not running")
        return
    if quiet:   # keep SDK log lines out of the monitor's live display
        # ERROR is the level the SDK logs its rejected requests AT, so this
        # silenced nothing; CRITICAL is what actually holds them back.
        logging.getLogger("webull").setLevel(logging.CRITICAL)

    broker = WebullBroker(cfg)
    interval = float(cfg.get("position_poll_sec", 5.0))
    last: list | None = None
    if not quiet:
        print(f"position feed -> {STATE_FILE} every {interval:.0f}s "
              "(Ctrl+C to stop)")
    while True:
        t0 = time.time()
        try:
            positions = await broker.positions()
            write_state(positions, ok=True)
            snap = [(p.symbol, p.qty, p.avg_price) for p in positions]
            if snap != last and not quiet:
                print(time.strftime("%H:%M:%S"),
                      " | ".join(f"{s} x{q:g} @ {c:.3f}" for s, q, c in snap)
                      or "flat (no positions)")
            last = snap
        except Exception as e:                        # noqa: BLE001
            write_state([], ok=False, error=str(e))
            if not quiet:
                print(time.strftime("%H:%M:%S"), "poll failed:", e)
        await asyncio.sleep(max(0.0, interval - (time.time() - t0)))


def start_daemon() -> bool:
    """Start the feed in a daemon thread (dies with the host process).

    Returns True if started, False when no keys are configured."""
    from webull_bridge.config import load_config
    cfg = load_config()
    if not have_keys(cfg):
        return False
    threading.Thread(target=lambda: asyncio.run(run(cfg, quiet=True)),
                     daemon=True, name="position-feed").start()
    return True
