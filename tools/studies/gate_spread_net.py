#!/usr/bin/env python3
"""Spread-refused names after costs. Spec: docs/studies/gate_counterfactual_prereg.json "amended_4_spread_net"
(information, registered after the screen's gross read and before any quote).

  --fetch   SIP NBBO (data-only account) at the start of each G2 unit's entry minute: the refused name and up to 3
            seeded board names from its control set -> spread_quotes.json (resumable)
  (default) NET DIFF = (refused 10-min return - its spread) - (board return - mean board spread), day-equal
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_counterfactual as G  # noqa: E402

QUOTE_AGE_S, N_BOARD, CAP_DAY = 30.0, 3, "2026-10-03"
OUT = os.path.join(G.OUT_DIR, "spread_quotes.json")


def entry_ts(day: str, minute: int) -> float:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=G.ET)
    return d.replace(hour=minute // 60, minute=minute % 60).timestamp()


def spread_from(quotes: list[tuple], t: float) -> float | None:
    """quotes: (ts, bid, ask, bid_size, ask_size); the last clean one at or before t, <= QUOTE_AGE_S old."""
    best = None
    for q in quotes:
        if q[0] <= t and q[1] > 0 and q[2] > q[1] and q[3] > 0 and q[4] > 0:
            if best is None or q[0] >= best[0]:
                best = q
    if best is None or t - best[0] > QUOTE_AGE_S:
        return None
    return (best[2] - best[1]) / ((best[1] + best[2]) / 2)


def board_pick(day: str, sym: str, ctl: list[str]) -> list[str]:
    rng = random.Random(int(hashlib.sha256(f"gate-spread-board|{day}|{sym}".encode()).hexdigest()[:16], 16))
    return sorted(rng.sample(ctl, min(N_BOARD, len(ctl))))


def net_rows(rows: list[dict], quotes: dict) -> list[dict]:
    out = []
    for r in rows:
        q = quotes.get(r["key"], {})
        rs = q.get(r["sym"])
        bs = [q.get(b) for b in r["board_pick"]]
        bs = [x for x in bs if x is not None]
        if rs is None or not bs:
            continue
        out.append({**r, "refused_spread": rs, "board_spread": statistics.mean(bs),
                    "net_diff": (r["ret"] - rs) - (r["ctrl"] - statistics.mean(bs))})
    return out


def units_with_windows() -> list[dict]:
    u = G.jload("units.json")
    units, board = u["units"], u["board"]
    cache = {}

    def bars_of(day):
        if day not in cache:
            import pickle
            cache[day] = pickle.load(open(os.path.join(G.BAR_DIR, f"{day}.pkl"), "rb"))
        return cache[day]
    rows, _st = G.evaluate({"G2_spread": units["G2_spread"]}, board, bars_of)
    out = []
    for r in rows["G2_spread"]:
        w = G.window(bars_of(r["day"]).get(r["sym"], {}), r["ts"], r["admit_ts"])
        e = w[0]
        ctl = G.board_at(board[r["day"]], r["ts"], r["sym"])
        out.append({**r, "entry_min": e, "board_pick": board_pick(r["day"], r["sym"], ctl),
                    "key": f"{r['day']}|{r['sym']}|{e}"})
    return out


def fetch(rows: list[dict]) -> dict:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockQuotesRequest
    s_ = json.load(open(os.path.join(G.ROOT, "secrets_data.json")))
    cl = StockHistoricalDataClient(s_["alpaca_data_api_key"], s_["alpaca_data_secret_key"])
    done = json.load(open(OUT)) if os.path.exists(OUT) else {}
    for n, r in enumerate(rows):
        if r["key"] in done:
            continue
        t = entry_ts(r["day"], r["entry_min"])
        syms = [r["sym"]] + r["board_pick"]
        for attempt in range(5):
            try:
                got = cl.get_stock_quotes(StockQuotesRequest(
                    symbol_or_symbols=syms, start=datetime.fromtimestamp(t - QUOTE_AGE_S, timezone.utc),
                    end=datetime.fromtimestamp(t, timezone.utc), feed=DataFeed.SIP)).data or {}
                break
            except Exception as e:  # noqa: BLE001
                print(f"  retry {attempt + 1}: {str(e)[:90]}", flush=True)
                time.sleep(10 * (attempt + 1))
        else:
            raise SystemExit(f"quote fetch failed at {r['key']}")
        done[r["key"]] = {s: spread_from([(q.timestamp.timestamp(), float(q.bid_price or 0), float(q.ask_price or 0),
                                           float(q.bid_size or 0), float(q.ask_size or 0)) for q in got.get(s, [])], t)
                          for s in syms}
        if n % 100 == 0:
            json.dump(done, open(OUT + ".tmp", "w"))
            os.replace(OUT + ".tmp", OUT)
            print(f"  {n + 1}/{len(rows)}", flush=True)
        time.sleep(0.45)
    json.dump(done, open(OUT + ".tmp", "w"))
    os.replace(OUT + ".tmp", OUT)
    return done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true")
    a = ap.parse_args()
    rows = units_with_windows()
    if a.fetch:
        fetch(rows)
        return
    quotes = json.load(open(OUT))
    nr = net_rows(rows, quotes)
    res = {"units": len(rows), "with_quotes": len(nr),
           "missing_refused": sum(1 for r in rows if quotes.get(r["key"], {}).get(r["sym"]) is None),
           "missing_board_all": sum(1 for r in rows if not any(quotes.get(r["key"], {}).get(b) is not None
                                                                for b in r["board_pick"]))}
    for lab, part in (("all", nr), ("from_10_03", [r for r in nr if r["day"] >= CAP_DAY]),
                      ("before_10_03", [r for r in nr if r["day"] < CAP_DAY])):
        res[lab] = {"net_diff": G.day_stats(part, "net_diff"), "gross_diff": G.day_stats(part, "diff"),
                    "refused_spread_median": statistics.median([r["refused_spread"] for r in part]) if part else None,
                    "board_spread_median": statistics.median([r["board_spread"] for r in part]) if part else None}
    s = res["from_10_03"]["net_diff"]
    res["reading"] = ("net lead" if s["mean"] is not None and s["t"] is not None and s["mean"] >= 0.0005 and s["t"] >= 2
                      else "no net case")
    G.write("spread_net.json", res)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
