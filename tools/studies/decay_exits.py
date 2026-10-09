"""decay_exits.py (mini, repo root): python3 tools/studies/decay_exits.py DAY [DAY ...]
Read-only, information only: trail exits where the time-decay leash had stepped the stop up (stop rose while the peak did not),
and what price did in the 30 min after the exit, from recorded IEX prints. Gross, no costs."""
import bisect, gzip, json, statistics as st, sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
days = sys.argv[1:]
pos = defaultdict(list)
for l in open("ai_reports/position_shadow.jsonl"):
    r = json.loads(l)
    d = datetime.fromtimestamp(r["ts"], ET).strftime("%Y-%m-%d")
    if d in days and r.get("entry_price"):
        pos[(d, r["symbol"], r["entry_price"])].append(r)
px = {}
def prints(d):
    if d not in px:
        m = defaultdict(lambda: ([], []))
        for l in gzip.open(f"ai_reports/sessions/{d}/prints.jsonl.gz", "rt"):
            r = json.loads(l)
            if r.get("price") and r.get("age_sec", 99) < 15:
                m[r["symbol"]][0].append(r["ts"]); m[r["symbol"]][1].append(r["price"])
        for a in m.values():
            z = sorted(zip(*a)); a[0][:] = [t for t, _ in z]; a[1][:] = [p for _, p in z]
        px[d] = m
    return px[d]
out = []
for (d, s, e), rows in pos.items():
    rows.sort(key=lambda r: r["ts"])
    ex = next((r for r in rows if r.get("exit_why") not in (None, "hold")), None)
    if not ex or ex["exit_why"] != "local_trail":
        continue
    steps = 0
    for a, b in zip(rows, rows[1:]):
        if b["ts"] > ex["ts"]:
            break
        if (b.get("local_stop_price") or 0) > (a.get("local_stop_price") or 0) and (b.get("peak_price") or 0) <= (a.get("peak_price") or 0):
            steps += 1
    ts, ps = prints(d)[s]
    i, j = bisect.bisect_left(ts, ex["ts"]), bisect.bisect_right(ts, ex["ts"] + 1800)
    after = ps[i:j]
    if not after:
        continue
    x = ex["price"]
    out.append({"d": d, "s": s, "decay": steps > 0, "kept": (x / e - 1) * 1e4, "mfe": ((ex.get("peak_price") or x) / e - 1) * 1e4,
                "up30": (max(after) / x - 1) * 1e4, "dn30": (min(after) / x - 1) * 1e4, "end30": (after[-1] / x - 1) * 1e4,
                "src": ex.get("source")})
def show(lab, rs):
    if not rs:
        print(f"  {lab}: none"); return
    ran = sum(r["up30"] >= 30 for r in rs); fell = sum(r["dn30"] <= -30 for r in rs)
    print(f"  {lab}: n {len(rs)} | kept {st.fmean(r['kept'] for r in rs):+.1f} bp (peak {st.fmean(r['mfe'] for r in rs):+.1f}) | 30 min after exit: "
          f"price at +30m {st.fmean(r['end30'] for r in rs):+.1f} bp (up {sum(r['end30'] > 0 for r in rs)}/{len(rs)}), "
          f"ran >= +30 bp {ran} ({ran / len(rs):.0%}), fell >= 30 bp {fell} ({fell / len(rs):.0%})")
print(f"days {days[0]}..{days[-1]}: {len(out)} trail exits with prints")
show("decay-stepped trail exits", [r for r in out if r["decay"]])
show("other trail exits        ", [r for r in out if not r["decay"]])
show("decay-stepped, tight source", [r for r in out if r["decay"] and r["src"] == "tight"])
for d in days:
    rs = [r for r in out if r["d"] == d and r["decay"]]
    if rs: print(f"   {d}: decay exits {len(rs)}, ran >= +30 bp {sum(r['up30'] >= 30 for r in rs)}, at +30m {st.fmean(r['end30'] for r in rs):+.1f} bp")
