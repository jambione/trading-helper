#!/usr/bin/env python3
"""$20-floor forward read. Spec: docs/studies/floor20_forward_prereg.json (as amended).

  --log      after each close: append {date, sha256 of config/bot_config.json, git HEAD, keys differing from 874a7791}
             to ai_reports/floor20_forward/freeze_log.jsonl (no outcome)
  --counts   forward sessions and fills so far (no realized value is printed)
  --read     the single registered read; refuses before >= 20 forward sessions and >= 150 forward fills unless the
             35-session cap is reached; prints the freeze evidence first

Fills: the desk's paper BUY orders FIFO-matched to sells (as indicator_levels_study.fills, plus the exit time of the sell
that closes each lot, for the SPY guard). Run on the mini (desk keys, read-only order list; SIP bars for SPY).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "ai_reports", "floor20_forward")
CONFIG = os.path.join(ROOT, "config", "bot_config.json")
ET = ZoneInfo("America/New_York")
FLOOR_COMMIT = "874a7791"
FWD_FIRST, SEEN_DAY = "2026-10-23", "2026-10-22"
CMP_FIRST, CMP_LAST, BASE_FIRST = "2026-10-16", "2026-10-21", "2026-10-05"
MIN_SESS, MIN_FILLS, CAP_SESS = 20, 150, 35
ECON_BP = 3.0
FREEZE_PATTERNS = ("ai_watch_", "ai_entry_", "arm_", "trail", "stop", "leash", "exit", "spread", "trade_amount", "qty",
                   "size", "risk_", "min_price")


# ── pure statistics ──────────────────────────────────────────────────────────

def t_ppf(p: float, df: int) -> float:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import indicator_confirm_study as S
    return S.t_ppf(p, df)


def cr1(rows: list[dict], key: str = "bp") -> dict:
    """Mean of per-fill values with a session-clustered CR1 SE; day-equal mean too."""
    by = collections.defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            by[r["day"]].append(r[key])
    xs = [x for v in by.values() for x in v]
    n, G = len(xs), len(by)
    if n == 0:
        return {"n": 0, "sessions": 0, "mean": None, "se": None, "t": None, "day_equal": None}
    mu = sum(xs) / n
    meat = sum(sum(x - mu for x in v) ** 2 for v in by.values())
    se = math.sqrt(G / (G - 1) * meat) / n if G > 1 else None
    de = sum(sum(v) / len(v) for v in by.values()) / G
    return {"n": n, "sessions": G, "mean": mu, "se": se, "t": mu / se if se else None, "day_equal": de}


def diff(a: dict, b: dict) -> dict:
    if a["se"] is None or b["se"] is None:
        return {"diff": None, "t": None}
    se = math.sqrt(a["se"] ** 2 + b["se"] ** 2)
    return {"diff": a["mean"] - b["mean"], "t": (a["mean"] - b["mean"]) / se if se else None, "se": se}


def drop_top(rows: list[dict], by: str, k: int = 3) -> float | None:
    tot = collections.Counter()
    for r in rows:
        tot[r[by]] += r["bp"]
    top = {x for x, _ in tot.most_common(k)}
    rest = [r["bp"] for r in rows if r[by] not in top]
    return sum(rest) / len(rest) if rest else None


def verdict(fwd: dict, d: dict, halves: list[dict], guards: dict) -> str:
    G = fwd["sessions"]
    if fwd["n"] < 40 or G < 8:
        return "UNDERPOWERED"
    tc = t_ppf(0.975, G - 1)
    better = d["t"] is not None and d["t"] >= tc
    if d["t"] is not None and d["t"] <= -tc:
        return "WORSE"
    profitable = (better and fwd["t"] is not None and fwd["t"] >= tc and fwd["mean"] >= ECON_BP
                  and fwd["day_equal"] > 0 and all(h["mean"] is not None and h["mean"] > 0 for h in halves)
                  and all(v is not None and v > 0 for v in guards.values()))
    if profitable:
        return "PROFITABLE (earns a sizing-step prereg only)"
    return "BETTER_NOT_PROFITABLE" if better else "NO_CHANGE"


def freeze_keys(cur: dict, ref: dict) -> list[str]:
    keys = set(cur) | set(ref)
    changed = [k for k in sorted(keys) if cur.get(k) != ref.get(k)]
    return [k for k in changed if any(p in k.lower() for p in FREEZE_PATTERNS)]


# ── data (mini) ──────────────────────────────────────────────────────────────

def fills() -> list[dict]:
    """Paper BUY fills FIFO-matched to sells: realized bp and the exit time of the sell that closes the lot."""
    sys.path.insert(0, ROOT)
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    from config import load_config
    c = load_config() or {}
    tc = TradingClient(c.get("api_key"), c.get("secret_key"), paper=True)
    since = datetime.strptime(BASE_FIRST, "%Y-%m-%d").replace(tzinfo=ET) - timedelta(days=1)
    orders, until = [], datetime.now(timezone.utc)
    while True:
        got = tc.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=500, after=since, until=until,
                                             direction="desc"))
        orders += [o for o in got if o.filled_at and o.filled_avg_price and float(o.filled_qty or 0) > 0]
        if len(got) < 500:
            break
        until = min(o.submitted_at for o in got)
    orders.sort(key=lambda o: o.filled_at)
    lots, rows = collections.defaultdict(list), []
    for o in orders:
        side = str(getattr(o.side, "value", o.side)).lower()
        q, px, t = float(o.filled_qty), float(o.filled_avg_price), o.filled_at.timestamp()
        if side == "buy":
            r = {"sym": o.symbol, "t": t, "fill": px, "day": datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d"),
                 "qty": q, "sold": 0.0, "proceeds": 0.0, "exit_t": None}
            rows.append(r)
            lots[o.symbol].append([r, q])
        else:
            left = q
            while left > 1e-9 and lots[o.symbol]:
                lot = lots[o.symbol][0]
                take = min(left, lot[1])
                lot[0]["sold"] += take
                lot[0]["proceeds"] += take * px
                lot[0]["exit_t"] = t
                lot[1] -= take
                left -= take
                if lot[1] <= 1e-9:
                    lots[o.symbol].pop(0)
    for r in rows:
        r["bp"] = (r["proceeds"] / r["sold"] / r["fill"] - 1) * 1e4 if r["sold"] >= 0.99 * r["qty"] else None
    return rows


def spy_bp(rows: list[dict]) -> None:
    """SPY's return over each fill's entry-to-exit window: close of the entry minute to close of the exit minute."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import bars as B
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    cl, cache = B.client(), {}
    for r in rows:
        if r.get("bp") is None or r.get("exit_t") is None:
            continue
        if r["day"] not in cache:
            d = datetime.strptime(r["day"], "%Y-%m-%d").replace(tzinfo=ET)
            df = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                                    start=d.replace(hour=9, minute=30).astimezone(timezone.utc),
                                                    end=d.replace(hour=16).astimezone(timezone.utc), feed=DataFeed.SIP)).df
            cache[r["day"]] = {int(t.timestamp()) // 60: float(x.close) for t, x in df.xs("SPY", level="symbol").iterrows()}
        b = cache[r["day"]]
        a, z = b.get(int(r["t"]) // 60), b.get(int(r["exit_t"]) // 60)
        r["spy_bp"] = (z / a - 1) * 1e4 if a and z else None
        r["bp_minus_spy"] = r["bp"] - r["spy_bp"] if r.get("spy_bp") is not None else None


def git_head() -> str:
    return subprocess.run(["git", "-C", ROOT, "rev-parse", "--short=8", "HEAD"], capture_output=True, text=True).stdout.strip()


def ref_config() -> dict:
    out = subprocess.run(["git", "-C", ROOT, "show", f"{FLOOR_COMMIT}:config/bot_config.json"], capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 else {}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    for f in ("--log", "--counts", "--read"):
        g.add_argument(f, action="store_true")
    a = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    if a.log:
        raw = open(CONFIG, "rb").read()
        rec = {"date": datetime.now(ET).strftime("%Y-%m-%d %H:%M"), "config_sha256": hashlib.sha256(raw).hexdigest(),
               "git_head": git_head(), "frozen_keys_changed": freeze_keys(json.loads(raw), ref_config())}
        open(os.path.join(OUT_DIR, "freeze_log.jsonl"), "a").write(json.dumps(rec) + "\n")
        print(rec)
        return
    rows = fills()
    fwd = [r for r in rows if r["day"] >= FWD_FIRST]
    sess = sorted({r["day"] for r in fwd})
    if a.counts:
        print({"forward_sessions": len(sess), "forward_fills": len(fwd),
               "unsold": sum(1 for r in fwd if r["bp"] is None), "read_due": len(sess) >= MIN_SESS and len(fwd) >= MIN_FILLS
               or len(sess) >= CAP_SESS})
        return
    if not ((len(sess) >= MIN_SESS and len(fwd) >= MIN_FILLS) or len(sess) >= CAP_SESS):
        raise SystemExit(f"read not due: {len(sess)} sessions, {len(fwd)} fills")
    log = [json.loads(x) for x in open(os.path.join(OUT_DIR, "freeze_log.jsonl"))] \
        if os.path.exists(os.path.join(OUT_DIR, "freeze_log.jsonl")) else []
    hist = subprocess.run(["git", "-C", ROOT, "log", "--oneline", f"{FLOOR_COMMIT}..HEAD"], capture_output=True,
                          text=True).stdout
    print("FREEZE EVIDENCE:", {"log_entries": len(log), "keys_changed_ever": sorted({k for x in log for k in
                                                                                   x["frozen_keys_changed"]}),
                               "heads": sorted({x["git_head"] for x in log})}, "\ncommits since floor:\n", hist)
    fv = [r for r in fwd if r["bp"] is not None]
    spy_bp(fv)
    cmp_ = [r for r in rows if CMP_FIRST <= r["day"] <= CMP_LAST and r["fill"] >= 20 and r["bp"] is not None]
    base = [r for r in rows if BASE_FIRST <= r["day"] <= CMP_LAST and r["bp"] is not None]
    F, C = cr1(fv), cr1(cmp_)
    half = sess[: len(sess) // 2]
    halves = [cr1([r for r in fv if r["day"] in set(half)]), cr1([r for r in fv if r["day"] not in set(half)])]
    guards = {"minus_spy": cr1(fv, "bp_minus_spy")["mean"], "drop_top3_sessions": drop_top(fv, "day"),
              "drop_top3_names": drop_top(fv, "sym")}
    d = diff(F, C)
    res = {"forward": F, "comparator_ge20_1016_1021": C, "difference": d, "halves": halves, "guards": guards,
           "mde_80pct_bp": (t_ppf(0.975, max(1, F["sessions"] - 1)) + 0.84) * d["se"] if d.get("se") else None,
           "verdict": verdict(F, d, halves, guards),
           "info": {"full_october_baseline": cr1(base), "seen_day_10_22": cr1([r for r in rows if r["day"] == SEEN_DAY
                                                                                and r["bp"] is not None]),
                    "opens_per_session_forward": len(fwd) / max(1, len(sess)),
                    "opens_per_session_comparator": len(cmp_) / max(1, len({r['day'] for r in cmp_})),
                    "largest_name_share": max(collections.Counter(r["sym"] for r in fv).values()) / max(1, len(fv)),
                    "win_rate": sum(1 for r in fv if r["bp"] > 0) / max(1, len(fv)),
                    "unsold": sum(1 for r in fwd if r["bp"] is None)}}
    json.dump(res, open(os.path.join(OUT_DIR, "read.json"), "w"), indent=1, default=str)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
