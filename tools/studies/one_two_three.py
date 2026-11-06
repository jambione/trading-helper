#!/usr/bin/env python3
"""Weekend item 9: the operator's 1-2-3 entry (docs/studies/one_two_three_prereg.json, amended after skeptic round 1). Read-only.

Point 1 = the desk's arm (the fill second t0). Point 2 = a pullback that holds a higher low than where leg 1 started (L0). Point 3 = the open:
a buy-stop one cent above point 1's high. Both the 1-2-3 trade and the control (buying at the arm, the desk's real fill) get the SAME simulated
exit (0.6% initial stop, trail = earlier bars' running high - 0.6%, flat 15:50), and both pay half the SIP quoted spread to exit (min 2 bp);
the 1-2-3 entry also pays it on the way in (the control's real fill already includes it). Net = exit vs entry - 3 bp. A historical pass is
PROMISING only; STRONG needs the forward re-run from 2026-11-09 (prereg amended item 5).

    .venv/bin/python tools/studies/one_two_three.py fetch-quotes   # SIP half-spreads at each needed minute (mini; cached)
    .venv/bin/python tools/studies/one_two_three.py report         # bars from tools/studies/sip_features.py fetch cache
"""
from __future__ import annotations

import json
import math
import pickle
import statistics as st
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
ET = ZoneInfo("America/New_York")
BARS = ROOT / "ai_reports" / "sip_features"
QCACHE = ROOT / "ai_reports" / "one_two_three" / "halfspread.json"
SINCE, UNTIL = "2026-09-16", "2026-11-05"
STOP_PCT, PULL_FRAC, PULL_MIN_BP, TICK = 0.006, 0.30, 10.0, 0.01
WINDOW_SEC, LAST_ENTRY, FLAT = 1800, (15, 45), (15, 50)
COST_BP, MIN_HALF_BP = 3.0, 2.0


def _et(ts):
    return datetime.fromtimestamp(ts, ET)


def _at(ts, hm):
    d = _et(ts)
    return d.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0).timestamp()


def half_bp(hs, sym, ts):
    """Half the median quoted spread (bp) in the minute before ts, from the cache; min 2 bp; 2 bp if unknown."""
    v = hs.get(f"{sym}|{int(ts - ts % 60)}")
    return max(MIN_HALF_BP, float(v)) if v is not None else MIN_HALF_BP


def simulate_exit(bars, entry_px, entry_bar_idx, sym, hs):
    """Exit from bars[entry_bar_idx] on (bars = [(start, o, h, l, c, v)]). Returns (exit_px, exit_ts, reason). Pure given hs."""
    init = entry_px * (1 - STOP_PCT)
    flat = _at(bars[entry_bar_idx][0], FLAT)
    run_hi = None
    for i in range(entry_bar_idx, len(bars)):
        b = bars[i]
        if b[0] >= flat:
            prev = bars[i - 1]
            return prev[4] * (1 - half_bp(hs, sym, flat) / 1e4), flat, "flat"
        stop = init if run_hi is None else max(init, run_hi * (1 - STOP_PCT))
        if b[3] <= stop:
            px = min(stop, b[1]) if i > entry_bar_idx else stop      # gap below the stop fills at the open
            return px * (1 - half_bp(hs, sym, b[0]) / 1e4), b[0], "stop"
        run_hi = b[2] if run_hi is None else max(run_hi, b[2])        # earlier closed bars only feed the next bar's trail
    last = bars[-1]
    return last[4] * (1 - half_bp(hs, sym, last[0]) / 1e4), last[0] + 60, "end"


def find_123(bars, t0):
    """Returns None (no setup) or dict(fill_px_raw, bar_idx, H1, L0, t3). Pure. bars sorted by start."""
    before = [b for b in bars if b[0] + 60 <= t0]
    if len(before) < 15:
        return None
    L0 = min(b[3] for b in before[-15:])
    post_idx = [i for i, b in enumerate(bars) if b[0] >= t0]
    if not post_idx:
        return None
    H, conf = None, None
    for i in post_idx:
        b = bars[i]
        if b[0] > t0 + WINDOW_SEC or b[0] >= _at(t0, LAST_ENTRY):
            return None
        if b[3] <= L0:
            return None                                              # leg 1 broke: no higher low
        H = b[2] if H is None else max(H, b[2])
        thr = max(PULL_FRAC * (H - L0), b[4] * PULL_MIN_BP / 1e4)
        if H > L0 and b[4] <= H - thr:
            conf = i
            break
    if conf is None:
        return None
    H1 = H
    for i in range(conf + 1, len(bars)):
        b = bars[i]
        if b[0] > t0 + WINDOW_SEC or b[0] >= _at(t0, LAST_ENTRY):
            return None
        if b[3] <= L0:
            return None                                              # cancelled, even if the same bar also reaches the trigger
        if b[2] >= H1 + TICK:
            return {"fill_raw": max(H1 + TICK, b[1]), "bar_idx": i, "H1": H1, "L0": L0, "t3": b[0]}
    return None


def run_feed(trades, feed, hs):
    rows, missing = [], []
    cache = {}
    for t in trades:
        key = (t["day"], feed)
        if key not in cache:
            f = BARS / f"{t['day']}_{feed}.pkl"
            cache[key] = pickle.load(open(f, "rb")) if f.exists() else None
        allbars = cache[key]
        bars = sorted(allbars.get(t["sym"], [])) if allbars else []
        if len(bars) < 30:
            missing.append(f"{t['day']} {t['sym']}")
            continue
        t0 = t["fill_ts"]
        post = [i for i, b in enumerate(bars) if b[0] >= t0]
        if not post:
            missing.append(f"{t['day']} {t['sym']}")
            continue
        cx, _, _ = simulate_exit(bars, t["entry"], post[0], t["sym"], hs)
        ctrl = (cx / t["entry"] - 1) * 1e4 - COST_BP
        s = find_123(bars, t0)
        r = {"d": t["day"], "sym": t["sym"], "ctrl": ctrl, "ng": t["never_green"], "taken": False, "net": 0.0}
        if s:
            fill = s["fill_raw"] * (1 + half_bp(hs, t["sym"], s["t3"]) / 1e4)
            ex, _, _ = simulate_exit(bars, fill, s["bar_idx"], t["sym"], hs)
            r.update(taken=True, net=(ex / fill - 1) * 1e4 - COST_BP, wait_bp=(fill / t["entry"] - 1) * 1e4, wait_s=s["t3"] - t0)
        rows.append(r)
    return rows, missing


def tstat(v):
    n = len(v)
    if n < 3:
        return float("nan")
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd else float("nan")


def day_stats(rows):
    days = sorted({r["d"] for r in rows})
    by = {d: [r for r in rows if r["d"] == d] for d in days}
    diff = [sum(r["net"] for r in by[d]) - sum(r["ctrl"] for r in by[d]) for d in days]
    s123 = [sum(r["net"] for r in by[d]) for d in days]
    mid = days[len(days) // 2] if days else ""
    h = lambda v: (sum(x for d, x in zip(days, v) if d < mid) / max(1, sum(1 for d in days if d < mid)),  # noqa: E731
                   sum(x for d, x in zip(days, v) if d >= mid) / max(1, sum(1 for d in days if d >= mid)))
    taken = [r for r in rows if r["taken"]]
    return {"days": len(days), "diff_mean": sum(diff) / max(1, len(diff)), "diff_t": tstat(diff), "diff_h": h(diff),
            "s123_mean": sum(s123) / max(1, len(s123)), "s123_t": tstat(s123), "s123_h": h(s123),
            "n_taken": len(taken), "n_taken_h": (sum(1 for r in taken if r["d"] < mid), sum(1 for r in taken if r["d"] >= mid)),
            "per_trade": sum(r["net"] for r in taken) / max(1, len(taken)), "ctrl_per_arm": sum(r["ctrl"] for r in rows) / max(1, len(rows)),
            "share": len(taken) / max(1, len(rows)), "ng_reach": sum(1 for r in taken if r["ng"]) / max(1, sum(1 for r in rows if r["ng"])),
            "wait_bp": st.median([r["wait_bp"] for r in taken]) if taken else None,
            "wait_min": st.median([r["wait_s"] for r in taken]) / 60 if taken else None}


def verdict(sip, iex):
    c1 = sip["diff_mean"] > 0 and sip["diff_t"] >= 2.0
    c2 = sip["diff_h"][0] > 0 and sip["diff_h"][1] > 0
    c3 = sip["s123_h"][0] > 0 and sip["s123_h"][1] > 0 and sip["s123_mean"] > 0 and sip["s123_t"] >= 2.0
    c4 = sip["n_taken"] >= 150 and min(sip["n_taken_h"]) >= 60
    c5 = iex["diff_h"][0] > 0 and iex["diff_h"][1] > 0
    return {"c1": c1, "c2": c2, "c3": c3, "c4": c4, "c5": c5, "pass": all((c1, c2, c3, c4, c5)),
            "loses_less_only": c1 and c2 and not c3}


def needed_minutes(trades):
    """(sym, minute_ts) pairs whose half-spread the study needs: control exits and every 1-2-3 entry/exit. Uses SIP bars."""
    need = set()
    for feed in ("sip", "iex"):
        rows_cache = {}
        for t in trades:
            key = t["day"]
            if key not in rows_cache:
                f = BARS / f"{t['day']}_{feed}.pkl"
                rows_cache[key] = pickle.load(open(f, "rb")) if f.exists() else None
            allbars = rows_cache[key]
            bars = sorted(allbars.get(t["sym"], [])) if allbars else []
            if len(bars) < 30:
                continue
            post = [i for i, b in enumerate(bars) if b[0] >= t["fill_ts"]]
            if not post:
                continue
            _, ts, _ = simulate_exit(bars, t["entry"], post[0], t["sym"], {})
            need.add((t["sym"], int(ts - ts % 60)))
            s = find_123(bars, t["fill_ts"])
            if s:
                need.add((t["sym"], int(s["t3"] - s["t3"] % 60)))
                _, ts2, _ = simulate_exit(bars, s["fill_raw"], s["bar_idx"], t["sym"], {})
                need.add((t["sym"], int(ts2 - ts2 % 60)))
    return need


def fetch_quotes():
    import alpaca_api as aa
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    from config import load_config_ro
    from early_path import trades_since
    tr, _ = trades_since(SINCE)
    tr = [t for t in tr if t["day"] <= UNTIL]
    client = aa.connect_data_client(dict(load_config_ro() or {}))
    QCACHE.parent.mkdir(parents=True, exist_ok=True)
    hs = json.loads(QCACHE.read_text()) if QCACHE.exists() else {}
    need = needed_minutes(tr)
    todo = [n for n in sorted(need) if f"{n[0]}|{n[1]}" not in hs]
    print(f"half-spread minutes needed {len(need)}, to fetch {len(todo)}")
    for k, (sym, m) in enumerate(todo):
        a = datetime.fromtimestamp(m - 60, timezone.utc)
        b = datetime.fromtimestamp(m, timezone.utc)
        try:
            q = client.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=a, end=b, feed=DataFeed.SIP, limit=10000))
            sp = [(x.ask_price - x.bid_price) / ((x.ask_price + x.bid_price) / 2) * 1e4 for x in (q.data or {}).get(sym, [])
                  if x.bid_price and x.ask_price and x.ask_price > x.bid_price]
            hs[f"{sym}|{m}"] = round(st.median(sp) / 2, 2) if sp else None
        except Exception as e:  # noqa: BLE001
            print("ERR", sym, m, str(e)[:80])
        if k % 100 == 0:
            QCACHE.write_text(json.dumps(hs))
        time.sleep(0.15)
    QCACHE.write_text(json.dumps(hs))
    print("done")
    return 0


def wait_txt(s_):
    return "-" if s_["wait_min"] is None else f"{s_['wait_min']:.1f}"


def report():
    from early_path import trades_since
    tr, unmatched = trades_since(SINCE)
    tr = [t for t in tr if t["day"] <= UNTIL]
    hs = json.loads(QCACHE.read_text()) if QCACHE.exists() else {}
    sip_rows, miss_sip = run_feed(tr, "sip", hs)
    iex_rows, miss_iex = run_feed(tr, "iex", hs)
    total = len(tr) + unmatched
    void = unmatched > 0.10 * total or len(miss_sip) > 0.10 * max(1, len(tr))
    S, I = day_stats(sip_rows), day_stats(iex_rows)
    v = verdict(S, I)
    f = lambda x: "-" if x is None else f"{x:+.1f}"  # noqa: E731
    out = [f"# 1-2-3 entry vs buying at the arm (prereg docs/studies/one_two_three_prereg.json), written {datetime.now(ET):%Y-%m-%d %H:%M} ET", "",
           f"Arms {SINCE}..{UNTIL}: {len(tr)} matched ({unmatched} outcomes without a fill row), SIP bars missing for {len(miss_sip)}, IEX {len(miss_iex)}; "
           f"half-spread minutes cached {len(hs)}. {'**VOID: more than 10% dropped, not scored.**' if void else ''}", ""]
    for lab, s_ in (("SIP (primary)", S), ("IEX (transfer check)", I)):
        out += [f"## {lab}",
                f"- sessions {s_['days']}; arms reaching point 3: {s_['n_taken']} ({100 * s_['share']:.0f}%), by half {s_['n_taken_h']}; never-green arms that reach point 3: {100 * s_['ng_reach']:.0f}%",
                f"- per day: 1-2-3 minus buy-at-arm {s_['diff_mean']:+.1f} bp (t {s_['diff_t']:+.2f}), halves {f(s_['diff_h'][0])} / {f(s_['diff_h'][1])}",
                f"- 1-2-3 day sum {s_['s123_mean']:+.1f} bp (t {s_['s123_t']:+.2f}), halves {f(s_['s123_h'][0])} / {f(s_['s123_h'][1])}; per 1-2-3 trade {s_['per_trade']:+.1f} bp; buy-at-arm per arm {s_['ctrl_per_arm']:+.1f} bp",
                f"- cost of waiting: median fill {f(s_['wait_bp'])} bp above the arm price, {wait_txt(s_)} min after the arm", ""]
    lab = ("VOID" if void else ("PROMISING (historical pass; STRONG needs the forward re-run from 2026-11-09)" if v["pass"]
           else ("LOSES LESS, DOES NOT MAKE MONEY (not a pass)" if v["loses_less_only"] else "FAIL / not shown")))
    out += [f"Conditions: (1) day diff t>=2 {v['c1']}; (2) both halves {v['c2']}; (3) makes money, t>=2, both halves {v['c3']}; "
            f"(4) >=150 trades, >=60 per half {v['c4']}; (5) IEX both halves {v['c5']}", "", f"**VERDICT: {lab}**", ""]
    (ROOT / "docs" / "ONE_TWO_THREE.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))
    return 0


def main(argv=None):
    a = (argv if argv is not None else sys.argv[1:]) or ["report"]
    return fetch_quotes() if a[0] == "fetch-quotes" else report()


if __name__ == "__main__":
    raise SystemExit(main())
