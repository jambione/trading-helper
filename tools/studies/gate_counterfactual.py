#!/usr/bin/env python3
"""Admission-gate counterfactual SCREEN. Spec: docs/studies/gate_counterfactual_prereg.json (as amended).

Did the names a gate refused move better or worse over the next 10 minutes than the names on the desk's board at the
same minute? PRIMARY: the float cap (float_too_big), on name-days refused by it and by no other tested gate.

  --units   refusal units from ai_reports/rejects.jsonl and the board from ai_reports/decision_ledger (stage=arm);
            no prices -> units.json
  --bars    SIP 1-minute bars (data-only account) per day for every refused and board name -> bars/DAY.pkl
  (default) count step: units per gate / half, overlap, missing-bar rates, MDE from random-minute dispersion of both
            groups (no gate outcome) -> count.json
  --score   the screen's reading; refuses without --skeptic-ready and a count step from this code
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("EEH_ROOT") or os.path.dirname(os.path.dirname(HERE))
OUT_DIR = os.path.join(ROOT, "ai_reports", "gate_counterfactual")
BAR_DIR = os.path.join(OUT_DIR, "bars")
ET = ZoneInfo("America/New_York")
FIRST, LAST = "2026-09-10", "2026-10-21"
T_LO, T_HI = 9 * 60 + 45, 15 * 60                     # refusal / admission minutes considered (ET)
HORIZON, ENTRY_MAX_LAG, BOARD_LOOKBACK_S = 10, 2, 120
COST, BAR, MIN_PER_GROUP = 0.0005, 0.0005, 3
SPREAD_CAP_DAY = "2026-10-03"
GATES = {"G1_float": ("float_too_big",), "G2_spread": ("spread_wide",), "G3_gapdown": ("gapped_down",),
         "G4_price": ("below_min_price",), "G5_rvol": ("thin_rvol",)}
TESTED = {r for rs in GATES.values() for r in rs}
SEED_TAG = "gate-counterfactual-random-minute"


def P(*a):
    print(*a, flush=True)


def write(name: str, obj) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    json.dump(obj, open(p + ".tmp", "w"), indent=1, default=str)
    os.replace(p + ".tmp", p)
    return p


def jload(name: str, default=None):
    p = os.path.join(OUT_DIR, name)
    return json.load(open(p)) if os.path.exists(p) else default


def et_min(ts: float) -> tuple[str, int]:
    t = datetime.fromtimestamp(ts, ET)
    return t.strftime("%Y-%m-%d"), t.hour * 60 + t.minute


# ── units (pure on records) ──────────────────────────────────────────────────

def build_units(rejects: list[dict], board: dict) -> dict:
    """rejects: [{ts, symbol, reason}]; board: {day: {sym: [ts, ...] sorted}}.
    Per gate: name-day units at the FIRST refusal by that gate in [T_LO, T_HI] (whether or not admitted later),
    with all tested reasons the name-day got, and the first board ts after the refusal (admission)."""
    first, meta = {}, {}
    reasons = defaultdict(set)
    for r in rejects:
        d, m = et_min(r["ts"])
        if not (FIRST <= d <= LAST) or d not in board:
            continue
        if r["reason"] in TESTED:
            reasons[(d, r["symbol"])].add(r["reason"])
        if not (T_LO <= m <= T_HI):
            continue
        k = (r["reason"], d, r["symbol"])
        if r["reason"] in TESTED and (k not in first or r["ts"] < first[k]):
            first[k] = r["ts"]
            meta[k] = {"pct": r.get("pct"), "source": r.get("source")}
    units = defaultdict(list)
    for (reason, d, sym), ts in sorted(first.items()):
        gate = next(g for g, rs in GATES.items() if reason in rs)
        later = [t for t in board[d].get(sym, []) if t > ts]
        units[gate].append({"day": d, "sym": sym, "ts": ts, "admit_ts": later[0] if later else None,
                            "other_gates": sorted(reasons[(d, sym)] - set(GATES[gate])),
                            "on_board_before": any(t <= ts for t in board[d].get(sym, [])),
                            **meta.get((reason, d, sym), {})})
    return dict(units)


def board_at(board_day: dict, ts: float, exclude: str) -> list[str]:
    """Board names with an arm row in [ts - BOARD_LOOKBACK_S, ts)."""
    out = []
    for sym, tss in board_day.items():
        if sym == exclude:
            continue
        import bisect
        i = bisect.bisect_left(tss, ts - BOARD_LOOKBACK_S)
        if i < len(tss) and tss[i] < ts:
            out.append(sym)
    return sorted(out)


# ── returns (pure on bars) ───────────────────────────────────────────────────

def window(bars: dict, ts: float, admit_ts: float | None = None) -> tuple:
    """Entry = open of the first bar starting after ts (<= ENTRY_MAX_LAG minutes later); exit = close of the bar
    HORIZON - 1 minutes after the entry bar, cut at the bar before the admission minute when admission comes first.
    bars: {minute_of_day: (open, close)}. Returns (entry_min, exit_min, cut) or None."""
    _d, m = et_min(ts)
    e = next((k for k in range(m + 1, m + 1 + ENTRY_MAX_LAG) if k in bars), None)
    if e is None:
        return None
    x = e + HORIZON - 1
    cut = False
    if admit_ts is not None:
        am = et_min(admit_ts)[1]
        if am <= x:
            x, cut = am - 1, True
    if x < e:
        return "admitted_before_entry"
    xx = next((k for k in range(x, e - 1, -1) if k in bars), None)
    if xx is None or x - xx > 2:
        return None
    return e, xx, cut


def ret(bars: dict, e: int, x: int) -> float | None:
    if e not in bars or x not in bars or bars[e][0] <= 0:
        return None
    return bars[x][1] / bars[e][0] - 1


def control_ret(day_bars: dict, syms: list[str], e: int, x: int, st: Counter | None = None,
                gate: str = "") -> float | None:
    rs = [ret(day_bars.get(s, {}), e, x) for s in syms]
    if st is not None:
        st[f"{gate}_ctrl_drawn"] += len(rs)
        st[f"{gate}_ctrl_missing"] += sum(1 for r in rs if r is None)
    rs = [r for r in rs if r is not None]
    return statistics.mean(rs) if rs else None


# ── statistics (pure) ────────────────────────────────────────────────────────

def t_ppf(p: float, df: int) -> float:
    sys.path.insert(0, HERE)
    import indicator_confirm_study as S
    return S.t_ppf(p, df)


def day_stats(rows: list[dict], key: str, min_per_day: int = MIN_PER_GROUP) -> dict:
    per = defaultdict(list)
    for r in rows:
        if r.get(key) is not None:
            per[r["day"]].append(r[key])
    means = {d: statistics.mean(v) for d, v in per.items() if len(v) >= min_per_day}
    xs = list(means.values())
    n = len(xs)
    if n < 2:
        return {"days": n, "mean": xs[0] if xs else None, "t": None, "lo": None, "hi": None, "obs": sum(map(len, per.values()))}
    mu, se = statistics.mean(xs), statistics.stdev(xs) / math.sqrt(n)
    tc = t_ppf(0.975, n - 1)
    return {"days": n, "mean": mu, "t": mu / se if se else None, "lo": mu - tc * se, "hi": mu + tc * se,
            "t_crit": tc, "obs": sum(map(len, per.values()))}


def reading(halves: dict) -> str:
    """amended (8) for the float cap."""
    a, b = halves["A"], halves["B"]
    if any(h["diff"]["mean"] is None for h in (a, b)):
        return "NO EVIDENCE (insufficient data)"
    costly = all(h["diff"]["mean"] >= BAR and h["level_net"] is not None and h["level_net"] >= 0 for h in (a, b))
    prot = all(h["diff"]["mean"] <= -BAR and h["diff"]["t"] is not None and h["diff"]["t"] <= -h["diff"]["t_crit"]
               for h in (a, b))
    return "COSTLY" if costly else "PROTECTIVE" if prot else "NO EVIDENCE"


# ── data (mini) ──────────────────────────────────────────────────────────────

def load_rejects() -> list[dict]:
    out = []
    for line in open(os.path.join(ROOT, "ai_reports", "rejects.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("ts") and r.get("symbol") and r.get("reason"):
            out.append({"ts": float(r["ts"]), "symbol": r["symbol"], "reason": r["reason"],
                        "pct": r.get("pct_change"), "source": r.get("source")})
    return out


def load_board() -> dict:
    out = {}
    d0 = datetime.strptime(FIRST, "%Y-%m-%d")
    while d0.strftime("%Y-%m-%d") <= LAST:
        d = d0.strftime("%Y-%m-%d")
        p = os.path.join(ROOT, "ai_reports", "decision_ledger", f"{d}.jsonl")
        if os.path.exists(p):
            b = defaultdict(list)
            for line in open(p):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("stage") == "arm" and r.get("symbol") and r.get("ts"):
                    b[r["symbol"]].append(float(r["ts"]))
            if b:
                out[d] = {s: sorted(v) for s, v in b.items()}
        d0 += timedelta(days=1)
    return out


def fetch_day(day: str, syms: list[str]) -> dict:
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    import time
    s_ = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    cl = StockHistoricalDataClient(s_["alpaca_data_api_key"], s_["alpaca_data_secret_key"])
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    out = {}
    for k in range(0, len(syms), 100):
        for attempt in range(5):
            try:
                got = cl.get_stock_bars(StockBarsRequest(symbol_or_symbols=syms[k:k + 100],
                                                         timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                                                         start=d.replace(hour=9, minute=30).astimezone(timezone.utc),
                                                         end=d.replace(hour=16).astimezone(timezone.utc),
                                                         feed=DataFeed.SIP)).data or {}
                break
            except Exception as e:  # noqa: BLE001
                P(f"  retry {attempt + 1}: {str(e)[:100]}")
                time.sleep(10 * (attempt + 1))
        else:
            raise SystemExit(f"bars failed {day}")
        for sym, bs in got.items():
            out[sym] = {b.timestamp.astimezone(ET).hour * 60 + b.timestamp.astimezone(ET).minute:
                        (float(b.open), float(b.close)) for b in bs}
        time.sleep(0.4)
    return out


def module_shas() -> dict:
    return {"gate_counterfactual": hashlib.sha256(open(os.path.abspath(__file__), "rb").read()).hexdigest()}


def evaluate(units: dict, board: dict, bars_of) -> tuple[dict, Counter]:
    """Per gate: rows with the refused return, the same-minute board control and their difference; missing counted."""
    st, out = Counter(), {}
    for gate, us in units.items():
        rows = []
        for u in us:
            if u.get("on_board_before"):
                st[f"{gate}_on_board_before"] += 1
                continue
            db = bars_of(u["day"])
            w = window(db.get(u["sym"], {}), u["ts"], u["admit_ts"])
            if w == "admitted_before_entry":
                st[f"{gate}_admitted_before_entry"] += 1
                continue
            if w is None:
                st[f"{gate}_refused_missing"] += 1
                continue
            e, x, cut = w
            r = ret(db.get(u["sym"], {}), e, x)
            ctl_syms = board_at(board[u["day"]], u["ts"], u["sym"])
            if not ctl_syms:
                st[f"{gate}_no_board_120s"] += 1
                continue
            c = control_ret(db, ctl_syms, e, x, st, gate)
            if r is None:
                st[f"{gate}_refused_missing"] += 1
                continue
            if c is None:
                st[f"{gate}_control_missing"] += 1
                continue
            st[f"{gate}_ok"] += 1
            x30 = min(e + 30 - 1, (et_min(u["admit_ts"])[1] - 1) if u.get("admit_ts") else 10 ** 6)
            r30, c30 = ret(db.get(u["sym"], {}), e, x30), control_ret(db, ctl_syms, e, x30)
            rows.append({**u, "ret": r, "ctrl": c, "diff": r - c, "cut": cut, "n_ctrl": len(ctl_syms),
                         "net": r - COST, "diff30": (r30 - c30) if (r30 is not None and c30 is not None) else None})
        out[gate] = rows
    return out, st


def random_mde(units: dict, board: dict, bars_of, mid: str) -> dict:
    """amended_2 (3): per gate and half, refused name minus a seeded board name at the SAME seeded random minute;
    MDE = (t_crit + 0.84) x SD of per-day means / sqrt(days). No gate outcome is read."""
    out = {}
    for gate, us in units.items():
        per = {"A": defaultdict(list), "B": defaultdict(list)}
        for u in us:
            if u.get("on_board_before"):
                continue
            db = bars_of(u["day"])
            rng = random.Random(int(hashlib.sha256(f"{SEED_TAG}|{gate}|{u['day']}|{u['sym']}".encode()).hexdigest()[:16], 16))
            m = rng.randint(T_LO, T_HI - HORIZON)
            other = rng.choice(sorted(s_ for s_ in board[u["day"]] if s_ != u["sym"]) or [u["sym"]])
            a = ret(db.get(u["sym"], {}), m, m + HORIZON - 1)
            b = ret(db.get(other, {}), m, m + HORIZON - 1)
            if a is not None and b is not None:
                per["A" if u["day"] < mid else "B"][u["day"]].append(a - b)
        out[gate] = {}
        for h, dd in per.items():
            xs = [statistics.mean(v) for v in dd.values() if len(v) >= MIN_PER_GROUP]
            if len(xs) >= 2:
                sd = statistics.stdev(xs)
                out[gate][h] = {"days": len(xs), "mde": (t_ppf(0.975, len(xs) - 1) + 0.84) * sd / math.sqrt(len(xs))}
            else:
                out[gate][h] = {"days": len(xs), "mde": None}
    return out


def g6_delay_count(rejects: list[dict], board: dict) -> dict:
    """amended_2 (8): not_uptrend / admit_range_pos name-days, and how many were admitted later that day."""
    seen, later = set(), set()
    for r in rejects:
        if r["reason"] not in ("not_uptrend", "admit_range_pos"):
            continue
        d, _m = et_min(r["ts"])
        if d not in board:
            continue
        seen.add((d, r["symbol"]))
        if any(t > r["ts"] for t in board[d].get(r["symbol"], [])):
            later.add((d, r["symbol"]))
    return {"name_days": len(seen), "admitted_later": len(later)}


def missing_rates(units: dict, st: Counter) -> dict:
    out = {}
    for gate, us in units.items():
        n = sum(1 for u in us if not u.get("on_board_before"))
        out[gate] = {"refused": st[f"{gate}_refused_missing"] / max(1, n),
                     "board": st[f"{gate}_ctrl_missing"] / max(1, st[f"{gate}_ctrl_drawn"]),
                     "no_board_120s": st[f"{gate}_no_board_120s"]}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    for f in ("--units", "--bars", "--score"):
        g.add_argument(f, action="store_true")
    ap.add_argument("--skeptic-ready", default="")
    a = ap.parse_args()
    if a.units:
        board = load_board()
        units = build_units(load_rejects(), board)
        P({g_: len(v) for g_, v in units.items()}, "board days", len(board))
        write("units.json", {"units": units, "board": board, "module_sha256": module_shas()})
        return
    u = jload("units.json")
    units, board = u["units"], u["board"]
    if a.bars:
        os.makedirs(BAR_DIR, exist_ok=True)
        for day in sorted(board):
            p = os.path.join(BAR_DIR, f"{day}.pkl")
            if os.path.exists(p):
                continue
            syms = sorted(set(board[day]) | {x["sym"] for us in units.values() for x in us if x["day"] == day})
            pickle.dump(fetch_day(day, syms), open(p + ".tmp", "wb"))
            os.replace(p + ".tmp", p)
            P(f"  bars {day}: {len(syms)} names")
        return
    cache = {}

    missing_files = [d_ for d_ in board if not os.path.exists(os.path.join(BAR_DIR, f"{d_}.pkl"))]
    if missing_files:
        raise SystemExit(f"bars missing for {len(missing_files)} board days (e.g. {missing_files[:3]}); run --bars")

    def bars_of(day):
        if day not in cache:
            cache[day] = pickle.load(open(os.path.join(BAR_DIR, f"{day}.pkl"), "rb"))
        return cache[day]
    days = sorted(board)
    mid = days[len(days) // 2]
    rows, st = evaluate(units, board, bars_of)
    if not a.score:
        cnt = {"window": [days[0], days[-1]], "half_split": mid, "stats": dict(st),
               "mde": random_mde(units, board, bars_of, mid), "g6_delay": g6_delay_count(load_rejects(), board),
               "units_sha256": hashlib.sha256(open(os.path.join(OUT_DIR, "units.json"), "rb").read()).hexdigest(),
               "module_sha256": module_shas()}
        for gate, us in units.items():
            only = [x for x in us if not x["other_gates"]]
            cnt[gate] = {"units": len(us), "only_this_gate": len(only),
                         "admitted_later": sum(1 for x in us if x["admit_ts"]),
                         "per_half": {h: sum(1 for x in (only if gate == "G1_float" else us)
                                             if (x["day"] < mid) == (h == "A")) for h in ("A", "B")}}
        cnt["missing_rate"] = missing_rates(units, st)
        P(json.dumps(cnt, indent=1, default=str))
        P(write("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = jload("count.json")
    if not cnt or cnt.get("module_sha256") != module_shas() or cnt.get("units_sha256") != hashlib.sha256(
            open(os.path.join(OUT_DIR, "units.json"), "rb").read()).hexdigest():
        raise SystemExit("count.json missing or from different code / units")
    miss = missing_rates(units, st)
    res = {"primary_gate": "G1_float", "gates": {}, "missing_rate": miss, "g6_delay": cnt.get("g6_delay"),
           "mde": cnt.get("mde")}
    for gate, rs in rows.items():
        use = [r for r in rs if not r["other_gates"]] if gate == "G1_float" else rs
        hv = {}
        for h, sel in (("A", lambda r: r["day"] < mid), ("B", lambda r: r["day"] >= mid)):
            part = [r for r in use if sel(r)]
            lev = day_stats(part, "net")
            hv[h] = {"diff": day_stats(part, "diff"), "refused_level": day_stats(part, "ret"),
                     "board_level": day_stats(part, "ctrl"), "level_net": lev["mean"]}
        rd = reading(hv) if gate == "G1_float" else "INFORMATION ONLY"
        if gate == "G1_float" and abs(miss[gate]["refused"] - miss[gate]["board"]) > 0.10:
            rd = "NOT READ (missing gap)"
        res["gates"][gate] = {"halves": hv, "all": day_stats(use, "diff"), "reading": rd,
                              "horizon_30min": day_stats(use, "diff30"),
                              "up_on_day": day_stats([r for r in use if (r.get("pct") or 0) > 0], "diff"),
                              "not_up_on_day": day_stats([r for r in use if r.get("pct") is not None and r["pct"] <= 0], "diff"),
                              "by_source": {src: day_stats([r for r in use if r.get("source") == src], "diff", 1)
                                            for src in sorted({r.get("source") for r in use if r.get("source")})}}
        if gate == "G2_spread":
            res["gates"][gate]["before_0_05pct_cap"] = day_stats([r for r in use if r["day"] < SPREAD_CAP_DAY], "diff")
            res["gates"][gate]["after_0_05pct_cap"] = day_stats([r for r in use if r["day"] >= SPREAD_CAP_DAY], "diff")
    res["skeptic_ready"], res["module_sha256"] = a.skeptic_ready, module_shas()
    write("result.json", res)
    P(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
