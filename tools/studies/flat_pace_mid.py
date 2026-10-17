#!/usr/bin/env python3
"""Busy but not extended, then the live mid-rise %R entry.

Spec: docs/studies/flat_pace_mid_prereg.json. One graded cell. Do not pass --score
until a result-skeptic READY on that prereg.

  .venv/bin/python tools/studies/flat_pace_mid.py
  .venv/bin/python tools/studies/flat_pace_mid.py --score --workers 6
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import early_entry_honest as E  # noqa: E402
import morning_funnel as mf  # noqa: E402
import prescreen_turn as P  # noqa: E402

PACE_LO, PACE_HI = 1.64, 4.0
FLAT = 0.01
RANGE_MAX = 0.90
PRIOR_DAYS = 20
COST3, COST10 = 0.0003, 0.0010
CUT = "2026-07-23"
WIN_FIRST, WIN_LAST, WIN_N = "2026-04-28", "2026-10-15", 119
OUT = os.path.join(ROOT, "ai_reports", "warehouse", "flat_pace_mid_result.md")


def keep(pace, day_chg, range_pos) -> bool:
    """The one graded cell. Thresholds are the prereg, not fit on this sample."""
    if pace is None or day_chg is None or range_pos is None:
        return False
    return PACE_LO <= pace < PACE_HI and -FLAT <= day_chg <= FLAT and range_pos < RANGE_MAX


def pace_of(cum_v: float, prior_vols: list[float], mins_since_open: float):
    """RTH volume so far / (mean of the prior 20 sessions x the time-of-day fraction).

    prior_vols must already exclude today. Fewer than 20 sessions -> undefined.
    """
    if len(prior_vols) < PRIOR_DAYS or cum_v < 0:
        return None
    avg = float(np.mean(prior_vols[-PRIOR_DAYS:]))
    frac = mf.expected_fraction(mins_since_open)
    if avg <= 0 or frac <= 0:
        return None
    return cum_v / (avg * frac)


def range_pos(running_high: float, running_low: float, close: float):
    """Share of today's range used by this bar. The high and low include this bar."""
    span = running_high - running_low
    if not (span > 0 and np.isfinite(close)):
        return None
    return (close - running_low) / span


def session_sip_stats(path: str) -> tuple[dict, dict]:
    """Full-session RTH SIP volume and last RTH close, per symbol. No lookahead inside the day:
    the caller may use a session only after it has closed."""
    z = np.load(path, allow_pickle=False)
    mins = z["minute"]
    rth = (mins >= E.RTH_START) & (mins < E.RTH_END)
    syms = [str(s) for s in z["syms"]]
    idx = z["sym_idx"]
    vol = np.zeros(len(syms), dtype=np.float64)
    np.add.at(vol, idx[rth], z["v"][rth].astype(np.float64))
    volumes = {syms[i]: float(vol[i]) for i in range(len(syms)) if vol[i] > 0}
    r_idx, r_min, r_c = idx[rth], mins[rth], z["c"][rth]
    order = np.argsort(-r_min, kind="stable")
    seen = np.zeros(len(syms), dtype=bool)
    last = {}
    for j in order:
        si = int(r_idx[j])
        if seen[si]:
            continue
        seen[si] = True
        last[syms[si]] = float(r_c[j])
    return volumes, last


def _sip_state(frame) -> dict:
    """minute -> (cum RTH volume, running high, running low, close), through that bar."""
    rows = []
    for t, h, lo, c, v in zip(frame["time"], frame["high"], frame["low"], frame["close"], frame["volume"]):
        m = E._et_min(t.timestamp())
        if E.RTH_START <= m < E.RTH_END:
            rows.append((m, float(h), float(lo), float(c), float(v)))
    rows.sort()
    out, cum, hi, low = {}, 0.0, None, None
    for m, h, lo, c, v in rows:
        cum += v
        hi = h if hi is None else max(hi, h)
        low = lo if low is None else min(low, lo)
        out[m] = (cum, hi, low, c)
    return out


def _finite(pace, dchg, rp, pclose) -> bool:
    """A fire is measurable only when pace, day change, range, and the prior close are finite."""
    if pclose is None or not np.isfinite(pclose) or pclose <= 0:
        return False
    return all(x is not None and np.isfinite(x) for x in (pace, dchg, rp))


def day_job(day: str, prev: str | None, prior_vols: dict, prior_close: dict) -> dict:
    # Spawned workers do not inherit a flag set in the parent. Set it here, before the series.
    E.WARM_LINES = True
    data = P.LazyDay(day, prev)
    iex, sip, prior, board = data["iex"], data["sip"], data["iex_prior"], data["board"]
    buckets = {k: [] for k in ("all", "kept", "pace", "flat", "range", "later", "cross")}
    drops = {"names": len(board), "indicator_fail": 0, "mid": 0, "no_feature": 0}
    sips = {sym: E.sip_map(frame) for sym, frame in sip.items()}
    for sym in board:
        try:
            ser = E.name_day_series(iex[sym], prior.get(sym))
        except Exception:  # noqa: BLE001
            drops["indicator_fail"] += 1
            continue
        state = _sip_state(sip[sym])
        flags = [E.mid_flag(ser["fast"], ser["slow"], ser["rsi"], i) for i in range(len(ser["minutes"]))]
        ons, _off = E.fires(ser["minutes"], flags, board[sym])
        pvols = prior_vols.get(sym) or []
        pclose = prior_close.get(sym)
        for sm in ons:
            drops["mid"] += 1
            st = state.get(sm)
            if st is None:
                drops["no_feature"] += 1
                continue
            cum, hi, lo, close = st
            pace = pace_of(cum, pvols, sm - E.RTH_START + 1)
            dchg = (close / pclose - 1.0) if (pclose is not None and np.isfinite(pclose) and pclose > 0
                                              and np.isfinite(close)) else None
            rp = range_pos(hi, lo, close)
            if not _finite(pace, dchg, rp, pclose):
                drops["no_feature"] += 1
                continue
            g = E.markout(sips[sym], sm + 1)
            if g is None or not np.isfinite(g):
                drops["no_feature"] += 1
                continue
            buckets["all"].append(g)
            if PACE_LO <= pace < PACE_HI:
                buckets["pace"].append(g)
            if -FLAT <= dchg <= FLAT:
                buckets["flat"].append(g)
            if rp < RANGE_MAX:
                buckets["range"].append(g)
            if not keep(pace, dchg, rp):
                continue
            buckets["kept"].append(g)
            km = E.control_minute(sym, day, "FLATBUSY", sips[sym], board[sym], sm + 1)
            if km is not None:
                gc = E.markout(sips[sym], km)
                if gc is not None and np.isfinite(gc):
                    buckets["later"].append(g - gc)
            xc = E.cross_control(sym, day, "FLATBUSY", board, sips, sm)
            if xc is not None and np.isfinite(xc):
                buckets["cross"].append(g - xc)
    return {"day": day, "b": buckets, "drops": drops}


def day_job_packed(payload: tuple) -> dict:
    return day_job(*payload)


def _de(vals: list[float]):
    if not vals:
        return None, None, 0
    mu = float(np.mean(vals))
    if len(vals) < 3 or float(np.std(vals, ddof=1)) == 0:
        return mu, None, len(vals)
    sd = float(np.std(vals, ddof=1))
    return mu, mu / (sd / np.sqrt(len(vals))), len(vals)


def half_rows(per_day: list[dict], dates: list[str], key: str, cut: str) -> dict:
    """Day-equal gross, net, and lift versus the same day's baseline ('all')."""
    out = {}
    for label, ds in (("A", [d for d in dates if d <= cut]), ("B", [d for d in dates if d > cut]), ("ALL", dates)):
        gross, lift, ns = [], [], 0
        by = {r["day"]: r for r in per_day}
        for d in ds:
            g = by[d]["b"][key]
            base = by[d]["b"]["all"]
            ns += len(g)
            if g:
                gross.append(float(np.mean(g)))
            if g and base:
                lift.append(float(np.mean(g)) - float(np.mean(base)))
        gm, gt, nd = _de(gross)
        lm, lt, ld = _de(lift)
        out[label] = {
            "n": ns, "days": nd, "gross": gm, "t_gross": gt,
            "net3": None if gm is None else gm - COST3,
            "net10": None if gm is None else gm - COST10,
            "lift": lm, "t_lift": lt, "lift_days": ld,
        }
    return out


def _misses_pass(h) -> bool:
    """A powered half that does not clear PASS: lift under 5 bp, t under 2, or net at 10 bp not positive."""
    if h["lift"] is None or h["lift"] < E.LIFT_BAR:
        return True
    if (h["t_lift"] or -1) < 2:
        return True
    return h["net10"] is None or h["net10"] <= 0


def verdict(kept: dict) -> str:
    """PASS / PROMISING / FAIL / UNDERPOWERED. See the prereg. `kept` is half_rows()['kept']."""
    def under(h):
        return h["n"] < E.MIN_SIGNALS or h["days"] < E.MIN_DAYS or h["gross"] is None
    a, b, both = kept["A"], kept["B"], kept["ALL"]
    if under(a) or under(b):
        # A thin half stays UNDERPOWERED only when every powered half would itself PASS.
        powered = [h for h in (a, b) if not under(h)]
        if any(_misses_pass(h) for h in powered):
            return "FAIL"
        return "UNDERPOWERED"
    lift_pos = (a["lift"] or 0) > 0 and (b["lift"] or 0) > 0
    money = (
        a["lift"] is not None and b["lift"] is not None
        and a["lift"] >= E.LIFT_BAR and b["lift"] >= E.LIFT_BAR
        and (a["t_lift"] or -1) >= 2 and (b["t_lift"] or -1) >= 2
        and a["net10"] > 0 and b["net10"] > 0 and lift_pos
    )
    if money:
        return "PASS"
    t3 = _net3_t(both)
    promising = a["net3"] > 0 and b["net3"] > 0 and lift_pos and t3 is not None and t3 >= 1.5
    return "PROMISING" if promising else "FAIL"


def _net3_t(block: dict):
    """t of the day-equal gross, shifted by a constant cost, is t of (gross - cost) only if we
    recompute it. Day-equal t of net equals (mean_gross - cost) / (sd / sqrt(n))."""
    if block["gross"] is None or block["t_gross"] in (None, 0):
        return None
    # t_gross = gross / se, so se = gross / t_gross. t_net = (gross - cost) / se.
    se = block["gross"] / block["t_gross"]
    if se == 0:
        return None
    return (block["gross"] - COST3) / se


def _bp(x) -> str:
    return "n/a" if x is None else f"{x * 1e4:+.1f}"


def _line(name: str, rows: dict) -> str:
    def cell(h):
        r = rows[h]
        tg = f"{r['t_gross']:+.2f}" if r["t_gross"] is not None else "n/a"
        tl = f"{r['t_lift']:+.2f}" if r["t_lift"] is not None else "n/a"
        return (f"{_bp(r['gross'])} (t {tg}, n {r['n']}, days {r['days']}, "
                f"net3 {_bp(r['net3'])}, net10 {_bp(r['net10'])}, lift {_bp(r['lift'])} t {tl})")
    return f"{name}: A {cell('A')} | B {cell('B')}"


def report(per_day: list[dict], dates: list[str]) -> str:
    kept = half_rows(per_day, dates, "kept", CUT)
    v = verdict(kept)
    lines = [
        "# Busy and still flat, then mid-rise %R (flat_pace_mid_prereg)",
        "",
        f"119-session window pinned; half A ends {CUT}. One graded cell.",
        "",
        f"**Kept on the prereg:** pace [1.64, 4), day change ±1%, range position < 0.90.",
        f"**Verdict:** {v}",
        "",
        _line("KEPT (graded)", kept),
        _line("ALL measurable mid-rise (baseline)", half_rows(per_day, dates, "all", CUT)),
        "",
        "Information only, not graded:",
        _line("pace only", half_rows(per_day, dates, "pace", CUT)),
        _line("flat only", half_rows(per_day, dates, "flat", CUT)),
        _line("range < 0.90 only", half_rows(per_day, dates, "range", CUT)),
    ]
    later = [float(np.mean(r["b"]["later"])) for r in per_day if r["b"]["later"]]
    cross = [float(np.mean(r["b"]["cross"])) for r in per_day if r["b"]["cross"]]
    lm, lt, ln = _de(later)
    cm, ct, cn = _de(cross)
    lines.append(f"kept vs a later minute: {_bp(lm)} (t {lt if lt is not None else float('nan'):+.2f}, days {ln})")
    lines.append(f"kept vs another name, same minute: {_bp(cm)} (t {ct if ct is not None else float('nan'):+.2f}, days {cn})")
    ind = sum(r["drops"]["indicator_fail"] for r in per_day)
    names = sum(r["drops"]["names"] for r in per_day)
    lines.append("")
    lines.append(f"Name-days {names}; indicator failures {ind} ({(ind / names * 100) if names else 0:.2f}%).")
    lines.append(f"Mid-rise onsets {sum(r['drops']['mid'] for r in per_day)}; "
                 f"dropped for a missing feature or markout {sum(r['drops']['no_feature'] for r in per_day)}.")
    return "\n".join(lines) + "\n"


def history_dates(sip_dates: list[str], usable: list[str], warm: str | None) -> list[str]:
    """Exactly one SIP session before the window, then the usable dates, in order.

    Any other SIP file before the window, inside it, or after it aborts the run.
    """
    if not usable or warm is None or not (warm < WIN_FIRST):
        raise SystemExit(f"REFUSING: need exactly one SIP session before {WIN_FIRST}")
    allowed = [warm] + list(usable)
    got = sorted(sip_dates)
    if got != allowed:
        usable_set = set(usable)
        raise SystemExit(
            "REFUSING: SIP files must be exactly one session before "
            f"{WIN_FIRST} ({warm}) plus the {len(usable)} sessions {usable[0]}..{usable[-1]}. "
            f"before {[d for d in got if d < WIN_FIRST and d != warm]}; "
            f"inside {[d for d in got if WIN_FIRST <= d <= WIN_LAST and d not in usable_set]}; "
            f"after {[d for d in got if d > WIN_LAST]}; "
            f"missing {[d for d in allowed if d not in set(got)]}"
        )
    return allowed


def _history(dates: list[str]) -> tuple[dict, dict]:
    """Prior-20 volume lists and previous-session close, keyed by the session they may be used on."""
    vols, lasts = {}, {}
    for d in dates:
        vols[d], lasts[d] = session_sip_stats(os.path.join(P.WH, "sip", f"{d}.npz"))
    prior_v, prior_c = {}, {}
    for i, d in enumerate(dates):
        bag: dict[str, list] = {}
        for prev in dates[max(0, i - PRIOR_DAYS):i]:
            for sym, v in vols[prev].items():
                bag.setdefault(sym, []).append(v)
        prior_v[d] = {s: vs for s, vs in bag.items() if len(vs) >= PRIOR_DAYS}
        prev_day = dates[i - 1] if i else None
        prior_c[d] = lasts[prev_day] if prev_day else {}
    return prior_v, prior_c


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    days = P.usable_days()
    dates = [d["day"] for d in days]
    print(f"sessions {len(dates)} {dates[0] if dates else '?'}..{dates[-1] if dates else '?'}; A ends {CUT}", flush=True)
    if len(dates) != WIN_N or not dates or dates[0] != WIN_FIRST or dates[-1] != WIN_LAST:
        raise SystemExit(f"REFUSING: window is not the pinned {WIN_N} sessions {WIN_FIRST}..{WIN_LAST}")
    if not a.score:
        print("inventory only. --score after a result-skeptic READY.", flush=True)
        print("Pace is undefined until a name has 20 prior SIP sessions, so the first warehouse sessions are in neither population.", flush=True)
        return
    print("building prior-volume history", flush=True)
    have = sorted(x[:-4] for x in os.listdir(os.path.join(P.WH, "sip")) if x.endswith(".npz"))
    prior_v, prior_c = _history(history_dates(have, dates, days[0]._prev))
    payloads = []
    for d in days:
        payloads.append((d["day"], d._prev, prior_v.get(d["day"], {}), prior_c.get(d["day"], {})))
    per = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for k, row in enumerate(ex.map(day_job_packed, payloads)):
            per.append(row)
            if k % 10 == 0:
                print(f"  {row['day']} ({k + 1}/{len(payloads)})", flush=True)
    per.sort(key=lambda r: r["day"])
    text = report(per, dates)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(text, flush=True)


if __name__ == "__main__":
    main()
