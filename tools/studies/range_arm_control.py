#!/usr/bin/env python3
"""range_arm_control.py — per-input check, location control and scoring for docs/studies/range_arm_replay_prereg.json.

The nightly replay (scripts/tight_trail_replay.sh) writes DAY-range_arm.json (+ DAY-range_arm_rerun.json, the
same session replayed again) and DAY-base.json (the reference). This tool:

  check DAY      per-input counts of the range_arm run (order-block coverage, engine freshness, checks failing
                 (a) / (b) / (c) alone, arms), the BUILD FAILURE verdict, and the determinism check (the rerun
                 must hold identical range_arm trades). Never prints P&L. Writes DAY.range_arm_check.json
                 (outside the nightly DAY-*.json glob).
  score DAY...   trades, no-NBBO counts, control fallbacks and re-score price misses per day. P&L, the control
                 difference, the reference difference and the PASS / FAIL verdict print ONLY with --read: the
                 prereg reads once, at session 12; development runs are blind to range_arm and control P&L.

Statistic (prereg): per trade net bp = exit/entry - 1 at the fill MINUS the full SIP quoted spread at entry
(tools/studies/replay_costing.entry_spread_bp); a trade with no NBBO is charged that arm's median spread of
the day (never dropped, counted). Per-day mean; mean over days, t across days (df = days - 1). Entries AND
exits re-scored at the next poll and at +10 s (recorded prices); the pass must hold under all three rules.

Control (prereg): for each range_arm trade, one random minute (seed 71 via sha256 of symbol|day|entry minute)
in the same name, day and ET clock hour where the engine fast %R (recorded signal_state) is < -20 and rising,
not within 10 min of the trade; the same % target / % stop and 30-min time stop; entered at that minute's IEX
1-min close; exits scanned on later IEX 1-min bars (stop wins a same-bar tie); the same spread charged. No
legal minute in the hour -> the next later hour, then the earlier (counted).

RANGE_ARM_RESOLUTIONS (scoring side; listed in the hand-back):
  S1  "Rising" at minute m = the recorded engine %R of minute m (last signal_state value captured inside
      [m, m+60)) above that of minute m-60; a minute without both values is not legal.
  S2  "Not within 10 min" = |m - trade entry minute| > 600 s.
  S3  Control exits: bars starting in [m+60, m+60+1800) and ending by 15:50 ET; STOP when low < stop level
      (strict, as the desk's price < S_btm - $0.01) at the stop level; TARGET when high >= target level at the
      target level; otherwise TIME at the last such bar's close; no later bar -> flat (counted).
  S4  Fill re-scoring applies to the replay's trades (range_arm and the reference); the control is bar-based
      and is the same under every rule. A re-scored price comes from the recording at t + poll_sec / t + 10 s;
      none -> the replay's own fill (counted).
  S5  The fill rule uses the replay's own ret (the reference's T1 blend included); a re-scored rule shifts it
      by the price change: ret + (x'/e' - x/e).
  S6  A day on which an arm has no NBBO on any trade is charged that arm's pooled median over all scored days.
  S7  "Drop the top 5 symbols by P&L" = remove every trade of the 5 symbols with the largest summed net bp.

USAGE (mini, after the nightly):
  .venv/bin/python tools/studies/range_arm_control.py check 2026-10-09 [--dir /tmp/tt_run]
  .venv/bin/python tools/studies/range_arm_control.py score 2026-10-09 2026-10-10 ... [--dir /tmp/tt_run] [--read]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(os.environ.get("REPO") or Path(__file__).resolve().parents[2])
SEED = 71
PR_MAX = -20.0
NEAR_SEC = 600.0
TIME_STOP_SEC = 1800.0
RES_PAD = 0.0002
STOP_PAD_PX = 0.01
MIN_OB_COVERAGE = 0.70
MIN_ENGINE_FRESH = 0.50
RULES = ("fill", "next_poll", "plus10")


# ── per-input check ──────────────────────────────────────────────────────────

def trade_key(t: dict) -> tuple:
    return (t.get("symbol"), round(float(t.get("entry_ts") or 0), 3), round(float(t.get("entry_px") or 0), 6),
            round(float(t.get("exit_ts") or 0), 3), round(float(t.get("exit_px") or 0), 6), t.get("reason"))


def trades_of(result: dict) -> list[dict]:
    return list(result.get("closed") or []) + list(result.get("open_at_end") or [])


def deterministic(a: dict, b: dict) -> bool:
    """Same session twice -> identical range_arm trades."""
    return sorted(map(trade_key, trades_of(a))) == sorted(map(trade_key, trades_of(b)))


def input_check(result: dict, rerun: dict | None = None) -> dict:
    """The prereg per-input check for one session's range_arm run. No P&L in the output."""
    c = dict(result.get("range_arm_inputs") or {})
    n = int(c.get("checks") or 0)
    share = (lambda k: round(int(c.get(k) or 0) / n, 4) if n else None)
    out = {
        "checks": n, "ob_reading_share": share("ob_reading"), "ob_support_side_share": share("ob_sup_side"),
        "ob_resistance_side_share": share("ob_res_side"), "engine_fresh_share": share("engine_fresh"),
        "fail_a_only": int(c.get("fail_a_only") or 0), "fail_b_only": int(c.get("fail_b_only") or 0),
        "fail_c_only": int(c.get("fail_c_only") or 0), "pass_abc": int(c.get("pass_abc") or 0),
        "ob_stale_bar": int(c.get("ob_stale_bar") or 0), "ob_stale_absorb": int(c.get("ob_stale_absorb") or 0),
        "ob_none": int(c.get("ob_none") or 0), "range_arms": int(c.get("arms") or 0),
        "known_ts_after_minute": int(c.get("known_ts_after_minute") or 0),
        "trades": len(trades_of(result)), "ob_feed": c.get("ob_feed"),
    }
    why = []
    if not n:
        why.append("no range_arm book checks recorded")
    else:
        if out["ob_reading_share"] < MIN_OB_COVERAGE:
            why.append(f"order-block readings on {out['ob_reading_share']:.0%} of checks (< 70%)")
        if out["engine_fresh_share"] < MIN_ENGINE_FRESH:
            why.append(f"fresh engine reads on {out['engine_fresh_share']:.0%} of checks (< 50%)")
    if out["known_ts_after_minute"]:
        why.append(f"{out['known_ts_after_minute']} arms used a block known after the check minute")
    if rerun is not None:
        out["deterministic"] = deterministic(result, rerun)
        if not out["deterministic"]:
            why.append("the rerun of the same session gave different range_arm trades")
    else:
        out["deterministic"] = None
    out["build_failure"] = bool(why)
    out["build_failure_why"] = why
    return out


# ── control ──────────────────────────────────────────────────────────────────

def minute_of(ts: float) -> float:
    return float(int(float(ts) // 60) * 60)


def et_hour(ts: float) -> int:
    return datetime.fromtimestamp(float(ts), ET).hour


def eod_cut(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=15, minute=50, tzinfo=ET).timestamp()


def legal_minutes(pctr: dict[float, float], hour: int, trade_minute: float) -> list[float]:
    """Minutes in ET clock hour `hour` with engine %R < -20 and above the previous minute's, > 10 min away."""
    out = []
    for m, v in pctr.items():
        if et_hour(m) != hour or abs(m - trade_minute) <= NEAR_SEC:
            continue
        prev = pctr.get(m - 60.0)
        if v is not None and prev is not None and v < PR_MAX and v > prev:
            out.append(m)
    return sorted(out)


def pick_minute(cands: list[float], sym: str, day: str, trade_minute: float) -> float | None:
    if not cands:
        return None
    hm = datetime.fromtimestamp(trade_minute, ET).strftime("%H:%M")
    h = int(hashlib.sha256(f"{SEED}|{sym}|{day}|{hm}".encode()).hexdigest(), 16)
    return sorted(cands)[h % len(cands)]


def control_trade(trade: dict, day: str, bars: list[tuple], pctr: dict[float, float]) -> dict:
    """One control for a range_arm trade (see the module doc). bars: IEX 1-min (ts, o, h, l, c, ...)."""
    sym, entry_ts, entry_px = trade["symbol"], float(trade["entry_ts"]), float(trade["entry_px"])
    tgt_pct = float(trade["range_r_btm"]) * (1.0 - RES_PAD) / entry_px - 1.0
    stop_pct = (float(trade["range_s_btm"]) - STOP_PAD_PX) / entry_px - 1.0
    tm = minute_of(entry_ts)
    h0 = et_hour(tm)
    m, fallback = None, None
    for i, h in enumerate((h0, h0 + 1, h0 - 1)):
        m = pick_minute(legal_minutes(pctr, h, tm), sym, day, tm)
        if m is not None:
            fallback = ("same_hour", "later_hour", "earlier_hour")[i]
            break
    if m is None:
        return {"symbol": sym, "control": None, "fallback": "none"}
    by_ts = {float(r[0]): r for r in bars}
    if m not in by_ts:
        return {"symbol": sym, "control": None, "fallback": "no_entry_bar", "minute": m}
    c_entry = float(by_ts[m][4])
    tgt, stop = c_entry * (1.0 + tgt_pct), c_entry * (1.0 + stop_pct)
    t_in = m + 60.0
    cut = min(t_in + TIME_STOP_SEC, eod_cut(day))
    later = sorted((r for r in bars if t_in <= float(r[0]) and float(r[0]) + 60.0 <= cut), key=lambda r: r[0])
    exit_px, why = c_entry, "flat_no_bars"
    for r in later:
        if float(r[3]) < stop:
            exit_px, why = stop, "stop"
            break
        if float(r[2]) >= tgt:
            exit_px, why = tgt, "target"
            break
        exit_px, why = float(r[4]), "time"
    return {"symbol": sym, "control": {"minute": m, "entry_px": c_entry, "exit_px": exit_px, "reason": why,
                                       "ret": exit_px / c_entry - 1.0}, "fallback": fallback}


# ── data on the mini ─────────────────────────────────────────────────────────

def engine_pctr_minutes(snap: Path, syms: set[str]) -> dict[str, dict[float, float]]:
    """{sym: {minute: last recorded engine fast %R inside that minute}} from state_snapshots signal_state."""
    import gzip
    out: dict[str, dict[float, float]] = defaultdict(dict)
    p = snap / "state_snapshots.jsonl.gz"
    if not p.exists():
        return {}
    with gzip.open(p, "rt") as f:
        for line in f:
            if '"signal_state.json"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            m = minute_of(float(r.get("ts") or 0))
            for s, row in ((r.get("data") or {}).get("tickers") or {}).items():
                if s in syms and isinstance(row, dict) and row.get("pctr") is not None:
                    try:
                        out[s][m] = float(row["pctr"])
                    except (TypeError, ValueError):
                        pass
    return dict(out)


class RecordedPrices:
    """The recorded dashboard price of a name at a time (what the replay fills at), for fill re-scoring."""

    def __init__(self, snap: Path):
        self.snap = snap

    def lookup(self, queries: list[tuple[str, float]]) -> dict[tuple[str, float], float | None]:
        sys.path.insert(0, str(ROOT / "tools"))
        import replay_session as rp
        out: dict[tuple[str, float], float | None] = {}
        p = self.snap / "state_snapshots.jsonl.gz"
        if not p.exists():
            return {q: None for q in queries}
        rec = rp.Recording(p)
        for sym, t in sorted(set(queries), key=lambda q: q[1]):
            rec.advance(t)
            st = rp.build_dashboard_state(rec, t, None)
            px = next((r.get("price") for r in st.get("tickers") or [] if r.get("ticker") == sym), None)
            try:
                out[(sym, t)] = float(px) if px and float(px) > 0 else None
            except (TypeError, ValueError):
                out[(sym, t)] = None
        return out


def load_bars(day: str) -> dict:
    cache = Path(os.getenv("REPLAY_CACHE_DIR") or Path.home() / "replay_cache") / f"bars_v2_{day}.pkl"
    return pickle.loads(cache.read_bytes()) if cache.exists() else {}


# ── statistics ───────────────────────────────────────────────────────────────

def charge_spreads(rows: list[dict], pooled_median: float | None = None) -> int:
    """Fill r['spr'] where it is None with the day median of the rows that have one. Returns the count charged."""
    known = [r["spr"] for r in rows if r.get("spr") is not None]
    med = statistics.median(known) if known else pooled_median
    n = 0
    for r in rows:
        if r.get("spr") is None:
            r["spr"], r["no_nbbo"] = med, True
            n += 1
    return n


def rescored_ret(t: dict, rule: str, prices: dict, poll_sec: float) -> tuple[float, bool]:
    """(ret under a fill rule, price found). S5."""
    ret = float(t["ret"])
    if rule == "fill":
        return ret, True
    dt = float(poll_sec) if rule == "next_poll" else 10.0
    e = prices.get((t["symbol"], float(t["entry_ts"]) + dt))
    x = prices.get((t["symbol"], float(t["exit_ts"]) + dt))
    if not e or not x:
        return ret, False
    return ret + (x / e - float(t["exit_px"]) / float(t["entry_px"])), True


def day_t(day_means: list[float]) -> tuple[float | None, float | None]:
    if not day_means:
        return None, None
    m = statistics.mean(day_means)
    if len(day_means) < 2 or statistics.stdev(day_means) == 0:
        return m, None
    return m, m / (statistics.stdev(day_means) / math.sqrt(len(day_means)))


def per_day(rows: list[dict], key: str = "net") -> dict[str, float]:
    by = defaultdict(list)
    for r in rows:
        by[r["day"]].append(r[key])
    return {d: statistics.mean(v) for d, v in by.items()}


def verdict(rule_rows: dict[str, list[dict]], ctrl_diff_days: dict[str, list[float]],
            ref_diff_days: dict[str, list[float]]) -> tuple[str, list[str]]:
    """PASS / FAIL / CLOSED UNPROVEN per the prereg, over all three fill rules."""
    notes, ok = [], True
    for rule, rows in rule_rows.items():
        days = per_day(rows)
        m, t = day_t(list(days.values()))
        neg = sum(1 for v in days.values() if v < 0) / len(days) if days else 1.0
        best = max(days, key=days.get) if days else None
        m_nobest, _ = day_t([v for d, v in days.items() if d != best])
        sym_pl = Counter()
        for r in rows:
            sym_pl[r["symbol"]] += r["net"]
        top5 = {s for s, _ in sym_pl.most_common(5)}
        m_notop, _ = day_t(list(per_day([r for r in rows if r["symbol"] not in top5]).values()))
        cm, ct = day_t(ctrl_diff_days.get(rule, []))
        rm, rt = day_t(ref_diff_days.get(rule, []))
        checks = {
            "net > 0, t >= 2": m is not None and m > 0 and t is not None and t >= 2,
            ">= 60 trades": len(rows) >= 60,
            "negative days <= 30%": neg <= 0.30,
            "> 0 without the best day": m_nobest is not None and m_nobest > 0,
            "> 0 without the top 5 symbols": m_notop is not None and m_notop > 0,
            "minus control >= +3 bp, t >= 2": cm is not None and cm >= 3 and ct is not None and ct >= 2,
            "minus reference >= +3 bp, t >= 2": rm is not None and rm >= 3 and rt is not None and rt >= 2,
        }
        for k, v in checks.items():
            notes.append(f"{rule:<9} {k:<34} {'yes' if v else 'NO'}")
            ok = ok and v
    if ok:
        return "PASS", notes
    fill = rule_rows.get("fill") or []
    m, _ = day_t(list(per_day(fill).values()))
    rm, rt = day_t(ref_diff_days.get("fill", []))
    if (len(fill) >= 60 and m is not None and m <= 0) or (rm is not None and rm <= -3 and rt is not None
                                                           and rt <= -2):
        return "FAIL", notes
    return "CLOSED UNPROVEN", notes


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_check(args) -> int:
    d = Path(args.dir)
    main = d / f"{args.day}-range_arm.json"
    if not main.exists():
        print(f"no {main}")
        return 1
    res = json.loads(main.read_text())
    rr = d / f"{args.day}-range_arm_rerun.json"
    out = input_check(res, json.loads(rr.read_text()) if rr.exists() else None)
    out["day"] = args.day
    (d / f"{args.day}.range_arm_check.json").write_text(json.dumps(out, indent=1))
    print(f"RANGE_ARM PER-INPUT {args.day}: " + json.dumps({k: v for k, v in out.items() if k != 'day'}))
    print("BUILD FAILURE: " + "; ".join(out["build_failure_why"]) if out["build_failure"] else "build check: ok")
    return 0


def cmd_score(args) -> int:
    sys.path.insert(0, str(ROOT / "tools" / "studies"))
    sys.path.insert(0, str(ROOT / "tools"))
    import replay_costing as rc
    d = Path(args.dir)
    cache, box = rc.load_cache(), {}
    snap_base = Path(os.getenv("SESSION_SNAPSHOT_DIR") or Path.home() / "session_snapshots")
    arms = {"range_arm": "range_arm", "reference": "base"}
    rows: dict[str, list[dict]] = {a: [] for a in arms}
    ctrl_rows: list[dict] = []
    counts = defaultdict(Counter)
    for day in args.days:
        res = {}
        for arm, var in arms.items():
            p = d / f"{day}-{var}.json"
            res[arm] = json.loads(p.read_text()) if p.exists() else {"closed": []}
        poll = float(res["range_arm"].get("poll_sec") or 10.0)
        queries = [(t["symbol"], float(t[k]) + dt) for a in arms for t in res[a].get("closed") or []
                   for k in ("entry_ts", "exit_ts") for dt in (poll, 10.0)]
        prices = RecordedPrices(snap_base / day).lookup(queries)
        for arm in arms:
            day_rows = []
            for t in res[arm].get("closed") or []:
                if t.get("ret") is None or not t.get("entry_px"):
                    continue
                r = {"day": day, "symbol": t["symbol"], "t": t,
                     "spr": rc.entry_spread_bp(cache, t["symbol"], t["entry_ts"], box)}
                for rule in RULES:
                    ret, found = rescored_ret(t, rule, prices, poll)
                    r[f"gross_{rule}"] = ret * 1e4
                    if not found:
                        counts[day][f"{arm}_rescore_price_missing_{rule}"] += 1
                day_rows.append(r)
            counts[day][f"{arm}_trades"] = len(day_rows)
            counts[day][f"{arm}_no_nbbo"] = sum(1 for r in day_rows if r["spr"] is None)
            rows[arm] += day_rows
        bars = load_bars(day)
        rt = [r for r in rows["range_arm"] if r["day"] == day]
        pctr = engine_pctr_minutes(snap_base / day, {r["symbol"] for r in rt})
        for r in rt:
            c = control_trade(r["t"], day, (bars.get(r["symbol"]) or {}).get("iex") or [], pctr.get(r["symbol"], {}))
            counts[day][f"control_{c['fallback']}"] += 1
            if c["control"] is not None:
                ctrl_rows.append({"day": day, "symbol": r["symbol"], "parent": r, "ret": c["control"]["ret"]})
        rc.save_cache(cache)
    for arm in arms:
        pooled = [r["spr"] for r in rows[arm] if r.get("spr") is not None]
        pm = statistics.median(pooled) if pooled else None
        for day in args.days:
            charge_spreads([r for r in rows[arm] if r["day"] == day], pm)
    print(f"RANGE_ARM SCORE {args.days[0]}..{args.days[-1]} ({len(args.days)} sessions): counts per session")
    for day in args.days:
        print(f"  {day} " + " ".join(f"{k}={v}" for k, v in sorted(counts[day].items())))
    if not args.read:
        print("\nP&L withheld: development runs are blind to range_arm and control P&L (prereg). "
              "--read only at the single session-12 read.")
        return 0
    fails = []
    for day in args.days:
        ck = d / f"{day}.range_arm_check.json"
        if not ck.exists() or json.loads(ck.read_text()).get("build_failure"):
            fails.append(day)
    traded = {r["day"] for r in rows["range_arm"]}
    print(f"\nbuild failures (or no check file): {fails or 'none'}; sessions with range_arm trades "
          f"{len(traded)}/{len(args.days)}")
    if len(fails) > 3 or len(traded) < 8:
        print("VERDICT: VACUOUS (not read): > 3 build failures or trades on fewer than 8 sessions")
        return 0
    rule_rows, ctrl_diff, ref_diff = {}, {}, {}
    for rule in RULES:
        rr = [dict(r, net=r[f"gross_{rule}"] - r["spr"]) for r in rows["range_arm"] if r["spr"] is not None]
        ref = [dict(r, net=r[f"gross_{rule}"] - r["spr"]) for r in rows["reference"] if r["spr"] is not None]
        cr = [dict(c, net=c["ret"] * 1e4 - c["parent"]["spr"]) for c in ctrl_rows if c["parent"]["spr"] is not None]
        rule_rows[rule] = rr
        a, c_, f = per_day(rr), per_day(cr), per_day(ref)
        ctrl_diff[rule] = [a[x] - c_[x] for x in a if x in c_]
        ref_diff[rule] = [a[x] - f[x] for x in a if x in f]
        m, t = day_t(list(a.values()))
        print(f"\n{rule}: range_arm {len(rr)} trades, net/trade by day {m if m is None else round(m, 2)} bp "
              f"(t {t if t is None else round(t, 2)}); control diff {day_t(ctrl_diff[rule])}; "
              f"reference diff {day_t(ref_diff[rule])} on {len(ref_diff[rule])} paired days "
              f"({len(a) - len(ref_diff[rule])} days left out: an arm had 0 trades)")
    v, notes = verdict(rule_rows, ctrl_diff, ref_diff)
    print("\n" + "\n".join(notes))
    print(f"\nVERDICT: {v}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("day")
    c.add_argument("--dir", default="/tmp/tt_run")
    s = sub.add_parser("score")
    s.add_argument("days", nargs="+")
    s.add_argument("--dir", default="/tmp/tt_run")
    s.add_argument("--read", action="store_true", help="print P&L and the verdict (the single session-12 read)")
    a = ap.parse_args(argv)
    return cmd_check(a) if a.cmd == "check" else cmd_score(a)


if __name__ == "__main__":
    sys.exit(main())
