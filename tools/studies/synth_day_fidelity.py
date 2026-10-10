#!/usr/bin/env python3
"""synth_day_fidelity.py — hist_sim_prereg.json fidelity gate part A: synthetic-day replay vs recorded replay.

On the recorded calibration days (2026-09-24..10-09, seen) the same code and the same day's config run twice:
  (a) the normal recorded replay (tools/replay_session.py --day DAY), and
  (b) the synthetic-day replay (--universe) fed that day's RECORDED watchlist (first admission per symbol from
      ai_reports/admit_range.jsonl, in the historical-universe format), the day's config stream and equity.
The difference is the synthesis error alone (prices from SIP prints, engine on IEX bars, fills at the next
print), not the universe error (part B, hist_universe.py).

SUBCOMMANDS (mini; data reads only, never on a day before 2026-09-24)
  export --out DIR DAY...          universe / config stream / equity per day, from the recordings (read-only)
         [--intervals]             also write DAY-iv.json with candidate spans from sessions/DAY/sources.jsonl.gz
  run    --out DIR --lane i/n --leg rec|syn [--universe-kind first|iv] DAY...
                                   the replays, nice'd, one at a time per lane; DIR/{rec,syn}/DAY-VARIANT.json
  score  --out DIR DAY...          the gate table (trades/day, overlap both ways within 90 s, gross / net bp per
                                   trade, diffs, paired wrrsi_st-base, per-day sd) -> stdout + DIR/fidelity.json

Variants (prereg amendment eb0c863): base = the day's config; st = time decay off + SuperTrend exit;
wrrsi_st = %R trend 15 + RSI rise 10 + st. Net = ret - full SIP spread at entry (replay_costing), no NBBO = that
day's pooled median across variants and legs (counted). The costing cache is a private copy (--cost-cache).
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(os.environ.get("REPO") or Path(__file__).resolve().parents[2])
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]

FIRST_CALIBRATION_DAY = "2026-09-24"
VARIANTS = {
    "base": [],
    "st": ["--set", "ai_local_trail_time_decay_enabled=false", "--set", "ai_exit_supertrend=true"],
    "wrrsi_st": ["--set", "ai_watch_wr_trend_min_rise=15", "--set", "ai_watch_wr_rsi_min_rise=10",
                 "--set", "ai_local_trail_time_decay_enabled=false", "--set", "ai_exit_supertrend=true"],
}
GATE = {"pooled_abs_diff_bp": 1.5, "day_abs_diff_bp": 6.0, "days_within": 9, "recall": 0.65,
        "paired_abs_diff_bp": 1.5}
MATCH_SEC = 90.0


def check_days(days: list[str]) -> None:
    bad = [d for d in days if d < FIRST_CALIBRATION_DAY]
    if bad:
        raise SystemExit(f"refused: {bad} are held out (before {FIRST_CALIBRATION_DAY})")


# ── export ──────────────────────────────────────────────────────────────────────────────────────────────

def recorded_universe(admit_rows, day: str) -> list[dict]:
    """First admission per symbol on *day*, in the historical-universe format. Pure."""
    first: dict[str, dict] = {}
    for r in admit_rows:
        if r.get("day") != day or not r.get("symbol") or r.get("ts") is None:
            continue
        s = str(r["symbol"]).upper()
        if s not in first or float(r["ts"]) < first[s]["first_ts"]:
            first[s] = {"symbol": s, "first_ts": float(r["ts"]), "source": str(r.get("source") or ""),
                        "raw_price": r.get("price"), "pct_change": r.get("pct_change"), "rvol": r.get("rvol")}
    return sorted(first.values(), key=lambda u: (u["first_ts"], u["symbol"]))


def candidate_intervals(source_rows, end_ts: float) -> dict[str, list[list[float]]]:
    """{symbol: [[enter, leave], ...]} of candidate presence (any source) from sources.jsonl rows. Pure."""
    open_: dict[tuple, float] = {}
    spans: dict[str, list] = defaultdict(list)
    for r in sorted((r for r in source_rows if r.get("event") in ("enter", "leave")), key=lambda r: r["ts"]):
        k = (str(r["symbol"]).upper(), r.get("source"))
        if r["event"] == "enter":
            open_.setdefault(k, float(r["ts"]))
        elif k in open_:
            spans[k[0]].append([open_.pop(k), float(r["ts"])])
    for k, t0 in open_.items():
        spans[k[0]].append([t0, end_ts])
    out = {}
    for s, iv in spans.items():
        iv.sort()
        merged = [list(iv[0])]
        for a, b in iv[1:]:
            if a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        out[s] = merged
    return out


def scan_snapshots(path: Path, t_start: float) -> tuple[list[dict], float | None]:
    """Every config/bot_config.json record (as {ts, config}) and the dashboard equity at t_start."""
    configs, equity, past = [], None, False
    with gzip.open(path, "rt") as f:
        for line in f:
            head = line[:200]
            is_cfg = '"config/bot_config.json"' in head
            if not is_cfg and (past or '"api/state"' not in head):
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("file") == "config/bot_config.json" and isinstance(r.get("data"), dict):
                configs.append({"ts": float(r["ts"]), "config": r["data"]})
            elif r.get("file") == "api/state":
                if float(r.get("ts") or 0) > t_start:
                    past = True
                    continue
                try:
                    equity = float(r["data"]["ai_positions"]["account"]["equity"])
                except (KeyError, TypeError, ValueError):
                    pass
    return configs, equity


def cmd_export(a) -> None:
    import replay_session as rp
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    admit = [json.loads(x) for x in open(ROOT / "ai_reports" / "admit_range.jsonl") if x.strip()]
    for day in a.days:
        uni = recorded_universe(admit, day)
        (out / f"{day}-first.json").write_text(json.dumps(uni, indent=0))
        if a.intervals:
            src = ROOT / "ai_reports" / "sessions" / day / "sources.jsonl.gz"
            if src.exists():
                rows = [json.loads(x) for x in gzip.open(src, "rt") if x.strip()]
                iv = candidate_intervals(rows, rp.at(day, "16:00"))
                (out / f"{day}-iv.json").write_text(json.dumps(
                    [dict(u, intervals=iv.get(u["symbol"], [])) for u in uni], indent=0))
        snap = rp.SNAP_BASE / day / "state_snapshots.jsonl.gz"
        cfgs, eq = scan_snapshots(snap, rp.at(day, "09:30"))
        with open(out / f"{day}-config.jsonl", "w") as f:
            for c in cfgs:
                f.write(json.dumps(c) + "\n")
        (out / f"{day}-equity.json").write_text(json.dumps({"equity": eq}))
        print(f"{day}: universe {len(uni)} {dict(Counter(u['source'] for u in uni))}, "
              f"{len(cfgs)} config versions, equity {eq}", flush=True)


# ── run ─────────────────────────────────────────────────────────────────────────────────────────────────

def cmd_run(a) -> None:
    out = Path(a.out)
    i, n = (int(x) for x in a.lane.split("/"))
    jobs = [(d, v) for v in VARIANTS for d in a.days]
    jobs = jobs[i::n]
    leg_dir = out / (a.leg if a.leg == "rec" else f"syn_{a.universe_kind}")
    leg_dir.mkdir(parents=True, exist_ok=True)
    py = str(ROOT / ".venv" / "bin" / "python")
    for d, v in jobs:
        res = leg_dir / f"{d}-{v}.json"
        if res.exists():
            continue
        cmd = ["nice", "-n", "10", py, str(ROOT / "tools" / "replay_session.py"), "--day", d, "--out", str(res)]
        if a.sha:
            cmd += ["--sha", a.sha]
        if a.leg == "syn":
            eq = json.loads((out / f"{d}-equity.json").read_text()).get("equity")
            cmd += ["--universe", str(out / f"{d}-{a.universe_kind}.json"),
                    "--config-stream", str(out / f"{d}-config.jsonl"), "--engine-bars", "iex"]
            if eq:
                cmd += ["--equity", str(eq)]
        cmd += VARIANTS[v]
        t0 = time.time()
        with open(leg_dir / f"{d}-{v}.log", "w") as log:
            rc = subprocess.call(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT))
        with open(leg_dir / "status.txt", "a") as f:
            f.write(f"{d} {v} rc={rc} wall={time.time() - t0:.0f}s\n")


# ── score ───────────────────────────────────────────────────────────────────────────────────────────────

def match_share(a_trades: list[dict], b_trades: list[dict], sec: float = MATCH_SEC) -> tuple[int, int]:
    """(# of a reproduced by b, # of a): same symbol, entry within *sec*, one-to-one nearest. Pure."""
    used, hit = set(), 0
    for t in sorted(a_trades, key=lambda x: x["t"]):
        best = None
        for j, u in enumerate(b_trades):
            if j in used or u["symbol"] != t["symbol"] or abs(u["t"] - t["t"]) > sec:
                continue
            if best is None or abs(u["t"] - t["t"]) < abs(b_trades[best]["t"] - t["t"]):
                best = j
        if best is not None:
            used.add(best)
            hit += 1
    return hit, len(a_trades)


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def sd(xs):
    xs = [x for x in xs if x is not None]
    return statistics.stdev(xs) if len(xs) > 1 else None


def gate_verdict(rows: dict, paired: dict, g: dict = GATE) -> dict:
    """Part A conditions per variant + paired. rows[v] = {"pooled_diff", "day_diffs", "recall"}. Pure."""
    out, ok = {}, True
    for v, r in rows.items():
        within = sum(1 for x in r["day_diffs"] if x is not None and abs(x) <= g["day_abs_diff_bp"])
        c = {"pooled_abs_diff_ok": r["pooled_diff"] is not None and abs(r["pooled_diff"]) <= g["pooled_abs_diff_bp"],
             "days_within_6bp": within, "days_within_ok": within >= g["days_within"],
             "recall_ok": r["recall"] is not None and r["recall"] >= g["recall"]}
        c["pass"] = c["pooled_abs_diff_ok"] and c["days_within_ok"] and c["recall_ok"]
        ok = ok and c["pass"]
        out[v] = c
    p_ok = paired.get("diff") is not None and abs(paired["diff"]) <= g["paired_abs_diff_bp"]
    out["paired"] = {"pass": p_ok}
    out["PASS"] = ok and p_ok
    return out


def cmd_score(a) -> dict:
    import replay_costing as RC
    out = Path(a.out)
    cache_path = Path(a.cost_cache)
    if not cache_path.exists():
        real = ROOT / "ai_reports" / "replay_costing_cache.json"
        if real.exists():
            shutil.copy2(real, cache_path)
    RC.CACHE = str(cache_path)
    cache, box = RC.load_cache(), {}
    legs = {"rec": out / "rec", "syn": out / f"syn_{a.universe_kind}"}
    trades = {(leg, v, d): [] for leg in legs for v in VARIANTS for d in a.days}
    missing, no_nbbo, vac = [], Counter(), {}
    for d in a.days:
        raw, spreads = {}, []
        for leg, ldir in legs.items():
            for v in VARIANTS:
                p = ldir / f"{d}-{v}.json"
                if not p.exists():
                    missing.append(f"{leg}/{d}-{v}")
                    continue
                res = json.loads(p.read_text())
                if leg == "syn":
                    vac[f"{d}-{v}"] = res.get("vacuity")
                for t in res.get("closed") or []:
                    if t.get("ret") is None:
                        continue
                    spr = RC.entry_spread_bp(cache, t["symbol"], t["entry_ts"], box)
                    if spr is not None:
                        spreads.append(spr)
                    raw.setdefault((leg, v), []).append((t, spr))
        med = statistics.median(spreads) if spreads else None
        for (leg, v), xs in raw.items():
            for t, spr in xs:
                if spr is None:
                    no_nbbo[(leg, v)] += 1
                    if med is None:
                        continue
                    spr = med
                trades[(leg, v, d)].append({
                    "symbol": t["symbol"], "t": float(t.get("decision_ts") or t["entry_ts"]),
                    "gross": t["ret"] * 1e4, "net": t["ret"] * 1e4 - spr})
        RC.save_cache(cache)
    n_days = len(a.days)
    report: dict = {"days": a.days, "universe_kind": a.universe_kind, "missing": missing,
                    "no_nbbo": {f"{k[0]}/{k[1]}": n for k, n in no_nbbo.items()}, "per_day": {}, "pooled": {},
                    "gate": GATE, "vacuity": vac}
    gate_rows = {}
    print(f"SYNTHETIC-DAY FIDELITY (part A, universe '{a.universe_kind}') {a.days[0]}..{a.days[-1]} "
          f"({n_days} days); net = ret - full SIP spread at entry; overlap within {MATCH_SEC:.0f} s")
    for v in VARIANTS:
        print(f"\n[{v}]  {'day':10s} {'n_rec':>5} {'n_syn':>5} {'rec->syn':>8} {'syn->rec':>8} "
              f"{'gross_r':>8} {'gross_s':>8} {'net_r':>7} {'net_s':>7} {'diff':>7}")
        hit_a = tot_a = hit_b = tot_b = 0
        day_diffs, per = [], {}
        for d in a.days:
            ra, sb = trades[("rec", v, d)], trades[("syn", v, d)]
            h1, n1 = match_share(ra, sb)
            h2, n2 = match_share(sb, ra)
            hit_a, tot_a, hit_b, tot_b = hit_a + h1, tot_a + n1, hit_b + h2, tot_b + n2
            nr, ns = mean([x["net"] for x in ra]), mean([x["net"] for x in sb])
            diff = ns - nr if nr is not None and ns is not None else None
            day_diffs.append(diff)
            per[d] = {"n_rec": n1, "n_syn": n2, "rec_reproduced": h1 / n1 if n1 else None,
                      "syn_reproduced": h2 / n2 if n2 else None,
                      "gross_rec": mean([x["gross"] for x in ra]), "gross_syn": mean([x["gross"] for x in sb]),
                      "net_rec": nr, "net_syn": ns, "diff": diff}
            f = lambda x, w=7: f"{x:+{w}.1f}" if x is not None else f"{'-':>{w}}"  # noqa: E731
            g = lambda x: f"{x:8.0%}" if x is not None else f"{'-':>8}"  # noqa: E731
            print(f"      {d:10s} {n1:5d} {n2:5d} {g(per[d]['rec_reproduced'])} {g(per[d]['syn_reproduced'])} "
                  f"{f(per[d]['gross_rec'], 8)} {f(per[d]['gross_syn'], 8)} {f(nr)} {f(ns)} {f(diff)}")
        allr = [x for d in a.days for x in trades[("rec", v, d)]]
        alls = [x for d in a.days for x in trades[("syn", v, d)]]
        pr, ps = mean([x["net"] for x in allr]), mean([x["net"] for x in alls])
        pooled = {"trades_per_day_rec": len(allr) / n_days, "trades_per_day_syn": len(alls) / n_days,
                  "recall_rec_by_syn": hit_a / tot_a if tot_a else None,
                  "precision_syn_in_rec": hit_b / tot_b if tot_b else None,
                  "gross_rec": mean([x["gross"] for x in allr]), "gross_syn": mean([x["gross"] for x in alls]),
                  "net_rec": pr, "net_syn": ps, "diff": ps - pr if pr is not None and ps is not None else None,
                  "sd_day_net_syn": sd([per[d]["net_syn"] for d in a.days])}
        report["per_day"][v], report["pooled"][v] = per, pooled
        gate_rows[v] = {"pooled_diff": pooled["diff"], "day_diffs": day_diffs, "recall": pooled["recall_rec_by_syn"]}
        print(f"      POOLED     trades/day rec {pooled['trades_per_day_rec']:.1f} syn {pooled['trades_per_day_syn']:.1f}; "
              f"rec reproduced {pooled['recall_rec_by_syn'] or 0:.0%}, syn in rec {pooled['precision_syn_in_rec'] or 0:.0%}; "
              f"net rec {pr if pr is not None else float('nan'):+.2f} syn {ps if ps is not None else float('nan'):+.2f} "
              f"diff {pooled['diff'] if pooled['diff'] is not None else float('nan'):+.2f} bp/trade")
    # Paired: (syn wrrsi_st - syn base) - (rec wrrsi_st - rec base), pooled trade means; per-day sd of the
    # synthetic paired difference (power, prereg) and of wrrsi_st net bp/trade.
    po = report["pooled"]
    if all(po[v][k] is not None for v in ("base", "wrrsi_st") for k in ("net_rec", "net_syn")):
        d_syn = po["wrrsi_st"]["net_syn"] - po["base"]["net_syn"]
        d_rec = po["wrrsi_st"]["net_rec"] - po["base"]["net_rec"]
        paired = {"syn_wrrsi_st_minus_base": d_syn, "rec_wrrsi_st_minus_base": d_rec, "diff": d_syn - d_rec}
    else:
        paired = {"diff": None}
    per_day_pd = [report["per_day"]["wrrsi_st"][d]["net_syn"] - report["per_day"]["base"][d]["net_syn"]
                  for d in a.days if report["per_day"]["wrrsi_st"][d]["net_syn"] is not None
                  and report["per_day"]["base"][d]["net_syn"] is not None]
    paired["sd_day_paired_syn"] = sd(per_day_pd)
    paired["sd_day_wrrsi_st_net_syn"] = po["wrrsi_st"]["sd_day_net_syn"]
    report["paired"] = paired
    report["verdict"] = gate_verdict(gate_rows, paired)
    print(f"\nPAIRED wrrsi_st - base: syn {paired.get('syn_wrrsi_st_minus_base', float('nan')):+.2f} "
          f"rec {paired.get('rec_wrrsi_st_minus_base', float('nan')):+.2f} -> diff "
          f"{paired['diff'] if paired['diff'] is not None else float('nan'):+.2f} bp (gate <= {GATE['paired_abs_diff_bp']})")
    print(f"per-day sd (synthetic): wrrsi_st net bp/trade {paired['sd_day_wrrsi_st_net_syn']}, "
          f"paired wrrsi_st-base {paired['sd_day_paired_syn']}")
    for v in VARIANTS:
        c = report["verdict"][v]
        print(f"GATE {v:9s} pooled |diff|<=1.5 {c['pooled_abs_diff_ok']}; days |diff|<=6: {c['days_within_6bp']}/"
              f"{n_days} (>= 9) {c['days_within_ok']}; reproduced >= 65% {c['recall_ok']} -> "
              f"{'PASS' if c['pass'] else 'FAIL'}")
    print(f"GATE paired -> {'PASS' if report['verdict']['paired']['pass'] else 'FAIL'};  "
          f"PART A {'PASS' if report['verdict']['PASS'] else 'FAIL'}")
    if missing:
        print(f"missing runs: {missing}")
    if no_nbbo:
        print(f"trades without NBBO (charged the day's median): {dict(report['no_nbbo'])}")
    (out / f"fidelity_{a.universe_kind}.json").write_text(json.dumps(report, indent=1, default=str))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("export", "run", "score"):
        p = sub.add_parser(name)
        p.add_argument("--out", required=True)
        p.add_argument("days", nargs="+")
        if name == "export":
            p.add_argument("--intervals", action="store_true")
        if name in ("run", "score"):
            p.add_argument("--universe-kind", default="first", choices=("first", "iv"))
        if name == "run":
            p.add_argument("--lane", default="0/1")
            p.add_argument("--leg", required=True, choices=("rec", "syn"))
            p.add_argument("--sha", default=None)
        if name == "score":
            p.add_argument("--cost-cache", default="/tmp/ws_r_out/costing_cache.json")
    a = ap.parse_args(argv)
    check_days(a.days)
    {"export": cmd_export, "run": cmd_run, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    main()
