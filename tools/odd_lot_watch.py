#!/usr/bin/env python3
"""odd_lot_watch.py - a list of recent company buyback (tender) offers that mention odd lots, for the operator to check
at his own broker (operator 11/15: "Yes, build it"). Running list row 130 has the background.

Source: the SEC's full-text search (efts.sec.gov), issuer tender offer filings (Schedule TO-I) that contain "odd lot",
filed in the last 60 days. Only companies with a stock ticker are kept (most hits are private funds you cannot buy).
For each: the filing date, a link to the filing, and the last close from Alpaca. The offer price, the deadline and
whether odd lots really get priority are in the filing itself, which this tool cannot read (the SEC's document site
refuses clients that do not give a contact), so each line says to check the filing. NOTHING HERE PLACES AN ORDER.

    .venv/bin/python tools/odd_lot_watch.py           # refresh ~/pick_ledgers/ODD_LOTS.txt
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import auto_picker as A  # noqa: E402
import pick_ledger as P  # noqa: E402

ET = A.ET
UA = "trading-helper research"
DAYS = 60
OUT = A.HOME / "ODD_LOTS.txt"


def parse_name(display: str) -> tuple[str, list[str], str]:
    """'PLAYSTUDIOS, Inc.  (MYPS)  (CIK 0001823878)' -> ('PLAYSTUDIOS, Inc.', ['MYPS'], '0001823878').
    A fund with no ticker gives an empty list."""
    cik = re.search(r"\(CIK (\d+)\)", display)
    groups = re.findall(r"\(([^()]*)\)", display)
    tick = [t.strip() for g in groups if not g.startswith("CIK") for t in g.split(",") if t.strip()]
    name = re.split(r"\s+\(", display, maxsplit=1)[0].strip()
    return name, tick, cik.group(1) if cik else ""


def common_ticker(ticks: list[str]) -> str | None:
    """The plain common-stock ticker: letters only, 1-5 long (drops preferreds like PRIF-PD and warrants like SQFTW when
    a shorter base symbol is listed)."""
    plain = [t for t in ticks if re.fullmatch(r"[A-Z]{1,5}", t)]
    if not plain:
        return None
    return min(plain, key=len)


def filing_url(cik: str, adsh: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}/"


def search(start: str, end: str) -> list[dict]:
    out, seen = [], set()
    for frm in range(0, 1000, 100):
        q = urllib.parse.urlencode({"q": '"odd lot"', "forms": "SC TO-I", "dateRange": "custom", "startdt": start, "enddt": end, "from": frm})
        req = urllib.request.Request(f"https://efts.sec.gov/LATEST/search-index?{q}", headers={"User-Agent": UA})
        hits = json.load(urllib.request.urlopen(req, timeout=30))["hits"]["hits"]
        for h in hits:
            s = h["_source"]
            if s.get("form") != "SC TO-I" or s["adsh"] in seen:
                continue
            seen.add(s["adsh"])
            out.append({"adsh": s["adsh"], "filed": s["file_date"], "display": (s.get("display_names") or [""])[0]})
        if len(hits) < 100:
            break
        time.sleep(0.3)
    return out


def deals(rows: list[dict]) -> list[dict]:
    """Listed companies only, newest first, one line per company (its latest offer)."""
    best: dict[str, dict] = {}
    for r in rows:
        name, ticks, cik = parse_name(r["display"])
        t = common_ticker(ticks)
        if not t:
            continue
        cur = {"ticker": t, "name": name, "filed": r["filed"], "url": filing_url(cik, r["adsh"]) if cik else ""}
        if t not in best or cur["filed"] > best[t]["filed"]:
            best[t] = cur
    return sorted(best.values(), key=lambda d: (d["filed"], d["ticker"]), reverse=True)


def render(ds: list[dict], prices: dict[str, float], today: str, known: set[str]) -> str:
    out = [f"Odd-lot tender offers: company buyback offers filed in the last {DAYS} days that mention odd lots. As of {today}.",
           "Listed stocks only. Read each filing for the offer price, the deadline, and whether odd lots (99 shares or fewer) really get",
           "priority. Most offers stay open about 20 business days, so older lines may have closed. Your broker's tender fee decides",
           "whether a deal is worth it (Alpaca charges $100 per tender). Information only: nothing is bought or tendered.", ""]
    if not ds:
        out.append("No listed odd-lot offers in this window.")
    for d in ds:
        px = prices.get(d["ticker"])
        cost = f"99 shares cost about ${99 * px:,.0f}" if px else "no recent price"
        new = "NEW  " if d["ticker"] not in known else "     "
        out.append(f"{new}{d['ticker']:<6} {d['name'][:40]:<40} filed {d['filed']}  last close {('$%.2f' % px) if px else 'n/a':>8}  {cost}")
        out.append(f"       {d['url']}")
    return "\n".join(out)


def main() -> int:
    now = datetime.now(ET)
    today = now.strftime("%Y-%m-%d")
    try:
        ds = deals(search((now - timedelta(days=DAYS)).strftime("%Y-%m-%d"), today))
    except Exception as e:  # noqa: BLE001
        print(f"SEC search failed: {type(e).__name__} {e!s:.200}; the old list is kept")
        return 1
    prices = {}
    if ds:
        try:
            bars = P.fetch([d["ticker"] for d in ds], now - timedelta(days=10))
            prices = {s: v[max(v)][1] for s, v in bars.items() if v}
        except Exception as e:  # noqa: BLE001
            print(f"prices unavailable: {type(e).__name__} {e!s:.120}")
    seen_f = A.HOME / ".odd_lots_seen.json"
    known = set(json.loads(seen_f.read_text())) if seen_f.exists() else set()
    text = render(ds, prices, today, known)
    A.HOME.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text + "\n")
    seen_f.write_text(json.dumps(sorted(known | {d["ticker"] for d in ds})))
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
