#!/usr/bin/env python3
"""Limit below the signal. Spec: docs/studies/limit_below_prereg.json.

Every pre-square fire from early_entry_honest's cached board; compare buying at the next minute's open
(market) with a resting buy limit k bp below the signal price for N minutes (fills only when the SIP
low trades 2 bp through the limit). Same 10-minute hold from the fill; 10 bp round trip on both arms.

  (default)  inventory only, no returns
  --score    after the prereg has a result-skeptic READY

Cache only (ai_reports/early_entry_honest/<day>.pkl); makes no network calls.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402

PRIMARY = (10, 5)
GRID_K, GRID_N = (5, 10, 20), (2, 5, 10)
THROUGH = 0.0002
THROUGH_INFO = 0.0005
HOLD = 10
COST, COST_LIMIT_INFO = 0.0010, 0.0005
SPLIT_END_A = "2026-09-24"
MIN_FILLS, MIN_DAYS = 60, 10    # fixed before any fill rate was seen (skeptic round 1 #5)
LIFT_BAR = 0.0005


# ── pure ─────────────────────────────────────────────────────────────────────

def ohlc_map(frame) -> dict:
    """{ET minute: (open, high, low, close)} from a cached SIP frame."""
    return {E._et_min(t.timestamp()): (float(o), float(h), float(lo), float(c))
            for t, o, h, lo, c in zip(frame["time"], frame["open"], frame["high"], frame["low"], frame["close"])}


def limit_fill(bars: dict, m: int, k_bp: float, n_min: int, ref: float | None = None,
               through: float = THROUGH) -> tuple[int, float] | None:
    """(fill minute, fill price) for a limit k bp below `ref`, live m+1..m+N (SIP bars). `ref` is the
    IEX close that fired the signal (what live sees; skeptic #1); None falls back to the SIP close of m.
    Filled when the minute's SIP low trades THROUGH the limit; price = min(limit, that minute's open)."""
    if ref is None:
        sig = bars.get(m)
        if sig is None or not sig[3]:
            return None
        ref = sig[3]
    lim = ref * (1 - k_bp / 1e4)
    for j in range(m + 1, m + n_min + 1):
        b = bars.get(j)
        if b is None:
            continue
        if b[2] <= lim * (1 - through):
            return j, min(lim, b[0])
    return None


def exit_close(bars: dict, fill_minute: int, hold: int = HOLD) -> float | None:
    """SIP close at minute fill + hold - 1, or the last close before it if that bar is missing
    (skeptic #6: the market arm keeps such signals too). None only past 16:00 or with no bar at all."""
    j = fill_minute + hold - 1
    if j >= E.RTH_END:
        return None
    for jj in range(j, fill_minute - 1, -1):
        b = bars.get(jj)
        if b is not None:
            return b[3]
    return None


def market_trade(bars: dict, m: int, hold: int = HOLD) -> float | None:
    """Gross return buying the SIP open of m+1, selling the close of m+hold (early_entry_honest.markout)."""
    sip = {k: (v[0], v[3]) for k, v in bars.items()}
    return E.markout(sip, m + 1, hold)


def limit_trade(bars: dict, m: int, k_bp: float, n_min: int, hold: int = HOLD, ref: float | None = None,
                through: float = THROUGH) -> dict:
    f = limit_fill(bars, m, k_bp, n_min, ref, through)
    if f is None:
        return {"filled": False}
    j, px = f
    x = exit_close(bars, j, hold)
    if x is None:
        return {"filled": True, "dropped": True, "fill_min": j}
    xs = exit_close(bars, m + 1, hold)          # the market arm's exit bar (skeptic #3a)
    return {"filled": True, "dropped": False, "fill_min": j, "gross": x / px - 1,
            "gross_same_exit": (xs / px - 1) if xs is not None else None}


def half_verdict(lift, t, fill_net, n_fills, n_days, fill_t=None) -> str:
    """Lift >= +5 bp with t >= 2, and filled trades net > 0 WITH t >= 2 (skeptic #2)."""
    if n_fills < MIN_FILLS or n_days < MIN_DAYS or lift is None or t is None or fill_net is None:
        return "UNDERPOWERED"
    return "PASS" if (lift >= LIFT_BAR and t >= 2.0 and fill_net > 0 and (fill_t or 0) >= 2.0) else "FAIL"


# ── scoring ──────────────────────────────────────────────────────────────────

SKIPS = Counter()


def signals(datas: list[dict]) -> list[dict]:
    out = []
    for data in datas:
        day = data["day"]
        for sym, board in data["board"].items():
            iex, sipf = data["iex"].get(sym), data["sip"].get(sym)
            if iex is None or sipf is None or len(iex) < 30:
                SKIPS["name_day_no_or_short_bars"] += 1
                continue
            try:
                ser = E.name_day_series(iex)
            except Exception:  # noqa: BLE001
                SKIPS["name_day_indicator_fail"] += 1
                continue
            bars = ohlc_map(sipf)
            iex_close = dict(zip(ser["minutes"], ser["close"]))
            sigs, _off = E.rule_fires(ser, board)["PRE"]
            for m in sigs:
                out.append({"day": day, "sym": sym, "m": m, "bars": bars, "ref": iex_close.get(m),
                            "iex_close": iex_close, "board": board})
    return out


def score_cell(sigs: list[dict], k: float, n: int, cost_limit: float = COST, ref_mode: str = "iex",
               through: float = THROUGH, control: bool = False) -> dict:
    """ref_mode 'iex' (graded) or 'sip' (information). control=True places the same limit at the
    harness's later-only random minute of the same name and day instead of the signal (skeptic #4)."""
    halves = {}
    for h in "AB":
        part = [s for s in sigs if (s["day"] <= SPLIT_END_A) == (h == "A")]
        fill_by, mkt_by, per_sig_by, fill_mins = defaultdict(list), defaultdict(list), defaultdict(list), Counter()
        same_by = defaultdict(list)
        ctl_sip_fallback = 0
        unfilled_mkt, filled_mkt = [], []
        n_fill = n_drop = 0
        for s in part:
            m0 = s["m"]
            if control:
                sipcl = {kk: (v[0], v[3]) for kk, v in s["bars"].items()}
                kc = E.control_minute(s["sym"], s["day"], "LIMIT", sipcl, set(), m0 + 1)
                if kc is None:
                    continue
                m0 = kc - 1
            mk = market_trade(s["bars"], m0)
            if mk is None:
                continue
            mkt_by[s["day"]].append(mk - COST)
            if control:
                # Same reference type as the graded arm (skeptic round 2 #4a): the IEX close at the
                # control's signal minute; the SIP close only when IEX has no bar there (counted).
                ref = None if ref_mode == "sip" else s["iex_close"].get(m0)
                if ref is None and ref_mode != "sip":
                    ctl_sip_fallback += 1
            else:
                ref = None if ref_mode == "sip" else s.get("ref")
            if ref_mode == "iex" and not control and ref is None:
                continue
            lt = limit_trade(s["bars"], m0, k, n, ref=ref, through=through)
            if lt.get("filled") and not lt.get("dropped"):
                n_fill += 1
                fill_by[s["day"]].append(lt["gross"] - cost_limit)
                per_sig_by[s["day"]].append(lt["gross"] - cost_limit)
                filled_mkt.append(mk - COST)
                fill_mins[lt["fill_min"] - m0] += 1
                if lt.get("gross_same_exit") is not None:
                    same_by[s["day"]].append(lt["gross_same_exit"] - cost_limit)
            else:
                n_drop += bool(lt.get("dropped"))
                per_sig_by[s["day"]].append(0.0)
                if not lt.get("filled"):
                    unfilled_mkt.append(mk - COST)
        lift_by = {d: [statistics.fmean(fill_by[d]) - statistics.fmean(mkt_by[d])]
                   for d in fill_by if fill_by[d] and mkt_by.get(d)}
        mu, t, _n, nd = E.day_equal(lift_by)
        fn, fn_t, _fnn, _fnd = E.day_equal({d: v for d, v in fill_by.items() if v})
        same = E.day_equal({d: v for d, v in same_by.items() if v})[0]
        mk_all = E.day_equal({d: v for d, v in mkt_by.items() if v})[0]
        ps = E.day_equal({d: v for d, v in per_sig_by.items() if v})[0]
        n_sig = sum(len(v) for v in mkt_by.values())
        halves[h] = {"lift": mu, "t": t, "fill_net": fn, "fill_t": fn_t, "same_exit_net": same,
                     "mkt_net": mk_all, "per_signal": ps,
                     "fills": n_fill, "signals": n_sig, "fill_rate": (n_fill / n_sig) if n_sig else None,
                     "days": nd, "dropped": n_drop,
                     "mkt_of_filled": statistics.fmean(filled_mkt) if filled_mkt else None,
                     "mkt_of_unfilled": statistics.fmean(unfilled_mkt) if unfilled_mkt else None,
                     "fill_minute": dict(sorted(fill_mins.items())), "ctl_sip_fallback": ctl_sip_fallback,
                     "verdict": half_verdict(mu, t, fn, n_fill, nd, fn_t)}
    return halves


def _bp(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def _tt(x):
    return "—" if x is None else f"{x:+.2f}"


def _pct(x):
    return "—" if x is None else f"{x:.0%}"


def report(sigs: list[dict]) -> str:
    k, n = PRIMARY
    pc = score_cell(sigs, k, n)
    L = [f"# Limit below the signal - primary k={k} bp, N={n} min", "", f"signals {len(sigs)}", "",
         "| half | signals | fills | fill rate | fill net | market net (all signals) | lift | t | verdict |",
         "|---|---:|---:|---:|---:|---:|---:|---:|---|"]
    for h, x in pc.items():
        fr = "—" if x["fill_rate"] is None else f"{x['fill_rate']:.0%}"
        L.append(f"| {h} | {x['signals']} | {x['fills']} | {fr} | {_bp(x['fill_net'])} | {_bp(x['mkt_net'])} "
                 f"| {_bp(x['lift'])} | {_tt(x['t'])} | {x['verdict']} |")
    vs = [x["verdict"] for x in pc.values()]
    final = "PASS" if all(v == "PASS" for v in vs) else ("FAIL" if "FAIL" in vs else "UNDERPOWERED")
    L += ["", f"**Limit below (10 bp, 5 min, IEX reference): {final}**", "",
          "Filled-trade net t per half: " + "; ".join(f"{h} {_tt(x['fill_t'])}" for h, x in pc.items()),
          "", "### Information only", ""]
    ctl = score_cell(sigs, k, n, control=True)
    L.append("- CONTROL (same limit at a random later minute of the same name and day): " + "; ".join(
        f"half {h} fill net {_bp(x['fill_net'])} (fill rate {_pct(x['fill_rate'])}, SIP-reference fallbacks {x['ctl_sip_fallback']})" for h, x in ctl.items())
        + " - if control fill net >= the signal's, the result is a fill-model artifact (forward test only)")
    for h, x in pc.items():
        L.append(f"- half {h}: limit fills exited at the MARKET arm's exit bar: {_bp(x['same_exit_net'])} (price gain over the same window)")
    sipref = score_cell(sigs, k, n, ref_mode="sip")
    L.append("- SIP-close reference (not available live): " + "; ".join(f"half {h} fill net {_bp(x['fill_net'])}, lift {_bp(x['lift'])}" for h, x in sipref.items()))
    th5 = score_cell(sigs, k, n, through=THROUGH_INFO)
    L.append("- 5 bp trade-through: " + "; ".join(f"half {h} fill net {_bp(x['fill_net'])}, lift {_bp(x['lift'])}" for h, x in th5.items()))
    L.append(f"- skipped name-days: {dict(SKIPS)}")
    for h, x in pc.items():
        L.append(f"- half {h}: market net of signals the limit FILLED {_bp(x['mkt_of_filled'])} vs NOT filled "
                 f"{_bp(x['mkt_of_unfilled'])} (adverse selection); per-signal value with unfilled = 0: {_bp(x['per_signal'])}; "
                 f"fill minute after signal {x['fill_minute']}; filled with no exit bar at all {x['dropped']}")
    L += ["", "| k bp | N min | half | fill rate | fill net | lift | t |", "|---:|---:|---|---:|---:|---:|---:|"]
    for kk in GRID_K:
        for nn in GRID_N:
            for h, x in score_cell(sigs, kk, nn).items():
                fr = "—" if x["fill_rate"] is None else f"{x['fill_rate']:.0%}"
                L.append(f"| {kk} | {nn} | {h} | {fr} | {_bp(x['fill_net'])} | {_bp(x['lift'])} "
                         f"| {_tt(x['t'])} |")
    c5 = score_cell(sigs, k, n, COST_LIMIT_INFO)
    L.append("")
    L.append("- primary with the limit charged 5 bp: " + "; ".join(f"half {h} fill net {_bp(x['fill_net'])}, lift {_bp(x['lift'])}" for h, x in c5.items()))
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    datas, ex = E.load_days()
    print(f"cached days {len(datas)}; excluded {ex}")
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    sigs = signals(datas)
    md = report(sigs)
    print(md)
    with open(os.path.join(E.OUT_DIR, "limit_below_result.md"), "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
