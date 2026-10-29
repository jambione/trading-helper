#!/usr/bin/env python3
"""move_to_cost_live.py — docs/studies/move_to_cost_live_prereg.json (amended, amended_2, ready).

Live paper trades (ai_reports/outcomes.jsonl), P1 = 2026-08-07..09-25, P2 = 10-14..10-29 (9/28-10/13 excluded).
Rows: entry_path 'watch', paper true, not adopted / scaled_out / exhaustion_scalp, exit_price present, exact duplicates
collapsed, overlapping same-symbol pairs dropped. Each row is matched to Alpaca FILL activities (buys in
[entry - 5 s, entry + 120 s], sells in [last buy, exit + 5 s], quantity-weighted, qty must equal total_qty); net bp
from the matched averages. Range = IEX 1-minute bars [m-21, m-1] before the entry minute m (entries before 09:51
missing, >= 10 bars); spread = last SIP quote at or before entry. move_to_cost < 8 = LOW. PRIMARY = matched buy
average >= $10.
  count   counts and MDEs only (both sets: all sessions, without 10/27-10/29), VOID check
  score   refuses unless count passed the power gate on both sets
USAGE (mini, after hours): .venv/bin/python tools/studies/move_to_cost_live.py count|score
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import move_to_cost_holdout as M  # noqa: E402
import std_scorer as S  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = Path(os.path.expanduser("~/replay_runs/move_to_cost_live"))
P1, P2 = ("2026-08-07", "2026-09-25"), ("2026-10-14", "2026-10-29")
DISCUSSED = {"2026-10-27", "2026-10-28", "2026-10-29"}
FLOOR, MIN_LOW, MIN_SESS, MDE_MAX = 10.0, 60, 15, 8.0


def day_of(t: float) -> str:
    return _dt.datetime.fromtimestamp(float(t), ET).strftime("%Y-%m-%d")


def period(day: str) -> str | None:
    if P1[0] <= day <= P1[1]:
        return "P1"
    if P2[0] <= day <= P2[1]:
        return "P2"
    return None


# ── rows (amended 1, 2, 6; amended_2 R3) ────────────────────────────────────────────────────────────────────────────
def clean_rows(raw: list[dict]) -> tuple[list[dict], dict]:
    drops: dict[str, int] = {}

    def drop(why):
        drops[why] = drops.get(why, 0) + 1
    keep, seen = [], set()
    for r in raw:
        try:
            et_, xt = float(r.get("entry_time")), float(r.get("exit_time"))
            ep, xp, q = float(r.get("entry_price") or 0), float(r.get("exit_price") or 0), float(r.get("total_qty") or 0)
        except (TypeError, ValueError):
            drop("unparseable")
            continue
        d = day_of(et_)
        if period(d) is None:
            drop("outside_periods")
            continue
        sym = str(r.get("symbol") or "")
        if not sym.isalpha() or any("MagicMock" in str(v) for v in r.values()):
            drop("bad_symbol_or_test")
            continue
        if r.get("entry_path") != "watch" or r.get("paper") is not True:
            drop("not_watch_paper")
            continue
        if r.get("scaled_out"):
            drop("scaled_out")
            continue
        if r.get("edge_mode") == "exhaustion_scalp":
            drop("exhaustion_scalp")
            continue
        if ep <= 0 or xp <= 0 or q <= 0:
            drop("no_exit_price_or_qty")
            continue
        key = (sym, round(et_, 3), round(xt, 3))
        if key in seen:
            drop("duplicate")
            continue
        seen.add(key)
        keep.append({"symbol": sym, "entry_time": et_, "exit_time": xt, "qty": q, "day": d, "period": period(d)})
    by: dict[str, list] = {}
    for r in keep:
        by.setdefault(r["symbol"], []).append(r)
    bad = set()
    for rs in by.values():
        rs.sort(key=lambda r: r["entry_time"])
        for a, b in zip(rs, rs[1:]):
            if b["entry_time"] - 5 <= a["exit_time"] + 5:
                bad |= {id(a), id(b)}
    out = [r for r in keep if id(r) not in bad]
    if bad:
        drops["overlap_pair"] = len(bad)
    return out, drops


# ── fills (amended_2 R2) ────────────────────────────────────────────────────────────────────────────────────────────
def match_fills(row: dict, fills: list[dict]) -> tuple[float, float] | None:
    """(buy avg, sell avg) or None. fills: [{'symbol','side','qty','price','t'}] for that day."""
    s = [f for f in fills if f["symbol"] == row["symbol"]]
    buys = [f for f in s if f["side"] == "buy" and row["entry_time"] - 5 <= f["t"] <= row["entry_time"] + 120]
    if not buys:
        return None
    last_buy = max(f["t"] for f in buys)
    sells = [f for f in s if f["side"] == "sell" and last_buy <= f["t"] <= row["exit_time"] + 5]
    bq, sq = sum(f["qty"] for f in buys), sum(f["qty"] for f in sells)
    if not sells or abs(bq - row["qty"]) > 1e-6 or abs(sq - row["qty"]) > 1e-6:
        return None
    return (sum(f["qty"] * f["price"] for f in buys) / bq, sum(f["qty"] * f["price"] for f in sells) / sq)


def fetch_fills(day: str) -> list[dict]:
    """Alpaca account FILL activities for the day (paper account, desk keys; mini only)."""
    from alpaca.trading.client import TradingClient
    from config import load_config
    cfg = load_config() or {}
    tc = TradingClient(cfg.get("api_key"), cfg.get("secret_key"), paper=True)
    out, token = [], None
    while True:
        params = {"date": day, "page_size": 100, "direction": "asc"}
        if token:
            params["page_token"] = token
        rows = tc.get("/account/activities/FILL", params) or []
        for a in rows:
            ts = _dt.datetime.fromisoformat(str(a["transaction_time"]).replace("Z", "+00:00")).timestamp()
            out.append({"symbol": str(a["symbol"]), "side": str(a["side"]).lower().replace("sell_short", "sell"),
                        "qty": float(a["qty"]), "price": float(a["price"]), "t": ts})
        if len(rows) < 100:
            return out
        token = rows[-1]["id"]


# ── measures ────────────────────────────────────────────────────────────────────────────────────────────────────────
def range_pct(bars: list, entry_t: float) -> float | None:
    d = _dt.datetime.fromtimestamp(entry_t, ET)
    m = d.hour * 60 + d.minute
    if m < 9 * 60 + 51:
        return None
    w = [b for b in bars if m - 21 <= int(b[0]) <= m - 1]
    if len(w) < 10:
        return None
    hi, lo = max(float(b[2]) for b in w), min(float(b[3]) for b in w)
    return (hi / lo - 1) * 100 if lo > 0 else None


def build(rows: list[dict], data, fills_by_day: dict) -> list[dict]:
    out = []
    for r in rows:
        x = dict(r)
        m = match_fills(r, fills_by_day.get(r["day"], []))
        if m is None:
            x["status"] = "unmatched"
            out.append(x)
            continue
        buy, sell = m
        x.update({"buy": buy, "net": (sell / buy - 1) * 1e4, "primary": buy >= FLOOR})
        try:
            bars = data.bars(r["symbol"], r["day"])
            sp = data.spread_bp(r["symbol"], r["entry_time"])
        except M.FetchError:
            x["status"] = M.ERROR
            out.append(x)
            continue
        rng = range_pct(bars, r["entry_time"])
        x.update({"status": "ok", "spread": sp, "rng": rng})
        x["m2c"] = None if rng is None or not sp else rng * 100.0 / sp
        x["group"] = M.label(x["m2c"])
        out.append(x)
    return out


def counts(rows: list[dict]) -> dict:
    sc = M.scored(rows)
    low = [r for r in sc if r["group"] == "LOW"]
    return {"rows": len(rows), "scored": len(sc), "low_n": len(low), "low_sessions": len({r["day"] for r in low}),
            "both_sessions": len(M.session_d(sc)), "missing": sum(r.get("group") == M.MISSING for r in rows),
            "mde": M.placebo_mde(sc, seed="MTCL-MDE")}


def powered(c: dict) -> bool:
    return c["low_n"] >= MIN_LOW and c["both_sessions"] >= MIN_SESS and c["mde"] is not None and c["mde"] <= MDE_MAX


def verdict(rows: list[dict]) -> dict:
    sc = M.scored(rows)
    ds = M.session_d(sc)
    st = S.day_stat(ds)
    n = len(ds)
    tcrit = M.t_crit(n - 1, 0.975) if n >= 2 else float("inf")
    low = [r["net"] for r in sc if r["group"] == "LOW"]
    per = {p: S.day_stat(M.session_d([r for r in sc if r["period"] == p]))["mean"] for p in ("P1", "P2")}
    rem_low = M.book_mean_after(sc, {id(r) for r in sc if r["group"] == "LOW"})

    def beats(xs):
        return bool(xs) and sum(rem_low > x for x in xs) / len(xs) >= 0.95
    checks = {"d": st["mean"] <= -5.0 and st["t"] <= -tcrit, "low_negative": bool(low) and statistics.fmean(low) < 0,
              "both_periods": all(v is not None and v == v and v < 0 for v in per.values()),
              "beats_random": beats(M.random_removal(sc, seed="MTCL")),
              "beats_spread_only": beats(M.ranked_removal(sc, "spread", True, seed="MTCL-TIE")),
              "beats_range_only": beats(M.ranked_removal(sc, "rng", False, seed="MTCL-TIE"))}
    ub = st["mean"] + tcrit * st["se"] if n >= 2 else float("nan")
    return {"sessions": n, "d": st, "t_crit": tcrit, "period_d": per, "low_mean": statistics.fmean(low) if low else None,
            "upper97_5": ub, "checks": checks, "holds": all(checks.values()), "refuted": ub == ub and ub > 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("count", "score"))
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    raw = [json.loads(line) for line in open(ROOT / "ai_reports" / "outcomes.jsonl") if line.strip()]
    rows, drops = clean_rows(raw)
    cache = OUT / "fills.json"
    fills_by_day = json.loads(cache.read_text()) if cache.exists() else {}
    for d in sorted({r["day"] for r in rows}):
        if d not in fills_by_day:
            fills_by_day[d] = fetch_fills(d)
    cache.write_text(json.dumps(fills_by_day))
    data = M.Data.live()
    data.dir = OUT / "cache"
    built = build(rows, data, fills_by_day)
    unmatched = sum(r.get("status") == "unmatched" for r in built)
    errors = sum(r.get("status") == M.ERROR for r in built)
    void = bool(built) and (unmatched / len(built) > 0.05 or errors / len(built) > 0.05)
    prim = [r for r in built if r.get("primary")]
    sets = {"all": prim, "without_10_27_29": [r for r in prim if r["day"] not in DISCUSSED]}
    if a.mode == "count":
        res = {"drops": drops, "clean_rows": len(rows), "unmatched": unmatched, "errors": errors, "void": void,
               "primary_rows": len(prim), **{k: counts(v) for k, v in sets.items()}}
        res["powered"] = (not void) and all(powered(res[k]) for k in sets)
        (OUT / "count.json").write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps(res, indent=1, default=str))
        return 0
    cnt = json.loads((OUT / "count.json").read_text()) if (OUT / "count.json").exists() else {}
    if not cnt.get("powered"):
        print("UNDERPOWERED or VOID (count.json): no score", file=sys.stderr)
        return 2
    res = {k: verdict(v) for k, v in sets.items()}
    res["verdict"] = ("HOLDS" if all(res[k]["holds"] for k in sets) else
                      "NOT SUPPORTED (refuted)" if res["all"]["refuted"] else "NOT SUPPORTED")
    res["note"] = "association only; Alpaca paper fills; a HOLDS earns the replay gate test, never a switch-on"
    (OUT / "result.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
