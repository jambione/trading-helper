#!/usr/bin/env python3
"""index_flows.py — S&P 500 additions/deletions: drift into the index-fund close, reversal after it, and the
deletion rebound. Pre-registered in docs/studies/index_flows_prereg.json.

Runs on the mini (panel + Alpaca keys). Inputs: an events JSON (from the Wikipedia change table) and
/tmp/lh/panel.npz. Announcement times come from Alpaca news, cached next to the events file.

usage: .venv/bin/python tools/studies/index_flows.py EVENTS.json [PANEL.npz]
"""
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
ET = ZoneInfo("America/New_York")
ANN = re.compile(r"S&P ?500", re.I)
VERB = re.compile(r"join|replac|added|addition|add |inclu|remov|delet|drop", re.I)
COST = {"add": 10.0 + 4.0, "rem": 20.0 + 4.0}    # bp round trip: half-spread allowance + 2 bp per auction leg
PARTS = [("tune", "2016-01-01", "2020-12-31"), ("validate", "2021-01-01", "2023-12-31"),
         ("holdout", "2024-01-01", "2026-12-31")]


def fetch_news(sym, eff, cache):
    key = f"{sym}|{eff}"
    if key in cache:
        return cache[key]
    import requests
    from config import load_config
    c = load_config() or {}
    hdr = {"APCA-API-KEY-ID": c.get("api_key"), "APCA-API-SECRET-KEY": c.get("secret_key")}
    e = date.fromisoformat(eff)
    params = {"symbols": sym, "start": f"{e - timedelta(days=45)}T00:00:00Z", "end": f"{e}T23:59:59Z",
              "limit": 50, "sort": "asc", "include_content": "false"}
    hits, tok = [], None
    for _ in range(20):
        if tok:
            params["page_token"] = tok
        for attempt in range(4):
            try:
                r = requests.get("https://data.alpaca.markets/v1beta1/news", params=params, headers=hdr, timeout=30)
                r.raise_for_status()
                j = r.json()
                break
            except Exception as ex:  # noqa: BLE001
                print(f"  news {sym} {eff}: {str(ex)[:100]} (try {attempt + 1})", flush=True)
                time.sleep(5)
        else:
            cache[key] = None            # unknown, not "no headline"
            return None
        time.sleep(0.4)
        hits += [(n["created_at"], n["headline"]) for n in j.get("news", [])
                 if ANN.search(n["headline"]) and VERB.search(n["headline"])]
        if hits:
            break                        # sorted ascending: the first page with a hit has the earliest one
        tok = j.get("next_page_token")
        if not tok:
            break
    cache[key] = hits[0] if hits else []
    return cache[key]


def entry_after(ts_utc, dates):
    """Index of the first session whose 09:30 ET open is after ts_utc."""
    a = datetime.fromisoformat(ts_utc.replace("Z", "+00:00")).astimezone(ET)
    for i, d in enumerate(dates):
        if datetime.combine(d, datetime.min.time(), ET).replace(hour=9, minute=30) > a:
            return i
    return None


def tstat(a):
    a = np.asarray(a, float)
    return a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if len(a) > 2 else float("nan")


def fmt(v):
    return f"n={len(v):3d} mean {np.mean(v):+7.1f} bp (t {tstat(v):+.2f}) median {np.median(v):+6.1f} win {np.mean(np.array(v) > 0):.0%}" if len(v) else "n=  0"


def main():
    evf = sys.argv[1]
    P = np.load(sys.argv[2] if len(sys.argv) > 2 else "/tmp/lh/panel.npz", allow_pickle=True)
    dates = [date.fromisoformat(str(x)[:10]) for x in P["dates"]]
    syms = {str(s): j for j, s in enumerate(P["syms"])}
    o, c, v = P["o"], P["c"], P["v"]
    so, sc = P["spy_o"], P["spy_c"]
    cache_p = os.path.join(os.path.dirname(os.path.abspath(evf)), "index_news_cache.json")
    cache = json.load(open(cache_p)) if os.path.exists(cache_p) else {}
    events = [e for e in json.load(open(evf)) if "2016-01-01" <= e["eff"] <= dates[-1].isoformat()]
    rows, drops = [], {"no_symbol": 0, "no_C": 0, "no_prices": 0, "no_volume_spike": 0}
    for e in events:
        E = date.fromisoformat(e["eff"])
        Ci = max((i for i, d in enumerate(dates) if d < E), default=None)
        if Ci is None or Ci < 25:
            drops["no_C"] += 1
            continue
        for side in ("add", "rem"):
            s = e[side]
            if not s:
                continue
            j = syms.get(s)
            if j is None:
                drops["no_symbol"] += 1
                continue
            need = Ci + (20 if side == "add" else 0)
            if need >= len(dates) or not np.isfinite(c[Ci, j]) or not np.isfinite(c[min(need, len(dates) - 1), j]):
                drops["no_prices"] += 1
                continue
            med = np.nanmedian(v[Ci - 20:Ci, j])
            if not (np.isfinite(med) and med > 0 and v[Ci, j] >= 3 * med):
                drops["no_volume_spike"] += 1
                continue
            r = {"eff": e["eff"], "side": side, "sym": s, "C": dates[Ci].isoformat(), "cost": COST[side]}
            n = fetch_news(s, e["eff"], cache)
            if n:
                k = entry_after(n[0], dates)
                r["A"], r["headline"] = n[0], n[1][:100]
                if k is not None and k <= Ci and np.isfinite(o[k, j]):
                    leg = (c[Ci, j] / o[k, j] - 1) - (sc[Ci] / so[k] - 1)
                    r["drift_xs"] = leg * 1e4                     # long, entry_after_A open -> C close, vs SPY
                    if k >= 1 and np.isfinite(c[k - 1, j]):
                        r["jump_xs"] = ((o[k, j] / c[k - 1, j] - 1) - (so[k] / sc[k - 1] - 1)) * 1e4
                    r["entry"] = dates[k].isoformat()
            for h in (5, 20):
                if Ci + h < len(dates) and np.isfinite(c[Ci + h, j]):
                    r[f"post{h}_xs"] = ((c[Ci + h, j] / c[Ci, j] - 1) - (sc[Ci + h] / sc[Ci] - 1)) * 1e4
            rows.append(r)
        json.dump(cache, open(cache_p, "w"))
    out = os.path.join(os.path.dirname(os.path.abspath(evf)), "index_flows_rows.json")
    json.dump(rows, open(out, "w"), indent=0)

    adds = [r for r in rows if r["side"] == "add"]; rems = [r for r in rows if r["side"] == "rem"]
    print(f"events since 2016: {len(events)}; kept adds {len(adds)}, deletions {len(rems)}; dropped {drops}")
    print(f"announcement headline found: adds {sum('A' in r for r in adds)}/{len(adds)}, "
          f"deletions {sum('A' in r for r in rems)}/{len(rems)}; drift computable: adds {sum('drift_xs' in r for r in adds)}")
    lag = [(date.fromisoformat(r["C"]) - date.fromisoformat(r["entry"])).days for r in adds if "entry" in r]
    if lag:
        print(f"calendar days from entry to C: median {np.median(lag):.0f}, range {min(lag)}..{max(lag)}")
    W = [("W1 add drift (long, entry->C)", adds, "drift_xs", +1),
         ("W2 add reversal (short, C->C+5)", adds, "post5_xs", -1),
         ("W3 deletion rebound (long, C->C+5)", rems, "post5_xs", +1)]
    I = [("info W2 to C+20 (short)", adds, "post20_xs", -1), ("info W3 to C+20 (long)", rems, "post20_xs", +1),
         ("info W4 deletion drift (short, entry->C)", rems, "drift_xs", -1),
         ("info add announcement jump (close->entry open, gross)", adds, "jump_xs", +1),
         ("info deletion announcement jump (gross)", rems, "jump_xs", +1)]
    for title, grp, key, sgn in W + I:
        print(f"\n=== {title}  (net of {grp[0]['cost'] if grp else 0:.0f} bp)" if not title.startswith("info") or "jump" not in title
              else f"\n=== {title}")
        pooled = []
        for name, lo, hi in PARTS:
            gross = [sgn * r[key] for r in grp if key in r and lo <= r["eff"] <= hi]
            net = [g - (0 if "jump" in title else r_cost) for g, r_cost in
                   zip(gross, [r["cost"] for r in grp if key in r and lo <= r["eff"] <= hi])]
            print(f"  {name:8s} net {fmt(net)}   gross mean {np.mean(gross) if gross else float('nan'):+7.1f}")
            if name != "holdout":
                pooled += net
        print(f"  tune+validate pooled: {fmt(pooled)}")


if __name__ == "__main__":
    main()
