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
# Resting-limit tranche A (operator 11/07: is it a strategy success but an execution failure?): sell at the target, no spread crossed,
# but only once price has traded THROUGH the target by th bp (stand-in for "the bid has reached it" on the paper account).
for _rr, _lab in ((0.03, 15), (0.04, 20), (0.05, 25)):
    for _th in (2, 4, 6):
        VARIANTS[f"lim{_lab}_60_th{_th}"] = {"ai_watch_synth_rr": _rr, "ai_watch_synth_scale_out_pct": 60.0,
                                            "sim_t1_limit": True, "sim_t1_through_bp": float(_th)}


# ---------------- print hygiene ----------------
BAD_TICK_BP = 50.0
BAD_TICK_SIDE = 5


def clean_prints(pr, bp=BAD_TICK_BP, k=BAD_TICK_SIDE):
    """Drop ISOLATED bad ticks: a print more than `bp` from the median of the k prints before AND the median of the k after,
    on the same side. A real gap (prints before at the old level, prints after at the new one) is kept. -> (clean, dropped)."""
    import statistics
    n = len(pr)
    if n < 2 * k + 1:
        return list(pr), 0
    px = [float(x[1]) for x in pr]
    keep, dropped = [], 0
    for i in range(n):
        lo, hi = px[max(0, i - k):i], px[i + 1:i + 1 + k]
        if len(lo) >= 3 and len(hi) >= 3:
            ml, mh = statistics.median(lo), statistics.median(hi)
            dl, dh = (px[i] / ml - 1) * 1e4, (px[i] / mh - 1) * 1e4
            if (dl > bp and dh > bp) or (dl < -bp and dh < -bp):
                dropped += 1
                continue
        keep.append(pr[i])
    return keep, dropped


# ---------------- simulation core (pure given prints + cfg) ----------------
def sim_trade(sym: str, entry: float, fill_ts: float, prints, cfg: dict, delay: float = 0.0) -> dict | None:
    """-> {gross_bp, why, hold_s, t1} for one trade. prints: [(ts, px, size)] sorted, from the fill on."""
    import ai_positions as cp
    import replay_session as rs
    pr, _drop = clean_prints([x for x in prints if x[0] >= fill_ts - 1e-9])
    if len(pr) < 2 or entry <= 0:
        return None
    p = rs.open_position(cp, cfg, float(entry), float(fill_ts), None)
    if p is None:
        return None
    ring = max(2, int(cfg.get("ai_local_trail_print_ring", 3) or 3))
    n = len(pr)

    def finish(i, why, t1_cost=True):
        j = i
        if delay > 0:
            while j < n - 1 and pr[j][0] < pr[i][0] + delay:
                j += 1
        px = float(pr[j][1])
        run = (px / entry - 1) * 1e4
        if p["t1_hit"]:
            tgt = (float(p["target_1"]) / entry - 1) * 1e4
            sc = float(p["scale"])
            extra = 0.0 if cfg.get("sim_t1_limit") else SELL_EXTRA        # a resting limit sells at its price, no spread crossed
            return {"gross_bp": sc * tgt + (1 - sc) * run - (sc * extra if t1_cost else 0.0), "why": why,
                    "hold_s": pr[j][0] - fill_ts, "t1": True}
        return {"gross_bp": run, "why": why, "hold_s": pr[j][0] - fill_ts, "t1": False}

    for i, (ts, px, _sz) in enumerate(pr):
        px = float(px)
        if px <= float(p["local_stop_price"]) + 1e-9:
            return finish(i, "local_trail")
        t1_rr = max(0.0, float(cfg.get("ai_watch_synth_rr", 0.6) or 0.0))
        through = float(cfg.get("sim_t1_through_bp", 0.0) or 0.0)      # resting limit: fills only after price trades THROUGH the target
        if t1_rr > 0 and float(p["scale"]) > 0 and not p["t1_hit"] and px >= float(p["target_1"]) * (1 + through / 1e4):
            p["t1_hit"], p["t1_r"] = True, t1_rr
            p["local_stop_price"] = max(float(p["local_stop_price"]), float(p["entry"]))   # tranche B to breakeven, as live
        e, risk = float(p["entry"]), float(p["risk_per_share"])
        p["peak_price"] = max(float(p.get("peak_price") or e), px)
        p["mfe_r"] = max(float(p["mfe_r"]), (p["peak_price"] - e) / risk)
        p["mae_r"] = min(float(p["mae_r"]), (px - e) / risk)
        p["last_seen_price"] = px
        cp.note_trail_print(p, px, n=ring)
        want = cp.local_profit_stop(p, cfg, now=float(ts))
        if want is not None and want > float(p["local_stop_price"]) + 1e-9:
            p["local_stop_price"] = want
    return finish(n - 1, "window_end")


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


def compare(rows, key="delta", boots=10000, seed=7):
    """rows: [{day, sym, base, var}] net bp per trade. -> S3/S4 statistics for var - base."""
    if not rows:
        return {"n": 0}
    days = sorted({r["day"] for r in rows})
    mid = days[len(days) // 2]
    dd = {d: sum(r["var"] - r["base"] for r in rows if r["day"] == d) / sum(1 for r in rows if r["day"] == d) for d in days}
    vals = list(dd.values())
    half = lambda f: [v for d, v in dd.items() if f(d)]  # noqa: E731
    h1, h2 = half(lambda d: d < mid), half(lambda d: d >= mid)
    best = set(sorted(dd, key=lambda d: -dd[d])[:5])
    names = {}
    for r in rows:
        names[r["sym"]] = names.get(r["sym"], 0.0) + (r["var"] - r["base"])
    top = set(sorted(names, key=lambda s: -names[s])[:5])
    rest = [r["var"] - r["base"] for r in rows if r["day"] not in best and r["sym"] not in top]
    rng = random.Random(seed)
    bs = sorted(sum(rng.choice(vals) for _ in vals) / len(vals) for _ in range(boots))
    return {"n": len(rows), "days": len(days), "mean_day": sum(vals) / len(vals), "t": tstat(vals),
            "h1": sum(h1) / max(1, len(h1)), "h2": sum(h2) / max(1, len(h2)),
            "drop": sum(rest) / max(1, len(rest)), "p5": bs[int(0.05 * len(bs))],
            "changed": sum(1 for r in rows if abs(r["var"] - r["base"]) > 1e-9),
            "base_mean": sum(r["base"] for r in rows) / len(rows), "var_mean": sum(r["var"] for r in rows) / len(rows)}


def verdict(s, s_delay):
    """S3 bar + S4 bar from docs/SIM_STAGE.md. -> (status, failed conditions)."""
    if not s.get("n"):
        return "NO_DATA", []
    if s["changed"] < max(10, 0.05 * s["n"]):
        return "VACUOUS", [f"only {s['changed']} of {s['n']} trades changed"]
    c = {"both halves > 0": s["h1"] > 0 and s["h2"] > 0, "day-t >= 2.0": s["t"] >= 2.0, "drop best5 days+top5 names > 0": s["drop"] > 0,
         "bootstrap p5 > 0": s["p5"] > 0, "1 s delay still > 0": bool(s_delay.get("n")) and s_delay["mean_day"] > 0}
    bad = [k for k, v in c.items() if not v]
    return ("PASS" if not bad else "FAIL"), bad


# ---------------- data ----------------
def load_trades():
    from early_path import trades_since
    tr, _ = trades_since(SINCE)
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


def run():
    cfg0 = base_cfg()
    trades = []
    for t in load_trades():
        f = CACHE / f"{t['day']}_{t['sym']}_{int(t['fill_ts'])}.pkl"
        if f.exists():
            trades.append((t, pickle.load(open(f, "rb"))))
    if not trades:
        print("no cached prints: run 'fetch' first")
        return 1

    def net(t, pr, over, delay=0.0):
        r = sim_trade(t["sym"], t["entry"], t["fill_ts"], pr, {**cfg0, **over}, delay)
        return None if r is None else r["gross_bp"] - COST

    base = {i: net(t, pr, {}) for i, (t, pr) in enumerate(trades)}
    base_d = {i: net(t, pr, {}, 1.0) for i, (t, pr) in enumerate(trades)}
    live = [x["net_bp"] - COST for x, _ in trades]
    ndrop = sum(clean_prints([x for x in pr if x[0] >= t["fill_ts"] - 1e-9])[1] for t, pr in trades)
    nall = sum(len(pr) for _, pr in trades)
    lines = [f"# S3/S4 trade simulation (tools/trade_sim.py), {datetime.now(ET):%Y-%m-%d %H:%M} ET", "",
             f"{len(trades)} desk entries since {SINCE} with cached post-fill SIP prints (max {HOLD_MAX:.0f} s). Price-only: trend hold OFF in every arm. "
             f"Sim baseline mean net {sum(v for v in base.values() if v is not None) / max(1, sum(1 for v in base.values() if v is not None)):+.2f} bp vs "
             f"logged desk net {sum(live) / len(live):+.2f} bp (the gap is the trend hold, fills and the window). Bad-tick filter dropped {ndrop} of {nall} prints ({100 * ndrop / max(1, nall):.3f}%).", "",
             "| variant | verdict | n | changed | base -> var net bp | mean day delta | t | halves | drop5 | boot p5 | 1 s delay delta | failed |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, over in VARIANTS.items():
        rows, rows_d = [], []
        for i, (t, pr) in enumerate(trades):
            v, vd = net(t, pr, over), net(t, pr, over, 1.0)
            if base[i] is not None and v is not None:
                rows.append({"day": t["day"], "sym": t["sym"], "base": base[i], "var": v})
            if base_d[i] is not None and vd is not None:
                rows_d.append({"day": t["day"], "sym": t["sym"], "base": base_d[i], "var": vd})
        s, sd = compare(rows), compare(rows_d, boots=1)
        st, bad = verdict(s, sd)
        if not s.get("n"):
            lines.append(f"| {name} | {st} | 0 | | | | | | | | | |")
            continue
        lines.append(f"| {name} | **{st}** | {s['n']} | {s['changed']} | {s['base_mean']:+.1f} -> {s['var_mean']:+.1f} | {s['mean_day']:+.2f} | {s['t']:.2f} | "
                     f"{s['h1']:+.2f}/{s['h2']:+.2f} | {s['drop']:+.2f} | {s['p5']:+.2f} | {sd.get('mean_day', float('nan')):+.2f} | {'; '.join(bad)} |")
    out = "\n".join(lines) + "\n"
    (ROOT / "docs" / "TRADE_SIM.md").write_text(out)
    print(out)
    return 0


def main(argv=None):
    a = (argv if argv is not None else sys.argv[1:]) or ["run"]
    return fetch() if a[0] == "fetch" else run()


if __name__ == "__main__":
    raise SystemExit(main())
