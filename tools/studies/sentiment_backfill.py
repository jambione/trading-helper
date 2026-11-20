#!/usr/bin/env python3
"""sentiment_backfill - Stocktwits bullish/bearish sentiment as it stood at each past trending entry (operator 11/20:
start testing the multi-source stack without waiting a month). Inputs only - no prices, no outcomes.

Events: momentum-monitor/journal kind 'st_new', first per name-day, weekdays 09:35-15:00 ET, 2026-08-03..2026-11-19
(the trending_two_step event set). For each event at time T: the latest 30 messages for the symbol created STRICTLY
BEFORE T, summarised exactly as tools/stack_logger.parse_stream does live (bull, bear, bull_share, newest age,
messages per hour). Point-in-time: every message newer than T is dropped by its own timestamp, never by id.

How it reaches the past: Stocktwits message ids grow with time, and a symbol stream accepts max=<id>. The script keeps
a time->id calibration from every message it sees, asks for max = id(T + margin), drops anything after T, and pages
back until it has 30 messages before T (or the oldest is > 24 h before T). Polite pace: one request per PACE seconds,
10-minute back-off on HTTP 429. Resumable: ai_reports/sentiment_backfill/cache.json.

    .venv/bin/python tools/studies/sentiment_backfill.py run
"""
from __future__ import annotations

import bisect
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from stack_logger import parse_stream  # noqa: E402

OUT = ROOT / "ai_reports" / "sentiment_backfill"
JOURNAL = ROOT / "momentum-monitor" / "journal"
ET = ZoneInfo("America/New_York")
LO, HI = "2026-08-03", "2026-11-19"
PACE, WANT, MAX_PAGES = 28.0, 30, 12     # ~130 req/h: Stocktwits throttles unauthenticated clients near 200/h, and the live logger shares the IP
UA = {"User-Agent": "Mozilla/5.0 (research logger)"}


def events():
    """First st_new per (symbol, day), weekdays 09:35-15:00 ET. -> sorted [(ts, day, sym, et)]. Pure on the journal."""
    seen, out = set(), []
    for f in sorted(JOURNAL.glob("2026-*.jsonl")):
        day = f.stem
        if not (LO <= day <= HI) or datetime.fromisoformat(day).weekday() >= 5:
            continue
        for line in open(f):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") != "st_new" or not ("09:35:00" <= str(r.get("et")) <= "15:00:00"):
                continue
            key = (day, r["sym"])
            if key in seen:
                continue
            seen.add(key)
            out.append((float(r["ts"]), day, r["sym"], r["et"]))
    return sorted(out)


def iso_ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


class Calib:
    """Monotone time -> message-id map built from observed (time, id) pairs."""

    def __init__(self, pts=None):
        self.t, self.i = [], []
        for t, i in sorted(pts or []):
            self.add(t, i)

    def add(self, t, i):
        k = bisect.bisect_left(self.t, t)
        self.t.insert(k, t)
        self.i.insert(k, i)

    def id_at(self, t):
        if not self.t:
            return None
        k = bisect.bisect_left(self.t, t)
        if k == 0:
            return self.i[0]
        if k >= len(self.t):
            return self.i[-1]
        t0, t1, i0, i1 = self.t[k - 1], self.t[k], self.i[k - 1], self.i[k]
        return int(i0 + (i1 - i0) * (t - t0) / max(t1 - t0, 1))

    def gap(self, t):
        """Seconds to the nearest calibration point (uncertainty proxy)."""
        if not self.t:
            return 1e9
        k = bisect.bisect_left(self.t, t)
        c = [abs(self.t[j] - t) for j in (k - 1, k) if 0 <= j < len(self.t)]
        return min(c)


_last = {"t": 0.0}


def get(url):
    while True:
        wait = PACE - (time.time() - _last["t"])
        if wait > 0:
            time.sleep(wait)
        _last["t"] = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(time.strftime("%H:%M:%S"), "429 - backing off 10 min", flush=True)
                time.sleep(600)
                continue
            if e.code == 404:
                return {"messages": [], "cursor": {}}
            print(time.strftime("%H:%M:%S"), "HTTP", e.code, url[-60:], flush=True)
            time.sleep(30)
        except Exception as e:                      # noqa: BLE001
            print(time.strftime("%H:%M:%S"), "error", str(e)[:80], flush=True)
            time.sleep(30)


def anchor(cal, target_ts, sym="SPY", iters=24):
    """Binary-search a message id whose SPY message time is just below target_ts; adds every seen point."""
    lo, hi = 1, cal.id_at(time.time()) or 700_000_000
    for _ in range(iters):
        mid = (lo + hi) // 2
        d = get(f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json?max={mid}")
        ms = d.get("messages") or []
        for m in ms:
            cal.add(iso_ts(m["created_at"]), int(m["id"]))
        if not ms:
            lo = mid
            continue
        newest = iso_ts(ms[0]["created_at"])
        if newest < target_ts:
            lo = mid
        else:
            hi = mid
        if hi - lo < 2000:
            break


def fetch_event(cal, ts, sym):
    """Latest WANT messages created strictly before ts. -> (messages before ts, requests used, valid).
    Coverage check (skeptic 11/20 #2): valid only if some page's newest message is at or after ts (so nothing between
    the start point and ts can be missing), or a query with max = id(ts + 3 h) confirms the symbol had no message
    between its newest one and ts."""
    margin = min(max(cal.gap(ts) * 0.2, 600), 3 * 3600)
    mx = cal.id_at(ts + margin)
    got, req, covered = [], 0, False
    first = True
    while req < MAX_PAGES:
        d = get(f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json?max={mx}")
        req += 1
        ms = d.get("messages") or []
        if first:
            first = False
            if ms and iso_ts(ms[0]["created_at"]) >= ts:
                covered = True
            elif not covered:
                d2 = get(f"https://api.stocktwits.com/api/2/streams/symbol/{sym}.json?max={cal.id_at(ts + 3 * 3600)}")
                req += 1
                ms2 = d2.get("messages") or []
                for m in ms2:
                    cal.add(iso_ts(m["created_at"]), int(m["id"]))
                if not ms2 or iso_ts(ms2[0]["created_at"]) >= ts or (ms and ms2[0]["id"] == ms[0]["id"]):
                    covered = True
                    if ms2 and iso_ts(ms2[0]["created_at"]) >= ts:
                        ms = ms2               # restart from the confirmed page
        if not ms:
            break
        for m in ms:
            t = iso_ts(m["created_at"])
            cal.add(t, int(m["id"]))
            if t < ts and all(int(m["id"]) != int(g["id"]) for g in got):
                got.append(m)
        oldest = iso_ts(ms[-1]["created_at"])
        if len(got) >= WANT or oldest < ts - 24 * 3600:
            break
        mx = int(ms[-1]["id"]) - 1
    got.sort(key=lambda m: -int(m["id"]))
    return got[:WANT], req, covered


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    cp = OUT / "cache.json"
    cache = json.loads(cp.read_text()) if cp.exists() else {"calib": [], "events": {}}
    cal = Calib(cache["calib"])
    if len(cal.t) < 5:
        now = time.time()
        d = get("https://api.stocktwits.com/api/2/streams/symbol/SPY.json")
        for m in d.get("messages") or []:
            cal.add(iso_ts(m["created_at"]), int(m["id"]))
        for day in ("2026-08-03", "2026-09-01", "2026-10-01", "2026-11-02"):
            anchor(cal, datetime.fromisoformat(day + "T09:30").replace(tzinfo=ET).timestamp())
        print("anchors", len(cal.t), round(time.time() - now), "s", flush=True)
    evs = events()
    print("events", len(evs), "done", len(cache["events"]), flush=True)
    for n, (ts, day, sym, et) in enumerate(evs):
        key = f"{day}|{sym}"
        if key in cache["events"]:
            continue
        msgs, req, valid = fetch_event(cal, ts, sym)
        summ = parse_stream({"messages": msgs}, ts)
        cache["events"][key] = {"ts": ts, "et": et, **summ, "requests": req, "valid": valid,
                                "oldest_age_min": round((ts - iso_ts(msgs[-1]["created_at"])) / 60, 1) if msgs else None}
        if n % 10 == 0:
            cache["calib"] = list(zip(cal.t, cal.i))[-20000:]
            cp.write_text(json.dumps(cache))
            print(time.strftime("%H:%M:%S"), n, "/", len(evs), key, summ.get("bull_share"), "req", req, flush=True)
    cache["calib"] = list(zip(cal.t, cal.i))[-20000:]
    cp.write_text(json.dumps(cache))
    print("DONE", len(cache["events"]), flush=True)


if __name__ == "__main__":
    {"run": run}[sys.argv[1]]()
