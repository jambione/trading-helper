#!/usr/bin/env python3
"""Early path after the fill (operator 11/06: 'we can see a difference after we opened until we exited ... if we can see similarities after
the open at a measured time and with confidence we should close those'). EXPLORATORY, read-only, SIP trades (free once 15 min old).

For each desk buy since 2026-10-15 (the clean volume-ratio era): the fill second from the fills ledger, then the SIP trade prints from
the fill to 5 minutes later (or the exit, whichever is first). At checkpoints T = 5, 10, 15, 20, 30, 45, 60, 90, 120 s it measures
    green_by_T       a print >= fill + 5 bp (the never-green definition) happened within T seconds
    down_X_by_T      price at T is <= fill - X bp and no print was green by T   (X = 5, 10, 15, 20)
and the tick-rule signed trade flow since the fill (second post-open signal), and for the group that is NOT green by T (or 'down X and not green'):
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
# STRONG-SIGNAL BAR (operator 11/06: 'I'm looking for a strong signal - nothing weak'). Fixed BEFORE the first run (16:37 11/06) and not tunable.
# A rule is STRONG only if it clears ALL of these in BOTH the TRAIN half and the TEST half, on at least STRONG_MIN_N flagged trades each:
STRONG_MIN_N = 30            # flagged trades per half
STRONG_MIN_SHARE = 0.08      # flagged share of all trades per half (a rule that almost never fires cannot matter)
STRONG_MAX_FALSE = 0.25      # at most 25% of flagged trades later turn green
STRONG_MAX_HELD = -35.0      # held net of the flagged group at most -35 bp (the saving a cut can bank)
STRONG_MIN_NG = 0.75         # at least 75% of flagged trades end never-green
# PAYS-ON-PAPER tier (second list, NOT the strong bar): what cutting at the checkpoint would have earned, from the prints, with an exit haircut.
CUT_HAIRCUT_BP = 6.0         # assumed cost of selling at the checkpoint: ~2.3 bp half-spread plus ~4 bp slippage (observed 5-19 bp on trail sells)
PAYS_MIN_GAIN = 10.0         # mean gain per flagged trade, bp, in BOTH halves
PAYS_MIN_N = 30


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


def signed_flow(w):
    """Tick-rule signed volume imbalance in [-1, 1] over prints w = [(ts, px, size)]: + when price ticks up, - when down, equal ticks keep
    the last sign. None when there is no volume information. Pure."""
    sign, prev, buy, sell = 0, None, 0.0, 0.0
    for r in w:
        p, sz = r[1], (r[2] if len(r) > 2 else 0.0)
        if prev is not None:
            if p > prev:
                sign = 1
            elif p < prev:
                sign = -1
        prev = p
        if sign > 0:
            buy += sz
        elif sign < 0:
            sell += sz
    tot = buy + sell
    return None if tot <= 0 else (buy - sell) / tot


def path_features(prints, entry, fill_ts):
    """prints: [(ts, px[, size])] sorted, from the fill on. Returns {T: {green, px_bp, min_bp, n, flow}} measured within T seconds. Pure."""
    res = {}
    for T in CHECK:
        w = [r for r in prints if fill_ts <= r[0] <= fill_ts + T]
        if not w:
            res[T] = None
            continue
        px = [r[1] for r in w]
        res[T] = {"green": any((p / entry - 1) * 1e4 >= GREEN_BP for p in px), "px_bp": (px[-1] / entry - 1) * 1e4,
                  "min_bp": (min(px) / entry - 1) * 1e4, "n": len(w), "flow": signed_flow(w)}
    return res


def later_green(prints, entry, fill_ts, T):
    return any((r[1] / entry - 1) * 1e4 >= GREEN_BP for r in prints if fill_ts + T < r[0] <= fill_ts + WINDOW)


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
            rows = [(x.timestamp.timestamp(), float(x.price), float(x.size or 0)) for x in (got.data or {}).get(t["sym"], [])]
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
        pr = [(r[0], r[1], (r[2] if len(r) > 2 else 0.0)) for r in pickle.load(open(f, "rb"))]
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


def rule_stats(rows, T, X):
    g = _grp(rows, T, X)
    if not rows or not g:
        return None
    return {"n": len(g), "share": len(g) / len(rows), "false": sum(later_green(r["prints"], r["entry"], r["fill_ts"], T) for r in g) / len(g),
            "held": sum(r["net_bp"] for r in g) / len(g), "ng": sum(r["never_green"] for r in g) / len(g)}


def cut_gain(rows, T, X, haircut=CUT_HAIRCUT_BP):
    """Mean paper gain per flagged trade of selling at the checkpoint instead of holding: (price at T - haircut - 3) - held net, in bp. Pure."""
    g = _grp(rows, T, X)
    if not g:
        return None
    gains = [(r["feat"][T]["px_bp"] - haircut - 3.0) - r["net_bp"] for r in g]
    return {"n": len(g), "gain": sum(gains) / len(gains), "total": sum(gains)}


def pays_rules(train, test):
    out = []
    for T in CHECK:
        for X in (None,) + XS:
            a, b = cut_gain(train, T, X), cut_gain(test, T, X)
            if a and b and a["n"] >= PAYS_MIN_N and b["n"] >= PAYS_MIN_N and a["gain"] >= PAYS_MIN_GAIN and b["gain"] >= PAYS_MIN_GAIN:
                out.append((T, X, a, b))
    return out


def is_strong(st):
    return bool(st) and st["n"] >= STRONG_MIN_N and st["share"] >= STRONG_MIN_SHARE and st["false"] <= STRONG_MAX_FALSE \
        and st["held"] <= STRONG_MAX_HELD and st["ng"] >= STRONG_MIN_NG


def strong_rules(train, test):
    """Rules (T, X) that clear the strong bar in BOTH halves. Pure."""
    out = []
    for T in CHECK:
        for X in (None,) + XS:
            a, b = rule_stats(train, T, X), rule_stats(test, T, X)
            if is_strong(a) and is_strong(b):
                out.append((T, X, a, b))
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


def flow_table(rows, label):
    """Second post-open signal: among trades with no green print by T, does sell-dominated flow since the fill (signed volume <= 0 by the
    tick rule) pick out the ones that never turn green? Same columns as the price table."""
    lines = [f"### {label}: trade flow after the fill (no green print by T seconds)", "",
             "| T | sell-dominated flow (<= -0.2): flagged | later turns green | never-green in flagged | held net | balanced or buy flow: n | later turns green | held net |", "|---|---|---|---|---|---|---|---|"]
    for T in CHECK:
        base = _grp(rows, T)
        sell = [r for r in base if r["feat"][T].get("flow") is not None and r["feat"][T]["flow"] <= -0.2]
        oth = [r for r in base if r["feat"][T].get("flow") is not None and r["feat"][T]["flow"] > -0.2]
        if len(sell) < 10 or len(oth) < 10:
            continue
        g = lambda gr: sum(later_green(r["prints"], r["entry"], r["fill_ts"], T) for r in gr) / len(gr)  # noqa: E731
        lines.append(f"| {T}s | {len(sell)} | {100 * g(sell):.0f}% | {100 * sum(r['never_green'] for r in sell) / len(sell):.0f}% | "
                     f"{sum(r['net_bp'] for r in sell) / len(sell):+.1f} | {len(oth)} | {100 * g(oth):.0f}% | {sum(r['net_bp'] for r in oth) / len(oth):+.1f} |")
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
    sr = strong_rules(train, test)
    out += ["## STRONG signals (bar fixed before the first run; ALL of these in BOTH halves)", "",
            f"At least {STRONG_MIN_N} flagged trades and {100 * STRONG_MIN_SHARE:.0f}% of trades per half, false alarms <= {100 * STRONG_MAX_FALSE:.0f}%, "
            f"held net of flagged <= {STRONG_MAX_HELD:.0f} bp, never-green >= {100 * STRONG_MIN_NG:.0f}% of flagged.", ""]
    if sr:
        out += ["| rule | train: n / false alarm / held bp | test: n / false alarm / held bp |", "|---|---|---|"]
        for T, X, a, b in sr:
            out.append(f"| T={T}s{'' if X is None else f', down {X} bp'} | {a['n']} / {100 * a['false']:.0f}% / {a['held']:+.0f} | {b['n']} / {100 * b['false']:.0f}% / {b['held']:+.0f} |")
    else:
        out.append("**None.** No rule clears the strong bar in both halves. Everything below is weak and is not a basis for an exit change.")
    out += [""]
    pr = pays_rules(train, test)
    out += ["## Pays on paper (second list: cutting at the checkpoint instead of holding)", "",
            f"Assumes the cut sells at the checkpoint print less {CUT_HAIRCUT_BP:.0f} bp (spread + slippage). A rule is listed only if the mean gain per flagged trade is "
            f">= {PAYS_MIN_GAIN:.0f} bp in BOTH halves on >= {PAYS_MIN_N} flagged trades each. This is NOT the strong bar: it says a rule is worth a real replay, not that it is proven.", ""]
    if pr:
        out += ["| rule | train: n / gain per trade / total bp | test: n / gain per trade / total bp |", "|---|---|---|"]
        for T, X, a, b in pr:
            out.append(f"| T={T}s{'' if X is None else f', down {X} bp'} | {a['n']} / {a['gain']:+.0f} / {a['total']:+.0f} | {b['n']} / {b['gain']:+.0f} / {b['total']:+.0f} |")
    else:
        out.append("**None.** No rule would have paid in both halves at this haircut.")
    out += [""]
    out += table(train, "TRAIN") + [""] + flow_table(train, "TRAIN") + [""] + table(test, "TEST (do not tune on this)") + [""] + flow_table(test, "TEST (do not tune on this)")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "EARLY_PATH.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))
    return 0


def main(argv=None):
    a = (argv if argv is not None else sys.argv[1:]) or ["report"]
    return fetch() if a[0] == "fetch" else report()


if __name__ == "__main__":
    raise SystemExit(main())
