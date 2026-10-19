#!/usr/bin/env python3
"""Short the desk's own buy fills with the live exits mirrored. Spec: docs/studies/fill_short_prereg.json.

Each desk BUY fill 2026-09-23..10-16 becomes a short at the first SIP round-lot print after it. The desk's own exit
code (call_exits.simulate -> replay_session.FakeBroker._live_exit) covers it, run on the mirrored path p' = E^2/p,
so the stop sits above, the trail and ratchet follow the low, and trail_dc reads mirrored bars. The cover is the
first real print after the exit decision. Data-only account; never places an order.

  TH_DATA_SECRETS=$PWD/secrets_data.json nice -n 19 .venv/bin/python tools/studies/fill_short.py
  ... fill_short.py --replay-dir /tmp/floor2_DATE     # docs/studies/floor2_short_prereg.json: the replay's opens
"""
from __future__ import annotations

import argparse
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
FETCH_ERROR_MAX = 0.05
MIN_HALF_DAYS = 5
# floor2_short_prereg: the replay of the desk with the floor at $2 (scripts/floor2_sweep.sh)
REPLAY_ARM = "floor2"
REPLAY_DAYS = ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14",
               "2026-10-15", "2026-10-16")
REPLAY_A_END = "2026-10-09"
REPLAY_MIN = {"min_days": 8, "min_half": 4, "min_trades": 100, "drop_names": 5}


# ── pure parts (tested) ──────────────────────────────────────────────────────

def mirror_trades(trades, e: float):
    return [(t, e * e / p, s) for t, p, s in trades if p > 0]


def mirror_bars(bars, e: float):
    """(ts, high, low, close) -> the mirrored bar: high' = e^2/low, low' = e^2/high."""
    return [(t, e * e / lo, e * e / hi, e * e / c) for t, hi, lo, c in bars if lo > 0 and hi > 0 and c > 0]


def short_bp(entry: float, cover: float) -> float:
    return (entry - cover) / entry * 1e4


def short_gross(e: float, cover: float, res: dict) -> float:
    """Short return in bp with the T1 blend. res carries the mirrored target (t1_px); its real price is e^2/t1_px."""
    full = short_bp(e, cover)
    if res.get("t1_hit") and res.get("t1_px"):
        sc = float(res.get("scale") or 0.0)
        return sc * short_bp(e, e * e / float(res["t1_px"])) + (1 - sc) * full
    return full


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


def load_replay(rdir: Path, days=REPLAY_DAYS, arm: str = REPLAY_ARM) -> tuple[list[dict], Counter, bool]:
    """Every open of the replay arm (closed list) as a 'fill' at its decision time. failed = a day missing or a
    line in failures.txt."""
    n, out, failed = Counter(), [], False
    fp = rdir / "failures.txt"
    if fp.exists() and fp.read_text().strip():
        failed = True
    for d in days:
        p = rdir / f"{d}_{arm}.json"
        if not p.exists() or p.stat().st_size == 0:
            failed = True
            n["missing_days"] += 1
            continue
        rec = json.loads(p.read_text())
        for t in (rec.get("closed") or []) + (rec.get("open_at_end") or []):
            if not t.get("symbol") or not t.get("entry_ts"):
                continue
            ts = float(t["entry_ts"])
            out.append({"oid": f"{t['symbol']}|{ts:.3f}", "symbol": str(t["symbol"]).upper(), "ts": ts,
                        "fill": float(t.get("entry_px") or 0), "day": datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d"),
                        "synthetic": bool(t.get("synthetic"))})
    n["orders"] = len(out)
    n["synthetic"] = sum(f["synthetic"] for f in out)
    return sorted(out, key=lambda f: f["ts"]), n, failed


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


def word(rows: list[dict], unknown_share: float, fetch_error_share: float = 0.0, *, a_end: str = A_END,
         min_days: int = MIN_DAYS, min_half: int = MIN_HALF_DAYS, min_trades: int = MIN_TRADES,
         extra_key: str | None = None, replay_failed: bool = False, drop_names: int = 0) -> tuple[str, dict]:
    """The prereg's call on the primary rows (each has day, net, slip). extra_key: a further line whose pooled
    day mean must be > 0 for a PASS (floor2: the tier-cost line)."""
    if replay_failed:
        return "NOT READ (a replay day failed or is missing)", {}
    if unknown_share > UNKNOWN_MAX:
        return "NOT READ (asset flags unknown)", {}
    if fetch_error_share > FETCH_ERROR_MAX:
        return "NOT READ (fetch errors)", {}
    days = by_day(rows, "net")
    if (len(days) < min_days or len(rows) < min_trades or sum(d <= a_end for d in days) < min_half
            or sum(d > a_end for d in days) < min_half):
        return "UNDERPOWERED", {}
    a = day_stat([v for d, v in days.items() if d <= a_end])
    b = day_stat([v for d, v in days.items() if d > a_end])
    pool = day_stat(list(days.values()))
    slip = day_stat(list(by_day(rows, "slip").values()))
    best3 = sorted(days, key=days.get, reverse=True)[:3]
    drop = day_stat([v for d, v in days.items() if d not in best3])
    extra = day_stat(list(by_day(rows, extra_key).values())) if extra_key else None
    names = None
    if drop_names:
        tot = defaultdict(float)
        for r in rows:
            tot[r.get("symbol")] += r["net"]
        top = set(sorted(tot, key=tot.get, reverse=True)[:drop_names])
        names = day_stat(list(by_day([r for r in rows if r.get("symbol") not in top], "net").values()))
        names["top"] = sorted(top, key=lambda k: -tot[k])
    info = {"A": a, "B": b, "pool": pool, "slip": slip, "drop3": drop, "best3": best3, "extra": extra,
            "drop_names": names}

    def pos(x):
        return x is not None and x > 0
    both = pos(a["mean"]) and pos(b["mean"])
    if (both and (pool["t"] or 0) >= T_PASS and pos(slip["mean"]) and pos(drop["mean"])
            and (extra is None or pos(extra["mean"])) and (names is None or pos(names["mean"]))):
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
                    res["gross_bp"] = round(short_gross(e, res["exit"], res), 1)
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


def asset_flags(syms: set[str]) -> dict[str, list | None]:
    """[shortable, easy_to_borrow] or None (unknown). Today's flags: one get_all_assets on the data-only account's
    keys; missing names are retried on every run. The 10/02 shortable.json wrote errors as [False, False], so only
    its True flags are taken from it."""
    p = OUT / "assets.json"
    have = {k: v for k, v in (json.load(open(p)) if p.exists() else {}).items() if v is not None}
    need = syms - set(have)
    if need:
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.enums import AssetClass
            from alpaca.trading.requests import GetAssetsRequest
            k = data_keys()
            tc = TradingClient(k["api_key"], k["secret_key"], paper=True)
            for a in tc.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY)):
                if a.symbol in need:
                    have[a.symbol] = [bool(a.shortable), bool(a.easy_to_borrow)]
        except Exception as e:  # noqa: BLE001 - unknown flags are counted and gate the word
            print(f"  asset list unavailable: {type(e).__name__} {str(e)[:100]}", file=sys.stderr)
    old = ROOT / "ai_reports" / "short_squares" / "shortable.json"
    if old.exists():
        for k, v in json.load(open(old)).items():
            if k in syms and k not in have and v and v[0]:
                have[k] = list(v)
    json.dump(have, open(p, "w"))
    return {k: have.get(k) for k in syms}


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
        try:
            got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start,
                                                     end=end, feed=DataFeed.SIP, adjustment=Adjustment.RAW)).data or {}
        except Exception as e:  # noqa: BLE001 - these names read ssr_unknown and stay out of primary
            print(f"  daily bars failed for {len(chunk)} names: {str(e)[:100]}", file=sys.stderr)
            continue
        for s in chunk:
            have[s] = [(b.timestamp.astimezone(ET).strftime("%Y-%m-%d"), float(b.low), float(b.close))
                       for b in got.get(s, [])]
        time.sleep(0.5)
    json.dump(have, open(p, "w"))
    return have


def ssr_for(daily: list | None, day: str, low_before: float | None) -> bool | None:
    """True/False, or None when there is no prior daily bar to read (ssr unknown: kept out of primary)."""
    prior = [r for r in (daily or []) if r[0] < day]
    if not prior:
        return None
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


def block(title: str, rows: list[dict], tier_cost: float | None = None, a_end: str = A_END) -> list[str]:
    f = lambda x: "—" if x is None else f"{x:+.1f}"  # noqa: E731
    g = lambda x: "—" if x is None else f"{x:+.2f}"  # noqa: E731
    lines = [f"### {title}", "",
             "| half | trades | days | short net/trade bp (t) | median | wins | -7 bp stops | scaled BE | fixed 30 m | long control | MDE at t 2 |"
             + (" tier cost |" if tier_cost else ""),
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|" + ("---:|" if tier_cost else "")]
    for tag, sl in (("all", rows), ("A", [r for r in rows if r["day"] <= a_end]), ("B", [r for r in rows if r["day"] > a_end])):
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
    global OUT, RESULT
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay-dir", default=None, help="floor2_short: score the replay's opens instead of the desk's fills")
    args = ap.parse_args()
    import config
    import call_exits as CE
    import replay_costing as RC
    replay = args.replay_dir is not None
    if replay:
        OUT, RESULT = ROOT / "ai_reports" / "floor2_short", ROOT / "ai_reports" / "floor2_short_result.md"
    OUT.mkdir(parents=True, exist_ok=True)
    CE.CACHE_DIR = OUT / "trades"
    cfg = config.load_config_ro()
    if replay:
        # The replay ran pinned to the commit's whole config (floor2_sweep.sh); exit with that, not today's file.
        bc = Path(args.replay_dir) / "base_config.json"
        if not bc.exists():
            sys.exit(f"REFUSING: {bc} missing (the sweep writes it)")
        cfg = {**cfg, **json.loads(bc.read_text())}
    if bool(cfg.get("ai_exit_supertrend", False)):
        sys.exit("REFUSING: ai_exit_supertrend is on; its bars are not mirrored (prereg)")
    replay_failed = False
    if replay:
        fills, n, replay_failed = load_replay(Path(args.replay_dir))
    else:
        fills, n = load_fills(sessions(), [ROOT / "ai_reports" / "fills", SNAP_BASE])
    print(f"fills: {dict(n)}; sessions with a fill {len({f['day'] for f in fills})}", file=sys.stderr, flush=True)
    if not fills:
        sys.exit("no opens found" if replay else "no desk buy fills found in ai_reports/fills or ~/session_snapshots")
    cl = CE._client()
    syms = {f["symbol"] for f in fills}
    flags = asset_flags(syms)
    daily = daily_bars(cl, syms)
    qcache, box = RC.load_cache(), {}
    bar_cache: dict[tuple, list] = {}
    rows, counts, reasons, err_days = [], Counter(), Counter(), Counter()
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
            err_days[day] += 1
            print(f"  {key}: {type(e).__name__} {str(e)[:120]}", file=sys.stderr)
            continue
        e_px, e_ts = res["entry"], res["entry_ts"]
        # Today's low before the short: completed RTH minute lows, the prints from the fill to the entry, the entry.
        before = ([b[2] for b in bars if b[0] + 60 <= e_ts and et_min(b[0]) >= OPEN_MIN]
                  + [p for t, p, s in trades if t <= e_ts and s >= CE.ROUND_LOT] + [e_px])
        hold = [p for t, p, s in trades if e_ts <= t <= res["exit_ts"] and s >= CE.ROUND_LOT]
        fx = fixed_hold(CE, trades, e_ts, e_px, day)
        rows.append({"day": day, "symbol": sym, "ts": ts, "synthetic": bool(fl.get("synthetic")), "entry": e_px, "tier": tier(e_px), "sp_q": sp_q,
                     "flags": flags.get(sym), "ssr": ssr_for(daily.get(sym), day, min(before)),
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
        r["tiercost"] = r["gross"] - TIER_COST_BP.get(r["tier"], sp) - FEE_BP
        reasons[r["reason"]] += 1
    json.dump(rows, open(OUT / "rows.json", "w"))

    ten = [r for r in rows if r["tier"] == "10+"]
    small = [r for r in rows if r["tier"] in ("2-5", "5-10")]
    base = small if replay else ten
    unknown = sum(1 for r in base if r["flags"] is None) / len(base) if base else 1.0
    etb = lambda r: r["flags"] is not None and r["flags"][0] and r["flags"][1]  # noqa: E731
    primary = [r for r in base if etb(r) and r["ssr"] is False]
    attempted = len(rows) + counts["fetch_error"]
    err_share = counts["fetch_error"] / attempted if attempted else 0.0
    a_end = REPLAY_A_END if replay else A_END
    if replay:
        w, info = word(primary, unknown, err_share, a_end=a_end, extra_key="tiercost", replay_failed=replay_failed,
                       **REPLAY_MIN)
    else:
        w, info = word(primary, unknown, err_share)
    fl = Counter("unknown" if r["flags"] is None else "easy" if etb(r) else "hard" if r["flags"][0] else "not shortable"
                 for r in rows)
    title = ("# Floor at $2: short the replayed desk's sub-$10 buys with the live exits mirrored (floor2_short_prereg)"
             if replay else "# Short the desk's fills with the live exits mirrored (fill_short_prereg)")
    span = f"replay {args.replay_dir}, days {REPLAY_DAYS[0]}..{REPLAY_DAYS[-1]}" if replay else f"Sessions {FIRST}..{LAST}"
    lines = [title, "",
             f"{span}; half A ..{a_end}, half B after. Opens {dict(n)}. Scored {len(rows)}; "
             f"{dict(counts)}.", "",
             f"Price tiers {dict(Counter(r['tier'] for r in rows))}. Asset flags (today's) {dict(fl)}; "
             f"unknown among {'$2-10' if replay else '$10+'}: {unknown:.0%}. SSR at entry: {sum(r['ssr'] is True for r in rows)}, "
             f"SSR unknown (no daily bar): {sum(r['ssr'] is None for r in rows)}. Fetch errors {counts['fetch_error']} "
             f"of {attempted} ({err_share:.1%}) by day {dict(sorted(err_days.items()))}. Fixed-30 m line missing "
             f"{sum(r['fixed30'] is None for r in rows)}.", "",
             "All prices are SIP (the consolidated tape); the live desk trades on IEX quotes.", "",
             "Net = short return - full quoted spread at entry - 0.5 bp fees. Long control = the same fill long, same exits unmirrored, minus the spread.", ""]
    if replay:
        lines += block("PRIMARY: $2-10, shortable and easy to borrow, no SSR (tier-cost column must be > 0 too)", primary,
                       None, a_end=a_end)
        pt = [r["tiercost"] for r in primary]
        lines += [f"Primary tier-cost line pooled day mean: "
                  f"{day_stat(list(by_day(primary, 'tiercost').values()))['mean'] or 0:+.1f} bp ({len(pt)} trades).", ""]
        lines += block("$10+ opens of the same replay (information)", ten, a_end=a_end)
        syn = [r for r in rows if r["synthetic"]]
        if syn:
            lines += block("Synthetic-row opens, any price (information)", syn, a_end=a_end)
        ps = sum(r["synthetic"] for r in primary)
        lines += [f"Primary opens on synthetic engine rows (last bar close, up to 60 s+ old): {ps} of {len(primary)}.", ""]
        lines += [f"Sub-$10 names seated: {dict(Counter(r['symbol'] for r in small).most_common(30))}", ""]
    else:
        lines += block("PRIMARY: $10+, shortable and easy to borrow, no SSR", primary, a_end=a_end)
        lines += block("$10+, any flags (information)", ten, a_end=a_end)
    for t in ("5-10", "2-5"):
        tr = [r for r in rows if r["tier"] == t]
        if tr:
            lines += block(f"${t} (information; last column charges the {TIER_COST_BP[t] / 100:.2f}% tier round trip)", tr,
                           TIER_COST_BP[t], a_end=a_end)
    lines += [f"Exit reasons: {dict(reasons.most_common(8))}", ""]
    if info:
        f = lambda x: "—" if x is None else f"{x:+.1f}"  # noqa: E731
        lines += [f"Pooled day-mean {f(info['pool']['mean'])} bp (t {info['pool']['t'] or 0:+.2f}); A {f(info['A']['mean'])}, "
                  f"B {f(info['B']['mean'])}; -7 bp stops {f(info['slip']['mean'])}; without the 3 best days "
                  f"{f(info['drop3']['mean'])} (best days {', '.join(info['best3'])})."
                  + (f" Without the top 5 names ({', '.join(info['drop_names']['top'])}): {f(info['drop_names']['mean'])}."
                     if info.get("drop_names") else ""), ""]
    lines += [f"**{w}**"]
    md = "\n".join(lines)
    print(md, flush=True)
    RESULT.write_text(md + "\n")


if __name__ == "__main__":
    main()
