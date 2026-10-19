#!/usr/bin/env python3
"""Short the desk's own buy fills with the live exits mirrored. Spec: docs/studies/fill_short_prereg.json.

Each desk BUY fill 2026-09-23..10-16 becomes a short at the first SIP round-lot print after it. The desk's own exit
code (call_exits.simulate -> replay_session.FakeBroker._live_exit) covers it, run on the mirrored path p' = E^2/p,
so the stop sits above, the trail and ratchet follow the low, and trail_dc reads mirrored bars. The cover is the
first real print after the exit decision. Data-only account; never places an order.

  TH_DATA_SECRETS=$PWD/secrets_data.json nice -n 19 .venv/bin/python tools/studies/fill_short.py
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("REPO") or Path(__file__).resolve().parent.parent.parent)
for p in (ROOT, ROOT / "tools", ROOT / "tools" / "studies"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

ET = ZoneInfo("America/New_York")
FIRST, LAST, A_END = "2026-09-23", "2026-10-16", "2026-10-02"
OUT = ROOT / "ai_reports" / "fill_short"
RESULT = ROOT / "ai_reports" / "fill_short_result.md"
SNAP_BASE = Path(os.getenv("SESSION_SNAPSHOT_DIR") or Path.home() / "session_snapshots")
OPEN_MIN, LAST_FILL_MIN = 9 * 60 + 30, 15 * 60 + 45
FEE_BP = 0.5
STOP_SLIP_BP = 7.0
FIXED_HOLD_S = 1800.0
TIER_COST_BP = {"5-10": 40.0, "2-5": 100.0}
MIN_DAYS, MIN_TRADES = 12, 200
T_PASS, T_PROMISING = 2.0, 1.5
UNKNOWN_MAX = 0.25


# ── pure parts (tested) ──────────────────────────────────────────────────────

def mirror_trades(trades, e: float):
    return [(t, e * e / p, s) for t, p, s in trades if p > 0]


def mirror_bars(bars, e: float):
    """(ts, high, low, close) -> the mirrored bar: high' = e^2/low, low' = e^2/high."""
    return [(t, e * e / lo, e * e / hi, e * e / c) for t, hi, lo, c in bars if lo > 0 and hi > 0 and c > 0]


def short_bp(entry: float, cover: float) -> float:
    return (entry - cover) / entry * 1e4


def tier(px: float) -> str:
    return "10+" if px >= 10 else "5-10" if px >= 5 else "2-5" if px >= 2 else "<2"


def ssr_on(low_before: float | None, prev_close: float | None, prev_low: float | None,
           prev_prev_close: float | None) -> bool:
    """Short-sale restriction at entry: a 10% drop from the prior close today before the entry, or yesterday."""
    if low_before and prev_close and low_before <= 0.90 * prev_close:
        return True
    return bool(prev_low and prev_prev_close and prev_low <= 0.90 * prev_prev_close)


def et_min(ts: float) -> int:
    d = datetime.fromtimestamp(ts, ET)
    return d.hour * 60 + d.minute


def load_fills(days: list[str], dirs: list[Path]) -> tuple[list[dict], Counter]:
    """Desk BUY fills, one per order_id (earliest fill time), filled 09:30-15:45 ET."""
    seen: dict[str, dict] = {}
    n = Counter()
    for day in days:
        for base in dirs:
            p = base / day / "fills.jsonl" if base.name != "fills" else base / f"{day}.jsonl"
            if not p.exists():
                continue
            for line in open(p):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("event") != "fill" or str(r.get("side")).lower() not in ("buy", "orderside.buy"):
                    continue
                px, ts = float(r.get("filled_avg_price") or 0), float(r.get("ts") or 0)
                oid = str(r.get("order_id") or "") or f"{r.get('symbol')}|{ts:.0f}"
                if px <= 0 or not r.get("symbol"):
                    continue
                n["rows"] += 1
                if oid not in seen or ts < seen[oid]["ts"]:
                    seen[oid] = {"oid": oid, "symbol": str(r["symbol"]).upper(), "ts": ts, "fill": px,
                                 "day": datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")}
    out = []
    for f in sorted(seen.values(), key=lambda f: f["ts"]):
        if f["day"] not in days or not (OPEN_MIN <= et_min(f["ts"]) < LAST_FILL_MIN):
            n["outside_window"] += 1
            continue
        out.append(f)
    n["orders"] = len(out)
    return out, n


def day_stat(xs: list[float]) -> dict:
    import math
    n = len(xs)
    if n == 0:
        return {"mean": None, "t": None, "n": 0, "sd": None}
    mu = statistics.fmean(xs)
    if n < 3:
        return {"mean": mu, "t": None, "n": n, "sd": None}
    sd = statistics.stdev(xs)
    return {"mean": mu, "t": (mu / (sd / math.sqrt(n))) if sd > 0 else None, "n": n, "sd": sd}


def by_day(rows: list[dict], key: str) -> dict[str, float]:
    g = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            g[r["day"]].append(r[key])
    return {d: statistics.fmean(v) for d, v in g.items()}


def word(rows: list[dict], unknown_share: float) -> tuple[str, dict]:
    """The prereg's call on the primary rows (each has day, net, slip)."""
    if unknown_share > UNKNOWN_MAX:
        return "NOT READ (asset flags unknown)", {}
    days = by_day(rows, "net")
    if len(days) < MIN_DAYS or len(rows) < MIN_TRADES:
        return "UNDERPOWERED", {}
    a = day_stat([v for d, v in days.items() if d <= A_END])
    b = day_stat([v for d, v in days.items() if d > A_END])
    pool = day_stat(list(days.values()))
    slip = day_stat(list(by_day(rows, "slip").values()))
    best3 = sorted(days, key=days.get, reverse=True)[:3]
    drop = day_stat([v for d, v in days.items() if d not in best3])
    info = {"A": a, "B": b, "pool": pool, "slip": slip, "drop3": drop, "best3": best3}

    def pos(x):
        return x is not None and x > 0
    both = pos(a["mean"]) and pos(b["mean"])
    if both and (pool["t"] or 0) >= T_PASS and pos(slip["mean"]) and pos(drop["mean"]):
        return "PASS", info
    if both and (pool["t"] or 0) >= T_PROMISING:
        return "PROMISING", info
    return "FAIL", info


# ── one fill ─────────────────────────────────────────────────────────────────

def run_side(CE, sym: str, key: str, ts: float, cl, bars, cfg: dict, end: float, sp, variant: str, short: bool):
    """The desk's exits from the first print after ts, growing the fetched window until the trade closes."""
    h = min(end, ts + FIXED_HOLD_S + 600)
    while True:
        trades = CE.cached_trades(cl, sym, key, ts, h)
        fill = CE.first_print_after(trades, ts, CE.ENTRY_WAIT_S)
        if fill is None:
            if h >= end or trades:
                return {"status": "no_fill"}, trades
        else:
            e = fill[1]
            tr, br = (mirror_trades(trades, e), mirror_bars(bars, e)) if short else (trades, bars)
            res = CE.simulate(sym, ts, tr, br, cfg, h, spread_bp=sp, variant=variant)
            if res["status"] == "final" or h >= end:
                if res["status"] == "open":
                    res = CE.finalize_at_session_end(res, h)
                if short and res["status"] == "final":
                    res["exit"] = e * e / res["exit"]
                    res["gross_bp"] = round(short_bp(e, res["exit"]), 1)
                return res, trades
        h = min(end, h + 3600)


def flatten_ts(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=15, minute=50, tzinfo=ET).timestamp()


def fixed_hold(CE, trades, entry_ts: float, e: float, day: str) -> float | None:
    x = CE.first_print_after(trades, min(entry_ts + FIXED_HOLD_S, flatten_ts(day)))
    return None if x is None else short_bp(e, x[1])


# ── data ─────────────────────────────────────────────────────────────────────

def data_keys() -> dict:
    path = os.environ.get("TH_DATA_SECRETS", "")
    if not path or not os.path.exists(path):
        sys.exit("REFUSING: set TH_DATA_SECRETS to the data-only account's secrets_data.json")
    d = json.load(open(path))
    return {"api_key": d["alpaca_data_api_key"], "secret_key": d["alpaca_data_secret_key"]}


def asset_flags(syms: set[str]) -> dict[str, list]:
    """[shortable, easy_to_borrow] or None (unknown). Today's flags, from the data-only account's keys."""
    p = OUT / "assets.json"
    have = json.load(open(p)) if p.exists() else {}
    old = ROOT / "ai_reports" / "short_squares" / "shortable.json"
    tc = None
    try:
        from alpaca.trading.client import TradingClient
        k = data_keys()
        tc = TradingClient(k["api_key"], k["secret_key"], paper=True)
    except Exception as e:  # noqa: BLE001
        print(f"  asset client unavailable: {str(e)[:100]}", file=sys.stderr)
    for s in sorted(syms - set(have)):
        got = None
        if tc is not None:
            try:
                a = tc.get_asset(s)
                got = [bool(a.shortable), bool(a.easy_to_borrow)]
            except Exception as e:  # noqa: BLE001
                if "not found" not in str(e).lower():
                    print(f"  asset {s}: {str(e)[:80]}", file=sys.stderr)
            time.sleep(0.25)
        have[s] = got
    if old.exists():
        for s, v in json.load(open(old)).items():
            if have.get(s) is None and s in syms:
                have[s] = list(v)
    json.dump(have, open(p, "w"))
    return have


def daily_bars(cl, syms: set[str]) -> dict[str, list]:
    """{sym: [(day, low, close)]} RAW SIP daily bars around the window, for the SSR check."""
    p = OUT / "daily.json"
    have = json.load(open(p)) if p.exists() else {}
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    need = sorted(syms - set(have))
    start = datetime(2026, 9, 10, tzinfo=ET).astimezone(timezone.utc)
    end = datetime(2026, 10, 17, tzinfo=ET).astimezone(timezone.utc)
    for i in range(0, len(need), 100):
        chunk = need[i:i + 100]
        got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start,
                                                 end=end, feed=DataFeed.SIP, adjustment=Adjustment.RAW)).data or {}
        for s in chunk:
            have[s] = [(b.timestamp.astimezone(ET).strftime("%Y-%m-%d"), float(b.low), float(b.close))
                       for b in got.get(s, [])]
        time.sleep(0.5)
    json.dump(have, open(p, "w"))
    return have


def ssr_for(daily: list, day: str, low_before: float | None) -> bool:
    prior = [r for r in daily if r[0] < day]
    pc = prior[-1][2] if prior else None
    pl = prior[-1][1] if prior else None
    ppc = prior[-2][2] if len(prior) > 1 else None
    return ssr_on(low_before, pc, pl, ppc)


def sessions() -> list[str]:
    out, d = [], datetime.strptime(FIRST, "%Y-%m-%d")
    while d.strftime("%Y-%m-%d") <= LAST:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


# ── report ───────────────────────────────────────────────────────────────────

def pct(xs, q):
    xs = sorted(xs)
    return xs[max(0, min(len(xs) - 1, int(q * (len(xs) - 1))))] if xs else None


def block(title: str, rows: list[dict], tier_cost: float | None = None) -> list[str]:
    f = lambda x: "—" if x is None else f"{x:+.1f}"  # noqa: E731
    g = lambda x: "—" if x is None else f"{x:+.2f}"  # noqa: E731
    lines = [f"### {title}", "",
             "| half | trades | days | short net/trade bp (t) | median | wins | -7 bp stops | scaled BE | fixed 30 m | long control | MDE at t 2 |"
             + (" tier cost |" if tier_cost else ""),
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|" + ("---:|" if tier_cost else "")]
    for tag, sl in (("all", rows), ("A", [r for r in rows if r["day"] <= A_END]), ("B", [r for r in rows if r["day"] > A_END])):
        if not sl:
            continue
        net = day_stat(list(by_day(sl, "net").values()))
        m = lambda k: day_stat(list(by_day(sl, k).values()))["mean"]  # noqa: E731
        md = None if net["sd"] is None else 2 * net["sd"] / net["n"] ** 0.5
        nets = [r["net"] for r in sl]
        row = (f"| {tag} | {len(sl)} | {net['n']} | {f(net['mean'])} ({g(net['t'])}) | {f(statistics.median(nets))} | "
               f"{sum(x > 0 for x in nets) / len(nets):.0%} | {f(m('slip'))} | {f(m('scaled'))} | {f(m('fixed30'))} | "
               f"{f(m('long'))} | {f(md)} |")
        if tier_cost:
            row += f" {f(day_stat(list(by_day([{**r, 'tc': r['gross'] - tier_cost - FEE_BP} for r in sl], 'tc').values()))['mean'])} |"
        lines.append(row)
    sq = [r["squeeze_bp"] for r in rows if r.get("squeeze_bp") is not None]
    if sq:
        lines += ["", f"Squeeze against the short before the cover: p50 {pct(sq, .5) / 100:+.2f}%, p95 {pct(sq, .95) / 100:+.2f}%, "
                      f"p99 {pct(sq, .99) / 100:+.2f}%, max {max(sq) / 100:+.2f}%."]
    return lines + [""]


def main() -> None:
    import config
    import call_exits as CE
    import replay_costing as RC
    OUT.mkdir(parents=True, exist_ok=True)
    CE.CACHE_DIR = OUT / "trades"
    cfg = config.load_config_ro()
    if bool(cfg.get("ai_exit_supertrend", False)):
        sys.exit("REFUSING: ai_exit_supertrend is on; its bars are not mirrored (prereg)")
    days = sessions()
    fills, n = load_fills(days, [ROOT / "ai_reports" / "fills", SNAP_BASE])
    print(f"fills: {dict(n)}; sessions with a fill {len({f['day'] for f in fills})}", file=sys.stderr, flush=True)
    if not fills:
        sys.exit("no desk buy fills found in ai_reports/fills or ~/session_snapshots")
    cl = CE._client()
    syms = {f["symbol"] for f in fills}
    flags = asset_flags(syms)
    daily = daily_bars(cl, syms)
    qcache, box = RC.load_cache(), {}
    bar_cache: dict[tuple, list] = {}
    rows, counts, reasons = [], Counter(), Counter()
    for i, fl in enumerate(fills):
        sym, ts, day = fl["symbol"], fl["ts"], fl["day"]
        if i % 25 == 0:
            print(f"  {i}/{len(fills)} {day} {sym}", file=sys.stderr, flush=True)
        end = datetime.strptime(day, "%Y-%m-%d").replace(hour=16, tzinfo=ET).timestamp()
        key = f"{sym}|{ts:.3f}"
        try:
            if (sym, day) not in bar_cache:
                bar_cache[(sym, day)] = CE.fetch_bars(cl, sym, day, end)
            bars = bar_cache[(sym, day)]
            sp_q = RC.entry_spread_bp(qcache, sym, ts, box)
            res, trades = run_side(CE, sym, key, ts, cl, bars, cfg, end, sp_q, "asis", short=True)
            if res["status"] != "final":
                counts["no_entry_print"] += 1
                continue
            sc, _ = run_side(CE, sym, key, ts, cl, bars, cfg, end, sp_q, "scaled", short=True)
            lg, _ = run_side(CE, sym, key, ts, cl, bars, cfg, end, sp_q, "asis", short=False)
        except Exception as e:  # noqa: BLE001 - one bad request must not end the run; counted
            counts["fetch_error"] += 1
            print(f"  {key}: {type(e).__name__} {str(e)[:120]}", file=sys.stderr)
            continue
        e_px, e_ts = res["entry"], res["entry_ts"]
        before = [b[2] for b in bars if b[0] + 60 <= e_ts and et_min(b[0]) >= OPEN_MIN]
        hold = [p for t, p, s in trades if e_ts <= t <= res["exit_ts"] and s >= CE.ROUND_LOT]
        fx = fixed_hold(CE, trades, e_ts, e_px, day)
        rows.append({"day": day, "symbol": sym, "ts": ts, "entry": e_px, "tier": tier(e_px), "sp_q": sp_q,
                     "flags": flags.get(sym), "ssr": ssr_for(daily.get(sym, []), day, min(before) if before else None),
                     "gross": res["gross_bp"], "reason": res["reason"],
                     "scaled_gross": sc.get("gross_bp") if sc.get("status") == "final" else None,
                     "long_gross": lg.get("gross_bp") if lg.get("status") == "final" else None,
                     "fixed30_gross": fx, "squeeze_bp": (max(hold) / e_px - 1) * 1e4 if hold else None})
    RC.save_cache(qcache)
    pooled = defaultdict(list)
    for r in rows:
        if r["sp_q"] is not None:
            pooled[r["day"]].append(r["sp_q"])
    for r in rows:
        sp = r["sp_q"] if r["sp_q"] is not None else (statistics.median(pooled[r["day"]]) if pooled[r["day"]] else 20.0)
        counts["no_quote"] += r["sp_q"] is None
        c = sp + FEE_BP
        r["spread_bp"] = sp
        r["net"] = r["gross"] - c
        r["slip"] = r["net"] - (STOP_SLIP_BP if any(k in str(r["reason"]) for k in ("stop", "trail", "ratchet")) else 0.0)
        r["scaled"] = None if r["scaled_gross"] is None else r["scaled_gross"] - c
        r["long"] = None if r["long_gross"] is None else r["long_gross"] - sp
        r["fixed30"] = None if r["fixed30_gross"] is None else r["fixed30_gross"] - c
        reasons[r["reason"]] += 1
    json.dump(rows, open(OUT / "rows.json", "w"))

    ten = [r for r in rows if r["tier"] == "10+"]
    unknown = sum(1 for r in ten if r["flags"] is None) / len(ten) if ten else 1.0
    etb = lambda r: r["flags"] is not None and r["flags"][0] and r["flags"][1]  # noqa: E731
    primary = [r for r in ten if etb(r) and not r["ssr"]]
    w, info = word(primary, unknown)
    fl = Counter("unknown" if r["flags"] is None else "easy" if etb(r) else "hard" if r["flags"][0] else "not shortable"
                 for r in rows)
    lines = ["# Short the desk's fills with the live exits mirrored (fill_short_prereg)", "",
             f"Sessions {FIRST}..{LAST}; half A ..{A_END}, half B after. Fills {dict(n)}. Scored {len(rows)}; "
             f"{dict(counts)}.", "",
             f"Price tiers {dict(Counter(r['tier'] for r in rows))}. Asset flags (today's) {dict(fl)}; "
             f"unknown among $10+: {unknown:.0%}. SSR at entry: {sum(r['ssr'] for r in rows)}.", "",
             "Net = short return - full quoted spread at entry - 0.5 bp fees. Long control = the same fill long, same exits unmirrored, minus the spread.", ""]
    lines += block("PRIMARY: $10+, shortable and easy to borrow, no SSR", primary)
    lines += block("$10+, any flags (information)", ten)
    for t in ("5-10", "2-5"):
        tr = [r for r in rows if r["tier"] == t]
        if tr:
            lines += block(f"${t} (information; last column charges the {TIER_COST_BP[t] / 100:.2f}% tier round trip)", tr,
                           TIER_COST_BP[t])
    lines += [f"Exit reasons: {dict(reasons.most_common(8))}", ""]
    if info:
        f = lambda x: "—" if x is None else f"{x:+.1f}"  # noqa: E731
        lines += [f"Pooled day-mean {f(info['pool']['mean'])} bp (t {info['pool']['t'] or 0:+.2f}); A {f(info['A']['mean'])}, "
                  f"B {f(info['B']['mean'])}; -7 bp stops {f(info['slip']['mean'])}; without the 3 best days "
                  f"{f(info['drop3']['mean'])} (best days {', '.join(info['best3'])}).", ""]
    lines += [f"**{w}**"]
    md = "\n".join(lines)
    print(md, flush=True)
    RESULT.write_text(md + "\n")


if __name__ == "__main__":
    main()
