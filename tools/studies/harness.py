#!/usr/bin/env python3
"""harness.py - one fast, CPU-only test for any entry signal (docs/TEST_STANDARD.md verdicts, no AI in the loop).

You write a signal file; this scores it on the SIP warehouse (ai_reports/warehouse/sip/DAY.npz) in minutes:

    # tools/studies/signals/my_idea.py
    NAME = "my_idea"
    HOLD = 3                                    # minutes held (optional, default 3)
    def signal(day, prev):                      # -> bool array (n_names, 390); True at minute m = "decided at the CLOSE of bar m"
        ...                                     # day/prev: dict(syms, o, h, l, c, v), matrices (n_names, 390), NaN where no bar,
                                                # column 0 = 09:30. prev = the prior session, same row order (NaN rows if absent).

    .venv/bin/python tools/studies/harness.py tools/studies/signals/my_idea.py            # development read (hold-out hidden)
    .venv/bin/python tools/studies/harness.py tools/studies/signals/my_idea.py --final    # the ONE read on the hold-out
    .venv/bin/python tools/studies/harness.py --selftest                                  # canaries on the real warehouse

Model (fixed, the same for every idea so results compare):
- Universe each day: top N names by the PRIOR session's dollar volume, first price in [lo, hi].
- Entry = open of bar m+1, exit = open of bar m+1+HOLD. One position per name at a time. No fills on missing bars.
- PRIMARY control = the same minute on every other universe name (exact mean, not a draw).
- SECONDARY control = the same name later only (m+HOLD+2 .. m+HOLD+62).
- Both control means are printed beside the unconditional random-minute mean.
- Stats are by DAY (day-clustered t), split in two halves by date; top 1% of trades winsorized as the concentration check.
- EDGE: primary lift > 0 in both halves, pooled day-t >= the critical value (--family k tightens it), secondary lift > 0,
  winsorized lift > 0. PROFITABLE: EDGE and net > 0 after cost in both halves and at the stress cost, both as the mean of
  day means AND per trade (pooled). The per-trade line is what an account earns; day means flatter rules that fire in bursts.
- Look-ahead guard: the signal is re-run with every bar after a random minute blanked; any change before that minute aborts.
- Hold-out: the latest --holdout sessions are never scored unless --final. Every read is appended to ai_reports/harness/reads.jsonl
  with the signal file's hash, so a second "final" read of the same NAME is visible (and refused without --force).
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import sys
import time
import warnings
from pathlib import Path
from statistics import NormalDist

import numpy as np

ROOT = Path(os.environ.get("REPO") or Path(__file__).resolve().parents[2])
WH = ROOT / "ai_reports" / "warehouse" / "sip"
OUT = ROOT / "ai_reports" / "harness"
M0, NBAR = 570, 390
KEYS = ("o", "h", "l", "c", "v")
DEFAULTS = dict(top_n=400, price_lo=10.0, price_hi=500.0, cost_bp=6.0, stress_bp=10.0, holdout=20, family=1,
                min_trades=200, min_days=20, movers_pct=0.0, cent_spread=0.0)


# ---------- data ----------
def load_day(path) -> dict:
    z = np.load(path, allow_pickle=True)
    syms = [str(s) for s in z["syms"]]
    mats = {k: np.full((len(syms), NBAR), np.nan, dtype=np.float64) for k in KEYS}
    si = z["sym_idx"].astype(np.int64)
    mi = z["minute"].astype(np.int64) - M0
    ok = (mi >= 0) & (mi < NBAR)
    for k in KEYS:
        mats[k][si[ok], mi[ok]] = z[k][ok]
    out = {"syms": syms, **mats}
    if "uni" in z.files:                                    # a warehouse that marks which names qualify for THIS day
        out["uni"] = z["uni"].astype(bool)
    return out


def universe(day: dict, prev: dict, top_n: int, lo: float, hi: float) -> tuple[dict, dict]:
    """Rows restricted to the day's universe; prev re-ordered to the same rows. Selection uses the prior session only."""
    pidx = {s: i for i, s in enumerate(prev["syms"])}
    dv = np.nansum(prev["c"] * prev["v"], axis=1)
    rows = []
    flag = day.get("uni")
    for i, s in enumerate(day["syms"]):
        if flag is not None and not flag[i]:
            continue
        j = pidx.get(s)
        if j is None or not dv[j] > 0:
            continue
        o = day["o"][i]
        first = o[~np.isnan(o)]
        if len(first) == 0 or not (lo <= first[0] <= hi):
            continue
        rows.append((-dv[j], i, j))
    rows.sort()
    rows = rows[:top_n]
    di = [r[1] for r in rows]
    pj = [r[2] for r in rows]
    d = {"syms": [day["syms"][i] for i in di], **{k: day[k][di] for k in KEYS}}
    p = {"syms": d["syms"], **{k: prev[k][pj] for k in KEYS}}
    return d, p


def forward_returns(day: dict, hold: int) -> np.ndarray:
    """fwd[s, m] in bp = buy the open of bar m+1, sell the open of bar m+1+hold. NaN where either bar is missing."""
    o = day["o"]
    fwd = np.full(o.shape, np.nan)
    last = NBAR - hold - 1
    if last > 0:
        fwd[:, :last] = (o[:, 1 + hold:1 + hold + last] / o[:, 1:1 + last] - 1.0) * 1e4
    return fwd


# ---------- scoring ----------
def check_no_lookahead(fn, day: dict, prev: dict, sig: np.ndarray, rng, n_checks: int = 3) -> None:
    for m in rng.integers(30, NBAR - 30, size=n_checks):
        cut = {"syms": day["syms"], **{k: day[k].copy() for k in KEYS}}
        for k in KEYS:
            cut[k][:, m + 1:] = np.nan
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            s2 = np.asarray(fn(cut, prev), dtype=bool)
        if not np.array_equal(s2[:, :m + 1], sig[:, :m + 1]):
            bad = int(np.sum(s2[:, :m + 1] != sig[:, :m + 1]))
            raise SystemExit(f"LOOK-AHEAD: blanking bars after minute {int(m)} changed {bad} earlier signal cells. "
                             f"The signal reads the future; fix it before any result counts.")


def movers_mask(day: dict, prev: dict, min_pct: float) -> np.ndarray:
    """True where the name is up at least min_pct on the day at that minute's close (vs the prior session's last close)."""
    pc = prev["c"]
    last = np.full(pc.shape[0], np.nan)
    for m in range(pc.shape[1]):
        last = np.where(np.isnan(pc[:, m]), last, pc[:, m])
    with np.errstate(all="ignore"):
        return (day["c"] / last[:, None] - 1.0) * 100.0 >= min_pct


def score_day(day: dict, sig: np.ndarray, hold: int, ctl_mask: np.ndarray | None = None) -> dict | None:
    """One session -> trade lifts. Pure. Returns None when no trade.
    ctl_mask limits the same-minute control (and the baseline) to cells that pass the same pre-filter as the trades."""
    fwd = forward_returns(day, hold)
    valid = ~np.isnan(fwd)
    n, _ = fwd.shape
    take = np.zeros_like(valid)
    for s in range(n):                                   # one position per name at a time
        free_at = 0
        for m in np.flatnonzero(sig[s] & valid[s]):
            if m >= free_at:
                take[s, m] = True
                free_at = m + hold + 1
    if not take.any():
        return None
    f0 = np.where(valid, fwd, 0.0)
    cv = valid if ctl_mask is None else (valid & ctl_mask)
    col_sum, col_n = np.where(cv, f0, 0.0).sum(0), cv.sum(0)
    t_sum, t_n = np.where(take, f0, 0.0).sum(0), take.sum(0)
    with np.errstate(invalid="ignore", divide="ignore"):
        ctl_min = (col_sum - t_sum) / (col_n - t_n)      # same minute, every other name (signal names excluded)
    cs = np.concatenate([np.zeros((n, 1)), np.cumsum(f0, 1)], 1)
    cn = np.concatenate([np.zeros((n, 1)), np.cumsum(valid, 1)], 1)
    ss, mm = np.nonzero(take)
    a = np.minimum(mm + hold + 2, NBAR)
    b = np.minimum(mm + hold + 63, NBAR)
    with np.errstate(invalid="ignore", divide="ignore"):
        ctl_later = (cs[ss, b] - cs[ss, a]) / (cn[ss, b] - cn[ss, a])
    gross = fwd[ss, mm]
    return {"gross": gross, "lift": gross - ctl_min[mm], "lift_later": gross - ctl_later,
            "ctl_min": ctl_min[mm], "ctl_later": ctl_later, "px": day["o"][ss, mm + 1], "sym": [day["syms"][i] for i in ss], "minute": mm,
            "base_sum": float(col_sum.sum()), "base_n": int(col_n.sum())}


def day_stat(vals: list[float]) -> dict:
    x = np.array([v for v in vals if v is not None and not math.isnan(v)], dtype=float)
    if len(x) < 2:
        return {"n": len(x), "mean": float(x.mean()) if len(x) else float("nan"), "t": float("nan")}
    sd = x.std(ddof=1)
    return {"n": len(x), "mean": float(x.mean()), "t": float(x.mean() / (sd / math.sqrt(len(x)))) if sd > 0 else float("nan")}


def _dmean(per_day: list[dict], key: str, cap: float | None = None, minus: float = 0.0) -> list[float]:
    out = []
    for d in per_day:
        v = d[key][~np.isnan(d[key])]
        if cap is not None:
            v = np.minimum(v, cap)
        out.append(float(v.mean() - minus) if len(v) else float("nan"))
    return out


def verdict(per_day: list[dict], cfg: dict) -> dict:
    """per_day = score_day outputs in date order (days with trades only)."""
    n_tr = int(sum(len(d["gross"]) for d in per_day))
    crit = NormalDist().inv_cdf(1 - 0.025 / max(1, int(cfg["family"])))
    res = {"trades": n_tr, "days": len(per_day), "t_crit": round(crit, 2)}
    if not per_day:
        return {**res, "verdict": "NO TRADES"}
    cents = float(cfg.get("cent_spread", 0) or 0)           # cost floor of N cents of spread per round trip (cheap names)
    for d in per_day:
        with np.errstate(all="ignore"):
            tick = cents * 100.0 / d["px"] if cents > 0 else np.zeros(len(d["gross"]))
        d["net"] = d["gross"] - np.maximum(cfg["cost_bp"], tick)
        d["net_stress"] = d["gross"] - np.maximum(cfg["stress_bp"], 2.0 * tick)
        d["cost"] = d["gross"] - d["net"]
    h = len(per_day) // 2
    halves = (per_day[:h], per_day[h:])
    lift = _dmean(per_day, "lift")
    res["lift"] = day_stat(lift)
    res["lift_halves"] = [day_stat(_dmean(x, "lift"))["mean"] if x else float("nan") for x in halves]
    res["lift_later"] = day_stat(_dmean(per_day, "lift_later"))
    all_lift = np.concatenate([d["lift"][~np.isnan(d["lift"])] for d in per_day])
    cap = float(np.percentile(all_lift, 99)) if len(all_lift) else None
    res["lift_winsor"] = day_stat(_dmean(per_day, "lift", cap=cap))
    res["gross"] = day_stat(_dmean(per_day, "gross"))
    res["net"] = day_stat(_dmean(per_day, "net"))
    res["cost_mean"] = float(np.nanmean(np.concatenate([d["cost"] for d in per_day])))
    res["net_halves"] = [day_stat(_dmean(x, "net"))["mean"] if x else float("nan") for x in halves]
    res["net_stress"] = day_stat(_dmean(per_day, "net_stress"))
    # Per TRADE, not per day. A day-mean treats a day with 2 trades like a day with 120, and the busy days are the
    # market-wide ones where an entry behaves differently (vwap_deep 11/11: day-mean gross +16 bp, per-trade gross -2 bp).
    def _pooled(xs, key):
        v = np.concatenate([d[key] for d in xs]) if xs else np.array([])
        v = v[~np.isnan(v)]
        return float(v.mean()) if len(v) else float("nan")
    res["gross_trade"] = _pooled(per_day, "gross")
    res["net_trade"] = _pooled(per_day, "net")
    res["net_trade_halves"] = [_pooled(x, "net") for x in halves]
    res["net_trade_stress"] = _pooled(per_day, "net_stress")
    res["ctl_same_minute"] = float(np.nanmean(np.concatenate([d["ctl_min"] for d in per_day])))
    res["ctl_same_name_later"] = float(np.nanmean(np.concatenate([d["ctl_later"] for d in per_day])))
    res["baseline_random_minute"] = sum(d["base_sum"] for d in per_day) / max(1, sum(d["base_n"] for d in per_day))
    pos = lambda v: v is not None and not math.isnan(v) and v > 0
    edge = (all(pos(v) for v in res["lift_halves"]) and pos(res["lift"]["mean"]) and res["lift"]["t"] >= crit
            and pos(res["lift_later"]["mean"]) and pos(res["lift_winsor"]["mean"]))
    prof = (edge and all(pos(v) for v in res["net_halves"]) and pos(res["net_stress"]["mean"])
            and all(pos(v) for v in res["net_trade_halves"]) and pos(res["net_trade_stress"]))
    under = n_tr < cfg["min_trades"] or len(per_day) < cfg["min_days"]
    res["edge"], res["profitable"] = bool(edge), bool(prof)
    res["verdict"] = "UNDERPOWERED" if under else "PROFITABLE" if prof else "EDGE (component)" if edge else "FAIL"
    return res


def run(fn, hold: int, files: list[Path], cfg: dict, check: bool = True, seed: int = 1, mutate=None) -> dict:
    """files = warehouse day files in date order; files[0] only supplies the first prior session.
    mutate(day_dict, day_str) may alter the universe-restricted bars (canaries only)."""
    rng = np.random.default_rng(seed)
    per_day, dates, checks = [], [], 0
    prev_raw = load_day(files[0])
    for f in files[1:]:
        raw = load_day(f)
        day, prev = universe(raw, prev_raw, cfg["top_n"], cfg["price_lo"], cfg["price_hi"])
        prev_raw = raw
        if not day["syms"]:
            continue
        if mutate:
            mutate(day, f.stem)
        with np.errstate(all="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            sig = np.asarray(fn(day, prev), dtype=bool)
        if sig.shape != day["o"].shape:
            raise SystemExit(f"signal() returned shape {sig.shape}, expected {day['o'].shape}")
        if check and checks < 3 and sig.any():
            check_no_lookahead(fn, day, prev, sig, rng)
            checks += 1
        mask = movers_mask(day, prev, cfg["movers_pct"]) if cfg.get("movers_pct", 0) > 0 else None
        if mask is not None:
            sig = sig & mask
        r = score_day(day, sig, hold, mask)
        if r:
            per_day.append(r)
            dates.append(f.stem)
    res = verdict(per_day, cfg)
    res["first_day"], res["last_day"] = (dates[0], dates[-1]) if dates else (None, None)
    res["lookahead_checks"] = checks
    return res


# ---------- canaries (also used by tests) ----------
def random_signal(rate: float, seed: int):
    def fn(day, prev):
        h = int(hashlib.sha1("".join(day["syms"][:5]).encode()).hexdigest()[:8], 16)
        return np.random.default_rng(seed + h).random(day["o"].shape) < rate
    return fn


def planted(rate: float, bump_bp: float, hold: int, seed: int):
    """A random signal plus a mutate() that lifts every bar from HOLD minutes after each pick by bump_bp.
    Picks on one name are spaced so bumps never overlap. The harness must recover about +bump_bp of lift."""
    picks: dict[str, np.ndarray] = {}

    def mutate(day, day_str):
        rng = np.random.default_rng(seed + int(hashlib.sha1(day_str.encode()).hexdigest()[:8], 16))
        p = np.zeros(day["o"].shape, dtype=bool)
        for s in range(p.shape[0]):
            if rng.random() < rate * NBAR:
                m = int(rng.integers(5, NBAR - hold - 5))
                p[s, m] = True
                for k in ("o", "h", "l", "c"):
                    day[k][s, m + 2:] *= 1.0 + bump_bp / 1e4      # after the entry bar's open, before the exit bar's open
        picks[day_str] = p
        picks["_last"] = p

    def fn(day, prev):
        return picks["_last"].copy() if picks["_last"].shape == day["o"].shape else np.zeros(day["o"].shape, bool)
    return fn, mutate


def selftest(files: list[Path], cfg: dict) -> int:
    cfg = {**cfg, "min_trades": 0, "min_days": 0}
    null = run(random_signal(0.002, 7), 3, files, cfg, check=False)
    fn, mut = planted(0.002, 10.0, 3, 11)
    plant = run(fn, 3, files, cfg, check=False, mutate=mut)
    ok_null = abs(null["lift"]["mean"]) < 1.0 and not null["edge"]
    ok_plant = 8.0 <= plant["lift"]["mean"] <= 12.0 and plant["edge"]
    print(f"null canary   : lift {null['lift']['mean']:+.2f} bp (t {null['lift']['t']:+.2f}), {null['trades']} trades -> "
          f"{'OK' if ok_null else 'BROKEN'} (want about 0, no EDGE)")
    print(f"planted +10 bp: lift {plant['lift']['mean']:+.2f} bp (t {plant['lift']['t']:+.2f}), {plant['trades']} trades -> "
          f"{'OK' if ok_plant else 'BROKEN'} (want 8..12, EDGE)")
    return 0 if ok_null and ok_plant else 1


# ---------- cli ----------
def load_signal(path: Path):
    if str(path.resolve().parent) not in sys.path:
        sys.path.insert(0, str(path.resolve().parent))          # signal files may import their neighbours (_lib)
    spec = importlib.util.spec_from_file_location("harness_signal", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.signal, int(getattr(mod, "HOLD", 3)), str(getattr(mod, "NAME", path.stem))


def report(name: str, hold: int, r: dict, cfg: dict, final: bool) -> str:
    if r["verdict"] == "NO TRADES":
        return f"{name}: NO TRADES"
    L = [f"{name}  hold {hold}m  {'FINAL (hold-out included)' if final else 'development (hold-out hidden)'}",
         f"  {r['first_day']} .. {r['last_day']}: {r['trades']} trades on {r['days']} days; look-ahead checks passed {r['lookahead_checks']}",
         f"  lift vs same minute, other names : {r['lift']['mean']:+.2f} bp  day-t {r['lift']['t']:+.2f} (need {r['t_crit']})  "
         f"halves {r['lift_halves'][0]:+.2f} / {r['lift_halves'][1]:+.2f}",
         f"  lift vs same name, later         : {r['lift_later']['mean']:+.2f} bp  day-t {r['lift_later']['t']:+.2f}",
         f"  lift, top 1% of trades capped    : {r['lift_winsor']['mean']:+.2f} bp",
         f"  day-mean gross {r['gross']['mean']:+.2f}   net (mean cost {r['cost_mean']:.1f} bp) {r['net']['mean']:+.2f} (halves {r['net_halves'][0]:+.2f} / "
         f"{r['net_halves'][1]:+.2f})   net at stress cost {r['net_stress']['mean']:+.2f}",
         f"  PER TRADE (what an account gets): gross {r['gross_trade']:+.2f}   net {r['net_trade']:+.2f} (halves {r['net_trade_halves'][0]:+.2f} / "
         f"{r['net_trade_halves'][1]:+.2f})   net at stress cost {r['net_trade_stress']:+.2f}",
         f"  control means: same minute {r['ctl_same_minute']:+.2f}, same name later {r['ctl_same_name_later']:+.2f}, "
         f"random minute {r['baseline_random_minute']:+.2f}",
         f"  VERDICT: {r['verdict']}"]
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("signal", nargs="?")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--final", action="store_true", help="include the hold-out sessions (one read per NAME)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--warehouse", default=str(WH))
    ap.add_argument("--hold", type=int)
    ap.add_argument("--holds", help="comma list of holds in minutes, e.g. 3,15,60: one read each, t-bar tightened for the count")
    for k, v in DEFAULTS.items():
        ap.add_argument("--" + k.replace("_", "-"), type=type(v), default=v)
    a = ap.parse_args(argv)
    cfg = {k: getattr(a, k) for k in DEFAULTS}
    files = sorted(Path(a.warehouse).glob("*.npz"))
    if len(files) < 3:
        print(f"no warehouse days in {a.warehouse} (the warehouse lives on the mini; fetch with tools/studies/warehouse_fetch.py)")
        return 2
    dev = files[:-cfg["holdout"]] if cfg["holdout"] > 0 else files
    if a.selftest:
        return selftest(dev, cfg)
    if not a.signal:
        ap.error("give a signal file or --selftest")
    path = Path(a.signal)
    fn, hold, base = load_signal(path)
    holds = [int(x) for x in a.holds.split(",")] if a.holds else [a.hold or hold]
    cfg["family"] = max(cfg["family"], len(holds))
    OUT.mkdir(parents=True, exist_ok=True)
    ledger = OUT / "reads.jsonl"
    for hold in holds:
        name = (base if len(holds) == 1 else f"{base}@{hold}m") + (f"+up{cfg['movers_pct']:g}" if cfg["movers_pct"] > 0 else "")
        prior = [json.loads(x) for x in ledger.read_text().splitlines()] if ledger.exists() else []
        if a.final and not a.force and any(p["name"] == name and p["final"] for p in prior):
            print(f"REFUSED: '{name}' already had its final read (see {ledger}). A changed rule is a new NAME and a new idea.")
            return 3
        t0 = time.time()
        r = run(fn, hold, files if a.final else dev, cfg)
        n_dev = sum(1 for p in prior if p["name"] == name and not p["final"])
        print(report(name, hold, r, cfg, a.final))
        print(f"  ({time.time() - t0:.1f} s; development reads of this NAME before this one: {n_dev})")
        rec = {"name": name, "sha": hashlib.sha1(path.read_bytes()).hexdigest()[:12], "hold": hold, "final": bool(a.final),
               "when": time.strftime("%Y-%m-%d %H:%M"), "cfg": cfg, "result": r}
        with ledger.open("a") as fh:
            fh.write(json.dumps(rec, default=float) + "\n")
        (OUT / f"{name}{'_final' if a.final else ''}.json").write_text(json.dumps(rec, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
