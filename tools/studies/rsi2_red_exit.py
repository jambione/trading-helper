#!/usr/bin/env python3
"""RSI-2 'red' as an exit for the desk's own trades. Spec: docs/studies/rsi2_red_exit_prereg.json.

For every desk fill on the cached sessions (outcomes.jsonl, entry_path 'watch'), walk the completed raw IEX
1-minute bars after the entry (prior-session warm-up as early_entry_honest.con_live_series). The first bar m
(starting at or after the entry minute) with RSI-2 > 90 exits at the SIP open of m+1 - if that is before the
desk's own exit minute. Otherwise the trade keeps the desk's exit. Both exits are priced on SIP bars, so the
entry cancels: lift = red exit / desk exit - 1 per trade (0 when red never fired first).

Control: the same number of early exits at a random minute between entry and the desk's exit (seeded), so a
pass is not just 'any earlier exit' (holding longer loses more on this book).

  (default)  inventory: trades, red fires; no returns
  --score    after a result-skeptic READY
Cache only (ai_reports/early_entry_honest/<day>.pkl with iex_prior) + ai_reports/outcomes.jsonl.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import early_entry_honest as E  # noqa: E402
import limit_below_study as L  # noqa: E402

RED = 90.0
INFO_LEVELS = (80.0, 95.0)
MIN_FIRED, MIN_DAYS = 60, 6
LIFT_BAR = 0.0003


# ── pure ─────────────────────────────────────────────────────────────────────

def rsi_by_minute(prior_frame, today_frame) -> tuple[dict, dict]:
    """({minute: RSI-2}, {minute: (sma200, sma5, close)}) on the raw IEX series with prior warm-up."""
    pc = [float(x) for x in prior_frame["close"]][-E.CON_PRIOR_BARS:] if prior_frame is not None else []
    tc = [float(x) for x in today_frame["close"]]
    closes = pc + tc
    rsi = E.rsi2_series(closes)
    mins = [E._et_min(t.timestamp()) for t in today_frame["time"]]
    out, avg = {}, {}
    for k, m in enumerate(mins):
        i = len(pc) + k
        if rsi[i] is not None:
            out[m] = rsi[i]
        if i + 1 >= 200:
            avg[m] = (sum(closes[i - 199:i + 1]) / 200, sum(closes[i - 4:i + 1]) / 5, closes[i])
    return out, avg


def red_exit_minute(rsi: dict, entry_min: int, exit_min: int, level: float = RED,
                    avg: dict | None = None, strict: bool = False) -> int | None:
    """SIP minute to sell at (m+1) for the first bar m in [entry_min, exit_min - 2] with RSI-2 > level;
    None if red never comes before the desk's exit. strict = the panel's mirror (close < SMA200, > SMA5)."""
    for m in range(entry_min, exit_min - 1):
        r = rsi.get(m)
        if r is None or r <= level:
            continue
        if strict:
            a = (avg or {}).get(m)
            if a is None or not (a[2] < a[0] and a[2] > a[1]):
                continue
        return m + 1
    return None


def control_exit_minute(sym: str, day: str, entry_min: int, exit_min: int) -> int | None:
    lo, hi = entry_min + 1, exit_min - 1
    if hi < lo:
        return None
    return E._rng(sym, day, entry_min, "RED_CTRL").randint(lo, hi)


# ── scoring ──────────────────────────────────────────────────────────────────

def trades(datas: list[dict]) -> list[dict]:
    days = {d["day"] for d in datas}
    out = []
    if not os.path.exists(E.OUTCOMES):
        return out
    for line in open(E.OUTCOMES):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        et, xt = r.get("entry_time"), r.get("exit_time")
        if not et or not xt or r.get("entry_path") != "watch" or not r.get("total_qty"):
            continue
        day = datetime.fromtimestamp(float(et), E.ET).strftime("%Y-%m-%d")
        if day in days:
            out.append({"sym": str(r["symbol"]).upper(), "day": day,
                        "entry_min": E._et_min(float(et)), "exit_min": E._et_min(float(xt))})
    return out


def score(datas: list[dict], level: float = RED, strict: bool = False) -> dict:
    by = {d["day"]: d for d in datas}
    skips = Counter()
    res = {h: {"red": defaultdict(list), "ctl": defaultdict(list), "all": defaultdict(list), "fired": 0, "n": 0}
           for h in "AB"}
    cache: dict = {}
    for t in trades(datas):
        data = by[t["day"]]
        iex, sipf = data["iex"].get(t["sym"]), data["sip"].get(t["sym"])
        if iex is None or sipf is None:
            skips["no_bars"] += 1
            continue
        key = (t["day"], t["sym"])
        if key not in cache:
            cache[key] = (rsi_by_minute((data.get("iex_prior") or {}).get(t["sym"]), iex), L.ohlc_map(sipf))
        (rsi, avg), sip = cache[key]
        base_bar = sip.get(t["exit_min"])
        if base_bar is None or t["exit_min"] <= t["entry_min"]:
            skips["no_exit_bar_or_same_minute"] += 1
            continue
        h = "A" if t["day"] <= L.SPLIT_END_A else "B"
        res[h]["n"] += 1
        base = base_bar[3]
        rm = red_exit_minute(rsi, t["entry_min"], t["exit_min"], level, avg, strict)
        if rm is None or sip.get(rm) is None:
            res[h]["all"][t["day"]].append(0.0)
            continue
        cm = control_exit_minute(t["sym"], t["day"], t["entry_min"], t["exit_min"])
        if cm is None or sip.get(cm) is None:
            skips["no_control_bar"] += 1
            res[h]["all"][t["day"]].append(0.0)
            continue
        red_l, ctl_l = sip[rm][0] / base - 1, sip[cm][0] / base - 1
        res[h]["fired"] += 1
        res[h]["red"][t["day"]].append(red_l - ctl_l)
        res[h]["ctl"][t["day"]].append(ctl_l)
        res[h]["all"][t["day"]].append(red_l)
    out = {}
    for h, x in res.items():
        vs, vs_t, _n, nd = E.day_equal(x["red"])
        al, al_t, _, _ = E.day_equal(x["all"])
        ok_n = x["fired"] >= MIN_FIRED and nd >= MIN_DAYS
        verdict = ("UNDERPOWERED" if not ok_n or None in (vs, vs_t, al) else
                   "PASS" if vs >= LIFT_BAR and vs_t >= 2.0 and al > 0 else "FAIL")
        out[h] = {"vs_control": vs, "vs_t": vs_t, "all_trades": al, "all_t": al_t, "fired": x["fired"],
                  "trades": x["n"], "days": nd, "control_alone": E.day_equal(x["ctl"])[0], "verdict": verdict}
    out["skips"] = dict(skips)
    return out


def _bp(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    datas, ex = E.load_days()
    datas = [d for d in datas if d["day"] <= "2026-10-13"]
    miss = [(d["day"], round(E.prior_miss_share(d), 3)) for d in datas if E.prior_miss_share(d) > E.CON_PRIOR_MAX_MISS]
    datas = [d for d in datas if E.prior_miss_share(d) <= E.CON_PRIOR_MAX_MISS]
    tr = trades(datas)
    print(f"days {len(datas)}; excluded for prior-bar coverage {miss}; desk trades {len(tr)}")
    if not a.score:
        print("(inventory only; --score after a result-skeptic READY)")
        return
    L_ = ["# RSI-2 red as an exit (RSI-2 > 90 on a completed IEX bar, sell next SIP open)", "",
          "| half | trades | red fired first | red - random early exit, per fired trade (t) | per trade, all (t) | random early exit alone | verdict |",
          "|---|---:|---:|---:|---:|---:|---|"]
    r = score(datas)
    for h in "AB":
        x = r[h]
        L_.append(f"| {h} | {x['trades']} | {x['fired']} | {_bp(x['vs_control'])} ({x['vs_t'] and round(x['vs_t'], 2)}) "
                  f"| {_bp(x['all_trades'])} ({x['all_t'] and round(x['all_t'], 2)}) | {_bp(x['control_alone'])} | {x['verdict']} |")
    vs = [r[h]["verdict"] for h in "AB"]
    L_ += ["", f"**Red exit: {'PASS' if all(v == 'PASS' for v in vs) else 'FAIL' if 'FAIL' in vs else 'UNDERPOWERED'}**",
           f"skips {r['skips']}", "", "Information:"]
    for lvl in INFO_LEVELS:
        x = score(datas, lvl)
        L_.append(f"- RSI-2 > {lvl:.0f}: " + "; ".join(f"{h} vs random {_bp(x[h]['vs_control'])}, all {_bp(x[h]['all_trades'])}, fired {x[h]['fired']}" for h in "AB"))
    x = score(datas, RED, strict=True)
    L_.append("- panel-strict red (also close < SMA200 and > SMA5): " + "; ".join(f"{h} vs random {_bp(x[h]['vs_control'])}, fired {x[h]['fired']}" for h in "AB"))
    out = "\n".join(L_)
    print(out)
    with open(os.path.join(E.OUT_DIR, "rsi2_red_exit_result.md"), "w") as f:
        f.write(out + "\n")


if __name__ == "__main__":
    main()
