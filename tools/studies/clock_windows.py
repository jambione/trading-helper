#!/usr/bin/env python3
"""Do some clock times make better entries? Spec: docs/studies/clock_windows_prereg.json.

Buy alerted names (first alert before the window) at the SIP open of a window minute, sell 5 minutes later,
pay the full SIP spread; compare with a random minute of the same name-day within 30 minutes of the window.
USAGE (mini, after 16:15 ET, after momentum_alert_markouts.py has cached the bars):
  .venv/bin/python tools/studies/clock_windows.py
"""
from __future__ import annotations

import hashlib
import os
import random
import statistics as st
import sys
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]
import momentum_alert_markouts as M  # noqa: E402

ET = ZoneInfo("America/New_York")
HOLD, INFO_HOLD, SPAN = 5, 10, 30
T_BAR, MIN_N, MIN_DAYS = 2.5, 60, 8


def _m(h, mi):
    return h * 60 + mi


WINDOWS = {f"w{h:02d}00": (_m(h, 0), _m(h, 5)) for h in (5, 6, 7, 8, 9, 10, 11)}
WINDOWS["w0830"] = (_m(8, 30), _m(8, 35))
WINDOWS["w0930_avoid"] = (_m(9, 30), _m(9, 35))
POWER = (_m(15, 0), _m(15, 55))
POWER_CTL = (_m(13, 0), _m(14, 55))
WORSE = {"w0930_avoid"}


# ── pure ─────────────────────────────────────────────────────────────────────

def ret(b: dict, m: int, hold: int) -> float | None:
    """Open of minute m to the close of minute m + hold - 1 (no fill-in for missing bars)."""
    a, z = b.get(m), b.get(m + hold - 1)
    if not a or not z or not a[0]:
        return None
    return z[1] / a[0] - 1


def _rng(*parts) -> random.Random:
    return random.Random(int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big"))


def window_minutes(b: dict, lo: int, hi: int, hold: int = HOLD) -> list[int]:
    return [m for m in range(lo, hi) if ret(b, m, hold) is not None]


def control_minute(b: dict, day: str, sym: str, name: str, lo: int, hi: int, hold: int = HOLD) -> int | None:
    """Random minute within SPAN of the window start, outside the window, with an exit bar."""
    elig = [m for m in range(lo - SPAN, lo + SPAN) if not lo <= m < hi and ret(b, m, hold) is not None]
    return _rng(day, sym, name).choice(elig) if elig else None


def power_control(b: dict, day: str, sym: str, hold: int = HOLD) -> int | None:
    elig = [m for m in range(*POWER_CTL) if ret(b, m, hold) is not None]
    return _rng(day, sym, "power").choice(elig) if elig else None


def day_t(by_day: dict) -> tuple[float | None, float | None, int]:
    means = [st.fmean(v) for v in by_day.values() if v]
    if len(means) < 3:
        return (st.fmean(means) if means else None), None, len(means)
    mu, sd = st.fmean(means), st.stdev(means)
    return mu, (mu / (sd / len(means) ** 0.5) if sd > 0 else None), len(means)


def verdict(name: str, lift_t, halves: list[float | None], n: int, days: int) -> str:
    if n < MIN_N or days < MIN_DAYS:
        return "UNDERPOWERED"
    sign = -1 if name in WORSE else 1
    if lift_t is None or sign * lift_t < T_BAR or any(h is None or sign * h <= 0 for h in halves):
        return "FAIL"
    return "PASS"


# ── run ──────────────────────────────────────────────────────────────────────

def _ts(day: str, m: int) -> float:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    return d.replace(hour=m // 60, minute=m % 60).timestamp()


def main() -> None:
    import replay_costing as RC
    cache, box = RC.load_cache(), {}
    ev = M.events()
    days = sorted(ev)
    mid = days[(len(days) - 1) // 2] if days else ""
    names = list(WINDOWS) + ["power_hour"]
    win = {k: defaultdict(list) for k in names}           # day -> [net bp]
    lift = {k: defaultdict(list) for k in names}
    info10 = {k: [] for k in names}
    absmv = {k: [] for k in names}
    skips = Counter()

    def spread(sym, day, m):
        sp = RC.entry_spread_bp(cache, sym, _ts(day, m), box)
        if sp is None:
            skips["no_nbbo"] += 1
        return sp

    for day in days:
        first = {}
        for kind, s, ts in ev[day]:
            if kind == "first_alert":
                first[s] = M.et_min(ts)
        bars = M.fetch_bars(day, sorted(first))
        for s, fm in first.items():
            b = bars.get(s)
            if not b:
                skips["no_bars"] += 1
                continue
            spans = {k: v for k, v in WINDOWS.items()}
            spans["power_hour"] = POWER
            for k, (lo, hi) in spans.items():
                if fm >= lo:
                    continue                                  # not yet on the list at the window
                for m in window_minutes(b, lo, hi):
                    c = power_control(b, day, f"{s}{m}") if k == "power_hour" else \
                        control_minute(b, day, s, f"{k}{m}", lo, hi)
                    if c is None:
                        skips[f"{k}_no_control"] += 1
                        continue
                    sw, sc = spread(s, day, m), spread(s, day, c)
                    if sw is None or sc is None:
                        continue
                    w = ret(b, m, HOLD) * 1e4 - sw
                    cn = ret(b, c, HOLD) * 1e4 - sc
                    win[k][day].append(w)
                    lift[k][day].append(w - cn)
                    r10 = ret(b, m, INFO_HOLD)
                    if r10 is not None:
                        info10[k].append(r10 * 1e4 - sw)
                    absmv[k].append(abs(ret(b, m, HOLD)) * 1e4)
        RC.save_cache(cache)

    lines = ["# Clock windows: entering alerted names at set times (SIP, 5-min hold, full spread)", "",
             f"days {len(days)} ({days[0] if days else '-'} .. {days[-1] if days else '-'}), split after {mid}; skips {dict(skips)}", "",
             "| window | n | days | net 5m day-mean | lift vs ±30m (t) | lift halves | net 10m mean | mean |move| | per day | verdict |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for k in names:
        n = sum(len(v) for v in win[k].values())
        mu, _, nd = day_t(win[k])
        lm, lt, _ = day_t(lift[k])
        h1, _, _ = day_t({d: v for d, v in lift[k].items() if d <= mid})
        h2, _, _ = day_t({d: v for d, v in lift[k].items() if d > mid})
        v = verdict(k, lt, [h1, h2], n, nd)
        if v == "PASS" and (mu is None or mu <= 0):
            v = "PASS (least bad, net < 0)"
        f = lambda x: "-" if x is None else f"{x:+.1f}"  # noqa: E731
        lines.append(f"| {k} | {n} | {nd} | {f(mu)} | {f(lm)} ({f(lt)}) | {f(h1)} / {f(h2)} | "
                     f"{f(st.fmean(info10[k]) if info10[k] else None)} | {f(st.fmean(absmv[k]) if absmv[k] else None)} | "
                     f"{n / max(nd, 1):.1f} | {v} |")
    out = "\n".join(lines)
    print(out)
    with open(os.path.join(ROOT, "ai_reports", "momentum_alerts", "clock_windows_result.md"), "w") as fh:
        fh.write(out + "\n")


if __name__ == "__main__":
    main()
