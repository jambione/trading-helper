#!/usr/bin/env python3
"""Exit capture. Spec: docs/studies/exit_capture_prereg.json (as amended).

If our entries HAD a real edge, how much of it would the live exit stack keep, and how big must an entry edge be
for the desk to make money? For every desk paper fill (>= $10, 9/23-10/20) the real SIP prints after entry are
multiplied by (1 + mu * g(t)), g ramping linearly from 0 at entry to 1 at tau, and the LIVE exit code runs on them
second by second (leash_replay's walk, plus the live trail-exit trend hold on drift-scaled IEX 1-minute bars and the
live dead_trade semantics). mu = 0 is the real path; its fidelity against what live booked gates the verdict.

  .venv/bin/python tools/studies/exit_capture.py            # fetch what is missing (data account), replay, report
  .venv/bin/python tools/studies/exit_capture.py --offline  # replay from the cache only (no network)

Writes ai_reports/exit_capture/{result.json, result.md}; caches in ai_reports/exit_capture/cache.pkl.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import pickle
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import leash_replay as L  # noqa: E402

cp, bars, ROOT = L.cp, L.bars, L.ROOT
OUT_DIR = os.path.join(ROOT, "ai_reports", "exit_capture")
CACHE = os.path.join(OUT_DIR, "cache.pkl")
FIRST_DAY, LAST_DAY, MIN_PRICE = "2026-09-23", "2026-10-20", 10.0
MUS = (0, 5, 10, 20, 40)
TAUS = (180, 600)
INFO_CELLS = (("ramp", -10, 180), ("ramp", -10, 600), ("step", 10, 60), ("step", 20, 60))
HOLDS = (180, 600, "end")
CONFIG_SHA_PREFIX = "cd84f7109ef61ca9"
FIDELITY_FP, FIDELITY_SEC, FIDELITY_RECALL, FIDELITY_GAP_BP = "4b267f14f8f8", 60, 0.55, 5.0
BOOT_N, BOOT_TAG = 2000, "exit-capture"
REQ_GAP_S = 0.4
DEAD_DEFAULT_MIN, DEAD_DEFAULT_MFE = 22.0, 0.10


# ── drift ────────────────────────────────────────────────────────────────────

def g(dt: float, tau: float, shape: str = "ramp") -> float:
    """Share of the injected edge that has arrived dt seconds after entry."""
    if dt <= 0:
        return 0.0
    if shape == "step":
        return 1.0 if dt >= tau else 0.0
    return min(dt / tau, 1.0)


def factor(t: float, t0: float, mu_bp: float, tau: float, shape: str = "ramp") -> float:
    return 1.0 + mu_bp / 1e4 * g(t - t0, tau, shape)


def scale_secs(secs: list, t0: float, mu_bp: float, tau: float, shape: str = "ramp") -> list:
    """leash_replay second rows (second, last, low) with every price after entry scaled by the drift."""
    if not mu_bp:
        return secs
    return [(s, last * factor(s, t0, mu_bp, tau, shape), lo * factor(s, t0, mu_bp, tau, shape)) for s, last, lo in secs]


def scale_bars(b1m: list, t0: float, mu_bp: float, tau: float, shape: str = "ramp") -> list:
    """IEX 1-minute bars (start, o, h, l, c); each bar scaled at its close time (start + 60)."""
    out = []
    for st, o, h, lo, c in b1m:
        f = factor(st + 60, t0, mu_bp, tau, shape)
        out.append((st, o * f, h * f, lo * f, c * f))
    return out


# ── the live exit walk with drift ────────────────────────────────────────────

def dead_trade_params(cfg: dict) -> tuple[float, float]:
    """Live semantics (ai_positions): float(value or default), so 0 means the default, not off (skeptic #2)."""
    return (float(cfg.get("ai_dead_trade_min") or DEAD_DEFAULT_MIN),
            float(cfg.get("ai_dead_trade_mfe_r") or DEAD_DEFAULT_MFE))


_BARS_NOW: dict = {"bars": None}


def _store_bars(_ticker):
    return _BARS_NOW["bars"]


def bars_known(b1m: list, now: float) -> tuple | None:
    """What the live store holds at `now`: closed bars (start + 60 <= now) as (stamps, highs, lows, closes); None
    under 20 bars, as ai_positions._double_check_store_bars."""
    rows = [b for b in b1m if b[0] + 60 <= now]
    if len(rows) < 20:
        return None
    return ([b[0] for b in rows], [b[2] for b in rows], [b[3] for b in rows], [b[4] for b in rows])


def walk(f: dict, secs: list, cfg: dict, ring: int, b1m: list | None = None) -> tuple[int, float, str, int]:
    """leash_replay.walk (variant A: no min hold on the trail) plus the live trail-exit trend hold and the live
    dead_trade semantics. Returns (exit second, trigger price, reason, trend-hold seconds)."""
    p = L.new_pos(f, cfg)
    p["entry_stop_price"] = f["entry"] - f["risk"]
    entry, risk, t0 = f["entry"], f["risk"], f["t0"]
    dead_min, dead_mfe = dead_trade_params(cfg)
    by_s = {s: (last, lo) for s, last, lo in secs}
    last_px, held = entry, 0
    end = int(t0) + L.MAX_HOLD
    orig = cp._double_check_store_bars
    cp._double_check_store_bars = _store_bars
    try:
        for s in range(int(t0) + 1, end + 1):
            if bars.et_minutes(s) >= L.EOD_MIN:
                return s, last_px, "eod", held
            row = by_s.get(s)
            stop = float(p["local_stop_price"])
            if row is not None:
                last, lo = row
                if lo <= entry - risk + 1e-9:
                    return s, lo, "plan_stop", held
                if lo <= stop + 1e-9:
                    _BARS_NOW["bars"] = bars_known(b1m or [], s)
                    if cp.trail_exit_trend_hold(p, f["sym"], float(lo), now=float(s), cfg=cfg):
                        held += 1
                    else:
                        return s, stop, "trail", held
                last_px = last
                cp.note_trail_print(p, last, n=ring)
                p["last_seen_price"] = last
                p["peak_price"] = max(float(p["peak_price"]), last)
                p["mfe_r"] = max(float(p["mfe_r"]), (p["peak_price"] - entry) / risk)
                p["mae_r"] = min(float(p["mae_r"]), (last - entry) / risk)
            want = cp.local_profit_stop(p, cfg, now=float(s))
            if want is not None and want > float(p["local_stop_price"]) + 1e-9:
                p["local_stop_price"] = want
            if (dead_min > 0 and float(p["local_stop_price"]) <= entry + 1e-9
                    and (s - t0) / 60 >= dead_min and p["mfe_r"] < dead_mfe):
                return s, last_px, "dead_trade", held
        return end, last_px, "time_cap", held
    finally:
        cp._double_check_store_bars = orig
        _BARS_NOW["bars"] = None


def exit_price(trig: float, bid: float | None, why: str) -> float | None:
    """leash_replay's rule: a stop exit fills at min(trigger, bid); other exits at the bid. No bid -> None (the
    fill is dropped from every cell; the spread is never silently skipped)."""
    if bid is None:
        return None
    return min(trig, bid) if why in ("trail", "plan_stop") else bid


def hold_second(t0: float, h) -> int:
    """A fixed-horizon hold exit: t0 + h seconds, or min(t0 + 2 h, 15:50) for 'end'; never past 15:50."""
    day = bars.day_of(t0)
    eod = int(datetime.strptime(day, "%Y-%m-%d").replace(hour=15, minute=50, tzinfo=bars.ET).timestamp())
    s = int(t0) + (L.MAX_HOLD if h == "end" else int(h))
    return min(s, eod)


# ── statistics ───────────────────────────────────────────────────────────────

def day_mean_ci(vals_by_day: dict) -> tuple[float | None, float | None, int]:
    """Day-equal mean and day-clustered SE."""
    m = [statistics.mean(v) for v in vals_by_day.values() if v]
    if not m:
        return None, None, 0
    mu = statistics.mean(m)
    se = statistics.stdev(m) / math.sqrt(len(m)) if len(m) >= 2 else None
    return mu, se, len(m)


def capture_per_fill(net_mu: float, net_0: float, mu_bp: float) -> float:
    return (net_mu - net_0) / mu_bp


def break_even(curve: list[tuple[float, float]]) -> float | str | None:
    """Smallest mu with net > 0 by linear interpolation on (mu, net) sorted by mu: 0 if net(0) > 0; '>max' if it
    never crosses (no extrapolation)."""
    curve = sorted(curve)
    if not curve:
        return None
    if curve[0][1] > 0:
        return 0.0
    for (m1, n1), (m2, n2) in zip(curve, curve[1:]):
        if n1 <= 0 < n2:
            return m1 + (0 - n1) * (m2 - m1) / (n2 - n1)
    return f">{curve[-1][0]:g}"


def bootstrap_break_even(net_by_mu_day: dict, n: int = BOOT_N, tag: str = BOOT_TAG) -> tuple:
    """Day-level bootstrap of mu*: resample days with replacement; returns (2.5%, 97.5%) over numeric draws and the
    share of draws that never cross ('>max')."""
    days = sorted(next(iter(net_by_mu_day.values())).keys())
    rng = random.Random(int(hashlib.sha256(tag.encode()).hexdigest()[:16], 16))
    draws, never = [], 0
    for _ in range(n):
        pick = [rng.choice(days) for _ in days]
        curve = []
        for mu, by_day in net_by_mu_day.items():
            vals = [statistics.mean(by_day[d]) for d in pick if by_day.get(d)]
            curve.append((mu, statistics.mean(vals)))
        be = break_even(curve)
        if isinstance(be, str):
            never += 1
        elif be is not None:
            draws.append(be)
    draws.sort()
    if not draws:
        return None, None, never / n
    return draws[int(0.025 * len(draws))], draws[min(len(draws) - 1, int(0.975 * len(draws)))], never / n


def winsor(xs: list[float], lo: float = 0.01, hi: float = 0.99) -> list[float]:
    s = sorted(xs)
    if not s:
        return xs
    a, b = s[int(lo * (len(s) - 1))], s[int(hi * (len(s) - 1))]
    return [min(max(x, a), b) for x in xs]


# ── data ─────────────────────────────────────────────────────────────────────

def data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])


def load_cache() -> dict:
    try:
        return pickle.load(open(CACHE, "rb"))
    except (OSError, EOFError, pickle.UnpicklingError):
        return {"trades": {}, "bars": {}, "bid": {}}


def save_cache(c: dict) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = CACHE + ".tmp"
    pickle.dump(c, open(tmp, "wb"))
    os.replace(tmp, CACHE)


def fetch_bid(cl, sym: str, ts: int, tries: int = 5, sleep=time.sleep) -> float | None:
    """Last clean SIP quote in [ts - 15 s, ts]; retried; raises after the last try (the caller never caches a
    failure). A successful fetch with no quote returns None (cached: there is genuinely no quote)."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    end = datetime.fromtimestamp(ts, timezone.utc)
    for k in range(tries):
        try:
            q = cl.get_stock_quotes(StockQuotesRequest(symbol_or_symbols=sym, start=end - timedelta(seconds=15),
                                                       end=end, feed=DataFeed.SIP)).data.get(sym) or []
            sleep(REQ_GAP_S)
            q = [x for x in q if x.bid_price and x.ask_price and x.ask_price > x.bid_price]
            return float(q[-1].bid_price) if q else None
        except Exception as e:  # noqa: BLE001
            print(f"  quote {sym} {ts} try {k}: {str(e)[:80]}", flush=True)
            sleep(5 * (k + 1))
    raise RuntimeError(f"bid fetch failed {sym} {ts}")


def bid(cl, cache: dict, sym: str, ts: int, offline: bool = False) -> float | None | str:
    key = (sym, int(ts))
    if key in cache["bid"]:
        return cache["bid"][key]
    if offline or cl is None:
        return "missing"
    try:
        b = fetch_bid(cl, sym, int(ts))
    except RuntimeError:
        return "missing"                     # not cached: a later run retries
    cache["bid"][key] = b
    return b


def fetch_bars(cl, sym: str, day: str) -> list:
    """IEX 1-minute bars 09:30-16:00 of `day` as (start, o, h, l, c) - the live trend-hold store's feed."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    y, m, d = map(int, day.split("-"))
    t0 = datetime(y, m, d, 9, 30, tzinfo=bars.ET)
    r = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=sym, timeframe=TimeFrame.Minute, start=t0,
                                           end=t0 + timedelta(hours=6, minutes=30), feed=DataFeed.IEX)).data.get(sym) or []
    time.sleep(REQ_GAP_S)
    return [(b.timestamp.timestamp(), float(b.open), float(b.high), float(b.low), float(b.close)) for b in r]


def load_fills() -> tuple[list[dict], Counter]:
    st, out = Counter(), []
    for line in open(os.path.join(ROOT, "ai_reports", "outcomes.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        t0, e, sp = r.get("entry_time"), r.get("entry_price"), r.get("stop_price")
        if not isinstance(t0, (int, float)) or not e or not sp or r.get("entry_path") != "watch":
            continue
        day = bars.day_of(t0)
        if not FIRST_DAY <= day <= LAST_DAY:
            continue
        e, sp = float(e), float(sp)
        if e < MIN_PRICE:
            st["under_10"] += 1
            continue
        if not 0 < sp < e:
            st["bad_stop"] += 1
            continue
        m = bars.et_minutes(t0)
        if m < 9 * 60 + 30 or m >= L.EOD_MIN:
            st["outside_rth"] += 1
            continue
        out.append({"sym": r["symbol"], "day": day, "t0": float(t0), "entry": e, "risk": e - sp,
                    "spread_r": r.get("spread_r"), "config_fp": r.get("config_fp"),
                    "live_exit": r.get("exit_price"), "live_exit_time": r.get("exit_time")})
    return out, st


def cells() -> list[tuple[str, float, float]]:
    out = [("ramp", 0, 180)]
    out += [("ramp", mu, tau) for tau in TAUS for mu in MUS if mu]
    return out + list(INFO_CELLS)


# ── run ──────────────────────────────────────────────────────────────────────

def replay(fills, cache, cfg, ring, cl, offline) -> tuple[dict, Counter]:
    """{fill index: {cell: (net_bp, exit_s, why, held_s), ('hold', h, cell): net_bp}}; a fill missing any bid,
    prints or bars is dropped from every cell."""
    res, st = {}, Counter()
    for i, f in enumerate(fills):
        secs = cache["trades"].get((f["sym"], int(f["t0"])))
        b1m = cache["bars"].get((f["sym"], f["day"]))
        if not secs:
            st["no_prints"] += 1
            continue
        if b1m is None:
            st["no_bars"] += 1
            continue
        row, ok = {}, True
        for cell in cells():
            shape, mu, tau = cell
            sc = scale_secs(secs, f["t0"], mu, tau, shape)
            xs, trig, why, held = walk(f, sc, cfg, ring, scale_bars(b1m, f["t0"], mu, tau, shape))
            b = bid(cl, cache, f["sym"], xs, offline)
            if b == "missing" or b is None:
                ok = False
                st[f"no_bid_{shape}_{mu}_{tau}"] += 1
                break
            px = exit_price(trig, b * factor(xs, f["t0"], mu, tau, shape), why)
            row[cell] = ((px / f["entry"] - 1) * 1e4, xs, why, held)
            for h in HOLDS:
                hs = hold_second(f["t0"], h)
                hb = bid(cl, cache, f["sym"], hs, offline)
                if hb == "missing" or hb is None:
                    ok = False
                    st[f"no_hold_bid_{h}"] += 1
                    break
                row[("hold", h, cell)] = (hb * factor(hs, f["t0"], mu, tau, shape) / f["entry"] - 1) * 1e4
            if not ok:
                break
        if ok:
            res[i] = row
        if (i + 1) % 50 == 0:
            save_cache(cache)
            print(f"  {i + 1}/{len(fills)} fills replayed ({datetime.now():%H:%M})", flush=True)
    save_cache(cache)
    return res, st


def summarize(fills, res) -> dict:
    out = {"cells": {}, "hold_did": {}, "naive": {}, "fidelity": {}, "buckets": {}}
    keep = sorted(res)
    base = ("ramp", 0, 180)
    for cell in cells():
        shape, mu, tau = cell
        by_day, cap, why, wins, holds, heldsec = defaultdict(list), defaultdict(list), Counter(), 0, [], 0
        for i in keep:
            net, xs, w, held = res[i][cell]
            by_day[fills[i]["day"]].append(net)
            if mu:
                cap[fills[i]["day"]].append(capture_per_fill(net, res[i][base][0], mu))
            why[w] += 1
            wins += net > 0
            holds.append(xs - fills[i]["t0"])
            heldsec += held
        m, se, nd = day_mean_ci(by_day)
        c, cse, _ = day_mean_ci(cap) if mu else (None, None, 0)
        cw = None
        if mu:
            allc = [x for v in cap.values() for x in v]
            cw = statistics.mean(winsor(allc)) if allc else None
        out["cells"][f"{shape}|{mu}|{tau}"] = {"net": m, "net_se": se, "days": nd, "n": len(keep),
                                               "capture": c, "capture_se": cse, "capture_winsor": cw,
                                               "win": wins / len(keep) if keep else None,
                                               "median_hold": statistics.median(holds) if holds else None,
                                               "why": dict(why), "trend_hold_seconds": heldsec}
        if mu:
            for h in HOLDS:
                did = defaultdict(list)
                for i in keep:
                    did[fills[i]["day"]].append((res[i][cell][0] - res[i][("hold", h, cell)])
                                                - (res[i][base][0] - res[i][("hold", h, base)]))
                dm, dse, _ = day_mean_ci(did)
                out["hold_did"][f"{shape}|{mu}|{tau}|{h}"] = {"did": dm, "t": dm / dse if dse else None}
    for tau in TAUS:
        nv = defaultdict(list)
        for i in keep:
            nv[fills[i]["day"]].append(g(res[i][base][1] - fills[i]["t0"], tau))
        out["naive"][tau] = day_mean_ci(nv)[0]
    # fidelity on the current-config fills
    fid = [i for i in keep if fills[i]["config_fp"] == FIDELITY_FP and fills[i]["live_exit"] and fills[i]["live_exit_time"]]
    if fid:
        close = sum(1 for i in fid if abs(res[i][base][1] - float(fills[i]["live_exit_time"])) <= FIDELITY_SEC)
        rep = statistics.mean(res[i][base][0] for i in fid)
        live = statistics.mean((float(fills[i]["live_exit"]) / fills[i]["entry"] - 1) * 1e4 for i in fid)
        out["fidelity"] = {"n": len(fid), "recall": close / len(fid), "replay_net": rep, "live_net": live,
                           "gap": rep - live,
                           "pass": close / len(fid) >= FIDELITY_RECALL and abs(rep - live) <= FIDELITY_GAP_BP}
    else:
        out["fidelity"] = {"n": 0, "pass": False}
    # break-even per tau
    for tau in TAUS:
        curve, by_mu_day = [], {}
        for mu in MUS:
            cell = ("ramp", mu, tau) if mu else base
            d = defaultdict(list)
            for i in keep:
                d[fills[i]["day"]].append(res[i][cell][0])
            by_mu_day[mu] = d
            curve.append((mu, day_mean_ci(d)[0]))
        lo, hi, never = bootstrap_break_even(by_mu_day)
        shift = out["fidelity"].get("gap") or 0.0
        out.setdefault("break_even", {})[tau] = {
            "mu_star": break_even(curve), "ci": (lo, hi), "boot_never_share": never,
            "mu_star_live_shifted": break_even([(m, n - shift) for m, n in curve]), "curve": curve}
    # price buckets at mu 10 / 20, tau 180
    for name, lo_, hi_ in (("<$20", 0, 20), ("$20-50", 20, 50), (">$50", 50, 1e9)):
        for mu in (10, 20):
            cap = defaultdict(list)
            for i in keep:
                if lo_ <= fills[i]["entry"] < hi_:
                    cap[fills[i]["day"]].append(capture_per_fill(res[i][("ramp", mu, 180)][0], res[i][base][0], mu))
            out["buckets"][f"{name}|{mu}"] = day_mean_ci(cap)[0]
    out["verdict"] = verdict(out)
    return out


def verdict(out: dict) -> str:
    if not out["fidelity"].get("pass"):
        return "UNPROVEN (fidelity gate failed)"
    c10, c20 = (out["cells"][f"ramp|{m}|180"] for m in (10, 20))
    caps = [c10["capture"], c20["capture"]]
    lows = [c["capture"] - 1.96 * (c["capture_se"] or 0) for c in (c10, c20)]
    if all(x is not None and x >= 0.70 for x in caps) and all(x > 0.5 for x in lows):
        return "KEEPS AN EDGE"
    if any(x is not None and x <= 0.40 for x in caps):
        return "LEAKY"
    return "IN BETWEEN"


def report(out: dict, meta: dict) -> str:
    def bp(x):
        return "-" if x is None else f"{x:+.1f}"
    L_ = ["# Exit capture: result", "", "Prereg: docs/studies/exit_capture_prereg.json (amended). Live exit code on real fill "
          "paths with an injected drift; net = real fill -> bid at exit (spread inside), bp of entry.", "",
          f"Fills: {meta['fills']} loaded, {meta['kept']} replayed in every cell; drops {meta['drops']}. Config sha "
          f"{meta['config_sha'][:16]}, code {meta['code']}.", "",
          f"**Verdict: {out['verdict']}**", "",
          f"Fidelity (config {FIDELITY_FP}, mu = 0 vs live booked): {out['fidelity']}", "",
          "| shape | mu bp | tau s | net (SE) | capture (SE) | capture winsor | win | median hold s | exits |",
          "|---|---|---|---|---|---|---|---|---|"]
    for k, c in out["cells"].items():
        s, mu, tau = k.split("|")
        cap = "-" if c["capture"] is None else f"{c['capture']:.2f}"
        cse = "-" if c["capture_se"] is None else f"{c['capture_se']:.2f}"
        cw = "-" if c["capture_winsor"] is None else f"{c['capture_winsor']:.2f}"
        win = "-" if c["win"] is None else f"{c['win']:.0%}"
        L_.append(f"| {s} | {mu} | {tau} | {bp(c['net'])} ({bp(c['net_se'])}) | {cap} ({cse}) | {cw} | {win} | "
                  f"{c['median_hold']} | {c['why']} |")
    L_ += ["", f"Naive capture (exit times unchanged): {out['naive']}", "",
           "Break-even entry edge mu* (bp), day-bootstrap 95% interval:", ""]
    for tau, b in out["break_even"].items():
        L_.append(f"- tau {tau} s: mu* {b['mu_star']}, CI {b['ci']}, never-crosses share {b['boot_never_share']:.0%}, "
                  f"live-shifted {b['mu_star_live_shifted']}; curve {[(m, round(n, 1)) for m, n in b['curve']]}")
    L_ += ["", "Exits vs a plain hold (difference-in-differences, bp; t):", ""]
    for k, v in out["hold_did"].items():
        L_.append(f"- {k}: {bp(v['did'])} (t {'-' if v['t'] is None else round(v['t'], 2)})")
    L_ += ["", f"Capture by price at tau 180: {out['buckets']}"]
    return "\n".join(L_)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--allow-config-change", action="store_true")
    a = ap.parse_args()
    cfg_path = os.path.join(ROOT, "config", "bot_config.json")
    sha = hashlib.sha256(open(cfg_path, "rb").read()).hexdigest()
    if not sha.startswith(CONFIG_SHA_PREFIX) and not a.allow_config_change:
        raise SystemExit(f"config sha {sha[:16]} != registered {CONFIG_SHA_PREFIX}; pass --allow-config-change only "
                         f"with a prereg amendment")
    cfg = copy.deepcopy(L.load_config())
    cfg.pop("_min_hold", None)                                   # variant A
    ring = max(2, int(cfg.get("ai_local_trail_print_ring", 3) or 3))
    fills, st = load_fills()
    print(f"{len(fills)} fills (>= ${MIN_PRICE:g}, {FIRST_DAY} .. {LAST_DAY}); skipped {dict(st)}", flush=True)
    cache = load_cache()
    old = L.load_cache()                                         # reuse leash_replay's prints where present
    cl = None if a.offline else data_client()
    for k, f in enumerate(fills, 1):
        key = (f["sym"], int(f["t0"]))
        if key not in cache["trades"]:
            if key in old.get("trades", {}):
                cache["trades"][key] = old["trades"][key]
            elif cl is not None:
                try:
                    cache["trades"][key] = L.fetch_seconds(cl, f["sym"], f["t0"] - 5, f["t0"] + L.MAX_HOLD + 60)
                    time.sleep(REQ_GAP_S)
                except Exception as e:  # noqa: BLE001
                    print(f"  prints {f['sym']}: {str(e)[:80]}", flush=True)
        bk = (f["sym"], f["day"])
        if bk not in cache["bars"] and cl is not None:
            try:
                cache["bars"][bk] = fetch_bars(cl, f["sym"], f["day"])
            except Exception as e:  # noqa: BLE001
                print(f"  bars {f['sym']} {f['day']}: {str(e)[:80]}", flush=True)
        if k % 50 == 0:
            save_cache(cache)
            print(f"  data {k}/{len(fills)}", flush=True)
    save_cache(cache)
    res, drops = replay(fills, cache, cfg, ring, cl, a.offline)
    out = summarize(fills, res)
    code = os.popen(f"git -C {ROOT} log --oneline -1").read().strip()
    md = report(out, {"fills": len(fills), "kept": len(res), "drops": dict(drops), "config_sha": sha, "code": code})
    print(md)
    os.makedirs(OUT_DIR, exist_ok=True)
    open(os.path.join(OUT_DIR, "result.md"), "w").write(md + "\n")
    json.dump({**out, "config_sha": sha, "code": code, "drops": dict(drops)}, open(os.path.join(OUT_DIR, "result.json"), "w"),
              indent=1, default=str)


if __name__ == "__main__":
    main()
