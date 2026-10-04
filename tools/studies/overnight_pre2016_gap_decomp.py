"""Exact decomposition of the 2017-2021 book gap (no pre-2016 outcomes)."""
import sys, os, numpy as np, pandas as pd, collections
os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
exec(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "overnight_pre2016_swap_audit.py")).read().split("PD = {d: i")[0])
PD = {d: i for i, d in enumerate(pd_)}
def picks(rc, adv, m, cf, t):
    ok = (rc[t] >= 5) & (adv[t] >= 50e6) & np.isfinite(m[t]) & np.isfinite(cf[t]); idx = np.where(ok)[0]
    return idx[np.argsort(-m[t, idx])][:20] if len(idx) >= 30 else None
G = collections.defaultdict(list)
for ty, d in enumerate(dates):
    if not (M.CHK[0] <= d <= M.CHK[1]) or ty + 1 >= T1: continue
    tp = PD.get(d)
    if tp is None or tp + 1 >= T2: continue
    py, pp = picks(yrc, yadv, ym, ycf, ty), picks(prc, padv, pm, pcf, tp)
    if py is None or pp is None: continue
    a, b = yON[ty + 1, py], pON[tp + 1, pp]
    if not (np.isfinite(a).any() and np.isfinite(b).any()): continue
    G["registered"].append(np.nanmean(a) - np.nanmean(b))
    # symmetric masking: a name-night counts only if finite on BOTH sources (and |ON|<=50% on both)
    yy, pa = yON[ty + 1], pON[tp + 1]
    both = np.isfinite(yy) & np.isfinite(pa) & (np.abs(pa) <= 0.5)
    a2, b2 = yy[py][both[py]], pa[pp][both[pp]]
    if len(a2) and len(b2): G["symmetric_mask"].append(a2.mean() - b2.mean())
    # Alpaca picks, alpaca ON, with vs without the names Yahoo masked
    G["alpaca_picks_yahoo-masked_effect"].append(np.nanmean(pa[pp]) - (pa[pp][np.isfinite(yy[pp])].mean() if np.isfinite(yy[pp]).any() else np.nan))
    # which masked names: Alpaca ON of picks that are NaN on Yahoo
    for k in pp:
        if np.isfinite(pa[k]) and not np.isfinite(yy[k]):
            G["masked_alpaca_ON"].append(pa[k])
for k, v in G.items():
    v = np.array(v, float); v = v[np.isfinite(v)] * 1e4
    print(f"{k:38s} n {len(v):5d} mean {v.mean():+8.2f} bp  t {v.mean()/(v.std(ddof=1)/len(v)**.5):+.2f}" if len(v) > 1 else k)
x = np.array(G["masked_alpaca_ON"]) * 1e4
print("Alpaca-pick slots masked on Yahoo:", len(x), "mean Alpaca ON", round(x.mean(), 1), "bp; n>|50%|", int((np.abs(x) > 5000).sum()), "sum/nights bp", round(x.sum() / 20 / len(G["registered"]), 2))
