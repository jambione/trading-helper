#!/usr/bin/env python3
"""marketable_limit_hist.py — docs/studies/marketable_limit_hist_prereg.json (amended, amended_2, ready).

Live paper day trades 2026-09-11..10-29 (move_to_cost_live.clean_rows' filters with its period gate widened),
G2 non-ask arms dropped, fill-matched to Alpaca FILL activities, matched buy average >= $20. ref = features.ask (the
current_ask passed to place_scaled_entry; a last print when the stream served it). slip = buy avg / ref - 1;
blocked at k = 5 bp when slip > k. Per session, session_sum = sum over blocked of (alt - net), alt = 0 (no trade);
under the re-arm model (amended_2 R2-1) alt = net of a re-entry at the first IEX 1m bar close at or after
send + 60 s x (1 + (fill - bid) / (2 fill)) held to the actual exit (no trade if the exit came first).
Primary set: sessions <= 10-26 (P1 09-11..09-30, P2 10-01..10-26); 10-27..10-29 reported with the with-set.
USAGE (mini, after the close): .venv/bin/python tools/studies/marketable_limit_hist.py
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import random
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools" / "studies"), str(ROOT / "tools"), str(ROOT)]
import move_to_cost_holdout as MH  # noqa: E402
import move_to_cost_live as L  # noqa: E402
import std_scorer as S  # noqa: E402

OUT = Path(os.path.expanduser("~/replay_runs/marketable_limit"))
FIRST, LAST, PRIMARY_LAST = "2026-09-11", "2026-10-29", "2026-10-26"
P1, P2 = ("2026-09-11", "2026-09-30"), ("2026-10-01", "2026-10-26")
K, KS_INFO, FLOOR = 5.0, (0.0, 10.0, 20.0), 20.0
MIN_BLOCKED, MIN_SESS, T_SCREEN, RAND_SHARE = 40, 15, 1.0, 0.90


def period(day: str) -> str | None:
    return "P1" if P1[0] <= day <= P1[1] else ("P2" if P2[0] <= day <= P2[1] else None)


# ── rows ────────────────────────────────────────────────────────────────────────────────────────────────────────────
def rows_from(raw: list[dict]) -> tuple[list[dict], dict]:
    """clean_rows with the period gate replaced by FIRST..LAST, features / entry_test re-attached, G2 non-ask out."""
    old = L.P1, L.P2
    L.P1, L.P2 = (FIRST, LAST), ("9999-01-01", "9999-01-01")
    try:
        keep, drops = L.clean_rows(raw)
    finally:
        L.P1, L.P2 = old
    by = {}
    for r in raw:
        try:
            by[(str(r.get("symbol")), round(float(r.get("entry_time")), 3))] = r
        except (TypeError, ValueError):
            continue
    out = []
    for r in keep:
        src = by.get((r["symbol"], round(r["entry_time"], 3))) or {}
        et = src.get("entry_test")
        arm = (et or {}).get("arm") if isinstance(et, dict) else et
        if et and arm != "ask":
            drops["g2_non_ask_arm"] = drops.get("g2_non_ask_arm", 0) + 1
            continue
        f = src.get("features") or {}
        try:
            ref, bid = float(f.get("ask") or 0), float(f.get("bid") or 0)
        except (TypeError, ValueError):
            ref, bid = 0.0, 0.0
        if ref <= 0:
            drops["no_features_ask"] = drops.get("no_features_ask", 0) + 1
            continue
        out.append(dict(r, ref=ref, bid=bid if bid > 0 else None))
    return out, drops


def match_with_time(row: dict, fills: list[dict]):
    """L.match_fills plus the first matched buy time (the send proxy)."""
    m = L.match_fills(row, fills)
    if m is None:
        return None
    buys = [f["t"] for f in fills if f["symbol"] == row["symbol"] and f["side"] == "buy"
            and row["entry_time"] - 5 <= f["t"] <= row["entry_time"] + 120]
    return m[0], m[1], min(buys)


def score_row(r: dict, k: float = K) -> dict:
    r = dict(r)
    r["net"] = (r["sell"] / r["buy"] - 1) * 1e4
    r["slip"] = (r["buy"] / r["ref"] - 1) * 1e4
    r["selection"] = (r["sell"] / r["ref"] - 1) * 1e4
    r["spread_proxy"] = (r["ref"] - r["bid"]) if r.get("bid") else None
    r["blocked"] = r["slip"] > k
    return r


def reentry_net(r: dict, bars: list) -> float | None:
    """amended_2 R2-1: re-entry at the first IEX 1m bar close at or after send + 60 s; None = no trade."""
    t_re = r["send"] + 60
    if r["exit_time"] <= t_re or not bars:
        return None
    day0 = _dt.datetime.fromisoformat(f"{r['day']} 00:00").replace(tzinfo=MH.ET).timestamp()
    bar = next((b for b in sorted(bars, key=lambda b: b[0]) if day0 + (int(b[0]) + 1) * 60 >= t_re), None)
    if bar is None:
        return None
    half = (r["buy"] - r["bid"]) / (2 * r["buy"]) if r.get("bid") else 0.0
    px = float(bar[4]) * (1 + half)
    return (r["sell"] / px - 1) * 1e4 if px > 0 else None


# ── statistics (pure) ───────────────────────────────────────────────────────────────────────────────────────────────
def by_day(rows):
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["day"], []).append(r)
    return out


def session_sums(rows, blocked_of, alt=lambda r: 0.0) -> dict[str, float]:
    """day -> sum over that day's blocked trades of (alt - net)."""
    return {d: sum((alt(r) - r["net"]) for r in rs if blocked_of(r)) for d, rs in by_day(rows).items()}


def stat(vals):
    st = S.day_stat(list(vals))
    return {"n": st["n"], "mean": st["mean"], "t": st["t"]}


def random_blocks(rows, draws=1000, seed="MLH"):
    days = by_day(rows)
    out = []
    for i in range(draws):
        rnd = random.Random(f"{seed}-{i}")
        tot = []
        for rs in days.values():
            kb = sum(r["blocked"] for r in rs)
            tot.append(-sum(r["net"] for r in rnd.sample(rs, kb)))
        out.append(statistics.fmean(tot))
    return out


def spread_ranked(rows, seed="MLH-S") -> dict[str, float]:
    rnd = random.Random(seed)
    out = {}
    for d, rs in by_day(rows).items():
        kb = sum(r["blocked"] for r in rs)
        order = sorted(rs, key=lambda r: (-(r["spread_proxy"] if r["spread_proxy"] is not None else -1e9), rnd.random()))
        out[d] = -sum(r["net"] for r in order[:kb])
    return out


def verdict(rows, alt_rearm) -> dict:
    ss = session_sums(rows, lambda r: r["blocked"])
    ss_re = session_sums(rows, lambda r: r["blocked"], alt=alt_rearm)
    days = sorted(ss)
    allst = stat(ss[d] for d in days)
    per = {p: stat(ss[d] for d in days if period(d) == p) for p in ("P1", "P2")}
    rest = stat(ss_re[d] for d in days)
    rnd = random_blocks(rows)
    beat_rand = sum(allst["mean"] > x for x in rnd) / len(rnd) if rnd else 0.0
    spr = spread_ranked(rows)
    paired = stat(ss[d] - spr[d] for d in days)
    blocked = [r for r in rows if r["blocked"]]
    unblocked = [r for r in rows if not r["blocked"]]
    n_sess_blocked = len({r["day"] for r in blocked})
    checks = {
        "1_session_sum": allst["mean"] > 0 and allst["t"] >= T_SCREEN and all(per[p]["mean"] > 0 for p in per),
        "2_rearm_bound": rest["mean"] > 0,
        "3_beats_random": beat_rand >= RAND_SHARE,
        "4_beats_spread_ranked": paired["mean"] > 0 and paired["t"] >= T_SCREEN,
        "5_n_gate": len(blocked) >= MIN_BLOCKED and n_sess_blocked >= MIN_SESS,
    }
    sel_b = statistics.fmean(r["selection"] for r in blocked) if blocked else None
    sel_u = statistics.fmean(r["selection"] for r in unblocked) if unblocked else None
    runners = sel_b is not None and sel_u is not None and sel_b > sel_u
    if not checks["5_n_gate"]:
        label = "UNDERPOWERED"
    elif all(checks.values()):
        label = "PROMISING"
    elif checks["1_session_sum"] and checks["3_beats_random"] and not checks["4_beats_spread_ranked"]:
        label = "SPREAD-EQUIVALENT"
    else:
        label = "NOT USEFUL"
    return {"label": label, "checks": checks, "session_sum": allst, "by_period": per, "rearm_session_sum": rest,
            "random_share_beaten": beat_rand, "minus_spread_ranked": paired,
            "blocked_n": len(blocked), "blocked_sessions": n_sess_blocked, "trades": len(rows),
            "decomposition": {"blocked_mean_slip": statistics.fmean(r["slip"] for r in blocked) if blocked else None,
                              "blocked_mean_selection": sel_b, "unblocked_mean_selection": sel_u,
                              "blocked_are_runners": runners,
                              "note": "if blocked_are_runners, the verdict cannot exceed PROMISING and the switch-off "
                                      "watch for blocked winners is mandatory"}}


def info(rows) -> dict:
    out = {"slip_share": {f">{t:g}bp": sum(r["slip"] > t for r in rows) / len(rows) for t in (0, 5, 10, 20)}}
    for k in KS_INFO:
        rs = [dict(r, blocked=r["slip"] > k) for r in rows]
        ss = session_sums(rs, lambda r: r["blocked"])
        out[f"k{k:g}"] = {"blocked": sum(r["blocked"] for r in rs), "session_sum": stat(ss.values())}
    return out


# ── run ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
def fills_for(day: str) -> list[dict]:
    p = OUT / f"fills_{day}.json"
    if p.exists():
        return json.loads(p.read_text())
    f = L.fetch_fills(day)
    p.write_text(json.dumps(f))
    return f


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    raw = [json.loads(x) for x in open(ROOT / "ai_reports" / "outcomes.jsonl") if x.strip()]
    rows, drops = rows_from(raw)
    st, scored = Counter(), []
    for day in sorted({r["day"] for r in rows}):
        fills = fills_for(day)
        for r in [x for x in rows if x["day"] == day]:
            m = match_with_time(r, fills)
            if m is None:
                st["unmatched"] += 1
                continue
            if m[0] < FLOOR:
                st["below_floor"] += 1
                continue
            scored.append(score_row(dict(r, buy=m[0], sell=m[1], send=m[2])))
    data = MH.Data.live()
    data.dir = OUT / "cache"
    alt_cache = {}

    def alt(r):
        key = (r["symbol"], r["entry_time"])
        if key not in alt_cache:
            try:
                bars = data.bars(r["symbol"], r["day"])
            except MH.FetchError:
                bars = []
                st["bar_errors"] += 1
            n = reentry_net(r, bars)
            alt_cache[key] = 0.0 if n is None else n
        return alt_cache[key]

    primary = [r for r in scored if r["day"] <= PRIMARY_LAST]
    res = {"drops": drops, "stats": dict(st), "unmatched_share": st["unmatched"] / max(1, len(rows)),
           "primary": verdict(primary, alt), "with_10_27_29": verdict(scored, alt), "info": info(primary),
           "note": "Paper fills are Alpaca simulations; slip mixes the IEX print the desk anchored on against Alpaca "
                   "paper's simulated quote. PROMISING earns a paper forward test, not a switch-on."}
    res["void"] = res["unmatched_share"] > 0.05
    (OUT / "result.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: res[k] for k in ("void", "unmatched_share", "stats")}, default=str))
    print(json.dumps(res["primary"], indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
