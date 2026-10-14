#!/usr/bin/env python3
"""Log when Alpaca news headlines actually ARRIVE, next to their created_at.

The news-seat study (docs/studies/news_seat_prereg.json) scores entries two
minutes after a headline's created_at. History cannot prove a headline was
available then: the REST archive carries today's created_at and today's
symbol tags. This logger is the forward check. One Alpaca news websocket
(the desk holds no other Alpaca stream; its live tape is Finnhub), every
symbol, one JSONL row per article:

    ai_reports/news_latency/YYYY-MM-DD.jsonl
    {"received": <unix>, "created_at": <unix>, "updated_at": <unix>,
     "id": ..., "symbols": [...], "headline": "..."}

Read-only: places no orders and touches no desk state.

    .venv/bin/python tools/news_latency_logger.py            # run (mini)
    .venv/bin/python tools/news_latency_logger.py --summary 2026-10-14
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "ai_reports", "news_latency")
ET = ZoneInfo("America/New_York")


def _ts(x) -> float | None:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if hasattr(x, "timestamp"):
        return x.timestamp()
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def row_from(msg, received: float) -> dict:
    get = (msg.get if isinstance(msg, dict) else lambda k, d=None: getattr(msg, k, d))
    return {
        "received": received,
        "created_at": _ts(get("created_at")),
        "updated_at": _ts(get("updated_at")),
        "id": get("id"),
        "symbols": list(get("symbols") or []),
        "headline": str(get("headline") or ""),
    }


def summary(day: str) -> str:
    p = os.path.join(OUT_DIR, f"{day}.jsonl")
    if not os.path.exists(p):
        return f"{day}: no log"
    lat = []
    for line in open(p):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("created_at") is not None and r.get("received") is not None:
            lat.append(r["received"] - r["created_at"])
    if not lat:
        return f"{day}: 0 rows"
    lat.sort()
    q = lambda f: lat[min(len(lat) - 1, int(f * len(lat)))]  # noqa: E731
    return (f"{day}: n {len(lat)}  latency (received - created_at) p50 {q(.5):.1f}s  "
            f"p90 {q(.9):.1f}s  p99 {q(.99):.1f}s  max {lat[-1]:.1f}s  "
            f"<= 60 s {sum(x <= 60 for x in lat) / len(lat):.0%}")


def run() -> None:
    from alpaca.data.live.news import NewsDataStream
    sec = json.load(open(os.path.join(ROOT, "config", "secrets.json")))
    os.makedirs(OUT_DIR, exist_ok=True)

    async def on_news(msg):
        received = time.time()
        day = datetime.fromtimestamp(received, ET).strftime("%Y-%m-%d")
        with open(os.path.join(OUT_DIR, f"{day}.jsonl"), "a") as f:
            f.write(json.dumps(row_from(msg, received)) + "\n")

    while True:
        try:
            stream = NewsDataStream(sec["api_key"], sec["secret_key"], raw_data=True)
            stream.subscribe_news(on_news, "*")
            stream.run()
        except Exception as e:  # noqa: BLE001 — reconnect; never die silently
            print(f"[news_latency] stream error {type(e).__name__}: {str(e)[:120]}; reconnect in 30 s",
                  file=sys.stderr, flush=True)
        time.sleep(30)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summary", metavar="DAY")
    a = ap.parse_args()
    if a.summary:
        print(summary(a.summary))
    else:
        run()


if __name__ == "__main__":
    main()
