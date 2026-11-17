#!/usr/bin/env python3
"""news_source - would Alpaca news add buy signals the desk does not already get, at no worse quality?
Pass bar: docs/studies/news_source_prereg.json (written before any news was fetched). Read-only; data-only Alpaca keys.

    .venv/bin/python tools/studies/news_source.py fetch     # cache news for the admission-ledger days (--all: every warehouse day)
    .venv/bin/python tools/studies/news_source.py run       # score -> ai_reports/news_source/result.json

A news name is a candidate from the minute of its FIRST story that day (pre-open stories count at 09:30). BASE = signals on the
tight-screener universe (top 400 liquid); NEWS_EXTRA = signals on news candidates outside that universe AND outside that day's
admission ledger (names the desk already had). Same signal (live pre-square, fast %R 21), admission ($20-$400, up >= 1.5% at the
signal minute), trade (open t+1 -> close t+10, 6 bp) as tools/studies/relative_admit.py.
"""
from __future__ import annotations

import gzip
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day  # noqa: E402
from board_structure_family import universe  # noqa: E402
from evening_family import minute_grid, presquare_bars, first_true, trade, WH_SIP, ALL_ETF  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = ROOT / "ai_reports" / "news_source"
NEWS = OUT / "news"
LEDGER_DIRS = (ROOT / "ai_reports" / "admit_ledger", Path.home() / "data_archive" / "admit_ledger")
SPREADS = ROOT / "ai_reports" / "combined_spreads.json"
BASE_PCT, PX_LO, PX_HI, H, COST, STRESS, GATE_BP, MINUTES = 1.5, 20.0, 400.0, 10, 6.0, 10.0, 15.0, 346


# ---------------- pure pieces ----------------
def first_story_minutes(articles, day: str) -> dict[str, int]:
    """[{symbols, ts}] -> {SYM: minute index since 09:30 of its first story that day}; pre-open -> 0, after 15:59 dropped."""
    out: dict[str, int] = {}
    for a in articles:
        t = datetime.fromtimestamp(float(a["ts"]), ET)
        if t.strftime("%Y-%m-%d") != day:
            continue
        m = max(0, t.hour * 60 + t.minute - 570)
        if m >= 390:
            continue
        for s in a.get("symbols") or []:
            s = str(s).upper()
            if s.isalpha() and len(s) <= 5 and (s not in out or m < out[s]):
                out[s] = m
    return out


def last_close(row):
    v = row[~np.isnan(row)]
    return float(v[-1]) if len(v) else float("nan")


def signals_for(day, prev, sym, spy_c, spy_pc, from_minute=0):
    """[{t, pct, spy, base, rel, net}] for one name: pre-square signals at/after from_minute inside the price band."""
    i, j = day["idx"].get(sym), prev["idx"].get(sym)
    if i is None or j is None:
        return None
    pc = last_close(prev["c"][j])
    if not pc > 0:
        return []
    cc, hh, ll, off = minute_grid(prev["c"][j], prev["h"][j], prev["l"][j], day["c"][i], day["h"][i], day["l"][i])
    out = []
    for t in first_true(presquare_bars(cc, hh, ll, off, 21)):
        if t < from_minute:
            continue
        px = day["c"][i][t]
        if np.isnan(px) or not (PX_LO <= px <= PX_HI) or np.isnan(spy_c[t]):
            continue
        net = trade(day["o"][i], day["c"][i], t, H, COST)
        if net is None:
            continue
        pct = (px / pc - 1) * 100
        spy = (spy_c[t] / spy_pc - 1) * 100
        out.append({"t": int(t), "pct": round(float(pct), 3), "spy": round(float(spy), 3), "base": bool(pct >= BASE_PCT),
                    "rel": bool(pct >= BASE_PCT + min(0.0, spy)), "net": round(float(net), 2)})
    return out


def quality(xs):
    if not xs:
        return {"n": 0}
    n = np.array([x["net"] for x in xs])
    return {"n": len(xs), "mean_net_bp": round(float(n.mean()), 2), "win_pct": round(float((n > 0).mean() * 100), 1),
            "mean_net_stress_bp": round(float(n.mean() - (STRESS - COST)), 2)}


def compare(days: dict, key: str = "base") -> dict:
    """days {day: {"base": [rows], "extra": [rows]}} -> per-10m rates, quality, day-clustered t of extra - base."""
    nd = len(days)
    b = [x for d in days.values() for x in d["base"] if x[key]]
    e = [x for d in days.values() for x in d["extra"] if x[key]]
    diffs = []
    for d in days.values():
        bb, ee = [x["net"] for x in d["base"] if x[key]], [x["net"] for x in d["extra"] if x[key]]
        if bb and ee:
            diffs.append(np.mean(ee) - np.mean(bb))
    sd = np.std(diffs, ddof=1) if len(diffs) >= 3 else 0.0
    return {"days": nd, "base_per_10m": round(len(b) / max(1, nd) / MINUTES * 10, 3),
            "extra_per_10m": round(len(e) / max(1, nd) / MINUTES * 10, 3),
            "extra_share_of_base": round(len(e) / len(b), 3) if b else None,
            "base": quality(b), "extra": quality(e), "days_with_both": len(diffs),
            "extra_minus_base_bp_daymean": round(float(np.mean(diffs)), 2) if diffs else None,
            "t_day_clustered": round(float(np.mean(diffs) / (sd / math.sqrt(len(diffs)))), 2) if sd > 0 else None}


def verdict(c: dict) -> tuple[str, list[str]]:
    """The prereg bar on the ledger-day comparison."""
    if c["days"] < 30 or c["extra"]["n"] < 300:
        return "UNDERPOWERED", [f"days {c['days']} (need 30), extra signals {c['extra']['n']} (need 300)"]
    fails = []
    if (c["extra_share_of_base"] or 0) < 0.15:
        fails.append(f"more_opens: extra {c['extra_share_of_base']} of base (need >= 0.15)")
    if c["extra"]["mean_net_bp"] < c["base"]["mean_net_bp"] - 1.0 or (c["t_day_clustered"] is not None and c["t_day_clustered"] <= -2.0):
        fails.append(f"quality_net: extra {c['extra']['mean_net_bp']} vs base {c['base']['mean_net_bp']} (t {c['t_day_clustered']})")
    if c["extra"]["win_pct"] < c["base"]["win_pct"] - 3:
        fails.append(f"quality_win: extra {c['extra']['win_pct']} vs base {c['base']['win_pct']}")
    return ("PASS" if not fails else "FAIL"), fails


# ---------------- io ----------------
def ledger_seen(day: str) -> set[str] | None:
    for d in LEDGER_DIRS:
        for p in (d / f"{day}.jsonl", d / f"{day}.jsonl.gz"):
            if p.exists():
                op = gzip.open if p.suffix == ".gz" else open
                seen = set()
                with op(p, "rt") as fh:
                    for line in fh:
                        try:
                            seen.add(json.loads(line)["symbol"])
                        except Exception:  # noqa: BLE001
                            continue
                return seen
    return None


def fetch(days, pause_sec: float = 15.0):
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest
    k = json.load(open(ROOT / "secrets_data.json"))
    nc = NewsClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    NEWS.mkdir(parents=True, exist_ok=True)
    for day in days:
        p = NEWS / f"{day}.json"
        if p.exists():
            continue
        a = datetime.fromisoformat(day + "T07:00").replace(tzinfo=ET)
        b = datetime.fromisoformat(day + "T15:30").replace(tzinfo=ET)
        arts = nc.get_news(NewsRequest(start=a, end=b, limit=10000, include_content=False)).data.get("news", [])
        rows = [{"ts": n.created_at.timestamp(), "symbols": list(n.symbols or [])} for n in arts]
        p.write_text(json.dumps(rows))
        print(day, len(rows), "articles", flush=True)
        import time
        time.sleep(pause_sec)        # the replays share this data account's rate budget


def run():
    files = sorted(WH_SIP.glob("20??-??-??.npz"))
    spreads = defaultdict(list)
    if SPREADS.exists():
        for kk, v in json.load(open(SPREADS)).items():
            if isinstance(v, (int, float)):          # the file holds some null readings
                spreads[kk.split("|")[0]].append(float(v))
    med_sp = {s: statistics.median(v) for s, v in spreads.items()}
    ledger_days, all_days, no_bars, n_news = {}, {}, 0, 0
    prev = None
    for f in files:
        day_s = f.stem
        day = load_day(f)
        npath = NEWS / f"{day_s}.json"
        if prev is not None and npath.exists():
            si, psi = day["idx"].get("SPY"), prev["idx"].get("SPY")
            if si is not None and psi is not None:
                spy_c, spy_pc = day["c"][si], last_close(prev["c"][psi])
                uni = universe(day, prev, set(ALL_ETF))
                uset = set(uni)
                first = first_story_minutes(json.load(open(npath)), day_s)
                n_news += len(first)
                base = [x | {"sym": s} for s in uni for x in (signals_for(day, prev, s, spy_c, spy_pc) or [])]
                seen = ledger_seen(day_s)
                extra_all, extra_led = [], []
                for s, m in first.items():
                    if s in uset or s in ALL_ETF:
                        continue
                    rows = signals_for(day, prev, s, spy_c, spy_pc, from_minute=m)
                    if rows is None:
                        no_bars += 1
                        continue
                    for x in rows:
                        x = x | {"sym": s, "gated": med_sp.get(s) is not None and med_sp[s] <= GATE_BP}
                        extra_all.append(x)
                        if seen is not None and s not in seen:
                            extra_led.append(x)
                all_days[day_s] = {"base": base, "extra": extra_all}
                if seen is not None:
                    ledger_days[day_s] = {"base": base, "extra": extra_led}
                print(day_s, "news", len(first), "base", len(base), "extra", len(extra_all),
                      "ledger-extra", len(extra_led) if seen is not None else "-", flush=True)
        prev = day
    primary = compare(ledger_days, "base")
    v, why = verdict(primary)
    gated = {d: {"base": r["base"], "extra": [x for x in r["extra"] if x.get("gated")]} for d, r in ledger_days.items()}
    res = {"generated": datetime.now(ET).isoformat(timespec="minutes"), "verdict": v, "fails": why,
           "primary_ledger_days": primary,
           "info_relative_bar": compare(ledger_days, "rel"),
           "info_all_days_not_in_top400": compare(all_days, "base"),
           "info_spread_gated_extra": compare(gated, "base"),
           "news_names_total": n_news, "news_names_without_bars": no_bars,
           "ledger_days": sorted(ledger_days)}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps(res, indent=1))
    print(json.dumps({k: res[k] for k in ("verdict", "fails", "primary_ledger_days")}, indent=1))


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] == "fetch":
        days = [f.stem for f in sorted(WH_SIP.glob("20??-??-??.npz"))]
        if "--all" not in argv:          # default: the ledger days (the prereg's primary sample) and the day before each
            keep = [d for d in days if ledger_seen(d) is not None]
            days = sorted(set(keep))
        fetch(days)
    elif argv and argv[0] == "run":
        run()
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
