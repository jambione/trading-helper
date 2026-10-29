#!/usr/bin/env python3
"""move_to_cost_holdout.py — docs/studies/move_to_cost_holdout_prereg.json (amended, amended_2, ready).

Trades = slope_now REF replay trades ('replay trades at 10/27 config'), priced at the first SIP trade within 60 s, net
= gross - full SIP quoted spread at entry (last SIP quote at or before the entry second). Range = % range of the 21
completed IEX 1-minute bars (Alpaca historical, raw) before the minute of the trade's ARM_OK row. move_to_cost = range
bp / spread bp; LOW < 8, REST >= 8, missing excluded (counted). Fetch errors are retried and kept apart from no-data;
> 5% unresolved errors = VOID.

  count    REF vs live fills on 10/27 (recall / precision within 120 s); LOW n, sessions with both groups and the
           placebo MDE for the held-out set with and without 10/27. Prints counts and MDEs only (no means).
  recheck  the exploration's 12 sessions on Alpaca bars: continue only if the session-equal mean d (LOW - REST) < 0;
           agreement of LOW/REST labels with the warehouse on 10/14-10/15.
  score    refuses unless count is powered and recheck passed; the pass rule with and without 10/27.
USAGE (mini, after the close): .venv/bin/python tools/studies/move_to_cost_holdout.py count|recheck|score
"""
from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import late_gates_read as LR  # noqa: E402
import slope_now_explore as X  # noqa: E402
import stale_age_read as SR  # noqa: E402
import std_scorer as S  # noqa: E402

ET = ZoneInfo("America/New_York")
REF_DIR = Path(os.path.expanduser("~/replay_runs/slope_now"))
OUT = Path(os.path.expanduser("~/replay_runs/move_to_cost"))
EXPLORE = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
           "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"]
HOLDOUT = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22",
           "2026-10-23", "2026-10-26", "2026-10-27"]
CUT, MISSING, ERROR = 8.0, "missing", "error"
_PRICER = None          # one SIP close fetcher for open_at_end marks


class FetchError(Exception):
    pass


# Student t quantiles by degrees of freedom (scipy is not in the venv). Beyond the table: the normal value.
_T = {0.975: [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145,
              2.131, 2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048,
              2.045, 2.042],
      0.80: [1.376, 1.061, 0.978, 0.941, 0.920, 0.906, 0.896, 0.889, 0.883, 0.879, 0.876, 0.873, 0.870, 0.868,
             0.866, 0.865, 0.863, 0.862, 0.861, 0.860, 0.859, 0.858, 0.858, 0.857, 0.856, 0.856, 0.855, 0.855,
             0.854, 0.854]}
_Z = {0.975: 1.960, 0.80: 0.842}


def t_crit(n_df: int, p: float) -> float:
    df = max(1, int(n_df))
    tab = _T[p]
    return tab[df - 1] if df <= len(tab) else _Z[p]


# ── data (errors kept apart from no-data) ───────────────────────────────────────────────────────────────────────────
class Data:
    def __init__(self, get=None, cache_dir: Path | None = None):
        self.get = get
        self.dir = cache_dir
        self.errors = 0
        self.calls = 0

    @classmethod
    def live(cls):
        import requests
        s = json.load(open(ROOT / "secrets_data.json"))
        hdr = {"APCA-API-KEY-ID": s["alpaca_data_api_key"], "APCA-API-SECRET-KEY": s["alpaca_data_secret_key"]}

        def get(path, params):
            r = requests.get(f"https://data.alpaca.markets{path}", headers=hdr, params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                raise FetchError(str(r.status_code))
            r.raise_for_status()
            return r.json()
        return cls(get, OUT / "cache")

    def _call(self, path, params):
        for k in range(3):
            try:
                self.calls += 1
                return self.get(path, params)
            except Exception:  # noqa: BLE001
                time.sleep(2.0 * (k + 1))
        self.errors += 1
        raise FetchError(path)

    def _cached(self, key, fn):
        if self.dir is None:
            return fn()
        p = self.dir / (hashlib.sha1(key.encode()).hexdigest() + ".json")
        if p.exists():
            return json.loads(p.read_text())
        val = fn()
        self.dir.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(val))
        return val

    def bars(self, sym: str, day: str):
        """IEX 1-minute raw bars as (minute-of-day ET, o, h, l, c, v), 09:30-16:00; [] = no data; FetchError = error."""
        def fn():
            t0 = _dt.datetime.fromisoformat(f"{day} 09:30").replace(tzinfo=ET)
            out, token = [], None
            while True:
                params = {"timeframe": "1Min", "feed": "iex", "adjustment": "raw", "limit": 10000,
                          "start": t0.isoformat(), "end": (t0 + _dt.timedelta(minutes=390)).isoformat()}
                if token:
                    params["page_token"] = token
                d = self._call(f"/v2/stocks/{sym}/bars", params)
                for b in d.get("bars") or []:
                    ts = _dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")).astimezone(ET)
                    out.append([ts.hour * 60 + ts.minute, b["o"], b["h"], b["l"], b["c"], b["v"]])
                token = d.get("next_page_token")
                if not token:
                    return out
        return self._cached(f"bars|{sym}|{day}", fn)

    def spread_bp(self, sym: str, t: float):
        """Full SIP quoted spread (bp of mid) of the last quote at or before t (60 s window); None = no quote."""
        def fn():
            end = _dt.datetime.fromtimestamp(t, _dt.timezone.utc)
            d = self._call(f"/v2/stocks/{sym}/quotes", {"feed": "sip", "limit": 10000, "sort": "desc",
                                                         "start": (end - _dt.timedelta(seconds=60)).isoformat(),
                                                         "end": end.isoformat()})
            for q in d.get("quotes") or []:
                b, a = float(q.get("bp") or 0), float(q.get("ap") or 0)
                if b > 0 and a > b:
                    return (a - b) / ((a + b) / 2) * 1e4
            return None
        return self._cached(f"q|{sym}|{int(t)}", fn)


# ── trades ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def label(m2c) -> str:
    return MISSING if m2c is None else ("LOW" if m2c < CUT else "REST")


def day_trades(day: str, data: Data, sip, ref_dir: Path = REF_DIR) -> list[dict]:
    res = LR.load(day, "REF", ref_dir)
    if res is None:
        return []
    arm = X.arms(ref_dir / f"{day}-REF-arms.jsonl.gz")
    out = []
    raw = [(t, False) for t in res.get("closed") or []] + [(t, True) for t in res.get("open_at_end") or []]
    bars_by: dict[str, list] = {}
    for t, is_open in raw:
        sym, ts, px = str(t["symbol"]).upper(), float(t["entry_ts"]), t.get("entry_px")
        if not px:
            continue
        row = {"day": day, "symbol": sym, "entry_ts": ts, "status": "ok"}
        try:
            if sym not in bars_by:
                bars_by[sym] = data.bars(sym, day)
            e = sip.price(day, sym, ts)
            sp = data.spread_bp(sym, ts)
        except FetchError:
            row["status"] = ERROR
            out.append(row)
            continue
        if day in getattr(sip, "failed", set()):
            row["status"] = ERROR
            out.append(row)
            continue
        if is_open:
            global _PRICER
            _PRICER = _PRICER or LR.Pricer.live()
            mark = _PRICER.close(sym, day, LR.close_1549(day))
            exit_px = float(mark) if mark else None
        else:
            exit_px = float(px) * (1 + float(t["ret"])) if t.get("ret") is not None else None
        if not e or exit_px is None or sp is None:
            row["status"] = "no_price"
            out.append(row)
            continue
        joined = X.join([{"symbol": sym, "entry_ts": ts}], arm, {sym: bars_by[sym]})[0]
        rng = joined.get("rng")
        row.update({"gross": (exit_px / float(e) - 1) * 1e4, "spread": sp, "rng": rng})
        row["net"] = row["gross"] - sp
        row["m2c"] = None if rng is None else rng * 100.0 / sp
        row["group"] = label(row["m2c"])
        out.append(row)
    return out


# ── statistics (pure) ───────────────────────────────────────────────────────────────────────────────────────────────
def scored(rows):
    return [r for r in rows if r.get("status") == "ok" and r.get("group") in ("LOW", "REST")]


def by_day(rows):
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["day"], []).append(r)
    return out


def session_d(rows, key="net", labels=None):
    """Per session: mean key(LOW) - mean key(REST), sessions with both groups. labels overrides r['group']."""
    ds = []
    for day, rs in sorted(by_day(rows).items()):
        lab = [labels[id(r)] if labels else r["group"] for r in rs]
        lo = [r[key] for r, g in zip(rs, lab) if g == "LOW"]
        re_ = [r[key] for r, g in zip(rs, lab) if g == "REST"]
        if lo and re_:
            ds.append(statistics.fmean(lo) - statistics.fmean(re_))
    return ds


def book_mean_after(rows, remove_ids: set) -> float:
    per = [statistics.fmean([r["net"] for r in rs if id(r) not in remove_ids])
           for rs in by_day(rows).values() if any(id(r) not in remove_ids for r in rs)]
    return statistics.fmean(per) if per else float("nan")


def ranked_removal(rows, key, reverse, seed="MTC-TIE", draws=100) -> list[float]:
    """Remove, per session, k = that session's LOW count trades ranked by key; ties at the cut broken at random."""
    out = []
    for i in range(draws):
        rnd = random.Random(f"{seed}|{i}")
        rem = set()
        for rs in by_day(rows).values():
            k = sum(r["group"] == "LOW" for r in rs)
            order = sorted(rs, key=lambda r: ((-r[key] if reverse else r[key]), rnd.random()))
            rem |= {id(r) for r in order[:k]}
        out.append(book_mean_after(rows, rem))
    return out


def random_removal(rows, seed="MTC", draws=1000) -> list[float]:
    out = []
    for i in range(draws):
        rnd = random.Random(f"{seed}-{i}")
        rem = set()
        for rs in by_day(rows).values():
            k = sum(r["group"] == "LOW" for r in rs)
            rem |= {id(r) for r in rnd.sample(rs, k)}
        out.append(book_mean_after(rows, rem))
    return out


def placebo_mde(rows, draws=200, seed="MTC-MDE") -> float | None:
    sds, n = [], 0
    days = by_day(rows)
    for i in range(draws):
        rnd = random.Random(f"{seed}|{i}")
        labels = {}
        for rs in days.values():
            k = sum(r["group"] == "LOW" for r in rs)
            pick = {id(r) for r in rnd.sample(rs, k)}
            for r in rs:
                labels[id(r)] = "LOW" if id(r) in pick else "REST"
        ds = session_d(rows, labels=labels)
        if len(ds) >= 3:
            sds.append(statistics.stdev(ds))
            n = len(ds)
    if not sds or n < 3:
        return None
    return (t_crit(n - 1, 0.975) + t_crit(n - 1, 0.80)) * statistics.median(sds) / math.sqrt(n)


def counts(rows) -> dict:
    sc = scored(rows)
    low = [r for r in sc if r["group"] == "LOW"]
    return {"trades": len(rows), "scored": len(sc), "low_n": len(low), "low_sessions": len({r["day"] for r in low}),
            "both_sessions": len(session_d(sc)), "missing": sum(r.get("group") == MISSING for r in rows),
            "error": sum(r.get("status") == ERROR for r in rows), "no_price": sum(r.get("status") == "no_price" for r in rows),
            "mde": placebo_mde(sc)}


def powered(c: dict) -> bool:
    return c["low_n"] >= 20 and c["low_sessions"] >= 6 and c["mde"] is not None and c["mde"] <= 10.0


def verdict_set(rows) -> dict:
    sc = scored(rows)
    ds = session_d(sc)
    st = S.day_stat(ds)
    n = len(ds)
    tcrit = t_crit(n - 1, 0.975) if n >= 2 else float("inf")
    low_mean = statistics.fmean([r["net"] for r in sc if r["group"] == "LOW"]) if any(r["group"] == "LOW" for r in sc) else None
    rem_low = book_mean_after(sc, {id(r) for r in sc if r["group"] == "LOW"})
    rnd = random_removal(sc)
    spread_ctl = ranked_removal(sc, "spread", reverse=True)
    range_ctl = ranked_removal(sc, "rng", reverse=False)
    checks = {
        "d": st["mean"] <= -5.0 and st["t"] <= -tcrit,
        "low_negative": low_mean is not None and low_mean < 0,
        "power": sum(r["group"] == "LOW" for r in sc) >= 20 and len({r["day"] for r in sc if r["group"] == "LOW"}) >= 6,
        "beats_random": sum(rem_low > x for x in rnd) / len(rnd) >= 0.95 if rnd else False,
        "beats_spread_only": sum(rem_low > x for x in spread_ctl) / len(spread_ctl) >= 0.95 if spread_ctl else False,
        "beats_range_only": sum(rem_low > x for x in range_ctl) / len(range_ctl) >= 0.95 if range_ctl else False,
    }
    return {"sessions": n, "d": st, "t_crit": tcrit, "low_mean_net": low_mean, "book_after_low": rem_low,
            "gross_d": S.day_stat(session_d(sc, "gross"))["mean"], "spread_d": S.day_stat(session_d(sc, "spread"))["mean"],
            "checks": checks, "holds": all(checks.values())}


# ── modes ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def load_rows(days, data, sip):
    rows = []
    for d in days:
        rows += day_trades(d, data, sip)
    return rows


def ref_vs_live(day="2026-10-27") -> dict:
    res = LR.load(day, "REF", REF_DIR) or {}
    rep = [(float(t["entry_ts"]), str(t["symbol"]).upper()) for t in (res.get("closed") or []) + (res.get("open_at_end") or [])]
    live = [(float(x["ts"]), str(x["symbol"]).upper()) for x in map(json.loads, open(Path.home() / "session_snapshots" / day / "fills.jsonl"))
            if x.get("event") == "fill" and str(x.get("side")).lower() == "buy"]
    used, m = set(), 0
    for lt, ls in sorted(live):
        best = min(((abs(rt - lt), j) for j, (rt, rs) in enumerate(rep) if rs == ls and j not in used and abs(rt - lt) <= 120),
                   default=None)
        if best:
            used.add(best[1])
            m += 1
    return {"recall": m / len(live) if live else None, "precision": m / len(rep) if rep else None,
            "live": len(live), "replay": len(rep)}


def void(rows) -> bool:
    return bool(rows) and sum(r.get("status") == ERROR for r in rows) / len(rows) > 0.05


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("count", "recheck", "score"))
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    data = Data.live()
    sip = SR.SipEntry(SR.SipEntry.live().fetch, OUT / "sip_entry_cache.json")
    try:
        if a.mode == "count":
            rows = load_rows(HOLDOUT, data, sip)
            no27 = [r for r in rows if r["day"] != "2026-10-27"]
            res = {"ref_vs_live_10_27": ref_vs_live(), "with_10_27": counts(rows), "without_10_27": counts(no27),
                   "void": void(rows)}
            res["powered"] = (not res["void"]) and powered(res["with_10_27"]) and powered(res["without_10_27"])
            (OUT / "count.json").write_text(json.dumps(res, indent=1, default=str))
            print(json.dumps(res, indent=1, default=str))
            return 0
        if a.mode == "recheck":
            rows = load_rows(EXPLORE, data, sip)
            ds = session_d(scored(rows))
            mean_d = statistics.fmean(ds) if ds else None
            agree = None
            try:
                import indicator_confirm_hist as H
                lab_a = {(r["day"], r["symbol"], int(r["entry_ts"])): r["group"]
                         for r in load_rows(["2026-10-14", "2026-10-15"], data, sip) if r.get("group")}
                same = tot = 0
                for d in ("2026-10-14", "2026-10-15"):
                    wh = H.load_raw("iex", d)
                    for r in X.join([{"symbol": k[1], "entry_ts": k[2]} for k in lab_a if k[0] == d],
                                    X.arms(REF_DIR / f"{d}-REF-arms.jsonl.gz"), {s: wh.get(s) for s in wh}):
                        k = (d, r["symbol"], int(r["entry_ts"]))
                        sp = data.spread_bp(r["symbol"], r["entry_ts"])
                        g = label(None if r.get("rng") is None or not sp else r["rng"] * 100.0 / sp)
                        if lab_a[k] in ("LOW", "REST") and g in ("LOW", "REST"):
                            tot += 1
                            same += lab_a[k] == g
                agree = same / tot if tot else None
            except Exception as e:  # noqa: BLE001 — reported, not fatal
                agree = f"unavailable: {e}"
            res = {"sessions": len(ds), "mean_d": mean_d, "continue": mean_d is not None and mean_d < 0,
                   "label": None if (mean_d is not None and mean_d < 0) else "source-dependent",
                   "warehouse_agreement_10_14_15": agree, "void": void(rows),
                   "bands": {g: sum(r.get("group") == g for r in rows) for g in ("LOW", "REST", MISSING)}}
            (OUT / "recheck.json").write_text(json.dumps(res, indent=1, default=str))
            print(json.dumps(res, indent=1, default=str))
            return 0
        cnt = json.loads((OUT / "count.json").read_text()) if (OUT / "count.json").exists() else {}
        rec = json.loads((OUT / "recheck.json").read_text()) if (OUT / "recheck.json").exists() else {}
        if not cnt.get("powered"):
            print("UNDERPOWERED or VOID (count.json): no score", file=sys.stderr)
            return 2
        if not rec.get("continue") or rec.get("void"):
            print("STOP: the exploration split is source-dependent on Alpaca bars (recheck.json); no held-out score",
                  file=sys.stderr)
            return 2
        rows = load_rows(HOLDOUT, data, sip)
        no27 = [r for r in rows if r["day"] != "2026-10-27"]
        res = {"with_10_27": verdict_set(rows), "without_10_27": verdict_set(no27), "ref_vs_live": cnt.get("ref_vs_live_10_27")}
        rv = res["ref_vs_live"] or {}
        res["replay_only"] = (rv.get("recall") or 0) < 0.5 or (rv.get("precision") or 0) < 0.5
        res["verdict"] = ("HOLDS" + (" (replay-only)" if res["replay_only"] else "")
                          if res["with_10_27"]["holds"] and res["without_10_27"]["holds"] else "NOT SUPPORTED")
        res["note"] = "association only; a HOLDS earns the replay gate test, never a switch-on"
        (OUT / "result.json").write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps(res, indent=1, default=str))
        return 0
    finally:
        sip.save()


if __name__ == "__main__":
    sys.exit(main())
