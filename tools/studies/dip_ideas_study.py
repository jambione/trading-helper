#!/usr/bin/env python3
"""Three ideas for the dip after a signal, plus float rotation. Spec: docs/studies/dip_ideas_prereg.json.

Signals: every PRE fire from early_entry_honest's cached board (limit_below_study.signals). Base trade
= today's entry: SIP open of m+1, SIP close of m+10, 10 bp round trip.

  CUT      hold the base trade, but sell when an IEX low in the first 5 held minutes (m+1..m+5) trades
           10 bp under the entry; sold at the SIP close of that minute (we act after seeing it).
  RECLAIM  after the signal, the IEX low dips 10 bp under the signal's IEX close within m+1..m+10, then a
           later IEX close in that window is back at or above it: buy the SIP open of the next minute,
           hold 10 minutes. Signals with no dip-then-reclaim are not traded.
  ROTATION premarket SIP volume (04:00-09:29) / current float >= 1.0: do signals in rotated names beat
           signals in the rest?
  HEDGE    (information, after the close: SPY bars) short SPY over each base trade's hold.

  (default)        inventory: signal counts, float coverage, rotation counts; no returns
  --rotation       desk-board ROTATION, information only (graded on the five-pillars universe; see prereg)
  --in-sample      CUT and RECLAIM on the 26 sessions that suggested them: a sanity floor only
  --forward        CUT and RECLAIM graded on sessions >= 2026-10-15 (read at 10, extension at 20)
  --hedge PKL      HEDGE information with a pickle {day: SPY SIP frame}
Cache only (plus ai_reports/float_cache.json); no network.
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402
import limit_below_study as L  # noqa: E402
import wait_held_study as W  # noqa: E402

COST = 0.0010
K_BP = 10.0
CUT_WIN = 5
RECLAIM_WIN = 10
HOLD = 10
ROT_THR = 1.0
PM_END = 9 * 60 + 30
T_BAR = 2.7            # four tests on the same signals (wait_held is the fourth), df ~9: Bonferroni ~2.7 (skeptic #8)
COST_INFO = 0.0020     # sensitivity: the 0.15% spread cap from 10/14 widens the board (skeptic #9)
LIFT_BAR = 0.0005
MIN_N, MIN_DAYS = 60, 10
FLOAT_PATH = os.path.join(E.ROOT, "ai_reports", "float_cache.json")


# ── pure ─────────────────────────────────────────────────────────────────────

def base_trade(bars: dict, m: int) -> tuple[float, float] | None:
    """(entry price, gross) for today's entry: SIP open of m+1 to the SIP close of m+10 (carried)."""
    b = bars.get(m + 1)
    if b is None or not b[0]:
        return None
    x = L.exit_close(bars, m + 1, HOLD)
    return None if x is None else (b[0], x / b[0] - 1)


def cut_trade(bars: dict, iex_low: dict, m: int, k_bp: float = K_BP, win: int = CUT_WIN) -> dict | None:
    """The base trade with the early-dip cut. Returns {gross, cut, cut_min} or None."""
    bt = base_trade(bars, m)
    if bt is None:
        return None
    px, gross = bt
    stop = px * (1 - k_bp / 1e4)
    for j in range(m + 1, m + win + 1):
        lo = iex_low.get(j)
        if lo is not None and lo <= stop:
            # The low is known when bar j closes: sell the SIP open of the first bar after j (skeptic #2).
            for jj in range(j + 1, m + HOLD + 1):
                b = bars.get(jj)
                if b is not None and b[0]:
                    tj = bars.get(j)
                    return {"gross": b[0] / px - 1, "cut": True, "cut_min": j - m,
                            "gross_close_j": (tj[3] / px - 1) if tj is not None else None}
            return {"gross": gross, "cut": True, "cut_min": j - m, "gross_close_j": None}   # no later bar: base exit
    return {"gross": gross, "cut": False, "cut_min": None}


def reclaim_entry(iex_low: dict, iex_close: dict, m: int, ref: float, k_bp: float = K_BP,
                  win: int = RECLAIM_WIN) -> int | None:
    """Entry minute (the minute AFTER the reclaim close) or None. Dip first, then a close >= ref.
    A bar whose low dips and whose close is back at ref counts (the low precedes the close)."""
    floor = ref * (1 - k_bp / 1e4)
    dipped = False
    for j in range(m + 1, m + win + 1):
        lo = iex_low.get(j)
        if lo is not None and lo <= floor:
            dipped = True
        c = iex_close.get(j)
        if dipped and c is not None and c >= ref:
            return j + 1
    return None


def rotation(pm_vol: float | None, float_m: float | None) -> float | None:
    if pm_vol is None or not float_m or float_m <= 0:
        return None
    return pm_vol / (float_m * 1e6)


def verdict(net, net_t, lift, lift_t, n, days) -> str:
    if n < MIN_N or days < MIN_DAYS or None in (net, net_t, lift, lift_t):
        return "UNDERPOWERED"
    if not (net > 0 and lift >= LIFT_BAR):
        return "FAIL"
    return "PASS" if net_t >= T_BAR and lift_t >= T_BAR else "UNDERPOWERED"


# ── data ─────────────────────────────────────────────────────────────────────

def load_float() -> dict:
    try:
        raw = json.load(open(FLOAT_PATH))
    except (OSError, ValueError):
        return {}
    return {s.upper(): r.get("float_m") for s, r in raw.items() if isinstance(r, dict)}


def pm_volume(datas: list[dict]) -> dict:
    """(day, sym) -> SIP volume 04:00-09:29 ET."""
    out = {}
    for data in datas:
        for sym, fr in data["sip"].items():
            v = 0.0
            for t, vol in zip(fr["time"], fr["volume"]):
                if E._et_min(t.timestamp()) < PM_END:
                    v += float(vol)
            out[(data["day"], sym)] = v
    return out


def enrich(sigs: list[dict], datas: list[dict]) -> None:
    fl = load_float()
    pmv = pm_volume(datas)
    for s in sigs:
        s["rot"] = rotation(pmv.get((s["day"], s["sym"])), fl.get(s["sym"]))


# ── scoring ──────────────────────────────────────────────────────────────────

def _by_day(rows):
    d = defaultdict(list)
    for day, v in rows:
        d[day].append(v)
    return d


def score_cut(sigs, cost: float = COST) -> dict:
    hold, cut, lift, n_cut, n = [], [], defaultdict(list), 0, 0
    for s in sigs:
        bt = base_trade(s["bars"], s["m"])
        ct = cut_trade(s["bars"], s["iex_low"], s["m"])
        if bt is None or ct is None:
            continue
        n += 1
        n_cut += ct["cut"]
        hold.append((s["day"], bt[1] - cost))
        cut.append((s["day"], ct["gross"] - cost))
    hb, cb = _by_day(hold), _by_day(cut)
    for d in cb:
        lift[d].append(sum(cb[d]) / len(cb[d]) - sum(hb[d]) / len(hb[d]))
    cn, ct_, _, nd = E.day_equal(cb)
    lf, lt, _, _ = E.day_equal(lift)
    return {"net": cn, "net_t": ct_, "hold_net": E.day_equal(hb)[0], "lift": lf, "lift_t": lt, "n": n,
            "cut_share": (n_cut / n) if n else None, "days": nd, "verdict": verdict(cn, ct_, lf, lt, n, nd)}


def score_reclaim(sigs, control: bool = False, cost: float = COST) -> dict:
    tr, mk, lift = [], [], defaultdict(list)
    for s in sigs:
        m, ref = s["m"], s.get("ref")
        if control:
            sipcl = {kk: (v[0], v[3]) for kk, v in s["bars"].items()}
            kc = E.control_minute(s["sym"], s["day"], "RECLAIM", sipcl, set(), m + 1)
            if kc is None:
                continue
            m = kc - 1
            ref = s["iex_close"].get(m)
        if ref is None:
            continue
        bt = base_trade(s["bars"], m)
        if bt is None:
            continue
        mk.append((s["day"], bt[1] - cost))
        e = reclaim_entry(s["iex_low"], s["iex_close"], m, ref)
        if e is None:
            continue
        g = W.entry_trade(s["bars"], e)
        if g is not None:
            tr.append((s["day"], g - cost))
    tb, mb = _by_day(tr), _by_day(mk)
    for d in tb:
        lift[d].append(sum(tb[d]) / len(tb[d]) - sum(mb[d]) / len(mb[d]))
    rn, rt, n, nd = E.day_equal(tb)
    lf, lt, _, _ = E.day_equal(lift)
    return {"net": rn, "net_t": rt, "mkt_net": E.day_equal(mb)[0], "lift": lf, "lift_t": lt, "n": n,
            "by_day": tb, "signals": sum(len(v) for v in mb.values()), "days": nd,
            "verdict": verdict(rn, rt, lf, lt, n, nd)}


def reclaim_verdict(x: dict, c: dict) -> tuple[str, float | None]:
    """The graded RECLAIM verdict: verdict() AND reclaim net - control net, paired per day, >= 0 (skeptic #3)."""
    pd_ = {d: [sum(v) / len(v) - sum(c["by_day"][d]) / len(c["by_day"][d])]
           for d, v in x["by_day"].items() if v and c["by_day"].get(d)}
    vs = E.day_equal(pd_)[0]
    v = x["verdict"]
    if v == "PASS" and (vs is None or vs < 0):
        v = "FAIL"
    return v, vs


def kill(lifts: list) -> bool:
    """In-sample floor (skeptic #4): KILL when the lift is <= 0 in both halves."""
    return all(v is not None and v <= 0 for v in lifts)


def score_rotation(sigs, thr: float = ROT_THR) -> dict:
    hi, lo, lift = [], [], defaultdict(list)
    unknown = 0
    for s in sigs:
        bt = base_trade(s["bars"], s["m"])
        if bt is None:
            continue
        if s.get("rot") is None:
            unknown += 1
            continue
        (hi if s["rot"] >= thr else lo).append((s["day"], bt[1] - COST))
    hb, lb = _by_day(hi), _by_day(lo)
    for d in hb:
        if d in lb:
            lift[d].append(sum(hb[d]) / len(hb[d]) - sum(lb[d]) / len(lb[d]))
    hn, ht, n, nd = E.day_equal(hb)
    lf, lt, _, _ = E.day_equal(lift)
    return {"net": hn, "net_t": ht, "rest_net": E.day_equal(lb)[0], "lift": lf, "lift_t": lt, "n": n,
            "rest_n": sum(len(v) for v in lb.values()), "unknown_float": unknown, "days": nd,
            "verdict": verdict(hn, ht, lf, lt, n, nd)}


def _bp(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def _t(x):
    return "—" if x is None else f"{x:+.2f}"


def halves(sigs):
    return {h: [s for s in sigs if (s["day"] <= L.SPLIT_END_A) == (h == "A")] for h in "AB"}


def cut_reclaim_lines(parts: dict, only: set | None = None) -> tuple[list[str], dict]:
    """Tables for CUT and RECLAIM; `only` limits which tests are graded here (others print 'not graded').
    Returns (lines, {test: [verdict per part]}) and the lifts for the kill floor."""
    only = only or {"CUT", "RECLAIM"}
    out, res = [], {"CUT": [], "RECLAIM": [], "CUT_lift": [], "RECLAIM_lift": []}
    if "CUT" in only:
        out += ["| part | CUT trades | cut share | cut net (t) | hold net | lift (t) | verdict | at 20 bp: cut net / lift |",
                "|---|---:|---:|---:|---:|---:|---|---|"]
        for name, sg in parts.items():
            x, x2 = score_cut(sg), score_cut(sg, cost=COST_INFO)
            cs = "—" if x["cut_share"] is None else f"{x['cut_share']:.0%}"
            out.append(f"| {name} | {x['n']} | {cs} | {_bp(x['net'])} ({_t(x['net_t'])}) | {_bp(x['hold_net'])} "
                       f"| {_bp(x['lift'])} ({_t(x['lift_t'])}) | {x['verdict']} | {_bp(x2['net'])} / {_bp(x2['lift'])} |")
            res["CUT"].append(x["verdict"])
            res["CUT_lift"].append(x["lift"])
    else:
        out.append("CUT: decided at 10 sessions, not graded here")
    if "RECLAIM" in only:
        out += ["", "| part | signals | RECLAIM trades | reclaim net (t) | market net | lift (t) | control n / net | reclaim - control | verdict | at 20 bp net |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---|---|"]
        for name, sg in parts.items():
            x, c = score_reclaim(sg), score_reclaim(sg, control=True)
            v, vs = reclaim_verdict(x, c)
            x2 = score_reclaim(sg, cost=COST_INFO)
            out.append(f"| {name} | {x['signals']} | {x['n']} | {_bp(x['net'])} ({_t(x['net_t'])}) | {_bp(x['mkt_net'])} "
                       f"| {_bp(x['lift'])} ({_t(x['lift_t'])}) | {c['n']} / {_bp(c['net'])} | {_bp(vs)} | {v} | {_bp(x2['net'])} |")
            res["RECLAIM"].append(v)
            res["RECLAIM_lift"].append(x["lift"])
    else:
        out.append("RECLAIM: decided at 10 sessions, not graded here")
    return out, res


def hedge_lines(sigs, spy: dict) -> list[str]:
    """Information: beta of base trades to SPY over the same minutes, and the hedged net."""
    import statistics
    pairs = []
    for s in sigs:
        bt = base_trade(s["bars"], s["m"])
        fr = spy.get(s["day"])
        if bt is None or fr is None:
            continue
        sp = L.ohlc_map(fr)
        a, b = sp.get(s["m"] + 1), L.exit_close(sp, s["m"] + 1, HOLD)
        if a is None or b is None:
            continue
        pairs.append((bt[1], b / a[0] - 1))
    if len(pairs) < 30:
        return [f"HEDGE: too few pairs ({len(pairs)})"]
    xs, ys = [p[1] for p in pairs], [p[0] for p in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    cov = sum((x - mx) * (y - my) for x, y in pairs) / (len(pairs) - 1)
    beta = cov / statistics.variance(xs)
    hedged = [y - beta * x for y, x in pairs]
    return [f"HEDGE (information): {len(pairs)} trades, beta to SPY {beta:+.2f}, mean SPY move over the hold {_bp(mx)}",
            f"  unhedged gross {_bp(my)} sd {_bp(statistics.stdev(ys))}; hedged gross {_bp(statistics.fmean(hedged))} "
            f"sd {_bp(statistics.stdev(hedged))} (before the SPY spread, ~1 bp)"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rotation", action="store_true")
    ap.add_argument("--in-sample", action="store_true")
    ap.add_argument("--forward", action="store_true")
    ap.add_argument("--hedge")
    a = ap.parse_args()
    datas, ex = E.load_days()
    ins = [d for d in datas if d["day"] <= W.IN_SAMPLE_END]
    fwd_days = sorted(d["day"] for d in datas if d["day"] >= W.FORWARD_START)
    print(f"cached days {len(datas)} (in-sample {len(ins)}, forward {len(fwd_days)}); excluded {ex}")
    if not (a.rotation or a.in_sample or a.forward or a.hedge):
        sigs = W.signals(ins)
        enrich(sigs, ins)
        known = [s for s in sigs if s["rot"] is not None]
        print(f"in-sample signals {len(sigs)}; float known {len(known)}; rotation >= {ROT_THR}: "
              f"{sum(1 for s in known if s['rot'] >= ROT_THR)} on {len({s['day'] for s in known if s['rot'] >= ROT_THR})} days")
        n_cut = n_rec = n_ok = 0
        for x in sigs:                               # counts only (skeptic #9)
            ct = cut_trade(x["bars"], x["iex_low"], x["m"])
            if ct is None or x.get("ref") is None:
                continue
            n_ok += 1
            n_cut += ct["cut"]
            n_rec += reclaim_entry(x["iex_low"], x["iex_close"], x["m"], x["ref"]) is not None
        print(f"tradable signals {n_ok}; would be cut {n_cut} ({n_cut / max(1, n_ok):.0%}); dip-and-reclaim {n_rec} "
              f"({n_rec / max(1, n_ok):.0%})")
        print("(inventory only; returns after the prereg has a result-skeptic READY)")
        return
    md = []
    if a.rotation:
        sigs = W.signals(ins)
        enrich(sigs, ins)
        md += [f"# Float rotation (premarket volume / float >= {ROT_THR}) - desk board, INFORMATION ONLY", "",
               "| half | rotated signals | rotated net (t) | rest net (rest n) | lift (t) | unknown float | verdict |",
               "|---|---:|---:|---:|---:|---:|---|"]
        vs = []
        for h, sg in halves(sigs).items():
            x = score_rotation(sg)
            vs.append(x["verdict"])
            md.append(f"| {h} | {x['n']} | {_bp(x['net'])} ({_t(x['net_t'])}) | {_bp(x['rest_net'])} ({x['rest_n']}) "
                      f"| {_bp(x['lift'])} ({_t(x['lift_t'])}) | {x['unknown_float']} | {x['verdict']} |")
        final = "PASS" if all(v == "PASS" for v in vs) else ("FAIL" if "FAIL" in vs else "UNDERPOWERED")
        md += ["", f"(desk board, INFORMATION, not a verdict: {final.lower()})", "", "Information (thresholds 0.25, 0.5, 2.0; $10+ names):"]
        for thr in (0.25, 0.5, 2.0):
            md.append(f"- >= {thr}: " + "; ".join(f"{h} net {_bp(x['net'])} lift {_bp(x['lift'])} n {x['n']}"
                                                  for h, x in ((h, score_rotation(sg, thr)) for h, sg in halves(sigs).items())))
        ten = [s for s in sigs if (s.get("ref") or 0) >= 10]
        md.append("- price >= $10: " + "; ".join(f"{h} net {_bp(x['net'])} lift {_bp(x['lift'])} n {x['n']}"
                                               for h, x in ((h, score_rotation(sg)) for h, sg in halves(ten).items())))
    if a.in_sample:
        sigs = W.signals(ins)
        md += ["", "# CUT and RECLAIM - in-sample (sanity floor only; suggested by these sessions)", ""]
        lines, res = cut_reclaim_lines({f"in-sample {h}": sg for h, sg in halves(sigs).items()})
        md += lines + [""]
        for t in ("CUT", "RECLAIM"):
            md.append(f"- {t}: {'KILL (lift <= 0 in both halves): forward read cancelled' if kill(res[t + '_lift']) else 'NO KILL (not evidence)'}")
    if a.forward:
        by = {d["day"]: d for d in datas}
        if len(fwd_days) < W.READ_AT:
            print(f"NOT DUE: {len(fwd_days)} forward sessions < {W.READ_AT}")
            return
        sigs = W.signals([by[d] for d in fwd_days[:W.READ_AT]])
        md += [f"# CUT and RECLAIM - forward read on {W.READ_AT} sessions", ""]
        lines, res = cut_reclaim_lines({"forward": sigs})
        md += lines
        again = {t for t in ("CUT", "RECLAIM") if res[t][0] == "UNDERPOWERED"}   # skeptic #1
        if again and len(fwd_days) >= W.EXTEND_TO:
            sigs20 = W.signals([by[d] for d in fwd_days[:W.EXTEND_TO]])
            md += ["", f"Extension read on {W.EXTEND_TO} sessions (only tests UNDERPOWERED at 10):", ""]
            md += cut_reclaim_lines({"forward20": sigs20}, only=again)[0]
    if a.hedge:
        spy = pickle.load(open(a.hedge, "rb"))
        md += [""] + hedge_lines(W.signals(ins), spy)
    out = "\n".join(md)
    print(out)
    with open(os.path.join(E.OUT_DIR, "dip_ideas_result.md"), "a") as f:
        f.write(out + "\n")


if __name__ == "__main__":
    main()
