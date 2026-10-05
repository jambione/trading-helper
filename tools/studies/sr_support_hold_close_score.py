#!/usr/bin/env python3
"""FORWARD (held-out) score: support-touch arm + hold to 15:55 on live-desk recordings.

Pre-registration (locked BEFORE any 10/06+ session existed):
  docs/studies/sr_support_hold_close_forward_prereg.json   (commit c1e9c30)
  docs/studies/SR_SUPPORT_HOLD_CLOSE_FORWARD_PREREG.md

Reads ONLY the desk's own recordings under ai_reports/sessions/<YYYY-MM-DD>/
(decisions, prints, wire, inputs .jsonl.gz). No network, no broker, no Databento,
no config, no secrets.

Modes
  --dry-run        COUNTS ONLY (sessions, seated names, usable name-days, support arms,
                   desk arms, usable quotes, arms per half, stopping-rule status).
                   Computes NO outcomes. Any date allowed (10/05 = parse check).
  (default) score  Refuses any session before 2026-10-06. Computes outcomes ONLY when the
                   locked stopping rule is met: >= 10 held-out sessions AND >= 100 support
                   arms in each chronological half, or >= 20 sessions (then a half with
                   < 100 arms is UNDERPOWERED). Otherwise prints counts and exits.

Notes on the locked rules as implemented
  * seated = symbol appears in an ev=="arm" decision row; eligible from its first row ts.
  * first-touch bookkeeping per block is tracked from 09:40 regardless of seating; only a
    decision close at/after the seat time can arm.
  * prior-day history = previous WEEKDAY session directory with prints (weekend dirs skipped).
  * quotes: wire GET /v2/stocks/quotes/latest and /v2/stocks/snapshots (latestQuote) with
    feed=iex only (the recorded quote history endpoint is SIP and is not used).
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import os
import random
import statistics
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
from sr_support_arm import ND, is_sup, is_res, bkey, et_min, tstat  # noqa: E402

ET = ZoneInfo("America/New_York")
SESS = os.path.join(ROOT, "ai_reports", "sessions")
HELD_OUT_START = "2026-10-06"
PREREG = "c1e9c30"
ROOM_MIN = 0.40
WIN_LO, WIN_HI = 9 * 60 + 40, 15 * 60 + 30
REFRACT = 15
MIN_RTH_MIN = 200
SEED = 43
COST_FLOOR = 0.0020
QUOTE_LOOKBACK = 120
MAX_SPREAD = 0.03
MIN_SESS, MIN_ARMS, HARD_STOP = 10, 100, 20
INF = 10 ** 9


# ----------------------------------------------------------------------------- io
def jl(path):
    with gzip.open(path, "rt") as f:
        for line in f:
            if line.strip():
                yield line


def iso_ts(s):
    s = s.rstrip("Z")
    if "." in s:
        a, b = s.split(".", 1)
        s = a + "." + b[:6]
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


def session_dirs():
    out = []
    for d in sorted(os.listdir(SESS)):
        p = os.path.join(SESS, d)
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
        if dt.weekday() >= 5:
            continue
        if os.path.exists(os.path.join(p, "decisions.jsonl.gz")) and os.path.exists(os.path.join(p, "prints.jsonl.gz")):
            out.append(d)
    return out


def load_decisions(day):
    seat, subs = {}, []
    for line in jl(os.path.join(SESS, day, "decisions.jsonl.gz")):
        if '"arm"' not in line:
            continue
        r = json.loads(line)
        if r.get("ev") != "arm":
            continue
        ts = float(r["ts"])
        for row in r.get("rows") or []:
            s = row.get("s")
            if not s:
                continue
            seat.setdefault(s, ts)
            seat[s] = min(seat[s], ts)
            if row.get("st") == "submitted":
                subs.append((s, ts))
    subs.sort(key=lambda x: (x[0], x[1]))
    arms, last = [], {}
    for s, ts in subs:                                   # rows of one symbol within 60 s = one arm
        if s in last and ts - last[s] <= 60:
            last[s] = ts
            continue
        last[s] = ts
        arms.append((s, ts))
    return seat, arms


def load_prints(day, syms):
    ticks = collections.defaultdict(list)
    for line in jl(os.path.join(SESS, day, "prints.jsonl.gz")):
        k = line.find('"symbol"')
        if k < 0:
            continue
        a = line.find('"', line.find(":", k) + 1) + 1
        s = line[a:line.find('"', a)]
        if s not in syms:
            continue
        r = json.loads(line)
        px = r.get("price")
        t = r.get("trade_ts") or r.get("ts")
        if px is None or t is None or not px > 0:
            continue
        ticks[s].append((float(t), float(px)))
    bars = {}
    for s, tk in ticks.items():
        tk.sort()
        b = {}
        for t, px in tk:
            m = int(t // 60) * 60
            if m not in b:
                b[m] = [px, px, px, px]
            else:
                x = b[m]
                x[1] = max(x[1], px)
                x[2] = min(x[2], px)
                x[3] = px
        bars[s] = b
    return bars


def load_wire(day, syms):
    """IEX 1-min bars and IEX quotes (quotes/latest, snapshots latestQuote) for syms."""
    wbars = collections.defaultdict(dict)
    quotes = collections.defaultdict(list)
    path = os.path.join(SESS, day, "wire.jsonl.gz")
    if not os.path.exists(path):
        return wbars, quotes
    for line in jl(path):
        if "/v2/stocks/" not in line:
            continue
        r = json.loads(line)
        p, q, resp = r.get("p") or "", r.get("q") or {}, r.get("r")
        if not isinstance(q, dict) or q.get("feed") != "iex" or not isinstance(resp, dict):
            continue
        if p.endswith("/v2/stocks/bars") and q.get("timeframe") == "1Min":
            for s, lst in (resp.get("bars") or {}).items():
                if s in syms:
                    for b in lst or []:
                        try:
                            wbars[s][int(iso_ts(b["t"]))] = [b["o"], b["h"], b["l"], b["c"]]
                        except (KeyError, ValueError, TypeError):
                            pass
        elif p.endswith("/quotes/latest"):
            for s, qt in (resp.get("quotes") or {}).items():
                if s in syms and isinstance(qt, dict):
                    _addq(quotes, s, qt)
        elif p.endswith("/snapshots"):
            snap = resp.get("snapshots") if isinstance(resp.get("snapshots"), dict) else resp
            for s, v in snap.items():
                if s in syms and isinstance(v, dict) and isinstance(v.get("latestQuote"), dict):
                    _addq(quotes, s, v["latestQuote"])
    for s in quotes:
        quotes[s] = sorted(set(quotes[s]))
    return wbars, quotes


def _addq(quotes, s, qt):
    try:
        quotes[s].append((iso_ts(qt["t"]), float(qt.get("bp") or 0), float(qt.get("ap") or 0)))
    except (KeyError, ValueError, TypeError):
        pass


def load_sip_spread(day, syms):
    out = collections.defaultdict(list)
    path = os.path.join(SESS, day, "inputs.jsonl.gz")
    if not os.path.exists(path):
        return out
    for line in jl(path):
        if "sip_spread" not in line:
            continue
        r = json.loads(line)
        if r.get("kind") == "sip_spread" and r.get("symbol") in syms and isinstance(r.get("value"), (int, float)):
            out[r["symbol"]].append((float(r["ts"]), float(r["value"])))
    for s in out:
        out[s].sort()
    return out


def merged_rows(pbars, wbars):
    b = dict(wbars or {})
    b.update(pbars or {})                                # prints first; IEX bars fill gaps only
    return [(t, *b[t]) for t in sorted(b)]


def rth_minutes(rows, day):
    n = 0
    for r in rows:
        d = datetime.fromtimestamp(r[0], ET)
        if d.strftime("%Y-%m-%d") == day and 9 * 60 + 30 <= d.hour * 60 + d.minute < 16 * 60:
            n += 1
    return n


def spread_at(quotes, t):
    best = None
    for qt, bp, ap in quotes:
        if t - QUOTE_LOOKBACK <= qt <= t:
            best = (bp, ap)
        elif qt > t:
            break
    if not best:
        return None
    bp, ap = best
    if bp <= 0 or ap <= 0:
        return None
    mid = (ap + bp) / 2
    sp = (ap - bp) / mid
    return sp if 0 < sp <= MAX_SPREAD else None


def latest_before(series, t):
    v = None
    for ts, x in series:
        if ts <= t:
            v = x
        else:
            break
    return v


# ----------------------------------------------------------------------------- arms (structure only)
def last_idx_before_close(nd, ie):
    last = None
    for j in range(ie, len(nd.rows)):
        if nd.rows[j][0] >= nd.day_end:
            break
        last = j
    return last


def enterable(nd, i):
    ie = i + 1
    return ie < len(nd.rows) and nd.rows[ie][0] < nd.day_end and last_idx_before_close(nd, ie) is not None


def support_arms(nd, seat_ts):
    used, last, arms, drops = set(), -INF, [], collections.Counter()
    for i in nd.window(WIN_LO, WIN_HI):
        ch = nd.ch(i)
        touch = [b for b in ch if is_sup(b) and nd.l[i] <= b.top and nd.c[i] >= b.btm]
        new = [b for b in touch if bkey(b) not in used]
        for b in touch:
            used.add(bkey(b))
        if not new:
            continue
        if nd.closes[i] < seat_ts:
            drops["before_seated"] += 1
            continue
        S = max(new, key=lambda b: b.top)
        res = [b for b in ch if is_res(b)]
        if any(b.btm <= nd.c[i] <= b.top for b in res):
            drops["inside_resistance"] += 1
            continue
        ab = [b.btm for b in res if b.btm > nd.c[i]]
        if not ab:
            drops["no_resistance_above"] += 1
            continue
        room = (min(ab) / nd.c[i] - 1) * 100
        if room < ROOM_MIN:
            drops["room_below_min"] += 1
            continue
        m = et_min(nd.closes[i])
        if m - last < REFRACT:
            drops["refractory_15min"] += 1
            continue
        if not enterable(nd, i):
            drops["not_enterable"] += 1
            continue
        last = m
        arms.append({"i": i, "S_btm": S.btm, "room": room})
    return arms, drops


def random_minute(nd, i, seat_ts, rng):
    hr = et_min(nd.closes[i]) // 60
    pool = [k for k in nd.window(WIN_LO, WIN_HI)
            if k != i and et_min(nd.closes[k]) // 60 == hr and nd.closes[k] >= seat_ts and enterable(nd, k)]
    return rng.choice(pool) if pool else None


# ----------------------------------------------------------------------------- outcomes (score mode only)
def outcomes(nd, i, stop_btm=None):
    rows, ie = nd.rows, i + 1
    entry = rows[ie][1]
    last = last_idx_before_close(nd, ie)
    eod = rows[last][4] / entry - 1
    ch = nd.ch(i)
    above = [b.btm for b in ch if is_res(b) and b.btm > entry]
    tgt = min(above) if above else None
    stop = stop_btm * (1 - 0.001) if stop_btm is not None else None
    gs = gt = None
    for j in range(ie, last + 1):
        o, hi, lo = rows[j][1], rows[j][2], rows[j][3]
        if gs is None and stop is not None and lo <= stop:
            gs = min(stop, o) / entry - 1
        if gt is None and tgt is not None and hi >= tgt:
            gt = max(tgt, o) / entry - 1
        if gs is not None and gt is not None:
            break
    return {"close": eod, "stop": eod if gs is None else gs, "res": eod if gt is None else gt}


# ----------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--dry-run", action="store_true", help="counts only, no outcomes")
    a = ap.parse_args()

    alld = session_dirs()
    days = [d for d in alld if a.start <= d <= a.end]
    if not a.dry_run:
        early = [d for d in days if d < HELD_OUT_START]
        if early:
            print(f"REFUSED: score mode excludes sessions before {HELD_OUT_START} (pre-prereg): {early}")
            days = [d for d in days if d >= HELD_OUT_START]
    if not days:
        print("no usable sessions in range")
        return 0
    mode = "DRY-RUN (counts only, no outcomes)" if a.dry_run else "SCORE"
    print(f"sr_support_hold_close_score  prereg {PREREG}  mode={mode}  sessions={days}")

    rng = random.Random(SEED)
    parsed = []
    recs, cnt, drops_all = [], collections.Counter(), collections.Counter()
    for day in days:
        prev = [d for d in alld if d < day]
        prev = prev[-1] if prev else None
        try:
            seat, desk = load_decisions(day)
        except (OSError, EOFError, ValueError) as e:
            print(f"  {day}: unreadable decisions ({e.__class__.__name__}); skipped")
            continue
        syms = set(seat)
        try:
            pb = load_prints(day, syms)
        except (OSError, EOFError) as e:
            print(f"  {day}: unreadable prints ({e.__class__.__name__}); skipped")
            continue
        wb, qt = load_wire(day, syms)
        sip = load_sip_spread(day, syms)
        ppb, pwb = ({}, {}) if prev is None else (load_prints(prev, syms), load_wire(prev, syms)[0])
        cnt["sessions"] += 1
        parsed.append(day)
        cnt["seated"] += len(syms)
        cnt["desk_arm_rows_dedup"] += len(desk)
        nd_by = {}
        for s in sorted(syms):
            rows = merged_rows({**ppb.get(s, {}), **pb.get(s, {})}, {**pwb.get(s, {}), **wb.get(s, {})})
            if rth_minutes(rows, day) < MIN_RTH_MIN:
                cnt["name_days_excluded_lt200"] += 1
                continue
            cnt["name_days_usable"] += 1
            nd = ND(s, day, rows)
            nd_by[s] = nd
            arms, dr = support_arms(nd, seat[s])
            drops_all.update(dr)
            for arm in arms:
                k = random_minute(nd, arm["i"], seat[s], rng)
                t_entry = nd.rows[arm["i"] + 1][0]
                recs.append({"g": "support", "day": day, "s": s, "i": arm["i"], "S_btm": arm["S_btm"], "ctrl": k,
                             "spread": spread_at(qt.get(s, []), t_entry), "sip": latest_before(sip.get(s, []), t_entry)})
                if k is None:
                    cnt["support_arms_no_control"] += 1
        for s, ts in desk:
            nd = nd_by.get(s)
            if nd is None:
                cnt["desk_arms_no_usable_bars"] += 1
                continue
            i = nd.idx_of_close(ts)                      # last bar closed at/before the arm row
            if i < 0 or not enterable(nd, i):
                cnt["desk_arms_not_enterable"] += 1
                continue
            t_entry = nd.rows[i + 1][0]
            recs.append({"g": "desk", "day": day, "s": s, "i": i, "S_btm": None, "ctrl": None,
                         "spread": spread_at(qt.get(s, []), t_entry), "sip": latest_before(sip.get(s, []), t_entry)})
        for r in recs:
            if r["day"] == day:
                r["nd"] = nd_by[r["s"]]
        n_sup = sum(1 for r in recs if r["day"] == day and r["g"] == "support")
        n_desk = sum(1 for r in recs if r["day"] == day and r["g"] == "desk")
        print(f"  {day}: prev={prev} seated={len(syms)} usable_name_days={len(nd_by)} support_arms={n_sup} desk_arms={n_desk}")

    sdays = parsed
    n = len(sdays)
    A = set(sdays[: n // 2])
    half = lambda r: "A" if r["day"] in A else "B"      # noqa: E731
    sup = [r for r in recs if r["g"] == "support"]
    dsk = [r for r in recs if r["g"] == "desk"]
    nA = sum(1 for r in sup if half(r) == "A")
    nB = len(sup) - nA
    print("\nCOUNTS")
    for k in ("sessions", "seated", "name_days_usable", "name_days_excluded_lt200", "desk_arm_rows_dedup",
              "desk_arms_no_usable_bars", "desk_arms_not_enterable", "support_arms_no_control"):
        print(f"  {k:28s} {cnt[k]}")
    print(f"  {'support_arms':28s} {len(sup)}  (half A {nA} / half B {nB}; A = {sorted(A)})")
    print(f"  {'desk_arms':28s} {len(dsk)}")
    print(f"  {'support_quotes_usable':28s} {sum(1 for r in sup if r['spread'] is not None)}/{len(sup)}")
    print(f"  {'desk_quotes_usable':28s} {sum(1 for r in dsk if r['spread'] is not None)}/{len(dsk)}")
    print(f"  support-arm drops: {dict(drops_all)}")

    held = [d for d in sdays if d >= HELD_OUT_START]
    met = len(held) >= MIN_SESS and nA >= MIN_ARMS and nB >= MIN_ARMS
    hard = len(held) >= HARD_STOP
    print(f"\nSTOPPING RULE: held-out sessions {len(held)} (need {MIN_SESS}), arms/half {nA}/{nB} (need {MIN_ARMS}); "
          f"hard stop at {HARD_STOP} sessions -> {'MET' if met else ('HARD STOP' if hard else 'NOT MET')}")
    if a.dry_run:
        print("dry-run: no outcomes computed.")
        return 0
    if not (met or hard):
        print("NOT SCORABLE YET: counts only (prereg forbids looking at outcomes before the stopping rule).")
        return 0

    # ---------------- score (once)
    for r in recs:
        nd = r["nd"]
        r["o"] = outcomes(nd, r["i"], r["S_btm"])
        if r["g"] == "support" and r["ctrl"] is not None:
            r["c"] = outcomes(nd, r["ctrl"])["close"]
    bp = lambda x: f"{x * 1e4:+7.1f}"                   # noqa: E731
    verdicts = {}
    print("\nRESULTS (bp; t = day-clustered)  PRIMARY = support - paired same-hour random, held to 15:55")
    for h in ("A", "B"):
        S = [r for r in sup if half(r) == h]
        P = [r for r in S if "c" in r]
        D = [r for r in dsk if half(r) == h]
        sp = [r["spread"] for r in S if r["spread"] is not None]
        meas = statistics.median(sp) if sp else None
        cost = max(meas, COST_FLOOR) if meas is not None else COST_FLOOR
        sipv = [r["sip"] for r in S if r["sip"] is not None]
        print(f"\n half {h}: sessions {sorted({r['day'] for r in S})}")
        print(f"   measured IEX round trip (median spread, support entries) = "
              f"{'n/a (no usable quotes)' if meas is None else bp(meas).strip()}  n_quotes={len(sp)}; "
              f"charged = {bp(cost).strip()}; desk sip_spread input median (raw units) = "
              f"{statistics.median(sipv) if sipv else 'n/a'}")
        for name, xs in (("support 15:55", [r["o"]["close"] for r in S]),
                         ("random ctrl 15:55", [r["c"] for r in P]),
                         ("desk arms 15:55", [r["o"]["close"] for r in D]),
                         ("info: support stop", [r["o"]["stop"] for r in S]),
                         ("info: support res-touch", [r["o"]["res"] for r in S])):
            dd = ([r["day"] for r in S] if name.startswith(("support", "info")) else
                  [r["day"] for r in P] if name.startswith("random") else [r["day"] for r in D])
            m, t = tstat(xs, dd) if xs else (float("nan"), float("nan"))
            print(f"   {name:24s} n={len(xs):4d} gross={bp(m)} net={bp(m - cost)} t={t:5.2f}")
        lift = [r["o"]["close"] - r["c"] for r in P]
        m, t = tstat(lift, [r["day"] for r in P]) if lift else (float("nan"), float("nan"))
        ok = len(S) >= MIN_ARMS and lift and m > cost
        under = len(S) < MIN_ARMS
        print(f"   PRIMARY lift (support - random) n={len(lift)} {bp(m)} bp t={t:5.2f} vs charged {bp(cost)} -> "
              f"{'UNDERPOWERED' if under else ('beats cost' if ok else 'does not beat cost')}")
        for nm, key in (("info: stop - random", "stop"), ("info: res-touch - random", "res")):
            xs = [r["o"][key] - r["c"] for r in P]
            mm, tt = tstat(xs, [r["day"] for r in P]) if xs else (float("nan"), float("nan"))
            print(f"   {nm:24s} {bp(mm)} t={tt:5.2f}")
        xs = [r["o"]["close"] for r in S]
        ys = [r["o"]["close"] for r in D]
        if xs and ys:
            print(f"   info: support mean - desk-arm mean {bp(statistics.fmean(xs) - statistics.fmean(ys))}")
        verdicts[h] = "UNDERPOWERED" if under else ("PASS" if ok else "FAIL")
    v = ("UNDERPOWERED (no verdict)" if "UNDERPOWERED" in verdicts.values() else
         "PASS - PENDING SKEPTIC REVIEW" if all(x == "PASS" for x in verdicts.values()) else "FAIL")
    print(f"\nVERDICT: {v}   (halves: {verdicts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
