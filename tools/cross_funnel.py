#!/usr/bin/env python3
"""Cross funnel: where does every -50 cross on a seed name die?

Usage (on the mini, after ~16:16 ET; SIP 1m bars are 15 min delayed)::

    .venv/bin/python tools/cross_funnel.py [--day YYYY-MM-DD]

Needs the session recorder (ai_reports/sessions/<day>/sources.jsonl.gz) and
the state archive (~/session_snapshots/<day>/state_snapshots.jsonl.gz).
Read-only apart from a bar cache in ~/replay_cache.
Crosses: fast %R up through -50 with the slow line rising
(signals.compute_percent_r_exhaustion, live config) on SIP 1m bars from 04:00,
RTH 09:35-15:55, in band ($20-$100; momentum $1-$100). Seed = any of the
recorded sources (movers, trending, momentum, research boards) per the
recorder's enter/leave stream. For each cross, the live state at that moment:
  0 not a seed name then (dropped earlier / never listed)
  1 seed name, not seated (admission ledger's last refusal, or "listed <3 min")
  2 seated, refused at the arm (decision ledger, +-90 s; tape age at the cross)
  3 seated and opened
"""
import argparse
import bisect
import collections
import gzip
import json
import os
import pickle
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)
import signals  # noqa: E402
from config import load_config  # noqa: E402
import ai_entry_watch as ew  # noqa: E402

ET = ZoneInfo("America/New_York")
_ap = argparse.ArgumentParser()
_ap.add_argument("--day", default=datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"))
DAY = _ap.parse_args().day
SNAP = os.path.expanduser(f"~/session_snapshots/{DAY}")
REC = f"{REPO}/ai_reports/sessions/{DAY}"
CACHE = os.path.expanduser(f"~/replay_cache/funnel_bars_{DAY}.pkl")
cfg = load_config()


def at(hh, mm):
    y, m, d = map(int, DAY.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=ET).timestamp()


OPEN, T0, T1 = at(9, 30), at(9, 35), at(15, 55)
DATA = {"stale_quote", "engine_stale", "spread_unknown", "gap_unknown", "no_rsi_data",
        "rvol_pace_unknown", "no_structure", "tape_only", "stale_tape", "confirm_stale",
        "no_exhaustion_data", "pctr_not_live_alpaca", "pctr_not_live_missing"}
GATE = {"spread_wide", "gapped_down", "above_max_price", "below_min_price",
        "reentry_cooldown", "rvol_pace_low", "look_wash", "hard_no",
        "extended_cheap", "cheap_ob_band"}
RESEARCH = {"agy", "xai", "grok", "claude", "research"}


def fam(src):
    s = str(src or "").lower()
    if s.startswith("momentum") or s in ("discord", "watchlist"):
        return "momentum"
    if s in RESEARCH:
        return "research"
    return s or "?"


# ── seed membership over time (recorder enter/leave stream)
# A process restart re-logs the whole pool as "enter" at one instant and never
# logs "leave" for names that dropped while it was down (session_recorder
# record_source_set), so membership is a set per (symbol, source) and a
# full re-log burst replaces the pool instead of adding to it.
raw = []
with gzip.open(f"{REC}/sources.jsonl.gz", "rt") as f:
    for line in f:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        s = str(r.get("symbol") or "").upper()
        if s:
            raw.append((float(r["ts"]), s, str(r.get("source") or "").lower(), r.get("event")))
raw.sort()
member = collections.defaultdict(list)       # sym -> [(ts, raw source, True/False)]
first_listed = {}
active = set()                                # {(sym, src)}
relogs = []
i = 0
while i < len(raw):
    j = i
    while j < len(raw) and raw[j][0] == raw[i][0]:
        j += 1
    grp = raw[i:j]
    enters = {(s, src) for _, s, src, ev in grp if ev == "enter"}
    leaves = {(s, src) for _, s, src, ev in grp if ev == "leave"}
    if not leaves and len(enters) >= max(8, 0.6 * len(active)) and active:
        for key in active - enters:           # restart re-log: the rest left
            member[key[0]].append((grp[0][0], key[1], False))
        relogs.append((grp[0][0], len(enters), len(active - enters)))
        active = set(enters)
    else:
        active = (active | enters) - leaves
    for ts, s, src, ev in grp:
        member[s].append((ts, src, ev == "enter"))
        if ev == "enter":
            first_listed.setdefault(s, ts)
    i = j


def sources_at(sym, t):
    state = {}
    for ts, src, on in member.get(sym, []):
        if ts > t:
            break
        state[src] = on
    return {fam(k) for k, v in state.items() if v}


def ever_listed_before(sym, t):
    return any(ts <= t for ts, _, on in member.get(sym, []) if on)


# ── seated sets over time (state archive)
seat_t, seat_sets = [], []
with gzip.open(f"{SNAP}/state_snapshots.jsonl.gz", "rt") as f:
    for line in f:
        if '"ai_reports/entry_watch_state.json"' not in line[:200]:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        d = r.get("data")
        if isinstance(d, dict):
            seat_t.append(float(r.get("ts") or 0))
            seat_sets.append({str(k).upper() for k, v in d.items() if isinstance(v, dict)})
first_seated = {}
for ts, ss in zip(seat_t, seat_sets):
    for s in ss:
        first_seated.setdefault(s, ts)


def seated_at(sym, t):
    i = bisect.bisect_right(seat_t, t) - 1
    return i >= 0 and sym in seat_sets[i]


def was_seated_before(sym, t):
    return first_seated.get(sym, 1e18) <= t


# ── ledgers (live files; ledgers are also hard-linked into SNAP at 16:05)
def lpath(name):
    p = f"{SNAP}/{name}.jsonl"
    return p if os.path.exists(p) else f"{REPO}/ai_reports/{name}/{DAY}.jsonl"


dec = collections.defaultdict(list)
for line in open(lpath("decision_ledger")):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    dec[str(r.get("symbol") or "").upper()].append(
        (float(r["ts"]), r.get("arm_why"), r.get("tape_age_sec")))
fills = collections.defaultdict(list)
for line in open(lpath("fills")):
    r = json.loads(line)
    if r.get("event") == "fill" and r.get("side") == "buy":
        fills[r["symbol"].upper()].append(datetime.fromisoformat(r["filled_at"]).timestamp())
adm = collections.defaultdict(list)
for line in open(lpath("admit_ledger")):
    if '"symbol"' not in line:
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("kept") is False or r.get("reason"):
        adm[str(r.get("symbol") or "").upper()].append((float(r.get("ts") or 0), str(r.get("reason"))))
for v in adm.values():
    v.sort()

# ── SIP 1m bars 04:00-16:00 for every seed name
syms = sorted(s for s in member if s and "." not in s)
bars = {}
if os.path.exists(CACHE) and not os.getenv("FUNNEL_NOCACHE"):
    bars = pickle.load(open(CACHE, "rb"))
need = [s for s in syms if s not in bars]
if need:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    cl = ew._data_client()
    for i in range(0, len(need), 50):
        batch = need[i:i + 50]
        data = cl.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=batch, timeframe=TimeFrame.Minute,
            start=datetime.fromtimestamp(at(4, 0), timezone.utc),
            end=datetime.fromtimestamp(min(at(16, 0), time.time() - 16 * 60), timezone.utc),
            feed=DataFeed.SIP)).data
        for s in batch:
            bars[s] = [(b.timestamp.timestamp(), float(b.open), float(b.high), float(b.low),
                        float(b.close), float(b.volume)) for b in data.get(s) or []]
    if not os.getenv("FUNNEL_NOCACHE") and time.time() - 16 * 60 >= at(16, 0):
        pickle.dump(bars, open(CACHE, "wb"))

# ── crosses
events = []
for sym, seq in bars.items():
    if len(seq) < 150:
        continue
    df = pd.DataFrame([r[1:5] for r in seq], columns=["open", "high", "low", "close"])
    ind = signals.compute_percent_r_exhaustion(df, cfg)
    fast, slow = ind["s_percentR"].to_numpy(), ind["l_percentR"].to_numpy()
    for i in range(2, len(seq)):
        t = seq[i][0] + 60                      # bar closes -> signal
        if not (T0 <= t <= T1):
            continue
        if not (fast[i - 1] <= -50 < fast[i] and slow[i] > slow[i - 2]):
            continue
        srcs = sources_at(sym, t)
        px = seq[i][4]
        lo = 1.0 if srcs == {"momentum"} else 20.0
        if lo <= px <= 100:
            events.append((t, sym, srcs, px))

out = collections.Counter()
detail = collections.defaultdict(collections.Counter)
by_hour = collections.defaultdict(collections.Counter)
by_src = collections.defaultdict(collections.Counter)
tape_ages = []
for t, sym, srcs, px in sorted(events):
    hour = datetime.fromtimestamp(t, ET).strftime("%H")
    sfam = "+".join(sorted(srcs)) or "none"
    if not srcs:
        k = "0 not a seed name at the cross"
        detail[k]["listed earlier, dropped off" if ever_listed_before(sym, t) else "never listed yet today"] += 1
    elif any(t - 30 <= ft <= t + 150 for ft in fills.get(sym, [])):
        k = "3 seated and OPENED"
    elif not seated_at(sym, t):
        k = "1 seed name, NOT seated"
        fl = first_listed.get(sym, t)
        if t - fl < 180:
            why = "listed <3 min ago"
        else:
            rows = [w for ts, w in adm.get(sym, []) if t - 600 <= ts <= t]
            why = collections.Counter(rows).most_common(1)[0][0] if rows else (
                "was seated earlier, dropped" if was_seated_before(sym, t) else "no admit row")
        detail[k][why] += 1
    else:
        k = "2 seated, refused at the arm"
        rows = [(ts, w, a) for ts, w, a in dec.get(sym, []) if t - 30 <= ts <= t + 90]
        whys = [w for _, w, _ in rows]
        top = collections.Counter(whys).most_common(1)[0][0] if whys else "no decision row"
        cls = ("data" if top in DATA else "gate" if top in GATE else
               "arm never saw the cross" if top == "wait_mid_rise" else
               "latched, went stale" if top == "mid_rise_stale" else "other")
        detail[k][f"{cls}: {top}"] += 1
        ages = [a for ts, _, a in rows if isinstance(a, (int, float))]
        if ages:
            tape_ages.append(min(ages))
    out[k] += 1
    by_hour[hour][k[0]] += 1
    by_src[sfam][k[0]] += 1

n = len(events)
print(f"{DAY}: in-band -50 crosses (fast up through -50, slow rising) 09:35-15:55: {n}"
      f"  over {len({s for _, s, _, _ in events})} names  = {n / ((T1 - T0) / 600):.1f} per 10 min")
for k in sorted(out):
    print(f"  {k:36s} {out[k]:5d}  ({out[k] / max(1, n):.0%})")
for k in sorted(detail):
    print(f"\n{k}:")
    for why, c in detail[k].most_common(12):
        print(f"    {why:44s} {c}")
if tape_ages:
    ta = sorted(tape_ages)
    print(f"\nseated+refused: tape age at the cross (min over +-90 s) median {ta[len(ta)//2]:.0f}s, "
          f">15 s in {sum(1 for a in ta if a > 15)}/{len(ta)}")
print("\nrestart re-logs detected (time, pool size, names closed out): "
      + ", ".join(f"{datetime.fromtimestamp(t, ET):%H:%M:%S} {n}/{d}" for t, n, d in relogs))
print("\nby hour (0 not seed / 1 not seated / 2 arm refused / 3 opened):")
for h in sorted(by_hour):
    c = by_hour[h]
    tot = sum(c.values())
    print(f"  {h}:00  crosses {tot:4d}  0={c['0']:3d} 1={c['1']:3d} 2={c['2']:3d} 3={c['3']:2d}"
          f"   seed crosses/10min {(tot - c['0']) / 6:.1f}")
print("\nby source at the cross:")
for s, c in sorted(by_src.items(), key=lambda x: -sum(x[1].values())):
    print(f"  {s:28s} {sum(c.values()):4d}  0={c['0']} 1={c['1']} 2={c['2']} 3={c['3']}")
lag = [first_seated[s] - first_listed[s] for s in first_seated if s in first_listed
       and first_seated[s] >= first_listed[s] and first_listed[s] >= at(9, 25)]
if lag:
    lag.sort()
    print(f"\nlisted -> first seated (names first listed after 09:25): n={len(lag)} "
          f"median {lag[len(lag)//2]:.0f}s, p90 {lag[int(len(lag)*0.9)]:.0f}s")
