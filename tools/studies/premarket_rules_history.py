#!/usr/bin/env python3
"""premarket_rules_history (docs/studies/premarket_rules_history_prereg.json, amended after skeptic round 1). Read-only; FREE historical SIP only.

Two long-only premarket entry rules on the operator's paid momentum-alert names (first alert per symbol per session):
  R1 pullback that holds a higher low, then a buy-stop break of the pre-pullback high; R2 break of the 10-minute range after the alert.
Same exit for both and for the random-minute control: sell at the close of the bar starting 10 minutes after the fill (never after the 09:24 bar), 5% hard stop.
Costs: half the SIP quoted spread (min 5 bp per side) in and out + 3 bp. A pass only earns the paid SIP month as a forward dry run.

    .venv/bin/python tools/studies/premarket_rules_history.py fetch-bars     # mini, free historical SIP bars
    .venv/bin/python tools/studies/premarket_rules_history.py plan           # detect rules, list the quote minutes needed
    .venv/bin/python tools/studies/premarket_rules_history.py fetch-quotes   # per-minute quoted-spread medians for those minutes
    .venv/bin/python tools/studies/premarket_rules_history.py report
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import pickle
import random
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
SESS = ROOT / "ai_reports" / "sessions"
OUT = ROOT / "ai_reports" / "premarket_rules_history"
SINCE, UNTIL = "2026-09-16", "2026-11-05"
MIN_PRICE, MIN_BARS_60, LOOKBACK = 2.0, 15, 15
FLOOR_BP, COST_BP, STOP_PCT, HOLD_MIN = 5.0, 3.0, 0.05, 10
LAST_ENTRY_HM, LAST_EXIT_HM = (9, 15), (9, 24)
MIN_QUOTES, SPARSE_EXIT_HALF_BP = 20, 50.0
N_CONTROL = 20


# ---------- time helpers ----------
def at_hm(ts, hm):
    d = datetime.fromtimestamp(ts, ET)
    return d.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0).timestamp()


def floor_min(ts):
    return ts - (ts % 60)


def ceil_min(ts):
    return ts if ts % 60 == 0 else ts + (60 - ts % 60)


# ---------- alerts ----------
def read_alerts(day):
    """-> (rows, mismatches). rows = [(ts, symbol)] from the day's discord.jsonl.gz."""
    p = SESS / day / "discord.jsonl.gz"
    rows, bad = [], 0
    if not p.exists():
        return rows, bad
    with gzip.open(p, "rt", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            sym, ts = str(r.get("symbol") or "").upper().strip(), r.get("ts")
            if not sym or ts is None:
                continue
            et = str(r.get("et") or "")
            if et and et[:8] != datetime.fromtimestamp(float(ts), ET).strftime("%H:%M:%S"):
                bad += 1
            rows.append((float(ts), sym))
    return rows, bad


def name_days(day, rows, prev_rows):
    """First alert per symbol with the amended filters. -> (list of (sym, t0), drop counts). Pure."""
    drops = {"early_capture": 0, "carryover": 0, "out_of_window": 0}
    if not rows:
        return [], drops
    first_cap = min(t for t, _ in rows)
    prev_last = max((t for t, _ in prev_rows), default=None)
    carry = {s for t, s in prev_rows if prev_last is not None and t >= prev_last - 1800}
    seen, out = set(), []
    for t, s in sorted(rows):
        if s in seen:
            continue
        seen.add(s)
        if t < at_hm(t, (5, 0)) or t > at_hm(t, LAST_ENTRY_HM):
            drops["out_of_window"] += 1
        elif t - first_cap < 600:
            drops["early_capture"] += 1
        elif s in carry:
            drops["carryover"] += 1
        else:
            out.append((s, t))
    return out, drops


# ---------- detectors (bars = [(start, o, h, l, c, v)] sorted) ----------
def l0_and_price(bars, t0):
    closed = [b for b in bars if b[0] + 60 <= t0]
    if len(closed) < LOOKBACK:
        return None, None
    last = closed[-1]
    if t0 - (last[0] + 60) > 300:
        return None, None
    n60 = sum(1 for b in bars if t0 - 3600 <= b[0] < t0)
    if n60 < MIN_BARS_60:
        return None, None
    return min(b[3] for b in closed[-LOOKBACK:]), float(last[4])


def detect_r1(bars, t0, L0):
    after = [i for i, b in enumerate(bars) if b[0] >= ceil_min(t0)]
    lim = min(t0 + 45 * 60, at_hm(t0, LAST_ENTRY_HM))
    H, conf = None, None
    for i in after:
        b = bars[i]
        if b[0] >= lim or b[3] <= L0:
            return None
        H = b[2] if H is None else max(H, b[2])
        if H > L0 and b[4] <= H - max(0.30 * (H - L0), 0.005 * b[4]):
            conf = i
            break
    if conf is None:
        return None
    H1 = H
    for j in range(conf + 1, len(bars)):
        b = bars[j]
        if b[0] >= lim or b[3] <= L0:
            return None
        if b[2] >= H1 * 1.0005:
            return {"idx": j, "fill_raw": max(H1 * 1.0005, b[1])}
    return None


def detect_r2(bars, t0):
    c0 = ceil_min(t0)
    rng = [b for b in bars if c0 <= b[0] < c0 + 600]
    if len(rng) < 5:
        return None
    hi, lo = max(b[2] for b in rng), min(b[3] for b in rng)
    lim = min(t0 + 40 * 60, at_hm(t0, LAST_ENTRY_HM))
    for j, b in enumerate(bars):
        if b[0] < c0 + 600:
            continue
        if b[0] >= lim or b[3] <= lo * 0.995:
            return None
        if b[2] >= hi * 1.0005:
            return {"idx": j, "fill_raw": max(hi * 1.0005, b[1])}
    return None


def fill_ok(b):
    return b[5] >= 200 and b[5] * b[4] >= 2000


# ---------- exit ----------
def simulate_exit(bars, idx, fill_raw):
    """-> (exit_raw, exit_ts, reason) or None. Stop 5% below the fill (fill bar's own low counts), else the close of the bar starting
    HOLD_MIN after the fill minute (capped at the 09:24 bar); if that bar is absent, the open of the next existing bar; never a stale close."""
    stop = fill_raw * (1 - STOP_PCT)
    fb = bars[idx][0]
    cap = at_hm(fb, LAST_EXIT_HM)
    target = min(fb + HOLD_MIN * 60, cap)
    for k in range(idx, len(bars)):
        b = bars[k]
        if b[0] > target:
            return float(b[1]), b[0], "next_open" if b[0] <= cap + 60 else None
        if b[3] <= stop:
            return min(stop, float(b[1])), b[0] + 60, "stop"
        if b[0] == target:
            return float(b[4]), b[0] + 60, "time"
    return None


def half_spread(table, key_ts, mult=1.0, sparse=None):
    """Half spread (bp, floor 5) from the per-minute table for the minute starting key_ts; None if sparse and no fallback."""
    v = table.get(int(key_ts))
    if v is None or v[1] < MIN_QUOTES:
        return None if sparse is None else sparse
    return max(FLOOR_BP, v[0] / 2.0) * mult


def trade_net(bars, idx, fill_raw, table, mult=1.0):
    """Net bp for one trade, or (None, reason). Entry spread = the 60 s before the fill minute; sparse entry drops; sparse exit uses max(entry, 50 bp)."""
    if not fill_ok(bars[idx]):
        return None, "thin_fill"
    ex = simulate_exit(bars, idx, fill_raw)
    if ex is None:
        return None, "no_exit"
    he = half_spread(table, bars[idx][0] - 60, mult)
    if he is None:
        return None, "no_entry_quotes"
    hx = half_spread(table, ex[1] - 60, mult, sparse=max(he, SPARSE_EXIT_HALF_BP * mult))
    entry = fill_raw * (1 + he / 1e4)
    exit_ = ex[0] * (1 - hx / 1e4)
    return (exit_ / entry - 1) * 1e4 - COST_BP, ex[2]


def control_starts(bars, sym, day, t0, fill_ts):
    """20 control entry bars: random minutes in [t0+5m, t0+60m], before 09:15, within +-30 min of the rule's fill clock time. -> list of bar idx."""
    lo, hi = t0 + 300, min(t0 + 3600, at_hm(t0, LAST_ENTRY_HM))
    lo2, hi2 = max(lo, fill_ts - 1800), min(hi, fill_ts + 1800)
    if hi2 - lo2 < 60:
        return []
    out = []
    for k in range(N_CONTROL):
        rnd = random.Random(int(hashlib.sha256(f"{sym}|{day}|PM-rand|{k}".encode()).hexdigest(), 16) % (2 ** 32))
        m = floor_min(lo2 + rnd.random() * (hi2 - lo2))
        nxt = next((i for i, b in enumerate(bars) if b[0] >= m + 60), None)
        if nxt is not None:
            out.append(nxt)
    return out


# ---------- statistics ----------
def tcrit(df, alpha=0.0125):
    from scipy import stats
    return float(stats.t.ppf(1 - alpha, max(1, df)))


def tstat(v):
    n = len(v)
    if n < 3:
        return float("nan")
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd else float("nan")


def evaluate(trades, halves_cut):
    """trades: [{d, sym, net, net15, ctrl (None allowed)}]. Pure. Returns the prereg's conditions."""
    if not trades:
        return {"status": "NO_TRADES"}
    days = sorted({t["d"] for t in trades})
    by_day = {d: [t for t in trades if t["d"] == d] for d in days}
    day_net = {d: sum(t["net"] for t in v) / len(v) for d, v in by_day.items()}
    syms = sorted({t["sym"] for t in trades})
    sym_net = {s: sum(t["net"] for t in trades if t["sym"] == s) / sum(1 for t in trades if t["sym"] == s) for s in syms}
    h1 = [t for t in trades if t["d"] < halves_cut]
    h2 = [t for t in trades if t["d"] >= halves_cut]
    n_h = (len(h1), len(h2))
    allnet = [t["net"] for t in trades]
    sd = st.pstdev(allnet) if len(allnet) > 1 else float("nan")
    mde = 2.4 * sd / math.sqrt(max(1, len(days)))
    under = len(trades) < 100 or min(n_h) < 40
    dv = list(day_net.values())
    t_day, t_sym = tstat(dv), tstat(list(sym_net.values()))
    crit_d, crit_s = tcrit(len(days) - 1), tcrit(len(syms) - 1)
    mean = lambda x: sum(x) / len(x) if x else float("nan")  # noqa: E731
    half_means = []
    for h in (h1, h2):
        dd = {}
        for t in h:
            dd.setdefault(t["d"], []).append(t["net"])
        half_means.append(mean([mean(v) for v in dd.values()]))
    lift = [t["net"] - t["ctrl"] for t in trades if t.get("ctrl") is not None]
    half_lift = []
    for h in (h1, h2):
        v = [t["net"] - t["ctrl"] for t in h if t.get("ctrl") is not None]
        half_lift.append(mean(v))
    best3 = set(sorted(day_net, key=lambda d: -day_net[d])[:3])
    top_names = set(sorted(syms, key=lambda s: -sum(t["net"] for t in trades if t["sym"] == s))[:3])
    drop_days = mean([t["net"] for t in trades if t["d"] not in best3])
    drop_names = mean([t["net"] for t in trades if t["sym"] not in top_names])
    c = {"1": mean(dv) > 0 and t_day >= crit_d and t_sym >= crit_s, "2": all(m > 0 for m in half_means),
         "3": all(m > 0 for m in half_lift), "5": drop_days > 0 and drop_names > 0, "6": mean([t["net15"] for t in trades]) > 0}
    status = "UNDERPOWERED" if under else ("STRONG" if all(c.values()) else "FAIL")
    return {"status": status, "n": len(trades), "n_h": n_h, "days": len(days), "names": len(syms), "mean_day": mean(dv), "t_day": t_day, "crit_day": crit_d,
            "t_name": t_sym, "crit_name": crit_s, "half_net": half_means, "half_lift": half_lift, "lift": mean(lift), "drop_days": drop_days, "drop_names": drop_names,
            "net15": mean([t["net15"] for t in trades]), "sd": sd, "mde": mde, "c": c}


# ---------- I/O ----------
def sessions():
    days = sorted(p.name for p in SESS.glob("20??-??-??") if SINCE <= p.name <= UNTIL and (p / "discord.jsonl.gz").exists())
    out, prev = [], []
    mism = 0
    for d in days:
        rows, bad = read_alerts(d)
        mism += bad
        if not rows or min(t for t, _ in rows) > at_hm(rows[0][0], (7, 0)):
            prev = rows or prev
            continue
        out.append((d, rows, prev))
        prev = rows
    return out, mism


def stock_names():
    from day_hold import asset_names
    from ticker_filters import is_not_stock
    names = {}
    for _ in range(4):
        names = asset_names() or {}
        if sum(1 for k in names if k != "__cached__") >= 1000:
            break
        time.sleep(5)
    if sum(1 for k in names if k != "__cached__") < 1000:
        raise SystemExit("asset names unavailable: refusing to run without the ETF/ETN exclusion")
    return lambda s: not is_not_stock(s, names.get(s, "") or "")


def fetch_bars():
    import alpaca_api as aa
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from config import load_config_ro
    client = aa.connect_data_client(dict(load_config_ro() or {}))
    is_stock = stock_names()
    OUT.mkdir(parents=True, exist_ok=True)
    ss, mism = sessions()
    print(f"sessions {len(ss)}; alert lines whose `et` disagrees with ts: {mism}")
    for d, rows, prev in ss:
        f = OUT / f"bars_{d}.pkl"
        if f.exists():
            continue
        nd, _ = name_days(d, rows, prev)
        syms = sorted({s for s, _ in nd if is_stock(s)})
        s0 = datetime.fromisoformat(d + "T04:00:00").replace(tzinfo=ET)
        s1 = datetime.fromisoformat(d + "T09:30:00").replace(tzinfo=ET)
        out = {}
        for i in range(0, len(syms), 20):
            try:
                df = client.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[i:i + 20], timeframe=TimeFrame.Minute, start=s0, end=s1, feed=DataFeed.SIP)).df
            except Exception as e:  # noqa: BLE001
                print("ERR", d, str(e)[:100])
                continue
            if df is None or df.empty:
                continue
            for s in df.index.get_level_values(0).unique():
                x = df.xs(s, level=0)
                out[s] = [(t.timestamp(), float(o), float(h), float(lo), float(c), float(v)) for t, o, h, lo, c, v in
                          zip(x.index, x["open"], x["high"], x["low"], x["close"], x["volume"])]
            time.sleep(0.3)
        pickle.dump(out, open(f, "wb"))
    print("bars done")
    return 0


def build(table_for=None):
    """Detect everything from the bar cache. -> (records, drops). records = [{d, sym, t0, rule, idx, fill_raw, bars, ctrl_idx}]."""
    is_stock = stock_names()
    ss, _ = sessions()
    recs, drops = [], {"denominator": 0, "etf": 0, "no_bars": 0, "price": 0, "no_setup_R1": 0, "no_setup_R2": 0, "alert_filters": {}}
    for d, rows, prev in ss:
        f = OUT / f"bars_{d}.pkl"
        if not f.exists():
            continue
        cache = pickle.load(open(f, "rb"))
        nd, dd = name_days(d, rows, prev)
        for k, v in dd.items():
            drops["alert_filters"][k] = drops["alert_filters"].get(k, 0) + v
        for sym, t0 in nd:
            if not is_stock(sym):
                drops["etf"] += 1
                continue
            drops["denominator"] += 1
            bars = sorted(cache.get(sym, []))
            L0, px = l0_and_price(bars, t0)
            if L0 is None:
                drops["no_bars"] += 1
                continue
            if px < MIN_PRICE:
                drops["price"] += 1
                continue
            for rule, det in (("R1", detect_r1(bars, t0, L0)), ("R2", detect_r2(bars, t0))):
                if det is None:
                    drops[f"no_setup_{rule}"] += 1
                    continue
                recs.append({"d": d, "sym": sym, "t0": t0, "rule": rule, "idx": det["idx"], "fill_raw": det["fill_raw"], "bars": bars,
                             "ctrl": control_starts(bars, sym, d, t0, bars[det["idx"]][0])})
    return recs, drops


def needed_minutes(recs):
    need = set()
    for r in recs:
        bars = r["bars"]
        for idx, fr in [(r["idx"], r["fill_raw"])] + [(c, bars[c][1]) for c in r["ctrl"]]:
            need.add((r["d"], r["sym"], int(bars[idx][0] - 60)))
            ex = simulate_exit(bars, idx, fr)
            if ex is not None:
                need.add((r["d"], r["sym"], int(ex[1] - 60)))
    return need


def plan():
    recs, drops = build()
    need = needed_minutes(recs)
    json.dump(sorted(need), open(OUT / "needed.json", "w"))
    print(f"denominator {drops['denominator']}; drops {drops}; detections R1 {sum(1 for r in recs if r['rule'] == 'R1')}, R2 {sum(1 for r in recs if r['rule'] == 'R2')}; quote minutes needed {len(need)}")
    return 0


def fetch_quotes():
    import alpaca_api as aa
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    from config import load_config_ro
    client = aa.connect_data_client(dict(load_config_ro() or {}))
    need = [tuple(x) for x in json.load(open(OUT / "needed.json"))]
    tf = OUT / "spreads.pkl"
    table = pickle.load(open(tf, "rb")) if tf.exists() else {}
    todo = [n for n in need if n not in table]
    print(f"minutes needed {len(need)}, to fetch {len(todo)}")
    for k, (d, sym, m) in enumerate(todo):
        a, b = datetime.fromtimestamp(m, timezone.utc), datetime.fromtimestamp(m + 60, timezone.utc)
        try:
            q = client.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=a, end=b, feed=DataFeed.SIP, limit=10000))
            rows = [(x.timestamp.timestamp(), float(x.bid_price), float(x.ask_price)) for x in (q.data or {}).get(sym, []) if x.bid_price > 0 and x.ask_price > x.bid_price]
        except Exception as e:  # noqa: BLE001
            print("ERR", d, sym, m, str(e)[:80])
            continue
        rows.sort()
        segs = []
        for i, (t, bd, ak) in enumerate(rows):
            end = rows[i + 1][0] if i + 1 < len(rows) else m + 60
            w = max(0.0, min(end, m + 60) - max(t, m))
            if w > 0:
                segs.append(((ak - bd) / ((ak + bd) / 2) * 1e4, w))
        med = None
        if segs:
            segs.sort()
            half, acc = sum(w for _, w in segs) / 2, 0.0
            for s_, w in segs:
                acc += w
                if acc >= half:
                    med = s_
                    break
        table[(d, sym, m)] = (med, len(rows))
        if k % 200 == 0:
            pickle.dump(table, open(tf, "wb"))
        time.sleep(0.2)
    pickle.dump(table, open(tf, "wb"))
    print("quotes done")
    return 0


def report():
    recs, drops = build()
    tf = OUT / "spreads.pkl"
    table_all = pickle.load(open(tf, "rb")) if tf.exists() else {}
    days = sorted({r["d"] for r in recs})
    cut = days[len(days) // 2] if days else ""
    den = max(1, drops["denominator"])
    lines = [f"# Premarket rules on history (prereg docs/studies/premarket_rules_history_prereg.json), written {datetime.now(ET):%Y-%m-%d %H:%M} ET", "",
             f"Denominator (first-alert stock name-days, 05:00-09:15): {drops['denominator']}; ETF/ETN excluded {drops['etf']}; alert-filter drops {drops['alert_filters']}; "
             f"no/too few bars {drops['no_bars']} ({100 * drops['no_bars'] / den:.0f}%); price < $2 {drops['price']}", ""]
    verdicts = {}
    for rule in ("R1", "R2"):
        trades, dropped = [], {}
        for r in recs:
            if r["rule"] != rule:
                continue
            tab = {k[2]: v for k, v in table_all.items() if k[0] == r["d"] and k[1] == r["sym"]}
            n1, why = trade_net(r["bars"], r["idx"], r["fill_raw"], tab, 1.0)
            if n1 is None:
                dropped[why] = dropped.get(why, 0) + 1
                continue
            n15, _ = trade_net(r["bars"], r["idx"], r["fill_raw"], tab, 1.5)
            cs = [trade_net(r["bars"], c, r["bars"][c][1], tab, 1.0)[0] for c in r["ctrl"]]
            cs = [x for x in cs if x is not None]
            trades.append({"d": r["d"], "sym": r["sym"], "net": n1, "net15": n15 if n15 is not None else n1 - 10, "ctrl": (sum(cs) / len(cs)) if cs else None,
                           "peak_ng": False})
        qdrop = dropped.get("no_entry_quotes", 0)
        void = (qdrop / den > 0.25) or (drops["no_bars"] / den > 0.25) or len(days) < 20
        ev = evaluate(trades, cut) if trades else {"status": "NO_TRADES"}
        st_ = "VOID" if void else ev.get("status")
        verdicts[rule] = st_
        lines += [f"## {rule}: detections {sum(1 for r in recs if r['rule'] == rule)}, scored trades {len(trades)}, dropped {dropped}", ""]
        if trades:
            lines += [f"- days {ev['days']}, names {ev['names']}, trades by half {ev['n_h']}; per-trade SD {ev['sd']:.0f} bp, MDE {ev['mde']:.0f} bp",
                      f"- mean per-day net {ev['mean_day']:+.1f} bp; day t {ev['t_day']:+.2f} (crit {ev['crit_day']:.2f}); name-clustered t {ev['t_name']:+.2f} (crit {ev['crit_name']:.2f})",
                      f"- by half net {ev['half_net'][0]:+.1f} / {ev['half_net'][1]:+.1f}; lift vs random minutes {ev['lift']:+.1f} (halves {ev['half_lift'][0]:+.1f} / {ev['half_lift'][1]:+.1f})",
                      f"- drop best 3 days {ev['drop_days']:+.1f}; drop top 3 names {ev['drop_names']:+.1f}; 1.5x costs {ev['net15']:+.1f}; conditions {ev['c']}"]
        lines += [f"**{rule} VERDICT: {st_}**", ""]
    lines += ["A STRONG verdict only earns the paid SIP month as a forward dry run. UNDERPOWERED = no verdict; the forward month would decide. FAIL closes premarket on history."]
    out = "\n".join(lines)
    (ROOT / "docs" / "PREMARKET_RULES_HISTORY.md").write_text(out + "\n")
    print(out)
    return 0


if __name__ == "__main__":
    a = sys.argv[1:] or ["report"]
    fn = {"fetch-bars": fetch_bars, "plan": plan, "fetch-quotes": fetch_quotes, "report": report}.get(a[0])
    raise SystemExit(fn() if fn else 2)
