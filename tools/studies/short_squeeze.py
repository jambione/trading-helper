#!/usr/bin/env python3
"""Short squeeze as a forced-buyer flow. Spec: docs/studies/short_squeeze_prereg.json (as amended).

A HIGH days-to-cover (top decile of FINRA's PUBLISHED report) stock that breaks out (+3%, 2x the 20-day median volume,
a 20-day high close) is bought at the NEXT day's close and held 5 trading days; vs NORMAL-DTC (< 70th pct) breakouts
matched on week, 60-day volatility tercile and 20-day dollar-volume tercile. Beta-adjusted returns; 10 bp on HIGH only.

  --coverage   per FINRA file: rows by marketClassCode, revision / split flags (format only) -> coverage.json
  --bars       Alpaca daily bars (data-only account), adjustment=all and raw, for the in-scope symbols + SPY
  (default)    count step: reports, publication dates, eligible universe, events per group / half, drops, own-DTC
               correlation, and the PRIMARY series' SEs and MDE (no mean is printed) -> count.json
  --score      verdict; refuses without --skeptic-ready and a count step from this code that cleared the power bar
"""
from __future__ import annotations

import argparse
import bisect
import csv
import glob
import hashlib
import json
import math
import os
import pickle
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(HERE))
SI_DIR = os.path.join(ROOT, "data", "finra_short_interest")
OUT_DIR = os.path.join(ROOT, "ai_reports", "short_squeeze")
PUB_LAG, PUB_LAG_INFO = 10, 12
MIN_PX, MIN_DV = 20.0, 20e6
HIGH_PCT, NORMAL_PCT = 0.90, 0.70
BRK_RET, BRK_VOL_X, LOOK = 0.03, 2.0, 20
BETA_DAYS, HOLD = 60, 5
COST, COST_HI = 0.0010, 0.0030
LIFT_BAR, T_PASS, MIN_EVENTS = 0.0050, 2.0, 150
H1_END = "2022-12-31"
FIRST_TRIGGER = "2020-02-01"
ETF_WORDS = ("ETF", "FUND", "TRUST", "ISHARES", "SPDR", "PROSHARES", "DIREXION", "ETN", "INDEX", "PORTFOLIO")
DROP_MAX, DROP_DIFF = 0.02, 0.01


def P(*a):
    print(*a, flush=True)


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def jload(name: str, default=None):
    p = os.path.join(OUT_DIR, name)
    return json.load(open(p)) if os.path.exists(p) else default


# ── FINRA files (pure parsing) ───────────────────────────────────────────────

def read_si(path: str) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f, delimiter="|"):
            rows.append(r)
    return rows


def fnum(x) -> float | None:
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None


def is_etf_like(name: str) -> bool:
    """Whole-word match on the issue name (so 'Trustmark' or 'Fundamental' are not caught)."""
    words = set("".join(ch if ch.isalnum() else " " for ch in (name or "").upper()).split())
    return any(w in words for w in ETF_WORDS)


def coverage() -> dict:
    out = {}
    for p in sorted(glob.glob(os.path.join(SI_DIR, "shrt*.csv"))):
        rows = read_si(p)
        out[os.path.basename(p)[4:12]] = {"rows": len(rows), "by_code": dict(Counter(r["marketClassCode"] for r in rows)),
                                          "revision": sum(1 for r in rows if r.get("revisionFlag")),
                                          "split": sum(1 for r in rows if r.get("stockSplitFlag"))}
    return out


# ── trading calendar and bars (pure helpers) ─────────────────────────────────

def add_trading_days(days: list[str], d: str, n: int) -> str | None:
    """The trading day n sessions after the first session on or after d."""
    i = bisect.bisect_left(days, d)
    j = i + n
    return days[j] if 0 <= j < len(days) else None


def beta(rn: list[float], rm: list[float]) -> float | None:
    if len(rn) < 20 or len(rn) != len(rm):
        return None
    mm = statistics.mean(rm)
    vm = sum((x - mm) ** 2 for x in rm)
    if vm <= 0:
        return None
    mn = statistics.mean(rn)
    return sum((a - mn) * (b - mm) for a, b in zip(rn, rm)) / vm


def is_breakout(closes: list[float], vols: list[float], i: int) -> bool:
    """Day i breaks out: close +3% vs i-1, volume >= 2 x median of i-20..i-1, close >= max close of i-20..i-1."""
    if i < LOOK or closes[i - 1] <= 0:
        return False
    prior_c, prior_v = closes[i - LOOK:i], vols[i - LOOK:i]
    return (closes[i] / closes[i - 1] - 1 >= BRK_RET and vols[i] >= BRK_VOL_X * statistics.median(prior_v)
            and closes[i] >= max(prior_c))


# ── statistics (pure) ────────────────────────────────────────────────────────

def t_ppf(p: float, df: int) -> float:
    sys.path.insert(0, HERE)
    import indicator_confirm_study as S
    return S.t_ppf(p, df)


def clustered_se(vals: list[float], keys: list) -> float | None:
    """CR1 SE of the mean of vals clustered by keys."""
    n = len(vals)
    if n < 2:
        return None
    mu = sum(vals) / n
    g = defaultdict(float)
    for v, k in zip(vals, keys):
        g[k] += v - mu
    G = len(g)
    if G < 2:
        return None
    return math.sqrt(G / (G - 1) * sum(x * x for x in g.values())) / n


def nw_se(series: list[float], lags: int = 5) -> float | None:
    """Newey-West SE of the mean of a time series (weekly calendar-time series here)."""
    n = len(series)
    if n < lags + 2:
        return None
    mu = sum(series) / n
    e = [x - mu for x in series]
    s = sum(x * x for x in e) / n
    for L in range(1, lags + 1):
        w = 1 - L / (lags + 1)
        s += 2 * w * sum(e[t] * e[t - L] for t in range(L, n)) / n
    return math.sqrt(max(s, 0.0) / n)


def week_of(d: str) -> str:
    y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def series_stats(ev: list[dict], key: str = "diff") -> dict:
    vals = [e[key] for e in ev if e.get(key) is not None]
    use = [e for e in ev if e.get(key) is not None]
    if len(vals) < 2:
        return {"n": len(vals), "mean": None, "se": None, "t": None, "ub": None}
    se_w = clustered_se(vals, [week_of(e["d0"]) for e in use])
    se_n = clustered_se(vals, [e["sym"] for e in use])
    wk = defaultdict(list)
    for e in use:
        wk[week_of(e["d0"])].append(e[key])
    se_c = nw_se([statistics.mean(wk[w]) for w in sorted(wk)])
    ses = [x for x in (se_w, se_n, se_c) if x]
    se = max(ses) if ses else None
    mu = sum(vals) / len(vals)
    return {"n": len(vals), "mean": mu, "se": se, "se_week": se_w, "se_name": se_n, "se_cal": se_c,
            "t": mu / se if se else None, "ub": mu + 1.96 * se if se else None}


def mde(st: dict) -> float | None:
    return 2.84 * st["se"] if st.get("se") else None


def half_verdict(st: dict, extra_ok: bool) -> str:
    if st["n"] < MIN_EVENTS or st["se"] is None:
        return "UNDERPOWERED"
    if mde(st) > LIFT_BAR:
        return "UNDERPOWERED"
    if st["t"] is not None and st["t"] <= -2:
        return "FAIL"
    if st["mean"] >= LIFT_BAR and st["t"] >= T_PASS and extra_ok:
        return "PASS"
    return "FAIL"                                   # amended (9): powered and not PASS = FAIL


def final(halves: list[str]) -> str:
    if all(h == "PASS" for h in halves):
        return "PASS (held-out 2017-2019 check and a forward prereg next)"
    if "UNDERPOWERED" in halves and "FAIL" not in halves:
        return "UNDERPOWERED"
    return "FAIL"


# ── events (pure on bars) ────────────────────────────────────────────────────

def build_reports(si_rows_by_date: dict, days: list[str]) -> list[dict]:
    """Per settlement date: publication = settlement + PUB_LAG trading days; rows with revision / split flags out."""
    reps = []
    for s, rows in sorted(si_rows_by_date.items()):
        sd = f"{s[:4]}-{s[4:6]}-{s[6:]}"
        pub = add_trading_days(days, sd, PUB_LAG)
        if pub is None:
            continue
        reps.append({"settle": sd, "pub": pub, "rows": rows})
    return reps


def eligible_at(bars_raw: dict, day_idx: dict, sym: str, d: str) -> bool:
    """$20 raw close and $20M mean 20-day dollar volume on the 20 sessions ending the session before d."""
    b = bars_raw.get(sym)
    if not b:
        return False
    i = bisect.bisect_left(b["d"], d) - 1
    if i < LOOK - 1:
        return False
    if b["c"][i] < MIN_PX:
        return False
    dv = [b["c"][k] * b["v"][k] for k in range(i - LOOK + 1, i + 1)]
    return statistics.mean(dv) >= MIN_DV


def pctile_cuts(xs: list[float]) -> tuple[float, float]:
    s = sorted(xs)

    def q(p):
        k = (len(s) - 1) * p
        lo = math.floor(k)
        return s[lo] + (s[min(lo + 1, len(s) - 1)] - s[lo]) * (k - lo)
    return q(NORMAL_PCT), q(HIGH_PCT)


CODES = ("NYSE", "NNM", "SC", "AMEX")          # amended_2: frozen from the coverage step
BARS_START = "2019-09-01"
SEED_TAG = "short-squeeze-random-day"


# ── data loading (mini) ──────────────────────────────────────────────────────

def load_reports_raw() -> dict:
    """settlement YYYYMMDD -> {sym: row} for in-scope, non-ETF, unflagged rows; plus drop counts."""
    out, st = {}, Counter()
    for p in sorted(glob.glob(os.path.join(SI_DIR, "shrt*.csv"))):
        d = os.path.basename(p)[4:12]
        rows = {}
        for r in read_si(p):
            if r.get("marketClassCode") not in CODES:
                continue
            if is_etf_like(r.get("issueName", "")):
                st["etf_like"] += 1
                continue
            if r.get("revisionFlag") or r.get("stockSplitFlag"):
                st["flagged"] += 1
                continue
            rows[r["symbolCode"]] = {"dtc": fnum(r.get("daysToCoverQuantity")),
                                     "short": fnum(r.get("currentShortPositionQuantity")),
                                     "chg": fnum(r.get("changePercent"))}
        out[d] = rows
    return out, st


def data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    s_ = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    return StockHistoricalDataClient(s_["alpaca_data_api_key"], s_["alpaca_data_secret_key"])


def fetch_bars(syms: list[str], adjustment: str) -> dict:
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    import time
    cl, out = data_client(), {}
    end = datetime.now(timezone.utc) - timedelta(days=1)
    adj = Adjustment.ALL if adjustment == "all" else Adjustment.RAW
    for k in range(0, len(syms), 100):
        ch = syms[k:k + 100]
        for attempt in range(5):
            try:
                got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=ch, timeframe=TimeFrame.Day,
                                                         start=datetime.strptime(BARS_START, "%Y-%m-%d").replace(
                                                             tzinfo=timezone.utc), end=end, feed=DataFeed.SIP,
                                                         adjustment=adj)).data or {}
                break
            except Exception as e:  # noqa: BLE001
                P(f"  retry {attempt + 1}: {str(e)[:100]}")
                time.sleep(10 * (attempt + 1))
        else:
            raise SystemExit(f"bars fetch failed at chunk {k}")
        for sym, bs in got.items():
            out[sym] = {"d": [b.timestamp.strftime("%Y-%m-%d") for b in bs], "o": [float(b.open) for b in bs],
                        "c": [float(b.close) for b in bs], "v": [float(b.volume) for b in bs]}
        if (k // 100) % 10 == 0:
            P(f"  {adjustment} {k + len(ch)}/{len(syms)}")
        time.sleep(0.5)
    return out


# ── events ───────────────────────────────────────────────────────────────────

def daily_rets(b: dict, i0: int, i1: int) -> dict:
    """date -> close-to-close return for indices i0+1 .. i1."""
    return {b["d"][k]: b["c"][k] / b["c"][k - 1] - 1 for k in range(max(1, i0 + 1), i1 + 1) if b["c"][k - 1] > 0}


def hold_return(b: dict, i_entry: int, hold: int) -> tuple:
    """Close of i_entry to the close `hold` sessions later (the last available close if the name stops trading)."""
    i_exit = min(i_entry + hold, len(b["c"]) - 1)
    return b["c"][i_exit] / b["c"][i_entry] - 1, b["d"][i_entry], b["d"][i_exit], i_exit - i_entry < hold


def spy_return(spy: dict, d_in: str, d_out: str) -> float | None:
    i, j = bisect.bisect_left(spy["d"], d_in), bisect.bisect_left(spy["d"], d_out)
    if i >= len(spy["d"]) or j >= len(spy["d"]) or spy["d"][i] != d_in or spy["d"][j] != d_out:
        return None
    return spy["c"][j] / spy["c"][i] - 1


_SPY_RETS: dict = {}


def event_features(b: dict, braw: dict, spy: dict, i0: int) -> dict | None:
    """Beta and 60-day volatility from the 60 sessions ending at i0 - 1; 20-day dollar volume (raw) ending i0 - 1."""
    if i0 < BETA_DAYS + 1:
        return None
    rn = daily_rets(b, i0 - BETA_DAYS - 1, i0 - 1)
    key = (id(spy), len(spy["c"]))
    if key not in _SPY_RETS:
        _SPY_RETS.clear()
        _SPY_RETS[key] = daily_rets(spy, 0, len(spy["c"]) - 1)
    sp = {d: r for d, r in _SPY_RETS[key].items() if d in rn}
    common = sorted(sp)
    bt = beta([rn[d] for d in common], [sp[d] for d in common])
    if bt is None:
        return None
    j = bisect.bisect_left(braw["d"], b["d"][i0]) - 1
    if j < LOOK - 1:
        return None
    dv = statistics.mean(braw["c"][k] * braw["v"][k] for k in range(j - LOOK + 1, j + 1))
    return {"beta": bt, "vol60": statistics.stdev(list(rn.values())), "dv20": dv}


def abnormal(b: dict, spy: dict, i_entry: int, bt: float, hold: int) -> tuple:
    r, d_in, d_out, short = hold_return(b, i_entry, hold)
    rm = spy_return(spy, d_in, d_out)
    return (None if rm is None else r - bt * rm), short


def build_events(si: dict, adj: dict, raw: dict, spy: dict, lag: int, st: Counter) -> list[dict]:
    days = spy["d"]
    reps = build_reports(si, days)
    if lag != PUB_LAG:
        reps = [dict(r, pub=add_trading_days(days, r["settle"], lag)) for r in reps]
        reps = [r for r in reps if r["pub"]]
    events, open_until = [], {}
    for k, rep in enumerate(reps):
        nxt = reps[k + 1]["pub"] if k + 1 < len(reps) else days[-1]
        elig = {s_: r for s_, r in rep["rows"].items() if r["dtc"] is not None and eligible_at(raw, {}, s_, rep["pub"])}
        if len(elig) < 50:
            st["report_too_small"] += 1
            continue
        lo, hi = pctile_cuts([r["dtc"] for r in elig.values()])
        st["reports_used"] += 1
        for sym, r in elig.items():
            grp = "HIGH" if r["dtc"] >= hi else "NORMAL" if r["dtc"] < lo else None
            if grp is None:
                continue
            b = adj.get(sym)
            if not b:
                st[f"{grp}_no_adjusted_bars"] += 1
                continue
            i_start = bisect.bisect_right(b["d"], rep["pub"])                # at least 1 session after publication
            i_end = bisect.bisect_right(b["d"], nxt)
            for i in range(i_start, i_end):
                if b["d"][i] < FIRST_TRIGGER or i <= open_until.get(sym, -1):
                    continue
                if not is_breakout(b["c"], b["v"], i) or not eligible_at(raw, {}, sym, b["d"][i]):
                    continue
                if i + 1 >= len(b["d"]):
                    st[f"{grp}_no_entry_bar"] += 1
                    break
                f = event_features(b, raw[sym], spy, i)
                if f is None:
                    st[f"{grp}_no_features"] += 1
                    break
                ab, short = abnormal(b, spy, i + 1, f["beta"], HOLD)
                if ab is None:
                    st[f"{grp}_no_spy"] += 1
                    break
                ev = {"sym": sym, "grp": grp, "d0": b["d"][i], "settle": rep["settle"], "dtc": r["dtc"],
                      "dtc_rank": sum(1 for x in elig.values() if x["dtc"] <= r["dtc"]) / len(elig),
                      "si_chg": r["chg"], "abn": ab, "short_hold": short, **f}
                for h in (1, 10, 20):
                    ev[f"abn{h}"] = abnormal(b, spy, i + 1, f["beta"], h)[0]
                ev["abn_d0close"] = abnormal(b, spy, i, f["beta"], HOLD)[0]          # information: not tradeable
                cands = [k2 for k2 in range(i_start, i_end) if k2 != i and k2 + 1 < len(b["d"])
                         and b["d"][k2] >= FIRST_TRIGGER and not is_breakout(b["c"], b["v"], k2)]
                if cands:
                    k2 = random.Random(int(hashlib.sha256(f"{SEED_TAG}|{sym}|{b['d'][i]}".encode()).hexdigest()[:16],
                                           16)).choice(cands)
                    f2 = event_features(b, raw[sym], spy, k2)
                    ev["abn_random_day"] = abnormal(b, spy, k2 + 1, f2["beta"], HOLD)[0] if f2 else None
                events.append(ev)
                st[f"{grp}_events"] += 1
                open_until[sym] = i + 1 + HOLD
                break                                                       # first trigger per name per window
    return events


def terciles(xs: list[float]) -> tuple[float, float]:
    s = sorted(xs)
    return s[len(s) // 3], s[(2 * len(s)) // 3]


def tbin(x: float, c: tuple) -> int:
    return 0 if x < c[0] else 1 if x < c[1] else 2


def match(events: list[dict]) -> tuple[list[dict], dict]:
    """amended (5): each HIGH event vs NORMAL events of the same ISO week, vol60 tercile and dv20 tercile (terciles
    cut on all H1 breakout events); diff = HIGH abnormal - COST - mean matched NORMAL abnormal (gross)."""
    h1 = [e for e in events if e["d0"] <= H1_END]
    cv, cd = terciles([e["vol60"] for e in h1]), terciles([e["dv20"] for e in h1])
    cell = defaultdict(list)
    for e in events:
        e["cell"] = (week_of(e["d0"]), tbin(e["vol60"], cv), tbin(e["dv20"], cd))
        if e["grp"] == "NORMAL":
            cell[e["cell"]].append(e)
    out, unmatched = [], 0
    for e in events:
        if e["grp"] != "HIGH":
            continue
        ns = cell.get(e["cell"], [])
        if not ns:
            unmatched += 1
            continue
        m = {k: statistics.mean(n[k] for n in ns if n.get(k) is not None) if any(n.get(k) is not None for n in ns)
             else None for k in ("abn", "abn1", "abn10", "abn20", "abn_d0close")}
        x = dict(e, n_matched=len(ns), diff=e["abn"] - COST - m["abn"])
        for h in ("1", "10", "20"):
            x[f"diff{h}"] = (e[f"abn{h}"] - COST - m[f"abn{h}"]) if (e.get(f"abn{h}") is not None and
                                                                     m[f"abn{h}"] is not None) else None
        out.append(x)
    return out, {"unmatched_high": unmatched, "vol_cuts": cv, "dv_cuts": cd}


def drop_top(ev: list[dict], key: str, k: int) -> float | None:
    tot = Counter()
    for e in ev:
        tot[e[key] if key == "sym" else week_of(e["d0"])] += e["diff"]
    top = {x for x, _ in tot.most_common(k)}
    rest = [e["diff"] for e in ev if (e[key] if key == "sym" else week_of(e["d0"])) not in top]
    return statistics.mean(rest) if rest else None


def spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None

    def rk(v):
        o = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(o):
            r[i] = pos
        return r
    rx, ry = rk(x), rk(y)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


def own_dtc_check(si: dict, raw: dict) -> dict:
    xs, ys = [], []
    for d, rows in si.items():
        sd = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        for sym, r in rows.items():
            b = raw.get(sym)
            if not b or r["dtc"] is None or not r["short"]:
                continue
            j = bisect.bisect_right(b["d"], sd) - 1
            if j < LOOK - 1:
                continue
            adv = statistics.mean(b["v"][j - LOOK + 1:j + 1])
            if adv > 0 and b["c"][j] >= MIN_PX:
                xs.append(r["dtc"])
                ys.append(r["short"] / adv)
    return {"spearman_finra_vs_own": spearman(xs[:20000], ys[:20000]), "n": len(xs)}


def module_shas() -> dict:
    return {"short_squeeze": hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--coverage", action="store_true")
    g.add_argument("--bars", action="store_true")
    g.add_argument("--score", action="store_true")
    ap.add_argument("--codes", default="", help="comma-separated marketClassCode list frozen from --coverage")
    ap.add_argument("--skeptic-ready", default="")
    a = ap.parse_args()
    if a.coverage:
        cov = coverage()
        P(json.dumps({k: v for k, v in list(cov.items())[:3]}, indent=1))
        P("codes over all files:", dict(sum((Counter(v["by_code"]) for v in cov.values()), Counter())))
        P(write("coverage.json", cov))
        return
    si, st0 = load_reports_raw()
    if a.bars:
        syms = sorted({x for rows in si.values() for x in rows})
        P(f"in-scope symbols {len(syms)}")
        raw = fetch_bars(syms + ["SPY"], "raw")
        pickle.dump(raw, open(os.path.join(OUT_DIR, "bars_raw.pkl"), "wb"))
        days = raw["SPY"]["d"]
        keep = set()
        for rep in build_reports(si, days):
            keep |= {x for x in rep["rows"] if eligible_at(raw, {}, x, rep["pub"])}
        P(f"symbols passing the screen at some publication: {len(keep)}; no raw bars: "
          f"{sum(1 for x in syms if x not in raw)}")
        adj = fetch_bars(sorted(keep) + ["SPY"], "all")
        pickle.dump(adj, open(os.path.join(OUT_DIR, "bars_adj.pkl"), "wb"))
        write("bars_meta.json", {"symbols": len(syms), "raw_with_bars": len(raw), "screen_pass": len(keep),
                                 "adj_with_bars": len(adj), "no_raw_bars": sum(1 for x in syms if x not in raw)})
        return
    raw = pickle.load(open(os.path.join(OUT_DIR, "bars_raw.pkl"), "rb"))
    adj = pickle.load(open(os.path.join(OUT_DIR, "bars_adj.pkl"), "rb"))
    spy = adj["SPY"]
    st = Counter(st0)
    events = build_events(si, adj, raw, spy, PUB_LAG, st)
    paired, minfo = match(events)
    halves = {"A": [e for e in paired if e["d0"] <= H1_END], "B": [e for e in paired if e["d0"] > H1_END]}
    n_ev = Counter(e["grp"] for e in events)
    drops = {g: sum(v for k, v in st.items() if k.startswith(g + "_no")) for g in ("HIGH", "NORMAL")}
    drop_rate = {g: drops[g] / max(1, drops[g] + n_ev[g]) for g in drops}
    abort = max(drop_rate.values()) > DROP_MAX or abs(drop_rate["HIGH"] - drop_rate["NORMAL"]) > DROP_DIFF
    if not a.score:
        cnt = {"stats": dict(st), "events": dict(n_ev), "match": minfo, "drop_rate": drop_rate, "abort": abort,
               "own_dtc": own_dtc_check(si, raw), "module_sha256": module_shas()}
        for h, ev in halves.items():
            s_ = series_stats(ev)
            cnt[h] = {"matched_high": s_["n"], "se": s_["se"], "se_week": s_["se_week"], "se_name": s_["se_name"],
                      "se_cal": s_["se_cal"], "mde": mde(s_), "normal_events": sum(1 for e in events if e["grp"] ==
                                                                                 "NORMAL" and (e["d0"] <= H1_END) ==
                                                                                 (h == "A")),
                      "power_ok": s_["n"] >= MIN_EVENTS and mde(s_) is not None and mde(s_) <= LIFT_BAR}
        P(json.dumps(cnt, indent=1, default=str))       # SEs and counts only; no mean is printed
        P(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = jload("count.json")
    if not cnt or cnt.get("module_sha256") != module_shas():
        raise SystemExit("count.json missing or from different code")
    if abort:
        raise SystemExit(f"drop rule tripped: {drop_rate}")
    ev12 = build_events(si, adj, raw, spy, PUB_LAG_INFO, Counter())
    p12, _ = match(ev12)
    res = {"halves": {}, "verdict": None, "info": {}}
    for h, ev in halves.items():
        s_ = series_stats(ev)
        hi30 = statistics.mean(e["abn"] - COST_HI for e in ev) if ev else None
        dw, dn = drop_top(ev, "week", 3), drop_top(ev, "sym", 5)
        lag12 = [e["diff"] for e in p12 if (e["d0"] <= H1_END) == (h == "A")]
        l12 = statistics.mean(lag12) if lag12 else None
        extra = all(x is not None and x > 0 for x in (hi30, dw, dn, l12))
        res["halves"][h] = {**s_, "mde": mde(s_), "high_own_at_30bp": hi30, "drop_top3_weeks": dw,
                            "drop_top5_names": dn, "lag12_mean": l12, "verdict": half_verdict(s_, extra)}
        hi = [e for e in events if e["grp"] == "HIGH" and (e["d0"] <= H1_END) == (h == "A")]
        no = [e for e in events if e["grp"] == "NORMAL" and (e["d0"] <= H1_END) == (h == "A")]

        def mean_of(xs, k):
            v = [x[k] for x in xs if x.get(k) is not None]
            return statistics.mean(v) if v else None

        def did(xs):
            v = [x["abn"] - x["abn_random_day"] for x in xs if x.get("abn_random_day") is not None]
            return statistics.mean(v) if v else None
        res["info"][h] = {"unmatched_high_minus_normal": (mean_of(hi, "abn") or 0) - (mean_of(no, "abn") or 0),
                          "did_breakout_minus_random": (did(hi) or 0) - (did(no) or 0),
                          "high_random_day": mean_of(hi, "abn_random_day"),
                          "holds": {k: series_stats(ev, k)["mean"] for k in ("diff1", "diff10", "diff20")},
                          "entry_at_d0_close": mean_of(hi, "abn_d0close"),
                          "si_rising": series_stats([e for e in ev if (e.get("si_chg") or 0) > 0])["mean"],
                          "si_falling": series_stats([e for e in ev if (e.get("si_chg") or 0) <= 0])["mean"],
                          "meme_2020_2021": series_stats([e for e in ev if e["d0"] <= "2021-12-31"])["mean"]}
    res["verdict"] = final([res["halves"][h]["verdict"] for h in ("A", "B")])
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    P(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
