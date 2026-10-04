"""Swap audit (skeptic gate, 2017-2021 only) + pre-2016 masking/coverage data quality (no pre-2016 outcomes)."""
import sys, os, json, collections, numpy as np, pandas as pd
os.chdir(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, "tools/studies"); sys.path.insert(0, os.path.expanduser("~/lh_cache"))
import overnight_pre2016 as M, lh_core as C, lh_strat as S
A = M.build(); X = M.derive(A); dates = pd.DatetimeIndex(A["dates"])
Pn = S.returns(C.load()); psyms = [str(s) for s in Pn["syms"]]
common = sorted(set(psyms) & set(A["syms"])); yj = np.array([A["syms"].index(s) for s in common]); pj = np.array([psyms.index(s) for s in common])
pd_ = pd.DatetimeIndex(Pn["dates"]); pcf = Pn["cf"][:, pj]; T2 = len(pd_)
pm = np.full_like(pcf, np.nan); pm[252:] = pcf[252-21:T2-21] / pcf[:T2-252] - 1
# third reference: momentum on Yahoo split-adjusted Close (no dividend adjustment)
yc = pd.DataFrame(A["c"][:, yj]).ffill().values; T1 = len(dates)
mc = np.full_like(yc, np.nan); mc[252:] = yc[252-21:T1-21] / yc[:T1-252] - 1
ym = X["m"][:, yj]; yrc, yadv, ycf, yON = A["rc"][:, yj], X["adv"][:, yj], X["cf"][:, yj], X["ON"][:, yj]
prc, padv, pON = Pn["rc"][:, pj], Pn["adv20"][:, pj], Pn["ON"][:, pj]
PD = {d: i for i, d in enumerate(pd_)}
contrib = collections.defaultdict(float); count = collections.Counter(); nights = 0; slots = 0; gap_tot = 0.0
examples = collections.defaultdict(list)
def picks(rc, adv, m, cf, t):
    ok = (rc[t] >= 5) & (adv[t] >= 50e6) & np.isfinite(m[t]) & np.isfinite(cf[t])
    idx = np.where(ok)[0]
    if len(idx) < 30: return None, None
    order = idx[np.argsort(-m[t, idx])]
    return order[:20], {k: r for r, k in enumerate(order)}
for ty, d in enumerate(dates):
    if not (M.CHK[0] <= d <= M.CHK[1]) or ty + 1 >= T1: continue
    tp = PD.get(d)
    if tp is None or tp + 1 >= T2: continue
    py, ry = picks(yrc, yadv, ym, ycf, ty); pp, rp = picks(prc, padv, pm, pcf, tp)
    if py is None or pp is None: continue
    a, b = yON[ty + 1, py], pON[tp + 1, pp]
    if not (np.isfinite(a).any() and np.isfinite(b).any()): continue
    nights += 1
    gap_tot += np.nanmean(a) - np.nanmean(b)
    ysw, psw = set(py) - set(pp), set(pp) - set(py)
    for side, sw, ON_, t1 in (("Y-only", ysw, yON, ty + 1), ("A-only", psw, pON, tp + 1)):
        for k in sw:
            slots += 1
            dm = ym[ty, k] - pm[tp, k] if np.isfinite(ym[ty, k]) and np.isfinite(pm[tp, k]) else np.nan
            if not np.isfinite(dm):
                cls = "MISSING_ONE_SIDE"
            elif abs(dm) <= 0.20:
                cls = "CONVENTION"
            else:
                yc_agree_p = np.isfinite(mc[ty, k]) and abs(mc[ty, k] - pm[tp, k]) <= 0.05
                yc_agree_y = np.isfinite(mc[ty, k]) and abs(mc[ty, k] - ym[ty, k]) <= 0.05
                cls = "YAHOO_ERROR" if yc_agree_p and not yc_agree_y else ("ALPACA_ERROR" if yc_agree_y and not yc_agree_p else "YAHOO_ERROR?ambiguous")
            on = ON_[t1, k]
            v = (on / 20 if side == "Y-only" else -on / 20) if np.isfinite(on) else 0.0
            contrib[cls] += v; count[cls] += 1
            if len(examples[cls]) < 6:
                examples[cls].append((str(d.date()), common[k], side, round(float(ym[ty, k]), 3) if np.isfinite(ym[ty, k]) else None,
                                      round(float(pm[tp, k]), 3) if np.isfinite(pm[tp, k]) else None,
                                      round(float(mc[ty, k]), 3) if np.isfinite(mc[ty, k]) else None, ry.get(k), rp.get(k) if rp else None))
out = {"nights": nights, "book_gap_bp": round(gap_tot / nights * 1e4, 2), "swapped_slots": slots,
       "slot_share": round(slots / 2 / (20 * nights), 4),
       "by_class": {c: {"slots": count[c], "share": round(count[c] / slots, 3), "bp_per_night": round(contrib[c] / nights * 1e4, 2)} for c in count},
       "examples (date, sym, side, m_yahooAdj, m_alpaca, m_yahooClose, rank_y, rank_a)": examples}
yerr = sum(contrib[c] for c in contrib if c.startswith("YAHOO_ERROR")) / nights * 1e4
out["yahoo_error_bp_incl_ambiguous"] = round(yerr, 2)
out["gate_pass_(|yahoo_error|<=0.5)"] = bool(abs(yerr) <= 0.5)
# pre-2016 data quality (no outcomes): masked share by year and rule among eligible name-nights and pick slots
o, c = A["o"], A["c"]; rule = {}
with np.errstate(all="ignore"):
    raw = np.full(c.shape, np.nan); raw[1:] = (o[1:] + A["div"][1:]) / c[:-1] - 1
stale = (A["o"] == A["h"]) & (A["h"] == A["l"]) & (A["l"] == A["c"]) & (A["v"] == 0)
q = {}
for y in range(2000, 2016):
    elig_n = pick_n = 0; m_el = collections.Counter(); m_pk = collections.Counter(); ok30 = 0; nn = 0
    for t in np.where(dates.year == y)[0]:
        if t + 1 >= T1: continue
        ok = (A["rc"][t] >= 5) & (X["adv"][t] >= 50e6) & np.isfinite(X["m"][t]) & np.isfinite(X["cf"][t])
        idx = np.where(ok)[0]; nn += 1
        if not len(idx): continue
        pk = idx[np.argsort(-X["m"][t, idx])][:20]
        ok30 += int(np.isfinite(X["ON"][t + 1, idx]).sum() >= 30)
        for grp, cnt, tot in ((idx, m_el, "e"), (pk, m_pk, "p")):
            r = raw[t + 1, grp]
            cnt["missing_or_nonpos"] += int((~np.isfinite(r)).sum())
            cnt["stale_bar"] += int(stale[t + 1, grp].sum())
            cnt["abs_gt_50pct"] += int((np.abs(r) > 0.5).sum())
            cnt["split_pm1"] += int(A["split_near"][t + 1, grp].sum())
            cnt["bad_name_year"] += int(X["badyr"][t + 1, grp].sum())
            cnt["any_masked"] += int((~np.isfinite(X["ON"][t + 1, grp])).sum())
        elig_n += len(idx); pick_n += len(pk)
    q[y] = {"nights_ge30_finite_ON_share": round(ok30 / max(nn, 1), 3),
            "elig_masked_share": {k: round(v / elig_n, 3) for k, v in m_el.items()},
            "pick_masked_share": {k: round(v / pick_n, 3) for k, v in m_pk.items()}}
out["pre2016_masking_by_year"] = q
json.dump(out, open("data/yahoo_pre2016/swap_audit.json", "w"), indent=1, default=str)
print(json.dumps({k: v for k, v in out.items() if k != "pre2016_masking_by_year"}, indent=1, default=str))
for y, v in q.items(): print(y, v["nights_ge30_finite_ON_share"], "elig any", v["elig_masked_share"].get("any_masked"), "pick any", v["pick_masked_share"].get("any_masked"), "pick stale", v["pick_masked_share"].get("stale_bar"), "pick badyr", v["pick_masked_share"].get("bad_name_year"))
