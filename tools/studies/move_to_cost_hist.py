#!/usr/bin/env python3
"""move_to_cost_hist.py — docs/studies/move_to_cost_hist_prereg.json (amended, amended_2, ready).

Events: pitch_open empty-square fires on warehouse IEX 1-minute lines (fast 21/EMA 7, slow 112/EMA 3, previous
session as warm-up), fires computed on 585..920 with the 15-minute refractory, kept for decision minutes t 591..920;
entry minute e = t + 1. Sessions 2026-05-11..2026-09-25 (9/28-10/13 excluded). Halves A ..07-29, B 07-30..

Draw (amended_2 A1): per session, walk events in sha256('MTCH'|day|symbol|t) order; fetch the SIP quote at e:00
(last usable quote at or before e:00: bid > 0, ask > bid, age <= 15 s). Mid $20-$200 = kept, else skipped (counted).
Stop at 60 kept. An event with no usable e:00 quote cannot be priced: it is KEPT as missing (it counts toward 60 and
toward the 10% VOID share). An event whose 21-bar range is missing (< 15 bars) is known before any fetch: counted,
not fetched, not kept.

Measures: range = % range (high / low - 1) of IEX bars [t-20, t]; spread = full SIP spread at e:00 in bp of mid;
move_to_cost = range bp / spread bp, LOW < 8. PRIMARY gross_mid = mid((e+10):00) / mid(e:00) - 1 (bp). Secondary:
net = gross_mid - spread; gross_bar = warehouse SIP open e -> close e+9.

  count   build events, draw, fetch; write rows.json; print counts, missing share, LOW share and MDE only (no means).
  score   refuses unless count.json is powered and not VOID; the pass rule and information tables.
USAGE (mini, after the close, one heavy Alpaca job at a time):
  .venv/bin/python tools/studies/move_to_cost_hist.py count [--workers 3]
  .venv/bin/python tools/studies/move_to_cost_hist.py score
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import move_to_cost_holdout as MH  # noqa: E402
import pitch_open as P  # noqa: E402
import std_scorer as S  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = Path(os.path.expanduser("~/replay_runs/move_to_cost_hist"))
FIRST, LAST, HALF_A_END = "2026-05-11", "2026-09-25", "2026-07-29"
T_LO, T_HI, WIN, MIN_BARS = 591, 920, 21, 15
PER_SESSION, PX_LO, PX_HI, QUOTE_AGE = 60, 20.0, 200.0, 15.0
CUT, BAR, GATE_BP = 8.0, -5.0, 15.0
MIN_LOW, MIN_SESSIONS, MAX_MDE, MAX_MISSING, MAX_ERROR = 200, 40, 5.0, 0.10, 0.05
PACE_SEC = 0.32                                      # ~190 requests/min on the data-only key


# ── events (warehouse only, no Alpaca) ──────────────────────────────────────────────────────────────────────────────
def range_bars(rows, t: int, n: int = WIN):
    """(% range of the bars [t-n+1, t], bar count); range None when fewer than MIN_BARS bars."""
    w = [r for r in (rows if rows is not None else []) if t - n < int(r[0]) <= t]
    if len(w) < MIN_BARS:
        return None, len(w)
    hi, lo = max(float(r[2]) for r in w), min(float(r[3]) for r in w)
    return ((hi / lo - 1) * 100 if lo > 0 else None), len(w)


def fire_minutes(fast: dict, slow: dict) -> list[int]:
    """Fires on pitch_open's 585..920 (the refractory sees fires from 09:45), kept for t in 591..920."""
    return [t for t in P.fires(lambda m: P.empty_square(fast, slow, m)) if T_LO <= t <= T_HI]


def draw_key(day: str, sym: str, t: int) -> str:
    return hashlib.sha256(f"MTCH|{day}|{sym}|{t}".encode()).hexdigest()


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
        sip = {int(r[0]): (float(r[1]), float(r[4])) for r in sip_raw.get(sym, [])}
        for t in fire_minutes(fast, slow):
            rng, nb = range_bars(lf_today[sym], t)
            g = P.gross_at(sip, t + 1, P.HOLD)
            out.append({"day": day, "symbol": sym, "t": t, "rng": rng, "bars": nb,
                        "gross_bar": None if g is None else g * 1e4, "key": draw_key(day, sym, t)})
    return {"events": out, "stats": dict(st)}


# ── quotes ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def minute_ts(day: str, m: int) -> float:
    return _dt.datetime.fromisoformat(f"{day} {m // 60:02d}:{m % 60:02d}:00").replace(tzinfo=ET).timestamp()


class QuoteData(MH.Data):
    _last = 0.0

    def quote(self, sym: str, ts: float):
        """(bid, ask) of the last usable SIP quote at or before ts within QUOTE_AGE s; None = no usable quote."""
        def fn():
            wait = PACE_SEC - (time.monotonic() - QuoteData._last)
            if wait > 0 and self.get is not None:
                time.sleep(wait)
            QuoteData._last = time.monotonic()
            end = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
            d = self._call(f"/v2/stocks/{sym}/quotes", {"feed": "sip", "limit": 1000, "sort": "desc",
                                                         "start": (end - _dt.timedelta(seconds=QUOTE_AGE)).isoformat(),
                                                         "end": end.isoformat()})
            for q in d.get("quotes") or []:
                b, a = float(q.get("bp") or 0), float(q.get("ap") or 0)
                if b > 0 and a > b:
                    return [b, a]
            return None
        return self._cached(f"qq|{sym}|{int(ts)}", fn)


def mid_spread(q):
    if not q:
        return None, None
    b, a = q
    mid = (a + b) / 2
    return mid, (a - b) / mid * 1e4


def label(rng, spread_bp) -> str:
    if rng is None or spread_bp is None or spread_bp <= 0:
        return "missing"
    return "LOW" if rng * 100.0 / spread_bp < CUT else "REST"


def draw_session(events: list[dict], data, per_session: int = PER_SESSION) -> tuple[list[dict], Counter]:
    """amended_2 A1: walk in hash order, fetch e:00, keep $20-$200 mids, stop at per_session kept."""
    kept, st = [], Counter()
    for ev in sorted(events, key=lambda r: r["key"]):
        if len(kept) >= per_session:
            break
        if ev["rng"] is None:
            st["range_missing"] += 1
            continue
        e = ev["t"] + 1
        row = dict(ev, status="ok")
        try:
            q0 = data.quote(ev["symbol"], minute_ts(ev["day"], e))
        except MH.FetchError:
            st["error"] += 1
            kept.append(dict(row, status="error", group="missing"))
            continue
        mid0, sp = mid_spread(q0)
        if mid0 is None:
            st["entry_quote_missing"] += 1
            kept.append(dict(row, status="missing", group="missing"))
            continue
        if not PX_LO <= mid0 <= PX_HI:
            st["price_skip"] += 1
            continue
        try:
            q1 = data.quote(ev["symbol"], minute_ts(ev["day"], e + P.HOLD))
        except MH.FetchError:
            st["error"] += 1
            kept.append(dict(row, status="error", group="missing", mid0=mid0, spread=sp))
            continue
        mid1, _ = mid_spread(q1)
        if mid1 is None:
            st["exit_quote_missing"] += 1
            kept.append(dict(row, status="missing", group="missing", mid0=mid0, spread=sp))
            continue
        gm = (mid1 / mid0 - 1) * 1e4
        kept.append(dict(row, mid0=mid0, spread=sp, gross_mid=gm, net=gm - sp, group=label(ev["rng"], sp)))
    return kept, st


# ── statistics (pure) ───────────────────────────────────────────────────────────────────────────────────────────────
def half(day: str) -> str:
    return "A" if day <= HALF_A_END else "B"


def scored(rows):
    return [r for r in rows if r.get("status") == "ok" and r.get("group") in ("LOW", "REST")]


def placebo_mde(rows, key="gross_mid", draws=200, seed="MTCH-MDE"):
    days = MH.by_day(rows)
    sds, n = [], 0
    for i in range(draws):
        rnd = random.Random(f"{seed}|{i}")
        labels = {}
        for rs in days.values():
            k = sum(r["group"] == "LOW" for r in rs)
            pick = {id(r) for r in rnd.sample(rs, k)}
            for r in rs:
                labels[id(r)] = "LOW" if id(r) in pick else "REST"
        ds = MH.session_d(rows, key=key, labels=labels)
        if len(ds) >= 3:
            sds.append(statistics.stdev(ds))
            n = len(ds)
    if not sds or n < 3:
        return None
    return (MH.t_crit(n - 1, 0.975) + MH.t_crit(n - 1, 0.80)) * statistics.median(sds) / math.sqrt(n)


def counts(rows) -> dict:
    sc = scored(rows)
    low = [r for r in sc if r["group"] == "LOW"]
    by_band: dict[str, Counter] = {}
    for r in rows:
        b = range_band(r.get("rng"))
        by_band.setdefault(b, Counter())[r.get("status") if r.get("status") != "ok" else "ok"] += 1
    sampled = len(rows)
    out = {"sampled": sampled, "scored": len(sc), "low_n": len(low), "low_share": len(low) / len(sc) if sc else None,
           "both_sessions": len(MH.session_d(sc, key="gross_mid")),
           "missing": sum(r.get("status") == "missing" for r in rows),
           "error": sum(r.get("status") == "error" for r in rows),
           "missing_share": (sum(r.get("status") in ("missing", "error") for r in rows) / sampled) if sampled else None,
           "status_by_range_band": {k: dict(v) for k, v in sorted(by_band.items())},
           "mde": placebo_mde(sc),
           "mde_by_half": {h: placebo_mde([r for r in sc if half(r["day"]) == h]) for h in ("A", "B")}}
    return out


def range_band(rng) -> str:
    if rng is None:
        return "missing"
    for lo, hi in ((0, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, 2.0)):
        if lo <= rng < hi:
            return f"[{lo},{hi})%"
    return ">=2%"


def void(c: dict) -> bool:
    s = c["sampled"] or 0
    return s == 0 or c["error"] / s > MAX_ERROR or (c["missing_share"] or 0) > MAX_MISSING


def powered(c: dict) -> bool:
    return (c["low_n"] >= MIN_LOW and c["both_sessions"] >= MIN_SESSIONS and c["mde"] is not None
            and c["mde"] <= MAX_MDE)


def d_stat(rows, key="gross_mid") -> dict:
    ds = MH.session_d(rows, key=key)
    st = S.day_stat(ds)
    n = len(ds)
    tc = MH.t_crit(n - 1, 0.975) if n >= 2 else float("inf")
    se = st.get("se")
    lo = hi = None
    if se is not None and math.isfinite(se):
        lo, hi = st["mean"] - tc * se, st["mean"] + tc * se
    return {"sessions": n, "mean": st["mean"], "t": st["t"], "t_crit": tc, "lower_97_5": lo, "upper_97_5": hi}


def verdict(rows, cnt: dict) -> dict:
    sc = scored(rows)
    d = d_stat(sc)
    halves = {h: d_stat([r for r in sc if half(r["day"]) == h]) for h in ("A", "B")}
    gated = d_stat([r for r in sc if r["spread"] <= GATE_BP])
    rem_low = MH.book_mean_after(sc, {id(r) for r in sc if r["group"] == "LOW"})
    rnd = MH.random_removal(sc, seed="MTCH")
    sp_ctl = statistics.median(MH.ranked_removal(sc, "spread", reverse=True, seed="MTCH-TIE"))
    rg_ctl = statistics.median(MH.ranked_removal(sc, "rng", reverse=False, seed="MTCH-TIE"))
    checks = {
        "d": d["mean"] <= BAR and d["t"] <= -d["t_crit"],
        "both_halves": all(halves[h]["mean"] < 0 for h in halves),
        "beats_random": bool(rnd) and sum(rem_low > x for x in rnd) / len(rnd) >= 0.95,
        "beats_widest_spread": rem_low > sp_ctl,
        "beats_lowest_range": rem_low > rg_ctl,
        "gated_spread_negative": gated["mean"] < 0,
        "power": powered(cnt),
    }
    holds = all(checks.values())
    return {"primary_d_gross_mid": d, "halves": halves, "spread_le_15bp": gated,
            "book_after_low": rem_low, "random_share_beaten": sum(rem_low > x for x in rnd) / len(rnd) if rnd else None,
            "widest_spread_median": sp_ctl, "lowest_range_median": rg_ctl, "checks": checks,
            "verdict": "HOLDS" if holds else "NOT SUPPORTED",
            "refutation_note": ("prereg 'fail' reads 'refuted only if the upper one-sided 97.5% bound of d > 0'; "
                                "both bounds are reported, the label is left to the reader")}


def info(rows) -> dict:
    sc = scored(rows)

    def cell(rs):
        per = {}
        for r in rs:
            per.setdefault(r["day"], []).append(r)
        out = {"n": len(rs), "days": len(per)}
        for k in ("gross_mid", "net", "gross_bar"):
            vals = [statistics.fmean([r[k] for r in v if r.get(k) is not None])
                    for v in per.values() if any(r.get(k) is not None for r in v)]
            out[k] = S.day_stat(vals)["mean"] if vals else None
        out["win_mid"] = sum(r["gross_mid"] > 0 for r in rs) / len(rs) if rs else None
        return out

    def m2c_band(r):
        m = r["rng"] * 100.0 / r["spread"]
        return "[0,4)" if m < 4 else "[4,8)" if m < 8 else "[8,16)" if m < 16 else ">=16"

    def px_band(r):
        return "$20-100" if r["mid0"] < 100 else "$100-200"

    out = {"m2c_bands": {}, "price_bands": {}, "groups": {}}
    for h in ("A", "B", "all"):
        rs = sc if h == "all" else [r for r in sc if half(r["day"]) == h]
        out["m2c_bands"][h] = {b: cell([r for r in rs if m2c_band(r) == b]) for b in ("[0,4)", "[4,8)", "[8,16)", ">=16")}
        out["groups"][h] = {g: cell([r for r in rs if r["group"] == g]) for g in ("LOW", "REST")}
    out["price_bands"] = {b: cell([r for r in sc if px_band(r) == b]) for b in ("$20-100", "$100-200")}
    out["d_net"] = d_stat(sc, "net")
    bar_rows = [r for r in sc if r.get("gross_bar") is not None]
    out["d_gross_bar"] = d_stat(bar_rows, "gross_bar")
    ds_by_day = {}
    for day, rs in MH.by_day(sc).items():
        x = MH.session_d(rs, key="gross_mid")
        if x:
            ds_by_day[day] = x[0]
    top3 = set(sorted(ds_by_day, key=lambda k: abs(ds_by_day[k]), reverse=True)[:3])
    out["d_drop_top3_days"] = d_stat([r for r in sc if r["day"] not in top3])
    name_n = Counter(r["symbol"] for r in sc if r["group"] == "LOW")
    top5 = {s for s, _ in name_n.most_common(5)}
    out["d_drop_top5_low_names"] = d_stat([r for r in sc if r["symbol"] not in top5])
    return out


# ── modes ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def sessions() -> list[tuple[str, str]]:
    import indicator_confirm_hist as H
    days = H.warehouse_days()
    return [(d, days[i - 1]) for i, d in enumerate(days) if FIRST <= d <= LAST and i >= 1]


def run_count(workers: int) -> dict:
    data = QuoteData.live()
    data.dir = OUT / "cache"
    rows, st = [], Counter()
    run = sessions()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(day_events, run, chunksize=2), 1):
            st.update(res["stats"])
            st["events"] += len(res["events"])
            kept, s2 = draw_session(res["events"], data)
            rows += kept
            st.update(s2)
            print(f"session {i}/{len(run)} {run[i - 1][0]}: events {len(res['events'])} kept {len(kept)} "
                  f"calls {data.calls}", file=sys.stderr, flush=True)
    (OUT / "rows.json").write_text(json.dumps(rows, default=str))
    c = counts(rows)
    c.update({"sessions": len(run), "stats": dict(st), "quote_calls": data.calls})
    c["void"] = void(c)
    c["powered"] = (not c["void"]) and powered(c)
    c["verdict_if_stopped"] = "VOID" if c["void"] else (None if c["powered"] else "UNDERPOWERED")
    (OUT / "count.json").write_text(json.dumps(c, indent=1, default=str))
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("count", "score"))
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.mode == "count":
        print(json.dumps(run_count(a.workers), indent=1, default=str))
        return 0
    cp = OUT / "count.json"
    cnt = json.loads(cp.read_text()) if cp.exists() else {}
    if not cnt.get("powered") or cnt.get("void"):
        print(f"no score: {cnt.get('verdict_if_stopped') or 'count.json missing'}", file=sys.stderr)
        return 2
    rows = json.loads((OUT / "rows.json").read_text())
    res = {"count": cnt, "result": verdict(rows, cnt), "info": info(rows),
           "note": "association on the broad market only; a HOLDS earns a separate replay-gate prereg, never an edge"}
    (OUT / "result.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res["result"], indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
