#!/usr/bin/env python3
"""Price an options-flow test before buying anything: Databento OPRA cost quotes only.

Calls only Databento's free metadata endpoints (symbology.resolve, metadata.get_cost,
metadata.get_billable_size). It never downloads data and never spends credit.

Universe: the warehouse names (ai_reports/warehouse/sip), ranked by median daily dollar volume over its
sessions: the top 300 (the pairs test's set) and every name. Options are requested by parent symbol
(AAPL.OPT = every AAPL option). Window: 09:30-16:00 ET on each warehouse session. Schemas:
  trades  every options trade (price, size, exchange); no side
  tbbo    every trade with the best bid and offer just before it, so a buy at the ask can be told from a sell
  cbbo-1m one consolidated quote per minute per contract (information: a cheaper context file)

  (databento venv) REPO=$PWD python tools/studies/opra_cost.py              # all sessions
  (databento venv) REPO=$PWD python tools/studies/opra_cost.py --days 5     # quick sample, extrapolated
Key: config/secrets.json.databento ({"api_key": ...}), as tools/studies/premarket_depth_probe.py.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

REPO = os.environ.get("REPO") or os.getcwd()
WH = os.path.join(REPO, "ai_reports", "warehouse", "sip")
OUT = os.path.join(REPO, "ai_reports", "opra_cost.json")
ET = ZoneInfo("America/New_York")
DS = "OPRA.PILLAR"
SCHEMAS = ("trades", "tbbo", "cbbo-1m")
TOP = 300
NS = 1_000_000_000
BATCH = 1000


def days_and_ranking() -> tuple[list[str], list[str]]:
    days = sorted(x[:-4] for x in os.listdir(WH) if x.endswith(".npz"))
    dv: dict[str, list[float]] = {}
    for d in days:
        z = np.load(os.path.join(WH, f"{d}.npz"), allow_pickle=False)
        tot = np.bincount(z["sym_idx"], weights=z["c"].astype(np.float64) * z["v"].astype(np.float64),
                          minlength=len(z["syms"]))
        for i, s in enumerate(z["syms"]):
            dv.setdefault(str(s), []).append(float(tot[i]))
    ranked = sorted(dv, key=lambda s: -st.median(dv[s]))
    return days, ranked


def client():
    import databento as db
    return db.Historical(json.load(open(os.path.join(REPO, "config", "secrets.json.databento")))["api_key"])


def _try(f, **k):
    for i in range(5):
        try:
            return f(**k)
        except Exception as e:  # network retries; a 4xx is raised at once
            if i == 4 or str(getattr(e, "http_status", "")).startswith("4"):
                raise
            time.sleep(3 * (i + 1))


def resolvable(c, syms: list[str], day: str) -> list[str]:
    """Underlyings with listed options on one session (parent symbology resolves). One day keeps the
    response small: a parent maps to every one of its contracts."""
    from datetime import date, timedelta
    nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    ok = []
    for i in range(0, len(syms), 200):
        part = [f"{s}.OPT" for s in syms[i:i + 200]]
        r = _try(c.symbology.resolve, dataset=DS, symbols=part, stype_in="parent", stype_out="instrument_id",
                 start_date=day, end_date=nxt)
        missing = set(r.get("not_found") or []) | set(r.get("partial") or [])
        ok += [p for p in part if p not in missing]
    return ok


def window(day: str) -> tuple[int, int]:
    d = datetime.strptime(day, "%Y-%m-%d")
    t0 = d.replace(hour=9, minute=30, tzinfo=ET).timestamp()
    t1 = d.replace(hour=16, minute=0, tzinfo=ET).timestamp()
    return int(t0) * NS, int(t1) * NS


def quote_day(c, day: str, parents: list[str], schema: str) -> tuple[float, int]:
    s, e = window(day)
    usd, size = 0.0, 0
    for i in range(0, len(parents), BATCH):
        part = parents[i:i + BATCH]
        kw = dict(dataset=DS, symbols=part, stype_in="parent", schema=schema, start=s, end=e)
        usd += float(_try(c.metadata.get_cost, **kw))
        size += int(_try(c.metadata.get_billable_size, **kw))
    return usd, size


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="quote only N evenly spaced sessions and extrapolate")
    a = ap.parse_args()
    days, ranked = days_and_ranking()
    if not days:
        raise SystemExit(f"no warehouse sessions under {WH}")
    pick = days if not a.days else [days[round(i * (len(days) - 1) / max(1, a.days - 1))] for i in range(a.days)]
    c = client()
    print(f"warehouse sessions {len(days)} {days[0]}..{days[-1]}; quoting {len(pick)}; names {len(ranked)}", flush=True)
    res: dict = {"sessions": len(days), "quoted": len(pick), "universes": {}}
    for label, syms in (("top300", ranked[:TOP]), ("all", ranked)):
        parents = resolvable(c, syms, days[len(days) // 2])
        print(f"{label}: {len(syms)} names, {len(parents)} with listed options", flush=True)
        row = {"names": len(syms), "with_options": len(parents)}
        for schema in SCHEMAS:
            with ThreadPoolExecutor(8) as ex:
                got = list(ex.map(lambda d, sc=schema: quote_day(c, d, parents, sc), pick))
            usd, size = sum(g[0] for g in got), sum(g[1] for g in got)
            scale = len(days) / len(pick)
            row[schema] = {"usd": round(usd * scale, 2), "gb": round(size * scale / 1e9, 2),
                           "usd_per_session": round(usd / len(pick), 2)}
            print(f"  {schema:8s} ${usd * scale:,.2f} for {len(days)} sessions ({size * scale / 1e9:,.1f} GB; "
                  f"${usd / len(pick):,.2f} a session){' (extrapolated)' if scale > 1 else ''}", flush=True)
        res["universes"][label] = row
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(res, open(OUT, "w"), indent=1)
    print(f"\nwritten {OUT}. Nothing was downloaded and no credit was used.")


if __name__ == "__main__":
    main()
