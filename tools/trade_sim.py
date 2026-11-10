#!/usr/bin/env python3
"""S3 held-out trade simulation (docs/SIM_STAGE.md). Read-only; replaces the live forward test.

Every logged desk entry since 10/08 is replayed on its REAL post-fill SIP prints through the desk's own position code
(ai_positions.local_profit_stop / note_trail_print, tools/replay_session.open_position), baseline vs a variant, trade by trade.
Price-only: the trend hold needs recorded %R bars, so it is OFF in every arm (baseline = live trail without the hold).

    .venv/bin/python tools/trade_sim.py fetch    # mini, Alpaca pulls; refused weekdays 09:00-16:30 ET
    .venv/bin/python tools/trade_sim.py run      # -> docs/TRADE_SIM.md
"""
from __future__ import annotations

import math
import pickle
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
ET = ZoneInfo("America/New_York")
CACHE = ROOT / "ai_reports" / "trade_sim_prints_v2"        # v2: last-sale-eligible prints only (v1 kept odd-lot/out-of-sequence prints)
SINCE = "2026-10-08"
HOLD_MAX = 1500.0            # seconds of prints kept after the fill
COST = 3.0                   # bp per round trip charged to the sim (as the sweeps: net = bp - 3)
SELL_EXTRA = 3.0             # bp charged on the T1 share for the extra sale
VARIANTS = {
    "lock50": {"ai_local_trail_peak_lock_frac": 0.5, "ai_local_trail_peak_lock_from_bp": 15.0},
    "step01": {"ai_local_trail_decay_step_r": 0.01},
    "overtake_off": {"ai_local_trail_decay_overtake": False},
    "argon_exits": {"ai_local_trail_peak_lock_frac": 0.5, "ai_local_trail_peak_lock_from_bp": 15.0, "ai_local_trail_decay_step_r": 0.01},
    "t15_60": {"ai_watch_synth_rr": 0.03, "ai_watch_synth_scale_out_pct": 60.0},
    "t20_60": {"ai_watch_synth_rr": 0.04, "ai_watch_synth_scale_out_pct": 60.0},
    "t25_60": {"ai_watch_synth_rr": 0.05, "ai_watch_synth_scale_out_pct": 60.0},
    "t20_40": {"ai_watch_synth_rr": 0.04, "ai_watch_synth_scale_out_pct": 40.0},
}
# Loss-to-win gap (operator 11/10): the OPPOSITE direction to the locks above. run_* lets a green trade run (slower or no idle
# step, wider give from the peak, later breakeven); init_30 halves the opening stop; init_30_run does both. Prints end 25 min after
# the fill (HOLD_MAX), so a runner is cut there.
_RUN = {"ai_local_trail_time_decay_enabled": False, "ai_local_trail_peak_give_pct": 0.70, "ai_local_trail_be_at_pct": 0.30}
VARIANTS.update({
    "run_idle60": {"ai_local_trail_decay_idle_sec": 60.0},
    "run_decay_off": {"ai_local_trail_time_decay_enabled": False},
    "run_peak70": {"ai_local_trail_peak_give_pct": 0.70},
    "run_be30": {"ai_local_trail_be_at_pct": 0.30},
    "run_all": dict(_RUN),
    "init_30": {"ai_local_trail_initial_give_r": 0.06},
    "init_30_run": {**_RUN, "ai_local_trail_initial_give_r": 0.06},
})
# Never-green bleed (operator + Argon 11/07): (a) the existing underwater leash ai_local_trail_decay_red_step_r (live 0 = off),
# (b) a resting SCRATCH: once a trade has been never-green (peak < entry + 5 bp) for T s, rest a sell limit at the entry price; it fills
# when a print reaches entry x (1 + 2 bp) (bid-reach proxy); the -60 bp shelf stays as the backstop.
for _st in (0.005, 0.01, 0.02):
    VARIANTS[f"red{_st:g}"] = {"ai_local_trail_decay_red_step_r": _st}
for _T in (120, 300, 540):
    VARIANTS[f"scratch{_T}"] = {"sim_scratch_after_sec": float(_T), "sim_scratch_through_bp": 2.0}

# Live-path tranche A (code review 11/07): at the first 3 s book tick market-scale if already through T1, else rest a limit
# that fills once a print reaches target x (1 + th) - th 2 / 4 bp stand in for "the bid reached it". These need a code change
# live (ai_watch_synth_rr is clamped at max(0.25) in ai_entry_watch), so a result here is a code-change candidate only.
for _rr, _lab in ((0.03, 15), (0.04, 20), (0.05, 25)):
    for _th in (2, 4):
        VARIANTS[f"lim{_lab}_60_th{_th}"] = {"ai_watch_synth_rr": _rr, "ai_watch_synth_scale_out_pct": 60.0,
                                            "sim_t1_limit": True, "sim_t1_through_bp": float(_th)}


# ---------------- print hygiene ----------------
BAD_TICK_BP = 50.0
BAD_TICK_SIDE = 5


def clean_prints(pr, bp=300.0, k=BAD_TICK_SIDE):
    """Drop impossible prints using ONLY the past: a print more than `bp` (default 300 bp) from the median of the previous
    k prints. Backward-looking (11/07 audit: the two-sided version used future prints). Real 50-150 bp spikes are kept;
    the 900 bp erroneous prints that faked stop-outs are dropped. -> (clean, dropped)."""
    import statistics
    keep, dropped = [], 0
    raw = [float(y[1]) for y in pr]
    for i, x in enumerate(pr):
        prev = raw[max(0, i - k):i]       # previous RAW prints: a genuine gap is accepted once it is the majority
        if len(prev) >= 3:
            m = statistics.median(prev)
            if abs(float(x[1]) / m - 1) * 1e4 > bp:
                dropped += 1
                continue
        keep.append(x)
    return keep, dropped


def tick_series(pr, tick=0.25, max_age=15.0, t0=None):
    """What the desk sees each shelf tick: live re-feeds the LAST print every `tick` s while it is <= `max_age` s old
    (ai_positions ~5851-5873, code review 11/07). Forward-filled 0.25 s grid from t0 (default first print)."""
    if not pr:
        return []
    out, j, n = [], 0, len(pr)
    t = float(pr[0][0] if t0 is None else t0)
    end = float(pr[-1][0])
    while t <= end + 1e-9:
        while j + 1 < n and float(pr[j + 1][0]) <= t + 1e-9:
            j += 1
        if float(pr[j][0]) <= t + 1e-9 and t - float(pr[j][0]) <= max_age:
            out.append((t, float(pr[j][1]), 0.0))
        t += tick
    return out


def _fill_at(raw, ts, lat):
    """Market fill: the first RAW print at or after ts + lat (or the last print). -> (time, price)."""
    import bisect
    keys = [float(x[0]) for x in raw]
    k = bisect.bisect_left(keys, ts + lat - 1e-9)
    k = min(k, len(raw) - 1)
    return float(raw[k][0]), float(raw[k][1])


# ---------------- simulation core (pure given prints + cfg) ----------------
BOOK_TICK = 3.0              # ai_book_tick_sec: live attaches the resting T1 limit at the first book tick after the fill


def sim_trade(sym: str, entry: float, fill_ts: float, prints, cfg: dict, lat: float = 0.5, t1_delay: float | None = None) -> dict | None:
    """-> {gross_bp, why, hold_s, t1} for one trade.

    Decisions on the live shelf tick (0.25 s, last print re-fed while <= 15 s old, ring round(2.0/0.25) = 8). Every MARKET
    fill (stop exit, market tranche sale) happens at the first raw print `lat` s after the decision, in every arm. A resting
    tranche limit is attached at fill + BOOK_TICK and fills at its price once a raw print reaches target x (1 + through).
    t1_delay is accepted for old callers and ignored (the market tranche uses `lat` like every other market order)."""
    import ai_positions as cp
    import replay_session as rs
    raw, _drop = clean_prints([x for x in prints if x[0] >= fill_ts - 1e-9])
    if len(raw) < 2 or entry <= 0:
        return None
    tick = float(cfg.get("ai_shelf_tick_sec", 0.25) or 0.25)
    ticks = tick_series(raw, tick, 15.0, t0=float(raw[0][0]))
    if len(ticks) < 2:
        return None
    p = rs.open_position(cp, cfg, float(entry), float(fill_ts), None)
    if p is None:
        return None
    ring = max(2, int(round(2.0 / tick)))
    limit = bool(cfg.get("sim_t1_limit"))
    through = float(cfg.get("sim_t1_through_bp", 0.0) or 0.0)
    t1_rr = max(0.0, float(cfg.get("ai_watch_synth_rr", 0.6) or 0.0))
    sc = float(p["scale"])
    t1_on = t1_rr > 0 and sc > 0
    tgt_px = float(p["target_1"]) if t1_on else None
    lim_hit = None
    pend = None                  # market tranche: decision time
    scratch_T = float(cfg.get("sim_scratch_after_sec", 0) or 0)
    scratch_th = float(cfg.get("sim_scratch_through_bp", 2.0) or 0.0)
    scratch_at = None            # attach time of the resting scratch at entry
    rk = 0                       # pointer into raw prints for the scratch fill check
    if t1_on and limit:          # live path: at the first book tick, market-scale if already through T1, else rest a limit
        attach = fill_ts + BOOK_TICK
        before = [x for x in raw if float(x[0]) <= attach + 1e-9]
        if before and float(before[-1][1]) >= tgt_px:
            limit, pend = False, attach
        else:
            for x in raw:
                if float(x[0]) >= attach - 1e-9 and float(x[1]) >= tgt_px * (1 + through / 1e4):
                    lim_hit = float(x[0])
                    break

    def t1_result(stop_ts):
        """tranche share outcome given the stop decision time (None = never stopped). -> (bp or None, sold?)"""
        if not t1_on:
            return None
        if limit:
            if lim_hit is not None and (stop_ts is None or lim_hit < stop_ts + lat):
                return (tgt_px / entry - 1) * 1e4
            return None
        if pend is not None:     # an order already sent is not lost when the stop hits first
            _, px = _fill_at(raw, pend, lat)
            return (px / entry - 1) * 1e4 - SELL_EXTRA
        return None

    def finish(stop_ts, why):
        if stop_ts is None:
            ft, fpx = float(raw[-1][0]), float(raw[-1][1])
        else:
            ft, fpx = _fill_at(raw, stop_ts, lat)
        run = (fpx / entry - 1) * 1e4
        t1 = t1_result(stop_ts)
        if t1 is not None:
            return {"gross_bp": sc * t1 + (1 - sc) * run, "why": why, "hold_s": ft - fill_ts, "t1": True}
        return {"gross_bp": run, "why": why, "hold_s": ft - fill_ts, "t1": False}

    for ts, px, _ in ticks:
        if scratch_at is not None:
            while rk < len(raw) and float(raw[rk][0]) <= ts + 1e-9:
                if float(raw[rk][0]) >= scratch_at - 1e-9 and float(raw[rk][1]) >= entry * (1 + scratch_th / 1e4):
                    return {"gross_bp": 0.0, "why": "scratch", "hold_s": float(raw[rk][0]) - fill_ts, "t1": False}
                rk += 1
        if px <= float(p["local_stop_price"]) + 1e-9:
            return finish(ts, "local_trail")
        if scratch_T > 0 and scratch_at is None and ts >= fill_ts + scratch_T \
                and float(p.get("peak_price") or entry) < entry * (1 + 5 / 1e4):
            scratch_at = ts
            rk = 0
            while rk < len(raw) and float(raw[rk][0]) < ts - 1e-9:
                rk += 1
        if t1_on and not p["t1_hit"]:
            if pend is not None and ts >= pend:
                p["t1_hit"] = True
            elif limit and lim_hit is not None and lim_hit <= ts:
                p["t1_hit"] = True
            elif not limit and ts >= fill_ts + BOOK_TICK and px >= tgt_px * (1 + through / 1e4):
                pend = ts                                              # market scale on a book tick through T1
                p["t1_hit"] = True
            if p["t1_hit"]:   # runner floor as live: _runner_stop_level >= breakeven_floor (entry + 1c)
                p["local_stop_price"] = max(float(p["local_stop_price"]), float(cp.breakeven_floor(float(p["entry"]), cfg) or p["entry"]))
        e, risk = float(p["entry"]), float(p["risk_per_share"])
        p["peak_price"] = max(float(p.get("peak_price") or e), px)
        p["mfe_r"] = max(float(p["mfe_r"]), (p["peak_price"] - e) / risk)
        p["mae_r"] = min(float(p["mae_r"]), (px - e) / risk)
        p["last_seen_price"] = px
        cp.note_trail_print(p, px, n=ring)
        want = cp.local_profit_stop(p, cfg, now=float(ts))
        if want is not None and want > float(p["local_stop_price"]) + 1e-9:
            p["local_stop_price"] = want
    return finish(None, "window_end")


def base_cfg() -> dict:
    from config import load_config_ro
    cfg = dict(load_config_ro() or {})
    cfg.update({"ai_trail_exit_trend_hold": False, "ai_max_positions": 3, "ai_max_position_pct": 33.0, "ai_risk_pct": 1.65,
                "ai_watch_synth_rr": 1.0, "ai_watch_synth_scale_out_pct": 50.0})
    return cfg


# ---------------- statistics ----------------
def tstat(v):
    n = len(v)
    if n < 3:
        return float("nan")
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd else float("nan")


def compare(rows, boots=10000, seed=7):
    """rows: [{day, sym, base, var}] net bp per trade -> statistics of var - base, day-clustered."""
    if not rows:
        return {"n": 0}
    days = sorted({r["day"] for r in rows})
    mid = days[len(days) // 2]

    def by_day(rs):
        out = {}
        for r in rs:
            out.setdefault(r["day"], []).append(r["var"] - r["base"])
        return {d: sum(v) / len(v) for d, v in out.items()}
    dd = by_day(rows)
    vals = list(dd.values())
    h1 = [v for d, v in dd.items() if d < mid]
    h2 = [v for d, v in dd.items() if d >= mid]
    # drop rule (reported, not a veto - TEST_STANDARD): remove the best 5 days and the 5 best names, then per-day mean
    best = set(sorted(dd, key=lambda d: -dd[d])[:5])
    names = {}
    for r in rows:
        names[r["sym"]] = names.get(r["sym"], 0.0) + (r["var"] - r["base"])
    top = set(sorted(names, key=lambda x: -names[x])[:5])
    rest = by_day([r for r in rows if r["day"] not in best and r["sym"] not in top])
    # winsorized veto (TEST_STANDARD): clip the top 1% of per-trade deltas, re-check both halves
    deltas = sorted(r["var"] - r["base"] for r in rows)
    cap = deltas[max(0, int(0.99 * len(deltas)) - 1)]
    win = by_day([{**r, "var": r["base"] + min(r["var"] - r["base"], cap)} for r in rows])
    w1 = [v for d, v in win.items() if d < mid]
    w2 = [v for d, v in win.items() if d >= mid]
    rng = random.Random(seed)
    bs = sorted(sum(rng.choice(vals) for _ in vals) / len(vals) for _ in range(max(1, boots)))
    return {"n": len(rows), "days": len(days), "mean_day": sum(vals) / len(vals), "t": tstat(vals),
            "h1": sum(h1) / max(1, len(h1)), "h2": sum(h2) / max(1, len(h2)),
            "drop": sum(rest.values()) / max(1, len(rest)), "win_h1": sum(w1) / max(1, len(w1)), "win_h2": sum(w2) / max(1, len(w2)),
            "p5": bs[int(0.05 * len(bs))],
            "changed": sum(1 for r in rows if abs(r["var"] - r["base"]) > 1e-9),
            "base_mean": sum(r["base"] for r in rows) / len(rows), "var_mean": sum(r["var"] for r in rows) / len(rows)}


def verdict(s, s4):
    """IMPROVES at the primary latency (0.5 s) + S4 (every exit 1 s late, both arms - the rule as written before results)."""
    if not s.get("n"):
        return "NO_DATA", []
    if s["changed"] < max(10, 0.05 * s["n"]):
        return "VACUOUS", [f"only {s['changed']} of {s['n']} trades changed"]
    c = {"both halves > 0": s["h1"] > 0 and s["h2"] > 0, "day-t >= 2.0": s["t"] >= 2.0,
         "winsorized both halves > 0": s["win_h1"] > 0 and s["win_h2"] > 0, "bootstrap p5 > 0": s["p5"] > 0,
         "S4 all exits 1 s late > 0": bool(s4.get("n")) and s4["mean_day"] > 0}
    bad = [k for k, v in c.items() if not v]
    return ("IMPROVES" if not bad else "NO"), bad


# ---------------- data ----------------
def load_trades():
    from early_path import trades_since
    tr, unmatched = trades_since(SINCE)
    load_trades.unmatched = unmatched
    return tr


def fetch():
    from early_path import blocked_now
    if blocked_now():
        print("refused: no Alpaca pulls 09:00-16:30 ET on weekdays (desk API budget)")
        return 1
    import alpaca_api as aa
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    from config import load_config_ro
    client = aa.connect_data_client(dict(load_config_ro() or {}))
    CACHE.mkdir(parents=True, exist_ok=True)
    today = datetime.now(ET).strftime("%Y-%m-%d")
    done = bad = 0
    for t in load_trades():
        f = CACHE / f"{t['day']}_{t['sym']}_{int(t['fill_ts'])}.pkl"
        if f.exists() or t["day"] >= today:
            continue
        a, b = t["fill_ts"] - 2, t["fill_ts"] + HOLD_MAX
        try:
            got = client.get_stock_trades(StockTradesRequest(symbol_or_symbols=t["sym"], start=datetime.fromtimestamp(a, timezone.utc),
                                                             end=datetime.fromtimestamp(b, timezone.utc), feed=DataFeed.SIP))
            import replay_session as rs
            rows = [(x.timestamp.timestamp(), float(x.price), float(x.size or 0)) for x in (got.data or {}).get(t["sym"], [])
                    if not rs.NON_LAST_SALE.intersection(getattr(x, "conditions", None) or ())]
        except Exception as e:  # noqa: BLE001
            bad += 1
            print("ERR", t["day"], t["sym"], str(e)[:80])
            continue
        pickle.dump(sorted(rows), open(f, "wb"))
        done += 1
        time.sleep(0.2)
    print(f"fetched {done}, errors {bad}")
    return 0


LATENCIES = (0.0, 0.5, 1.0)   # 0 = reference only; 0.5 = primary; 1.0 = S4


def run(only=None):
    cfg0 = base_cfg()
    all_tr = load_trades()
    trades, skipped = [], 0
    for t in all_tr:
        f = CACHE / f"{t['day']}_{t['sym']}_{int(t['fill_ts'])}.pkl"
        if f.exists():
            trades.append((t, pickle.load(open(f, "rb"))))
        else:
            skipped += 1
    if not trades:
        print("no cached prints: run 'fetch' first")
        return 1

    def net(t, pr, over, lat):
        r = sim_trade(t["sym"], t["entry"], t["fill_ts"], pr, {**cfg0, **over}, lat)
        return None if r is None else r["gross_bp"] - COST

    base = {L: {i: net(t, pr, {}, L) for i, (t, pr) in enumerate(trades)} for L in LATENCIES}
    live = [x["net_bp"] - COST for x, _ in trades]
    bm = {L: sum(v for v in b.values() if v is not None) / max(1, sum(1 for v in b.values() if v is not None)) for L, b in base.items()}
    lines = [f"# S3/S4 trade simulation (tools/trade_sim.py), {datetime.now(ET):%Y-%m-%d %H:%M} ET", "",
             f"Trades: {len(all_tr)} matched since {SINCE} ({getattr(load_trades, 'unmatched', '?')} unmatched outcomes), {len(trades)} simulated, {skipped} skipped for missing prints. "
             f"{len(VARIANTS)} variants (cells) on one sample: about one t~2 is expected by chance; the lim grid is correlated with itself.",
             f"Sim baseline mean net at exit latency 0 / 0.5 / 1.0 s: {bm[0.0]:+.2f} / {bm[0.5]:+.2f} / {bm[1.0]:+.2f} bp vs logged desk {sum(live) / len(live):+.2f} bp. "
             "Known baseline gaps: no trend hold, no dead_trade / no_progress / double_check / left_overbought / EOD exits, no broker-mark low.", "",
             "Verdict at 0.5 s exit latency (both arms). IMPROVES = beats the sim baseline on every condition; it does NOT mean profitable (last column).", "",
             "| variant | verdict | changed | base -> var net (0.5 s) | delta @0 / @0.5 / @1.0 s | t | halves | winsorized halves | drop5 (reported) | boot p5 | failed | profitable @3 / @10 bp |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, over in VARIANTS.items():
        if only and not any(name.startswith(o) for o in only):
            continue
        st_by = {}
        for L in LATENCIES:
            rows = []
            for i, (t, pr) in enumerate(trades):
                v = net(t, pr, over, L)
                if base[L][i] is not None and v is not None:
                    rows.append({"day": t["day"], "sym": t["sym"], "base": base[L][i], "var": v})
            st_by[L] = compare(rows, boots=10000 if L == 0.5 else 1)
        s, s4 = st_by[0.5], st_by[1.0]
        st, bad = verdict(s, s4)
        if not s.get("n"):
            lines.append(f"| {name} | {st} | | | | | | | | | | |")
            continue
        lines.append(f"| {name} | **{st}** | {s['changed']} | {s['base_mean']:+.1f} -> {s['var_mean']:+.1f} | "
                     f"{st_by[0.0]['mean_day']:+.2f} / {s['mean_day']:+.2f} / {s4['mean_day']:+.2f} | {s['t']:.2f} | {s['h1']:+.2f}/{s['h2']:+.2f} | "
                     f"{s['win_h1']:+.2f}/{s['win_h2']:+.2f} | {s['drop']:+.2f} | {s['p5']:+.2f} | {'; '.join(bad)} | "
                     f"{'yes' if s['var_mean'] > 0 else 'no'} / {'yes' if s['var_mean'] - 7.0 > 0 else 'no'} |")
    out = "\n".join(lines) + "\n"
    name = "TRADE_SIM.md" if not only else f"TRADE_SIM_{'_'.join(only)}.md"
    (ROOT / "docs" / name).write_text(out)
    print(out)
    return 0


def main(argv=None):
    a = (argv if argv is not None else sys.argv[1:]) or ["run"]
    if a[0] == "fetch":
        return fetch()
    only = a[a.index("--only") + 1].split(",") if "--only" in a else None
    return run(only)


if __name__ == "__main__":
    raise SystemExit(main())
