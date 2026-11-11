#!/usr/bin/env python3
"""crypto_carry.py - crypto funding-rate carry, backtested on public history (docs/studies/crypto_funding_carry_prereg.json).

The trade: hold the coin, short the same dollar amount of its perpetual future, collect the funding the short is paid.
The two legs cancel the coin's price move; what is left is funding plus the small move of the future against the coin
(the basis). A BACKTEST ONLY: no account, no order, no money.

Source: Deribit's public API (BTC-PERPETUAL, ETH-PERPETUAL). Binance and Bybit, which have the longest histories, refuse
connections from the United States (HTTP 451 / 403 on 11/11), so they are not used and not worked around.
Deribit pays funding continuously; its history is hourly (interest_1h = funding paid over that hour, positive = the
short receives). Hours are summed into 8-hour periods so the written rules (per 8 h) apply unchanged.

    python3 tools/studies/crypto_carry.py fetch      # public JSON -> ai_reports/crypto_carry/ (resumable)
    python3 tools/studies/crypto_carry.py            # the report
"""
from __future__ import annotations

import json
import math
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "ai_reports" / "crypto_carry"
API = "https://www.deribit.com/api/v2/public/"
COINS = {"BTC": "BTC-PERPETUAL", "ETH": "ETH-PERPETUAL"}
START = datetime(2019, 1, 1, tzinfo=timezone.utc)
HOUR, DAY = 3_600_000, 86_400_000
PER_YEAR = 3 * 365                          # 8-hour periods in a year
CAPITAL = 1.5                               # per 1.0 of notional: the coin plus 50% margin for the short (as pre-registered)
ROUND_TRIP = 0.0030                         # 0.10% a side on spot + 0.05% a side on the perpetual
SWITCH_ON, SWITCH_N = 0.0001, 21            # on while the mean funding of the last 21 periods is above +0.01% per 8 h
CASH = {2019: 0.021, 2020: 0.005, 2021: 0.001, 2022: 0.020, 2023: 0.050, 2024: 0.050, 2025: 0.040, 2026: 0.040}   # approximate


def _get(method: str, **params) -> dict:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{API}{method}?{q}", headers={"User-Agent": "research-script"})
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)["result"]
        except Exception:  # noqa: BLE001
            time.sleep(2 * (attempt + 1))
    raise SystemExit(f"could not fetch {method} {params}")


def fetch() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    now = int(time.time() * 1000) // HOUR * HOUR
    for coin, inst in COINS.items():
        f = OUT / f"{coin}.json"
        d = json.loads(f.read_text()) if f.exists() else {"funding": {}, "perp": {}}
        t = max([int(k) for k in d["funding"]] + [int(START.timestamp() * 1000)])
        while t < now:
            end = min(t + 20 * DAY, now)
            for r in _get("get_funding_rate_history", instrument_name=inst, start_timestamp=t, end_timestamp=end):
                d["funding"][str(r["timestamp"])] = [r["interest_1h"], r["index_price"]]
            c = _get("get_tradingview_chart_data", instrument_name=inst, start_timestamp=t, end_timestamp=end, resolution=60)
            for ts, close in zip(c.get("ticks", []), c.get("close", [])):
                d["perp"][str(ts)] = close
            t = end
            time.sleep(0.15)
        f.write_text(json.dumps(d))
        ks = sorted(int(k) for k in d["funding"])
        print(f"{coin}: {len(ks)} hourly funding rows, {datetime.fromtimestamp(ks[0] / 1000, timezone.utc):%Y-%m-%d} to "
              f"{datetime.fromtimestamp(ks[-1] / 1000, timezone.utc):%Y-%m-%d}; {len(d['perp'])} perpetual closes", flush=True)


# ---------- pure ----------
def periods(funding: dict, perp: dict) -> list[dict]:
    """Hourly rows -> 8-hour periods ending 00:00 / 08:00 / 16:00 UTC, complete periods only:
    {t, funding (sum of the 8 hours), index (coin price at the end), perp (future's price at the end), hi (largest index rise inside)}."""
    hours = sorted(int(k) for k in funding)
    out, bucket = [], []
    for ts in hours:
        bucket.append(ts)
        if ts % (8 * HOUR) == 0:
            # Deribit chart bars are keyed by their START: the bar keyed ts-1h closes at ts, the same instant as the index price
            # stamped ts. Using the bar keyed ts (which closes an hour later) set the coin and the future an hour apart and made a
            # market-neutral position look like it moved 10% in eight hours (found 11/11 before any result was reported).
            if len(bucket) == 8 and bucket[-1] - bucket[0] == 7 * HOUR and str(ts - HOUR) in perp:
                idx = [funding[str(x)][1] for x in bucket]
                out.append({"t": ts, "funding": sum(funding[str(x)][0] for x in bucket), "index": idx[-1], "perp": perp[str(ts - HOUR)]})
            bucket = []
    return out


def carry_series(p: list[dict], switched: bool = False) -> list[dict]:
    """Per-period return on NOTIONAL for long coin / short perpetual: funding + change in (coin - future) / coin.
    switched=True: in the trade only while the mean funding of the previous SWITCH_N periods is above SWITCH_ON; a round trip is
    charged each time it turns on. Always-on is charged one round trip per year, spread evenly."""
    out, on = [], not switched
    for i in range(1, len(p)):
        basis_now, basis_prev = (p[i]["index"] - p[i]["perp"]) / p[i]["index"], (p[i - 1]["index"] - p[i - 1]["perp"]) / p[i - 1]["index"]
        gross = p[i]["funding"] + (basis_now - basis_prev)
        cost = 0.0
        if switched:
            want = i >= SWITCH_N and sum(x["funding"] for x in p[i - SWITCH_N:i]) / SWITCH_N > SWITCH_ON
            if want and not on:
                cost = ROUND_TRIP
            on = want
        else:
            cost = ROUND_TRIP / PER_YEAR
        out.append({"t": p[i]["t"], "ret": (gross - cost) if on else 0.0, "funding": p[i]["funding"], "on": on,
                    "rise": p[i]["index"] / p[i - 1]["index"] - 1})
    return out


def blend(a: list[dict], b: list[dict]) -> list[dict]:
    """Half of each coin, on the periods both have."""
    bt = {x["t"]: x for x in b}
    return [{"t": x["t"], "ret": 0.5 * x["ret"] + 0.5 * bt[x["t"]]["ret"], "funding": 0.5 * x["funding"] + 0.5 * bt[x["t"]]["funding"],
             "on": x["on"] or bt[x["t"]]["on"], "rise": max(x["rise"], bt[x["t"]]["rise"])} for x in a if x["t"] in bt]


def stats(s: list[dict], capital: float = CAPITAL) -> dict:
    r = [x["ret"] / capital for x in s]
    yrs = len(r) / PER_YEAR
    eq, peak, dd, curve = 1.0, 1.0, 0.0, []
    for v in r:
        eq *= 1 + v
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
        curve.append(eq)
    worst12 = min((curve[i] / curve[i - PER_YEAR] - 1 for i in range(PER_YEAR, len(curve))), default=float("nan"))
    by_year: dict[int, float] = {}
    for x, v in zip(s, r):
        y = datetime.fromtimestamp(x["t"] / 1000, timezone.utc).year
        by_year[y] = (1 + by_year.get(y, 0.0)) * (1 + v) - 1
    neg, run, longest = 0, 0, 0
    for x in s:
        if x["funding"] < 0:
            neg += 1
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    h = len(r) // 2
    ann = lambda v: (math.prod(1 + a for a in v) ** (PER_YEAR / len(v)) - 1) if v else float("nan")   # noqa: E731
    mean = sum(r) / len(r)
    sd = math.sqrt(sum((a - mean) ** 2 for a in r) / (len(r) - 1))
    cash = sum(CASH.get(datetime.fromtimestamp(x["t"] / 1000, timezone.utc).year, 0.04) for x in s) / len(s)
    return {"years": yrs, "annual": ann(r), "halves": (ann(r[:h]), ann(r[h:])), "worst12": worst12, "maxdd": dd, "by_year": by_year,
            "neg_share": neg / len(s), "longest_neg_days": longest / 3, "max_rise": max(x["rise"] for x in s), "sharpe": mean / sd * math.sqrt(PER_YEAR) if sd else float("nan"),
            "on_share": sum(x["on"] for x in s) / len(s), "cash": cash, "funding_annual": sum(x["funding"] for x in s) / len(s) * PER_YEAR}


def verdict(st: dict) -> tuple[str, list[str]]:
    c = {"net return > 0 in both halves": st["halves"][0] > 0 and st["halves"][1] > 0,
         "at least 2 points a year above cash": st["annual"] - st["cash"] >= 0.02,
         "worst 12 months no worse than -5%": st["worst12"] >= -0.05,
         "largest single-period rise under 40%": st["max_rise"] < 0.40}
    bad = [k for k, v in c.items() if not v]
    return ("PASS" if not bad else "FAIL"), bad


def show(label: str, st: dict) -> str:
    yrs = "  ".join(f"{y}: {100 * v:+.1f}%" for y, v in sorted(st["by_year"].items()))
    return (f"{label}\n    net per year on capital {100 * st['annual']:+.2f}% (halves {100 * st['halves'][0]:+.2f}% / {100 * st['halves'][1]:+.2f}%); "
            f"cash paid about {100 * st['cash']:.1f}% a year over the same span; raw funding {100 * st['funding_annual']:+.1f}% of notional a year\n"
            f"    worst 12 months {100 * st['worst12']:+.2f}%, worst drawdown {100 * st['maxdd']:.2f}%, Sharpe {st['sharpe']:.1f}; "
            f"funding negative in {100 * st['neg_share']:.0f}% of periods, longest negative stretch {st['longest_neg_days']:.0f} days; "
            f"in the trade {100 * st['on_share']:.0f}% of the time; largest 8-hour rise in the coin {100 * st['max_rise']:.0f}%\n    by year: {yrs}")


def main(argv=None) -> int:
    a = (argv if argv is not None else sys.argv[1:])
    if a[:1] == ["fetch"]:
        fetch()
        return 0
    data = {}
    for coin in COINS:
        f = OUT / f"{coin}.json"
        if not f.exists():
            print("no data: run `python3 tools/studies/crypto_carry.py fetch` first")
            return 1
        d = json.loads(f.read_text())
        data[coin] = periods(d["funding"], d["perp"])
    first = max(p[0]["t"] for p in data.values())
    last = min(p[-1]["t"] for p in data.values())
    print(f"Deribit BTC and ETH perpetuals, 8-hour periods, {datetime.fromtimestamp(first / 1000, timezone.utc):%Y-%m-%d} to "
          f"{datetime.fromtimestamp(last / 1000, timezone.utc):%Y-%m-%d}. Returns are on capital of {CAPITAL} per 1.0 of position, after fees.\n")
    always = {c: carry_series(p) for c, p in data.items()}
    switch = {c: carry_series(p, switched=True) for c, p in data.items()}
    both = stats(blend(always["BTC"], always["ETH"]))
    v, bad = verdict(both)
    print(show("PRIMARY: ALWAYS-ON, half BTC and half ETH", both))
    print(f"    VERDICT: {v}" + (f"  (failed: {'; '.join(bad)})" if bad else "") + "\n")
    for c in COINS:
        print(show(f"always-on, {c} only", stats(always[c])) + "\n")
    sw = stats(blend(switch["BTC"], switch["ETH"]))
    print(show("SECONDARY: SWITCHED (on only while the last 7 days' funding averages above +0.01% per 8 h), half and half", sw) + "\n")
    yr = both["annual"] * CAPITAL
    print(f"WHAT THE HISTORY CANNOT SHOW: the exchange failing. If the capital held on one exchange were lost, it would erase about "
          f"{1 / max(1e-9, both['annual']):.0f} years of this carry on that capital (carry {100 * both['annual']:.1f}% a year; {100 * yr:.1f}% on the position itself).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
