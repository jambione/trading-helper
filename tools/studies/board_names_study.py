#!/usr/bin/env python3
"""Which names belong on the board? Spec: docs/studies/board_names_prereg.json.

Four name gates known at the seat minute (price >= $30; up >= 2.35% and seated
by 10:46; above the 50-day; 20-day dollar volume >= $500M), each scored by the
gross 10-minute forward return over every board minute of the names it keeps
versus the names it drops.

  --fetch-daily  daily SIP bars for every board symbol, plus minute bars for
                 refused-candidate symbols (mini only: Alpaca keys)
  (default)      inventory only, no returns
  --score        after the prereg has a result-skeptic READY

Board, IEX/SIP minute bars and the scoring helpers come from
early_entry_honest.py and its cache (ai_reports/early_entry_honest/).
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import pickle
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402

ROOT = E.ROOT
OUT_DIR = os.path.join(ROOT, "ai_reports", "board_names")
DAILY = os.path.join(OUT_DIR, "daily.pkl")
REFUSED = os.path.join(OUT_DIR, "refused_bars.pkl")
ADMIT_DIR = os.path.join(ROOT, "ai_reports", "admit_ledger")

PRICE_MIN = 30.0
RUNWAY_CHG, RUNWAY_BY = 0.0235, 10 * 60 + 46
SMA_N, ADV_N, ADV_MIN = 50, 20, 500e6
GAP_DOWN = -0.01
MIN_SIDE_MIN = 30          # board minutes per side per day for a day lift
MIN_DAYS, MIN_NAMES = 8, 20
GATES = ("G1_price", "G2_runway", "G3_above_50d", "G4_liquid")


# ── pure features ────────────────────────────────────────────────────────────

def prior_daily(daily: list[tuple[str, float, float]], day: str) -> list[tuple[str, float, float]]:
    """Daily rows (date, close, volume) strictly before `day`, oldest first."""
    return [r for r in daily if r[0] < day]


def gate_values(seat_px: float | None, seat_min: int, day_open: float | None,
                prior: list[tuple[str, float, float]], pc_scale: float = 1.0) -> dict:
    """Each gate True / False, or None when the input is unknown.

    Daily bars are split-adjusted, minute bars are raw: pc_scale converts the
    prior close to the minute bars' basis for G2 and gap_down (raw / adjusted
    close of the same session). G3 and G4 stay on the adjusted series."""
    pc = prior[-1][1] if prior else None
    pc_raw = pc * pc_scale if pc else None
    out: dict = {}
    out["G1_price"] = None if seat_px is None else seat_px >= PRICE_MIN
    # G2 is graded only among names seated by 10:46, so its out group is the
    # morning seats that were not up 2.35%, not the afternoon (skeptic 10/14 #2).
    if seat_px is None or not pc_raw or seat_min > RUNWAY_BY:
        out["G2_runway"] = None
    else:
        out["G2_runway"] = seat_px / pc_raw - 1 >= RUNWAY_CHG
    out["G2_any_seat"] = None if (seat_px is None or not pc_raw) else (
        seat_px / pc_raw - 1 >= RUNWAY_CHG and seat_min <= RUNWAY_BY)
    if len(prior) < SMA_N:
        out["G3_above_50d"] = None
    else:
        # The 50 prior closes ending with the prior session (prereg G3).
        sma = statistics.mean(r[1] for r in prior[-SMA_N:])
        out["G3_above_50d"] = pc > sma
    if len(prior) < ADV_N:
        out["G4_liquid"] = None
    else:
        out["G4_liquid"] = statistics.mean(r[1] * r[2] for r in prior[-ADV_N:]) >= ADV_MIN
    # information
    out["gap_down"] = None if (day_open is None or not pc_raw) else day_open / pc_raw - 1 <= GAP_DOWN
    out["intraday_winner"] = None if (day_open is None or seat_px is None) else seat_px > day_open
    out["seat_hour"] = seat_min // 60
    return out


def split_scale(daily: list[tuple[str, float, float]], day: str, raw_close: float | None) -> float:
    """raw / split-adjusted close of `day`; 1.0 unless they differ by more than 5% (a split)."""
    adj = next((c for d, c, _v in daily if d == day), None)
    if not adj or not raw_close:
        return 1.0
    r = raw_close / adj
    return r if abs(r - 1.0) > 0.05 else 1.0


def rvol_at(sip_vol: dict, seat_min: int, prior) -> float | None:
    if len(prior) < ADV_N:
        return None
    adv = statistics.mean(r[2] for r in prior[-ADV_N:])
    elapsed = (seat_min + 1 - E.RTH_START) / 390.0
    if adv <= 0 or elapsed <= 0:
        return None
    cum = sum(v for m, v in sip_vol.items() if E.RTH_START <= m <= seat_min)
    return cum / (adv * elapsed)


def day_lift(rows: list[tuple[bool, float]]) -> tuple[float | None, float | None, int, int]:
    """(in mean, lift, n_in, n_out) over one day's board minutes; lift None if a side is thin."""
    ins = [f for g, f in rows if g]
    outs = [f for g, f in rows if not g]
    mi = statistics.mean(ins) if ins else None
    if len(ins) < MIN_SIDE_MIN or len(outs) < MIN_SIDE_MIN:
        return mi, None, len(ins), len(outs)
    return mi, mi - statistics.mean(outs), len(ins), len(outs)


def half_verdict(lift, t, in_mean, days, names) -> str:
    if days < MIN_DAYS or names < MIN_NAMES or lift is None or t is None or in_mean is None:
        return "UNDERPOWERED"
    return "PASS" if lift >= 0.0005 and t >= 2.0 and in_mean >= 0 else "FAIL"


# ── data ─────────────────────────────────────────────────────────────────────

def admit_rows(day: str) -> list[dict]:
    p = os.path.join(ADMIT_DIR, f"{day}.jsonl")
    out = []
    if os.path.exists(p):
        for line in open(p, encoding="utf-8", errors="replace"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def refused_first(day: str, board: dict) -> dict[str, int]:
    """Symbol -> first refusal minute, for every refused candidate (kept=false).
    Names seated later stay in: dropping them would drop the refusals that went
    on to run (skeptic #4). The scorer stops each at its seat minute."""
    first: dict[str, int] = {}
    for r in admit_rows(day):
        s = str(r.get("symbol") or "").upper()
        if not s or r.get("kept") is not False:
            continue
        m = E._et_min(float(r["ts"]))
        first[s] = min(first.get(s, m), m)
    return first


def sources(day: str) -> dict[str, str]:
    src: dict[str, str] = {}
    for r in admit_rows(day):
        s = str(r.get("symbol") or "").upper()
        if s and r.get("source"):
            src.setdefault(s, str(r["source"]))
    return src


def fetch_daily(syms: list[str], start: str, end: str, errors: list | None = None) -> dict:
    import bars
    import pandas as pd
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    cl = bars.client()
    if cl is None:
        raise SystemExit("no Alpaca data client; run on the mini")
    out: dict = {}
    bad: set = set()
    for i in range(0, len(syms), 200):
        chunk = syms[i:i + 200]
        df = None
        for attempt in range(5):
            chunk = [c for c in chunk if c not in bad]
            try:
                df = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=chunk, timeframe=TimeFrame.Day,
                    start=datetime.fromisoformat(start).replace(tzinfo=timezone.utc),
                    end=datetime.fromisoformat(end).replace(tzinfo=timezone.utc),
                    adjustment=Adjustment.SPLIT, feed=DataFeed.SIP)).df
                break
            except Exception as e:  # noqa: BLE001
                import re
                m = re.search(r"invalid symbol: ([A-Za-z0-9.\-]+)", str(e))
                if m:
                    bad.add(m.group(1).upper())
                    continue
                print(f"  daily try {attempt}: {str(e)[:80]}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
                df = None
        if df is None and chunk and errors is not None:
            errors.append(("daily", len(chunk)))       # every try failed
        if df is None or df.empty:
            continue
        if not isinstance(df.index, pd.MultiIndex):
            df = pd.concat({chunk[0]: df}, names=["symbol"])
        for s in df.index.get_level_values("symbol").unique():
            sub = df.xs(s, level="symbol").sort_index()
            out[str(s).upper()] = [(t.astimezone(E.ET).strftime("%Y-%m-%d"), float(c), float(v))
                                   for t, c, v in zip(sub.index, sub["close"], sub["volume"])]
        time.sleep(0.4)
    return out


def do_fetch(datas: list[dict]) -> None:
    import bars
    os.makedirs(OUT_DIR, exist_ok=True)
    days = sorted(d["day"] for d in datas)
    refused = {d["day"]: refused_first(d["day"], d["board"]) for d in datas}
    syms = sorted({s for d in datas for s in d["board"]} | {s for r in refused.values() for s in r})
    daily = fetch_daily(syms, "2026-05-01", (datetime.fromisoformat(days[-1]) + timedelta(days=1)).strftime("%Y-%m-%d"))
    pickle.dump(daily, open(DAILY, "wb"))
    print(f"daily: requested {len(syms)}, got {len(daily)}, missing {sorted(set(syms) - set(daily))[:20]}")
    cl = bars.client()
    rb = {}
    for day in days:
        want = sorted(s for s in refused[day] if s not in next(d for d in datas if d["day"] == day)["sip"])
        if want:
            rb[day] = E._fetch_feed(cl, want, day, "sip")
            print(f"refused {day}: {len(rb[day])}/{len(want)}")
    pickle.dump({"refused": refused, "bars": rb}, open(REFUSED, "wb"))


# ── scoring ──────────────────────────────────────────────────────────────────

def name_days(datas: list[dict], daily: dict) -> list[dict]:
    """One record per board name-day: gate values and its board-minute forward returns."""
    out = []
    for data in datas:
        day = data["day"]
        src = sources(day)
        for sym, board in data["board"].items():
            f = data["sip"].get(sym)
            mins = sorted(m for m in board if E.WIN_START <= m <= E.WIN_END)
            if f is None or not mins:
                continue
            sip = E.sip_map(f)
            vol = {E._et_min(t.timestamp()): float(v) for t, v in zip(f["time"], f["volume"])}
            seat = mins[0]
            seat_px = (sip.get(seat) or (None, None))[1]
            day_open = (sip.get(E.RTH_START) or (None, None))[0]
            prior = prior_daily(daily.get(sym, []), day)
            last_rth = max((m for m in sip if m < E.RTH_END), default=None)
            scale = split_scale(daily.get(sym, []), day, sip[last_rth][1] if last_rth is not None else None)
            g = gate_values(seat_px, seat, day_open, prior, scale)
            fw10 = [x for x in (E.markout(sip, m + 1) for m in mins) if x is not None]
            fw30 = [x for x in (E.markout(sip, m + 1, 30) for m in mins) if x is not None]
            out.append({"sym": sym, "day": day, "gates": g, "fwd": fw10, "fwd30": fw30, "split": scale != 1.0,
                        "rvol": rvol_at(vol, seat, prior), "source": src.get(sym, "?")})
    return out


def name_day_equal(nds, key, cut, h, which="fwd", min_names=3):
    """One mean per name-day, then in - out per day, then day-equal."""
    per_day = defaultdict(lambda: {True: [], False: []})
    for n in nds:
        g = key(n)
        if g is None or (n["day"] <= cut) != (h == "A") or not n[which]:
            continue
        per_day[n["day"]][bool(g)].append(statistics.mean(n[which]))
    lifts = {d: [statistics.mean(v[True]) - statistics.mean(v[False])]
             for d, v in per_day.items() if len(v[True]) >= min_names and len(v[False]) >= min_names}
    mu, t, _n, _d = E.day_equal(lifts)
    return mu, t


def score_gate(nds: list[dict], key, cut: str, which: str = "fwd") -> dict:
    halves = {}
    unknown = sum(1 for n in nds if key(n) is None)
    for h in "AB":
        per_day = defaultdict(list)
        names = 0
        for n in nds:
            g = key(n)
            if g is None or (n["day"] <= cut) != (h == "A"):
                continue
            names += bool(g)
            per_day[n["day"]] += [(bool(g), x) for x in n[which]]
        lifts, ins = {}, {}
        for d, rows in per_day.items():
            mi, li, _ni, _no = day_lift(rows)
            if li is not None:            # 'in' on the same days as the lift (skeptic #3)
                lifts[d] = [li]
                ins[d] = [mi]
        ml, tl, _n, nd = E.day_equal(lifts)
        mi, ti, _n2, _d2 = E.day_equal(ins)
        nde = name_day_equal(nds, key, cut, h, which)
        v = half_verdict(ml, tl, mi, nd, names)
        if v == "PASS" and not (nde[0] is not None and nde[0] > 0):
            v = "FAIL"                    # name-day-equal lift must agree (skeptic #5)
        halves[h] = {"lift": ml, "t": tl, "in": mi, "in_t": ti, "days": nd, "names": names,
                     "nde_lift": nde[0], "nde_t": nde[1], "verdict": v}
    return {"halves": halves, "unknown": unknown,
            "verdict": E.rule_verdict([halves[h]["verdict"] for h in "AB"])}


def refused_vs_board(datas: list[dict]) -> dict | None:
    if not os.path.exists(REFUSED):
        return None
    rf = pickle.load(open(REFUSED, "rb"))
    out = defaultdict(lambda: {"ref": [], "board": []})
    for data in datas:
        day = data["day"]
        if day not in rf["bars"]:
            continue
        # Rebuilt from the ledger at score time: the cached list predates the
        # fix that keeps names seated later (bars for those come from the board cache).
        for sym, first in refused_first(day, data["board"]).items():
            f = rf["bars"][day].get(sym)
            if f is None:
                f = data["sip"].get(sym)
            if f is None:
                continue
            sip = E.sip_map(f)
            seated = [m for m in data["board"].get(sym, ()) if m >= first]
            end = min(seated) - 1 if seated else E.WIN_END
            for m in range(max(first, E.WIN_START), min(end, E.WIN_END) + 1):
                x = E.markout(sip, m + 1)
                if x is not None:
                    out[day]["ref"].append(x)
        for sym, board in data["board"].items():
            f = data["sip"].get(sym)
            if f is None:
                continue
            sip = E.sip_map(f)
            out[day]["board"] += [x for x in (E.markout(sip, m + 1) for m in board
                                              if E.WIN_START <= m <= E.WIN_END) if x is not None]
    diff = {d: [statistics.mean(v["ref"]) - statistics.mean(v["board"])]
            for d, v in out.items() if len(v["ref"]) >= MIN_SIDE_MIN and len(v["board"]) >= MIN_SIDE_MIN}
    ref = {d: [statistics.mean(v["ref"])] for d, v in out.items() if len(v["ref"]) >= MIN_SIDE_MIN}
    return {"refused_minus_board": E.day_equal(diff)[:2] + (len(diff),), "refused_mean": E.day_equal(ref)[:2]}


def _bp(x) -> str:
    return "—" if x is None else f"{x * 1e4:+.1f}"


def _t(x) -> str:
    return "—" if x is None else f"{x:+.2f}"


def report(datas: list[dict], daily: dict) -> str:
    days = sorted(d["day"] for d in datas)
    cut = E.split(days)
    nds = name_days(datas, daily)
    L = [f"# Board names — {days[0]} .. {days[-1]}", "",
         f"{len(nds)} board name-days, {len(days)} days, half A ends {cut}. Outcome: gross 10-minute forward "
         "return over every board minute, gated names vs the rest (bp per 10 minutes).", "",
         "| gate | half | gated names | days | in (gated board) | t | lift vs excluded | t | name-day-equal lift | verdict |",
         "|---|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    res = {}
    for g in GATES:
        r = score_gate(nds, lambda n, g=g: n["gates"].get(g), cut)
        res[g] = r
        for h, x in r["halves"].items():
            L.append(f"| {g} | {h} | {x['names']} | {x['days']} | {_bp(x['in'])} | {_t(x['in_t'])} "
                     f"| {_bp(x['lift'])} | {_t(x['t'])} | {_bp(x['nde_lift'])} | {x['verdict']} |")
    L += ["", "**Verdicts:** " + ", ".join(f"{g} {r['verdict']} (unknown {r['unknown']})" for g, r in res.items()),
          "", "### Information only", ""]

    def info(label, key, which="fwd"):
        r = score_gate(nds, key, cut, which)
        a, b = r["halves"]["A"], r["halves"]["B"]
        L.append(f"- {label}: in {_bp(a['in'])} / {_bp(b['in'])}, lift {_bp(a['lift'])} (t {_t(a['t'])}) / "
                 f"{_bp(b['lift'])} (t {_t(b['t'])}), gated names {a['names']} / {b['names']}")
    for g1, g2 in itertools.combinations(GATES, 2):
        info(f"{g1} AND {g2}", lambda n, g1=g1, g2=g2: None if n["gates"].get(g1) is None or n["gates"].get(g2) is None
             else bool(n["gates"][g1] and n["gates"][g2]))
    info("G2 as first written (any seat; out includes afternoon seats)", lambda n: n["gates"].get("G2_any_seat"))
    info("gap-down > 1% (gated = gapped down)", lambda n: n["gates"].get("gap_down"))
    info("intraday winner at seat", lambda n: n["gates"].get("intraday_winner"))
    rv = sorted(n["rvol"] for n in nds if n["rvol"] is not None)
    if len(rv) >= 3:
        t1, t2 = rv[len(rv) // 3], rv[2 * len(rv) // 3]
        # RVOL: daily volume vs summed minute volume (granularity mismatch); tercile cuts use both halves.
        info(f"RVOL top tercile (>= {t2:.2f})", lambda n: None if n["rvol"] is None else n["rvol"] >= t2)
        info(f"RVOL bottom tercile (< {t1:.2f})", lambda n: None if n["rvol"] is None else n["rvol"] < t1)
    for hr in range(9, 16):
        info(f"seated in hour {hr}", lambda n, hr=hr: n["gates"]["seat_hour"] == hr)
    for s, c in Counter(n["source"] for n in nds).most_common(6):
        info(f"source {s} ({c} name-days)", lambda n, s=s: n["source"] == s)
    for g in GATES:
        info(f"{g}, 30-minute hold", lambda n, g=g: n["gates"].get(g), "fwd30")
    rb = refused_vs_board(datas)
    if rb:
        (mu, t, nd), (rm, rt) = rb["refused_minus_board"], rb["refused_mean"]
        L.append(f"- refused candidates (never seated) minus board: {_bp(mu)} (t {_t(t)}, {nd} days); "
                 f"refused mean {_bp(rm)} (t {_t(rt)})")
    allf = defaultdict(list)
    for n in nds:
        allf[n["day"]] += n["fwd"]
    mu, t, _n, _d = E.day_equal({d: [statistics.mean(v)] for d, v in allf.items() if v})
    L.append(f"- whole board, day-equal: {_bp(mu)} (t {_t(t)})")
    L.append(f"- split-rescaled name-days (G2 / gap prior close): {sum(n['split'] for n in nds)}")
    L.append("- None of the information lines can be promoted to a gate; each would need its own prereg.")
    return "\n".join(L)


FWD_MIN_DAYS = 10


def forward_g1(nds: list[dict]) -> tuple[str, dict]:
    """docs/studies/price30_forward_prereg.json: G1 (price >= $30) on forward sessions, one block."""
    key = lambda n: n["gates"].get("G1_price")  # noqa: E731
    per_day = defaultdict(list)
    names = 0
    for n in nds:
        g = key(n)
        if g is None:
            continue
        names += bool(g)
        per_day[n["day"]] += [(bool(g), x) for x in n["fwd"]]
    lifts, ins = {}, {}
    for d, rows in per_day.items():
        mi, li, _ni, _no = day_lift(rows)
        if li is not None:
            lifts[d] = [li]
            ins[d] = [mi]
    ml, tl, _n, nd = E.day_equal(lifts)
    mi, ti, _n2, _d2 = E.day_equal(ins)
    nde, _ndt = name_day_equal(nds, key, "9999-12-31", "A")      # every day counts as one block
    if nd < FWD_MIN_DAYS or names < MIN_NAMES or ml is None or tl is None:
        v = "UNDERPOWERED"
    else:
        v = "PASS" if (ml > 0 and tl >= 2.0 and nde is not None and nde > 0) else "FAIL"
    res = {"lift": ml, "t": tl, "in": mi, "in_t": ti, "days": nd, "names": names, "nde": nde, "verdict": v}
    md = (f"G1 price >= $30, forward: {nd} sessions, {names} gated name-days | lift {_bp(ml)} bp (t {_t(tl)}) | "
          f"name-day-equal {_bp(nde)} | gated board {_bp(mi)} (t {_t(ti)}) | **{v}**")
    return md, res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch-daily", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--since", default=None, help="score only sessions on or after this day")
    ap.add_argument("--forward-g1", action="store_true",
                    help="price30_forward_prereg.json read (with --since 2026-10-14)")
    a = ap.parse_args()
    datas, excluded = E.load_days()
    if a.since:
        datas = [d for d in datas if d["day"] >= a.since]
    days = sorted(d["day"] for d in datas)
    if a.fetch_daily:
        do_fetch(datas)
        return
    daily = pickle.load(open(DAILY, "rb")) if os.path.exists(DAILY) else {}
    nds = name_days(datas, daily)
    unk = Counter(g for n in nds for g in GATES if n["gates"].get(g) is None)
    print(f"days {len(days)} (half A ends {E.split(days)}), board name-days {len(nds)}, "
          f"daily symbols {len(daily)}, unknown per gate {dict(unk)}")
    if os.path.exists(REFUSED):
        rf = pickle.load(open(REFUSED, "rb"))
        print("refused symbols per day:", {d: len(v) for d, v in rf["refused"].items() if v})
    if a.forward_g1:
        if not a.since:
            raise SystemExit("--forward-g1 needs --since (the first forward session)")
        md, _res = forward_g1(nds)
        print(md)
        return
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    md = report(datas, daily)
    print(md)
    with open(os.path.join(OUT_DIR, "result.md"), "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
