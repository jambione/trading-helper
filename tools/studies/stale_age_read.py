#!/usr/bin/env python3
"""stale_age_read.py — scorer for docs/studies/stale_age_replay_prereg.json (amended, amended_2, ready).

Inputs: ~/replay_runs/stale_age/DAY-VAR.json (tools/studies/stale_age_run.py), its fidelity.json and the
DAY-RREL_share.json calibration records.

Per trade (amended_2 #3): entry = the first SIP trade after the replayed decision (entry_ts) within 60 s, fetched with
the data-only key (secrets_data.json) and cached; none = 'no print' (counted; a session with > 10% such trades is
excluded); a session whose fetch failed is not scored. Replay exit price = recorded entry x (1 + replay ret);
gross = exit / SIP entry - 1. open_at_end is marked at the SIP 15:49 close. Net = gross - full SIP spread at entry
(replay_costing.entry_spread_bp; late_gates_read.Pricer).

Statistics: lift_ref = mean net(AGE30) - mean net(REF); lift_rand = mean net(AGE30) - mean over seeds of mean net(RREL_k)
(a session whose calibration is UNMATCHED has no lift_rand); TIMING = signed median of AGE30 - REF entry seconds over
one-to-one matched pairs (same symbol, nearest within +-600 s, greedy by distance); MOVED SHARE = REF trades whose
match differs by >= 30 s or have none, over REF trades. Halves: A = sessions <= 2026-10-27, B = sessions >= 2026-10-28.
  (default) count  sessions, trades, no-print and failed counts, moved share, MDE per half; no means.
  --canary         +10 bp on every AGE30 trade in a copy; lift_ref must move 10 +- 1 bp in each half.
  --score          the verdict; refuses unless the prereg's 'ready' verdict is READY, canary_ok and fidelity pass.
USAGE (mini, after the close): .venv/bin/python tools/studies/stale_age_read.py [--canary | --score]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import late_gates_read as LR  # noqa: E402
import std_scorer as S  # noqa: E402

OUT = Path(os.path.expanduser("~/replay_runs/stale_age"))
PREREG = ROOT / "docs" / "studies" / "stale_age_replay_prereg.json"
SPLIT = "2026-10-27"
MIN_B = 10
WINDOW = 60.0
MATCH_SEC, MOVE_SEC = 600.0, 30.0
SEEDS = (1, 2, 3)
T_PASS, BAR = 2.0, 3.0
T975 = {9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110,
        18: 2.101, 19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 25: 2.060, 30: 2.045, 40: 2.023}


def t975(n_days: int) -> float:
    df = max(1, n_days - 1)
    keys = sorted(T975)
    for k in keys:
        if df <= k:
            return T975[k]
    return 1.96


# ── SIP entry prices ─────────────────────────────────────────────────────────────────────────────────────────────
class SipEntry:
    """first SIP trade after t within WINDOW; cached in OUT/sip_entry_cache.json. fetch(sym, t0, t1) -> [(ts, px)]."""

    def __init__(self, fetch=None, cache_path: Path | None = None):
        self.fetch = fetch
        self.path = cache_path
        self.cache = json.loads(cache_path.read_text()) if cache_path and cache_path.exists() else {}
        self.failed: set = set()

    @classmethod
    def live(cls):
        import requests
        import replay_session as R
        s = json.load(open(ROOT / "secrets_data.json"))
        hdr = {"APCA-API-KEY-ID": s["alpaca_data_api_key"], "APCA-API-SECRET-KEY": s["alpaca_data_secret_key"]}

        def get(url, params):
            r = requests.get(url, headers=hdr, params=params, timeout=30)
            if r.status_code == 429:
                raise RuntimeError("429")
            r.raise_for_status()
            return r.json()
        return cls(lambda sym, t0, t1: R.fetch_sip_trades(sym, t0, t1, get), OUT / "sip_entry_cache.json")

    def price(self, day: str, sym: str, t: float):
        key = f"{sym}|{t:.3f}"
        if key not in self.cache:
            try:
                prints = self.fetch(sym, t, t + WINDOW) if self.fetch else []
            except Exception:
                self.failed.add(day)
                return None
            first = next((p for ts, p in sorted(prints) if ts > t and p > 0), None)
            self.cache[key] = first
        return self.cache[key]

    def save(self):
        if self.path:
            self.path.write_text(json.dumps(self.cache))


# ── per-trade pricing ────────────────────────────────────────────────────────────────────────────────────────────
def price_trades(res: dict, day: str, pr: LR.Pricer, sip: SipEntry) -> tuple[list[dict], int]:
    """Trades with gross bp on the SIP entry; returns (trades, no_print_count)."""
    out, no_print = [], 0
    for t, is_open in [(x, False) for x in res.get("closed") or []] + [(x, True) for x in res.get("open_at_end") or []]:
        px = t.get("entry_px")
        if not px:
            continue
        if is_open:
            mark = pr.close(t["symbol"], day, LR.close_1549(day))
            if not mark:
                continue
            exit_px = float(mark)
        else:
            if t.get("ret") is None:
                continue
            exit_px = float(px) * (1 + float(t["ret"]))
        e = sip.price(day, t["symbol"], float(t["entry_ts"]))
        if not e:
            no_print += 1
            continue
        g = (exit_px / float(e) - 1.0) * 1e4
        sp = pr.spread(t["symbol"], float(t["entry_ts"]))
        out.append({"symbol": t["symbol"], "entry_ts": float(t["entry_ts"]), "gross": g, "spread": sp})
    sps = [x["spread"] for x in out if x["spread"] is not None]
    med = statistics.median(sps) if sps else 0.0
    for x in out:
        x["net"] = x["gross"] - (x["spread"] if x["spread"] is not None else med)
    return out, no_print


def match_pairs(ref: list[dict], g: list[dict]) -> list[tuple[int, int, float]]:
    """One-to-one (ref index, g index, g_ts - ref_ts) pairs: same symbol, |dt| <= MATCH_SEC, greedy by |dt|."""
    cand = sorted(((abs(b["entry_ts"] - a["entry_ts"]), i, j, b["entry_ts"] - a["entry_ts"])
                   for i, a in enumerate(ref) for j, b in enumerate(g)
                   if a["symbol"] == b["symbol"] and abs(b["entry_ts"] - a["entry_ts"]) <= MATCH_SEC))
    ui, uj, pairs = set(), set(), []
    for _, i, j, dt in cand:
        if i in ui or j in uj:
            continue
        ui.add(i)
        uj.add(j)
        pairs.append((i, j, dt))
    return pairs


def mean_net(ts: list[dict]):
    return statistics.fmean(x["net"] for x in ts) if ts else None


def session(day: str, out_dir: Path, pr, sip, canary_bp: float = 0.0) -> dict:
    names = ["REF", "AGE30", "MDE1"] + [f"RREL_{k}" for k in SEEDS]
    row = {"day": day}
    tr, np_ = {}, {}
    for v in names:
        res = LR.load(day, v, out_dir)
        if res is None:
            continue
        tr[v], np_[v] = price_trades(res, day, pr, sip)
    if "REF" not in tr or "AGE30" not in tr:
        row["excluded"] = "missing REF/AGE30"
        return row
    if day in sip.failed:
        row["excluded"] = "SIP fetch failed"
        return row
    tot = sum(len(x) + np_[v] for v, x in tr.items())
    if tot and sum(np_.values()) / tot > 0.10:
        row["excluded"] = f"no SIP print {sum(np_.values())}/{tot}"
        return row
    if len(tr["REF"]) < 5:
        row["excluded"] = f"ref_trades {len(tr['REF'])} < 5"
        return row
    if canary_bp:
        for x in tr["AGE30"]:
            x["net"] += canary_bp
    ref, g = tr["REF"], tr["AGE30"]
    pairs = match_pairs(ref, g)
    moved_ref = len(ref) - sum(1 for _, _, dt in pairs if abs(dt) < MOVE_SEC)
    row.update({"n": {v: len(x) for v, x in tr.items()}, "no_print": np_,
                "lift_ref": mean_net(g) - mean_net(ref),
                "timing": [dt for _, _, dt in pairs if abs(dt) >= MOVE_SEC],
                "moved_share": moved_ref / len(ref)})
    same_i = {i for i, _, dt in pairs if abs(dt) < MOVE_SEC}
    same_j = {j for _, j, dt in pairs if abs(dt) < MOVE_SEC}
    mr = [x for i, x in enumerate(ref) if i not in same_i]
    mg = [x for j, x in enumerate(g) if j not in same_j]
    row["lift_moved"] = (mean_net(mg) - mean_net(mr)) if mr and mg else None
    seeds = [tr[f"RREL_{k}"] for k in SEEDS if f"RREL_{k}" in tr]
    cal = out_dir / f"{day}-RREL_share.json"
    unmatched = (not cal.exists()) or json.loads(cal.read_text()).get("unmatched")
    if len(seeds) == len(SEEDS) and not unmatched:
        row["lift_rand"] = mean_net(g) - statistics.fmean(mean_net(s) or 0.0 for s in seeds)
        row["ctl_rand"] = statistics.fmean(mean_net(s) or 0.0 for s in seeds) - mean_net(ref)
    if "MDE1" in tr and tr["MDE1"]:
        row["ctl_mde1"] = mean_net(tr["MDE1"]) - mean_net(ref)
    return row


def rows_for(out_dir: Path, pr, sip, canary_bp: float = 0.0):
    days = sorted({p.name[:10] for p in out_dir.glob("20??-??-??-REF.json")})
    rows = [session(d, out_dir, pr, sip, canary_bp) for d in days]
    return [r for r in rows if not r.get("excluded")], [r for r in rows if r.get("excluded")]


def split(rows):
    return [r for r in rows if r["day"] <= SPLIT], [r for r in rows if r["day"] > SPLIT]


def lower95(vals) -> float | None:
    st = S.day_stat([v for v in vals if v is not None])
    if not st["n"] or not math.isfinite(st.get("se", float("nan"))):
        return None
    return st["mean"] - t975(st["n"]) * st["se"]


def count(rows, excl) -> dict:
    a, b = split(rows)
    return {"sessions": [len(a), len(b)], "excluded": [(r["day"], r["excluded"]) for r in excl],
            "trades": {k: sum(r["n"].get(k, 0) for r in rows) for k in ("REF", "AGE30")},
            "moved_share_pooled": statistics.fmean(r["moved_share"] for r in rows) if rows else None,
            "mde_half": [LR.mde(a), LR.mde(b)],
            "read_allowed": len(b) >= MIN_B}


def verdict(rows, cnt) -> dict:
    a, b = split(rows)
    lr = S.day_stat([r["lift_ref"] for r in rows])
    ha, hb = (S.day_stat([r["lift_ref"] for r in h])["mean"] for h in (a, b))
    lrand_rows = [r for r in rows if r.get("lift_rand") is not None]
    lrand = S.day_stat([r["lift_rand"] for r in lrand_rows])
    timing_all = [dt for r in rows for dt in r["timing"]]
    timing = statistics.median(timing_all) / 60 if timing_all else None
    moved = cnt["moved_share_pooled"]
    unmatched_many = len(lrand_rows) < (2 / 3) * len(rows)
    power_ok = all(m is not None and m <= 6.0 for m in cnt["mde_half"]) and min(cnt["sessions"]) >= MIN_B
    checks = {
        "lift_ref": lr["mean"] >= BAR and lr["t"] >= T_PASS,
        "both_halves": ha > 0 and hb > 0,
        "lift_rand": (not unmatched_many) and lrand["mean"] > 0 and lrand["t"] >= T_PASS,
        "timing": timing is not None and timing <= -1.0,
        "trades": cnt["trades"]["REF"] >= 300 and cnt["trades"]["AGE30"] >= 300,
    }
    if not power_ok:
        v = "UNDERPOWERED (not scored)"
    elif unmatched_many:
        v = "CONTROL UNMATCHED (UNPROVEN)"
    elif all(checks.values()):
        v = "PASS"
    else:
        lb_a, lb_b = lower95([r["lift_ref"] for r in a]), lower95([r["lift_ref"] for r in b])
        moved_lift = S.day_stat([r["lift_moved"] for r in rows if r.get("lift_moved") is not None])["mean"]
        fnw = (moved is not None and moved >= 0.10 and timing is not None and timing <= -1.0
               and lb_a is not None and lb_a >= -3.0 and lb_b is not None and lb_b >= -3.0
               and moved_lift is not None and moved_lift >= -5.0
               and (not unmatched_many) and lrand["mean"] >= 0)
        v = "FASTER, NOT WORSE" if fnw else "FAIL"
    return {"verdict": v, "checks": checks, "lift_ref": lr, "halves": [ha, hb], "lift_rand": lrand,
            "timing_min": timing, "moved_share": moved}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--canary", action="store_true")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    pr, sip = LR.Pricer.live(), SipEntry.live()
    try:
        if a.canary:
            base, _ = rows_for(OUT, pr, sip)
            bump, _ = rows_for(OUT, pr, sip, canary_bp=10.0)
            res = {}
            for lab, x, y in zip(("A", "B"), split(base), split(bump)):
                d = S.day_stat([r["lift_ref"] for r in y])["mean"] - S.day_stat([r["lift_ref"] for r in x])["mean"]
                res[lab] = d
            res["canary_ok"] = all(isinstance(v, float) and abs(v - 10.0) <= 1.0 for v in (res["A"], res["B"]))
            (OUT / "canary.json").write_text(json.dumps(res, indent=1))
            print(json.dumps(res, indent=1))
            return 0
        rows, excl = rows_for(OUT, pr, sip)
        cnt = count(rows, excl)
        (OUT / "count.json").write_text(json.dumps(cnt, indent=1, default=str))
        if not a.score:
            print(json.dumps(cnt, indent=1, default=str))
            return 0
        d = json.loads(PREREG.read_text())
        can = json.loads((OUT / "canary.json").read_text()) if (OUT / "canary.json").exists() else {}
        fid = json.loads((OUT / "fidelity.json").read_text()) if (OUT / "fidelity.json").exists() else {}
        if (d.get("ready") or {}).get("verdict") != "READY" or not can.get("canary_ok") or not fid.get("pass"):
            print("refusing: needs a READY prereg, canary_ok and a fidelity pass", file=sys.stderr)
            return 2
        if not cnt["read_allowed"]:
            print(f"refusing: half B has {cnt['sessions'][1]} sessions, needs {MIN_B}", file=sys.stderr)
            return 2
        res = verdict(rows, cnt)
        (OUT / "result.json").write_text(json.dumps(res, indent=1, default=str))
        print(json.dumps(res, indent=1, default=str))
        return 0
    finally:
        pr.save()
        sip.save()


if __name__ == "__main__":
    sys.exit(main())
