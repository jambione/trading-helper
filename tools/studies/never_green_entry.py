#!/usr/bin/env python3
"""never_green_entry.py: docs/studies/never_green_entry_prereg.json (READY 2026-11-03).

Can a fixed model, using only entry-time facts, flag the entries that never go green,
well enough that refusing its riskiest 20% raises the mean net of the trades kept?

READ ONLY: no orders. Run on the mini AFTER the close (SIP 1-minute bars, data key):
    .venv/bin/python tools/studies/never_green_entry.py population   # counts only, no API
    .venv/bin/python tools/studies/never_green_entry.py run --sleep 0.5
Writes ai_reports/never_green_entry/result.json.

Model: scikit-learn LogisticRegression(C=1.0, max_iter=2000), as registered, on features
standardised with TRAIN statistics. fit_logistic (exact Newton on the same objective,
0.5*|w|^2 + C*sum(logloss), intercept unpenalised) is kept as a cross-check: the tests
require both to agree.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT), str(ROOT / "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

ET = ZoneInfo("America/New_York")
REPORTS = ROOT / "ai_reports"
TRAIN = ("2026-09-11", "2026-10-09")
TEST = ("2026-10-12", "2026-10-30")
H1 = ("2026-10-12", "2026-10-21")
H2 = ("2026-10-22", "2026-10-30")
EXCLUDE_DAYS = {"2026-11-02", "2026-11-03"}
RT_COST_BP = 3.0
SEED = 20261103
N_RANDOM = 2000
N_BOOT = 2000
DAY_RE = re.compile(r"^\d{4}-\d\d-\d\d$")
TICKER_RE = re.compile(r"^[A-Z][A-Z.]{0,5}$")
JUNK = {"TEST", "FAKE", "MOCK", "XXX"}
NUMERIC = ["pctr", "pctr_slow", "entry_exhaustion", "cm_rsi", "pct_change", "proximity_pct",
           "dwell_sec", "entry_hour_et", "log_price", "spread_bp"]
CATS = ["source", "arm_why"]
BINARY = ["late_into_square", "reentry_same_day", "prior_exit_won"]
SENSITIVITY_ARMS = {"last_presquare", "last_mid_rise"}


# ------------------------------------------------------------------ population
def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def parse_row(o: dict, day: str) -> dict | None:
    """One outcome row -> a study row, or None when it is outside the population."""
    sym = str(o.get("symbol") or "").upper()
    if not TICKER_RE.match(sym) or sym in JUNK:
        return None
    if str(o.get("entry_path") or "") != "watch":
        return None
    e, x, t = _num(o.get("entry_price")), _num(o.get("exit_price")), _num(o.get("entry_time"))
    if not (e and x and t) or e < 20.0:
        return None
    hm = datetime.fromtimestamp(t, ET).strftime("%H:%M")
    if not ("09:30" <= hm < "15:50"):
        return None
    f = o.get("features") if isinstance(o.get("features"), dict) else {}

    def g(k):
        v = o.get(k)
        return _num(v if v is not None else f.get(k))
    ask, bid = _num(f.get("ask")), _num(f.get("bid"))
    spread = None
    if ask and bid and ask > bid:
        spread = (ask - bid) / ((ask + bid) / 2.0) * 1e4
    sq = _num(f.get("square_since"))
    return {
        "day": day, "symbol": sym, "entry_time": t, "exit_time": _num(o.get("exit_time")),
        "entry": e, "exit": x, "peak": _num(o.get("peak_price")),
        "net_bp": (x - e) / e * 1e4 - RT_COST_BP,
        "num": {"pctr": g("pctr"), "pctr_slow": g("pctr_slow"), "entry_exhaustion": _num(o.get("entry_exhaustion")),
                "cm_rsi": _num(f.get("cm_rsi")), "pct_change": _num(f.get("pct_change")),
                "proximity_pct": _num(f.get("proximity_pct")), "dwell_sec": _num(f.get("dwell_sec")),
                "entry_hour_et": _num(f.get("entry_hour_et")), "log_price": math.log(e), "spread_bp": spread},
        "cat": {"source": str(f.get("source") or o.get("source") or ""), "arm_why": str(o.get("arm_why") or "")},
        "late_into_square": 1.0 if (sq is not None and t - sq >= 60.0) else 0.0,
    }


def add_reentry_flags(rows: list[dict]) -> None:
    """reentry_same_day / prior_exit_won from earlier trades on the same symbol and day
    whose exit came before this entry."""
    by = defaultdict(list)
    for r in rows:
        by[(r["day"], r["symbol"])].append(r)
    for v in by.values():
        v.sort(key=lambda r: r["entry_time"])
        for i, r in enumerate(v):
            prior = [p for p in v[:i] if p["exit_time"] is not None and p["exit_time"] < r["entry_time"]]
            r["reentry_same_day"] = 1.0 if prior else 0.0
            r["prior_exit_won"] = 1.0 if (prior and prior[-1]["exit"] > prior[-1]["entry"]) else 0.0


def load_population(reports: Path = REPORTS) -> list[dict]:
    rows, seen = [], set()
    tapes = reports / "tapes"
    for d in sorted(os.listdir(tapes)):
        if not DAY_RE.match(d) or d in EXCLUDE_DAYS or not (TRAIN[0] <= d <= TEST[1]):
            continue
        p = tapes / d / "outcomes.jsonl"
        if not p.exists():
            continue
        for line in open(p, encoding="utf-8"):
            try:
                o = json.loads(line)
            except ValueError:
                continue
            r = parse_row(o, d)
            if r is None:
                continue
            k = (r["symbol"], round(r["entry_time"]))
            if k in seen:
                continue
            seen.add(k)
            rows.append(r)
    add_reentry_flags(rows)
    return rows


# ------------------------------------------------------------------ label
def label_from_bars(bars: list, entry_time: float, entry_price: float) -> bool | None:
    """never green: every 1-minute bar STARTING in (entry_time, entry_time+300] has high
    < entry x 1.0005. None when no bar starts in the window."""
    w = [b for b in bars if entry_time < b[0] <= entry_time + 300.0]
    if not w:
        return None
    return max(float(b[2]) for b in w) < entry_price * 1.0005


def fetch_bars(cl, sym: str, day: str) -> list:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute,
                                           start=d.replace(hour=9, minute=30), end=d.replace(hour=16, minute=5),
                                           feed=DataFeed.SIP))
    out = []
    for b in (getattr(r, "data", {}) or {}).get(sym, []) or []:
        ts = b.timestamp.timestamp() if hasattr(b.timestamp, "timestamp") else float(b.timestamp)
        out.append((ts, float(b.open), float(b.high), float(b.low), float(b.close)))
    return sorted(out)


def attach_labels(rows: list[dict], cache_path: Path, sleep: float) -> None:
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    cl = None
    for key in sorted({f"{r['symbol']}|{r['day']}" for r in rows}):
        if key in cache:
            continue
        if cl is None:
            import fill_truth_report as ft
            ft._load_env()
            from alpaca.data.historical import StockHistoricalDataClient
            cl = StockHistoricalDataClient(os.getenv("ALPACA_DATA_API_KEY") or os.getenv("ALPACA_API_KEY"),
                                           os.getenv("ALPACA_DATA_SECRET_KEY") or os.getenv("ALPACA_SECRET_KEY"))
        sym, day = key.split("|")
        try:
            cache[key] = fetch_bars(cl, sym, day)
        except Exception as e:  # noqa: BLE001
            cache[key] = {"error": str(e)[:120]}
        time.sleep(sleep)
        cache_path.write_text(json.dumps(cache))
    for r in rows:
        b = cache.get(f"{r['symbol']}|{r['day']}")
        r["never_green"] = label_from_bars(b, r["entry_time"], r["entry"]) if isinstance(b, list) else None
        r["peak_never_green"] = (r["peak"] < r["entry"] * 1.0005) if r["peak"] else None


# ------------------------------------------------------------------ features + model
class Prep:
    """Fitted on TRAIN only: medians, missing flags, spread winsor, standardisation, categories."""

    def fit(self, rows: list[dict]) -> "Prep":
        self.med, self.lo, self.hi = {}, {}, {}
        for k in NUMERIC:
            v = sorted(r["num"][k] for r in rows if r["num"][k] is not None)
            self.med[k] = float(np.median(v)) if v else 0.0
            if k == "spread_bp" and v:
                self.lo[k], self.hi[k] = float(np.percentile(v, 1)), float(np.percentile(v, 99))
        self.cats = {c: sorted({r["cat"][c] for r in rows}) for c in CATS}
        raw = np.array([self._raw(r) for r in rows], dtype=float)
        self.mu = raw.mean(axis=0)
        self.sd = raw.std(axis=0)
        self.sd[self.sd == 0] = 1.0
        return self

    def _raw(self, r: dict) -> list[float]:
        out = []
        for k in NUMERIC:
            v = r["num"][k]
            miss = v is None
            if miss:
                v = self.med[k]
            if k in self.lo:
                v = min(max(v, self.lo[k]), self.hi[k])
            out += [float(v), 1.0 if miss else 0.0]
        for c in CATS:
            out += [1.0 if r["cat"][c] == lvl else 0.0 for lvl in self.cats[c]]  # unseen -> all zero
        out += [float(r.get(b) or 0.0) for b in BINARY]
        return out

    def transform(self, rows: list[dict]) -> np.ndarray:
        return (np.array([self._raw(r) for r in rows], dtype=float) - self.mu) / self.sd


def fit_logistic(X: np.ndarray, y: np.ndarray, C: float = 1.0, iters: int = 100) -> tuple[np.ndarray, float]:
    """Minimise 0.5*|w|^2 + C*sum(log(1+exp(z)) - y*z), z = Xw + b, b unpenalised (Newton)."""
    n, k = X.shape
    th = np.zeros(k + 1)
    Xa = np.hstack([X, np.ones((n, 1))])
    reg = np.eye(k + 1)
    reg[k, k] = 0.0

    def obj(t):
        z = Xa @ t
        return 0.5 * t[:k] @ t[:k] + C * np.sum(np.logaddexp(0.0, z) - y * z)
    for _ in range(iters):
        z = Xa @ th
        p = 1.0 / (1.0 + np.exp(-z))
        g = reg @ th + C * Xa.T @ (p - y)
        if np.linalg.norm(g) < 1e-9:
            break
        H = reg + C * (Xa.T * (p * (1 - p))) @ Xa + 1e-12 * np.eye(k + 1)
        step = np.linalg.solve(H, g)
        a, f0 = 1.0, obj(th)
        while obj(th - a * step) > f0 and a > 1e-6:
            a *= 0.5
        th = th - a * step
    return th[:k], float(th[k])


def fit_sklearn(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float]:
    """The registered model: LogisticRegression(C=1.0, max_iter=2000)."""
    from sklearn.linear_model import LogisticRegression
    m = LogisticRegression(C=1.0, max_iter=2000).fit(X, y)
    return m.coef_[0].astype(float), float(m.intercept_[0])


def predict(X: np.ndarray, w: np.ndarray, b: float) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-(X @ w + b)))


def auc(scores, labels) -> float | None:
    s = np.asarray(scores, float)
    y = np.asarray(labels, bool)
    pos, neg = s[y], s[~y]
    if not len(pos) or not len(neg):
        return None
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    allv = np.concatenate([pos, neg])[order]
    ranks = np.empty(len(allv))
    i = 0
    while i < len(allv):  # average ranks over ties
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        ranks[i:j + 1] = (i + j) / 2.0 + 1.0
        i = j + 1
    r = np.empty(len(allv))
    r[order] = ranks
    return float((r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg)))


# ------------------------------------------------------------------ scoring
def m2(rows: list[dict], blocked: set) -> float | None:
    if not rows:
        return None
    kept = [r["net_bp"] for i, r in enumerate(rows) if i not in blocked]
    if not kept:
        return None
    return float(np.mean(kept) - np.mean([r["net_bp"] for r in rows]))


def _t(v):
    v = [x for x in v if x is not None]
    if len(v) < 3:
        return None
    sd = float(np.std(v, ddof=1))
    return float(np.mean(v) / (sd / math.sqrt(len(v)))) if sd > 0 else None


def score_test(test: list[dict], p: np.ndarray, cut: float, seed: int = SEED) -> dict:
    blocked = {i for i, x in enumerate(p) if x >= cut}
    out = {"n": len(test), "n_blocked": len(blocked), "block_fraction": len(blocked) / len(test) if test else None,
           "auc": auc(p, [r["never_green"] for r in test])}
    out["M2"] = m2(test, blocked)
    days = sorted({r["day"] for r in test})
    per = []
    for d in days:
        idx = [i for i, r in enumerate(test) if r["day"] == d]
        sub = [test[i] for i in idx]
        bl = {k for k, i in enumerate(idx) if i in blocked}
        per.append(m2(sub, bl) if len(bl) < len(sub) else None)
    out["t_day"] = _t(per)
    for name, (lo, hi) in (("half_1", H1), ("half_2", H2)):
        idx = [i for i, r in enumerate(test) if lo <= r["day"] <= hi]
        sub = [test[i] for i in idx]
        bl = {k for k, i in enumerate(idx) if i in blocked}
        out[name] = {"n": len(sub), "M2": m2(sub, bl), "n_blocked": len(bl)}
    rng = random.Random(seed)
    by_day = defaultdict(list)
    for i, r in enumerate(test):
        by_day[r["day"]].append(i)
    nb = {d: sum(1 for i in v if i in blocked) for d, v in by_day.items()}
    rand = []
    for _ in range(N_RANDOM):
        rb = set()
        for d, v in by_day.items():
            rb.update(rng.sample(v, nb[d]))
        rand.append(m2(test, rb))
    rand = [x for x in rand if x is not None]
    out["random_share_beaten"] = (sum(1 for x in rand if out["M2"] is not None and out["M2"] > x) / len(rand)) if rand else None
    rng2 = np.random.default_rng(seed)
    boots = []
    for _ in range(N_BOOT):
        pick = rng2.choice(days, size=len(days), replace=True)
        idx = [i for d in pick for i in by_day[d]]
        a = auc([p[i] for i in idx], [test[i]["never_green"] for i in idx])
        if a is not None:
            boots.append(a)
    out["auc_ci95"] = [float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))] if boots else None
    return out


def verdict(n_train: int, n_test: int, unlabeled_share: float, s: dict) -> str:
    lo = s["auc_ci95"][0] if s.get("auc_ci95") else None
    if (n_train < 400 or n_test < 250 or s["half_1"]["n"] < 120 or s["half_2"]["n"] < 120
            or unlabeled_share > 0.10 or s["block_fraction"] is None or not (0.10 <= s["block_fraction"] <= 0.35)):
        return "VOID"
    if lo is None or lo <= 0.50 or s["M2"] is None or s["M2"] <= 0:
        return "FAIL"
    h_ok = all(s[h]["M2"] is not None and s[h]["M2"] > 0 for h in ("half_1", "half_2"))
    if (s["auc"] >= 0.60 and lo > 0.55 and s["M2"] >= 2.0 and s["t_day"] is not None and s["t_day"] >= 2.0
            and h_ok and s["random_share_beaten"] >= 0.95):
        return "PASS"
    if h_ok and s["auc"] > 0.55 and s["random_share_beaten"] >= 0.80:
        return "PROMISING"
    return "INCONCLUSIVE"


def run(rows: list[dict]) -> dict:
    lab = [r for r in rows if r.get("never_green") is not None]
    unl = 1.0 - len(lab) / len(rows) if rows else 1.0
    train = [r for r in lab if TRAIN[0] <= r["day"] <= TRAIN[1]]
    test = [r for r in lab if TEST[0] <= r["day"] <= TEST[1]]

    def fit_and_score(tr):
        prep = Prep().fit(tr)
        w, b = fit_sklearn(prep.transform(tr), np.array([1.0 if r["never_green"] else 0.0 for r in tr]))
        cut = float(np.percentile(predict(prep.transform(tr), w, b), 80))
        return score_test(test, predict(prep.transform(test), w, b), cut)
    s = fit_and_score(train)
    out = {"n_rows": len(rows), "unlabeled_share": unl, "n_train": len(train), "n_test": len(test),
           "train_never_green_rate": float(np.mean([r["never_green"] for r in train])) if train else None,
           "test_never_green_rate": float(np.mean([r["never_green"] for r in test])) if test else None,
           "primary": s}
    out["verdict"] = verdict(len(train), len(test), unl, s)
    both = [r for r in lab if r.get("peak_never_green") is not None]
    out["report_only"] = {
        "peak_label_agreement": float(np.mean([r["peak_never_green"] == r["never_green"] for r in both])) if both else None,
    }
    tr_s = [r for r in train if r["cat"]["arm_why"] in SENSITIVITY_ARMS]
    if len(tr_s) >= 100:
        ss = fit_and_score(tr_s)
        out["report_only"]["sensitivity_presquare_mid_rise_train"] = {
            "n_train": len(tr_s), "auc": ss["auc"], "M2": ss["M2"], "block_fraction": ss["block_fraction"]}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["population", "run"])
    ap.add_argument("--sleep", type=float, default=0.5)
    a = ap.parse_args(argv)
    rows = load_population()
    tr = sum(1 for r in rows if TRAIN[0] <= r["day"] <= TRAIN[1])
    te = sum(1 for r in rows if TEST[0] <= r["day"] <= TEST[1])
    print(f"population {len(rows)}  train {tr}  test {te}  symbol-days {len({(r['symbol'], r['day']) for r in rows})}")
    if a.cmd == "population":
        return 0
    out_dir = REPORTS / "never_green_entry"
    out_dir.mkdir(parents=True, exist_ok=True)
    attach_labels(rows, out_dir / "bars_cache.json", a.sleep)
    res = run(rows)
    (out_dir / "result.json").write_text(json.dumps(res, indent=2, default=float))
    print(json.dumps(res, indent=2, default=lambda x: round(float(x), 4)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
