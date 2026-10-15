#!/usr/bin/env python3
"""Do some clock times make better entries? Spec: docs/studies/clock_windows_prereg.json (amended_1 after review).

One entry per name per window: buy alerted names (first alert before the window) at the SIP open of the first
minute in the window that prints, sell at the last SIP close at or before 4 minutes later (5-minute hold), pay
the full SIP spread 10 s into the entry minute. Control: one random minute of the same name-day AFTER its first
alert, within 30 minutes of the window, on the same side of 09:30 (09:30 itself: 09:35-10:04), outside every
window, scored the same way. Power hour: 2 random minutes per name in 15:00-15:54 vs 13:00-14:54.
USAGE (mini, after 16:15 ET, after momentum_alert_markouts.py has cached the bars):
  .venv/bin/python tools/studies/clock_windows.py
"""
from __future__ import annotations

import hashlib
import os
import random
import statistics as st
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.getcwd()
sys.path[:0] = [os.path.join(ROOT, "tools", "studies"), os.path.join(ROOT, "tools"), ROOT]
import momentum_alert_markouts as M  # noqa: E402

ET = ZoneInfo("America/New_York")
HOLD, INFO_HOLD, SPAN = 5, 10, 30
T_BAR, MIN_N, MIN_DAYS = 3.0, 60, 8
POWER_PICKS = 2
OPEN = 9 * 60 + 30
QUOTE_DELAY = 10          # seconds into the entry minute (09:30: after the opening cross)


def _m(h, mi):
    return h * 60 + mi


WINDOWS = {f"w{h:02d}00": (_m(h, 0), _m(h, 5)) for h in (5, 6, 7, 8, 9, 10, 11)}
WINDOWS["w0830"] = (_m(8, 30), _m(8, 35))
WINDOWS["w0930_avoid"] = (_m(9, 30), _m(9, 35))
POWER = (_m(15, 0), _m(15, 55))
POWER_CTL = (_m(13, 0), _m(14, 55))
WORSE = {"w0930_avoid"}
IN_ANY_WINDOW = {m for lo, hi in WINDOWS.values() for m in range(lo, hi)}


# ── pure ─────────────────────────────────────────────────────────────────────

def ret(b: dict, m: int, hold: int) -> float | None:
    """Open of minute m (must print) to the last close at or before m + hold - 1 (no later than the hold)."""
    a = b.get(m)
    if not a or not a[0]:
        return None
    c = M.close_at_or_before(b, m + hold - 1, back=hold - 1)
    return c / a[0] - 1 if c else None


def _rng(*parts) -> random.Random:
    return random.Random(int.from_bytes(hashlib.sha256("|".join(map(str, parts)).encode()).digest()[:8], "big"))


def window_entry(b: dict, lo: int, hi: int, hold: int = HOLD) -> int | None:
    """The first minute of the window that prints (the clock-tick entry)."""
    return next((m for m in range(lo, hi) if ret(b, m, hold) is not None), None)


def control_span(name: str, lo: int) -> range:
    if name == "w0930_avoid":
        return range(OPEN + 5, OPEN + 35)
    span = range(lo - SPAN, lo + SPAN)
    pre = lo < OPEN
    return range(max(span.start, 0) if pre else max(span.start, OPEN + 5),
                 min(span.stop, OPEN) if pre else span.stop)


def control_minute(b: dict, day: str, sym: str, name: str, lo: int, first_alert: int,
                   hold: int = HOLD) -> int | None:
    """Random minute after the name's first alert, near the window, same session side, outside every window."""
    elig = [m for m in control_span(name, lo)
            if m > first_alert and m not in IN_ANY_WINDOW and ret(b, m, hold) is not None]
    return _rng(day, sym, name).choice(elig) if elig else None


def power_minutes(b: dict, day: str, sym: str, first_alert: int, span: tuple, tag: str,
                  k: int = POWER_PICKS, hold: int = HOLD) -> list[int]:
    elig = [m for m in range(*span) if m > first_alert and ret(b, m, hold) is not None]
    r = _rng(day, sym, tag)
    return sorted(r.sample(elig, min(k, len(elig))))


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


# ── quotes (errors are never cached as "no spread") ──────────────────────────

class QuoteError(Exception):
    pass


def spread_bp(cache: dict, box: dict, sym: str, ts: float) -> float | None:
    key = f"{sym}|{ts:.0f}"
    if key not in cache:
        import bars
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockQuotesRequest
        box["cl"] = box.get("cl") or bars.client()
        t = datetime.fromtimestamp(ts, timezone.utc)
        q = None
        for back in (2, 60):              # short window first: results come oldest first and stop at the limit
            for attempt in range(3):
                try:
                    rows = box["cl"].get_stock_quotes(StockQuotesRequest(
                        symbol_or_symbols=sym, start=t - timedelta(seconds=back), end=t + timedelta(milliseconds=1),
                        feed=DataFeed.SIP, limit=10000)).data.get(sym) or []
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 2:
                        raise QuoteError(str(e)[:80]) from e
                    time.sleep(5 * (attempt + 1))
            rows = [r for r in rows if r.bid_price and r.ask_price and r.ask_price >= r.bid_price]
            if len(rows) >= 10000 and back > 2:
                raise QuoteError("quote limit hit: latest quote not reached")
            if rows:
                q = [float(rows[-1].bid_price), float(rows[-1].ask_price)]
                break
        cache[key] = q
        time.sleep(0.3)
    q = cache[key]
    if not q or q[1] <= q[0] or q[0] <= 0:
        return None
    return (q[1] - q[0]) / ((q[0] + q[1]) / 2) * 1e4


# ── run ──────────────────────────────────────────────────────────────────────

def _ts(day: str, m: int) -> float:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    return d.replace(hour=m // 60, minute=m % 60).timestamp() + QUOTE_DELAY


def main() -> None:
    import replay_costing as RC
    cache, box = RC.load_cache(), {}
    ev = M.events()
    days = [d for d in sorted(ev) if any(k == "first_alert" for k, _s, _t in ev[d])]
    names = list(WINDOWS) + ["power_hour"]
    win = {k: defaultdict(list) for k in names}           # day -> [net bp]
    lift = {k: defaultdict(list) for k in names}
    lift_early = {k: defaultdict(list) for k in names}    # first alert > SPAN before the window
    info10 = {k: [] for k in names}
    absmv = {k: [] for k in names}
    tried = Counter()
    drops = defaultdict(Counter)                          # window -> reason
    skips, day_notes = Counter(), []

    def leg(s, day, m):
        sp = spread_bp(cache, box, s, _ts(day, m))
        if sp is None:
            raise LookupError("no_nbbo")
        r5, r10 = ret(b, m, HOLD), ret(b, m, INFO_HOLD)
        return r5 * 1e4 - sp, (None if r10 is None else r10 * 1e4 - sp), abs(r5) * 1e4

    used_days = []
    for day in days:
        first = {}
        for kind, s, ts in ev[day]:
            if kind == "first_alert":
                first[s] = M.et_min(ts)
        bars_ = M.fetch_bars(day, sorted(first))
        if not os.path.exists(os.path.join(M.CACHE, f"{day}.pkl")):
            day_notes.append(f"{day}: dropped, the SIP bar fetch failed (partial day, not cached)")
            continue
        if min(first.values()) >= OPEN:
            day_notes.append(f"{day}: no premarket alerts (OCR started {min(first.values()) // 60:02d}:"
                             f"{min(first.values()) % 60:02d})")
        used_days.append(day)
        for s, fm in first.items():
            b = bars_.get(s)
            if not b:
                skips["no_bars"] += 1
                continue
            pairs = []                                    # (window, entry, control, early)
            for k, (lo, hi) in WINDOWS.items():
                if fm >= lo:
                    continue                              # not yet on the list at the window
                tried[k] += 1
                m = window_entry(b, lo, hi)
                if m is None:
                    drops[k]["window_no_print"] += 1
                    continue
                c = control_minute(b, day, s, k, lo, fm)
                if c is None:
                    drops[k]["no_control"] += 1
                    continue
                pairs.append((k, m, c, fm < lo - SPAN))
            if fm < POWER[0]:
                tried["power_hour"] += 1
                pw = power_minutes(b, day, s, fm, POWER, "power")
                pc = power_minutes(b, day, s, fm, POWER_CTL, "power_ctl", k=len(pw))
                if not pw or len(pc) < len(pw):
                    drops["power_hour"]["no_print_or_control"] += 1
                pairs += [("power_hour", m, c, fm < POWER_CTL[0] - SPAN) for m, c in zip(pw, pc)]
            for k, m, c, early in pairs:
                try:
                    w, w10, mv = leg(s, day, m)
                    cn, _c10, _ = leg(s, day, c)
                except LookupError:
                    drops[k]["no_nbbo"] += 1
                    continue
                except QuoteError:
                    drops[k]["quote_error"] += 1
                    continue
                win[k][day].append(w)
                lift[k][day].append(w - cn)
                if early:
                    lift_early[k][day].append(w - cn)
                if w10 is not None:
                    info10[k].append(w10)
                absmv[k].append(mv)
        RC.save_cache(cache)

    used_days = [d for d in used_days if any(d in win[k] and win[k][d] for k in names)]
    mid = used_days[(len(used_days) - 1) // 2] if used_days else ""
    f = lambda x: "-" if x is None else f"{x:+.1f}"  # noqa: E731
    lines = ["# Clock windows: entering alerted names at set times (SIP, 5-min hold, full spread)", "",
             f"days used {len(used_days)} ({used_days[0] if used_days else '-'} .. {used_days[-1] if used_days else '-'}), "
             f"split after {mid}; t bar {T_BAR} (10 windows); skips {dict(skips)}", ""]
    lines += [f"- {n}" for n in day_notes] + [""]
    lines += ["| window | name-windows | days | net 5m day-mean | lift vs control (t) | lift halves | net 10m mean | "
              "mean abs move | per day | lift, alerted >30m before | drops | verdict |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    for k in names:
        n = sum(len(v) for v in win[k].values())
        mu, _, nd = day_t(win[k])
        lm, lt, _ = day_t(lift[k])
        h1, _, _ = day_t({d: v for d, v in lift[k].items() if d <= mid})
        h2, _, _ = day_t({d: v for d, v in lift[k].items() if d > mid})
        le, _, _ = day_t(lift_early[k])
        v = verdict(k, lt, [h1, h2], n, nd)
        sign = -1 if k in WORSE else 1
        if v == "PASS" and (le is None or sign * le <= 0):
            v = "FAIL (not on names alerted >30 min earlier)"
        if v == "PASS" and k not in WORSE and (mu is None or mu <= 0):
            v = "PASS (least bad, net < 0)"
        dr = ", ".join(f"{r} {c}" for r, c in drops[k].items()) or "-"
        lines.append(f"| {k} | {n}/{tried[k]} | {nd} | {f(mu)} | {f(lm)} ({f(lt)}) | {f(h1)} / {f(h2)} | "
                     f"{f(st.fmean(info10[k]) if info10[k] else None)} | {f(st.fmean(absmv[k]) if absmv[k] else None)} | "
                     f"{n / max(nd, 1):.1f} | {f(le)} | {dr} | {v} |")
    lines += ["", "Feed note: all SIP. The live desk is on IEX, which is blind premarket; a premarket PASS is tradeable "
              "only with real-time SIP (the SIP month)."]
    out = "\n".join(lines)
    print(out)
    with open(os.path.join(ROOT, "ai_reports", "momentum_alerts", "clock_windows_result.md"), "w") as fh:
        fh.write(out + "\n")


if __name__ == "__main__":
    main()
