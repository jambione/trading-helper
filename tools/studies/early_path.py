#!/usr/bin/env python3
"""Early path after the fill (operator 11/06: 'we can see a difference after we opened until we exited ... if we can see similarities after
the open at a measured time and with confidence we should close those'). EXPLORATORY, read-only, SIP trades (free once 15 min old).

For each desk buy since 2026-10-15 (the clean volume-ratio era): the fill second from the fills ledger, then the SIP trade prints from
the fill to 5 minutes later (or the exit, whichever is first). At checkpoints T = 5, 10, 15, 20, 30, 45, 60, 90, 120 s it measures
    green_by_T       a print >= fill + 5 bp (the never-green definition) happened within T seconds
    down_X_by_T      price at T is <= fill - X bp and no print was green by T   (X = 5, 10, 15, 20)
and for the group that is NOT green by T (or 'down X and not green'):
    later_green      share that still became green after T   (the false-alarm rate of an early cut)
    final net bp     mean outcome of those trades if simply held (what the desk actually got)
A rule is only interesting if the flagged group has a LOW later_green share and a clearly negative held net.
TRAIN = fills 10/15-10/29, TEST = 10/30 onward. The prereg freezes ONE rule from TRAIN only; TEST is then read once. No exit is simulated here.

    .venv/bin/python tools/studies/early_path.py fetch    # mini only, after 16:30 ET; caches prints in ai_reports/early_path/
    .venv/bin/python tools/studies/early_path.py report   # table from the cache, no API
"""
from __future__ import annotations

import json
import pickle
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ET = ZoneInfo("America/New_York")
CACHE = ROOT / "ai_reports" / "early_path"
SINCE, SPLIT = "2026-10-15", "2026-10-30"
CHECK = (5, 10, 15, 20, 30, 45, 60, 90, 120)
XS = (5, 10, 15, 20)
GREEN_BP = 5.0
WINDOW = 300.0


def trades_since(since=SINCE):
    """Desk buys with the real fill second. [{sym, day, fill_ts, entry, exit_px, peak, net_bp, never_green}]"""
    fills = {}
    for p in sorted((ROOT / "ai_reports" / "fills").glob("20??-??-??.jsonl")):
        if p.stem < since:
            continue
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") == "fill" and r.get("side") == "buy" and r.get("status") == "filled" and r.get("filled_at") and r.get("filled_avg_price"):
                try:
                    ts = datetime.fromisoformat(str(r["filled_at"]).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                fills.setdefault((p.stem, r["symbol"]), []).append((ts, float(r["filled_avg_price"])))
    out, unmatched = [], 0
    for line in (ROOT / "ai_reports" / "outcomes.jsonl").read_text().splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        e, x, t = o.get("entry_price"), o.get("exit_price"), o.get("entry_time")
        if not (e and x and t):
            continue
        day = datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d")
        if day < since:
            continue
        cand = [(abs(ts - t), ts) for ts, px in fills.get((day, o["symbol"]), []) if abs(px - e) <= max(0.02, e * 3e-4) and -15 <= ts - t <= 60]
        if not cand:
            unmatched += 1
            continue
        fill_ts = min(cand)[1]
        pk = o.get("peak_price") or e
        out.append({"sym": o["symbol"], "day": day, "fill_ts": fill_ts, "entry": float(e), "exit_px": float(x), "peak": float(pk),
                    "exit_ts": float(o.get("exit_time") or fill_ts + WINDOW), "net_bp": (x / e - 1) * 1e4,
                    "never_green": (pk - e) / e * 1e4 < GREEN_BP})
    return out, unmatched


def path_features(prints, entry, fill_ts):
    """prints: [(ts, px)] sorted, from the fill on. Returns {T: {green, px, minpx}} measured within T seconds of the fill. Pure."""
    res = {}
    for T in CHECK:
        w = [(t, p) for t, p in prints if fill_ts <= t <= fill_ts + T]
        if not w:
            res[T] = None
            continue
        green = any((p / entry - 1) * 1e4 >= GREEN_BP for _, p in w)
        res[T] = {"green": green, "px_bp": (w[-1][1] / entry - 1) * 1e4, "min_bp": (min(p for _, p in w) / entry - 1) * 1e4, "n": len(w)}
    return res


def later_green(prints, entry, fill_ts, T):
    return any((p / entry - 1) * 1e4 >= GREEN_BP for t, p in prints if fill_ts + T < t <= fill_ts + WINDOW)


def blocked_now():
    n = datetime.now(ET)
    return n.weekday() < 5 and (9, 0) <= (n.hour, n.minute) < (16, 30)


def fetch():
    if blocked_now():
        print("refused: no Alpaca pulls 09:00-16:30 ET on weekdays (desk API budget)")
        return 1
    import alpaca_api as aa
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    from config import load_config_ro
    client = aa.connect_data_client(dict(load_config_ro() or {}))
    CACHE.mkdir(parents=True, exist_ok=True)
    tr, unmatched = trades_since()
    today = datetime.now(ET).strftime("%Y-%m-%d")
    done = bad = 0
    for t in tr:
        f = CACHE / f"{t['day']}_{t['sym']}_{int(t['fill_ts'])}.pkl"
        if f.exists() or t["day"] >= today and (datetime.now(ET).hour, datetime.now(ET).minute) < (16, 30):
            continue
        a, b = t["fill_ts"] - 2, min(t["exit_ts"], t["fill_ts"] + WINDOW) + 1
        try:
            got = client.get_stock_trades(StockTradesRequest(symbol_or_symbols=t["sym"], start=datetime.fromtimestamp(a, timezone.utc),
                                                             end=datetime.fromtimestamp(b, timezone.utc), feed=DataFeed.SIP, limit=10000))
            rows = [(x.timestamp.timestamp(), float(x.price)) for x in (got.data or {}).get(t["sym"], [])]
        except Exception as e:  # noqa: BLE001
            bad += 1
            print("ERR", t["day"], t["sym"], str(e)[:80])
            continue
        pickle.dump(sorted(rows), open(f, "wb"))
        done += 1
        time.sleep(0.2)
    print(f"fetched {done}, errors {bad}, unmatched outcomes (no fill row) {unmatched}, total trades {len(tr)}")
    return 0


def load_all():
    tr, unmatched = trades_since()
    rows = []
    for t in tr:
        f = CACHE / f"{t['day']}_{t['sym']}_{int(t['fill_ts'])}.pkl"
        if not f.exists():
            continue
        pr = [(a, b) for a, b in pickle.load(open(f, "rb"))]
        if len(pr) < 3:
            continue
        rows.append({**t, "prints": pr, "feat": path_features(pr, t["entry"], t["fill_ts"])})
    return rows, unmatched


def _grp(rows, T, X=None):
    """Flagged = no green print within T s (and, if X, price at T at least X bp below the fill)."""
    out = []
    for r in rows:
        f = r["feat"].get(T)
        if not f or f["green"]:
            continue
        if X is not None and f["px_bp"] > -X:
            continue
        out.append(r)
    return out


def table(rows, label):
    lines = [f"### {label}: {len(rows)} trades, never-green {100 * sum(r['never_green'] for r in rows) / max(1, len(rows)):.0f}%, "
             f"held net {sum(r['net_bp'] for r in rows) / max(1, len(rows)):+.1f} bp/trade", "",
             "| rule: no green print and price down X bp by T seconds | flagged | share of all trades | later turns green (false alarm) | never-green in flagged | held net of flagged (bp) | held net of the rest |",
             "|---|---|---|---|---|---|---|"]
    for T in CHECK:
        for X in (None,) + XS:
            g = _grp(rows, T, X)
            if len(g) < 10:
                continue
            ids = {id(r) for r in g}
            rest = [r for r in rows if id(r) not in ids and r["feat"].get(T)]
            lg = sum(later_green(r["prints"], r["entry"], r["fill_ts"], T) for r in g) / len(g)
            ng = sum(r["never_green"] for r in g) / len(g)
            lines.append(f"| T={T}s{'' if X is None else f', down {X} bp'} | {len(g)} | {100 * len(g) / len(rows):.0f}% | {100 * lg:.0f}% | {100 * ng:.0f}% | "
                         f"{sum(r['net_bp'] for r in g) / len(g):+.1f} | {sum(r['net_bp'] for r in rest) / max(1, len(rest)):+.1f} |")
    return lines


def report():
    rows, unmatched = load_all()
    if not rows:
        print("no cached paths: run 'fetch' first")
        return 1
    train = [r for r in rows if r["day"] < SPLIT]
    test = [r for r in rows if r["day"] >= SPLIT]
    out = [f"# Early path after the fill (exploratory), written {datetime.now(ET):%Y-%m-%d %H:%M} ET", "",
           f"{len(rows)} desk buys with SIP prints ({len(train)} train 10/15-10/29, {len(test)} test 10/30 on); {unmatched} outcomes had no matching fill row. "
           "A useful early-cut rule needs a LOW false-alarm share (flagged trades that later turn green) and a clearly NEGATIVE held net. "
           "No exit is simulated; the rule is frozen from TRAIN only and TEST is read once.", ""]
    out += table(train, "TRAIN") + [""] + table(test, "TEST (do not tune on this)")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "EARLY_PATH.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))
    return 0


def main(argv=None):
    a = (argv if argv is not None else sys.argv[1:]) or ["report"]
    return fetch() if a[0] == "fetch" else report()


if __name__ == "__main__":
    raise SystemExit(main())
