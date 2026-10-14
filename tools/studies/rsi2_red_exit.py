#!/usr/bin/env python3
"""RSI-2 'red' as an exit for the desk's own trades. Spec: docs/studies/rsi2_red_exit_prereg.json.

For every desk fill on the cached sessions (outcomes.jsonl, entry_path 'watch'), walk the completed raw IEX
1-minute bars after the entry (prior-session warm-up as early_entry_honest.con_live_series). The first bar m
(starting at or after the entry minute) with RSI-2 > 90 exits at the SIP open of m+1 - if that is before the
desk's own exit minute. Otherwise the trade keeps the desk's exit. Both exits are priced on SIP bars, so the
entry cancels: lift = red exit / desk exit - 1 per trade (0 when red never fired first).

Control: a hazard-matched random exit - at each bar offset after the entry it exits with red's own fire rate at
that offset (bars at risk), so a pass is not just 'any earlier exit' (holding longer loses more on this book).

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
sys.path.insert(0, os.path.dirname(HERE))
import early_entry_honest as E  # noqa: E402
import limit_below_study as L  # noqa: E402

RED = 90.0
INFO_LEVELS = (80.0, 95.0)
MIN_FIRED, MIN_DAYS = 60, 6
LIFT_BAR = 0.0002     # per ELIGIBLE trade vs the hazard-matched null (skeptic round 1 #1, #8)


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
            out.append({"sym": str(r["symbol"]).upper(), "day": day, "entry_ts": float(et), "exit_ts": float(xt),
                        "entry_min": E._et_min(float(et)), "exit_min": E._et_min(float(xt)),
                        "exit_price": float(r["exit_price"]) if r.get("exit_price") else None})
    return out


def _bin(k: int) -> int:
    return min(k, 3)


def hazard_exit_minute(sym: str, day: str, start_min: int, exit_min: int, p) -> int | None:
    """Hazard-matched null: at each eligible bar m in [start, exit-2], exit at m+1 with red's fire rate at that
    bar offset (p: {0, 1, 2, 3(+)} -> rate, or one float). Seeded per trade."""
    rng = E._rng(sym, day, start_min, "RED_HAZARD")
    for m in range(start_min, exit_min - 1):
        pk = p.get(_bin(m - start_min), 0.0) if isinstance(p, dict) else float(p)
        if rng.random() < pk:
            return m + 1
    return None


def hazard_by_offset(rows: list[dict]) -> dict:
    """Red's fire rate per bar offset over BARS AT RISK (skeptic round 2 #1a-b): a fired trade is at risk from its
    start bar through the firing bar; one that never fired, through exit-2."""
    risk, fire = Counter(), Counter()
    for r in rows:
        last = (r["rm"] - 1) if r["rm"] is not None else (r["exit_min"] - 2)
        for m in range(r["start"], last + 1):
            risk[_bin(m - r["start"])] += 1
        if r["rm"] is not None:
            fire[_bin(r["rm"] - 1 - r["start"])] += 1
    return {k: (fire[k] / risk[k]) if risk[k] else 0.0 for k in range(4)}


def prepare(datas: list[dict], level: float, strict: bool, min_m_offset: int = 0) -> tuple[list[dict], Counter]:
    """Eligible trades (exit - entry >= 2 minutes, bars present) with red's exit minute; skips counted."""
    by = {d["day"]: d for d in datas}
    skips, out, cache = Counter(), [], {}
    for t in trades(datas):
        data = by[t["day"]]
        iex, sipf = data["iex"].get(t["sym"]), data["sip"].get(t["sym"])
        if iex is None or sipf is None:
            skips["no_bars"] += 1
            continue
        if t["exit_min"] - t["entry_min"] < 2:
            skips["hold_under_2_minutes"] += 1
            continue
        if t["exit_price"] is None:
            skips["no_exit_price"] += 1
            continue
        key = (t["day"], t["sym"])
        if key not in cache:
            prior = (data.get("iex_prior") or {}).get(t["sym"])
            if prior is None:
                skips["traded_name_without_prior_bars"] += 1
            cache[key] = (rsi_by_minute(prior, iex), L.ohlc_map(sipf))
        (rsi, avg), sip = cache[key]
        start = t["entry_min"] + min_m_offset
        if t["exit_min"] - start < 2:
            skips["hold_under_2_minutes_after_offset"] += 1
            continue
        rm = red_exit_minute(rsi, start, t["exit_min"], level, avg, strict)
        out.append({**t, "rm": rm, "sip": sip, "start": start})
    return out, skips


def score(datas: list[dict], level: float = RED, strict: bool = False, lag: int = 0, spread_of=None,
          min_m_offset: int = 0) -> dict:
    """lag=1 sells at m+2 (live latency, skeptic #5). spread_of(sym, ts) -> full SIP spread in bp or None;
    red and control exits are charged half of it (a sell fills at the bid, skeptic #3)."""
    rows, skips = prepare(datas, level, strict, min_m_offset)
    p = hazard_by_offset(rows)
    nbbo = Counter()
    res = {h: {"diff": defaultdict(list), "all": defaultdict(list), "fired": 0, "n": 0, "entry_bar": 0}
           for h in "AB"}

    def px(r, minute, arm):
        b = r["sip"].get(minute + lag)
        if b is None:
            return None
        half = 0.0
        if spread_of is not None:
            # The exit minute's open + 1 s, not the entry's second-of-minute (skeptic round 2 #3).
            ts = (r["entry_ts"] // 60) * 60 + (minute + lag - r["entry_min"]) * 60.0 + 1.0
            sp = spread_of(r["sym"], ts)
            nbbo[arm + "_n"] += 1
            if sp is None:
                nbbo[arm + "_missing"] += 1
            else:
                half = sp / 2e4
        return b[0] * (1 - half)

    for r in rows:
        h = "A" if r["day"] <= L.SPLIT_END_A else "B"
        base = r["exit_price"]
        cm = hazard_exit_minute(r["sym"], r["day"], r["start"], r["exit_min"], p)
        red_px = px(r, r["rm"], "red") if r["rm"] is not None else base
        ctl_px = px(r, cm, "hazard") if cm is not None else base
        if red_px is None or ctl_px is None:
            skips["exit_bar_missing"] += 1
            continue
        res[h]["n"] += 1
        if r["rm"] is not None:
            res[h]["fired"] += 1
            res[h]["entry_bar"] += r["rm"] == r["entry_min"] + 1
        res[h]["diff"][r["day"]].append(red_px / base - ctl_px / base)
        res[h]["all"][r["day"]].append(red_px / base - 1)
    miss = {a: (nbbo[a + "_missing"] / nbbo[a + "_n"]) if nbbo[a + "_n"] else 0.0 for a in ("red", "hazard")}
    out = {"p_fire_per_bar": p, "skips": dict(skips), "nbbo_missing": miss,
           "aborted": spread_of is not None and max(miss.values()) > 0.05}
    for h, x in res.items():
        d, d_t, _n, nd = E.day_equal(x["diff"])
        al, al_t, _, _ = E.day_equal(x["all"])
        ok = x["fired"] >= MIN_FIRED and nd >= MIN_DAYS and None not in (d, d_t, al)
        verdict = ("NO VERDICT (NBBO missing > 5% in an arm)" if out["aborted"] else "UNDERPOWERED" if not ok else
                   "PASS" if d >= LIFT_BAR and d_t >= 2.0 and al > 0 else "FAIL")
        out[h] = {"vs_hazard": d, "vs_t": d_t, "all_trades": al, "all_t": al_t, "fired": x["fired"],
                  "trades": x["n"], "days": nd, "entry_bar_share": (x["entry_bar"] / x["fired"]) if x["fired"] else None,
                  "verdict": verdict}
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
    import replay_costing as RC
    cache, box = RC.load_cache(), {}
    spread_of = lambda sym, ts: RC.entry_spread_bp(cache, sym, ts, box)  # noqa: E731
    L_ = ["# RSI-2 red as an exit (RSI-2 > 90 on a completed IEX bar, sell next SIP open less half the SIP spread)", "",
          "| half | eligible trades | red fired | fired on the entry bar | red - hazard-matched random exit, per eligible trade (t) | red vs desk fill, per trade (t) | verdict |",
          "|---|---:|---:|---:|---:|---:|---|"]
    r = score(datas, spread_of=spread_of)
    for h in "AB":
        x = r[h]
        eb = "—" if x["entry_bar_share"] is None else f"{x['entry_bar_share']:.0%}"
        L_.append(f"| {h} | {x['trades']} | {x['fired']} | {eb} | {_bp(x['vs_hazard'])} ({x['vs_t'] and round(x['vs_t'], 2)}) "
                  f"| {_bp(x['all_trades'])} ({x['all_t'] and round(x['all_t'], 2)}) | {x['verdict']} |")
    vs = [r[h]["verdict"] for h in "AB"]
    L_ += ["", f"**Red exit: {'PASS' if all(v == 'PASS' for v in vs) else 'FAIL' if 'FAIL' in vs else 'UNDERPOWERED'}**",
           f"red fire rate by bar offset (bars at risk) {r['p_fire_per_bar']}; NBBO missing by arm {r['nbbo_missing']}; skips {r['skips']}",
           "", "Information:"]
    for lbl, kw in (("sell one minute later (m+2, live latency)", {"lag": 1}),
                    ("red only from the bar after the entry bar", {"min_m_offset": 1}),
                    ("no spread charge (SIP open)", {"spread_of": None}),
                    ("RSI-2 > 80", {"level": 80.0}), ("RSI-2 > 95", {"level": 95.0}),
                    ("panel-strict red (also < SMA200, > SMA5)", {"strict": True})):
        kw = {"spread_of": spread_of, **kw}
        x = score(datas, **kw)
        L_.append(f"- {lbl}: " + "; ".join(f"{h} vs hazard {_bp(x[h]['vs_hazard'])} (t {x[h]['vs_t'] and round(x[h]['vs_t'], 2)}), vs fill {_bp(x[h]['all_trades'])}, fired {x[h]['fired']}" for h in "AB"))
    RC.save_cache(cache)
    out = "\n".join(L_)
    print(out)
    with open(os.path.join(E.OUT_DIR, "rsi2_red_exit_result.md"), "w") as f:
        f.write(out + "\n")


if __name__ == "__main__":
    main()
