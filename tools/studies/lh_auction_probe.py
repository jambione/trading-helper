"""Compare SIP official open/close (daily bar) vs first/last NBBO mid near the auction for a sample of liquid names.
Answers: how far is MOO/MOC fill realism from using daily o/c as the auction price.
"""
import json, os, time, random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockQuotesRequest
from alpaca.data.enums import DataFeed
from config import load_config
import lh_core as C
import lh_strat as S

ET = ZoneInfo("America/New_York")
OUT = "/tmp/lh"
c = load_config() or {}
cl = StockHistoricalDataClient(c.get("api_key"), c.get("secret_key"))


def mid_at(sym, day, hm, win_s=30):
    dd = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    hh, mm = map(int, hm.split(":"))
    t0 = dd.replace(hour=hh, minute=mm, second=0)
    try:
        q = cl.get_stock_quotes(StockQuotesRequest(
            symbol_or_symbols=sym,
            start=t0.astimezone(__import__("datetime").timezone.utc),
            end=(t0 + timedelta(seconds=win_s)).astimezone(__import__("datetime").timezone.utc),
            feed=DataFeed.SIP))
    except Exception as e:  # noqa: BLE001
        return None, str(type(e).__name__)
    rows = (q.data or {}).get(sym) or []
    mids = []
    for r in rows:
        bb, a = float(r.bid_price), float(r.ask_price)
        if bb > 0 and a >= bb:
            mids.append((a + bb) / 2)
    if not mids:
        return None, "empty"
    return mids[0], "ok"


def main():
    P = C.load()
    # sample 80 liquid name-days in 2025-26
    rng = random.Random(7)
    cands = []
    for t in range(len(P["dates"])):
        day = str(P["dates"][t].date())
        if day < "2025-01-01" or day > "2026-09-25":
            continue
        js = np.where(P["liquid"][t])[0]
        if len(js) < 50:
            continue
        for j in rng.sample(list(js), 3):
            cands.append((day, str(P["syms"][j]), float(P["o"][t, j]), float(P["c"][t, j])))
    cands = rng.sample(cands, min(80, len(cands)))
    rows = []
    for i, (day, s, o, c_) in enumerate(cands):
        mo, eo = mid_at(s, day, "09:30", 20)
        mc, ec = mid_at(s, day, "15:59", 20)
        time.sleep(0.2)
        rec = {"day": day, "s": s, "o": o, "c": c_, "mid0930": mo, "mid1559": mc, "eo": eo, "ec": ec}
        if mo and o:
            rec["open_vs_mid_bp"] = (mo / o - 1) * 1e4
        if mc and c_:
            rec["close_vs_mid_bp"] = (mc / c_ - 1) * 1e4
        rows.append(rec)
        if i % 10 == 0:
            print(i, day, s, rec.get("open_vs_mid_bp"), rec.get("close_vs_mid_bp"), flush=True)
    json.dump(rows, open(f"{OUT}/auction_probe.json", "w"), indent=0)
    ov = [r["open_vs_mid_bp"] for r in rows if "open_vs_mid_bp" in r]
    cv = [r["close_vs_mid_bp"] for r in rows if "close_vs_mid_bp" in r]
    def summ(xs):
        xs = np.array(xs); 
        return dict(n=len(xs), mean=float(xs.mean()), med=float(np.median(xs)), p90=float(np.quantile(np.abs(xs), 0.9)))
    print("open_vs_first_mid_bp", summ(ov))
    print("close_vs_last_mid_bp", summ(cv))
    print("NOTE: daily o/c are official auction prints; NBBO mid is continuous book. Gap is not auction slippage vs fill;")
    print("it bounds how far the continuous book sits from the auction print. True MOC/MOO slippage needs broker fill logs.")


if __name__ == "__main__":
    main()
