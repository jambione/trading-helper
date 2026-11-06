#!/usr/bin/env python3
"""Weekend item 8 (docs/studies/spread_compression_prereg.json, amended after skeptic round 1). Read-only; free historical SIP quotes.

At each desk entry: is the real (SIP) quoted spread in the last minute tighter than in the four minutes before it? COMPRESSED = ratio <= 0.70
(time-weighted median spreads, regular session only, entries from 09:36, >= 20 quotes per window). Outcome = sell fill vs buy fill less 3 bp.
Compared within the same day AND clock hour. Live use would need REAL-TIME quotes (paid SIP), so a pass is shelved until SIP.

    .venv/bin/python tools/studies/spread_compression.py fetch    # mini; caches quotes per entry (weekend / after 16:30)
    .venv/bin/python tools/studies/spread_compression.py report
"""
from __future__ import annotations

import json
import math
import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
ET = ZoneInfo("America/New_York")
CACHE = ROOT / "ai_reports" / "spread_compression"
SINCE, UNTIL = "2026-09-16", "2026-11-05"
CUT, MIN_Q, MIN_N_HALF, COST = 0.70, 20, 60, 3.0


def tw_median(quotes, a, b):
    """Time-weighted median spread (bp of mid) over [a, b). quotes = [(ts, bid, ask)] sorted; each quote holds until the next. None if < MIN_Q usable."""
    segs = []
    use = [(t, bd, ak) for t, bd, ak in quotes if bd > 0 and ak > bd]
    inw = [q for q in use if a <= q[0] < b]
    if len(inw) < MIN_Q:
        return None
    for i, (t, bd, ak) in enumerate(inw):
        end = inw[i + 1][0] if i + 1 < len(inw) else b
        w = max(0.0, min(end, b) - max(t, a))
        if w > 0:
            segs.append(((ak - bd) / ((ak + bd) / 2) * 1e4, w))
    if not segs:
        return None
    segs.sort()
    half = sum(w for _, w in segs) / 2
    acc = 0.0
    for s, w in segs:
        acc += w
        if acc >= half:
            return s
    return segs[-1][0]


def ratio(quotes, t_dec):
    r = tw_median(quotes, t_dec - 60, t_dec)
    p = tw_median(quotes, t_dec - 300, t_dec - 60)
    if r is None or p is None or p <= 0:
        return None
    return r / p


def load_trades():
    from early_path import trades_since
    tr, unmatched = trades_since(SINCE)
    return [t for t in tr if t["day"] <= UNTIL], unmatched


def t_dec_of(t):
    return min(t["fill_ts"], t.get("entry_ts") or t["fill_ts"])


def fetch():
    import alpaca_api as aa
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    from config import load_config_ro
    client = aa.connect_data_client(dict(load_config_ro() or {}))
    CACHE.mkdir(parents=True, exist_ok=True)
    tr, _ = load_trades()
    done = 0
    for t in tr:
        f = CACHE / f"{t['day']}_{t['sym']}_{int(t['fill_ts'])}.pkl"
        if f.exists():
            continue
        td = t_dec_of(t)
        try:
            q = client.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=t["sym"], start=datetime.fromtimestamp(td - 301, timezone.utc),
                                                           end=datetime.fromtimestamp(td + 1, timezone.utc), feed=DataFeed.SIP, limit=10000))
            rows = [(x.timestamp.timestamp(), float(x.bid_price), float(x.ask_price)) for x in (q.data or {}).get(t["sym"], [])]
        except Exception as e:  # noqa: BLE001
            print("ERR", t["day"], t["sym"], str(e)[:80])
            continue
        pickle.dump(sorted(rows), open(f, "wb"))
        done += 1
        time.sleep(0.15)
    print("fetched", done, "of", len(tr))
    return 0


def tstat(v):
    n = len(v)
    if n < 3:
        return float("nan")
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd else float("nan")


def evaluate(rows):
    """rows: [{d, hr, comp (bool), bp}]. Within-day-and-clock-hour pairing. Pure."""
    cells = {}
    for r in rows:
        cells.setdefault((r["d"], r["hr"]), {"c": [], "o": []})["c" if r["comp"] else "o"].append(r["bp"])
    day = {}
    for (d, _), v in cells.items():
        if v["c"] and v["o"]:
            day.setdefault(d, []).append(sum(v["c"]) / len(v["c"]) - sum(v["o"]) / len(v["o"]))
    dd = {d: sum(x) / len(x) for d, x in day.items()}
    days = sorted(dd)
    mid = sorted({r["d"] for r in rows})[len({r["d"] for r in rows}) // 2] if rows else ""
    h1 = [dd[d] for d in days if d < mid]
    h2 = [dd[d] for d in days if d >= mid]
    comp = [r for r in rows if r["comp"]]
    c1 = [r["bp"] for r in comp if r["d"] < mid]
    c2 = [r["bp"] for r in comp if r["d"] >= mid]
    mean = sum(dd.values()) / max(1, len(dd))
    t = tstat(list(dd.values()))
    n1, n2 = len(c1), len(c2)
    under = n1 < MIN_N_HALF or n2 < MIN_N_HALF
    ok = (not under and mean > 0 and t >= 2.0 and h1 and h2 and sum(h1) / len(h1) > 0 and sum(h2) / len(h2) > 0 and c1 and c2 and sum(c1) / n1 > 0 and sum(c2) / n2 > 0)
    return {"days": len(dd), "mean": mean, "t": t, "h": (sum(h1) / len(h1) if h1 else None, sum(h2) / len(h2) if h2 else None), "n_comp": (n1, n2),
            "comp_net": (sum(c1) / n1 if n1 else None, sum(c2) / n2 if n2 else None), "share": len(comp) / max(1, len(rows)), "under": under, "pass": bool(ok),
            "ng_comp": sum(r.get("ng", False) for r in comp) / max(1, len(comp)), "ng_other": sum(r.get("ng", False) for r in rows if not r["comp"]) / max(1, len(rows) - len(comp))}


def sell_fills():
    out = {}
    for p in sorted((ROOT / "ai_reports" / "fills").glob("20??-??-??.jsonl")):
        if p.stem < SINCE or p.stem > UNTIL:
            continue
        for line in p.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("event") == "fill" and r.get("side") == "sell" and r.get("status") == "filled" and r.get("filled_at") and r.get("filled_avg_price"):
                ts = datetime.fromisoformat(str(r["filled_at"]).replace("Z", "+00:00")).timestamp()
                out.setdefault((p.stem, r["symbol"]), []).append((ts, float(r["filled_avg_price"])))
    return out


def report():
    tr, unmatched = load_trades()
    sf = sell_fills()
    rows, dropped, nosell, noq, early = [], 0, 0, 0, 0
    for t in tr:
        td = t_dec_of(t)
        d = datetime.fromtimestamp(td, ET)
        if (d.hour, d.minute) < (9, 36):
            early += 1
            continue
        f = CACHE / f"{t['day']}_{t['sym']}_{int(t['fill_ts'])}.pkl"
        if not f.exists():
            noq += 1
            continue
        r = ratio(pickle.load(open(f, "rb")), td)
        if r is None:
            dropped += 1
            continue
        sells = [(ts, px) for ts, px in sf.get((t["day"], t["sym"]), []) if ts > t["fill_ts"]]
        if not sells:
            nosell += 1
            continue
        px = min(sells)[1]
        rows.append({"d": t["day"], "hr": d.hour, "comp": r <= CUT, "bp": (px / t["entry"] - 1) * 1e4 - COST, "ng": t["never_green"]})
    total = len(tr) + unmatched
    void = unmatched > 0.10 * total or (noq + dropped) > 0.10 * len(tr) or nosell > 0.05 * len(tr)
    ev = evaluate(rows)
    f2 = lambda x: "-" if x is None else f"{x:+.1f}"  # noqa: E731
    verdict = "VOID" if void else ("UNDERPOWERED" if ev["under"] else ("PASS (shelved until real-time SIP quotes)" if ev["pass"] else "FAIL / not shown"))
    out = [f"# Spread compression at the entry (prereg docs/studies/spread_compression_prereg.json), written {datetime.now(ET):%Y-%m-%d %H:%M} ET", "",
           f"Entries {SINCE}..{UNTIL}: {len(tr)} matched, {unmatched} outcomes without a fill row; before 09:36: {early}; quotes not cached: {noq}; not measurable (< {MIN_Q} quotes): {dropped}; no sell fill: {nosell}. {'**VOID: more than the allowed share dropped, not scored.**' if void else ''}", "",
           f"Scored entries {len(rows)}; COMPRESSED share {100 * ev['share']:.0f}%; compressed by half {ev['n_comp']}",
           f"Day-and-hour paired difference (compressed minus other): {ev['mean']:+.1f} bp, t {ev['t']:+.2f}, halves {f2(ev['h'][0])} / {f2(ev['h'][1])}",
           f"Compressed mean net by half: {f2(ev['comp_net'][0])} / {f2(ev['comp_net'][1])} bp; never-green share compressed {100 * ev['ng_comp']:.0f}% vs other {100 * ev['ng_other']:.0f}%", "",
           f"**VERDICT: {verdict}**", ""]
    (ROOT / "docs" / "SPREAD_COMPRESSION.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    a = sys.argv[1:] or ["report"]
    raise SystemExit(fetch() if a[0] == "fetch" else report())
