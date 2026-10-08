#!/usr/bin/env python3
"""sr_breakout_book.py — scoring tool for docs/studies/sr_breakout_book_prereg.json (buy the clean breakout on the book).

READ ONLY: ai_reports/shadow.jsonl (the desk's book checks) + Alpaca historical SIP quotes/bars, after hours.
No orders. Blind by design: `score` writes paired results to a SEALED file and prints counts only; `power` prints
counts, sessions, SE and MDE only; `read GROUP` is the single verdict per group and refuses to run before the
group meets the power bars (or before 2026-12-15), and refuses a second read.

USAGE (mini, after 16:30 ET):
  .venv/bin/python tools/studies/sr_breakout_book.py score 2026-10-08
  .venv/bin/python tools/studies/sr_breakout_book.py power
  .venv/bin/python tools/studies/sr_breakout_book.py read-due   (nightly: reads a group on its first ready night)
  .venv/bin/python tools/studies/sr_breakout_book.py read TIGHT|MOVERS

Prereg sections are cited as [P:key]. Resolutions of wording the prereg leaves open are marked R1.. and listed in
RESOLUTIONS (printed into every power report).
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import random
import re
import subprocess
import sys
import time
from datetime import date, datetime

REPO = os.environ.get("REPO") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (os.path.join(REPO, "tools", "studies"), os.path.join(REPO, "tools"), REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import bro_sr_wr as B  # noqa: E402  (paced/cached Alpaca client, t helpers, ET helpers)

WORK = os.environ.get("SRBB_WORK") or os.path.join(REPO, "ai_reports", "sr_breakout_book")
SHADOW = os.environ.get("SRBB_SHADOW") or os.path.join(REPO, "ai_reports", "shadow.jsonl")
OUTCOMES = os.path.join(REPO, "ai_reports", "outcomes.jsonl")
SEALED = os.path.join(WORK, "sealed.jsonl")
COUNTS = os.path.join(WORK, "counts.jsonl")
READS = os.path.join(WORK, "reads.json")
READY_FIRST = os.path.join(WORK, "ready_first.json")   # first sealed day each group met the power bars

FIRST_SESSION = "2026-10-08"                         # [P:data.sessions]
READ_DEADLINE = "2026-12-15"                         # [P:pass] / [P:decision_date]
PINS = {"ob_observe.py": "b763402", "tools/order_blocks.py": "a70a56b"}   # [P:integrity.code_pins] (commits)
CLEAN = (0.10, 0.30)                                 # [P:event.definition]
POKE = (1e-9, 0.10)
CHASE = (0.30, 1e9)
WIN_START, WIN_END = 9 * 60 + 45, 15 * 60 + 30       # 09:45 <= c <= 15:30
PREV_MAX_SEC = 120.0
MIN_OB_BARS = 30
MAX_BARS_JUMP = 2
LAT_SEC = 5.0                                        # [P:event.entry] c + 5 s
HOLD_SEC = 15 * 60
QUOTE_STALE = 5.0                                    # quotes must be <= 5 s old
CTRL_WIN = 30.0                                      # control check in [c - 30 s, c]
CTRL_EXCL = 15 * 60.0                                # no CLEAN new reading in [c - 15 min, c]
SEED = 59
NONPRIMARY_ARM = {"prefilter_far", "tape_only"}
MDE_BAR = {"TIGHT": 15.0, "MOVERS": 40.0}
MIN_PAIRS, MIN_SESSIONS = 60, 15
MID_MIN_PCT = 0.10                                   # mid re-check: >= 0.10% above the rebuilt block top
HALT_MIN = 5
CENT_UNDER = 5.0

RESOLUTIONS = {
    "R1_group": "TIGHT = source 'tight' on the event row; MOVERS = any other desk source (the row's source field).",
    "R2_primary_row": "price_src == 'quote' and arm_why not in {prefilter_far, tape_only}; rows without price are not primary.",
    "R3_new_reading": "the symbol's latest row (any row type) with ts in [c-120 s, c) is the preceding check; it must carry the "
                      "ob keys with ob_bars >= 30 and ob_brk_dist_pct null or below the band's floor (< 0.10 for CLEAN); "
                      "ob_bars(c) - ob_bars(prev) <= 2. The event row itself must carry ob keys with ob_bars >= 30.",
    "R4_mid_recheck": "block top = row price / (1 + brk/100); SIP NBBO mid at c (quote <= 5 s old) must be >= top x (1 + 0.10/100); "
                      "otherwise the event is spread_only (information) and the symbol has no primary event that session.",
    "R5_mid_quote": "a quote's age = c minus its SIP timestamp; the last valid NBBO (bid > 0, ask >= bid) in [t - 5 s, t].",
    "R6_exit_halt": "if no valid quote in [exit - 5 s, exit]: halted if SIP 1-min bars miss >= 5 consecutive minutes covering the "
                    "exit minute -> first valid quote within 30 min after the exit (counted halt_exit); else dropped (exit_stale).",
    "R7_cost": "net = (exit_mid / entry_mid - 1) x 1e4 - half spread at entry - half spread at exit (bp of each mid) - "
               "(1 cent round trip / entry_mid when entry_mid < $5).",
    "R8_controlA": "candidates: other symbols in the same group whose latest row in [c-30 s, c] is primary, carries ob keys with "
                   "ob_bars >= 30, ob_brk_dist_pct null, ob_resist_0.3 not True, and the symbol has no CLEAN new reading in "
                   "[c-15 min, c]; sorted by symbol, one drawn with random.Random('59|SYM|c').",
    "R9_controlB": "same symbol, rows after entry + 15 min (and <= 15:30) that are primary with ob_brk_dist_pct null; one drawn "
                   "with random.Random('59|SYM|c|B'); information only.",
    "R10_pins": "a row's git_version (trailing '+' = dirty build -> excluded) must resolve to commits where both pinned files have "
                "the pinned blobs; other builds counted and excluded.",
    "R11_halves": "NYSE sessions from 2026-10-08 by the Alpaca calendar; index 0 = A, 1 = B, alternating.",
    "R13_read_cutoff": "the read uses sealed sessions up to the first night `power` found the group ready (frozen in "
                       "ready_first.json), or up to 2026-12-15; nightly `read-due` performs it so it cannot be timed by hand.",
    "R12_t": "t_crit = bro_sr_wr.t_crit(df) (one-sided p of z = 2.24); MDE = (t_crit + 0.84) x session-clustered SE.",
}


# ------------------------------------------------------------------ small helpers
def jl_append(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def jl_read(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
    return out


def jl_rewrite_without_day(path, day):
    rows = [r for r in jl_read(path) if r.get("day") != day]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")


def band_of(v):
    if v is None:
        return None
    for name, (lo, hi) in (("CLEAN", CLEAN), ("POKE", POKE), ("CHASE", CHASE)):
        if lo <= v < hi:
            return name
    return None


def group_of(source):
    return "TIGHT" if str(source or "").strip().lower() == "tight" else "MOVERS"


def has_ob(r):
    return "ob_brk_dist_pct" in r and "ob_bars" in r and (r.get("ob_bars") or 0) >= MIN_OB_BARS


def in_window(day, ts):
    """09:45:00 <= ts <= 15:30:00 ET exactly (not 15:30:59)."""
    return B.et_ts(day, 9, 45) <= ts <= B.et_ts(day, 15, 30)


def is_primary_row(r):
    return r.get("price_src") == "quote" and r.get("arm_why") not in NONPRIMARY_ARM and (r.get("price") or 0) > 0


# ------------------------------------------------------------------ code pins
_PIN_BLOBS: dict = {}
_BUILD_OK: dict = {}


def _git(*a):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True, text=True).stdout.strip()


def pin_blobs():
    if not _PIN_BLOBS:
        for f, c in PINS.items():
            _PIN_BLOBS[f] = _git("rev-parse", f"{c}:{f}")
            if not _PIN_BLOBS[f]:
                sys.exit(f"pin {c}:{f} does not resolve in {REPO}: refusing to score (every row would be excluded)")
    return _PIN_BLOBS


def build_ok(ver) -> bool:
    """R10."""
    v = str(ver or "")
    if v in _BUILD_OK:
        return _BUILD_OK[v]
    ok = bool(v) and not v.endswith("+")
    if ok:
        for f, blob in pin_blobs().items():
            if not blob or _git("rev-parse", f"{v}:{f}") != blob:
                ok = False
                break
    _BUILD_OK[v] = ok
    return ok


# ------------------------------------------------------------------ shadow rows
_TS_RE = re.compile(r'"ts":\s*([0-9.]+)')


def load_day_rows(day, path=SHADOW, counts=None):
    """Rows of `day` (ET), ts-sorted, with only the fields this study reads. Unparseable lines are counted."""
    counts = counts if counts is not None else collections.Counter()
    t0 = B.et_ts(day, 0, 0)
    t1 = t0 + 86400
    keep = ("ts", "symbol", "source", "price", "price_src", "arm_why", "ob_brk_dist_pct", "ob_resist_0.3",
            "ob_room_pct", "ob_bars", "git_version", "pctr")
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _TS_RE.search(line[:200])
            if not m:
                counts["lines_without_ts"] += 1
                continue
            ts = float(m.group(1))
            if not (t0 <= ts < t1):
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                counts["lines_unparseable"] += 1
                continue
            out.append({k: r[k] for k in keep if k in r})
    out.sort(key=lambda r: (r["ts"], r.get("symbol") or ""))
    return out


def readings(rows_by_sym, counts):
    """New band readings per symbol: list of (row, band, prev_row). R3."""
    out = collections.defaultdict(list)
    for sym, rs in rows_by_sym.items():
        for i, r in enumerate(rs):
            b = band_of(r.get("ob_brk_dist_pct"))
            if b is None or not has_ob(r):
                continue
            prev = None
            for j in range(i - 1, -1, -1):
                if rs[j]["ts"] < r["ts"] - PREV_MAX_SEC:
                    break
                if rs[j]["ts"] < r["ts"]:
                    prev = rs[j]
                    break
            if prev is None:
                counts[f"{b}_no_prev_check"] += 1
                continue
            if not has_ob(prev):
                counts[f"{b}_prev_without_ob"] += 1
                continue
            floor = {"CLEAN": CLEAN[0], "POKE": POKE[0], "CHASE": CHASE[0]}[b]
            pv = prev.get("ob_brk_dist_pct")
            if pv is not None and pv >= floor:
                continue                                   # a continuation, not a new reading
            if (r.get("ob_bars") or 0) - (prev.get("ob_bars") or 0) > MAX_BARS_JUMP:
                counts[f"{b}_bars_jump"] += 1
                continue
            out[sym].append((r, b, prev))
    return out


# ------------------------------------------------------------------ market access
class Mkt(B.AlpacaMarket):
    def quote_ts(self, sym, t):
        """[ts, bid, ask] of the last valid SIP NBBO in [t - 5 s, t], or None. R5."""
        def fn():
            token = None
            while True:
                p = {"start": B.iso(t - QUOTE_STALE), "end": B.iso(t), "feed": "sip", "limit": 1000, "sort": "desc"}
                if token:
                    p["page_token"] = token
                js = self._get(f"{B.DATA}/v2/stocks/{sym}/quotes", p)
                for q in js.get("quotes") or []:
                    b, a = float(q.get("bp") or 0), float(q.get("ap") or 0)
                    if b > 0 and a >= b:
                        return [B.parse_ts(q["t"]), b, a]
                token = js.get("next_page_token")
                if not token:
                    return None
        return self._cached("quotes5", f"{sym}|{t:.3f}", fn)

    def first_quote_after(self, sym, t, within=1800.0):
        def fn():
            p = {"start": B.iso(t), "end": B.iso(t + within), "feed": "sip", "limit": 1000, "sort": "asc"}
            js = self._get(f"{B.DATA}/v2/stocks/{sym}/quotes", p)
            for q in js.get("quotes") or []:
                b, a = float(q.get("bp") or 0), float(q.get("ap") or 0)
                if b > 0 and a >= b:
                    return [B.parse_ts(q["t"]), b, a]
            return None
        return self._cached("quotes_after", f"{sym}|{t:.3f}", fn)


def _halted(mkt, sym, day, X):
    rows = mkt.minute_bars(sym, "sip", B.et_ts(day, 9, 30), B.et_ts(day, 16, 0))
    return B.is_halted(rows, X)


def simulate(mkt, sym, day, c, hold, counts, role):
    """R7 net bp for entry at c + 5 s held `hold` seconds; None if dropped (counted by role)."""
    te = c + LAT_SEC
    tx = te + hold
    try:
        qe = mkt.quote_ts(sym, te)
    except B.FetchFail:
        counts[f"{role}_fetch_fail"] += 1
        return None
    if not qe:
        counts[f"{role}_entry_stale"] += 1
        return None
    try:
        qx = mkt.quote_ts(sym, tx)
        if not qx:
            if _halted(mkt, sym, day, tx):
                qx = mkt.first_quote_after(sym, tx)
                if qx:
                    counts[f"{role}_halt_exit"] += 1
            if not qx:
                counts[f"{role}_exit_stale"] += 1
                return None
    except B.FetchFail:
        counts[f"{role}_fetch_fail"] += 1
        return None
    me, mx = (qe[1] + qe[2]) / 2, (qx[1] + qx[2]) / 2
    gross = (mx / me - 1) * 1e4
    cost = (qe[2] - qe[1]) / 2 / me * 1e4 + (qx[2] - qx[1]) / 2 / mx * 1e4
    if me < CENT_UNDER:
        cost += 0.01 / me * 1e4
    return {"gross": gross, "cost": cost, "net": gross - cost, "entry_mid": me, "exit_mid": mx}


# ------------------------------------------------------------------ one session
def score_day(day, mkt=None, rows=None, outcomes=None):
    """Score one session; returns (sealed_rows, counts). Never prints means."""
    counts = collections.Counter()
    mkt = mkt or Mkt(work=WORK)
    rows = load_day_rows(day, counts=counts) if rows is None else rows
    counts["rows_total"] = len(rows)
    in_win = [r for r in rows if in_window(day, r["ts"])]
    counts["rows_window"] = len(in_win)
    counts["rows_with_ob"] = sum(1 for r in in_win if "ob_brk_dist_pct" in r)
    pinned = []
    for r in rows:
        if build_ok(r.get("git_version")):
            pinned.append(r)
        else:
            counts["rows_other_build"] += 1
    by_sym = collections.defaultdict(list)
    for r in pinned:
        if r.get("symbol"):
            by_sym[r["symbol"]].append(r)
        else:
            counts["rows_without_symbol"] += 1
    news = readings(by_sym, counts)
    # every CLEAN new reading on a primary row (primary, non-first or spread_only), for the Control A
    # exclusion [P:controls.CONTROL_A_primary]
    clean_times = {s: [r["ts"] for r, b, _ in lst if b == "CLEAN" and is_primary_row(r)] for s, lst in news.items()}

    out = []
    for sym in sorted(news):
        firsts = {}
        for r, b, prev in news[sym]:
            c = r["ts"]
            if not in_window(day, c):
                continue
            if not is_primary_row(r):
                counts[f"{b}_nonprimary_row"] += 1
                continue
            if b in firsts:
                counts[f"{b}_non_first"] += 1
                out.append(_info_row(day, sym, r, b, "non_first"))
                continue
            firsts[b] = c
            out.append(_score_event(mkt, day, sym, r, b, by_sym, clean_times, counts, outcomes))
    out = [x for x in out if x]
    for x in out:
        counts[f"{x['band']}_{x['status']}"] += 1
    return out, counts


def _info_row(day, sym, r, band, status):
    return {"day": day, "symbol": sym, "c": r["ts"], "band": band, "group": group_of(r.get("source")),
            "status": status}


def _pctr_rising(by_sym, sym, c):
    prior = [x for x in by_sym[sym] if x["ts"] <= c and x.get("pctr") is not None][-4:]
    if len(prior) < 4:
        return None
    return prior[-1]["pctr"] > prior[0]["pctr"]


def _score_event(mkt, day, sym, r, band, by_sym, clean_times, counts, outcomes):
    c = r["ts"]
    grp = group_of(r.get("source"))
    row = _info_row(day, sym, r, band, "pending")
    row.update(brk=r.get("ob_brk_dist_pct"), room=r.get("ob_room_pct"), pctr_rising=_pctr_rising(by_sym, sym, c))
    # R4 mid re-check
    top = float(r["price"]) / (1 + float(r["ob_brk_dist_pct"]) / 100)
    try:
        q = mkt.quote_ts(sym, c)
    except B.FetchFail:
        counts["event_fetch_fail"] += 1
        row["status"] = "drop_fetch_fail"
        return row
    if not q:
        row["status"] = "drop_mid_stale"
        return row
    mid = (q[1] + q[2]) / 2
    if band == "CLEAN" and mid < top * (1 + MID_MIN_PCT / 100):
        row["status"] = "spread_only"
        return row
    ev = simulate(mkt, sym, day, c, HOLD_SEC, counts, "event")
    if ev is None:
        row["status"] = "drop_event"
        return row
    row.update(ev=ev)
    for h, k in ((300, "ev5"), (1800, "ev30")):
        x = simulate(mkt, sym, day, c, h, counts, f"event{k}")
        row[k] = x["net"] if x else None
    # Control A (R8)
    cand = []
    for s2, rs in by_sym.items():
        if s2 == sym:
            continue
        last = None
        for x in rs:
            if x["ts"] > c:
                break
            if x["ts"] >= c - CTRL_WIN:
                last = x
        if last is None or group_of(last.get("source")) != grp or not is_primary_row(last) or not has_ob(last):
            continue
        if last.get("ob_brk_dist_pct") is not None or last.get("ob_resist_0.3") is True:
            continue
        if any(c - CTRL_EXCL <= t <= c for t in clean_times.get(s2, [])):
            continue
        cand.append(s2)
    cand.sort()
    if not cand:
        row["status"] = "drop_no_control"
        return row
    ctrl = random.Random(f"{SEED}|{sym}|{c:.3f}").choice(cand)
    cx = simulate(mkt, ctrl, day, c, HOLD_SEC, counts, "controlA")
    if cx is None:
        row["status"] = "drop_control"
        return row
    row.update(ctrlA=ctrl, ca=cx, diff=ev["net"] - cx["net"], status="pair")
    # Control B (R9, information)
    later = [x for x in by_sym[sym] if c + LAT_SEC + HOLD_SEC < x["ts"] and x["ts"] <= B.et_ts(day, 15, 30)
             and is_primary_row(x) and "ob_brk_dist_pct" in x and x.get("ob_brk_dist_pct") is None]
    if later:
        pick = random.Random(f"{SEED}|{sym}|{c:.3f}|B").choice(later)
        cb = simulate(mkt, sym, day, pick["ts"], HOLD_SEC, counts, "controlB")
        row["cb"] = cb["net"] if cb else None
    # desk bought within 5 min (information)
    for o in outcomes or []:
        if o.get("symbol") == sym and c <= float(o.get("entry_time") or 0) <= c + 300 and o.get("exit_price"):
            row["desk_bp"] = (float(o["exit_price"]) / float(o["entry_price"]) - 1) * 1e4
            break
    return row


# ------------------------------------------------------------------ halves, power, read
def halves_map(mkt=None, hi=None):
    mkt = mkt or Mkt(work=WORK, guard=False)   # one cached calendar call: allowed in market hours
    cal = mkt.calendar(FIRST_SESSION, hi or "2027-03-31")
    return {d: ("A" if i % 2 == 0 else "B") for i, d in enumerate(sorted(cal))}


def power_table(sealed, halves):
    """Counts, sessions, SE, MDE per group and half — no means. R12."""
    out = {}
    for grp in ("TIGHT", "MOVERS"):
        pairs = [r for r in sealed if r.get("group") == grp and r.get("band") == "CLEAN" and r.get("status") == "pair"]
        g = {}
        for h in ("A", "B", "ALL"):
            ps = [dict(day=r["day"], d=r["diff"]) for r in pairs if h == "ALL" or halves.get(r["day"]) == h]
            st = B.clustered(ps, key="d")
            tc = B.t_crit(st["df"]) if st["df"] else None
            mde = (tc + 0.84) * st["se"] if (tc is not None and st["se"]) else None
            g[h] = {"pairs": st["n"], "sessions": st["sessions"], "se": st["se"], "t_crit": tc, "mde": mde}
        ready = all(g[h]["pairs"] >= MIN_PAIRS and g[h]["sessions"] >= MIN_SESSIONS
                    and g[h]["mde"] is not None and g[h]["mde"] <= MDE_BAR[grp] for h in ("A", "B"))
        out[grp] = {"halves": g, "ready": ready}
    return out


def _drop_top(pairs, key, k):
    contrib = collections.defaultdict(float)
    for p in pairs:
        contrib[p[key]] += p["d"]
    top = {x for x, _ in sorted(contrib.items(), key=lambda kv: -kv[1])[:k]}
    return [p for p in pairs if p[key] not in top]


def verdict(grp, sealed, halves, today=None):
    """[P:pass] for one group. Returns a dict with the verdict and the numbers behind it."""
    today = today or datetime.now(B.ET).strftime("%Y-%m-%d")
    pw = power_table(sealed, halves)[grp]
    pairs = [dict(day=r["day"], symbol=r["symbol"], d=r["diff"], ev=r["ev"]["net"]) for r in sealed
             if r.get("group") == grp and r.get("band") == "CLEAN" and r.get("status") == "pair"]
    pooled = B.clustered(pairs, key="d")
    evn = B.clustered(pairs, key="ev")
    hv = {h: B.clustered([p for p in pairs if halves.get(p["day"]) == h], key="d") for h in ("A", "B")}
    tc = B.t_crit(pooled["df"]) if pooled["df"] else None
    res = {"group": grp, "pooled": pooled, "event_net": evn, "halves": hv, "t_crit": tc, "power": pw}
    if pooled["t"] is not None and pooled["t"] <= -2:
        res["verdict"] = "FAIL"
        res["why"] = "pooled paired t <= -2"
        return res
    if not pw["ready"]:
        res["verdict"] = "CLOSED UNPROVEN" if today >= READ_DEADLINE else "UNDERPOWERED"
        return res
    passed = (pooled["mean"] >= 5 and tc is not None and pooled["t"] >= tc
              and all(hv[h]["mean"] is not None and hv[h]["mean"] > 0 for h in ("A", "B"))
              and evn["mean"] > 0)
    if passed:
        for key, k in (("day", 3), ("symbol", 5)):
            rest = _drop_top(pairs, key, k)
            m = sum(p["d"] for p in rest) / len(rest) if rest else None
            res[f"drop_top_{key}"] = m
            if m is None or m <= 0:
                passed = False
    res["verdict"] = "PASS" if passed else "FAIL"
    if passed:
        side = {}
        for b in ("POKE", "CHASE"):
            ds = [r["diff"] for r in sealed if r.get("group") == grp and r.get("band") == b and r.get("status") == "pair"]
            side[b] = sum(ds) / len(ds) if ds else None
        res["neighbours"] = side
        res["knife_edge"] = all(v is not None and v <= 0 for v in side.values())
    return res


# ------------------------------------------------------------------ commands
def cmd_score(day):
    if day < FIRST_SESSION:
        sys.exit(f"{day} is before {FIRST_SESSION}: excluded by the prereg")
    B.market_hours_guard()
    mkt = Mkt(work=WORK)
    rows, counts = score_day(day, mkt=mkt, outcomes=jl_read(OUTCOMES))
    mkt.flush()
    unrec = sum(v for k, v in counts.items() if k.endswith("fetch_fail"))
    flag = {"coverage": (counts["rows_with_ob"] / counts["rows_window"]) if counts["rows_window"] else None,
            "unrecovered_fetch_failures": unrec}
    flag["other_build_share"] = (counts["rows_other_build"] / counts["rows_total"]) if counts["rows_total"] else None
    flag["flagged"] = (bool(unrec) or (flag["coverage"] is not None and flag["coverage"] < 0.80)
                       or (flag["other_build_share"] or 0) > 0.05)
    jl_rewrite_without_day(SEALED, day)
    jl_append(SEALED, rows)
    jl_rewrite_without_day(COUNTS, day)
    jl_append(COUNTS, [{"day": day, "counts": dict(counts), **flag, "requests": mkt.requests}])
    print(f"sr_breakout_book score {day}: rows {counts['rows_total']} (window {counts['rows_window']}, "
          f"ob coverage {flag['coverage'] if flag['coverage'] is None else round(flag['coverage'], 3)}), "
          f"other-build rows {counts['rows_other_build']}, requests {mkt.requests}"
          + ("  ** SESSION FLAGGED **" if flag["flagged"] else ""))
    for k in sorted(counts):
        if k.startswith(("CLEAN", "POKE", "CHASE", "event", "control")):
            print(f"  {k:32s} {counts[k]}")
    print("  (blind: no means are computed; paired results sealed in ai_reports/sr_breakout_book/sealed.jsonl)")


def cmd_power():
    sealed = jl_read(SEALED)
    halves = halves_map()
    pw = power_table(sealed, halves)
    last_day = max((r["day"] for r in sealed), default=None)
    first = B.jload(READY_FIRST, {}) or {}
    for grp, v in pw.items():
        if v["ready"] and grp not in first and last_day:
            first[grp] = last_day                     # frozen: the read uses sealed days up to this one
    B.jsave(READY_FIRST, first)
    print("sr_breakout_book power (counts, sessions, SE, MDE only - no means)")
    print(f"  first ready day by group (frozen read cutoff): {first or 'none yet'}")
    for grp, v in pw.items():
        print(f"  {grp}: ready for its single read: {v['ready']} (bars: >= {MIN_PAIRS} pairs, >= {MIN_SESSIONS} sessions, "
              f"MDE <= {MDE_BAR[grp]:.0f} bp per half)")
        for h, s in v["halves"].items():
            f = lambda x: "n/a" if x is None else f"{x:.1f}"  # noqa: E731
            print(f"    {h:3s} pairs {s['pairs']:4d} sessions {s['sessions']:3d} SE {f(s['se'])} t_crit {f(s['t_crit'])} MDE {f(s['mde'])}")
    print("resolutions:", json.dumps(RESOLUTIONS, indent=1))


def cmd_read(grp):
    grp = grp.upper()
    if grp not in MDE_BAR:
        sys.exit("group must be TIGHT or MOVERS")
    reads = B.jload(READS, {}) or {}
    if grp in reads:
        sys.exit(f"{grp} was already read on {reads[grp]['at']}: one read per group (prereg)")
    today = datetime.now(B.ET).strftime("%Y-%m-%d")
    first = (B.jload(READY_FIRST, {}) or {}).get(grp)
    if first:
        cutoff = first                                # the first night the group met the power bars
    elif today >= READ_DEADLINE:
        cutoff = READ_DEADLINE
    else:
        sys.exit(f"{grp} has not met the power bars (run `power`) and it is before {READ_DEADLINE}: no read")
    sealed = [r for r in jl_read(SEALED) if r.get("day", "") <= cutoff]
    halves = halves_map()
    res = verdict(grp, sealed, halves, today=cutoff if first else today)
    res["cutoff_day"] = cutoff
    reads[grp] = {"at": today, "cutoff_day": cutoff, "verdict": res["verdict"]}
    B.jsave(READS, reads)
    print(json.dumps(res, indent=1, default=str))


def cmd_read_due():
    """Nightly: read any unread group that is due (first-ready day recorded, or the deadline reached)."""
    reads = B.jload(READS, {}) or {}
    first = B.jload(READY_FIRST, {}) or {}
    today = datetime.now(B.ET).strftime("%Y-%m-%d")
    for grp in MDE_BAR:
        if grp not in reads and (grp in first or today >= READ_DEADLINE):
            print(f"== single read: {grp}")
            cmd_read(grp)


def main(argv):
    if len(argv) < 2 or argv[1] not in ("score", "power", "read", "read-due"):
        sys.exit(__doc__)
    if argv[1] == "score":
        cmd_score(argv[2] if len(argv) > 2 else datetime.now(B.ET).strftime("%Y-%m-%d"))
    elif argv[1] == "power":
        cmd_power()
    elif argv[1] == "read-due":
        cmd_read_due()
    else:
        cmd_read(argv[2] if len(argv) > 2 else "")


if __name__ == "__main__":
    main(sys.argv)
