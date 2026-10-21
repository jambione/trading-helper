#!/usr/bin/env python3
"""Order-flow pilot. Spec: docs/studies/order_flow_pilot_prereg.json (as amended).

This file holds the first two steps only (the features, count step, outcomes and score come after the pre-step's
condition list is registered):

  --sample   every PRE onset on the 400-name 'indicator-hist-sample' (warm %R, decision minute m = onset + 1,
             10:00-15:27; no IEX bar-coverage rule), then a seeded draw of 16,000 entries (8,000 per half,
             sha256('order-flow-pilot')) -> sample.json. Bars only; no tick and no outcome is read.
  --prestep  20 seeded entries from sample.json: fetch SIP trades [T - 60 s, T) and NBBO quotes [T - 5 s, T + 1 s]
             (T = the start of minute m) from the data-only account and print, per tape (A/B = CTA, C = UTP), the
             trade condition codes with counts and whether size / conditions / tape / bid_size / ask_size are
             populated, plus locked / crossed / zero quotes -> prestep.json. No feature is computed.

Mini, after hours only (16:15-08:30 ET); the data-only account in secrets_data.json, never the desk's key.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicator_confirm_hist as H  # noqa: E402

S, E, BS = H.S, H.E, H.BS
OUT_DIR = os.path.join(E.ROOT, "ai_reports", "order_flow_pilot")
SAMPLE_N, SAMPLE_TAG, PRESTEP_N, PRESTEP_TAG = 16_000, "order-flow-pilot", 20, "order-flow-prestep"
ONSET_LO, ONSET_HI = S.ARM_START - 1, 15 * 60 + 26      # decision m = onset + 1 in 10:00 .. 15:27
WINDOW_S, QUOTE_BEFORE_S, QUOTE_AFTER_S = 60, 5, 1
TAPES = {"A": "CTA (NYSE-listed)", "B": "CTA (other exchanges)", "C": "UTP (Nasdaq-listed)"}


def decision_ts(day: str, m: int) -> float:
    """T: the start of ET minute m on `day` (the moment the onset bar m - 1 is complete)."""
    return BS.et_ts(day, m // 60, m % 60)


def pre_decisions(ser: dict) -> list[int]:
    m, f, s = ser["minutes"], ser["fast"], ser["slow"]
    flags = [E.pre_flag(f, s, i) for i in range(len(m))]
    on, _off = E.fires(m, flags, set(m), start=ONSET_LO, end=ONSET_HI, refract=H.REFRACT)
    return [x + 1 for x in on]


def day_onsets(args) -> list[tuple[str, str, int]]:
    """(day, symbol, decision minute) for every PRE onset of one session (bars only)."""
    k, days = args
    day, prev = days[k], days[k - 1]
    iex, sip, p_iex = H.load_raw("iex", day), H.load_raw("sip", day), H.load_raw("iex", prev)
    names = H.sample_names(day, [s for s in iex if s in sip and s in p_iex])
    E.WARM_LINES = True
    out = []
    for sym in names:
        ser = E.name_day_series(H.frame(day, iex[sym]), H.frame(prev, p_iex[sym][-E.CON_PRIOR_BARS:]))
        out += [(day, sym, m) for m in pre_decisions(ser)]
    return out


def draw(onsets: list[tuple[str, str, int]], n: int = SAMPLE_N, tag: str = SAMPLE_TAG) -> list[dict]:
    """Seeded draw of n entries, n/2 per half (all of a half when it has fewer), in a stable order."""
    rng = random.Random(int(hashlib.sha256(tag.encode()).hexdigest()[:16], 16))
    out = []
    for half in ("A", "B"):
        pool = sorted(o for o in onsets if H.half_of(o[0]) == half)
        pick = pool if len(pool) <= n // 2 else rng.sample(pool, n // 2)
        out += [{"day": d, "sym": s, "m": m, "half": half} for d, s, m in sorted(pick)]
    return out


# ── pre-step inventory ───────────────────────────────────────────────────────

def _codes(x) -> list[str]:
    c = getattr(x, "conditions", None)
    return [str(v) for v in c] if c else []


def inventory(trades: list, quotes: list) -> dict:
    """Per tape: trade counts, condition codes, field population; quote sizes and locked / crossed / zero quotes.
    `trades` / `quotes` are Alpaca Trade / Quote models (or anything with the same attributes)."""
    inv: dict = defaultdict(lambda: {"trades": 0, "codes": Counter(), "no_conditions": 0, "size_ok": 0,
                                     "exchanges": Counter(), "quotes": 0, "bid_size_ok": 0, "ask_size_ok": 0,
                                     "locked": 0, "crossed": 0, "zero_price": 0, "zero_size": 0,
                                     "quote_codes": Counter()})
    for t in trades:
        tape = str(getattr(t, "tape", None) or "?")
        r = inv[tape]
        r["trades"] += 1
        codes = _codes(t)
        if not codes:
            r["no_conditions"] += 1
        for c in codes:
            r["codes"][c] += 1
        if (getattr(t, "size", None) or 0) > 0:
            r["size_ok"] += 1
        r["exchanges"][str(getattr(t, "exchange", None))] += 1
    for q in quotes:
        tape = str(getattr(q, "tape", None) or "?")
        r = inv[tape]
        r["quotes"] += 1
        b, a = float(getattr(q, "bid_price", 0) or 0), float(getattr(q, "ask_price", 0) or 0)
        bs, as_ = getattr(q, "bid_size", None), getattr(q, "ask_size", None)
        r["bid_size_ok"] += 1 if (bs or 0) > 0 else 0
        r["ask_size_ok"] += 1 if (as_ or 0) > 0 else 0
        if b <= 0 or a <= 0:
            r["zero_price"] += 1
        elif a == b:
            r["locked"] += 1
        elif b > a:
            r["crossed"] += 1
        if not bs or not as_:
            r["zero_size"] += 1
        for c in _codes(q):
            r["quote_codes"][c] += 1
    return {k: {kk: (dict(vv.most_common()) if isinstance(vv, Counter) else vv) for kk, vv in v.items()}
            for k, v in inv.items()}


def merge(invs: list[dict]) -> dict:
    out: dict = {}
    for inv in invs:
        for tape, r in inv.items():
            o = out.setdefault(tape, {})
            for k, v in r.items():
                if isinstance(v, dict):
                    c = Counter(o.get(k) or {})
                    c.update(v)
                    o[k] = dict(c.most_common())
                else:
                    o[k] = o.get(k, 0) + v
    return out


def data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    s = json.load(open(os.path.join(E.ROOT, "secrets_data.json")))
    return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])


def fetch_entry(cl, sym: str, t: float) -> tuple[list, list]:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest, StockTradesRequest

    def utc(x):
        return datetime.fromtimestamp(x, timezone.utc)
    for attempt in range(5):
        try:
            tr = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=utc(t - WINDOW_S), end=utc(t),
                                                        feed=DataFeed.SIP, limit=10_000)).data.get(sym) or []
            time.sleep(0.45)
            qu = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=utc(t - QUOTE_BEFORE_S),
                                                        end=utc(t + QUOTE_AFTER_S), feed=DataFeed.SIP,
                                                        limit=10_000)).data.get(sym) or []
            time.sleep(0.45)
            return tr, qu
        except Exception as e:  # noqa: BLE001
            print(f"  {sym} {int(t)} try {attempt}: {type(e).__name__} {str(e)[:80]}", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"fetch failed for {sym} at {int(t)}")


def after_hours(now: datetime | None = None) -> bool:
    now = (now or datetime.now(E.ET)).astimezone(E.ET)
    hm = now.hour * 60 + now.minute
    return now.weekday() >= 5 or hm >= 16 * 60 + 15 or hm < 8 * 60 + 30


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p, "w"), indent=1, default=str)
    return p


def print_inventory(inv: dict) -> None:
    for tape in sorted(inv):
        r = inv[tape]
        print(f"\ntape {tape} - {TAPES.get(tape, 'unknown')}: {r.get('trades', 0)} trades, {r.get('quotes', 0)} quotes")
        if r.get("trades"):
            print(f"  trades: size populated {r['size_ok']}/{r['trades']}, no conditions {r['no_conditions']}")
            print(f"  condition codes: {r['codes']}")
            print(f"  exchanges: {r['exchanges']}")
        if r.get("quotes"):
            print(f"  quotes: bid_size>0 {r['bid_size_ok']}/{r['quotes']}, ask_size>0 {r['ask_size_ok']}/{r['quotes']}, "
                  f"locked {r['locked']}, crossed {r['crossed']}, zero price {r['zero_price']}, zero size {r['zero_size']}")
            print(f"  quote condition codes: {r['quote_codes']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sample", action="store_true")
    g.add_argument("--prestep", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    if a.sample:
        days = H.warehouse_days()
        ks = list(range(H.LOOKBACK, len(days)))
        sessions = [days[k] for k in ks]
        if (sessions[0], sessions[-1]) != ("2026-05-11", "2026-10-15"):
            raise SystemExit(f"warehouse sessions {sessions[0]} .. {sessions[-1]}, expected 2026-05-11 .. 2026-10-15")
        onsets = []
        chunk = max(1, math.ceil(len(ks) / max(a.workers, 1)))
        with ProcessPoolExecutor(max_workers=a.workers) as ex:
            for got in ex.map(day_onsets, [(k, days) for k in ks], chunksize=chunk):
                onsets += got
        sample = draw(onsets)
        per = Counter(e["half"] for e in sample)
        print(f"PRE onsets {len(onsets)} over {len(sessions)} sessions; sample {len(sample)} ({dict(per)})")
        print(write("sample.json", {"sessions": sessions, "onsets": len(onsets), "seed_tag": SAMPLE_TAG,
                                    "entries": sample}))
        return
    if not after_hours():
        raise SystemExit("tick fetches run only 16:15-08:30 ET (prereg fetch plan)")
    smp = json.load(open(os.path.join(OUT_DIR, "sample.json")))["entries"]
    rng = random.Random(int(hashlib.sha256(PRESTEP_TAG.encode()).hexdigest()[:16], 16))
    picks = sorted(rng.sample(smp, PRESTEP_N), key=lambda e: (e["day"], e["sym"], e["m"]))
    cl = data_client()
    invs, rows = [], []
    for e in picks:
        t = decision_ts(e["day"], e["m"])
        tr, qu = fetch_entry(cl, e["sym"], t)
        inv = inventory(tr, qu)
        invs.append(inv)
        rows.append({**e, "T": t, "trades": len(tr), "quotes": len(qu), "tapes": sorted(inv)})
        print(f"  {e['day']} {e['sym']} {e['m'] // 60:02d}:{e['m'] % 60:02d}: {len(tr)} trades, {len(qu)} quotes, "
              f"tapes {sorted(inv)}", flush=True)
    total = merge(invs)
    print_inventory(total)
    print(write("prestep.json", {"entries": rows, "inventory": total,
                                 "note": "no feature or outcome computed; the keep/drop condition list is registered "
                                         "from this inventory as a prereg amendment before any feature fetch"}))


if __name__ == "__main__":
    main()
