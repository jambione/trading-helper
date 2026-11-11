#!/usr/bin/env python3
"""pick_ledger.py - do the operator's own stock picks beat a fair control? (docs/RESTRUCTURE_PLAN_2026-11-11.md WP4,
docs/studies/pick_ledger_prereg.json). Nothing here places an order.

    python3 tools/pick_ledger.py add ABCD --thesis "why I think it runs" --months 6 --sell-if "what would make me sell"
    python3 tools/pick_ledger.py list
    .venv/bin/python tools/pick_ledger.py score          # needs Alpaca data: run on the mini after `git pull`

Record picks on the MacBook (it can push). `add` commits the ledger line itself; push it the same day.

Rules (fixed in the prereg, enforced here):
- A pick is stamped with the time it was recorded; its entry is the OPEN of the first session that starts after that time.
  No past dates. `--retro` records a pick made after the fact: listed, never scored, never counted.
- Proof of order: every line carries the id of the line before it (a hash chain), and `score` only scores a pick whose
  line was COMMITTED to git before its entry open. A broken chain, an edited line or a late commit is refused.
- One pick per symbol per 92 calendar days (63 sessions): one idea, one pick. Common stocks only (funds are never scored).
- Scored at 21, 63 and 126 sessions, open to open, adjusted prices, whether or not the operator still holds it:
  ratio lift (1+pick)/(1+control)-1 against SPY; against the mean of 30 control names drawn at `add` time from a frozen
  list (docs/studies/pick_ledger_universe.txt) and stored in the ledger line; the pick's rank among those 30; and the
  beta-adjusted excess, pick - beta x SPY, beta from the 252 sessions before entry.
- ONE verdict, on the first 30 countable picks in record order, once all 30 have a closed 63-session window. If any of the
  30 cannot be scored, or lacks its control, the verdict is BLOCKED, never quietly replaced by pick 31.
  Picks recorded after the prereg's end date do not count.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("REPO") or Path(__file__).resolve().parents[1])
LEDGER = ROOT / "docs" / "pick_ledger.jsonl"
UNIVERSE = ROOT / "docs" / "studies" / "pick_ledger_universe.txt"
ET = ZoneInfo("America/New_York")
HORIZONS = (21, 63, 126)
PRIMARY, NEED, N_CONTROL, DEDUPE_DAYS, BETA_DAYS = 63, 30, 30, 92, 252
EARLY, EARLY_T = 21, 2.5                  # one-month early look on the same first 30 picks; a higher t because it is a second look
END_DATE = "2027-11-11"                   # picks recorded after this do not count; fewer than 30 by then = UNPROVEN


# ---------- ledger ----------
def read_ledger(path: Path | None = None) -> list[dict]:
    path = path or LEDGER
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def frozen_universe(path: Path | None = None) -> list[str]:
    path = path or UNIVERSE
    return [x.strip() for x in path.read_text().splitlines() if x.strip() and not x.startswith("#")] if path.exists() else []


def record_id(prev: str, sym: str, recorded_at: str, thesis: str) -> str:
    return hashlib.sha1(f"{prev}|{sym}|{recorded_at}|{thesis}".encode()).hexdigest()[:12]


def chain_problem(picks: list[dict]) -> str | None:
    """None when every line links to the one before it and its id matches its content."""
    prev = ""
    for i, p in enumerate(picks, 1):
        if p.get("prev", "") != prev:
            return f"line {i} ({p.get('symbol')}) does not follow the line before it: a line was removed, inserted or reordered"
        if p.get("id") != record_id(prev, p["symbol"], p["recorded_at"], p["thesis"]):
            return f"line {i} ({p.get('symbol')}) was edited after it was recorded"
        prev = p["id"]
    return None


def add_pick(symbol: str, thesis: str, months: float, sell_if: str, *, now: datetime | None = None, retro: bool = False,
             path: Path | None = None, universe: list[str] | None = None, extra: dict | None = None) -> dict:
    path = path or LEDGER
    sym = symbol.strip().upper()
    if not re.fullmatch(r"[A-Z]{1,5}(\.[A-Z])?", sym):
        raise ValueError(f"'{symbol}' is not a stock symbol")
    if sym in ("SPY", "QQQ"):
        raise ValueError("the index is the control, not a pick")
    if len(thesis.strip()) < 15:
        raise ValueError("write the thesis in a full sentence (at least 15 characters): it is what gets judged later")
    if len(sell_if.strip()) < 8:
        raise ValueError("say what would make you sell (at least 8 characters)")
    if not 0.5 <= months <= 36:
        raise ValueError("intended hold must be between 0.5 and 36 months")
    now = (now or datetime.now(ET)).astimezone(ET)
    picks = read_ledger(path)
    bad = chain_problem(picks)
    if bad:
        raise ValueError(f"the ledger is damaged ({bad}); restore it from git before adding")
    for p in picks:
        if p["symbol"] == sym and not p.get("retro") and not retro:
            gap = abs((now - datetime.fromisoformat(p["recorded_at"])).days)
            if gap < DEDUPE_DAYS:
                raise ValueError(f"{sym} was already recorded on {p['recorded_at'][:10]} (one pick per symbol per {DEDUPE_DAYS} days)")
    uni = frozen_universe() if universe is None else universe
    prev = picks[-1]["id"] if picks else ""
    stamp = now.isoformat(timespec="seconds")
    rid = record_id(prev, sym, stamp, thesis.strip())
    pool = sorted(s for s in uni if s != sym)
    rec = {"id": rid, "prev": prev, "symbol": sym, "recorded_at": stamp, "thesis": thesis.strip(), "hold_months": months,
           "sell_if": sell_if.strip(), "retro": bool(retro),
           "controls": random.Random(rid).sample(pool, N_CONTROL) if len(pool) >= N_CONTROL else [], **(extra or {})}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def _git_root(path: Path) -> Path | None:
    r = subprocess.run(["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None


def git_commit(path: Path, sym: str) -> bool:
    """Commit the ledger file in whichever git repo holds it (the project repo, or a ledger's own repo)."""
    root = _git_root(path)
    if root is None:
        return False
    rel = str(path.resolve().relative_to(root.resolve()))
    ident = ["-c", "user.name=pick-ledger", "-c", "user.email=pick-ledger@localhost"] if root.resolve() != ROOT.resolve() else []
    a = subprocess.run(["git", "-C", str(root), "add", rel], capture_output=True, text=True)
    c = subprocess.run(["git", "-C", str(root), *ident, "commit", "-q", "-m", f"pick: {sym}", "--", rel], capture_output=True, text=True)
    return a.returncode == 0 and c.returncode == 0


def first_commit_ts(pick_id: str, path: Path | None = None) -> float | None:
    """Unix time of the first commit that contains this pick's id; None when it was never committed."""
    path = path or LEDGER
    root = _git_root(path)
    if root is None:
        return None
    rel = str(path.resolve().relative_to(root.resolve()))
    r = subprocess.run(["git", "-C", str(root), "log", "--reverse", "--format=%ct", f"-S\"id\": \"{pick_id}\"", "--", rel], capture_output=True, text=True)
    out = r.stdout.split()
    return float(out[0]) if out else None


# ---------- scoring (pure) ----------
def open_ts(day: str) -> float:
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, 9, 30, tzinfo=ET).timestamp()


def entry_index(days: list[str], recorded_at: str) -> int | None:
    """Index of the first session whose 09:30 ET open is after the record time; None when it has not happened yet."""
    t = datetime.fromisoformat(recorded_at).timestamp()
    for i, d in enumerate(days):
        if open_ts(d) > t:
            return i
    return None


def span(bars: dict[str, tuple], days: list[str], a: int, b: int) -> float | None:
    """Simple return, open of days[a] to open of days[b]; a name with no bar at b is sold at its last close before b
    (this OVERSTATES a bankruptcy; such a pick is rare and is named in the output)."""
    e = bars.get(days[a])
    if not e:
        return None
    x = bars.get(days[b])
    if x:
        return x[0] / e[0] - 1.0
    for k in range(b - 1, a - 1, -1):
        if days[k] in bars:
            return bars[days[k]][1] / e[0] - 1.0
    return None


def beta_before(bars_s: dict, bars_m: dict, days: list[str], a: int, n: int = BETA_DAYS) -> float | None:
    """Slope of the name's daily close-to-close return on SPY's over the n sessions before entry; None with under 120 pairs."""
    xs, ys = [], []
    for k in range(max(1, a - n), a):
        d0, d1 = days[k - 1], days[k]
        if d0 in bars_s and d1 in bars_s and d0 in bars_m and d1 in bars_m:
            ys.append(bars_s[d1][1] / bars_s[d0][1] - 1)
            xs.append(bars_m[d1][1] / bars_m[d0][1] - 1)
    if len(xs) < 120:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den > 0 else None


def score_pick(pick: dict, days: list[str], bars: dict[str, dict], *, commit_ts: float | None = None, is_fund: bool | None = False) -> dict:
    """days = sessions with an SPY bar, ascending. bars = {symbol: {day: (open, close)}} adjusted."""
    out = {"id": pick["id"], "symbol": pick["symbol"], "recorded_at": pick["recorded_at"], "status": "", "counts": True, "h": {}}
    if pick.get("retro"):
        return {**out, "status": "retro (never scored)", "counts": False}
    if pick["recorded_at"][:10] > END_DATE:
        return {**out, "status": f"recorded after {END_DATE} (does not count)", "counts": False}
    if is_fund:
        return {**out, "status": "a fund, not a common stock (never scored)", "counts": False}
    a = entry_index(days, pick["recorded_at"])
    if a is None:
        return {**out, "status": "waiting for its first open"}
    if is_fund is None:
        return {**out, "status": "stock type could not be checked (BLOCKS the verdict)"}
    if commit_ts is None:
        return {**out, "status": "never committed to git (BLOCKS the verdict)"}
    if commit_ts > open_ts(days[a]):
        return {**out, "status": "committed AFTER its entry open (BLOCKS the verdict)"}
    if pick["symbol"] not in bars or days[a] not in bars[pick["symbol"]]:
        return {**out, "status": "no bar on the entry day (BLOCKS the verdict)"}
    out["entry_day"] = days[a]
    beta = beta_before(bars[pick["symbol"]], bars["SPY"], days, a)
    last = len(days) - 1
    for h in HORIZONS:
        b = a + h
        closed = b <= last
        bb = min(b, last)
        if bb <= a:
            continue
        r, s = span(bars[pick["symbol"]], days, a, bb), span(bars["SPY"], days, a, bb)
        cs = [x for x in (span(bars.get(c, {}), days, a, bb) for c in pick.get("controls", [])) if x is not None]
        if r is None or s is None:
            continue
        full = len(cs) >= 0.8 * N_CONTROL
        out["h"][h] = {"closed": closed, "sessions": bb - a, "pick": r, "spy": s, "vs_spy": (1 + r) / (1 + s) - 1,
                       "vs_random": ((1 + r) / (1 + sum(cs) / len(cs)) - 1) if full else None,
                       "rank": (sum(r > c for c in cs) / len(cs)) if full else None,
                       "beta": beta, "beta_excess": (r - beta * s) if beta is not None else None, "n_control": len(cs),
                       "delisted": days[bb] not in bars[pick["symbol"]]}
    out["status"] = "scored"
    return out


def cluster_t(x: list[float], groups: list[str]) -> float:
    """t of the mean with picks entered in the same month treated as one cluster."""
    n, m = len(x), sum(x) / len(x)
    labs = sorted(set(groups))
    g = len(labs)
    if g < 4:
        return float("nan")
    s = [sum(v - m for v, q in zip(x, groups) if q == lab) for lab in labs]
    var = sum(v * v for v in s) / n ** 2 * g / (g - 1)
    return m / math.sqrt(var) if var > 0 else float("nan")


def verdict(scored: list[dict], h: int = PRIMARY, need: int = NEED, today: str | None = None, t_min: float = 2.0) -> dict:
    """The first `need` countable picks in record order. Any of them unscoreable or without its controls -> BLOCKED."""
    order = sorted((s for s in scored if s.get("counts", True)), key=lambda s: datetime.fromisoformat(s["recorded_at"]))
    first = order[:need]
    res = {"horizon": h, "counted": len(first), "need": need}
    blocked = [f"{s['symbol']}: {s['status']}" for s in first if "BLOCKS" in s["status"]]
    if blocked:
        return {**res, "verdict": "BLOCKED", "why": blocked}
    closed = [s for s in first if s["status"] == "scored" and s["h"].get(h, {}).get("closed")]
    if len(first) < need:
        if today and today > END_DATE:
            return {**res, "verdict": f"UNPROVEN ({len(first)} of {need} picks recorded by {END_DATE})"}
        return {**res, "verdict": f"NOT YET ({len(first)} of {need} picks recorded)"}
    if len(closed) < need:
        return {**res, "verdict": f"NOT YET ({len(closed)} of {need} picks have a closed {h}-session window)"}
    hh = [s["h"][h] for s in closed]
    missing = [s["symbol"] for s in closed if s["h"][h]["vs_random"] is None or s["h"][h]["beta_excess"] is None]
    if missing:
        return {**res, "verdict": "BLOCKED", "why": [f"{m}: no random-name control or no beta" for m in missing]}
    x = [v["vs_spy"] for v in hh]
    months = [s["entry_day"][:7] for s in closed]
    res.update(mean_vs_spy=sum(x) / len(x), median_vs_spy=statistics.median(x), t=cluster_t(x, months), ahead=sum(v > 0 for v in x) / len(x),
               mean_vs_random=statistics.fmean(v["vs_random"] for v in hh), mean_rank=statistics.fmean(v["rank"] for v in hh),
               mean_beta_excess=statistics.fmean(v["beta_excess"] for v in hh), best_share=(max(x) / sum(x)) if sum(x) > 0 else None,
               months=len(set(months)))
    ok = (res["mean_vs_spy"] > 0 and res["t"] >= t_min and res["ahead"] > 0.5 and res["median_vs_spy"] > 0 and res["mean_vs_random"] > 0
          and res["mean_rank"] > 0.5 and res["mean_beta_excess"] > 0)
    res["verdict"] = "PASS" if ok else "FAIL"
    return res


# ---------- data (mini only) ----------
def fetch(symbols: list[str], start: datetime) -> dict[str, dict]:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    with open(ROOT / "secrets_data.json") as fh:
        s = json.load(fh)
    cl = StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])
    out: dict[str, dict] = {}
    end = datetime.now(ET) - timedelta(minutes=20)
    for i in range(0, len(symbols), 50):
        r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbols[i:i + 50], timeframe=TimeFrame.Day, start=start, end=end,
                                               feed=DataFeed.SIP, adjustment=Adjustment.ALL))
        for sym, rows in (r.data or {}).items():
            out[sym] = {b.timestamp.astimezone(ET).strftime("%Y-%m-%d"): (float(b.open), float(b.close)) for b in rows}
    return out


def fund_check(symbols: list[str]) -> dict[str, bool | None]:
    """True = fund / levered product, False = common stock, None = could not check (the asset list is not on this machine)."""
    p = ROOT / "ai_reports" / "allsym" / "assets.json"
    if not p.exists():
        return {s: None for s in symbols}
    sys.path.insert(0, str(ROOT))
    from ticker_filters import is_common, is_not_stock
    with open(p) as fh:
        names = json.load(fh)
    return {s: (None if s not in names else bool(is_not_stock(s, names[s]) or not is_common(s))) for s in symbols}


# ---------- cli ----------
def pct(v) -> str:
    return "   n/a" if v is None else f"{100 * v:+6.1f}%"


def cmd_score(ledger: Path | None = None) -> int:
    ledger = ledger or LEDGER
    picks = read_ledger(ledger)
    if not picks:
        print("no picks recorded yet")
        return 0
    bad = chain_problem(picks)
    if bad:
        print(f"REFUSED: the ledger is damaged: {bad}. Restore docs/pick_ledger.jsonl from git.")
        return 1
    live = [p for p in picks if not p.get("retro")]
    start = min(datetime.fromisoformat(p["recorded_at"]) for p in picks) - timedelta(days=420)
    need = sorted({p["symbol"] for p in live} | {"SPY"} | {c for p in live for c in p.get("controls", [])})
    bars = fetch(need, start)
    days = sorted(bars["SPY"])
    known = set(frozen_universe())                                       # the frozen list is common stocks only
    funds = {**fund_check([p["symbol"] for p in live if p["symbol"] not in known]), **{x: False for x in known}}
    scored = [score_pick(p, days, bars, commit_ts=first_commit_ts(p["id"], ledger), is_fund=funds.get(p["symbol"], False)) for p in picks]
    print(f"{len(picks)} picks; prices through {days[-1]}; picks recorded after {END_DATE} do not count\n")
    print(f"{'symbol':<7}{'recorded':<12}{'entry':<12}" + "".join(f"{f'{h}s pick':>9}{'vs SPY':>8}{'vs rand':>8}{'':>2}" for h in HORIZONS))
    for s in scored:
        if s["status"] != "scored":
            print(f"{s['symbol']:<7}{s['recorded_at'][:10]:<12}{s['status']}")
            continue
        row = f"{s['symbol']:<7}{s['recorded_at'][:10]:<12}{s['entry_day']:<12}"
        for h in HORIZONS:
            x = s["h"].get(h)
            flag = "" if not x else ("†" if x["delisted"] else "" if x["closed"] else "*")
            row += f"{'':>27}" if not x else f"{pct(x['pick']):>9}{pct(x['vs_spy']):>8}{pct(x['vs_random']):>8}{flag:>2}"
        print(row)
    print("\n* = window still open (marked to the latest open; not part of any verdict)   † = stopped trading, valued at its last close")
    today = datetime.now(ET).strftime("%Y-%m-%d")
    ev = verdict(scored, h=EARLY, today=today, t_min=EARLY_T)
    print(f"\nEARLY LOOK at {EARLY} sessions on the first {NEED} picks (needs t >= {EARLY_T}; the {PRIMARY}-session verdict below is the stronger one): {ev['verdict']}")
    if "mean_vs_spy" in ev:
        print(f"  vs SPY: mean {pct(ev['mean_vs_spy'])}, median {pct(ev['median_vs_spy'])}, t {ev['t']:+.2f}, ahead {100 * ev['ahead']:.0f}%; "
              f"vs random names {pct(ev['mean_vs_random'])}; beta-adjusted {pct(ev['mean_beta_excess'])}")
    v = verdict(scored, today=today)
    print(f"\nVERDICT at {PRIMARY} sessions on the first {NEED} picks: {v['verdict']}")
    for w in v.get("why", []):
        print(f"  {w}")
    if "mean_vs_spy" in v:
        print(f"  vs SPY: mean {pct(v['mean_vs_spy'])}, median {pct(v['median_vs_spy'])}, t {v['t']:+.2f} (clustered over {v['months']} entry months), ahead {100 * v['ahead']:.0f}%\n"
              f"  vs 30 random names: mean {pct(v['mean_vs_random'])}, average rank {100 * v['mean_rank']:.0f}th percentile;  beta-adjusted excess {pct(v['mean_beta_excess'])};  "
              f"best single pick = {pct(v['best_share'])} of the total")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record stock picks before the outcome and score them against SPY. Places no orders.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="record a pick now")
    a.add_argument("symbol")
    a.add_argument("--thesis", required=True)
    a.add_argument("--months", type=float, required=True, help="how long you intend to hold")
    a.add_argument("--sell-if", required=True, help="what would make you sell")
    a.add_argument("--retro", action="store_true", help="a pick made after the fact: listed, never scored")
    sub.add_parser("list")
    sub.add_parser("score")
    for sp in sub.choices.values():
        sp.add_argument("--ledger", type=Path, help="a ledger file other than docs/pick_ledger.jsonl (e.g. an automatic picker's)")
    args = ap.parse_args(argv)
    ledger = args.ledger or LEDGER
    if args.cmd == "add":
        try:
            r = add_pick(args.symbol, args.thesis, args.months, args.sell_if, retro=args.retro, path=ledger)
        except ValueError as e:
            print(f"not recorded: {e}")
            return 1
        ok = git_commit(ledger, r["symbol"])
        print(f"recorded {r['symbol']} at {r['recorded_at']} ({'RETRO, never scored' if r['retro'] else 'entry = the next session open'}); "
              f"{len(r['controls'])} control names drawn.")
        print("committed. Now run:  git push" if ok else "NOT COMMITTED (git refused). A pick only counts if it is committed before its entry open: commit docs/pick_ledger.jsonl now.")
        if not r["controls"]:
            print("WARNING: the control list docs/studies/pick_ledger_universe.txt is missing here, so this pick has no random-name control and will BLOCK the verdict.")
        return 0
    if args.cmd == "list":
        for p in read_ledger(ledger):
            print(f"{p['recorded_at'][:16]}  {p['symbol']:<6} {p['hold_months']:g} mo  {'RETRO  ' if p.get('retro') else ''}{p['thesis']}  | sell if: {p['sell_if']}")
        return 0
    return cmd_score(ledger)


if __name__ == "__main__":
    sys.exit(main())
