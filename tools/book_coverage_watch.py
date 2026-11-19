#!/usr/bin/env python3
"""book_coverage_watch - is the book seeing the day's real leaders? READ-ONLY (operator 11/19 'Yes, build both').

Every 10 minutes in RTH: the top liquid $20-$400 gainers (Alpaca snapshots over most-actives + movers + the prior
day's top-400 by dollar volume) against the names seated on the watch book (ai_reports/entry_watch_state.json),
with each missing leader's refusal reasons from today's admission ledger. Writes
ai_reports/book_coverage/<day>.jsonl (one row per scan) and latest.json. Never places orders, never edits desk state.

    .venv/bin/python tools/book_coverage_watch.py --once     # one scan now
    .venv/bin/python tools/book_coverage_watch.py --loop     # 09:35-16:00 ET every 10 min (run on the mini)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "book_coverage"
WATCH_STATE = ROOT / "ai_reports" / "entry_watch_state.json"
LEDGER_DIR = ROOT / "ai_reports" / "admit_ledger"
DATA = "https://data.alpaca.markets"
TOP_N, MIN_PCT, MIN_PREV_DV, PX_LO, PX_HI = 30, 1.5, 50e6, 20.0, 400.0
INTERVAL, START, END = 600, dtime(9, 35), dtime(16, 0)


def leaders(snaps: dict, top_n=TOP_N, min_pct=MIN_PCT, min_prev_dv=MIN_PREV_DV) -> list[dict]:
    """Alpaca snapshot dict {SYM: snap} -> top gainers in $20-$400 with prior-day dollar volume >= min_prev_dv. Pure."""
    rows = []
    for sym, s in (snaps or {}).items():
        try:
            prev = s.get("prevDailyBar") or {}
            pc, pv = float(prev.get("c") or 0), float(prev.get("v") or 0)
            px = float((s.get("latestTrade") or {}).get("p") or (s.get("minuteBar") or {}).get("c") or 0)
            q = s.get("latestQuote") or {}
            bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if not (pc > 0 and px > 0) or not (PX_LO <= px <= PX_HI) or pc * pv < min_prev_dv:
            continue
        pct = (px / pc - 1) * 100
        if pct < min_pct:
            continue
        spread = (ask - bid) / ((ask + bid) / 2) * 1e4 if bid > 0 and ask >= bid else None
        rows.append({"symbol": sym, "price": round(px, 2), "pct": round(pct, 2),
                     "prev_dollar_vol_m": round(pc * pv / 1e6, 1),
                     "spread_bp_iex": None if spread is None else round(spread, 1)})
    rows.sort(key=lambda r: -r["pct"])
    return rows[:top_n]


def coverage(top: list[dict], seated: set[str], reasons: dict[str, Counter], kept: set[str]) -> dict:
    """Join leaders to the book and the ledger. Pure."""
    seen, missing = [], []
    for r in top:
        s = r["symbol"]
        if s in seated:
            seen.append({**r, "status": "seated"})
            continue
        why = reasons.get(s) or Counter()
        missing.append({**r, "status": "kept_earlier" if s in kept else ("refused" if why else "never_offered"),
                        "top_reasons": dict(why.most_common(3))})
    blockers = Counter()
    for m in missing:
        if m["top_reasons"]:
            blockers[next(iter(m["top_reasons"]))] += 1
        else:
            blockers[m["status"]] += 1
    return {"leaders": len(top), "seated": len(seen), "missing": len(missing),
            "coverage_pct": round(100 * len(seen) / len(top), 1) if top else None,
            "blockers": dict(blockers.most_common()), "seen": seen, "missing_detail": missing}


class LedgerTail:
    """Incremental reader of today's admission ledger: refusal reasons and kept names per symbol."""

    def __init__(self, day: str):
        self.path, self.pos = LEDGER_DIR / f"{day}.jsonl", 0
        self.reasons: dict[str, Counter] = defaultdict(Counter)
        self.kept: set[str] = set()

    def update(self):
        if not self.path.exists():
            return
        with open(self.path, "rb") as fh:
            fh.seek(self.pos)
            chunk = fh.read()
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return
        self.pos += cut + 1
        for line in chunk[:cut].splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            s = r.get("symbol")
            if not s:
                continue
            if r.get("kept") is True:
                self.kept.add(s)
            elif r.get("reason"):
                self.reasons[s][r["reason"]] += 1


def _keys():
    p = os.environ.get("TH_DATA_SECRETS") or str(ROOT / "secrets_data.json")
    k = json.load(open(p))
    return {"APCA-API-KEY-ID": k["alpaca_data_api_key"], "APCA-API-SECRET-KEY": k["alpaca_data_secret_key"]}


def universe(headers) -> list[str]:
    import requests
    syms: set[str] = set()
    for path, key in (("v1beta1/screener/stocks/most-actives?by=volume&top=100", "most_actives"),
                      ("v1beta1/screener/stocks/movers?top=50", "gainers")):
        try:
            r = requests.get(f"{DATA}/{path}", headers=headers, timeout=15)
            if r.status_code == 200:
                syms |= {x.get("symbol") for x in r.json().get(key, []) if x.get("symbol")}
        except Exception:  # noqa: BLE001
            pass
    try:   # prior session's top-400 by dollar volume from the warehouse (what the studies call the universe)
        sys.path.insert(0, str(ROOT / "tools" / "studies"))
        from relative_strength_scan import load_day
        from evening_family import WH_SIP
        import numpy as np
        last = sorted(WH_SIP.glob("20??-??-??.npz"))[-1]
        d = load_day(last)
        dv = np.nansum(d["c"] * d["v"], axis=1)
        syms |= {d["syms"][i] for i in np.argsort(-dv)[:400]}
    except Exception:  # noqa: BLE001
        pass
    return sorted(s for s in syms if s and s.isalpha())


def snapshots(headers, syms) -> dict:
    import requests
    out = {}
    for i in range(0, len(syms), 200):
        try:
            r = requests.get(f"{DATA}/v2/stocks/snapshots", headers=headers, timeout=20,
                             params={"symbols": ",".join(syms[i:i + 200]), "feed": "iex"})
            if r.status_code == 200:
                out.update(r.json() or {})
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return out


def seated_now() -> set[str]:
    try:
        return {str(k).upper() for k in json.loads(WATCH_STATE.read_text()).keys()}
    except Exception:  # noqa: BLE001
        return set()


def scan(tail: LedgerTail, headers, syms=None) -> dict:
    now = datetime.now(ET)
    tail.update()
    top = leaders(snapshots(headers, syms or universe(headers)))
    res = {"ts": now.isoformat(timespec="seconds"), **coverage(top, seated_now(), tail.reasons, tail.kept)}
    OUT.mkdir(parents=True, exist_ok=True)
    with open(OUT / f"{now.date().isoformat()}.jsonl", "a") as fh:
        fh.write(json.dumps(res) + "\n")
    (OUT / "latest.json").write_text(json.dumps(res, indent=1))
    miss = ", ".join(f"{m['symbol']} +{m['pct']}% [{next(iter(m['top_reasons']), m['status'])}]"
                     for m in res["missing_detail"][:12])
    print(f"{res['ts']} coverage {res['seated']}/{res['leaders']} ({res['coverage_pct']}%) "
          f"blockers {res['blockers']} | missing: {miss}", flush=True)
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    a = ap.parse_args(argv)
    headers = _keys()
    day = datetime.now(ET).date().isoformat()
    tail = LedgerTail(day)
    if a.once or not a.loop:
        scan(tail, headers)
        return 0
    syms = universe(headers)
    while True:
        now = datetime.now(ET)
        if now.date().isoformat() != day or now.time() >= END or now.weekday() >= 5:
            print("session over", flush=True)
            return 0
        if now.time() >= START:
            scan(tail, headers, syms)
            time.sleep(INTERVAL)
        else:
            time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
