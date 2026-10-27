#!/usr/bin/env python3
"""Marked %R turn with the frozen minute trail. Spec: docs/studies/pitch_trough_prereg.json.

One predicate. Lines and the ladder come from pitch_open. The walk comes from
pitch_trail and is not re-coded. PRIMARY lines are IEX. Prices are SIP.

  (default)  count step. Trades, days, MDE, skips. No means.
  --canary   add 10 bp to a copy of the trade gross. Print the change only.
  --score    the ladder. Needs the round-2 READY id, the floors, and canary_ok.
  --lines sip  information run. It has no pass of its own.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pitch_open as P  # noqa: E402
import pitch_trail as T  # noqa: E402

READY_ID = "01a17a4d-d270-7b11-90de-7414ce3b2597"
TAGS = ("ptrough-d", "ptrough-d2", "ptrough-x", "ptrough-x2")


def prereg_path() -> str:
    return os.path.join(P.ROOT, "docs", "studies", "pitch_trough_prereg.json")


def prereg_sha() -> str:
    return hashlib.sha256(open(prereg_path(), "rb").read()).hexdigest()


def module_shas() -> dict:
    d = os.path.dirname(os.path.abspath(__file__))
    names = ("pitch_trough", "pitch_trail", "pitch_open", "eod_reversal", "indicator_confirm_hist")
    return {
        m: hashlib.sha256(open(os.path.join(d, f"{m}.py"), "rb").read()).hexdigest()
        for m in names
    }


def pitch5(fast: dict, slow: dict, t: int):
    """Tenths of the five-clock-minute rate, or None when t-5 is not a real minute."""
    if t not in fast or (t - 5) not in fast or t not in slow or (t - 5) not in slow:
        return None
    fp = round((fast[t] - fast[t - 5]) / 5.0, 1)
    sp = round((slow[t] - slow[t - 5]) / 5.0, 1)
    return P.tenths(fp), P.tenths(sp)


def turn(fast: dict, slow: dict, t: int) -> bool:
    """Trough, marked shape, and a five-minute pitch that does not say no."""
    prev = P.pair(fast, slow, t - 1)
    if prev is None or prev[0] > 0 or prev[1] > 0:
        return False
    fs = P.pair(fast, slow, t)
    if fs is None or t not in fast or t not in slow:
        return False
    f, s = fs
    if not (f > 0 and s > 0 and fast[t] < slow[t] and f + 5 >= s):
        return False
    p5 = pitch5(fast, slow, t)
    if p5 is None:
        return False
    return p5[0] > 0 and p5[1] > 0


def decide(bars: dict, t: int, walk):
    """Fill when t+1 has a positive open and the walk has a price. Else a miss."""
    e = t + 1
    b = bars.get(e)
    if b is None or len(b) != 4 or b[0] <= 0:
        return ("miss", "missing_entry")
    got = walk(e)
    if got is None:
        return ("miss", "missing_exit")
    px, exit_m, reason = got
    return ("fill", px, exit_m, reason)


def take(onsets, classify, control_fn):
    """Scored fills, and everything else.

    A fill locks through its exit minute inside run_clock, including when
    control_fn returns None and the fill is not scored.
    """
    scored, other = [], []
    for event in T.run_clock(onsets, classify):
        if event[0] == "miss":
            other.append(event)
            continue
        _, t, px, exit_m, reason = event
        grosses = control_fn(t)
        if grosses is None:
            other.append(("control", t))
            continue
        scored.append((t, px, exit_m, reason, grosses))
    return scored, other


def check_rows(rows) -> None:
    for r in rows:
        if r.get("pop") != "primary":
            raise SystemExit("predicate partition broken")
        f, s = r.get("f"), r.get("s")
        if not (
            f is not None and s is not None and f > 0 and s > 0 and f + 5 >= s
            and r.get("fast_under") is True
            and r.get("pf") is not None and r.get("ps") is not None
            and r["pf"] <= 0 and r["ps"] <= 0
            and r.get("f5") is not None and r.get("s5") is not None
            and r["f5"] > 0 and r["s5"] > 0
        ):
            raise SystemExit("predicate partition broken")


def out_dir_for(lines: str) -> str:
    name = "pitch_trough" if lines == "iex" else "pitch_trough_sip_lines"
    return os.path.join(P.ROOT, "ai_reports", name)


def write_json(path: str, obj, pretty: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        if pretty:
            json.dump(obj, f, indent=1, default=str)
        else:
            json.dump(obj, f, separators=(",", ":"), default=str)
    os.replace(tmp, path)


def day_task(args) -> dict:
    day, prev_day, line_feed = args
    import indicator_confirm_hist as H
    sip_raw = H.load_raw("sip", day)
    lf_today = H.load_raw(line_feed, day)
    lf_prev = H.load_raw(line_feed, prev_day)
    sip_prev = H.load_raw("sip", prev_day)
    sips = {
        s: {int(r[0]): (float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows}
        for s, rows in sip_raw.items()
    }
    pool = sorted(sips)
    closes = {s: T.close_minutes(sips[s]) for s in pool}
    half = P.half_of(day)
    st, out = Counter(), []
    hold_caches: dict = {}
    walks: dict = {}
    x_lists: dict = {}

    def hold_of(sym):
        c = hold_caches.get(sym)
        if c is None:
            c = T.build_hold_cache(sips[sym])
            hold_caches[sym] = c
        return c

    def walk(sym, minute):
        key = (sym, minute)
        if key not in walks:
            walks[key] = T.trail_exit(sips[sym], minute, hold_of(sym))
        return walks[key]

    def gross_of(sym, minute):
        b = sips[sym].get(minute)
        got = walk(sym, minute)
        if got is None or b is None or len(b) != 4 or b[0] <= 0:
            return None
        return got[0] / b[0] - 1.0

    def x_at(entry):
        names = x_lists.get(entry)
        if names is None:
            names = T.other_names(sips, pool, "", entry, closes)
            x_lists[entry] = names
        return names

    for sym in pool:
        if sym not in lf_today:
            st[f"{half}|no_line_feed"] += 1
            continue
        if sym not in lf_prev:
            st[f"{half}|no_previous_session"] += 1
            continue
        if not P.series_ok(lf_today[sym]) or not P.series_ok(lf_prev[sym]):
            st[f"{half}|bad_line_series"] += 1
            continue
        if sym in sip_prev and len(sip_prev[sym]) and len(sip_raw[sym]):
            pc, op = float(sip_prev[sym][-1][4]), float(sip_raw[sym][0][1])
            if pc > 0 and abs(op / pc - 1) > P.SPLIT_JUMP:
                st[f"{half}|split_jump"] += 1
                continue
        fast, slow = P.lines(lf_prev[sym], lf_today[sym])
        if not any(P.DEC_LO <= m <= P.DEC_HI for m in fast):
            st[f"{half}|no_warmup"] += 1
            continue
        bars = sips[sym]

        def classify(t, _sym=sym, _bars=bars):
            return decide(_bars, t, lambda minute, _s=_sym: walk(_s, minute))

        def control_fn(t, _sym=sym, _bars=bars):
            e = t + 1
            d = T.pick_gross(
                T.same_name_minutes(_bars, e, closes[_sym]), _sym, day, e, TAGS[0],
                lambda c, _s=_sym: gross_of(_s, c))
            d2 = T.pick_gross(
                T.same_name_minutes(_bars, e, closes[_sym]), _sym, day, e, TAGS[1],
                lambda c, _s=_sym: gross_of(_s, c))
            base = [n for n in x_at(e) if n != _sym]
            x = T.pick_gross(base, _sym, day, e, TAGS[2], lambda n, _e=e: gross_of(n, _e))
            x2 = T.pick_gross(base, _sym, day, e, TAGS[3], lambda n, _e=e: gross_of(n, _e))
            if d is None or d2 is None or x is None or x2 is None:
                return None
            return (d, d2, x, x2)

        pred = lambda t, _fast=fast, _slow=slow: turn(_fast, _slow, t)
        scored, other = take(T.onset_minutes(pred), classify, control_fn)
        for event in other:
            if event[0] == "miss":
                st[f"{half}|{event[2]}"] += 1
            elif event[0] == "control":
                st[f"{half}|missing_control"] += 1
        for t, px, _exit_m, _reason, grosses in scored:
            e = t + 1
            d, d2, x, x2 = grosses
            fs = P.pair(fast, slow, t)
            prev = P.pair(fast, slow, t - 1)
            p5 = pitch5(fast, slow, t)
            if fs is None or prev is None or p5 is None or not (fast[t] < slow[t]):
                raise SystemExit("predicate partition broken")
            out.append({
                "day": day, "sym": sym, "pop": "primary", "dec_min": t, "entry_min": e,
                "f": fs[0], "s": fs[1], "pf": prev[0], "ps": prev[1],
                "f5": p5[0], "s5": p5[1],
                "fast_under": True,
                "gross": px / bars[e][0] - 1.0,
                "d": d, "d2": d2, "x": x, "x2": x2,
            })
    return {"rows": out, "stats": dict(st)}


def count_body(rows, st: Counter) -> dict:
    check_rows(rows)
    cnt = {
        "stats_skips": {h: {k: int(st.get(f"{h}|{k}", 0)) for k in P.SKIP_KEYS} for h in ("A", "B")},
        "module_sha256": module_shas(),
        "prereg_sha256": prereg_sha(),
    }
    for h in ("A", "B"):
        hr = [r for r in rows if P.half_of(r["day"]) == h]
        md, mx = P.mde(hr, "d", "d2"), P.mde(hr, "x", "x2")
        days_n = len({r["day"] for r in hr})
        cnt[h] = {
            "trades": len(hr),
            "days": days_n,
            "mde_d": md,
            "mde_x": mx,
            "floors_met": bool(
                len(hr) >= P.FLOOR_TRADES and days_n >= P.FLOOR_DAYS
                and md is not None and md <= P.LIFT_BAR and mx is not None and mx <= P.LIFT_BAR
            ),
        }
    return cnt


def load_or_build(lines: str, workers: int, prereg_commit: str):
    import indicator_confirm_hist as H
    from concurrent.futures import ProcessPoolExecutor
    dest = out_dir_for(lines)
    cache = os.path.join(dest, "rows.json")
    sha, psha = module_shas(), prereg_sha()
    if os.path.exists(cache):
        with open(cache) as f:
            c = json.load(f)
        if c.get("module_sha256") == sha and c.get("prereg_sha256") == psha:
            return c["rows"], Counter(c.get("stats") or {}), dest
    days = H.warehouse_days()
    run = [(d, days[i - 1], lines) for i, d in enumerate(days) if P.FIRST <= d <= P.LAST and i >= 1]
    rows, st = [], Counter()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(day_task, run, chunksize=2), 1):
            rows.extend(res["rows"])
            st.update(res["stats"])
            print(f"session {i}/{len(run)}", file=sys.stderr, flush=True)
    write_json(cache, {
        "rows": rows, "stats": dict(st), "module_sha256": sha, "prereg_sha256": psha,
        "prereg_commit": prereg_commit, "lines": lines,
    })
    return rows, st, dest


def run_canary(lines: str) -> None:
    dest = out_dir_for(lines)
    cache = os.path.join(dest, "rows.json")
    if not os.path.exists(cache):
        raise SystemExit("canary needs rows.json from this count")
    with open(cache) as f:
        c = json.load(f)
    if c.get("module_sha256") != module_shas() or c.get("prereg_sha256") != prereg_sha():
        raise SystemExit("rows.json is from different code or a different prereg")
    deltas = P.canary_deltas(c["rows"])
    ok = all(v is not None and abs(v - 0.0010) <= 1e-9 for v in deltas.values())
    obj = {"deltas": deltas, "canary_ok": ok, "module_sha256": module_shas(), "prereg_sha256": prereg_sha()}
    write_json(os.path.join(dest, "canary.json"), obj, pretty=True)
    print(json.dumps({"deltas": deltas, "canary_ok": ok}))


def run_score(lines: str, skeptic_ready: str) -> None:
    dest = out_dir_for(lines)
    if skeptic_ready != READY_ID:
        raise SystemExit("score needs the round-2 READY id")
    cnt_path = os.path.join(dest, "count.json")
    if not os.path.exists(cnt_path):
        raise SystemExit("score needs count.json")
    with open(cnt_path) as f:
        cnt = json.load(f)
    if cnt.get("module_sha256") != module_shas() or cnt.get("prereg_sha256") != prereg_sha():
        raise SystemExit("count.json from different code or a different prereg")
    if not cnt.get("prereg_commit"):
        raise SystemExit("count.json without --prereg-commit")
    if not all(cnt[h]["floors_met"] for h in ("A", "B")):
        print("UNDERPOWERED")
        return
    canary_path = os.path.join(dest, "canary.json")
    if not os.path.exists(canary_path):
        raise SystemExit("score needs canary.json")
    with open(canary_path) as f:
        canary = json.load(f)
    if (not canary.get("canary_ok") or canary.get("module_sha256") != module_shas()
            or canary.get("prereg_sha256") != prereg_sha()):
        raise SystemExit("canary_ok is missing on this code and prereg")
    with open(os.path.join(dest, "rows.json")) as f:
        rows = json.load(f)["rows"]
    check_rows(rows)
    halves = {}
    for h in ("A", "B"):
        hr = [r for r in rows if P.half_of(r["day"]) == h]
        halves[h] = P.ladder(hr)
    verdict = P.overall(halves)
    res = {
        "verdict": verdict,
        "halves": halves,
        "skeptic_ready": skeptic_ready,
        "lines": lines,
        "prereg_sha256": prereg_sha(),
        "module_sha256": module_shas(),
    }
    write_json(os.path.join(dest, "result.json"), res, pretty=True)
    print(json.dumps(res, indent=1, default=str))


def run_count(lines: str, workers: int, prereg_commit: str) -> None:
    if not prereg_commit:
        raise SystemExit("count needs --prereg-commit")
    rows, st, dest = load_or_build(lines, workers, prereg_commit)
    body = count_body(rows, st)
    body["prereg_commit"] = prereg_commit
    body["lines"] = lines
    write_json(os.path.join(dest, "count.json"), body, pretty=True)
    shown = {k: body[k] for k in ("A", "B", "stats_skips", "lines") if k in body}
    print(json.dumps(shown, indent=1, default=str))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--canary", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--prereg-commit", default="")
    ap.add_argument("--lines", default="iex", choices=("iex", "sip"))
    a = ap.parse_args()
    if READY_ID not in open(prereg_path()).read():
        raise SystemExit("prereg is missing the round-2 READY id")
    if a.canary:
        run_canary(a.lines)
        return
    if a.score:
        run_score(a.lines, a.skeptic_ready)
        return
    run_count(a.lines, a.workers, a.prereg_commit)


if __name__ == "__main__":
    main()
