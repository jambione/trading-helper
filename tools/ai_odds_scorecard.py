#!/usr/bin/env python3
"""ai_odds_scorecard - are the AI pickers' odds worth anything? (operator 11/19). READ-ONLY, nightly after the warehouse.

Each seed_rank pick (ai_reports/seed_rank_raw/<slot>_{g,x}.json, from 11/19 with "p_good") is scored against the tape:
GOOD = the name traded +0.5% before -0.5% within 30 minutes of the next regular-session minute after the ranking
(SIP warehouse 1-minute bars; a minute touching both counts as down first; neither = not good). Reports per model:
hit rate, mean p_good, Brier score vs a constant base-rate forecaster, a calibration table, and the desk's broker
result on the names it actually traded. Writes ai_reports/ai_odds/scorecard.md (+ rows.jsonl).

    .venv/bin/python tools/ai_odds_scorecard.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

ET = ZoneInfo("America/New_York")
RAW = ROOT / "ai_reports" / "seed_rank_raw"
OUT = ROOT / "ai_reports" / "ai_odds"
UP, DN, WIN = 0.005, 0.005, 30
BUCKETS = ((0.0, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 1.01))
MODEL = {"g": "agy", "x": "grok"}


def start_minute(slot: str) -> int:
    """'2026-11-19T09:25' -> index of the next regular-session minute (09:30 = 0). Pure."""
    hh, mm = (int(x) for x in slot[11:16].split(":"))
    return max(0, hh * 60 + mm - 570 + 1) if hh * 60 + mm >= 570 else 1


def outcome(o, h, l, m0: int, up=UP, dn=DN, win=WIN):
    """1 if +up before -dn within win minutes from the open of m0, 0 otherwise, None without data. Pure."""
    if m0 >= len(o) or not np.isfinite(o[m0]) or o[m0] <= 0:
        return None
    e = o[m0]
    for m in range(m0, min(m0 + win, len(o))):
        if np.isfinite(l[m]) and l[m] <= e * (1 - dn):
            return 0
        if np.isfinite(h[m]) and h[m] >= e * (1 + up):
            return 1
    return 0


def brier(ps, ys):
    return float(np.mean([(p - y) ** 2 for p, y in zip(ps, ys)])) if ps else None


def model_stats(rows):
    """rows: [{p, y}] -> hit, mean p, Brier, base-rate Brier, skill, calibration buckets. Pure."""
    rows = [r for r in rows if r.get("p") is not None and r.get("y") is not None]
    if not rows:
        return {"n": 0}
    ps, ys = [r["p"] for r in rows], [r["y"] for r in rows]
    base = float(np.mean(ys))
    b, b0 = brier(ps, ys), brier([base] * len(ys), ys)
    cal = []
    for lo, hi in BUCKETS:
        sel = [r for r in rows if lo <= r["p"] < hi]
        if sel:
            cal.append({"bucket": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": len(sel),
                        "mean_p": round(float(np.mean([r["p"] for r in sel])), 3),
                        "hit": round(float(np.mean([r["y"] for r in sel])), 3)})
    return {"n": len(rows), "hit": round(base, 3), "mean_p": round(float(np.mean(ps)), 3), "brier": round(b, 4),
            "brier_base": round(b0, 4), "skill": round(1 - b / b0, 3) if b0 else None, "calibration": cal}


def collect():
    from relative_strength_scan import load_day
    from evening_family import WH_SIP
    days = {p.stem for p in WH_SIP.glob("20??-??-??.npz")}
    cache, rows = {}, []
    for f in sorted(RAW.glob("*_[gx].json")):
        try:
            j = json.loads(f.read_text())
        except ValueError:
            continue
        slot, tag = str(j.get("slot") or ""), f.stem[-1]
        day = slot[:10]
        sug = [s for s in (j.get("suggestions") or []) if s.get("p_good") is not None]
        if not sug or day not in days:
            continue
        if day not in cache:
            cache.clear()
            cache[day] = load_day(WH_SIP / f"{day}.npz")
        d = cache[day]
        m0 = start_minute(slot)
        for rank, s in enumerate(sug, 1):
            i = d["idx"].get(s["symbol"])
            y = None if i is None else outcome(d["o"][i], d["h"][i], d["l"][i], m0)
            rows.append({"day": day, "slot": slot, "model": MODEL.get(tag, tag), "symbol": s["symbol"], "rank": rank,
                         "p": s["p_good"], "conf": s.get("confidence"), "y": y})
    return rows


def desk_results(rows):
    """Broker round trips on picked names after the pick, per model (information)."""
    try:
        import flow_exit as fx
        days = sorted({r["day"] for r in rows})
        if not days:
            return {}
        fx.LO, fx.HI = days[0], days[-1]
        trips = fx.load_trades()
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}
    out = defaultdict(list)
    for r in rows:
        t0 = datetime.fromisoformat(r["slot"]).replace(tzinfo=ET).timestamp()
        for t in trips:
            if t["day"] == r["day"] and t["symbol"] == r["symbol"] and t["t_in"] >= t0:
                out[r["model"]].append(t["ret_bp"])
    return {m: {"trips": len(v), "mean_bp": round(float(np.mean(v)), 2), "win_pct": round(100 * float(np.mean(np.array(v) > 0)), 1)}
            for m, v in out.items() if v}


def render(stats, desk, n_days, missing):
    L = ["# AI odds scorecard", "", f"- As of {datetime.now(ET):%Y-%m-%d %H:%M} ET; {n_days} days with odds; picks without bars: {missing}",
         "- GOOD = +0.5% before -0.5% within 30 min of the next regular-session minute after the ranking",
         "- skill = 1 - Brier / Brier(base rate); > 0 means the odds beat always guessing the average", ""]
    for m, s in stats.items():
        L.append(f"## {m}")
        if not s.get("n"):
            L += ["- no scored picks yet", ""]
            continue
        L.append(f"- picks {s['n']}, hit {s['hit']}, mean p_good {s['mean_p']}, Brier {s['brier']} vs base {s['brier_base']}, **skill {s['skill']}**")
        L.append("- calibration (bucket: n, mean p, actual hit): " +
                 "; ".join(f"{c['bucket']}: {c['n']}, {c['mean_p']}, {c['hit']}" for c in s["calibration"]))
        if m in desk:
            d = desk[m]
            L.append(f"- desk trades on these names after the pick: {d['trips']} trips, {d['mean_bp']} bp, win {d['win_pct']}%")
        L.append("")
    return "\n".join(L) + "\n"


def main() -> int:
    rows = collect()
    scored = [r for r in rows if r["y"] is not None]
    stats = {m: model_stats([r for r in scored if r["model"] == m]) for m in ("agy", "grok")}
    stats["both"] = model_stats(scored)
    md = render(stats, desk_results(scored), len({r["day"] for r in rows}), sum(r["y"] is None for r in rows))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "scorecard.md").write_text(md)
    with open(OUT / "rows.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
