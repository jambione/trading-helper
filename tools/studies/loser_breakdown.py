#!/usr/bin/env python3
"""loser_breakdown - what do the desk's losing trades look like, and could anything at entry have told them apart?
(operator 11/18: "do the loser breakdown now"). Read-only.

Trades: broker fills (ai_reports/broker_truth/fills.jsonl; today's from the Alpaca FILL activities), paired FIFO per symbol per
day. Path: SIP minute bars for the traded names from the data-only account from the entry minute to the exit minute -> MFE / MAE
in bp of the fill. Entry features: the last arm_ok decision-ledger row for the symbol in the 15 min before the buy, plus bar
context (30-min range position, 5-min and 15-min run-up, day change, distance below the day high so far).

    .venv/bin/python tools/studies/loser_breakdown.py 2026-11-02 2026-11-18   # -> ai_reports/loser_breakdown/result.json
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
from relative_strength_scan import load_day  # noqa: E402
from evening_family import WH_SIP  # noqa: E402

ET = ZoneInfo("America/New_York")
FILLS = ROOT / "ai_reports" / "broker_truth" / "fills.jsonl"
DEC = ROOT / "ai_reports" / "decision_ledger"
OUT = ROOT / "ai_reports" / "loser_breakdown"
FEATS = ("pctr", "pctr_fast_pitch", "pctr_slow_pitch", "cm_rsi", "tape_age_sec", "pos30", "run5_bp", "run15_bp",
         "day_pct", "below_hod_bp", "minute")


def pair_fifo(fills):
    """[{symbol, side, qty, price, t}] (one day) -> [{symbol, t_in, px_in, t_out, px_out, qty}] FIFO per symbol."""
    out, book = [], defaultdict(list)
    for f in sorted(fills, key=lambda x: x["t"]):
        s, q, px = f["symbol"], float(f["qty"]), float(f["price"])
        if f["side"] == "buy":
            book[s].append([f["t"], px, q])
            continue
        while q > 1e-9 and book[s]:
            lot = book[s][0]
            take = min(q, lot[2])
            out.append({"symbol": s, "t_in": lot[0], "px_in": lot[1], "t_out": f["t"], "px_out": px, "qty": take})
            lot[2] -= take
            q -= take
            if lot[2] <= 1e-9:
                book[s].pop(0)
    # merge pieces of one buy into one trade (same symbol and entry time)
    merged = {}
    for r in out:
        k = (r["symbol"], round(r["t_in"]))
        m = merged.setdefault(k, {**r, "qty": 0.0, "_val": 0.0, "t_out": r["t_out"]})
        m["qty"] += r["qty"]
        m["_val"] += r["qty"] * r["px_out"]
        m["t_out"] = max(m["t_out"], r["t_out"])
    res = []
    for m in merged.values():
        m["px_out"] = m.pop("_val") / m["qty"]
        m["ret_bp"] = (m["px_out"] / m["px_in"] - 1) * 1e4
        res.append(m)
    return sorted(res, key=lambda r: r["t_in"])


def auc(pos, neg):
    """P(a random pos value > a random neg value); ties count half."""
    if not pos or not neg:
        return None
    a = np.array(pos)[:, None]
    b = np.array(neg)[None, :]
    return float(((a > b).sum() + 0.5 * (a == b).sum()) / (len(pos) * len(neg)))


def minute_of(t, day):
    t0 = datetime.fromisoformat(day + "T09:30").replace(tzinfo=ET).timestamp()
    return int((t - t0) // 60)


def today_fills(day):
    import requests
    k = json.load(open(ROOT / "config" / "secrets.json"))
    base = "https://paper-api.alpaca.markets"
    h = {"APCA-API-KEY-ID": k["api_key"], "APCA-API-SECRET-KEY": k["secret_key"]}
    r = requests.get(f"{base}/v2/account/activities/FILL", headers=h, timeout=30,
                     params={"date": day, "direction": "asc", "page_size": 100})
    r.raise_for_status()
    out = []
    for a in r.json():
        t = datetime.fromisoformat(a["transaction_time"].replace("Z", "+00:00")).timestamp()
        out.append({"symbol": a["symbol"], "side": a["side"], "qty": float(a["qty"]), "price": float(a["price"]), "t": t})
    return out


def api_bars(day, syms):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import DataFeed
    k = json.load(open(ROOT / "secrets_data.json"))
    cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
    a = datetime.fromisoformat(day + "T09:30").replace(tzinfo=ET)
    b = min(datetime.now(ET) - timedelta(minutes=16), datetime.fromisoformat(day + "T16:00").replace(tzinfo=ET))
    res = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sorted(syms), timeframe=TimeFrame.Minute, start=a, end=b,
                                             feed=DataFeed.SIP)).data
    n = len(syms)
    idx = {s: i for i, s in enumerate(sorted(syms))}
    mats = {f: np.full((n, 390), np.nan) for f in "ohlcv"}
    for s, bars in res.items():
        for bar in bars:
            m = minute_of(bar.timestamp.timestamp(), day)
            if 0 <= m < 390:
                for f, v in zip("ohlcv", (bar.open, bar.high, bar.low, bar.close, bar.volume)):
                    mats[f][idx[s], m] = v
    return {"idx": idx, **mats}


def prev_close(day):
    """{sym: prior session close} from the warehouse day before `day`."""
    files = sorted(f for f in WH_SIP.glob("20??-??-??.npz") if f.stem < day)
    if not files:
        return {}
    p = load_day(files[-1])
    out = {}
    for s, i in p["idx"].items():
        v = p["c"][i][~np.isnan(p["c"][i])]
        if len(v):
            out[s] = float(v[-1])
    return out


def context(bars, sym, m, pc):
    i = bars["idx"].get(sym)
    if i is None or m < 1:
        return {}
    c, h, l = bars["c"][i], bars["h"][i], bars["l"][i]
    last = c[:m][~np.isnan(c[:m])]
    if not len(last):
        return {}
    px = float(last[-1])
    out = {}
    lo30, hi30 = np.nanmin(l[max(0, m - 30):m]), np.nanmax(h[max(0, m - 30):m])
    if hi30 > lo30:
        out["pos30"] = (px - lo30) / (hi30 - lo30)
    for n, key in ((5, "run5_bp"), (15, "run15_bp")):
        ref = c[max(0, m - n):m - n + 1] if m - n >= 0 else c[:1]
        ref = ref[~np.isnan(ref)]
        if len(ref):
            out[key] = (px / float(ref[0]) - 1) * 1e4
    hod = np.nanmax(h[:m])
    out["below_hod_bp"] = (1 - px / hod) * 1e4 if hod > 0 else None
    if pc:
        out["day_pct"] = (px / pc - 1) * 100
    return out


def path(bars, sym, m_in, m_out, px_in):
    i = bars["idx"].get(sym)
    if i is None:
        return {}
    h, l = bars["h"][i][m_in:m_out + 1], bars["l"][i][m_in:m_out + 1]
    if not np.isfinite(h).any():
        return {}
    return {"mfe_bp": (np.nanmax(h) / px_in - 1) * 1e4, "mae_bp": (np.nanmin(l) / px_in - 1) * 1e4}


def arm_row(day, sym, t):
    p = DEC / f"{day}.jsonl"
    best = None
    if not p.exists():
        return {}
    for line in open(p):
        if f'"{sym}"' not in line or '"arm_ok": true' not in line:
            continue
        r = json.loads(line)
        if r.get("symbol") == sym and t - 900 <= r["ts"] <= t + 5:
            best = r
    return best or {}


def main(argv):
    lo, hi = argv[0], argv[1]
    skip = set(argv[2].split(",")) if len(argv) > 2 else set()   # days the desk was off (picker fills only)
    fills = defaultdict(list)
    for line in open(FILLS):
        f = json.loads(line)
        d = datetime.fromtimestamp(f["t"], ET).date().isoformat()
        if lo <= d <= hi and d not in skip:
            fills[d].append(f)
    today = datetime.now(ET).date().isoformat()
    if lo <= today <= hi and today not in fills:
        try:
            fills[today] = today_fills(today)
        except Exception as e:  # noqa: BLE001
            print("today's fills unavailable:", str(e)[:120])
    trades = []
    for d in sorted(fills):
        tr = pair_fifo(fills[d])
        if not tr:
            continue
        syms = {t["symbol"] for t in tr}
        wh = WH_SIP / f"{d}.npz"
        bars = api_bars(d, syms)          # traded names are mostly not in the warehouse
        pc = prev_close(d)
        for t in tr:
            m_in, m_out = minute_of(t["t_in"], d), minute_of(t["t_out"], d)
            if not (0 <= m_in < 390):
                continue
            arm = arm_row(d, t["symbol"], t["t_in"])
            row = {"day": d, **{k: t[k] for k in ("symbol", "px_in", "px_out", "qty")}, "ret_bp": round(t["ret_bp"], 1),
                   "hold_min": round((t["t_out"] - t["t_in"]) / 60, 1), "minute": m_in,
                   **path(bars, t["symbol"], m_in, min(389, m_out), t["px_in"]),
                   **context(bars, t["symbol"], m_in, pc.get(t["symbol"]))}
            for k in ("pctr", "pctr_fast_pitch", "pctr_slow_pitch", "cm_rsi", "tape_age_sec"):
                if isinstance(arm.get(k), (int, float)):
                    row[k] = arm[k]
            trades.append(row)
    W = [t for t in trades if t["ret_bp"] > 0]
    L = [t for t in trades if t["ret_bp"] <= 0]

    def dist(xs, k):
        v = [x[k] for x in xs if isinstance(x.get(k), (int, float)) and math.isfinite(x[k])]
        return {"n": len(v), "median": round(float(np.median(v)), 1) if v else None,
                "p25": round(float(np.percentile(v, 25)), 1) if v else None,
                "p75": round(float(np.percentile(v, 75)), 1) if v else None}
    never_green = [t for t in L if isinstance(t.get("mfe_bp"), float) and t["mfe_bp"] < 5]
    res = {
        "days": sorted(fills), "trades": len(trades), "wins": len(W), "losses": len(L),
        "win_pct": round(100 * len(W) / max(1, len(trades)), 1),
        "avg_win_bp": round(float(np.mean([t["ret_bp"] for t in W])), 1) if W else None,
        "avg_loss_bp": round(float(np.mean([t["ret_bp"] for t in L])), 1) if L else None,
        "net_bp_per_trade": round(float(np.mean([t["ret_bp"] for t in trades])), 2) if trades else None,
        "loss_share_of_dollars": None,
        "losers": {"mfe_bp": dist(L, "mfe_bp"), "mae_bp": dist(L, "mae_bp"), "hold_min": dist(L, "hold_min"),
                   "never_green_share": round(len(never_green) / max(1, len(L)), 3),
                   "went_up_15bp_then_lost": sum(1 for t in L if (t.get("mfe_bp") or 0) >= 15)},
        "winners": {"mfe_bp": dist(W, "mfe_bp"), "ret_bp": dist(W, "ret_bp"), "hold_min": dist(W, "hold_min")},
        "entry_auc_win_vs_loss": {k: (round(a, 3) if (a := auc([t[k] for t in W if isinstance(t.get(k), (int, float))],
                                                                [t[k] for t in L if isinstance(t.get(k), (int, float))])) is not None else None)
                                  for k in FEATS},
        "entry_medians": {k: {"win": dist(W, k)["median"], "loss": dist(L, k)["median"]} for k in FEATS},
        "today": [t for t in trades if t["day"] == today],
        "generated": datetime.now(ET).isoformat(timespec="minutes"),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "result.json").write_text(json.dumps({**res, "all": trades}, indent=1, default=float))
    print(json.dumps(res, indent=1, default=float))


if __name__ == "__main__":
    main(sys.argv[1:])
