#!/usr/bin/env python3
"""Order-flow pilot. Spec: docs/studies/order_flow_pilot_prereg.json (as amended).

This file holds the first two steps only (the features, count step, outcomes and score come after the pre-step's
condition list is registered):

  --sample   every PRE onset on the 400-name 'indicator-hist-sample' (warm %R, decision minute m = onset + 1,
             10:00-15:27; no IEX bar-coverage rule), then a seeded draw of 16,000 entries (8,000 per half,
             sha256('order-flow-pilot')) -> sample.json. Bars only; no tick and no outcome is read.
  --prestep  20 seeded entries from sample.json: fetch SIP trades [T - 60 s, T) and NBBO quotes [T - 120 s, T + 1 s]
             (T = the start of minute m) from the data-only account and print, per tape (A/B = CTA, C = UTP), the
             trade condition codes with counts and whether size / conditions / tape / bid_size / ask_size are
             populated, plus locked / crossed / zero quotes -> prestep.json. No feature is computed.

Mini, any time (prereg amended_2, operator 10/21); the data-only account in secrets_data.json, never the desk's key;
paced under 150 requests/min.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import random
import statistics
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
WINDOW_S, QUOTE_BEFORE_S, QUOTE_AFTER_S = 60, 120, 1     # amended_3: quotes over [T - 120 s, T + 1 s]
KEEP_CODES = frozenset({" ", "@", "I", "F"})               # amended_3: regular (CTA / UTP), odd lot, ISO
SIGN_MAX_AGE, READ_MAX_AGE = 60.0, 30.0                    # amended_3: quote age for signing / for QI, entry, exits
PAGE = 10_000
OUTCOME_HORIZONS = 4                                       # 60 / 120 / 180 / 300 s, one [t - 30 s, t] window each
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


def keep_trade(t) -> bool:
    """amended_3 keep-list: every condition code must be regular, odd lot or ISO."""
    codes = _codes(t)
    return bool(codes) and all(c in KEEP_CODES for c in codes)


def _ts(x) -> float:
    ts = getattr(x, "timestamp", None)
    return ts.timestamp() if hasattr(ts, "timestamp") else float(ts)


def coverage(trades: list, quotes: list, T: float) -> dict:
    """Data-quality counts for one entry (no feature: no trade is signed, no imbalance computed): kept / dropped
    trades by code, kept trades with a quote at most SIGN_MAX_AGE old at or before them, and whether a quote at most
    READ_MAX_AGE old exists at T (QI) and at T + 1 s (entry)."""
    import bisect
    qts = sorted(_ts(q) for q in quotes if (getattr(q, "bid_price", 0) or 0) > 0 and (getattr(q, "ask_price", 0) or 0) > 0
                 and float(q.ask_price) > float(q.bid_price) and getattr(q, "bid_size", 0) and getattr(q, "ask_size", 0))

    def fresh(t, max_age):
        i = bisect.bisect_right(qts, t) - 1
        return i >= 0 and t - qts[i] <= max_age
    kept = [t for t in trades if keep_trade(t)]
    dropped = Counter(c for t in trades if not keep_trade(t) for c in _codes(t) if c not in KEEP_CODES)
    return {"trades": len(trades), "kept": len(kept), "dropped_codes": dict(dropped),
            "signable": sum(1 for t in kept if fresh(_ts(t), SIGN_MAX_AGE)),
            "qi_quote": fresh(T, READ_MAX_AGE), "entry_quote": fresh(T + 1, READ_MAX_AGE),
            "quotes": len(qts), "requests": max(1, math.ceil(len(trades) / PAGE)) + max(1, math.ceil(len(quotes) / PAGE))}


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


ROW_CAP = 10_000          # the first feature fetch's per-request limit; a file at the cap may be truncated


def fetch_entry(cl, sym: str, t: float, sleep=time.sleep, limit: int | None = ROW_CAP) -> tuple[list, list]:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest, StockTradesRequest

    def utc(x):
        return datetime.fromtimestamp(x, timezone.utc)
    for attempt in range(5):
        try:
            tr = cl.get_stock_trades(StockTradesRequest(symbol_or_symbols=sym, start=utc(t - WINDOW_S), end=utc(t),
                                                        feed=DataFeed.SIP, limit=limit)).data.get(sym) or []
            sleep(REQ_GAP_S)
            qu = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=utc(t - QUOTE_BEFORE_S),
                                                        end=utc(t + QUOTE_AFTER_S), feed=DataFeed.SIP,
                                                        limit=limit)).data.get(sym) or []
            sleep(REQ_GAP_S)
            return tr, qu
        except Exception as e:  # noqa: BLE001
            print(f"  {sym} {int(t)} try {attempt}: {type(e).__name__} {str(e)[:80]}", flush=True)
            sleep(10 * (attempt + 1))
    raise RuntimeError(f"fetch failed for {sym} at {int(t)}")


# ── feature fetch (cached, resumable, paced) ─────────────────────────────────

TICK_DIR = os.path.join(OUT_DIR, "ticks")
REQ_GAP_S = 0.45                       # two requests per entry -> ~130 requests/min, under the 150/min cap
FAIL_ABORT, FAIL_MIN_ATTEMPTS = 0.02, 200


def entry_key(e: dict) -> str:
    return f"{e['day']}_{e['sym']}_{e['m']}"


def compact(trades: list, quotes: list) -> dict:
    """What the features need, as plain tuples: trades (ts, price, size, codes, tape), quotes (ts, bid, ask, bid_size,
    ask_size)."""
    return {"trades": [(_ts(t), float(t.price), float(t.size or 0), _codes(t), str(getattr(t, "tape", "") or ""))
                       for t in trades],
            "quotes": [(_ts(q), float(q.bid_price or 0), float(q.ask_price or 0), float(q.bid_size or 0),
                        float(q.ask_size or 0)) for q in quotes]}


def capped(tick: dict) -> bool:
    """skeptic r1 #2: alpaca-py stops at `limit` rows and keeps the EARLIEST, so a capped quote file lacks the rows near
    T. Such entries are refetched paged (limit=None) before the count step."""
    return len(tick["trades"]) >= ROW_CAP or len(tick["quotes"]) >= ROW_CAP


def refetch_capped(entries: list[dict], cl, tick_dir: str = TICK_DIR, sleep=time.sleep, log=print) -> Counter:
    import pickle
    st = Counter()
    for e in entries:
        path = os.path.join(tick_dir, entry_key(e) + ".pkl")
        if not os.path.exists(path) or not capped(pickle.load(open(path, "rb"))):
            continue
        st["capped"] += 1
        tr, qu = fetch_entry(cl, e["sym"], decision_ts(e["day"], e["m"]), sleep=sleep, limit=None)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump({**compact(tr, qu), "paged": True}, f)
        os.replace(tmp, path)
        st["refetched"] += 1
        log(f"  refetched {entry_key(e)} paged: {len(tr)} trades, {len(qu)} quotes")
    return st


def fetch_features(entries: list[dict], cl, tick_dir: str = TICK_DIR, sleep=time.sleep, log=print) -> Counter:
    """Fetch SIP trades [T - 60 s, T) and quotes [T - 120 s, T + 1 s] for every entry without a cache file. Resumable
    (a cache file per entry, written atomically). Aborts when > 2% of attempted entries fail after
    FAIL_MIN_ATTEMPTS attempts (prereg fetch plan)."""
    import pickle
    os.makedirs(tick_dir, exist_ok=True)
    st = Counter()
    for i, e in enumerate(entries):
        path = os.path.join(tick_dir, entry_key(e) + ".pkl")
        if os.path.exists(path):
            st["cached"] += 1
            continue
        st["attempted"] += 1
        try:
            tr, qu = fetch_entry(cl, e["sym"], decision_ts(e["day"], e["m"]), sleep=sleep)
        except RuntimeError as ex:
            st["failed"] += 1
            log(f"  FAILED {entry_key(e)}: {ex}")
            if st["attempted"] >= FAIL_MIN_ATTEMPTS and st["failed"] / st["attempted"] > FAIL_ABORT:
                log(f"ABORT: {st['failed']} of {st['attempted']} entries failed (> {FAIL_ABORT:.0%})")
                st["aborted"] = 1
                return st
            continue
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(compact(tr, qu), f)
        os.replace(tmp, path)
        st["fetched"] += 1
        if st["fetched"] % 250 == 0:
            log(f"  {i + 1}/{len(entries)}: fetched {st['fetched']}, cached {st['cached']}, failed {st['failed']} "
                f"({datetime.now(E.ET):%H:%M} ET)")
    return st


# ── features (from cached ticks only) ────────────────────────────────────────

MIN_SIGNED = 5


def clean_quotes(quotes: list) -> list:
    """Drop locked, crossed and zero-price / zero-size quotes; sort by time."""
    return sorted((q for q in quotes if q[1] > 0 and q[2] > 0 and q[2] > q[1] and q[3] > 0 and q[4] > 0),
                  key=lambda q: q[0])          # time only: stable, so delivery order survives within a timestamp


def last_quote(qs: list, t: float, max_age: float):
    import bisect
    i = bisect.bisect_right([q[0] for q in qs], t) - 1
    return qs[i] if i >= 0 and t - qs[i][0] <= max_age else None


def sign_trades(trades: list, qs: list, T: float) -> list[tuple[float, int]]:
    """Lee-Ready on kept trades in [T - 60 s, T): vs the prevailing mid (last clean quote at or before the trade,
    <= 60 s old): above +1, below -1; at the mid by the tick test against the previous kept trade's price (up +1,
    down -1, unchanged -> the previous sign). Unsignable trades are left out. Returns (size, sign) pairs."""
    import bisect
    qts = [q[0] for q in qs]
    out, prev_px, prev_sign = [], None, None
    for ts, px, size, codes, _tape in sorted(trades, key=lambda t: t[0]):     # time only (stable), not by price
        if not (T - WINDOW_S <= ts < T) or not codes or not all(c in KEEP_CODES for c in codes):
            continue
        i = bisect.bisect_right(qts, ts) - 1
        sign = None
        if i >= 0 and ts - qts[i] <= SIGN_MAX_AGE:
            mid = (qs[i][1] + qs[i][2]) / 2
            if px > mid:
                sign = 1
            elif px < mid:
                sign = -1
            elif prev_px is not None:
                sign = 1 if px > prev_px else -1 if px < prev_px else prev_sign
        if sign is not None:
            out.append((size, sign))
            prev_sign = sign
        prev_px = px
    return out


def features(tick: dict, T: float) -> dict:
    """OFI60, QI, MICRO and the entry-quote check for one entry. None = missing (entry leaves that cell)."""
    qs = clean_quotes(tick["quotes"])
    signed = sign_trades(tick["trades"], qs, T)
    vol = sum(sz for sz, _ in signed)
    ofi = (sum(sz * sg for sz, sg in signed) / vol) if len(signed) >= MIN_SIGNED and vol > 0 else None
    q = last_quote(qs, T, READ_MAX_AGE)
    qi = micro = None
    if q:
        _t, b, a, bs, as_ = q
        qi = (bs - as_) / (bs + as_)
        mid = (a + b) / 2
        micro = ((b * as_ + a * bs) / (bs + as_) - mid) / mid
    return {"ofi60": ofi, "qi": qi, "micro": micro, "n_signed": len(signed),
            "entry_quote": last_quote(qs, T + 1, READ_MAX_AGE) is not None}


def tie_u(r: dict) -> float:
    """amended_4: a seeded uniform per entry that breaks ties at the tercile cut (QI has an atom at 0)."""
    return int(hashlib.sha256(f"{r['sym']}|{r['day']}|{r['m']}|tercile-tie".encode()).hexdigest()[:16], 16) / 2 ** 64


def top_cut(values: list) -> tuple | None:
    """The top-tercile cut as a (value, tie_u) pair on half A: the first pair of the top third when entries are ranked
    by (value, tie_u). Entries are IN when (value, tie_u) >= the cut, so half A's IN share is exactly a third even
    with ties. `values` holds (value, tie_u) pairs; plain numbers are taken with tie_u = 0."""
    v = sorted((x if isinstance(x, tuple) else (x, 0.0)) for x in values
               if x is not None and (not isinstance(x, tuple) or x[0] is not None))
    return v[(2 * len(v)) // 3] if len(v) >= 3 else None


def groups(f: dict, cuts: dict) -> dict:
    """G1 / G2 / G3 sides (IN = top tercile by (value, tie_u); OUT = the rest with the feature; None = missing)."""
    u = tie_u(f) if all(k in f for k in ("sym", "day", "m")) else 0.0

    def side(x, c):
        if x is None or c is None:
            return None
        c = c if isinstance(c, (tuple, list)) else (c, 0.0)
        return "IN" if (x, u) >= tuple(c) else "OUT"
    g1, g2 = side(f["ofi60"], cuts["ofi60"]), side(f["qi"], cuts["qi"])
    g3 = None if g1 is None or g2 is None else ("IN" if g1 == "IN" and g2 == "IN" else "OUT")
    return {"G1": g1, "G2": g2, "G3": g3}


CTRL_TAG, HOLD_BARS = "order-flow-ctrl", 3
FLOORS = {"G1": 1000, "G2": 1000, "G3": 300}
N_TESTS = 3


def control_return(sym: str, day: str, m: int, sip: dict) -> float | None:
    """Outcome-blind projection input: the 3-minute SIP bar return (open of c to close of c + 2) at a same-name
    later decision minute c in m+1 .. m+60, seed sha256(symbol|day|m|order-flow-ctrl). No tick outcome is read."""
    elig = [c for c in range(m + 1, m + S.CTRL_SPAN + 1)
            if c + HOLD_BARS - 1 < S.RTH_END and c in sip and (c + HOLD_BARS - 1) in sip]
    if not elig:
        return None
    c = random.Random(int(hashlib.sha256(f"{sym}|{day}|{m}|{CTRL_TAG}".encode()).hexdigest()[:16], 16)).choice(elig)
    return S.net(sip, c, hold=HOLD_BARS, lag=0, cost=0.0)


def t_crit3(days: int) -> float | None:
    return S.t_ppf(1 - 0.025 / N_TESTS, days - 1) if days >= 2 else None


def count_step(rows: list[dict]) -> dict:
    """Per group and half: IN / OUT counts and days (>= 3 a side), projected MDE = (t_crit + 0.84) x the
    day-clustered SE of per-day (IN - OUT) means of the control-minute bar returns, judged against 5 bp."""
    out = {}
    for g in ("G1", "G2", "G3"):
        out[g] = {}
        for half in ("A", "B"):
            per = {"IN": defaultdict(list), "OUT": defaultdict(list)}
            for r in rows:
                s_ = r["groups"][g]
                if r["half"] == half and s_ and r["ctrl"] is not None:
                    per[s_][r["day"]].append(r["ctrl"])
            n_in, n_out = (sum(len(v) for v in per[k].values()) for k in ("IN", "OUT"))
            days = [d for d in per["IN"] if len(per["IN"][d]) >= 3 and len(per["OUT"][d]) >= 3]
            diffs = [sum(per["IN"][d]) / len(per["IN"][d]) - sum(per["OUT"][d]) / len(per["OUT"][d]) for d in days]
            se = (statistics.stdev(diffs) / math.sqrt(len(diffs))) if len(diffs) >= 2 else None
            tc = t_crit3(len(diffs))
            mde = (tc + 0.84) * se if se is not None and tc is not None else None
            out[g][half] = {"n_in": n_in, "n_out": n_out, "days": len(days), "t_crit": tc, "projected_mde": mde,
                            "mde_le_5bp": mde is not None and mde <= 0.0005,
                            "floors_met": n_in >= FLOORS[g] and len(days) >= 20}
    return out


def build_rows(entries: list[dict], tick_dir: str = TICK_DIR) -> tuple[list[dict], Counter]:
    """Features for every cached entry (no outcome), plus the control bar return from the warehouse."""
    import pickle
    st, rows, by_day = Counter(), [], defaultdict(list)
    for e in entries:
        by_day[e["day"]].append(e)
    for day in sorted(by_day):
        sip = H.load_raw("sip", day)
        for e in by_day[day]:
            path = os.path.join(tick_dir, entry_key(e) + ".pkl")
            if not os.path.exists(path):
                st["not_fetched"] += 1
                continue
            T = decision_ts(e["day"], e["m"])
            tick = pickle.load(open(path, "rb"))
            f = features(tick, T)
            f["capped"] = capped(tick) and not tick.get("paged")
            if not f["entry_quote"]:
                st["no_entry_quote"] += 1
                continue
            st["ofi_missing"] += f["ofi60"] is None
            st["qi_missing"] += f["qi"] is None
            ctrl = control_return(e["sym"], day, e["m"], H.sip_oc(sip[e["sym"]])) if e["sym"] in sip else None
            rows.append({**e, **f, "ctrl": ctrl})
    return rows, st



# ── outcomes (fetched only after the count step; paged) ──────────────────────

OUTCOME_DIR = os.path.join(OUT_DIR, "outcomes")
HORIZONS, PRIMARY_H = (60, 120, 180, 300), 180
OUT_BEFORE_S, OUT_AFTER_S = 30, 301                # one window [T + 1 - 30 s, T + 1 + 300 s] (budget_restated)


def fetch_outcome_quotes(cl, sym: str, T: float, sleep=time.sleep, tries: int = 5) -> list:
    """NBBO quotes over [T + 1 - 30 s, T + 1 + 300 s], paged (no row cap; amended_4 (2))."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest

    def utc(x):
        return datetime.fromtimestamp(x, timezone.utc)
    for k in range(tries):
        try:
            qu = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=utc(T + 1 - OUT_BEFORE_S),
                                                        end=utc(T + OUT_AFTER_S), feed=DataFeed.SIP,
                                                        limit=None)).data.get(sym) or []
            sleep(REQ_GAP_S)
            return [(_ts(q), float(q.bid_price or 0), float(q.ask_price or 0), float(q.bid_size or 0),
                     float(q.ask_size or 0)) for q in qu]
        except Exception as e:  # noqa: BLE001
            print(f"  {sym} {int(T)} outcome try {k}: {type(e).__name__} {str(e)[:80]}", flush=True)
            sleep(10 * (k + 1))
    raise RuntimeError(f"outcome fetch failed for {sym} at {int(T)}")


def fetch_outcomes(entries: list[dict], cl, out_dir: str = OUTCOME_DIR, sleep=time.sleep, log=print) -> Counter:
    """Resumable, atomic per-entry cache; aborts when > 2% of attempted entries fail after FAIL_MIN_ATTEMPTS."""
    os.makedirs(out_dir, exist_ok=True)
    st = Counter()
    for i, e in enumerate(entries):
        path = os.path.join(out_dir, entry_key(e) + ".pkl")
        if os.path.exists(path):
            st["cached"] += 1
            continue
        st["attempted"] += 1
        try:
            qs = fetch_outcome_quotes(cl, e["sym"], decision_ts(e["day"], e["m"]), sleep=sleep)
        except RuntimeError as ex:
            st["failed"] += 1
            log(f"  FAILED {entry_key(e)}: {ex}")
            if st["attempted"] >= FAIL_MIN_ATTEMPTS and st["failed"] / st["attempted"] > FAIL_ABORT:
                log(f"ABORT: {st['failed']} of {st['attempted']} entries failed")
                st["aborted"] = 1
                return st
            continue
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(qs, f)
        os.replace(tmp, path)
        st["fetched"] += 1
        if st["fetched"] % 500 == 0:
            log(f"  {i + 1}/{len(entries)}: fetched {st['fetched']}, cached {st['cached']}, failed {st['failed']} "
                f"({datetime.now(E.ET):%H:%M} ET)")
    return st


def outcomes_for(quotes: list, T: float) -> dict:
    """Entry at the ask of the last clean quote <= T + 1 s (<= 30 s old); at each horizon H the last clean quote
    <= T + 1 + H (<= 30 s old): gross = mid / entry mid - 1, net = bid / entry ask - 1. Also the max bid over
    (T + 1, T + 1 + 180] and the entry spread (information). Missing pieces are None."""
    qs = clean_quotes(quotes)
    q0 = last_quote(qs, T + 1, READ_MAX_AGE)
    if q0 is None:
        return {"entry": False}
    _t, b0, a0, _bs, _as = q0
    mid0 = (a0 + b0) / 2
    out = {"entry": True, "spread": (a0 - b0) / mid0}
    for h in HORIZONS:
        q = last_quote(qs, T + 1 + h, READ_MAX_AGE)
        if q is None:
            out[f"gross{h}"] = out[f"net{h}"] = None
            continue
        out[f"gross{h}"] = ((q[1] + q[2]) / 2) / mid0 - 1
        out[f"net{h}"] = q[1] / a0 - 1
    path_bids = [q[1] for q in qs if T + 1 < q[0] <= T + 1 + PRIMARY_H]
    out["mfe_bid"] = (max(path_bids) / a0 - 1) if path_bids else None
    return out


# ── score ────────────────────────────────────────────────────────────────────

LIFT_BAR = 0.0005


def _day_t(vals: dict) -> tuple[float | None, float | None, int]:
    xs = [v for v in vals.values() if v is not None]
    if len(xs) < 2:
        return (xs[0] if xs else None), None, len(xs)
    mu, se = statistics.mean(xs), statistics.stdev(xs) / math.sqrt(len(xs))
    return mu, (mu / se if se > 0 else None), len(xs)


def gated_minus_rest(rows: list[dict], g: str, half: str, key: str, drop_names=frozenset(), drop_days=frozenset(),
                     side_fn=None) -> dict:
    """Per day: IN mean - OUT mean of `key` (days with >= 3 a side), day-equal, day-clustered t; IN mean net (at the
    primary horizon) day-equal; name-day-equal difference."""
    side_fn = side_fn or (lambda r: r["groups"][g])
    per = {"IN": defaultdict(list), "OUT": defaultdict(list)}
    nd = {"IN": defaultdict(lambda: defaultdict(list)), "OUT": defaultdict(lambda: defaultdict(list))}
    net_in = defaultdict(list)
    for r in rows:
        s_ = side_fn(r)
        if r["half"] != half or not s_ or r.get(key) is None or r["sym"] in drop_names or r["day"] in drop_days:
            continue
        per[s_][r["day"]].append(r[key])
        nd[s_][r["day"]][r["sym"]].append(r[key])
        if s_ == "IN" and r.get(f"net{PRIMARY_H}") is not None:
            net_in[r["day"]].append(r[f"net{PRIMARY_H}"])
    days = sorted(d for d in per["IN"] if len(per["IN"][d]) >= 3 and len(per["OUT"][d]) >= 3)
    diff = {d: statistics.mean(per["IN"][d]) - statistics.mean(per["OUT"][d]) for d in days}
    ndd = {d: statistics.mean(statistics.mean(v) for v in nd["IN"][d].values())
           - statistics.mean(statistics.mean(v) for v in nd["OUT"][d].values()) for d in days}
    mu, t, n = _day_t(diff)
    xs = list(diff.values())
    se = statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) >= 2 else None
    return {"diff": mu, "t": t, "days": n, "n_in": sum(len(v) for v in per["IN"].values()),
            "n_out": sum(len(v) for v in per["OUT"].values()), "nameday": _day_t(ndd)[0],
            "in_net": _day_t({d: statistics.mean(v) for d, v in net_in.items() if v})[0],
            "t_crit": t_crit3(n), "per_day": diff,
            "ub": (mu + S.t_ppf(0.975, n - 1) * se) if (mu is not None and se is not None) else None}


def grade_group(rows: list[dict], g: str, half: str) -> dict:
    key = f"gross{PRIMARY_H}"
    res = gated_minus_rest(rows, g, half, key)
    # drop-3 robustness: names / days with the largest share of the difference
    contrib, cnt = Counter(), Counter()
    live = [r for r in rows if r["half"] == half and r["groups"][g] and r.get(key) is not None
            and r["day"] in res["per_day"]]
    for r in live:
        cnt[(r["day"], r["groups"][g])] += 1
    for r in live:
        s_ = r["groups"][g]
        w = r[key] / cnt[(r["day"], s_)] / max(1, len(res["per_day"]))
        contrib[r["sym"]] += w if s_ == "IN" else -w
    top_names = frozenset(n for n, _ in contrib.most_common(3))
    top_days = frozenset(sorted(res["per_day"], key=lambda d: res["per_day"][d], reverse=True)[:3])
    res["drop_names"] = gated_minus_rest(rows, g, half, key, drop_names=top_names)["diff"]
    res["drop_days"] = gated_minus_rest(rows, g, half, key, drop_days=top_days)["diff"]
    res.pop("per_day")
    return res


def half_verdict(r: dict, g: str) -> str:
    if r["n_in"] < FLOORS[g] or r["days"] < 20 or r["t"] is None:
        return "UNDERPOWERED"
    if r["t"] <= -2.0:
        return "FAIL"
    ok = (r["diff"] >= LIFT_BAR and r["t"] >= r["t_crit"] and r["in_net"] is not None and r["in_net"] > 0
          and r["nameday"] is not None and r["nameday"] > 0 and r["drop_names"] is not None and r["drop_names"] >= 0
          and r["drop_days"] is not None and r["drop_days"] >= 0)
    return "PASS" if ok else "NOT_PASSED"


def group_verdict(halves: list[str], ubs: list) -> str:
    if "FAIL" in halves:
        return "FAIL"
    if all(h == "PASS" for h in halves):
        return "PASS"
    if all(h != "UNDERPOWERED" for h in halves) and all(u is not None and u < LIFT_BAR for u in ubs):
        return "FAIL"
    return "UNDERPOWERED"


def score_rows(features: dict, out_dir: str = OUTCOME_DIR) -> tuple[list[dict], Counter]:
    st, rows = Counter(), []
    for e in features["entries"]:
        path = os.path.join(out_dir, entry_key(e) + ".pkl")
        if not os.path.exists(path):
            st["not_fetched"] += 1
            continue
        qs = pickle.load(open(path, "rb"))
        if not clean_quotes(qs):
            st["empty_file"] += 1
        o = outcomes_for(qs, decision_ts(e["day"], e["m"]))
        if not o["entry"]:
            st["no_entry_quote"] += 1
            continue
        for h in HORIZONS:
            st[f"missing_h{h}"] += o[f"gross{h}"] is None
        rows.append({**e, **o})
    return rows, st


def tercile_cuts(values: list[float]) -> tuple[float, float] | None:
    v = sorted(x for x in values if x is not None)
    return (v[len(v) // 3], v[(2 * len(v)) // 3]) if len(v) >= 3 else None


def onset_returns(rows: list[dict]) -> dict:
    """The onset bar's own SIP return (bar m - 1, open -> close) per entry key, from the warehouse (outcome-blind)."""
    out, by_day = {}, defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    for day, rs in sorted(by_day.items()):
        sip = H.load_raw("sip", day)
        for r in rs:
            b = H.bars_dict(sip[r["sym"]]).get(r["m"] - 1) if r["sym"] in sip else None
            out[entry_key(r)] = (b[3] / b[0] - 1) if b and b[0] else None
    return out


def information(rows: list[dict], features: dict, onset: dict | None = None) -> dict:
    info = {}
    for h in HORIZONS:
        if h == PRIMARY_H:
            continue
        info[f"H={h}s gross"] = {g: {hf: _bp_t(gated_minus_rest(rows, g, hf, f"gross{h}")) for hf in ("A", "B")}
                                 for g in ("G1", "G2", "G3")}
    # MICRO: cut on features.json half A (all scored-feature entries, before outcome drops), tie rule as amended_4
    fa = [e for e in features["entries"] if e["half"] == "A" and e.get("micro") is not None]
    cut = top_cut([(e["micro"], tie_u(e)) for e in fa])
    micro_fn = (lambda r: None if r.get("micro") is None or cut is None
                else ("IN" if (r["micro"], tie_u(r)) >= tuple(cut) else "OUT"))
    info["MICRO top tercile"] = {hf: _bp_t(gated_minus_rest(rows, "G1", hf, f"gross{PRIMARY_H}", side_fn=micro_fn))
                                 for hf in ("A", "B")}
    info["by hour (global cuts)"] = {
        f"{hr}:00": {g: {hf: _bp_t(gated_minus_rest([r for r in rows if r["m"] // 60 == hr], g, hf,
                                                     f"gross{PRIMARY_H}")) for hf in ("A", "B")}
                     for g in ("G1", "G2", "G3")} for hr in range(10, 16)}
    # terciles cut WITHIN each hour (half A features), G1 / G2 pooled over hours: the time-of-day confound
    hour_cuts = {}
    for hr in range(10, 16):
        ea = [e for e in features["entries"] if e["half"] == "A" and e["m"] // 60 == hr]
        hour_cuts[hr] = {k: top_cut([(e[k], tie_u(e)) for e in ea if e.get(k) is not None]) for k in ("ofi60", "qi")}

    def within_hour(k):
        def fn(r):
            c = hour_cuts.get(r["m"] // 60, {}).get(k)
            return None if r.get(k) is None or c is None else ("IN" if (r[k], tie_u(r)) >= tuple(c) else "OUT")
        return fn
    info["terciles cut within each hour"] = {
        g: {hf: _bp_t(gated_minus_rest(rows, g, hf, f"gross{PRIMARY_H}", side_fn=within_hour(k))) for hf in ("A", "B")}
        for g, k in (("G1", "ofi60"), ("G2", "qi"))}
    # OFI60 within terciles of the onset bar's own return: is a G1 result order flow or a big onset bar?
    if onset is not None:
        oc = tercile_cuts([onset.get(entry_key(r)) for r in rows if r["half"] == "A"])
        cell = {}
        if oc:
            for name, lo, hi in (("low", -1e9, oc[0]), ("mid", oc[0], oc[1]), ("high", oc[1], 1e9)):
                sub = [r for r in rows if onset.get(entry_key(r)) is not None and lo <= onset[entry_key(r)] < hi]
                cell[f"onset return {name}"] = {hf: _bp_t(gated_minus_rest(sub, "G1", hf, f"gross{PRIMARY_H}"))
                                                for hf in ("A", "B")}
        info["G1 within onset-bar-return terciles"] = {"cuts (half A)": oc, **cell}
    mfe = {}
    for g in ("G1", "G2", "G3"):
        for s_ in ("IN", "OUT"):
            xs = [r for r in rows if r["groups"][g] == s_ and r.get("mfe_bid") is not None and r.get("spread")]
            mfe[f"{g} {s_}"] = (sum(1 for r in xs if r["mfe_bid"] > 2 * r["spread"]) / len(xs)) if xs else None
    info["share whose 3-min max BID clears the entry ask by > 2x the spread (net excursion)"] = mfe
    info["not computed"] = {"IEX-only trade signing": "needs the if_pass feed=iex fetch (amended_4 (6))"}
    info["note"] = "gated net averages every day with an IN entry, not only the days graded for the difference"
    return info


def _bp_t(r: dict) -> str:
    return "-" if r["diff"] is None else f"{r['diff'] * 1e4:+.1f} bp (t {'-' if r['t'] is None else round(r['t'], 2)}, n_in {r['n_in']})"


def report(res: dict) -> str:
    def bp(x):
        return "-" if x is None else f"{x * 1e4:+.1f}"
    L_ = ["# Order-flow pilot: result", "",
          "Prereg: docs/studies/order_flow_pilot_prereg.json (amended .. amended_4, budget_restated). PRE entries, SIP "
          "ticks; gated (top tercile) minus rest on gross 180 s mid-to-mid; gated net = ask in, bid out.", "",
          f"Rows {res['rows']}, drops {res['drops']}.", "",
          "| group | half | IN | OUT | days | gated - rest (bp) | t | t_crit | 97.5% UB | gated net | name-day | drop 3 names / days | half |",
          "|" + "---|" * 13]
    for g, hs in res["graded"].items():
        for hf, r in hs.items():
            L_.append(f"| {g} | {hf} | {r['n_in']} | {r['n_out']} | {r['days']} | {bp(r['diff'])} | "
                      f"{'-' if r['t'] is None else round(r['t'], 2)} | {'-' if r['t_crit'] is None else round(r['t_crit'], 2)} | "
                      f"{bp(r['ub'])} | {bp(r['in_net'])} | {bp(r['nameday'])} | {bp(r['drop_names'])} / {bp(r['drop_days'])} | "
                      f"{res['halves'][g][hf]} |")
    L_ += ["", "| group | verdict |", "|---|---|"] + [f"| {g} | **{v}** |" for g, v in res["verdicts"].items()]
    L_ += ["", "## Information (cannot pass)", "", "```", json.dumps(res["info"], indent=1, default=str), "```"]
    return "\n".join(L_)


def module_shas() -> dict[str, str]:
    """sha256 of this script and the modules whose code shapes the entries and features (skeptic r1 #8)."""
    import signals
    mods = {"order_flow_pilot": sys.modules[__name__], "indicator_confirm_hist": H, "indicator_confirm_study": S,
            "early_entry_honest": E, "bars_structure": BS, "signals": signals}
    return {k: hashlib.sha256(open(os.path.abspath(m.__file__), "rb").read()).hexdigest() for k, m in mods.items()}


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
    g.add_argument("--fetch-features", action="store_true")
    g.add_argument("--count", action="store_true")
    g.add_argument("--refetch-capped", action="store_true")
    g.add_argument("--fetch-outcomes", action="store_true")
    g.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    if a.fetch_features:
        smp = json.load(open(os.path.join(OUT_DIR, "sample.json")))["entries"]
        print(f"feature fetch: {len(smp)} entries ({datetime.now(E.ET):%H:%M} ET)", flush=True)
        st = fetch_features(smp, data_client(), log=lambda x: print(x, flush=True))
        print(f"feature fetch done: {dict(st)} ({datetime.now(E.ET):%H:%M} ET)")
        if st.get("aborted"):
            raise SystemExit(1)
        return
    if a.fetch_outcomes:
        feats = json.load(open(os.path.join(OUT_DIR, "features.json")))
        if feats.get("module_sha256") is None:
            raise SystemExit("features.json missing its module hashes; rerun --count")
        print(f"outcome fetch: {len(feats['entries'])} entries ({datetime.now(E.ET):%H:%M} ET)", flush=True)
        st = fetch_outcomes(feats["entries"], data_client(), log=lambda x: print(x, flush=True))
        print(f"outcome fetch done: {dict(st)} ({datetime.now(E.ET):%H:%M} ET)")
        if st.get("aborted"):
            raise SystemExit(1)
        return
    if a.score:
        if not a.skeptic_ready:
            raise SystemExit("--score needs --skeptic-ready (the result-skeptic READY on this script)")
        feats = json.load(open(os.path.join(OUT_DIR, "features.json")))
        if feats.get("module_sha256") != module_shas():
            raise SystemExit("features.json was written by a different script or module version; rerun --count")
        rows, st = score_rows(feats)
        n_all = len(feats["entries"])
        if st["not_fetched"] > FAIL_ABORT * n_all:
            raise SystemExit(f"{st['not_fetched']} entries without outcomes (> 2%); rerun --fetch-outcomes")
        # amended_5: a fetch fault is an empty file or a lost entry quote; a quiet stretch at the exit is data
        if st["no_entry_quote"] + st["empty_file"] > FAIL_ABORT * n_all:
            raise SystemExit(f"fetch fault: {st['no_entry_quote']} entries lost their entry quote and "
                             f"{st['empty_file']} outcome files are empty (> 2%)")
        miss = Counter(f"{g} {r['groups'][g]}" for r in rows for g in ("G1", "G2", "G3")
                       if r["groups"][g] and r.get(f"gross{PRIMARY_H}") is None)
        tot = Counter(f"{g} {r['groups'][g]}" for r in rows for g in ("G1", "G2", "G3") if r["groups"][g])
        share = {k: miss[k] / tot[k] for k in tot}
        print(f"missing primary horizon by group and side: {dict(miss)}; share {share}")
        for g in ("G1", "G2", "G3"):
            if abs(share.get(f"{g} IN", 0) - share.get(f"{g} OUT", 0)) > 0.02:
                raise SystemExit(f"{g}: missing share differs between IN and OUT by > 2 pp: {share}")
        graded, halves, verdicts = {}, {}, {}
        for g in ("G1", "G2", "G3"):
            graded[g] = {hf: grade_group(rows, g, hf) for hf in ("A", "B")}
            halves[g] = {hf: half_verdict(graded[g][hf], g) for hf in ("A", "B")}
            verdicts[g] = group_verdict([halves[g][h] for h in ("A", "B")], [graded[g][h]["ub"] for h in ("A", "B")])
        res = {"rows": len(rows), "drops": dict(st), "graded": graded, "halves": halves, "verdicts": verdicts,
               "info": information(rows, feats, onset_returns(rows)), "missing_h180_by_side": dict(miss),
               "count_rerun_identical": feats.get("identical_to_previous"),
               "skeptic_ready": a.skeptic_ready, "module_sha256": module_shas()}
        md = report(res)
        print(md)
        open(os.path.join(OUT_DIR, "result.md"), "w").write(md + "\n")
        write("result.json", res)
        return
    if a.refetch_capped:
        smp = json.load(open(os.path.join(OUT_DIR, "sample.json")))["entries"]
        print(f"refetch capped: {dict(refetch_capped(smp, data_client(), log=lambda x: print(x, flush=True)))}")
        return
    if a.count:
        smp = json.load(open(os.path.join(OUT_DIR, "sample.json")))["entries"]
        rows, st = build_rows(smp)
        # skeptic r1 #7: after --fetch-features has been rerun once (a retry pass), entries still missing are dropped
        # and counted; more than 2% missing means the fetch is unfinished or failing
        if st["not_fetched"] > FAIL_ABORT * len(smp):
            raise SystemExit(f"{st['not_fetched']} entries not fetched (> 2%); rerun --fetch-features (retry pass) first")
        capped_left = [r for r in rows if r.get("capped")]
        if capped_left:
            raise SystemExit(f"{len(capped_left)} entries hit the {ROW_CAP}-row cap; run --refetch-capped first")
        half_a = [r for r in rows if r["half"] == "A"]
        cuts = {k: top_cut([(r[k], tie_u(r)) for r in half_a if r[k] is not None]) for k in ("ofi60", "qi")}
        for r in rows:
            r["groups"] = groups(r, cuts)
        cnt = count_step(rows)
        share = {k: {h: (sum(1 for r in rows if r["half"] == h and r["groups"][g] == "IN") /
                         max(1, sum(1 for r in rows if r["half"] == h and r["groups"][g])))
                     for h in ("A", "B")} for k, g in (("ofi60", "G1"), ("qi", "G2"))}
        print(f"IN share by half (half A is a third by construction): {share}")
        print(f"rows {len(rows)}, drops {dict(st)}, cuts (half A top tercile) {cuts}")
        for g, hs in cnt.items():
            for h, c in hs.items():
                print(f"  {g} {h}: IN {c['n_in']} OUT {c['n_out']} days {c['days']} projected MDE "
                      f"{H._bp(c['projected_mde'])} bp "
                      f"(<= 5 bp: {c['mde_le_5bp']}), floors {c['floors_met']}")
        print(write("count.json", {"module_sha256": module_shas(), "rows": len(rows), "drops": dict(st), "cuts": cuts,
                                   "in_share": share, "groups": cnt,
                                   "note": "features and control bar returns only; no tick outcome read"}))
        fpath = os.path.join(OUT_DIR, "features.json")
        new_entries = [{k: r[k] for k in ("day", "sym", "m", "half", "ofi60", "qi", "micro", "n_signed", "groups")}
                       for r in rows]
        same = None
        if os.path.exists(fpath):
            old = json.load(open(fpath))
            same = (json.loads(json.dumps(old.get("cuts"))) == json.loads(json.dumps(cuts))
                    and [(e["day"], e["sym"], e["m"], e["groups"]) for e in old["entries"]]
                    == [(e["day"], e["sym"], e["m"], e["groups"]) for e in new_entries])
            tag = (old.get("module_sha256") or {}).get("order_flow_pilot", "unknown")[:12]
            write(f"features_{tag}.json", old)
            print(f"rerun identical to the previous features.json ({tag}): {same}")
        print(write("features.json", {"module_sha256": module_shas(), "cuts": cuts, "identical_to_previous": same,
                                      "entries": new_entries}))
        return
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
        cov = coverage(tr, qu, t)
        rows.append({**e, "T": t, "tapes": sorted(inv), **cov})
        print(f"  {e['day']} {e['sym']} {e['m'] // 60:02d}:{e['m'] % 60:02d}: {cov['trades']} trades ({cov['kept']} kept, "
              f"{cov['signable']} signable), {cov['quotes']} quotes, QI quote {cov['qi_quote']}, entry quote "
              f"{cov['entry_quote']}, requests {cov['requests']}", flush=True)
    total = merge(invs)
    print_inventory(total)
    n = len(rows)
    ok_g1 = sum(1 for r in rows if r["signable"] >= 5)
    req = sum(r["requests"] for r in rows) / n + OUTCOME_HORIZONS
    print(f"\ncoverage: kept trades {sum(r['kept'] for r in rows)}/{sum(r['trades'] for r in rows)}, signable "
          f"{sum(r['signable'] for r in rows)}/{sum(r['kept'] for r in rows)}; entries with >= 5 signed trades {ok_g1}/{n}; "
          f"QI quote {sum(r['qi_quote'] for r in rows)}/{n}; entry quote {sum(r['entry_quote'] for r in rows)}/{n}")
    print(f"budget: ~{req:.1f} requests/entry (features measured + {OUTCOME_HORIZONS} outcome windows) -> "
          f"~{req * SAMPLE_N:,.0f} for {SAMPLE_N:,} entries, ~{req * SAMPLE_N / 150 / 60:.1f} h at 150/min")
    print(write("prestep.json", {"amendment": "amended_3", "entries": rows, "inventory": total,
                                 "note": "no feature or outcome computed; the keep/drop condition list is registered "
                                         "from this inventory as a prereg amendment before any feature fetch"}))


if __name__ == "__main__":
    main()
