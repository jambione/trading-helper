#!/usr/bin/env python3
"""late_gates_read.py — scorer for docs/studies/late_gates_replay_prereg.json (amended, amended_2, ready).

Inputs: ~/replay_runs/late_gates/DAY-VAR.json from tools/studies/late_gates_run.py, and its fidelity.json.

  (default)  count step. Sessions, trades per variant, exclusions, refused / removed shares, MDE per half.
             No mean of any gate variant is printed.
  --canary   +10 bp on every G trade in a copy; lift_ref must move 10 +- 1 bp per gate. Writes canary.json.
  --score    the read. Refuses without the prereg's ready review_id (--skeptic-ready), canary_ok, a fidelity pass,
             and the gate's own fidelity 'testable'. Information lines print only after the verdict.

Per trade: closed trades use the replay's ret; open_at_end trades are marked at the SIP 1-minute close of 15:49
(amendment 16). Net = gross bp - full SIP spread at entry (replay_costing.entry_spread_bp); a trade with no NBBO takes
the session's pooled median over the variants read, and a session where those are >= 10% of its trades is excluded
(amendment 10). Repriced (amendment 4 / 16): exit = entry_px * (1 + ret), repriced ret = exit / SIP 1m close of the
entry minute - 1; a session where >= 10% of trades lack that bar is excluded and listed.

Trade classes vs REF (amendment 5): same = same symbol, entries within 120 s; delayed = same symbol, G entry 120 s to
30 min after an unmatched REF entry; removed = REF trade with neither; new = G trade with neither.

USAGE (mini): .venv/bin/python tools/studies/late_gates_read.py [--canary | --score --skeptic-ready ID]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import std_scorer as S  # noqa: E402

ET = ZoneInfo("America/New_York")
OUT = Path(os.path.expanduser("~/replay_runs/late_gates"))
PREREG = ROOT / "docs" / "studies" / "late_gates_replay_prereg.json"
WR_FIRST = "2026-10-12"
S5_EXCLUDED = ("2026-10-23", "2026-10-26")
T_BONF = 2.24
BAR_LIFT = 3.0
MDE_MAX = 6.0
CAP = 40
MIN_HALF = {"S5": 12, "WR": 12}
SEEDS = (1, 2, 3)
GATES = ("S5", "WR")


# ── per-trade pricing ──────────────────────────────────────────────────────────────────────────────────────────
class Pricer:
    """spread(sym, ts) -> bp | None, close(sym, day, minute_ts) -> px | None. Real ones fetch; tests inject."""

    def __init__(self, spread=None, close=None):
        self.spread = spread
        self.close = close

    @classmethod
    def live(cls):
        import bars
        import replay_costing as RC
        cache, box, closes = RC.load_cache(), {}, {}

        def spread(sym, ts):
            return RC.entry_spread_bp(cache, sym, ts, box)

        def close(sym, day, minute_ts):
            key = (sym, day)
            if key not in closes:
                st, cl = bars.fetch(sym, day, "sip")
                closes[key] = {int(t): c for t, c in zip(st or [], cl or [])}
            return closes[key].get(int(minute_ts))

        p = cls(spread, close)
        p.save = lambda: RC.save_cache(cache)
        return p

    def save(self):
        pass


def minute_of(ts: float) -> int:
    return int(float(ts) // 60) * 60


def close_1549(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=15, minute=49, tzinfo=ET).timestamp())


def trades(res: dict, day: str, pr: Pricer) -> list[dict]:
    """Closed + open_at_end trades with gross (bp), repriced gross (bp or None), symbol, entry_ts."""
    out = []
    for t in res.get("closed") or []:
        if t.get("ret") is None or not t.get("entry_px"):
            continue
        out.append({"symbol": t["symbol"], "entry_ts": float(t["entry_ts"]), "entry_px": float(t["entry_px"]),
                    "ret": float(t["ret"]), "open": False})
    for t in res.get("open_at_end") or []:
        px = t.get("entry_px")
        if not px:
            continue
        mark = pr.close(t["symbol"], day, close_1549(day))
        if not mark:
            continue
        out.append({"symbol": t["symbol"], "entry_ts": float(t["entry_ts"]), "entry_px": float(px),
                    "ret": float(mark) / float(px) - 1, "open": True})
    for t in out:
        t["gross"] = t["ret"] * 1e4
        c = pr.close(t["symbol"], day, minute_of(t["entry_ts"]))
        t["gross_rp"] = ((t["entry_px"] * (1 + t["ret"])) / c - 1) * 1e4 if c else None
    return out


def charge(sessions_trades: dict[str, list[dict]], pr: Pricer) -> dict:
    """Net per trade for every variant of one session. Returns {'excluded': why | None, 'no_nbbo': n, 'no_bar': n}."""
    allt = [t for ts in sessions_trades.values() for t in ts]
    sp = []
    for t in allt:
        t["spread"] = pr.spread(t["symbol"], t["entry_ts"])
        if t["spread"] is not None:
            sp.append(t["spread"])
    no_nbbo = sum(t["spread"] is None for t in allt)
    no_bar = sum(t["gross_rp"] is None for t in allt)
    why = None
    if allt and no_nbbo / len(allt) >= 0.10:
        why = f"no_nbbo {no_nbbo}/{len(allt)}"
    elif allt and no_bar / len(allt) >= 0.10:
        why = f"no_entry_bar {no_bar}/{len(allt)}"
    med = statistics.median(sp) if sp else 0.0
    for t in allt:
        s = t["spread"] if t["spread"] is not None else med
        t["net"] = t["gross"] - s
        t["net_rp"] = None if t["gross_rp"] is None else t["gross_rp"] - s
    return {"excluded": why, "no_nbbo": no_nbbo, "no_bar": no_bar}


def mean_of(ts: list[dict], key: str = "net") -> float | None:
    xs = [t[key] for t in ts if t.get(key) is not None]
    return statistics.fmean(xs) if xs else None


def classify(ref: list[dict], g: list[dict]) -> dict:
    """same / delayed / removed / new counts and the G trades in each class."""
    used_ref, cls = set(), {"same": [], "delayed": [], "new": []}
    for t in sorted(g, key=lambda x: x["entry_ts"]):
        best = None
        for j, r in enumerate(ref):
            if j in used_ref or r["symbol"] != t["symbol"]:
                continue
            d = t["entry_ts"] - r["entry_ts"]
            if abs(d) <= 120 and (best is None or abs(d) < abs(t["entry_ts"] - ref[best]["entry_ts"])):
                best = j
        if best is not None:
            used_ref.add(best)
            cls["same"].append(t)
            continue
        for j, r in enumerate(ref):
            if j in used_ref or r["symbol"] != t["symbol"]:
                continue
            if 120 < t["entry_ts"] - r["entry_ts"] <= 1800:
                used_ref.add(j)
                cls["delayed"].append({**t, "delay_min": (t["entry_ts"] - r["entry_ts"]) / 60})
                break
        else:
            cls["new"].append(t)
    removed = [r for j, r in enumerate(ref) if j not in used_ref]
    return {"same": len(cls["same"]), "delayed": len(cls["delayed"]), "new": len(cls["new"]), "removed": len(removed),
            "delays": [t["delay_min"] for t in cls["delayed"]], "classes": cls, "removed_trades": removed}


# ── loading ─────────────────────────────────────────────────────────────────────────────────────────────────────
def load(day: str, var: str, out_dir: Path) -> dict | None:
    p = out_dir / f"{day}-{var}.json"
    return json.loads(p.read_text()) if p.exists() else None


def gate_days(gate: str, out_dir: Path) -> list[str]:
    days = sorted({p.name[:10] for p in out_dir.glob("20??-??-??-REF.json")})
    if gate == "WR":
        return [d for d in days if d >= WR_FIRST]
    return [d for d in days if d not in S5_EXCLUDED]


def session(day: str, gate: str, out_dir: Path, pr: Pricer, canary_bp: float = 0.0) -> dict:
    """Everything one session contributes to one gate. canary_bp is added to every G trade (copy only)."""
    names = ["REF", gate, "MDE1"] + [f"{gate}_RAND_{k}" for k in SEEDS]
    raw = {v: load(day, v, out_dir) for v in names}
    row = {"day": day, "missing": [v for v in names if raw[v] is None]}
    if raw["REF"] is None or raw[gate] is None:
        row["excluded"] = "missing " + ",".join(row["missing"])
        return row
    tr = {v: trades(raw[v], day, pr) for v in names if raw[v] is not None}
    if canary_bp:
        for t in tr[gate]:
            t["ret"] += canary_bp / 1e4
            t["gross"] += canary_bp
            if t["gross_rp"] is not None:
                t["gross_rp"] += canary_bp
    ch = charge(tr, pr)
    row.update({"n": {v: len(x) for v, x in tr.items()}, "no_nbbo": ch["no_nbbo"], "no_bar": ch["no_bar"]})
    if ch["excluded"]:
        row["excluded"] = ch["excluded"]
        return row
    if len(tr["REF"]) < 5:                                  # amendment 6: keyed on REF (pre-treatment)
        row["excluded"] = f"ref_trades {len(tr['REF'])} < 5"
        return row
    ref, g = tr["REF"], tr[gate]
    row["identical"] = [(t["symbol"], t["entry_ts"]) for t in g] == [(t["symbol"], t["entry_ts"]) for t in ref]
    cl = classify(ref, g)
    row["classes"] = {k: cl[k] for k in ("same", "delayed", "removed", "new")}
    row["delays"] = cl["delays"]
    row["share_net"] = max(0.0, 1 - len(g) / len(ref))
    row["share_removed"] = cl["removed"] / len(ref)
    m_ref, m_g = mean_of(ref) or 0.0, mean_of(g)
    # amendment 6: an identical session is scored, not dropped. Same entries replay to the same exits, so its
    # natural lift is 0; computing it (not forcing 0) keeps the canary honest on those sessions.
    row["lift_ref"] = (m_g if m_g is not None else 0.0) - m_ref
    rp_ref, rp_g = mean_of(ref, "net_rp"), mean_of(g, "net_rp")
    row["lift_ref_rp"] = None if rp_ref is None or rp_g is None else rp_g - rp_ref
    seeds = [tr[f"{gate}_RAND_{k}"] for k in SEEDS if f"{gate}_RAND_{k}" in tr]
    if len(seeds) == len(SEEDS):
        n_rand = statistics.fmean(len(s) for s in seeds)
        m_rand = statistics.fmean((mean_of(s) or 0.0) for s in seeds)
        row["rand_count_ok"] = abs(n_rand - len(g)) <= 0.15 * max(len(g), 1)
        row["lift_rand"] = ((m_g or 0.0) - m_rand) if row["rand_count_ok"] else None
        row["ctl_rand"] = m_rand - m_ref                    # outcome-blind for G: MDE proxy
    if "MDE1" in tr:
        row["ctl_mde1"] = (mean_of(tr["MDE1"]) or 0.0) - m_ref
    row["m"] = {"REF": m_ref, gate: m_g}
    row["pre11_lift"] = _pre11_lift(ref, g, day)
    row["names"] = {}                                       # per-symbol net contribution, G minus REF (ranking only)
    for sign, ts in ((1, g), (-1, ref)):
        for t in ts:
            row["names"][t["symbol"]] = row["names"].get(t["symbol"], 0.0) + sign * t["net"]
    row["_ref"], row["_g"] = ref, g
    return row


def _pre11_lift(ref, g, day):
    cut = datetime.strptime(day, "%Y-%m-%d").replace(hour=11, tzinfo=ET).timestamp()
    a, b = [t for t in ref if t["entry_ts"] < cut], [t for t in g if t["entry_ts"] < cut]
    ma, mb = mean_of(a), mean_of(b)
    return None if ma is None or mb is None else mb - ma


def halves(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    n = len(rows)
    k = math.ceil(n / 2)
    return rows[:k], rows[k:]


def mde(rows: list[dict]) -> float | None:
    """amendment 15: max of the seed-averaged control proxy and the fixed 0.25-share proxy."""
    vals = []
    for key in ("ctl_rand", "ctl_mde1"):
        xs = [r[key] for r in rows if r.get(key) is not None]
        if len(xs) >= 3:
            vals.append(2.8 * statistics.stdev(xs) / math.sqrt(len(rows)))
    return max(vals) if vals else None


def gate_rows(gate: str, out_dir: Path, pr: Pricer, canary_bp: float = 0.0):
    rows, excl = [], []
    for d in gate_days(gate, out_dir)[:CAP]:
        r = session(d, gate, out_dir, pr, canary_bp)
        (excl if r.get("excluded") else rows).append(r)
    return rows, excl


# ── modes ───────────────────────────────────────────────────────────────────────────────────────────────────────
def count(out_dir: Path, pr: Pricer) -> dict:
    res = {}
    for gate in GATES:
        rows, excl = gate_rows(gate, out_dir, pr)
        a, b = halves(rows)
        n_ref = sum(r["n"]["REF"] for r in rows)
        n_g = sum(r["n"][gate] for r in rows)
        res[gate] = {
            "scored_sessions": len(rows), "half_sessions": [len(a), len(b)],
            "excluded": [(r["day"], r["excluded"]) for r in excl],
            "trades": {"REF": n_ref, gate: n_g},
            "identical_sessions": sum(bool(r.get("identical")) for r in rows),
            "share_net_pooled": (1 - n_g / n_ref) if n_ref else None,
            "share_removed_pooled": (sum(r["classes"]["removed"] for r in rows) / n_ref) if n_ref else None,
            "classes": {k: sum(r["classes"][k] for r in rows) for k in ("same", "delayed", "removed", "new")},
            "rand_count_bad": [r["day"] for r in rows if r.get("rand_count_ok") is False],
            "mde_half": [mde(a), mde(b)],
            "no_nbbo": sum(r.get("no_nbbo", 0) for r in rows), "no_entry_bar": sum(r.get("no_bar", 0) for r in rows),
        }
        h = res[gate]
        h["power_ok"] = (all(m is not None and m <= MDE_MAX for m in h["mde_half"])
                         and min(h["half_sessions"]) >= MIN_HALF[gate])
    return res


def canary(out_dir: Path, pr: Pricer) -> dict:
    out = {}
    for gate in GATES:
        base, _ = gate_rows(gate, out_dir, pr)
        bump, _ = gate_rows(gate, out_dir, pr, canary_bp=10.0)
        b0 = S.day_stat([r["lift_ref"] for r in base])["mean"]
        b1 = S.day_stat([r["lift_ref"] for r in bump])["mean"]
        delta = b1 - b0 if all(map(math.isfinite, (b0, b1))) else float("nan")
        out[gate] = {"delta": delta, "ok": bool(math.isfinite(delta) and abs(delta - 10.0) <= 1.0)}
    out["canary_ok"] = all(v["ok"] for v in out.values())
    return out


def verdict(gate: str, rows: list[dict], cnt: dict, dup_pen: float) -> dict:
    a, b = halves(rows)
    st = S.day_stat([r["lift_ref"] for r in rows])
    st_rp = S.day_stat([r["lift_ref_rp"] for r in rows])
    ha, hb = (S.day_stat([r["lift_ref"] for r in h])["mean"] for h in (a, b))
    hra, hrb = (S.day_stat([r["lift_ref_rp"] for r in h])["mean"] for h in (a, b))
    rand = S.day_stat([r["lift_rand"] for r in rows if r.get("lift_rand") is not None])
    uninformative = (cnt["share_net_pooled"] is not None and cnt["share_net_pooled"] < 0.05
                     and cnt["share_removed_pooled"] is not None and cnt["share_removed_pooled"] < 0.05)
    bar = BAR_LIFT + dup_pen
    checks = {
        "lift_ref": st["mean"] >= bar and st["t"] >= T_BONF,
        "both_halves": ha > 0 and hb > 0,
        "lift_rand": True if uninformative else (rand["mean"] > 0 and rand["t"] >= T_BONF),
        "trades": cnt["trades"]["REF"] >= 300 and cnt["trades"][gate] >= 150,
        "repriced": st_rp["mean"] >= bar and st_rp["t"] >= T_BONF and hra > 0 and hrb > 0,
    }
    if not cnt["power_ok"]:
        v = "FAIL (UNDERPOWERED at the cap)" if len(rows) >= CAP else "UNDERPOWERED (not scored)"
    elif ha <= 0 or hb <= 0:
        v = "FAIL"
    else:
        v = "PASS" if all(checks.values()) else "FAIL"
    m_g = statistics.fmean(r["m"][gate] for r in rows if r["m"].get(gate) is not None) if rows else float("nan")
    return {"verdict": v, "checks": checks, "rand_uninformative": uninformative, "bar_bp": bar,
            "lift_ref": st, "lift_ref_halves": [ha, hb], "lift_ref_repriced": st_rp, "repriced_halves": [hra, hrb],
            "lift_rand": rand, "gate_mean_net": m_g,
            "reading": ("profitable" if m_g > 0 else "loss reducer only") if v == "PASS" else None}


def dup_penalty(out_dir: Path, pr: Pricer) -> tuple[float, list]:
    diffs = []
    for p in sorted(out_dir.glob("20??-??-??-REF_DUP.json")):
        day = p.name[:10]
        tr = {"REF": trades(load(day, "REF", out_dir), day, pr), "DUP": trades(load(day, "REF_DUP", out_dir), day, pr)}
        charge(tr, pr)
        diffs.append((day, (mean_of(tr["DUP"]) or 0.0) - (mean_of(tr["REF"]) or 0.0)))
    worst = max((abs(d) for _, d in diffs), default=0.0)
    return (worst if worst > 1.0 else 0.0), diffs


def information(out_dir: Path, pr: Pricer, gate: str, rows: list[dict]) -> dict:
    info = {}
    by_day = sorted(rows, key=lambda r: r["lift_ref"], reverse=True)
    info["drop_top3_days"] = S.day_stat([r["lift_ref"] for r in by_day[3:]])["mean"]
    tot = {}
    for r in rows:
        for s, v in r["names"].items():
            tot[s] = tot.get(s, 0.0) + v
    top5 = {s for s, _ in sorted(tot.items(), key=lambda x: x[1], reverse=True)[:5]}
    info["drop_top5_names_symbols"] = sorted(top5)
    lifts = []
    for r in rows:
        a_ = [t for t in r["_ref"] if t["symbol"] not in top5]
        b_ = [t for t in r["_g"] if t["symbol"] not in top5]
        ma, mb = mean_of(a_), mean_of(b_)
        if ma is not None and mb is not None:
            lifts.append(mb - ma)
    info["drop_top5_names"] = S.day_stat(lifts)["mean"]
    info["pre11_lift"] = S.day_stat([r["pre11_lift"] for r in rows if r["pre11_lift"] is not None])
    delays = [d for r in rows for d in r["delays"]]
    info["median_delay_min"] = statistics.median(delays) if delays else None
    info["abs_mean_net"] = {v: statistics.fmean(r["m"][v] for r in rows if r["m"].get(v) is not None)
                            for v in ("REF", gate)} if rows else {}
    for var in ({"S5": ("S5m2", "S5p2", "S5_L1"), "WR": ("WR10", "WR20", "WR_L1")}[gate] + ("BOTH", "REF_L1")):
        lifts = []
        for r in rows:
            d = r["day"]
            base_var = "REF_L1" if var.endswith("_L1") else "REF"
            if var == "REF_L1":
                base_var = "REF"
            x, y = load(d, var, out_dir), load(d, base_var, out_dir)
            if x is None or y is None:
                continue
            tr = {"X": trades(x, d, pr), "Y": trades(y, d, pr)}
            if var.endswith("_L1") and len(tr["X"]) < 3:
                continue
            charge(tr, pr)
            mx, my = mean_of(tr["X"]), mean_of(tr["Y"])
            if mx is not None and my is not None:
                lifts.append(mx - my)
        info[f"{var}_vs_{'REF_L1' if var.endswith('_L1') and var != 'REF_L1' else 'REF'}"] = S.day_stat(lifts)
    return info


def ready_id() -> str:
    d = json.loads(PREREG.read_text())
    return str((d.get("ready") or {}).get("review_id") or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(OUT))
    ap.add_argument("--canary", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    a = ap.parse_args()
    out_dir = Path(a.dir)
    pr = Pricer.live()
    try:
        if a.canary:
            res = canary(out_dir, pr)
            (out_dir / "canary.json").write_text(json.dumps(res, indent=1))
            print(json.dumps(res, indent=1))
            return 0
        cnt = count(out_dir, pr)
        (out_dir / "count.json").write_text(json.dumps(cnt, indent=1, default=str))
        if not a.score:
            print(json.dumps(cnt, indent=1, default=str))
            return 0
        rid = ready_id()
        if not rid or a.skeptic_ready != rid:
            print(f"refusing: --skeptic-ready must equal the prereg ready review_id ({rid!r})", file=sys.stderr)
            return 2
        can = json.loads((out_dir / "canary.json").read_text()) if (out_dir / "canary.json").exists() else {}
        fid = json.loads((out_dir / "fidelity.json").read_text()) if (out_dir / "fidelity.json").exists() else {}
        if not can.get("canary_ok") or not fid.get("pass"):
            print("refusing: canary_ok and a fidelity pass are required", file=sys.stderr)
            return 2
        pen, diffs = dup_penalty(out_dir, pr)
        result = {"dup_diffs": diffs, "dup_penalty_bp": pen, "gates": {}}
        for gate in GATES:
            if not (fid.get("gates", {}).get(gate) or {}).get("testable"):
                result["gates"][gate] = {"verdict": "NOT TESTABLE IN REPLAY", "fidelity": fid["gates"].get(gate)}
                continue
            rows, _ = gate_rows(gate, out_dir, pr)
            v = verdict(gate, rows, cnt[gate], pen)
            if v["verdict"] in ("PASS", "FAIL"):
                v["information"] = information(out_dir, pr, gate, rows)
            result["gates"][gate] = v
        (out_dir / "result.json").write_text(json.dumps(result, indent=1, default=str))
        print(json.dumps(result, indent=1, default=str))
        return 0
    finally:
        pr.save()


if __name__ == "__main__":
    sys.exit(main())
