#!/usr/bin/env python3
"""POST-HOC robustness check for gate_grade.py (written after its results): same gates, but REFUSED and KEPT both
taken from the INCLUSION stage (KEPT for the range gate must carry a measured range_pos), so both sides are timed at
the gate itself. Adds range_pos buckets and a 1%-trimmed diff. Run on the mini from the repo root."""
import json, os, sys, statistics, collections, glob
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)
sys.path.insert(0, "tools/studies"); sys.path.insert(0, "tools"); sys.path.insert(0, ".")
import gate_grade as G, runway_study as rs, bars
def run(gate, since):
    first = {}
    for fp in sorted(glob.glob("ai_reports/proposal_ledger/*.jsonl")):
        day = os.path.basename(fp)[:10]
        if day < since: continue
        ref, kep = {}, {}
        for l in open(fp):
            try: e = json.loads(l)
            except Exception: continue
            if e.get("stage") != "inclusion" or e.get("heartbeat"): continue
            sym, ts = e.get("symbol") or "", e.get("ts")
            if not rs.SYM_RE.match(sym) or not isinstance(ts, (int, float)) or not (G.T0 <= bars.et_minutes(ts) <= G.T1): continue
            rp = e.get("range_pos")
            if e.get("decision") == "dropped" and e.get("reason") == gate:
                if sym not in ref: ref[sym] = (ts, rp)
            elif e.get("decision") == "kept" and (gate != "admit_range_pos" or rp is not None):
                if sym not in kep: kep[sym] = (ts, rp)
        for s, v in ref.items(): first[(s, day)] = (v[0], True, v[1])
        for s, v in kep.items():
            if s not in ref: first[(s, day)] = (v[0], False, v[1])
    cache = rs._load_cache()
    want = collections.defaultdict(set)
    for (s, d) in first: want[d].add(s)
    for d, ss in sorted(want.items()):
        ss = sorted(ss)
        for i in range(0, len(ss), 100): rs.fetch_days({d: set(ss[i:i+100])}, cache)
    out = []
    for (s, d), (ts, ref, rp) in first.items():
        B = cache.get((s, d)); o = G.outcome(B, ts) if B else None
        if o: out.append({"day": d, "ref": ref, "rp": rp, "px": o[0], "r30": o[1], "rend": o[2], "m": bars.et_minutes(ts)})
    print(f"\n== {gate}, INCLUSION stage only, since {since}: n {len(out)} refused {sum(r['ref'] for r in out)} days {len({r['day'] for r in out})}")
    for lab, sub in (("all", out), (">=$10", [r for r in out if r["px"] >= 10]), ("<$10", [r for r in out if r["px"] < 10])):
        for key in ("r30", "rend"):
            xs = [r for r in sub if r[key] is not None]
            R = [r[key] for r in xs if r["ref"]]; K = [r[key] for r in xs if not r["ref"]]
            if len(R) < 10 or len(K) < 10: print(lab, key, "too few", len(R), len(K)); continue
            b, se = G.cl_diff([r[key] for r in xs], [int(r["ref"]) for r in xs], [r["day"] for r in xs])
            print(f"{lab:6} {key:5} R n {len(R):4} mean {statistics.fmean(R):+7.1f} med {statistics.median(R):+6.1f} | K n {len(K):4} mean {statistics.fmean(K):+7.1f} med {statistics.median(K):+6.1f} | diff {b:+6.1f} t {b/se:+.2f}")
    print("median minute R", statistics.median(r["m"] for r in out if r["ref"]), "K", statistics.median(r["m"] for r in out if not r["ref"]))
    if gate == "admit_range_pos":
        for lo, hi in ((0, 50), (50, 75), (75, 90.01), (90.01, 95), (95, 101)):
            xs = [r["r30"] for r in out if r["rp"] is not None and lo <= r["rp"] < hi]
            if xs: print(f"  range_pos {lo}-{hi}: n {len(xs)} mean {statistics.fmean(xs):+.1f} med {statistics.median(xs):+.1f}")
        # trimmed check: drop top/bottom 1% to see if a few runners drive it
        xs = sorted(out, key=lambda r: r["r30"]); k = len(xs) // 100
        xs = xs[k:len(xs) - k]
        b, se = G.cl_diff([r["r30"] for r in xs], [int(r["ref"]) for r in xs], [r["day"] for r in xs])
        print(f"  1%-trimmed diff {b:+.1f} t {b/se:+.2f}")
run("admit_range_pos", "2026-09-06"); run("gapped_down", "2026-09-24")
