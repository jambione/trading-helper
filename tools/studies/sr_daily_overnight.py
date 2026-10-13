#!/usr/bin/env python3
"""Daily support/resistance location inside the overnight 12-1 book.

Pre-registered: docs/studies/sr_daily_overnight_prereg.json
Locked before any location-split outcome. Do not edit the window, the median
split, the 4 bp bar, or the halves after reading output.

Run on the mini (the panel is not on the laptop):
  cd ~/repo/trading-helper && .venv/bin/python tools/studies/sr_daily_overnight.py

Prints one table. Does not read bot_config, does not trade, does not fetch.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

PANEL = os.path.expanduser("~/lh_cache/panel.npz")
IS_END = np.datetime64("2021-12-31")
WINDOW = 20
WINDOW_INFO = 60
MIN_ELIGIBLE = 30
TOP_N = 20
MIN_LOCATED = 16
MIN_SIDE = 5
MIN_ABS_SIDE = 3
MIN_NIGHTS = 200
PASS_BP = 4.0
PASS_T = 2.0
COST_BP = 4.0  # auction round trip; cancels in the swap, still the pass hurdle


def prior_hl(high: np.ndarray, low: np.ndarray, window: int) -> tuple[np.ndarray, np.ndarray]:
    """Prior `window` sessions, excluding today. Row t is max/min of t-window..t-1."""
    h = pd.DataFrame(high).rolling(window, min_periods=window).max().shift(1).values
    l = pd.DataFrame(low).rolling(window, min_periods=window).min().shift(1).values
    return h, l


def location(close: np.ndarray, prior_high: np.ndarray, prior_low: np.ndarray) -> np.ndarray:
    span = prior_high - prior_low
    out = np.full(close.shape, np.nan, dtype=float)
    ok = np.isfinite(span) & (span > 0) & np.isfinite(close) & np.isfinite(prior_high) & np.isfinite(prior_low)
    out[ok] = (close[ok] - prior_low[ok]) / span[ok]
    return out


def mom_12_1(cf: np.ndarray) -> np.ndarray:
    """cf[t-21] / cf[t-252] - 1. Matches overnight_variants.mom(252, 21)."""
    t, _n = cf.shape
    m = np.full(cf.shape, np.nan, dtype=float)
    with np.errstate(all="ignore"):
        m[252:] = cf[231:t - 21] / cf[:t - 252] - 1
    return m


def tstat(x: np.ndarray) -> tuple[float, int]:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n < 3:
        return float("nan"), n
    sd = float(x.std(ddof=1))
    if not math.isfinite(sd) or sd == 0:
        return float("nan"), n
    return float(x.mean() / (sd / math.sqrt(n))), n


def ols_intercept(y: np.ndarray, x: np.ndarray) -> tuple[float, float, float]:
    """Return (intercept, t of intercept, slope). y and x already finite and aligned."""
    y = np.asarray(y, dtype=float)
    x = np.asarray(x, dtype=float)
    n = int(y.size)
    if n < 5:
        return float("nan"), float("nan"), float("nan")
    X = np.column_stack([np.ones(n), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = n - 2
    s2 = float(resid @ resid) / dof
    xtx_inv = np.linalg.inv(X.T @ X)
    se = math.sqrt(s2 * float(xtx_inv[0, 0]))
    t_a = float(beta[0] / se) if se > 0 else float("nan")
    return float(beta[0]), t_a, float(beta[1])


def _finite_pick(on: np.ndarray, extra: np.ndarray) -> np.ndarray:
    return np.isfinite(on) & np.isfinite(extra)


def median_split(loc: np.ndarray, on: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    """Low side (loc <= median) and high side (loc > median), finite ON only."""
    m = _finite_pick(on, loc)
    if int(m.sum()) < MIN_LOCATED:
        return None
    use_loc = loc[m]
    med = float(np.median(use_loc))
    low = m & (loc <= med)
    high = m & (loc > med)
    if int(low.sum()) < MIN_SIDE or int(high.sum()) < MIN_SIDE:
        return None
    return low, high


def side_mean(on: np.ndarray, mask: np.ndarray) -> float:
    return float(on[mask].mean())


def accumulate(rows: list[dict], half: str, key: str, value: float) -> None:
    if math.isfinite(value):
        rows.append({"half": half, "key": key, "v": value})


def summarize(vals: list[float]) -> tuple[float, float, int]:
    mu_t, n = tstat(np.array(vals, dtype=float))
    mu = float(np.nanmean(vals)) if vals else float("nan")
    return mu, mu_t, n


def verdict(mu_bp: float, t: float, n: int) -> str:
    if n < MIN_NIGHTS or not math.isfinite(mu_bp) or not math.isfinite(t):
        return "UNDERPOWERED"
    if mu_bp > PASS_BP and t >= PASS_T:
        return "PASS"
    return "FAIL"


def run(panel: dict) -> None:
    c = panel["c"]
    o = panel["o"]
    h = panel["h"]
    l = panel["l"]
    v = panel["v"]
    rc = panel["rc"]
    dates = pd.to_datetime(panel["dates"]).to_numpy()
    cf = pd.DataFrame(c).ffill(limit=None).values
    adv = pd.DataFrame(c * v).rolling(20, min_periods=15).mean().values
    mom = mom_12_1(cf)
    on = np.full(c.shape, np.nan, dtype=float)
    with np.errstate(all="ignore"):
        on[1:] = o[1:] / c[:-1] - 1
    spy_on = np.full(c.shape[0], np.nan, dtype=float)
    with np.errstate(all="ignore"):
        spy_on[1:] = panel["spy_o"][1:] / panel["spy_c"][:-1] - 1
    H20, L20 = prior_hl(h, l, WINDOW)
    H60, L60 = prior_hl(h, l, WINDOW_INFO)
    loc20 = location(c, H20, L20)
    loc60 = location(c, H60, L60)

    buckets: dict[tuple[str, str], list[float]] = {}
    diag: dict[str, list[float]] = {}
    # Primary nights keep the SPY overnight beside the gap so a missing SPY
    # drops that night from the regression only, not from the gap.
    primary_nights: dict[str, list[tuple[float, float]]] = {"IS": [], "OOS": []}

    def add(half: str, key: str, value: float) -> None:
        if math.isfinite(value):
            buckets.setdefault((half, key), []).append(value)

    def add_diag(key: str, value: float) -> None:
        if math.isfinite(value):
            diag.setdefault(key, []).append(value)

    T = c.shape[0]
    for t in range(252, T - 1):
        half = "IS" if dates[t] <= IS_END else "OOS"
        ok = (
            (rc[t] >= 5.0)
            & (adv[t] >= 50e6)
            & np.isfinite(mom[t])
            & np.isfinite(cf[t])
        )
        idx = np.flatnonzero(ok)
        if idx.size < MIN_ELIGIBLE:
            continue
        pick = idx[np.argsort(-mom[t, idx])][:TOP_N]
        ret = on[t + 1, pick]
        loc = loc20[t, pick]
        price = rc[t, pick]
        m12 = mom[t, pick]
        split = median_split(loc, ret)
        if split is not None:
            low, high = split
            diff = side_mean(ret, low) - side_mean(ret, high)
            primary_nights[half].append((diff, float(spy_on[t + 1])))
            add_diag(f"{half}:n_low", float(low.sum()))
            add_diag(f"{half}:n_high", float(high.sum()))
            add_diag(f"{half}:loc_low", float(loc[low].mean()))
            add_diag(f"{half}:loc_high", float(loc[high].mean()))
            add_diag(f"{half}:on_low", side_mean(ret, low))
            add_diag(f"{half}:on_high", side_mean(ret, high))
            add_diag(f"{half}:px_low", float(price[low].mean()))
            add_diag(f"{half}:px_high", float(price[high].mean()))
            add_diag(f"{half}:mom_low", float(m12[low].mean()))
            add_diag(f"{half}:mom_high", float(m12[high].mean()))
            # book: low half vs full top 20 (finite ON only)
            base_m = np.isfinite(ret)
            if int(base_m.sum()) >= 1 and int(low.sum()) >= 1:
                add(half, "book", side_mean(ret, low) - side_mean(ret, base_m))

        # absolute: close at or above the prior 20-day high vs the rest
        at_high = np.isfinite(ret) & np.isfinite(H20[t, pick]) & (c[t, pick] >= H20[t, pick])
        below = np.isfinite(ret) & np.isfinite(H20[t, pick]) & (c[t, pick] < H20[t, pick])
        if int(at_high.sum()) >= MIN_ABS_SIDE and int(below.sum()) >= MIN_ABS_SIDE:
            add(half, "absolute", side_mean(ret, below) - side_mean(ret, at_high))

        loc6 = loc60[t, pick]
        split6 = median_split(loc6, ret)
        if split6 is not None:
            low6, high6 = split6
            add(half, "w60", side_mean(ret, low6) - side_mean(ret, high6))

    print("daily SR inside the overnight 12-1 book")
    print(f"panel {PANEL}")
    print(f"primary: low-location half minus high-location half, prior {WINDOW} sessions, median split")
    print(f"pass: both halves mean > {PASS_BP:.0f} bp and t >= {PASS_T:.1f} and n >= {MIN_NIGHTS}")
    print(f"cost hurdle {COST_BP:.0f} bp (cancels in the swap; still required)")
    print()
    primary_ok = True
    gate_ok = True
    for half in ("IS", "OOS"):
        pairs = primary_nights[half]
        vals = [p[0] for p in pairs]
        mu, tv, n = summarize(vals)
        mu_bp = mu * 1e4
        vd = verdict(mu_bp, tv, n)
        if vd != "PASS":
            primary_ok = False
        if pairs:
            y = np.array([p[0] for p in pairs], dtype=float)
            spy = np.array([p[1] for p in pairs], dtype=float)
            m = np.isfinite(y) & np.isfinite(spy)
            a, t_a, b = ols_intercept(y[m], spy[m])
        else:
            a, t_a, b = float("nan"), float("nan"), float("nan")
        if not (math.isfinite(t_a) and t_a >= PASS_T):
            gate_ok = False
        def dmean(key: str) -> float:
            xs = diag.get(f"{half}:{key}", [])
            return float(np.mean(xs)) if xs else float("nan")
        print(f"{half}  n {n:5d}  diff {mu_bp:+7.2f} bp  t {tv:+5.2f}  {vd}")
        print(f"    names low/high {dmean('n_low'):.1f}/{dmean('n_high'):.1f}"
              f"  loc {dmean('loc_low'):+.2f}/{dmean('loc_high'):+.2f}"
              f"  ON {dmean('on_low') * 1e4:+.1f}/{dmean('on_high') * 1e4:+.1f} bp")
        print(f"    price ${dmean('px_low'):.1f}/${dmean('px_high'):.1f}"
              f"  12-1 {dmean('mom_low') * 100:+.1f}%/{dmean('mom_high') * 100:+.1f}%")
        print(f"    SPY regression intercept {a * 1e4:+.2f} bp  t {t_a:+.2f}  slope {b:+.2f}"
              f"  ship_gate {'HOLD' if (math.isfinite(t_a) and t_a >= PASS_T) else 'BETA'}")
        for key, label in (("book", "INFO book low-half minus full top20"),
                           ("absolute", "INFO below-prior-high minus at-prior-high"),
                           ("w60", "INFO 60-day window median split")):
            mu_i, t_i, n_i = summarize(buckets.get((half, key), []))
            print(f"    {label}: n {n_i}  {mu_i * 1e4:+.2f} bp  t {t_i:+.2f}  (not a verdict)")
        print()
    if primary_ok and gate_ok:
        overall = "PASS pending skeptic review — do not ship"
    elif primary_ok and not gate_ok:
        overall = "PRIMARY MET, SHIP GATE FAILED (beta) — do not ship"
    else:
        overall = "NO PASS"
    print(overall)


def self_test() -> None:
    """Synthetic look-ahead check. Does not read the panel."""
    n = 30
    high = np.arange(1, n + 1, dtype=float).reshape(-1, 1)
    low = high - 0.5
    close = high - 0.25
    high[-1, 0] = 999.0  # today's high must not enter yesterday's prior window
    H, L = prior_hl(high, low, 20)
    t = n - 1
    # row t's prior window is t-20..t-1, i.e. values  (t-20+1) .. t   because high[i] = i+1
    expect_high = float(np.max(high[t - 20:t, 0]))
    assert math.isclose(float(H[t, 0]), expect_high), (float(H[t, 0]), expect_high)
    assert float(H[t, 0]) != 999.0
    assert math.isclose(float(L[t, 0]), float(np.min(low[t - 20:t, 0])))
    loc = location(close, H, L)
    assert math.isfinite(loc[t, 0])
    assert H[19, 0] != H[19, 0]  # NaN: shift leaves row 19 without 20 prior bars
    # median split: 16 names, ties at the median stay on the low side
    loc_v = np.linspace(0, 1, 16)
    on_v = np.linspace(0.01, 0.02, 16)
    split = median_split(loc_v, on_v)
    assert split is not None
    low_m, high_m = split
    assert int(low_m.sum()) >= MIN_SIDE and int(high_m.sum()) >= MIN_SIDE
    assert int((low_m & high_m).sum()) == 0
    # a name without a next open is not a side member; 17 names so 16 remain
    loc_v2 = np.linspace(0, 1, 17)
    on_v2 = np.linspace(0.01, 0.02, 17)
    on_v2[0] = np.nan
    split2 = median_split(loc_v2, on_v2)
    assert split2 is not None and not bool(split2[0][0]) and not bool(split2[1][0])
    # 12-1: m[252] = cf[231] / cf[0] - 1
    cf = np.arange(1, 401, dtype=float).reshape(-1, 1)
    m = mom_12_1(cf)
    assert math.isclose(float(m[252, 0]), cf[231, 0] / cf[0, 0] - 1)
    assert not math.isfinite(m[251, 0])
    print("self-test ok")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        self_test()
        return
    if not os.path.exists(PANEL):
        sys.exit(f"panel not found at {PANEL}; run this on the mini")
    z = np.load(PANEL, allow_pickle=False)
    run({k: z[k] for k in z.files})


if __name__ == "__main__":
    main()
