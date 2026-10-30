"""EXPLORATION ONLY (2026-10-30): the 3-minute never-green cut replayed on 10/26-10/30, the look that chose N = 3 for
docs/studies/never_green_cut_prereg.json. Kept so the seen numbers can be reproduced; not the scorer."""
import json, sys, datetime as dt, statistics as st
from zoneinfo import ZoneInfo
sys.path[:0] = ["tools/studies", "tools", "."]
import move_to_cost_holdout as MH

ET = ZoneInfo("America/New_York")
days = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
rows, seen = [], set()
for l in open("ai_reports/outcomes.jsonl"):
    try:
        r = json.loads(l)
    except ValueError:
        continue
    t = r.get("entry_time")
    if not t or r.get("entry_path") != "watch" or not r.get("exit_price"):
        continue
    d = dt.datetime.fromtimestamp(float(t), ET)
    k = (r["symbol"], round(float(t)))
    if str(d.date()) in days and k not in seen:
        seen.add(k)
        rows.append((str(d.date()), d, r))
data = MH.Data.live()
data.dir = MH.Path("/tmp/ng3_cache")
out = []
for day, d, r in rows:
    ep, xp = float(r["entry_price"]), float(r["exit_price"])
    actual = (xp / ep - 1) * 1e4
    hold = float(r.get("hold_sec") or 0)
    if hold <= 180:                       # closed within 3 minutes anyway: rule changes nothing
        out.append((day, actual, actual, False))
        continue
    m0 = d.hour * 60 + d.minute
    bars = {int(b[0]): b for b in data.bars(r["symbol"], day)}
    win = [bars[k] for k in range(m0 + 1, m0 + 4) if k in bars]   # the 3 completed minutes after the entry minute
    went_green = any(b[2] > ep for b in win) or (bars.get(m0) and bars[m0][2] > ep and d.second < 30)
    if went_green or not win:
        out.append((day, actual, actual, False))
        continue
    cut = (win[-1][4] / ep - 1) * 1e4        # sell at the close of the 3rd minute
    out.append((day, actual, cut, True))

def show(name, xs):
    a, c = [x[1] for x in xs], [x[2] for x in xs]
    cut = [x for x in xs if x[3]]
    print(f"{name:11} trades {len(xs):3}  cut by rule {len(cut):3}  actual avg {st.fmean(a):6.2f} bp  with rule {st.fmean(c):6.2f} bp"
          f"  diff {st.fmean(c) - st.fmean(a):+5.2f} bp/trade"
          + (f" | cut trades: actual {st.fmean(x[1] for x in cut):6.1f} -> rule {st.fmean(x[2] for x in cut):6.1f};"
             f" cut winners {sum(x[1] > 0 for x in cut)}" if cut else ""))
for day in days:
    show(day, [x for x in out if x[0] == day])
show("WEEK", out)
