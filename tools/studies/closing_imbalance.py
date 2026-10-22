#!/usr/bin/env python3
"""Closing-auction imbalance, stage 1. Spec: docs/studies/closing_auction_imbalance_prereg.json (as amended).

At T = 15:50:00 ET, names whose first Nasdaq NOII closing-cross message (<= 5 s after T) shows a top-decile signed BUY
imbalance (imbalance shares x reference price / 20-day mean raw SIP dollar volume) are bought at the SIP ask <= T + 5 s
and sold at the official closing-cross print; vs in-universe Nasdaq names with neither a top-decile buy nor a top-decile
sell imbalance, same session. 40 seeded sessions 2026-05-11 .. 10-15, halves at 2026-07-29.

Steps (in order; Databento steps run in the separate venv, e.g. /tmp/dbn/venv; the rest in the desk venv):
  --price      metadata.get_cost for every session request (free); abort if the total > $25                [dbn venv]
  --probe      ONE small download (one session, 15:50:00-15:50:10) -> field inventory for the amended (6)
               checks (auction types, sides, cadence, first-message time, price fields); refuses without
               --billing-ok                                                                                  [dbn venv]
  --fetch      every session 15:50:00-15:56:00, all symbols, priced first, cumulative cap $25; needs
               --billing-ok and probe.json                                                                   [dbn venv]
  --extract    first closing-cross message per name at >= 15:50:00 (<= 5 s) and >= 15:55:00 -> noii.json     [dbn venv]
  --adv        20-prior-session mean raw SIP daily dollar volume per in-universe name (Alpaca, free)        [desk venv]
  (default)    outcome-blind count step: cuts from half A, BUY / SELL / control counts, floors, projected MDE
               from warehouse sessions OUTSIDE the sample -> count.json                                      [desk venv]
  --outcomes   SIP quotes [T - 5 s, T + 5 s] and the official closing print per in-universe name (Alpaca, free;
               refuses before count.json)                                                                    [desk venv]
  --score      verdicts; refuses without --skeptic-ready and matching module shas                           [desk venv]

The Databento key is read from config/secrets.json.databento and is never printed.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(HERE))
OUT_DIR = os.path.join(ROOT, "ai_reports", "closing_imbalance")
DBN_DIR = os.path.join(OUT_DIR, "dbn")
DS, SCHEMA = "XNAS.ITCH", "imbalance"
FIRST, LAST, HALF_A_END = "2026-05-11", "2026-10-15", "2026-07-29"
SEED_TAG, PER_HALF = "closing-imbalance-stage1", 20
T_HMS, WIN_END_HMS, LATE_HMS = (15, 50, 0), (15, 56, 0), (15, 55, 0)
FIRST_MSG_S, QUOTE_AGE_S = 5.0, 5.0
CAP_USD, PROBE_CAP_USD = 25.0, 1.0
ADV_DAYS, DECILE = 20, 0.90
MIN_BUY_PER_SESSION = 3
FLOORS = (300, 15)                       # BUY entries, sessions with >= 3 BUY names, per half
LIFT_BAR, T_PASS = 0.0005, 2.0
EXIT_FEE = 0.00003                       # SEC / TAF on the sell (amended NOTES)
CLOSE_CODES, CLOSE_PREFER = {"6", "M"}, "M"
# amended (8): flagged days (reported with and without; the verdict uses all days)
FLAG_DAYS = {"2026-05-29": "MSCI semi-annual", "2026-06-19": "quad witching", "2026-06-26": "Russell reconstitution",
             "2026-08-31": "MSCI quarterly", "2026-09-18": "quad witching"}
# amended (6): Databento imbalance fields, confirmed from probe.json before any outcome is read
F_TYPE, F_SIDE, F_IMB, F_PAIRED, F_REF = "auction_type", "side", "total_imbalance_qty", "paired_qty", "ref_price"
CLOSE_TYPE, BUY_SIDE, SELL_SIDE = "C", "B", "A"


def P(*a):
    print(*a, flush=True)


def jload(name: str, default=None):
    p = os.path.join(OUT_DIR, name)
    return json.load(open(p)) if os.path.exists(p) else default


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def et_ts(day: str, hms: tuple) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=hms[0], minute=hms[1], second=hms[2], tzinfo=ET)


def half_of(day: str) -> str:
    return "A" if day <= HALF_A_END else "B"


# ── the sample ───────────────────────────────────────────────────────────────

def draw_sessions(days: list[str]) -> list[str]:
    """20 sessions per half from the warehouse days in [FIRST, LAST], seeded by sha256(SEED_TAG); sorted."""
    rng = random.Random(int(hashlib.sha256(SEED_TAG.encode()).hexdigest(), 16))
    pool = sorted(d for d in days if FIRST <= d <= LAST)
    out = []
    for h in ("A", "B"):
        hd = [d for d in pool if half_of(d) == h]
        out += rng.sample(hd, min(PER_HALF, len(hd)))
    return sorted(out)


def warehouse_days() -> list[str]:
    """Same as indicator_confirm_hist.warehouse_days, stdlib only (the dbn venv has no desk modules)."""
    wh = os.path.join(ROOT, "ai_reports", "warehouse")
    have = [{x[:-4] for x in os.listdir(os.path.join(wh, f)) if x.endswith(".npz")} for f in ("iex", "sip")]
    return sorted(have[0] & have[1])


def sessions() -> list[str]:
    s = jload("sessions.json")
    if s is None:
        s = {"sessions": draw_sessions(warehouse_days())}
        write("sessions.json", s)
    return s["sessions"]


# ── Databento (dbn venv) ─────────────────────────────────────────────────────

def dbn_client():
    import databento as db
    return db.Historical(json.load(open(os.path.join(ROOT, "config", "secrets.json.databento")))["api_key"])


def _req(day: str, end_hms: tuple = WIN_END_HMS) -> dict:
    return {"dataset": DS, "schema": SCHEMA, "symbols": "ALL_SYMBOLS",
            "start": et_ts(day, T_HMS).astimezone(timezone.utc).isoformat(),
            "end": et_ts(day, end_hms).astimezone(timezone.utc).isoformat()}


def spend_log(day: str, quoted: float, what: str) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, "spend_log.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["utc", "day", "what", "quoted_usd"])
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), day, what, f"{quoted:.6f}"])


def price_all(cl, days: list[str]) -> dict:
    per = {d: float(cl.metadata.get_cost(**_req(d))) for d in days}
    return {"per_session": per, "total": sum(per.values()), "cap": CAP_USD, "ok": sum(per.values()) <= CAP_USD}


def probe(cl, day: str) -> dict:
    req = _req(day, (15, 50, 10))
    cost = float(cl.metadata.get_cost(**req))
    if cost > PROBE_CAP_USD:
        raise SystemExit(f"probe quoted ${cost:.4f} > ${PROBE_CAP_USD}")
    spend_log(day, cost, "probe")
    df = cl.timeseries.get_range(**req).to_df()
    return {"day": day, "quoted_usd": cost, "rows": len(df), "columns": list(df.columns),
            "dtypes": {c: str(t) for c, t in df.dtypes.items()}, **inventory(df)}


def inventory(df) -> dict:
    """amended (6): what the data must confirm before any outcome is read."""
    import pandas as pd
    out = {}
    for c in (F_TYPE, F_SIDE, "unpaired_side", "auction_status", "significant_imbalance"):
        if c in df.columns:
            out[f"values {c}"] = {str(k): int(v) for k, v in df[c].astype(str).value_counts().head(12).items()}
    mask = (df[F_TYPE].astype(str) == CLOSE_TYPE) if F_TYPE in df.columns else (df.iloc[:, 0] != df.iloc[:, 0])
    close = df[mask]
    if len(close):
        cts = _ts(close)
        first = cts.groupby(close["symbol"].values).min().dt.tz_convert(ET)
        out["close_symbols"] = int(close["symbol"].nunique())
        out["first_msg_et quantiles"] = {q: str(first.quantile(q).time()) for q in (0.0, 0.5, 0.99, 1.0)}
        gaps = cts.groupby(close["symbol"].values).apply(lambda x: x.sort_values().diff().dt.total_seconds().median())
        out["median inter-message gap s (per symbol, median)"] = float(gaps.median()) if len(gaps) else None
        sample = close.head(5)
        out["sample rows"] = [{k: str(v) for k, v in r.items()} for r in sample.reset_index().to_dict("records")]
        for c in (F_REF, "cont_book_clr_price", "auct_interest_clr_price", F_IMB, F_PAIRED):
            if c in close.columns:
                out[f"describe {c}"] = {k: float(v) for k, v in close[c].describe().items()}
    return out


def _ts(df):
    """ts_event as a UTC Series aligned to df (to_df() indexes by ts_recv and keeps ts_event as a column)."""
    import pandas as pd
    if "ts_event" in df.columns:
        return pd.Series(pd.to_datetime(df["ts_event"], utc=True).values, index=df.index)
    return pd.Series(pd.to_datetime(df.index, utc=True), index=df.index)


def extract_day(path: str, day: str) -> dict:
    """{sym: {"t": first closing message at >= T within 5 s, "late": first at >= 15:55:00 within 5 s, "n": messages}}"""
    import databento as db
    df = db.DBNStore.from_file(path).to_df()
    df = df[df[F_TYPE].astype(str) == CLOSE_TYPE]
    df = df.assign(_t=_ts(df).values).sort_values("_t")
    out = {}
    t0, t1 = et_ts(day, T_HMS).timestamp(), et_ts(day, LATE_HMS).timestamp()
    for sym, g in df.groupby("symbol"):
        secs = [x.timestamp() for x in g["_t"]]
        rec = {"n": int(len(g))}
        for key, start in (("t", t0), ("late", t1)):
            i = bisect.bisect_left(secs, start)
            if i < len(secs) and secs[i] - start <= FIRST_MSG_S:
                r = g.iloc[i]
                rec[key] = {"ts": float(secs[i]), "side": str(r[F_SIDE]), "imb": float(r[F_IMB]),
                            "paired": float(r[F_PAIRED]), "ref": float(r[F_REF])}
        out[str(sym)] = rec
    return out


# ── signal (pure) ────────────────────────────────────────────────────────────

def signed_imb(msg: dict | None, adv: float | None) -> float | None:
    """amended (4): signed IMB = +/- imbalance shares x reference price / ADV (buy positive, sell negative, none 0)."""
    if not msg or not adv or adv <= 0 or not msg.get("ref") or msg["ref"] <= 0:
        return None
    sign = 1.0 if msg["side"] == BUY_SIDE else -1.0 if msg["side"] == SELL_SIDE else 0.0
    return sign * msg["imb"] * msg["ref"] / adv


def quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo = math.floor(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def cuts(imb_a: list[float]) -> tuple[float, float]:
    """amended (4): BUY cut = the 90th percentile of half A's pooled signed IMB; SELL cut = the 10th percentile."""
    return quantile(imb_a, DECILE), quantile(imb_a, 1 - DECILE)


def group(imb: float, side: str, buy_cut: float, sell_cut: float) -> str:
    if side == BUY_SIDE and imb > 0 and imb >= buy_cut:
        return "BUY"
    if side == SELL_SIDE and imb < 0 and imb <= sell_cut:
        return "SELL"
    return "CTRL"


# ── statistics (pure) ────────────────────────────────────────────────────────

def t_ppf(p: float, df: int) -> float:
    sys.path.insert(0, HERE)
    import indicator_confirm_study as S
    return S.t_ppf(p, df)


def session_diffs(rows: list[dict], key: str = "gross", grp: str = "BUY") -> dict:
    """Per session: mean `key` of `grp` names minus mean of CTRL names; sessions with >= 3 `grp` names."""
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.get(key) is not None:
            by[r["day"]][r["group"]].append(r[key])
    return {d: statistics.mean(g[grp]) - statistics.mean(g["CTRL"])
            for d, g in by.items() if len(g[grp]) >= MIN_BUY_PER_SESSION and g["CTRL"]}


def day_t(vals: dict) -> tuple:
    xs = list(vals.values())
    if len(xs) < 2:
        return (xs[0] if xs else None), None, len(xs), None
    mu, sd = statistics.mean(xs), statistics.stdev(xs)
    se = sd / math.sqrt(len(xs))
    t = mu / se if se > 0 else None
    return mu, t, len(xs), (mu + t_ppf(0.975, len(xs) - 1) * se if se > 0 else None)


def grade(rows: list[dict]) -> dict:
    diffs = session_diffs(rows)
    mu, t, n, ub = day_t(diffs)
    buy = [r for r in rows if r["group"] == "BUY" and r.get("gross") is not None]
    net_by = defaultdict(list)
    for r in buy:
        if r.get("net") is not None and r["day"] in diffs:
            net_by[r["day"]].append(r["net"])
    net = statistics.mean(statistics.mean(v) for v in net_by.values()) if net_by else None
    # drop-3: shares of the summed (BUY gross - that session's CTRL mean) by session / by name
    ctrl = {d: statistics.mean([r["gross"] for r in rows if r["day"] == d and r["group"] == "CTRL"
                                and r.get("gross") is not None] or [0.0]) for d in diffs}
    sh_d, sh_n = Counter(), Counter()
    for r in buy:
        if r["day"] in diffs:
            sh_d[r["day"]] += r["gross"] - ctrl[r["day"]]
            sh_n[r["sym"]] += r["gross"] - ctrl[r["day"]]
    top_d = {d for d, _ in sh_d.most_common(3)}
    top_n = {s for s, _ in sh_n.most_common(3)}
    drop_d = day_t({d: v for d, v in diffs.items() if d not in top_d})[0]
    drop_n = day_t(session_diffs([r for r in rows if not (r["group"] == "BUY" and r["sym"] in top_n)]))[0]
    n_buy = sum(1 for r in buy if r["day"] in diffs)
    return {"lift": mu, "t": t, "sessions": n, "ub": ub, "n_buy": n_buy, "net": net, "drop_sessions": drop_d,
            "drop_names": drop_n}


def half_verdict(g: dict) -> str:
    if g["t"] is not None and g["t"] <= -2:
        return "FAIL"
    if g["n_buy"] < FLOORS[0] or g["sessions"] < FLOORS[1] or g["t"] is None:
        return "UNDERPOWERED"
    ok = (g["lift"] >= LIFT_BAR and g["t"] >= T_PASS and g["net"] is not None and g["net"] > 0
          and g["drop_sessions"] is not None and g["drop_sessions"] >= 0
          and g["drop_names"] is not None and g["drop_names"] >= 0)
    return "PASS" if ok else "NOT_PASSED"


def final(halves: list[str], ubs: list, sessions_n: list[int]) -> str:
    if "FAIL" in halves:
        return "FAIL"
    if all(h == "PASS" for h in halves):
        return "PASS"
    if all(n >= 2 for n in sessions_n) and all(u is not None and u < LIFT_BAR for u in ubs):
        return "FAIL"
    return "UNDERPOWERED"


# ── desk-venv steps ──────────────────────────────────────────────────────────

def module_shas() -> dict:
    out = {}
    for name in ("closing_imbalance", "indicator_confirm_hist", "indicator_confirm_study"):
        p = os.path.join(HERE, name + ".py")
        out[name] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _retry(f, *a, **k):
    for i in range(5):
        try:
            return f(*a, **k)
        except Exception as e:  # noqa: BLE001
            if i == 4:
                raise
            P(f"  retry {i + 1}: {str(e)[:120]}")
            time.sleep(3 * (i + 1))


def universe(day: str, noii_day: dict) -> list[str]:
    """In-universe = warehouse (liquid) names that day that carry a Nasdaq closing-cross NOII message (Nasdaq-listed is
    taken from the NOII data itself, amended NOTES)."""
    sys.path.insert(0, HERE)
    import indicator_confirm_hist as H
    return sorted(set(H.load_raw("sip", day)) & set(noii_day))


def adv_fetch(cl, day: str, syms: list[str]) -> dict:
    """amended (2): mean raw SIP daily dollar volume over the 20 sessions before `day` (Alpaca daily bars)."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    start = et_ts(day, (0, 0, 0)) - timedelta(days=40)
    out = defaultdict(list)
    for ch in _chunks(syms, 100):
        got = _retry(cl.get_stock_bars, StockBarsRequest(
            symbol_or_symbols=ch, timeframe=TimeFrame.Day, start=start.astimezone(timezone.utc),
            end=et_ts(day, (0, 0, 0)).astimezone(timezone.utc), feed=DataFeed.SIP, adjustment=Adjustment.RAW)).data
        for s, bs in (got or {}).items():
            for b in bs:
                d = b.timestamp.astimezone(ET).strftime("%Y-%m-%d")
                if d < day:
                    out[s].append((d, float(b.close) * float(b.volume)))
    res = {}
    for s, v in out.items():
        v = sorted(v)[-ADV_DAYS:]
        res[s] = statistics.mean(x for _, x in v) if len(v) == ADV_DAYS else None
    return res


def build_rows(sess: list[str], noii: dict, adv: dict) -> tuple[list[dict], Counter]:
    rows, st = [], Counter()
    for day in sess:
        nd = noii.get(day, {})
        for s in universe(day, nd):
            msg = nd[s].get("t")
            if msg is None:
                st["no_msg_at_T"] += 1
                continue
            imb = signed_imb(msg, adv.get(day, {}).get(s))
            if imb is None:
                st["no_adv_or_ref"] += 1
                continue
            rows.append({"day": day, "sym": s, "half": half_of(day), "imb": imb, "side": msg["side"],
                         "msg_ts": msg["ts"]})
    return rows, st


def count_step(rows: list[dict], sess: list[str], st: Counter) -> dict:
    bc, sc = cuts([r["imb"] for r in rows if r["half"] == "A"])
    for r in rows:
        r["group"] = group(r["imb"], r["side"], bc, sc)
    cnt = {"buy_cut": bc, "sell_cut": sc, "drops": dict(st)}
    for h in ("A", "B"):
        rs = [r for r in rows if r["half"] == h]
        per = Counter(r["day"] for r in rs if r["group"] == "BUY")
        ok_s = [d for d, n in per.items() if n >= MIN_BUY_PER_SESSION]
        cnt[h] = {"sessions": len({r["day"] for r in rs}), "names": len(rs),
                  "BUY": sum(per.values()), "SELL": sum(1 for r in rs if r["group"] == "SELL"),
                  "CTRL": sum(1 for r in rs if r["group"] == "CTRL"),
                  "sessions_with_3_buy": len(ok_s), "BUY_in_those": sum(per[d] for d in ok_s),
                  "median_buy_per_session": statistics.median(per.values()) if per else 0,
                  "flagged_sessions": sorted(d for d in {r["day"] for r in rs} if d in FLAG_DAYS)}
        cnt[h]["floors_met"] = cnt[h]["BUY_in_those"] >= FLOORS[0] and len(ok_s) >= FLOORS[1]
    return cnt


def projected_mde(sess: list[str], k_by_half: dict) -> dict:
    """amended (7): MDE from warehouse sessions OUTSIDE the sample: per session, k seeded random in-universe names vs
    the rest, gross from the 15:50 bar open to the 15:59 bar close (SIP 1-minute bars; a projection only)."""
    sys.path.insert(0, HERE)
    import indicator_confirm_hist as H
    out = {}
    for h in ("A", "B"):
        k = max(MIN_BUY_PER_SESSION, int(round(k_by_half[h])))
        diffs = []
        for day in [d for d in H.warehouse_days() if FIRST <= d <= LAST and half_of(d) == h and d not in sess]:
            g = {}
            for s, r in H.load_raw("sip", day).items():
                b = H.sip_oc(r)
                if 950 in b and 959 in b and b[950][0] > 0:
                    g[s] = b[959][1] / b[950][0] - 1
            if len(g) <= k:
                continue
            names = sorted(g)
            pick = set(random.Random(H._seed(day, SEED_TAG, "mde")).sample(names, k))
            diffs.append(statistics.mean(g[s] for s in pick) - statistics.mean(g[s] for s in names if s not in pick))
        n = PER_HALF
        sd = statistics.stdev(diffs) if len(diffs) >= 2 else None
        out[h] = {"outside_sessions": len(diffs), "k": k,
                  "mde": (t_ppf(0.975, n - 1) + 0.84) * sd / math.sqrt(n) if sd else None}
    return out


def outcomes_day(cl, day: str, syms: list[str]) -> dict:
    """SIP quotes [T - 5 s, T + 5 s] (entry: the last clean quote <= T + 5 s, <= 5 s old) and the official closing print
    (condition M preferred, else the largest '6' print) in 16:00:00 - 16:01:00."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest, StockTradesRequest
    t = et_ts(day, T_HMS).timestamp()
    out = {s: {} for s in syms}
    for ch in _chunks(syms, 50):
        got = _retry(cl.get_stock_quotes, StockQuotesRequest(
            symbol_or_symbols=ch, start=datetime.fromtimestamp(t - QUOTE_AGE_S, timezone.utc),
            end=datetime.fromtimestamp(t + FIRST_MSG_S + 0.001, timezone.utc), feed=DataFeed.SIP)).data
        for s, qs in (got or {}).items():
            clean = sorted((q.timestamp.timestamp(), float(q.bid_price or 0), float(q.ask_price or 0)) for q in qs
                           if (q.bid_price or 0) > 0 and (q.ask_price or 0) > (q.bid_price or 0)
                           and (q.bid_size or 0) > 0 and (q.ask_size or 0) > 0)
            q = last_at(clean, t + FIRST_MSG_S, QUOTE_AGE_S)
            if q:
                out[s]["bid"], out[s]["ask"], out[s]["q_ts"] = q[1], q[2], q[0]
        got = _retry(cl.get_stock_trades, StockTradesRequest(
            symbol_or_symbols=ch, start=et_ts(day, (16, 0, 0)).astimezone(timezone.utc),
            end=et_ts(day, (16, 1, 0)).astimezone(timezone.utc), feed=DataFeed.SIP)).data
        for s, trs in (got or {}).items():
            px, code = close_print([(float(x.price), float(x.size), set(x.conditions or [])) for x in trs])
            if px:
                out[s]["close"], out[s]["close_code"] = px, code
    return out


def last_at(qs: list, t: float, max_age: float):
    i = bisect.bisect_right([q[0] for q in qs], t) - 1
    return qs[i] if i >= 0 and t - qs[i][0] <= max_age else None


def close_print(trades: list[tuple]) -> tuple:
    """amended (5): the official closing-cross print: the largest 'M' print, else the largest '6' print, else None."""
    off = [x for x in trades if CLOSE_PREFER in x[2]]
    if off:
        return max(off, key=lambda x: x[1])[0], CLOSE_PREFER
    six = [x for x in trades if x[2] & CLOSE_CODES]
    if six:
        return max(six, key=lambda x: x[1])[0], "6"
    return None, None


def returns(o: dict) -> dict:
    if not o.get("close") or not o.get("ask") or not o.get("bid"):
        return {"gross": None, "net": None}
    mid = (o["bid"] + o["ask"]) / 2
    return {"gross": o["close"] / mid - 1, "net": o["close"] / o["ask"] - 1 - EXIT_FEE}


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    for f in ("--price", "--probe", "--fetch", "--extract", "--adv", "--outcomes", "--score"):
        g.add_argument(f, action="store_true")
    ap.add_argument("--billing-ok", default="", help="the operator's confirmation of the Databento balance / spend limit")
    ap.add_argument("--skeptic-ready", default="")
    a = ap.parse_args()
    if a.price:
        days = sessions()
        res = price_all(dbn_client(), days)
        P(json.dumps({k: res[k] for k in ("total", "cap", "ok")}), f"over {len(days)} sessions")
        write("price.json", res)
        if not res["ok"]:
            raise SystemExit("over the cap: shrink sessions (never names) and re-price")
        return
    if a.probe or a.fetch:
        if not a.billing_ok:
            raise SystemExit("needs --billing-ok (the operator confirms balance and spend limit first)")
        cl = dbn_client()
        if a.probe:
            res = probe(cl, sessions()[0])
            res["billing_ok"] = a.billing_ok
            P(json.dumps(res, indent=1, default=str))
            write("probe.json", res)
            return
        pr = jload("price.json")
        if not jload("probe.json") or not pr or not pr["ok"]:
            raise SystemExit("needs a passed --price and --probe first")
        os.makedirs(DBN_DIR, exist_ok=True)
        spent = sum(float(r["quoted_usd"]) for r in csv.DictReader(open(os.path.join(OUT_DIR, "spend_log.csv"))))
        for day in sessions():
            path = os.path.join(DBN_DIR, f"{day}.dbn.zst")
            if os.path.exists(path):
                continue
            cost = float(cl.metadata.get_cost(**_req(day)))
            if spent + cost > CAP_USD:
                raise SystemExit(f"cap: spent ${spent:.4f} + ${cost:.4f} > ${CAP_USD}")
            spend_log(day, cost, "fetch")
            spent += cost
            _retry(cl.timeseries.get_range, **_req(day), path=path + ".tmp")
            os.replace(path + ".tmp", path)
            P(f"  {day} ${cost:.4f} (total ${spent:.4f})")
        return
    if a.extract:
        noii = {d: extract_day(os.path.join(DBN_DIR, f"{d}.dbn.zst"), d) for d in sessions()}
        P({d: len(v) for d, v in noii.items()})
        write("noii.json", {"noii": noii, "module_sha256": module_shas()})
        return
    sess = sessions()
    noii = jload("noii.json")["noii"]
    if a.adv:
        cl, adv = data_client(), {}
        for d in sess:
            adv[d] = adv_fetch(cl, d, universe(d, noii.get(d, {})))
            P(f"  adv {d}: {sum(1 for v in adv[d].values() if v)} names")
        write("adv.json", adv)
        return
    adv = jload("adv.json")
    rows, st = build_rows(sess, noii, adv)
    if not (a.outcomes or a.score):
        cnt = count_step(rows, sess, st)
        cnt["mde"] = projected_mde(sess, {h: cnt[h]["median_buy_per_session"] for h in ("A", "B")})
        cnt["module_sha256"] = module_shas()
        P(json.dumps(cnt, indent=1, default=str))
        P(write("count.json", cnt))
        return
    cnt = jload("count.json")
    if not cnt or cnt.get("module_sha256") != module_shas():
        raise SystemExit("count.json missing or written by different code; rerun the count step")
    if a.outcomes:
        cl, outs = data_client(), jload("outcomes.json", {})
        for d in sess:
            if d in outs:
                continue
            outs[d] = outcomes_day(cl, d, sorted({r["sym"] for r in rows if r["day"] == d}))
            write("outcomes.json", outs)
            P(f"  outcomes {d}: {len(outs[d])}")
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    outs = jload("outcomes.json")
    if not outs or set(outs) != set(sess):
        raise SystemExit("outcomes incomplete; run --outcomes")
    for r in rows:
        r["group"] = group(r["imb"], r["side"], cnt["buy_cut"], cnt["sell_cut"])
        o = outs[r["day"]].get(r["sym"], {})
        r.update(returns(o))
        r["close_code"] = o.get("close_code")
    res = {"halves": {}, "graded": {}, "info": {}}
    for h in ("A", "B"):
        rs = [r for r in rows if r["half"] == h]
        res["graded"][h] = grade(rs)
        hv = half_verdict(res["graded"][h])
        res["halves"][h] = "UNDERPOWERED" if hv != "FAIL" and not cnt[h]["floors_met"] else hv
        res["info"][h] = {"without_flagged_days": grade([r for r in rs if r["day"] not in FLAG_DAYS]),
                          "SELL_minus_CTRL": day_t(session_diffs(rs, grp="SELL"))[:3],
                          "no_close_print": sum(1 for r in rs if r.get("gross") is None),
                          "close_code": dict(Counter(r["close_code"] for r in rs))}
    res["verdict"] = final([res["halves"][h] for h in ("A", "B")], [res["graded"][h]["ub"] for h in ("A", "B")],
                           [res["graded"][h]["sessions"] for h in ("A", "B")])
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    P(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
