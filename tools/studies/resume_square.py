#!/usr/bin/env python3
"""Buy the next square after a shallow, still-high pullback.

Spec: docs/studies/resume_square_prereg.json. One graded cell. Do not pass
--score until a result-skeptic READY on that prereg. Uses 6 workers. Do not
start it while another 6-worker warehouse job is running.

  .venv/bin/python tools/studies/resume_square.py
  .venv/bin/python tools/studies/resume_square.py --score --workers 6
"""
from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, HERE)

import early_entry_honest as E  # noqa: E402
import prescreen_turn as P  # noqa: E402

# Published display EMAs (config.py) and the published square / mid levels.
EMA_SHORT, EMA_LONG = 8, 21
HH = 21
MIN_INDEX = 41          # 42nd bar of today: windows [i-41, i-21] and [i-20, i]
COST3, COST10 = 0.0003, 0.0010
CUT = "2026-07-23"
WIN_FIRST, WIN_LAST, WIN_N = "2026-04-28", "2026-10-15", 119
OUT = os.path.join(ROOT, "ai_reports", "warehouse", "resume_square_result.md")
_COLS = ["time", "open", "high", "low", "close", "volume"]


def ema_series(close, span: int) -> list:
    """signals.ema: ewm(span, adjust=False). A non-finite close clears the state."""
    alpha = 2.0 / (span + 1.0)
    out = []
    prev = None
    for v in close:
        if not E._fin(v):
            out.append(None)
            prev = None
            continue
        prev = float(v) if prev is None else alpha * float(v) + (1.0 - alpha) * prev
        out.append(prev)
    return out


def _as_minute(m):
    if type(m) is not int:
        return None
    return m


def _pullback_ok(fast, low, close, i: int, prev: int) -> bool:
    """Every bar strictly between the prior square and this onset stays above -50.

    The onset close is above that pullback's lowest low. The onset low does not
    break it. One in-between bar is enough. A longer minimum would be a new cell.
    """
    if i - prev < 2:
        return False
    pb = None
    for k in range(prev + 1, i):
        if not E._fin(fast[k]) or fast[k] <= E.MID_LEVEL or not E._fin(low[k]):
            return False
        pb = low[k] if pb is None else min(pb, low[k])
    if pb is None or not (E._fin(close[i]) and E._fin(low[i])):
        return False
    return close[i] > pb and low[i] >= pb


def _trend_ok(i: int, close, high, ema_s, ema_l) -> bool:
    """Today's bars only. Close above EMA(8) above EMA(21), and a higher 21-bar high."""
    if i < MIN_INDEX:
        return False
    if not (E._fin(close[i]) and E._fin(ema_s[i]) and E._fin(ema_l[i])):
        return False
    if not (close[i] > ema_s[i] and ema_s[i] > ema_l[i]):
        return False
    w1 = high[i - 41:i - 20]
    w2 = high[i - 20:i + 1]
    if len(w1) != HH or len(w2) != HH:
        return False
    if not all(E._fin(x) for x in w1) or not all(E._fin(x) for x in w2):
        return False
    return max(w2) > max(w1)


def scan(minutes, fast, slow, high, low, close) -> dict:
    """fires, structural window minutes, and every square onset in the window.

    A square on a bar we were not given does not count. The caller passes
    today's bars only, so a square that existed only yesterday is invisible.
    EMA is computed on these closes, not on the warm-up bars.
    """
    n = len(minutes)
    empty = {"fires": [], "structural": [], "onsets": []}
    if n == 0 or not (len(fast) == len(slow) == len(high) == len(low) == len(close) == n):
        return empty
    ema_s = ema_series(close, EMA_SHORT)
    ema_l = ema_series(close, EMA_LONG)
    sq = [E.sq_flag(fast, slow, i) for i in range(n)]
    fires, structural, onsets = [], [], []
    last_sq = None
    last_fire = None
    for i in range(n):
        if not sq[i]:
            continue
        onset = i == 0 or not sq[i - 1]
        prev = last_sq
        last_sq = i
        m = _as_minute(minutes[i])
        in_win = m is not None and E.WIN_START <= m <= E.WIN_END
        if onset and in_win:
            onsets.append(m)
        if not onset or prev is None or not _pullback_ok(fast, low, close, i, prev):
            continue
        if not _trend_ok(i, close, high, ema_s, ema_l):
            continue
        if not in_win:
            continue
        structural.append(m)
        if last_fire is not None and m - last_fire < E.REFRACT:
            continue
        fires.append(m)
        last_fire = m
    return {"fires": fires, "structural": structural, "onsets": onsets}


def resume_onsets(minutes, fast, slow, high, low, close) -> list[int]:
    return scan(minutes, fast, slow, high, low, close)["fires"]


def other_onset_minutes(scanned: dict) -> list[int]:
    """Square onsets in the window that are not this resumption.

    A resumption the 15-minute refractory held back stays out of this list.
    It is the same rule, so it cannot be the contrast.
    """
    struct = set(scanned["structural"])
    return [m for m in scanned["onsets"] if m not in struct]


def tail_prior(frame):
    """Last 220 rows of the previous session. None when there is nothing to warm."""
    if frame is None or len(frame) == 0:
        return None
    return frame.tail(E.CON_PRIOR_BARS).reset_index(drop=True)


def warmed_series(iex_frame, prior_frame):
    """Today's %R, high, low, and close from one frame.

    The warm-up concat is here, in the worker, and does not read E.WARM_LINES.
    Rows before today's first bar are dropped before anything is returned, so
    the EMA and the higher-high window cannot see yesterday.
    """
    import pandas as pd
    import signals
    prior = tail_prior(prior_frame)
    if prior is None or iex_frame is None or len(iex_frame) == 0:
        return None
    day0 = pd.to_datetime(iex_frame["time"], utc=True).min()
    both = pd.concat([prior[_COLS], iex_frame[_COLS]], ignore_index=True)
    both = both.assign(time=pd.to_datetime(both["time"], utc=True)).sort_values("time")
    both = both.drop_duplicates("time", keep="last").reset_index(drop=True)
    df = signals.compute_percent_r_exhaustion(E.grid_fill(both), E.CFG)
    df = signals.compute_cm_rsi_lower(df, E.CFG)
    df = df[pd.to_datetime(df["time"], utc=True) >= day0].reset_index(drop=True)
    if df.empty:
        return None
    return {
        "minutes": [E._et_min(t.timestamp()) for t in df["time"]],
        "fast": [float(x) for x in df["s_percentR"]],
        "slow": [float(x) for x in df["l_percentR"]],
        "high": [float(x) for x in df["high"]],
        "low": [float(x) for x in df["low"]],
        "close": [float(x) for x in df["close"]],
    }


def universe_names(iex: dict, sip: dict, prior: dict) -> list[str]:
    """Every name with today's IEX, today's SIP, and the previous session's IEX.

    Not the 400-name sample. A name missing any of the three is not scored.
    """
    return sorted(s for s in iex if s in sip and s in prior)


def day_job(day: str, prev: str | None) -> dict:
    # Spawned workers do not inherit a flag set in the parent.
    E.WARM_LINES = True
    iex = P._frames(os.path.join(P.WH, "iex", f"{day}.npz"))
    sip_frames = P._frames(os.path.join(P.WH, "sip", f"{day}.npz"))
    prior = P._frames(os.path.join(P.WH, "iex", f"{prev}.npz")) if prev else {}
    names = universe_names(iex, sip_frames, prior)
    gross, later, other, cross = [], [], [], []
    pending = []
    sips = {}
    drops = {"names": len(names), "indicator_fail": 0, "no_markout": 0, "no_control": 0}
    first_error = None
    for sym in names:
        try:
            ser = warmed_series(iex[sym], prior.get(sym))
            if not ser:
                drops["indicator_fail"] += 1
                continue
            sips[sym] = E.sip_map(sip_frames[sym])
            scanned = scan(ser["minutes"], ser["fast"], ser["slow"], ser["high"], ser["low"], ser["close"])
        except Exception as exc:  # noqa: BLE001 — one bad name does not drop the day
            drops["indicator_fail"] += 1
            if first_error is None:
                first_error = f"{sym}: {type(exc).__name__}: {exc}"[:200]
            continue
        sip = sips[sym]
        for m in other_onset_minutes(scanned):
            g = E.markout(sip, m + 1)
            if g is not None and math.isfinite(g):
                other.append(g)
        for m in scanned["fires"]:
            g = E.markout(sip, m + 1)
            if g is None or not math.isfinite(g):
                drops["no_markout"] += 1
                continue
            km = E.control_minute(sym, day, "RESUME", sip, set(), m + 1)
            gc = None if km is None else E.markout(sip, km)
            if gc is None or not math.isfinite(gc):
                drops["no_control"] += 1
                continue
            gross.append(g)
            later.append(g - gc)
            pending.append((sym, m, g))
    del iex, sip_frames, prior
    rth = set(range(E.RTH_START, E.RTH_END))
    boards = {sym: rth for sym in sips}
    for sym, m, g in pending:
        xc = E.cross_control(sym, day, "RESUME", boards, sips, m)
        if xc is not None and math.isfinite(xc):
            cross.append(g - xc)
    return {
        "day": day, "gross": gross, "later": later, "other": other, "cross": cross,
        "drops": drops, "error": first_error,
    }


def day_job_packed(payload: tuple) -> dict:
    return day_job(*payload)


def day_mean_t(vals: list[float]):
    if not vals:
        return None, None, 0
    mu = float(statistics.mean(vals))
    if len(vals) < 3:
        return mu, None, len(vals)
    sd = float(statistics.stdev(vals))
    if sd == 0:
        return mu, None, len(vals)
    return mu, mu / (sd / math.sqrt(len(vals))), len(vals)


def half_rows(per_day: list[dict], dates: list[str]) -> dict:
    """Day-equal gross and the graded lift, which is the paired later minute."""
    by = {r["day"]: r for r in per_day}
    out = {}
    for label, pred in (
        ("A", lambda d: d <= CUT),
        ("B", lambda d: d > CUT),
        ("ALL", lambda d: True),
    ):
        gross_means, lift_means = [], []
        ns = 0
        for d in dates:
            if not pred(d):
                continue
            g = by[d]["gross"]
            lat = by[d]["later"]
            ns += len(g)
            if g:
                gross_means.append(float(statistics.mean(g)))
            if lat:
                lift_means.append(float(statistics.mean(lat)))
        gm, gt, nd = day_mean_t(gross_means)
        lm, lt, ld = day_mean_t(lift_means)
        out[label] = {
            "n": ns, "days": nd, "gross": gm, "t_gross": gt,
            "net3": None if gm is None else gm - COST3,
            "net10": None if gm is None else gm - COST10,
            "lift": lm, "t_lift": lt, "lift_days": ld,
        }
    return out


def _net3_t(block: dict):
    if block["gross"] is None or block["t_gross"] in (None, 0):
        return None
    se = block["gross"] / block["t_gross"]
    if se == 0:
        return None
    return (block["gross"] - COST3) / se


def _misses_pass(h: dict) -> bool:
    if h["lift"] is None or h["lift"] < E.LIFT_BAR:
        return True
    if (h["t_lift"] or -1) < 2:
        return True
    return h["net10"] is None or h["net10"] <= 0


def verdict(kept: dict) -> str:
    """PASS / PROMISING / FAIL / UNDERPOWERED. The lift is the later-minute lift."""
    def under(h):
        return h["n"] < E.MIN_SIGNALS or h["days"] < E.MIN_DAYS or h["gross"] is None
    a, b = kept["A"], kept["B"]
    if under(a) or under(b):
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
    t3 = _net3_t(kept["ALL"])
    promising = (
        a["net3"] is not None and b["net3"] is not None
        and a["net3"] > 0 and b["net3"] > 0 and lift_pos
        and t3 is not None and t3 >= 1.5
    )
    return "PROMISING" if promising else "FAIL"


def _bp(x) -> str:
    return "n/a" if x is None else f"{x * 1e4:+.1f}"


def _t(x) -> str:
    return "n/a" if x is None else f"{x:+.2f}"


def _line(name: str, rows: dict) -> str:
    def cell(h):
        r = rows[h]
        return (f"{_bp(r['gross'])} (t {_t(r['t_gross'])}, n {r['n']}, days {r['days']}, "
                f"net3 {_bp(r['net3'])}, net10 {_bp(r['net10'])}, "
                f"later {_bp(r['lift'])} t {_t(r['t_lift'])})")
    return f"{name}: A {cell('A')} | B {cell('B')}"


def _info_means(per_day: list[dict], dates: list[str], kind: str) -> str:
    by = {r["day"]: r for r in per_day}
    parts = []
    for label, pred in (("A", lambda d: d <= CUT), ("B", lambda d: d > CUT)):
        means = []
        for d in dates:
            if not pred(d):
                continue
            if kind == "cross":
                xs = by[d]["cross"]
                if xs:
                    means.append(float(statistics.mean(xs)))
            else:
                g, o = by[d]["gross"], by[d]["other"]
                if g and o:
                    means.append(float(statistics.mean(g)) - float(statistics.mean(o)))
        mu, t, n = day_mean_t(means)
        parts.append(f"{label} {_bp(mu)} (t {_t(t)}, days {n})")
    return " | ".join(parts)


def report(per_day: list[dict], dates: list[str]) -> str:
    kept = half_rows(per_day, dates)
    lines = [
        "# Resumption square after a shallow pullback (resume_square_prereg)",
        "",
        f"119-session window pinned; half A ends {CUT}. One graded cell.",
        "",
        "**Kept on the prereg:** second square, fast %R stayed above -50 on the pullback,",
        "price still above that low, close > EMA(8) > EMA(21), higher 21-bar high.",
        f"**Verdict:** {verdict(kept)}",
        "",
        _line("RESUME (graded)", kept),
        "",
        "Information only, not graded:",
        "lift versus other square onsets: " + _info_means(per_day, dates, "other"),
        "versus another name, same minute: " + _info_means(per_day, dates, "cross"),
    ]
    names = sum(r["drops"]["names"] for r in per_day)
    ind = sum(r["drops"]["indicator_fail"] for r in per_day)
    lines.append("")
    lines.append(f"Name-days {names}; indicator failures {ind} ({(ind / names * 100) if names else 0:.2f}%).")
    lines.append(f"Dropped for a missing markout {sum(r['drops']['no_markout'] for r in per_day)}; "
                 f"missing later-minute control {sum(r['drops']['no_control'] for r in per_day)}.")
    errs = [r["error"] for r in per_day if r.get("error")]
    if errs:
        lines.append(f"First indicator error: {errs[0]}")
    return "\n".join(lines) + "\n"


def require_window(dates: list[str]) -> None:
    got = f"{len(dates)} {dates[0] if dates else '?'}..{dates[-1] if dates else '?'}"
    if len(dates) != WIN_N or not dates or dates[0] != WIN_FIRST or dates[-1] != WIN_LAST:
        raise SystemExit(
            f"REFUSING: window is not the pinned {WIN_N} sessions {WIN_FIRST}..{WIN_LAST} (got {got})"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    days = P.usable_days()
    dates = [d["day"] for d in days]
    print(f"sessions {len(dates)} {dates[0] if dates else '?'}..{dates[-1] if dates else '?'}; A ends {CUT}",
          flush=True)
    require_window(dates)
    if not a.score:
        print("inventory only. --score after a result-skeptic READY.", flush=True)
        print("Universe is every name with today's IEX, today's SIP, and prior IEX. Not the 400-name sample.",
              flush=True)
        return
    payloads = [(d["day"], d._prev) for d in days]
    per = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for k, row in enumerate(ex.map(day_job_packed, payloads)):
            per.append(row)
            if k % 10 == 0:
                print(f"  {row['day']} ({k + 1}/{len(payloads)}) fires {len(row['gross'])}", flush=True)
    per.sort(key=lambda r: r["day"])
    text = report(per, dates)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print(text, flush=True)


if __name__ == "__main__":
    main()
