#!/usr/bin/env python3
"""lag_breakdown_recent.py (DIAGNOSTIC, no pass/fail): lag_breakdown.py on sessions after the bar warehouse ends.

The warehouse stops at 2026-10-15, so the per-symbol IEX and SIP 1-minute bars lag_audit needs for the turn are
fetched from Alpaca historical (data-only key, raw, RTH, cached) for the filled symbols only. Everything else is
lag_breakdown.day_rows unchanged. Re-measures the delay stages after the 10/15 (confirm / vol gate / G2 off / 3 s
tick) and 10/28 (movers scan, push-filter spread aging) fixes.
USAGE (mini, one heavy Alpaca job at a time): .venv/bin/python tools/studies/lag_breakdown_recent.py [FIRST LAST]
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lag_breakdown as LB  # noqa: E402
import move_to_cost_holdout as MH  # noqa: E402

OUT = Path(os.path.expanduser("~/replay_runs/lag_breakdown_recent.json"))


class FeedData(MH.Data):
    def bars_feed(self, sym: str, day: str, feed: str):
        import datetime as _dt
        def fn():
            t0 = _dt.datetime.fromisoformat(f"{day} 09:30").replace(tzinfo=MH.ET)
            out, token = [], None
            while True:
                params = {"timeframe": "1Min", "feed": feed, "adjustment": "raw", "limit": 10000,
                          "start": t0.isoformat(), "end": (t0 + _dt.timedelta(minutes=390)).isoformat()}
                if token:
                    params["page_token"] = token
                d = self._call(f"/v2/stocks/{sym}/bars", params)
                for b in d.get("bars") or []:
                    ts = _dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(MH.ET)
                    out.append([ts.hour * 60 + ts.minute, b["o"], b["h"], b["l"], b["c"], b["v"]])
                token = d.get("next_page_token")
                if not token:
                    return out
        return self._cached(f"bars|{feed}|{sym}|{day}", fn)


class Lazy(dict):
    """load_raw stand-in: symbol -> bars, fetched on first membership test."""
    def __init__(self, data, feed, day):
        super().__init__()
        self.data, self.feed, self.day = data, feed, day

    def __contains__(self, sym):
        if not dict.__contains__(self, sym):
            try:
                rows = self.data.bars_feed(sym, self.day, self.feed)
            except MH.FetchError:
                rows = []
            if rows:
                dict.__setitem__(self, sym, rows)
        return dict.__contains__(self, sym)


def main() -> int:
    first, last = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("2026-10-16", "2026-10-29")
    data = FeedData.live()
    data.dir = Path(os.path.expanduser("~/replay_runs/lag_breakdown_recent_cache"))
    LB.H.load_raw = lambda feed, day: Lazy(data, feed, day)
    days = sorted(d for d in os.listdir(LB.SNAP) if d[:2] == "20" and os.path.exists(os.path.join(LB.SNAP, d, "fills.jsonl")))
    rows = []
    for i, day in enumerate(days):
        if first <= day <= last and i >= 1:
            rows += LB.day_rows(day, days[i - 1])
            print(f"{day}: {len(rows)} rows, calls {data.calls}", file=sys.stderr, flush=True)
    for r in rows:
        if isinstance(r.get("wait_reasons"), dict):
            r["wait_reasons"] = dict(r["wait_reasons"])
    OUT.write_text(json.dumps(rows, default=str))
    print(f"wrote {OUT}: {len(rows)} fills, {sum(not r.get('skip') for r in rows)} with all stages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
