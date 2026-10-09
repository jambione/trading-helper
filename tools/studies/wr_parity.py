"""wr_parity.py — read-only %R accuracy check (docs/studies/WR_ACCURACY_2026-10-09.md).
The live desk's logged %R (fast r, slow rs) vs %R computed from full-tape SIP 1-min bars at the same minute.
SIP bars that CLOSED before the logged moment; fast = %R(21) EMA 7, slow = %R(112) EMA 3 on a minute grid (signals.py)."""
# USAGE (mini, repo root, during or after a session; one SIP bars request): .venv/bin/python tools/studies/wr_parity.py DAY
import gzip, json, random, sys, os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
sys.path[:0] = [os.getcwd(), os.path.join(os.getcwd(), "tools")]
import pandas as pd
import signals as S
import bars as B
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed
ET = ZoneInfo("America/New_York")
day = sys.argv[1]
samples = []
for l in gzip.open(f"ai_reports/sessions/{day}/decisions.jsonl.gz", "rt"):
    r = json.loads(l)
    if r.get("ev") != "arm":
        continue
    for x in r.get("rows", []):
        if x.get("r") is not None and x.get("rs") is not None:
            samples.append((r["ts"], x["s"], x["r"], x["rs"], x.get("rsrc"), x.get("src")))
rng = random.Random(7)
now = datetime.now(ET)
samples = [s for s in samples if datetime.fromtimestamp(s[0], ET) < now - timedelta(minutes=20)
           and datetime.fromtimestamp(s[0], ET).hour * 60 + datetime.fromtimestamp(s[0], ET).minute >= 11 * 60 + 30]
pick = rng.sample(samples, min(60, len(samples)))
syms = sorted({p[1] for p in pick})
d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
res = B.client().get_stock_bars(StockBarsRequest(symbol_or_symbols=syms, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
        start=d0.replace(hour=9, minute=30), end=now - timedelta(minutes=16), feed=DataFeed.SIP))
bars = {s: pd.DataFrame([{"time": b.timestamp, "high": b.high, "low": b.low, "close": b.close} for b in rows])
        for s, rows in (res.data or {}).items()}
out = []
for ts, s, r, rs, rsrc, src in pick:
    df = bars.get(s)
    if df is None or df.empty:
        continue
    cut = df[df["time"] + pd.Timedelta(minutes=1) <= pd.Timestamp(ts, unit="s", tz="UTC")].reset_index(drop=True)
    if len(cut) < 130:
        continue
    f = S._minute_grid_pr(cut, 21, 7).iloc[-1]
    sl = S._minute_grid_pr(cut, 112, 3).iloc[-1]
    out.append((s, datetime.fromtimestamp(ts, ET).strftime("%H:%M:%S"), r, f, rs, sl, rsrc, src))
import statistics as st
df_ = [abs(a - b) for _, _, a, b, _, _, _, _ in out]
ds_ = [abs(a - b) for _, _, _, _, a, b, _, _ in out]
def q(v): v = sorted(v); return f"median {st.median(v):.1f}, 80th pct {v[int(.8*len(v))]:.1f}, worst {v[-1]:.1f}, within 5 pts {sum(x <= 5 for x in v)}/{len(v)}"
print(f"{day}: {len(out)} live readings (11:30+) vs SIP-tape %R at the same minute")
print("  FAST |live - SIP|:", q(df_))
print("  SLOW |live - SIP|:", q(ds_))
by = {}
for o in out:
    by.setdefault(o[6], []).append(abs(o[2] - o[3]))
print("  fast error by live source:", {k: f"{st.median(v):.1f} (n {len(v)})" for k, v in by.items()})
for o in sorted(out, key=lambda o: -abs(o[2] - o[3]))[:6]:
    print(f"   worst: {o[0]:6s} {o[1]} fast live {o[2]:+.1f} vs SIP {o[3]:+.1f} | slow live {o[4]:+.1f} vs SIP {o[5]:+.1f} | src {o[6]}/{o[7]}")
