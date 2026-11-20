#!/usr/bin/env python3
"""stack_logger - record every layer the operator's stack model uses, once a minute, so stacks can be backtested.

Operator 11/20: "Everything in our system is not an isolated event. They all work together to prove a direction and a
movement" -> "build the logger". The trending SCORE, mention counts/rates and the desk's per-name readings were never
saved, so no stack using them could be tested. This polls the dashboard's /api/state (read-only, the same feed the
momentum monitor reads) and appends one compact snapshot per minute to ai_reports/stack_log/DAY.jsonl.gz.

It never trades, never writes to the dashboard, and only reads. Weekdays 04:00-20:00 ET.

    .venv/bin/python tools/stack_logger.py            # loop (run by the LaunchAgent com.jambi.stack-logger)
    .venv/bin/python tools/stack_logger.py --once     # one snapshot to stdout (check)

Snapshot record: {"ts", "et", "trending": [...], "movers": [...], "ai": [...], "tickers": [...], "discord_n", "health"}
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "ai_reports" / "stack_log"
ET = ZoneInfo("America/New_York")
PERIOD = 60.0

TRENDING_KEYS = ("symbol", "rank", "trending_score", "watchlist_count", "price", "pct_change", "vol_session", "rvol",
                 "price_age_sec", "high_52w", "low_52w", "avg_vol_consolidated")
MOVER_KEYS = ("symbol", "score", "pct_change", "price", "rvol", "float_m", "dollar_volume", "origin", "criteria", "live_pct")
AI_KEYS = ("symbol", "rank", "trending_score", "p30", "p50", "p100", "position_pct", "source")
TICKER_KEYS = ("ticker", "src", "price", "price_age_sec", "prev_close", "pct_change", "mention_count", "mention_window",
               "mention_burst", "price_stale")
SP_KEYS = ("status", "proximity_pct", "is_hot", "mention_velocity", "cm_rsi", "cm_ok", "cm_rsi_rising", "pctr",
           "pctr_ok", "pctr_rising", "pctr_slow", "in_position")
FUNNEL_KEYS = ("score", "state", "rvol", "rejects")


def _pick(d: dict, keys) -> dict:
    return {k: d.get(k) for k in keys if d.get(k) is not None}


def snapshot(state: dict, now: float) -> dict:
    """Compact, JSON-safe record of the layers in one /api/state payload. Pure."""
    tr = state.get("trending") or {}
    mv = state.get("movers") or {}
    ai = state.get("ai_suggestions") or {}
    dc = state.get("discord") or {}
    tickers = []
    for t in state.get("tickers") or []:
        row = _pick(t, TICKER_KEYS)
        sp = t.get("signal_proximity")
        if isinstance(sp, dict):
            row["sp"] = _pick(sp, SP_KEYS)
        fn = t.get("funnel")
        if isinstance(fn, dict):
            row["funnel"] = _pick(fn, FUNNEL_KEYS)
        tickers.append(row)
    return {
        "ts": round(now, 1),
        "et": datetime.fromtimestamp(now, ET).strftime("%H:%M:%S"),
        "trending": [_pick(r, TRENDING_KEYS) for r in (tr.get("rows") or []) if isinstance(r, dict)],
        "movers": [_pick(r, MOVER_KEYS) for r in (mv.get("rows") or []) if isinstance(r, dict)],
        "ai": [_pick(r, AI_KEYS) for r in (ai.get("rows") or []) if isinstance(r, dict)],
        "tickers": tickers,
        "discord_n": dc.get("count"),
        "health": {"trending_updated": tr.get("updated"), "trending_error": tr.get("error") or None,
                   "trending_volume_error": tr.get("volume_error") or None, "rvol_coverage": tr.get("rvol_coverage"),
                   "movers_ts": mv.get("ts"), "discord_running": dc.get("running"), "discord_last_ts": dc.get("last_ts")},
    }


def in_window(now: float) -> bool:
    d = datetime.fromtimestamp(now, ET)
    return d.weekday() < 5 and "0400" <= d.strftime("%H%M") < "2000"


def fetch_state() -> dict | None:
    import windows_agent as wa                      # the momentum monitor's auth path (token or desk secret)
    headers = {"Accept": "application/json", "User-Agent": wa._UA}
    try:
        wa._ensure_token()
        headers.update(wa._auth_header())
        with urlopen(Request("http://127.0.0.1:8888/api/state", headers=headers), timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception as e:                          # noqa: BLE001
        print(time.strftime("%H:%M:%S"), "fetch failed:", str(e)[:120], flush=True)
        return None


def append(rec: dict, now: float) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{datetime.fromtimestamp(now, ET).strftime('%Y-%m-%d')}.jsonl.gz"
    with gzip.open(p, "at") as f:                   # one gzip member per write; readers handle concatenated members
        f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
    return p


def main(argv) -> int:
    if "--once" in argv:
        st = fetch_state()
        print(json.dumps(snapshot(st, time.time()), indent=1, default=str)[:4000] if st else "no state")
        return 0 if st else 1
    while True:
        now = time.time()
        if in_window(now):
            st = fetch_state()
            if st is not None:
                append(snapshot(st, now), now)
            else:
                append({"ts": round(now, 1), "et": datetime.fromtimestamp(now, ET).strftime("%H:%M:%S"),
                        "health": {"fetch_failed": True}}, now)
        time.sleep(max(1.0, PERIOD - (time.time() - now)))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
