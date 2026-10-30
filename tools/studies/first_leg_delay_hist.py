#!/usr/bin/env python3
"""first_leg_delay_hist.py — docs/studies/first_leg_delay_hist_prereg.json.

Events: pitch_open empty-square fires on warehouse IEX 1-minute lines, fires on 585..920 with the 15-minute refractory,
kept for t 585..910; entry base e = t + 1 at e:00. Sessions 2026-05-11..2026-09-25. Draw: per session, events in
sha256('FLD'|day|symbol|t) order, SIP quote at e:00, mid $20-$200 kept, 30 kept per session (no usable e:00 quote =
kept as missing). For d in {0, 2, 5}: entry (e+d):00, exit (e+d+3):00; gross = mid ratio, net = gross - full SIP
spread at entry.

  run     fetch, write rows.json, then count (VOID / power) and score the verdicts. Stops launching sessions at
          --stop-at ET (default 09:15) on a weekday so it never overlaps the session; a stopped run is INCOMPLETE.
USAGE (mini): .venv/bin/python tools/studies/first_leg_delay_hist.py run [--workers 3] [--stop-at 09:15]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import random
import statistics
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import move_to_cost_hist as M  # noqa: E402
import pitch_open as P  # noqa: E402
import std_scorer as S  # noqa: E402

OUT = Path(os.path.expanduser("~/replay_runs/first_leg_delay"))
T_LO, T_HI, PER_SESSION, DELAYS, HOLD = 585, 910, 30, (0, 2, 5), 3
MAX_MISSING, MAX_ERROR, MAX_HALF_WIDTH, MARGIN = 0.10, 0.05, 3.0, 2.0


def key(day: str, sym: str, t: int) -> str:
    return hashlib.sha256(f"FLD|{day}|{sym}|{t}".encode()).hexdigest()


def day_events(args) -> dict:
    day, prev_day = args
    import indicator_confirm_hist as H
    lf_today, lf_prev = H.load_raw("iex", day), H.load_raw("iex", prev_day)
    sip_raw, sip_prev = H.load_raw("sip", day), H.load_raw("sip", prev_day)
    st, out = Counter(), []
    for sym in sorted(lf_today):
        if sym not in lf_prev or not P.series_ok(lf_today[sym]) or not P.series_ok(lf_prev[sym]):
            st["bad_line_series"] += 1
            continue
        if sym in sip_prev and sym in sip_raw and len(sip_prev[sym]) and len(sip_raw[sym]):
            pc, op = float(sip_prev[sym][-1][4]), float(sip_raw[sym][0][1])
            if pc > 0 and abs(op / pc - 1) > P.SPLIT_JUMP:
                st["split_jump"] += 1
                continue
        fast, slow = P.lines(lf_prev[sym], lf_today[sym])
        for t in P.fires(lambda m: P.empty_square(fast, slow, m)):
            if T_LO <= t <= T_HI:
                out.append({"day": day, "symbol": sym, "t": t, "key": key(day, sym, t)})
    return {"events": out, "stats": dict(st)}


def measure(ev: dict, data, q0, delays=DELAYS) -> dict:
    """Fill gross/net per delay; q0 is the already-fetched e:00 quote."""
    e = ev["t"] + 1
    row = dict(ev)
    for d in delays:
        try:
            qa = q0 if d == 0 else data.quote(ev["symbol"], M.minute_ts(ev["day"], e + d))
            qb = data.quote(ev["symbol"], M.minute_ts(ev["day"], e + d + HOLD))
        except M.MH.FetchError:
            row[f"err{d}"] = True
            continue
        ma, sp = M.mid_spread(qa)
        mb, spx = M.mid_spread(qb)
        if ma is None or mb is None:
            continue
        g = (mb / ma - 1) * 1e4
        # amended F3: half the spread at entry + half at exit against a mid-to-mid gross; full-at-entry secondary
        row[f"gross{d}"], row[f"spread{d}"], row[f"net{d}"] = g, sp, g - (sp + spx) / 2
        row[f"net_full{d}"] = g - sp
    return row


def draw_session(events, data, per_session=PER_SESSION):
    kept, st = [], Counter()
    for ev in sorted(events, key=lambda r: r["key"]):
        if len(kept) >= per_session:
            break
        try:
            q0 = data.quote(ev["symbol"], M.minute_ts(ev["day"], ev["t"] + 1))
        except M.MH.FetchError:
            st["error"] += 1
            kept.append(dict(ev, status="error"))
            continue
        mid0, _ = M.mid_spread(q0)
        if mid0 is None:
            st["entry_quote_missing"] += 1
            kept.append(dict(ev, status="missing"))
            continue
        if not 20.0 <= mid0 <= 200.0:
            st["price_skip"] += 1
            continue
        kept.append(dict(measure(ev, data, q0), status="ok", mid0=mid0))
    return kept, st


# ── statistics (pure) ───────────────────────────────────────────────────────────────────────────────────────────────
def per_session(rows, f):
    per: dict[str, list[float]] = {}
    for r in rows:
        v = f(r)
        if v is not None:
            per.setdefault(r["day"], []).append(v)
    return [statistics.fmean(v) for _, v in sorted(per.items())]


def stat(vals):
    st = S.day_stat(vals)
    n = st["n"]
    tc = M.MH.t_crit(n - 1, 0.975) if n >= 2 else float("inf")
    se = st.get("se")
    ok = se is not None and se == se
    return {"n": n, "mean": st["mean"], "t": st["t"], "lo": st["mean"] - tc * se if ok else None,
            "hi": st["mean"] + tc * se if ok else None, "half_width": tc * se if ok else None}


def half(day):
    return "A" if day <= "2026-07-29" else "B"


def get(k):
    return lambda r: r.get(k)


def cost(k):
    return lambda r: (r["gross0"] - r[f"gross{k}"]) if "gross0" in r and f"gross{k}" in r else None


def signal_verdict(out: dict, control_lift: dict | None) -> dict:
    """amended F1: one signal label, SPEED_MATTERS alongside (VOID per F6 when d=2 pairs are too thin)."""
    n0, c2 = out["net0"], out["cost2"]
    lo, hi = n0["all"]["lo"], n0["all"]["hi"]
    halves_pos = n0["A"]["mean"] > 0 and n0["B"]["mean"] > 0
    if lo is not None and lo > 0 and halves_pos:
        if control_lift is None:
            label = "CONTROL_NEEDED"
        elif control_lift["A"]["mean"] > 0 and control_lift["B"]["mean"] > 0:
            label = "EDGE_BELOW_MARGIN" if hi is not None and hi < MARGIN else "EDGE_AT_SIGNAL"
        else:
            label = "NOT_ABOVE_CONTROL"
    elif hi is not None and hi < MARGIN:
        label = "NO_EDGE_EVEN_AT_SIGNAL"
    else:
        label = "UNCLEAR"
    sm = c2["all"]["lo"] is not None and c2["all"]["lo"] > 0 and c2["A"]["mean"] > 0 and c2["B"]["mean"] > 0
    if out.get("speed_void"):
        speed = "VOID (> 10% of kept events lack the d=2 pair)"
    elif not sm:
        speed = "not shown"
    elif n0["all"]["mean"] <= 0:
        speed = "SPEED_MATTERS: waiting is worse, but the signal is not profitable"
    else:
        speed = "SPEED_MATTERS"
    return {"signal": label, "speed": speed, "control_lift": control_lift}


def verdicts(rows, control_lift: dict | None = None) -> dict:
    ok = [r for r in rows if r.get("status") == "ok"]
    out = {"speed_void": missing_share(rows, 2) > MAX_MISSING}
    for name, f in (("net0", get("net0")), ("gross0", get("gross0")), ("net2", get("net2")), ("net5", get("net5")),
                    ("cost2", cost(2)), ("cost5", cost(5))):
        out[name] = {"all": stat(per_session(ok, f)),
                     **{h: stat(per_session([r for r in ok if half(r["day"]) == h], f)) for h in ("A", "B")}}
    out["net_full0"] = {"all": stat(per_session(ok, get("net_full0")))}
    out.update(signal_verdict(out, control_lift))
    early = [r for r in ok if r["t"] <= 630]
    late = [r for r in ok if r["t"] > 630]
    out["info_early_09_45_10_30"] = {k: stat(per_session(early, f)) for k, f in (("net0", get("net0")), ("cost2", cost(2)))}
    out["info_rest_of_day"] = {k: stat(per_session(late, f)) for k, f in (("net0", get("net0")), ("cost2", cost(2)))}
    return out


def missing_share(rows, d) -> float:
    n = len(rows)
    return sum(not (r.get("status") == "ok" and f"net{d}" in r) for r in rows) / n if n else 1.0


def counts(rows) -> dict:
    n = len(rows)
    miss0 = sum(r.get("status") in ("missing", "error") or (r.get("status") == "ok" and "net0" not in r) for r in rows)
    err = sum(r.get("status") == "error" or any(r.get(f"err{d}") for d in DELAYS) for r in rows)
    ok = [r for r in rows if r.get("status") == "ok" and "net0" in r]
    hw = stat(per_session(ok, get("net0")))["half_width"]
    hw2 = stat(per_session(ok, cost(2)))["half_width"]
    c = {"kept": n, "scored_d0": len(ok), "sessions": len({r["day"] for r in ok}),
         "by_half": {h: sum(half(r["day"]) == h for r in ok) for h in ("A", "B")},
         "missing_share_d0": miss0 / n if n else None, "error_share": err / n if n else None,
         "net0_half_width": hw, "cost2_half_width": hw2,
         "missing_share_by_delay": {d: missing_share(rows, d) for d in DELAYS},
         "by_half_sessions": {h: len({r["day"] for r in ok if half(r["day"]) == h}) for h in ("A", "B")}}
    c["void"] = n == 0 or (c["missing_share_d0"] or 0) > MAX_MISSING or (c["error_share"] or 0) > MAX_ERROR
    c["underpowered"] = hw is None or hw > MAX_HALF_WIDTH or hw2 is None or hw2 > MAX_HALF_WIDTH
    return c


def control_minute(ev: dict, fires: list[int]):
    """amended F5: same symbol and session, same clock hour, >= 15 min from any fire of that symbol, seed FLD-CTL."""
    h0 = (ev["t"] // 60) * 60
    cand = [m for m in range(max(h0, T_LO), min(h0 + 60, T_HI + 1)) if all(abs(m - f) >= 15 for f in fires)]
    if not cand:
        return None
    return random.Random(f"FLD-CTL|{ev['day']}|{ev['symbol']}|{ev['t']}").choice(cand)


def control_rows(rows, fires_by, data) -> list[dict]:
    out = []
    for r in rows:
        if r.get("status") != "ok" or "net0" not in r:
            continue
        c = control_minute(r, fires_by.get(f"{r['day']}|{r['symbol']}", []))
        if c is None:
            continue
        try:
            q0 = data.quote(r["symbol"], M.minute_ts(r["day"], c + 1))
        except M.MH.FetchError:
            continue
        m = measure({"day": r["day"], "symbol": r["symbol"], "t": c}, data, q0, delays=(0,))
        if "net0" in m:
            out.append({"day": r["day"], "lift": r["net0"] - m["net0"]})
    return out


def lift_stat(ctl) -> dict:
    return {"all": stat(per_session(ctl, get("lift"))),
            **{h: stat(per_session([r for r in ctl if half(r["day"]) == h], get("lift"))) for h in ("A", "B")}}


def past(stop_at: str) -> bool:
    import zoneinfo
    now = _dt.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    h, m = (int(x) for x in stop_at.split(":"))
    return now.weekday() < 5 and (h, m) <= (now.hour, now.minute) < (16, 0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("run",))
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--stop-at", default="09:15")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    data = M.QuoteData.live()
    data.dir = M.OUT / "cache"            # same quote rule and key space as move_to_cost_hist
    rows, st, run, done, fires_by = [], Counter(), M.sessions(), 0, {}
    run = [(d, p) for d, p in run if M.FIRST <= d <= M.LAST]
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for i, res in enumerate(ex.map(day_events, run, chunksize=2), 1):
            if past(a.stop_at):
                print(f"stopped at {a.stop_at} ET after {done} sessions", file=sys.stderr)
                break
            for ev in res["events"]:
                fires_by.setdefault(f"{ev['day']}|{ev['symbol']}", []).append(ev["t"])
            kept, s2 = draw_session(res["events"], data)
            rows += kept
            st.update(s2)
            done = i
            print(f"session {i}/{len(run)} {run[i - 1][0]}: events {len(res['events'])} kept {len(kept)} "
                  f"calls {data.calls}", file=sys.stderr, flush=True)
    (OUT / "rows.json").write_text(json.dumps(rows, default=str))
    c = counts(rows)
    c.update({"sessions_run": done, "sessions_planned": len(run), "stats": dict(st), "quote_calls": data.calls,
              "complete": done == len(run)})
    res = {"count": c}
    if c["complete"] and not c["void"] and not c["underpowered"]:
        res["result"] = verdicts(rows)
        if res["result"]["signal"] == "CONTROL_NEEDED" and not past(a.stop_at):
            ctl = control_rows(rows, fires_by, data)
            res["result"] = verdicts(rows, lift_stat(ctl))
    else:
        res["result"] = "INCOMPLETE" if not c["complete"] else ("VOID" if c["void"] else "UNDERPOWERED")
    (OUT / "result.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
