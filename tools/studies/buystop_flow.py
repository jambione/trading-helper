#!/usr/bin/env python3
"""Buy-stop entries filtered by buyer-initiated order flow, at quoted cost; arm A = %R turn-down exit, arm B = the
desk's live exit stack. Spec: docs/studies/buystop_flow_prereg.json (main + amended).

Phases (each resumable, data-only account for every fetch):
  --sample   seeded, day-stratified sample of batch 2 T2 trades >= $20 (no outcomes read); re-derives each trade's stop
  --probe    real fetch_one + fetch_arm_b on >= 10 stride-spread trades -> largest N per half inside 9 hours
  --fetch    per trade: SIP trades [k-60 s, k+60 s) (trigger T + flow), quotes [T-120 s, T+1 s], exit quotes
  --cuts     G1 (OFI60) top-tercile cut from HALF A features only (outcome-blind), then arm-B prints + IEX bars for
             G1-IN trades
  (default)  count step (outcome-blind)       --score  verdicts (refuses without READY / floors / same code)
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
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
import batch2 as B2  # noqa: E402
import wr_trend_trade as W  # noqa: E402

ROOT = W.ROOT
OUT = os.path.join(ROOT, "ai_reports", "buystop_flow")
MIN_PX, N_MAX, BUDGET_H, REQ_PER_MIN = 20.0, 8000, 9.0, 140
READ_AGE, T_ARM, ENTRY_AGE = 30.0, 2.24, 3.0
TRIGGER_CODES = frozenset({" ", "@", "F"})          # round lots (amended_2 (1)); odd lots 'I' never trigger a stop
R_RATIO = 0.05009                                    # amended_2 (4): median desk R/entry, fills >= $20
CONFIG_SHA_PREFIX = "837dfd79268ebf16"               # amended_2 (3)  # pragma: allowlist secret (a git commit prefix)
EOD_MIN = 15 * 60 + 50
UB_P = 0.9875                                        # 98.75% bound is t_ppf(0.9875): Bonferroni of the 97.5% bound over 2 arms
IN_SHARE = 1.0 / 3.0                                 # G1 is a top tercile, so arm B is fetched for about a third of trades
PROBE_MIN, PROBE_CAP = 10, 40
MDE_BAR, FLOOR_TRADES, FLOOR_DAYS = 0.0003, 300, 20
# amended_2 (6): projected MDE at 8,000 trades per half, from the batch2 control rows (outcome-blind)
PREREG_MDE_AT_8000 = {"A": 2.63e-4, "B": 2.23e-4}
SHA_IN_STUDIES = (
    "buystop_flow", "batch2", "wr_trend_trade", "order_flow_pilot", "exit_capture",
    "leash_replay", "eod_reversal", "indicator_confirm_hist",
)


def jload(name, default=None):
    p = os.path.join(OUT, name)
    return json.load(open(p)) if os.path.exists(p) else default


def jwrite(name, obj):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    json.dump(obj, open(p + ".tmp", "w"), default=str)
    os.replace(p + ".tmp", p)
    return p


def seeded(*parts) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def et_epoch(day: str, minute: int, sec: float = 0.0) -> float:
    from zoneinfo import ZoneInfo
    d = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("America/New_York"))
    return d.replace(hour=minute // 60, minute=minute % 60).timestamp() + sec


def et_minute(day: str, ts: float) -> int:
    from zoneinfo import ZoneInfo
    dt = datetime.fromtimestamp(ts, ZoneInfo("America/New_York"))
    if dt.strftime("%Y-%m-%d") != day:
        return -1
    return dt.hour * 60 + dt.minute


def pace() -> None:
    time.sleep(60 / REQ_PER_MIN)


def round_lot(codes) -> bool:
    """A trigger print is a round lot when every condition is in TRIGGER_CODES.

    An empty condition list is a regular sale: it is a subset of that set, and
    leash_replay.fetch_seconds keeps it. order_flow_pilot's OFI60 still drops
    empty codes. Declared (amended_3), not copied from the pilot — the trigger
    and the flow feature are different filters on purpose.
    """
    cs = list(codes or [])
    return all(c in TRIGGER_CODES for c in cs) if cs else True


def merge_secs(chunks: list) -> list:
    """One row per second. Chunks are in time order. A second split across a chunk
    boundary keeps the lowest low and the later chunk's last price."""
    acc: dict[int, tuple] = {}
    for secs in chunks:
        for s, last, lo in secs:
            if s in acc:
                _prev_last, prev_lo = acc[s]
                acc[s] = (last, min(prev_lo, lo))
            else:
                acc[s] = (last, lo)
    return sorted((s, last, lo) for s, (last, lo) in acc.items())


def arm_b_horizons() -> tuple[int, ...]:
    """Seconds after T. The last fetch ends MAX_HOLD + 60 past T so a time-cap exit
    (the walk returns at T + MAX_HOLD) sits inside the fetched prints."""
    import leash_replay as L
    return (15 * 60, 60 * 60, L.MAX_HOLD + 60)


def chunk_end(t0: float, horizon: int, eod: float) -> float:
    return min(t0 + horizon, eod)


def exit_beyond_fetch(xs: float, fetched_to: float, eod: float) -> bool:
    """The walk stopped on the edge of a print fetch that does not reach 15:50."""
    return xs >= fetched_to - 1 and fetched_to < eod - 1e-6


def select_trades(trades: list, n_half: int) -> list:
    """Day-stratified prefix of an already-seeded sample. Does not read outcomes.
    Within a day the sample's existing order is kept, so the same seed is reused."""
    by = defaultdict(list)
    for t in trades:
        by[(t["half"], t["day"])].append(t)
    out = []
    for h in ("A", "B"):
        days = sorted(d for (hh, d) in by if hh == h)
        if not days or n_half <= 0:
            continue
        per, extra = divmod(n_half, len(days))
        for i, d in enumerate(days):
            out.extend(by[(h, d)][:per + (1 if i < extra else 0)])
    return out


def projected_mde(n_half: int) -> dict:
    """Scale amended_2's 8,000-trade projection by sqrt(8000 / n). The count step recomputes it."""
    if n_half <= 0:
        return {"A": None, "B": None}
    scale = math.sqrt(8000 / n_half)
    return {h: PREREG_MDE_AT_8000[h] * scale for h in ("A", "B")}


def budget_n_half(sec_a: float, sec_b: float, sample_half: int, in_share: float = IN_SHARE) -> dict:
    """Largest n per half that fits BUDGET_H, capped by the sample and by N_MAX.
    Arm B is costed on `in_share` of the trades (the G1-IN tercile)."""
    per = sec_a + in_share * sec_b
    if per <= 0:
        raise ValueError("probe measured no time")
    n_full = sample_half * 2
    n_total = int(BUDGET_H * 3600 / per)
    n_half = max(0, min(N_MAX, sample_half, n_total // 2))
    mde = projected_mde(n_half)
    fits = n_half > 0 and all(v is not None and v <= MDE_BAR for v in mde.values())
    return {"sec_per_trade_A": sec_a, "sec_per_in_trade_B": sec_b, "sec_per_trade": per,
            "hours_at_full_sample": (n_full * per) / 3600, "n_half": n_half,
            "projected_mde": mde, "projected_under_bar": fits}


def probe_indexes(n_trades: int, cap: int = PROBE_CAP) -> list[int]:
    """Up to `cap` indexes spread across the sample (a stride, not the first rows)."""
    if n_trades <= 0:
        return []
    stride = max(1, n_trades // cap)
    return list(range(0, n_trades, stride))[:cap]


def probe_blocks(n_trades: int, need: int = PROBE_MIN, cap: int = PROBE_CAP) -> list[list[int]]:
    """Spread indexes, split into `need` blocks. The probe takes one success from each block."""
    idxs = probe_indexes(n_trades, cap)
    if not idxs:
        return []
    blocks: list[list[int]] = [[] for _ in range(need)]
    for i, ix in enumerate(idxs):
        blocks[min(need - 1, i * need // len(idxs))].append(ix)
    return [b for b in blocks if b]


def spread_bp(bid, ask) -> float | None:
    if not bid or not ask or bid <= 0 or ask <= 0:
        return None
    mid = (ask + bid) / 2
    return (ask - bid) / mid * 1e4 if mid > 0 else None


def median_of(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def spy_return(opens: dict | None, entry_min: int, exit_min: int) -> float | None:
    """SPY SIP open of the entry minute to open of the exit minute. Missing bar -> None."""
    if not opens or entry_min not in opens or exit_min not in opens:
        return None
    o0, o1 = opens[entry_min], opens[exit_min]
    if o0 <= 0 or o1 <= 0:
        return None
    return o1 / o0 - 1


def spy_opens(day: str) -> dict:
    import indicator_confirm_hist as H
    raw = H.load_raw("sip", day).get("SPY")
    if raw is None:
        return {}
    return {int(r[0]): float(r[1]) for r in raw}


# ── sample ───────────────────────────────────────────────────────────────────

def t2_with_stops(fast, slow, sipf, iexf) -> list[tuple]:
    """batch2.run_rule's T2 branch, also returning the stop level at the fill (same state machine)."""
    out, state = [], {"free_from": 0}
    t = W.DEC_LO - 30
    while t <= W.DEC_HI + 10:
        before = state.get("stop")
        hit = B2.t2_step(fast, slow, iexf, sipf, t, state)
        if hit is None:
            t += 1
            continue
        e, px = hit
        u = W.exit_minute(fast, slow, e + 1)
        x_min = W.M_LAST + 1 if (u is None or u + 1 > W.M_LAST) else u + 1
        out.append((e, px, x_min, before))
        state = {"free_from": x_min}
        t = x_min
    return out


def build_sample(n_half: int) -> dict:
    import indicator_confirm_hist as H
    rows = json.load(open(os.path.join(ROOT, "ai_reports", "batch2", "rows.json")))["rows"]
    t2 = [r for r in rows if r["test"] == "T2" and r["px"] >= MIN_PX]
    by_day = defaultdict(list)
    for r in t2:
        by_day[r["day"]].append(r)
    days = H.warehouse_days()
    out, st = [], Counter()
    for h in ("A", "B"):
        hd = sorted(d for d in by_day if (d <= W.HALF_A_END) == (h == "A"))
        per_day = max(1, n_half // max(1, len(hd)))
        for d in hd:
            pick = sorted(by_day[d], key=lambda r: (r["sym"], r["entry_min"]))
            seeded("bsf-sample", d).shuffle(pick)
            pick = pick[:per_day]
            if not pick:
                continue
            prev = days[days.index(d) - 1]
            sip, iex, iexp = H.load_raw("sip", d), H.load_raw("iex", d), H.load_raw("iex", prev)
            for r in pick:
                s = r["sym"]
                if s not in iex or s not in iexp or s not in sip:
                    st["missing_bars"] += 1
                    continue
                W.REAL.clear()
                fast, slow = W.lines(iexp[s], iex[s])
                sipf = {int(x[0]): (float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in sip[s]}
                iexf = {int(x[0]): (float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in iex[s]}
                match = [x for x in t2_with_stops(fast, slow, sipf, iexf) if x[0] == r["entry_min"]]
                if not match or abs(match[0][1] - r["px"]) > 1e-6 or match[0][3] is None:
                    st["stop_not_rederived"] += 1
                    continue
                e, px, x_min, stop = match[0]
                out.append({"day": d, "sym": s, "half": h, "k": e, "px_bar": px, "stop": stop, "exit_min": x_min,
                            "bar_gross": r["gross"], "ctrl_d": r.get("d")})
    b2 = json.load(open(os.path.join(ROOT, "ai_reports", "batch2", "rows.json"))).get("module_sha256")
    return {"trades": out, "stats": dict(st), "n_half": n_half, "batch2_rows_module_sha256": b2}


# ── fetch ────────────────────────────────────────────────────────────────────

def client():
    from alpaca.data.historical import StockHistoricalDataClient
    s = json.load(open(os.path.join(ROOT, "secrets_data.json")))
    return StockHistoricalDataClient(s["alpaca_data_api_key"], s["alpaca_data_secret_key"])


def _req(fn, *a, **k):
    for attempt in range(6):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            print(f"  retry {attempt + 1}: {str(e)[:80]}", flush=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError("fetch failed")


def trades_between(cl, sym, a, b):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    utc = lambda x: datetime.fromtimestamp(x, timezone.utc)  # noqa: E731
    got = _req(cl.get_stock_trades, StockTradesRequest(symbol_or_symbols=sym, start=utc(a), end=utc(b),
                                                       feed=DataFeed.SIP)).data.get(sym) or []
    return [(x.timestamp.timestamp(), float(x.price), float(x.size), list(x.conditions or []), x.tape) for x in got]


def quotes_between(cl, sym, a, b):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest
    utc = lambda x: datetime.fromtimestamp(x, timezone.utc)  # noqa: E731
    got = _req(cl.get_stock_quotes, StockQuotesRequest(symbol_or_symbols=sym, start=utc(a), end=utc(b),
                                                       feed=DataFeed.SIP)).data.get(sym) or []
    return [(x.timestamp.timestamp(), float(x.bid_price or 0), float(x.ask_price or 0), float(x.bid_size or 0),
             float(x.ask_size or 0)) for x in got]


def trigger(trades: list, k_start: float, stop: float):
    """(T, price) of the first ROUND-LOT print in minute k at >= stop + 1c, else None."""
    for ts, px, size, codes, _tape in sorted(trades, key=lambda x: x[0]):
        if k_start <= ts < k_start + 60 and round_lot(codes) and px >= stop + 0.01:
            return ts, px
    return None


def ask_at(qs: list, t: float, max_age: float):
    import order_flow_pilot as OF
    q = OF.last_quote(OF.clean_quotes(qs), t, max_age)
    return q


def fetch_one(cl, t: dict) -> dict:
    import order_flow_pilot as OF
    k0 = et_epoch(t["day"], t["k"])
    tr = trades_between(cl, t["sym"], k0 - 60, k0 + 60)
    nreq = 1
    pace()
    trg = trigger(tr, k0, t["stop"])
    if trg is None:
        return {"status": "no_trigger_in_tape", "requests": nreq}
    T, trig_px = trg
    qs = quotes_between(cl, t["sym"], T - 120, T + 12)
    nreq += 1
    pace()
    x_ts = et_epoch(t["day"], 15 * 60 + 59, 59) if t["exit_min"] > W.M_LAST else et_epoch(t["day"], t["exit_min"])
    xq = quotes_between(cl, t["sym"], x_ts - 30, x_ts + 1)
    nreq += 1
    pace()
    tick = {"trades": tr, "quotes": qs}
    f = OF.features(tick, T)
    q_in = ask_at(qs, T + 1, ENTRY_AGE + 1)            # a quote in [T - 3 s, T + 1 s]
    q3, q11 = ask_at(qs, T + 3, ENTRY_AGE), ask_at(qs, T + 11, ENTRY_AGE)
    q_out = OF.last_quote(OF.clean_quotes(xq), x_ts + 1, READ_AGE)
    ask = q_in[2] if q_in else None
    return {"status": "ok", "T": T, "trig_px": trig_px, "x_ts": x_ts, "ofi60": f["ofi60"], "qi": f["qi"],
            "n_signed": f["n_signed"], "ask_raw": ask, "ask_in": max(ask, trig_px) if ask else None,
            "ask_below_trigger": (ask < trig_px) if ask else None, "bid_in": q_in[1] if q_in else None,
            "ask_T3": max(q3[2], trig_px) if q3 else None, "ask_T11": max(q11[2], trig_px) if q11 else None,
            "bid_out": q_out[1] if q_out else None, "ask_out": q_out[2] if q_out else None,
            "requests": nreq}


# ── arm B (desk exits) ───────────────────────────────────────────────────────

def arm_b_walk(r: dict, secs: list, b1m: list, cfg: dict, ring: int):
    import exit_capture as XC
    entry = r["ask_in"]
    risk = entry * R_RATIO
    spread_r = ((r["ask_raw"] - r["bid_in"]) / risk) if r.get("ask_raw") and r.get("bid_in") else None
    pos = {"sym": r["sym"], "day": r["day"], "t0": r["T"], "entry": entry, "risk": risk, "spread_r": spread_r}
    return XC.walk(pos, secs, cfg, ring, b1m)


def fetch_arm_b(cl, r: dict, cfg: dict, ring: int) -> dict:
    """Chunked prints (amended_2 (5)): extend while the walk's exit falls at the fetched end.
    The last chunk ends at min(T + MAX_HOLD + 60, 15:50). Seconds split by a boundary are merged."""
    import exit_capture as XC
    import leash_replay as L
    b1m = _req(XC.fetch_bars, cl, r["sym"], r["day"])
    nreq = 1
    eod = et_epoch(r["day"], EOD_MIN)
    chunks, got_to = [], r["T"] - 5
    secs, why = [], None
    for horizon in arm_b_horizons():
        end = chunk_end(r["T"], horizon, eod)
        if end > got_to + 1e-6:
            chunks.append(_req(L.fetch_seconds, cl, r["sym"], got_to, end))
            nreq += 1
            pace()
            got_to = end
        secs = merge_secs(chunks)
        xs, _trig, why, _held = arm_b_walk(r, secs, b1m, cfg, ring)
        if xs < got_to - 1 or got_to >= eod - 1e-6:
            return {"secs": secs, "b1m": b1m, "fetched_to": got_to, "requests": nreq, "exit_why": why}
    return {"secs": secs, "b1m": b1m, "fetched_to": got_to, "requests": nreq, "exit_why": why}


# ── statistics ───────────────────────────────────────────────────────────────

def dstat(rows, key):
    return W.dstat(rows, key)


def desk_cfg():
    import leash_replay as L
    sha = hashlib.sha256(open(os.path.join(ROOT, "config", "bot_config.json"), "rb").read()).hexdigest()
    if not sha.startswith(CONFIG_SHA_PREFIX):
        raise SystemExit(f"config sha {sha[:16]} != pinned {CONFIG_SHA_PREFIX} (amended_2 (3))")
    cfg = copy.deepcopy(L.load_config())
    cfg.pop("_min_hold", None)
    return cfg, max(2, int(cfg.get("ai_local_trail_print_ring", 3) or 3))


def ub_at(rows, key, p):
    s = W.dstat(rows, key)
    means = list(W.per_day(rows, key).values())
    if s["mean"] is None or len(means) < 2:
        return None
    se = statistics.stdev(means) / math.sqrt(len(means))
    return s["mean"] + W.E.t_ppf(p, len(means) - 1) * se


def module_shas():
    """Hash every module the walk and the sample actually import, not only this file."""
    d = os.path.dirname(os.path.abspath(__file__))
    paths = {m: os.path.join(d, f"{m}.py") for m in SHA_IN_STUDIES}
    paths["ai_positions"] = os.path.join(ROOT, "ai_positions.py")
    paths["bars"] = os.path.join(ROOT, "tools", "bars.py")
    paths["config"] = os.path.join(ROOT, "config.py")
    # trail_exit_trend_hold -> sell_double_check.live_value -> ai_entry_watch._live_percent_r_line
    paths["sell_double_check"] = os.path.join(ROOT, "tools", "sell_double_check.py")
    paths["ai_entry_watch"] = os.path.join(ROOT, "ai_entry_watch.py")
    missing = [m for m, p in paths.items() if not os.path.isfile(p)]
    if missing:
        raise SystemExit(f"module sha missing files: {missing}")
    return {m: hashlib.sha256(open(p, "rb").read()).hexdigest() for m, p in paths.items()}


def assert_sample_current(smp: dict | None) -> None:
    """Probe, fetch, count and score all refuse a sample built by older code."""
    if (smp or {}).get("module_sha256") != module_shas():
        raise SystemExit("sample.json was built by different code; rerun --sample")


def chosen_trades(smp: dict) -> list:
    """The day-stratified subset the probe's budget allows. Fetch, count and score all use this."""
    probe = jload("probe.json")
    if not probe or "n_half" not in probe:
        raise SystemExit("run --probe first")
    if not probe.get("projected_under_bar"):
        raise SystemExit("projected UNDERPOWERED inside the 9-hour budget; do not fetch")
    return select_trades(smp["trades"], int(probe["n_half"]))


def arm_half_verdict(mean, t, diff, drop_days_v, drop_names_v, n, n_days) -> str:
    """Under the trade or day floor the half is UNDERPOWERED. Arm B hits this when exit bids are missing."""
    if n < FLOOR_TRADES or n_days < FLOOR_DAYS:
        return "UNDERPOWERED"
    if (t or 0) <= -2:
        return "FAIL"
    drops_ok = (drop_days_v is not None and drop_days_v >= 0 and drop_names_v is not None and drop_names_v >= 0)
    if mean is not None and mean > 0 and (t or 0) >= T_ARM and diff is not None and diff >= 0 and drops_ok:
        return "PASS"
    return "NOT_PASSED"


def rollup(hv: list, ubs: list) -> str:
    """UB-fail only when both halves were large enough to score. A thin half is not evidence of a negative bound."""
    if "FAIL" in hv:
        return "FAIL"
    if all(x == "PASS" for x in hv):
        return "PASS"
    if (all(v != "UNDERPOWERED" for v in hv) and all(u is not None and u < 0 for u in ubs)):
        return "FAIL"
    return "UNDERPOWERED"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for f_ in ("--sample", "--probe", "--fetch", "--cuts", "--score"):
        ap.add_argument(f_, action="store_true")
    ap.add_argument("--n-half", type=int, default=N_MAX)
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    if a.sample:
        smp = build_sample(a.n_half)
        print(len(smp["trades"]), smp["stats"])
        jwrite("sample.json", {**smp, "module_sha256": module_shas()})
        return
    smp = jload("sample.json")
    assert_sample_current(smp)
    if a.probe:
        cl = client()
        cfg, ring = desk_cfg()
        a_times, b_times, a_reqs, b_reqs = [], [], [], []
        complete = attempts = 0
        for block in probe_blocks(len(smp["trades"])):
            for i in block:
                t = smp["trades"][i]
                attempts += 1
                try:
                    s0 = time.time()
                    got = fetch_one(cl, t)
                    a_times.append(time.time() - s0)
                    a_reqs.append(got.get("requests") or 0)
                except RuntimeError as e:
                    print(f"  probe skip {t.get('sym')}: {e}", flush=True)
                    continue
                if got.get("status") != "ok" or not got.get("ask_in") or got.get("T") is None:
                    continue
                try:
                    s1 = time.time()
                    b = fetch_arm_b(cl, {**t, **got}, cfg, ring)
                    b_times.append(time.time() - s1)
                    b_reqs.append(b.get("requests") or 0)
                except RuntimeError as e:
                    print(f"  probe skip arm B {t.get('sym')}: {e}", flush=True)
                    continue
                complete += 1
                break
        if complete < PROBE_MIN:
            raise SystemExit(f"probe finished both fetches on {complete} of {attempts} trades; need {PROBE_MIN}")
        halves = Counter(t["half"] for t in smp["trades"])
        sample_half = min(halves.get("A", 0), halves.get("B", 0))
        out = budget_n_half(statistics.mean(a_times), statistics.mean(b_times), sample_half)
        out["probe_complete"] = complete
        out["probe_attempts"] = attempts
        out["req_per_trade_A"] = statistics.mean(a_reqs)
        out["req_per_in_trade_B"] = statistics.mean(b_reqs)
        n_full = sample_half * 2
        out["est_requests_at_full_sample"] = (n_full * out["req_per_trade_A"]
                                               + n_full * IN_SHARE * out["req_per_in_trade_B"])
        out["est_hours_from_requests_at_full"] = out["est_requests_at_full_sample"] / REQ_PER_MIN / 60
        n = out["n_half"] * 2
        out["est_requests_at_n"] = n * out["req_per_trade_A"] + n * IN_SHARE * out["req_per_in_trade_B"]
        print(out)
        jwrite("probe.json", out)
        return
    chosen = chosen_trades(smp)
    if a.fetch:
        cl, done = client(), jload("fetched.json", {})
        failed = []
        for i, t in enumerate(chosen):
            key = f"{t['day']}|{t['sym']}|{t['k']}"
            if key in done:
                continue
            try:
                done[key] = fetch_one(cl, t)
            except RuntimeError:
                failed.append(key)
            if i % 100 == 0:
                jwrite("fetched.json", done)
                print(f"  {i}/{len(chosen)}", flush=True)
        jwrite("fetched.json", done)
        jwrite("fetch_log.json", {"failed": failed, "n_chosen": len(chosen)})
        print("failed", len(failed))
        return
    fetched = jload("fetched.json", {})
    if jload("fetch_log.json", {}).get("failed"):
        raise SystemExit("fetch failures (C4): rerun --fetch")
    import order_flow_pilot as OF
    rows, st = [], Counter()
    for t in chosen:
        f = fetched.get(f"{t['day']}|{t['sym']}|{t['k']}")
        if f is None:
            st["not_fetched"] += 1
            continue
        if f["status"] != "ok":
            st[f["status"]] += 1
            continue
        rows.append({**t, **f, "m": t["k"]})
    if any(v for k, v in st.items() if k == "not_fetched"):
        raise SystemExit(f"{st['not_fetched']} sampled trades not fetched (C4)")
    cut = OF.top_cut([(r["ofi60"], OF.tie_u(r)) for r in rows if r["half"] == "A" and r["ofi60"] is not None])
    cut_qi = OF.top_cut([(r["qi"], OF.tie_u(r)) for r in rows if r["half"] == "A" and r["qi"] is not None])
    for r in rows:
        g = OF.groups(r, {"ofi60": cut, "qi": cut_qi})
        r["g1"], r["g2"], r["g3"] = g["G1"], g["G2"], g["G3"]
        ok = r["ask_in"] and r["bid_out"]
        r["net_A"] = (r["bid_out"] / r["ask_in"] - 1) if ok else None
    if a.cuts:
        cfg, ring = desk_cfg()
        cl = client()
        store = os.path.join(OUT, "armB.pkl")
        data = pickle.load(open(store, "rb")) if os.path.exists(store) else {}
        for r in rows:
            if r["g1"] != "IN" or r["net_A"] is None:
                continue
            key = f"{r['day']}|{r['sym']}|{r['k']}"
            if key in data:
                continue
            data[key] = fetch_arm_b(cl, r, cfg, ring)
            if len(data) % 50 == 0:
                pickle.dump(data, open(store, "wb"))
        pickle.dump(data, open(store, "wb"))
        print("arm B paths", len(data))
        return
    half_rows = lambda h, g=None: [r for r in rows if r["half"] == h and (g is None or r["g1"] == g)  # noqa: E731
                                   and r["net_A"] is not None]
    if not a.score:
        cnt = {"stats": dict(st), "cut": cut, "cut_qi": cut_qi, "n_chosen": len(chosen),
               "dollar_10_20": "DROPPED: sample is entry price >= $20",
               "module_sha256": module_shas(), "prereg_commit": a.prereg_commit}
        for h in ("A", "B"):
            inn = half_rows(h, "IN")
            ctrl = [r for r in inn if r.get("ctrl_d") is not None]
            per = defaultdict(list)
            for r in ctrl:
                per[r["day"]].append(r["ctrl_d"])
            xs = [statistics.mean(v) for v in per.values()]
            mde = (T_ARM + 0.84) * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) >= 2 else None
            nd = len({r["day"] for r in inn})
            cnt[h] = {"in_trades": len(inn), "out_trades": len(half_rows(h, "OUT")),
                      "g2_in": sum(1 for r in rows if r["half"] == h and r["g2"] == "IN"),
                      "g3_in": sum(1 for r in rows if r["half"] == h and r["g3"] == "IN"),
                      "drops_by_reason": dict(Counter(
                          (fetched.get(f"{t['day']}|{t['sym']}|{t['k']}") or {}).get("status", "not_fetched")
                          for t in chosen if t["half"] == h)),
                      "missing_quote_by_side": dict(Counter(r["g1"] for r in rows if r["half"] == h
                                                            and r["net_A"] is None)),
                      "ask_below_trigger_share": (sum(1 for r in inn if r.get("ask_below_trigger")) / len(inn))
                      if inn else None,
                      "missing_flow": sum(1 for r in rows if r["half"] == h and r["g1"] is None), "days": nd,
                      "mde": mde, "floors_met": len(inn) >= FLOOR_TRADES and nd >= FLOOR_DAYS
                      and mde is not None and mde <= MDE_BAR}
        print(json.dumps(cnt, indent=1, default=str))
        print(jwrite("count.json", cnt))
        return
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    cnt = jload("count.json")
    if cnt.get("module_sha256") != module_shas() or not cnt.get("prereg_commit"):
        raise SystemExit("count.json from different code or without --prereg-commit")
    if not all(cnt[h]["floors_met"] for h in ("A", "B")):
        raise SystemExit("UNDERPOWERED at the count step: no outcome is read")
    import exit_capture as XC
    cfg, ring = desk_cfg()
    data = pickle.load(open(os.path.join(OUT, "armB.pkl"), "rb"))
    cl = client()
    spy_cache: dict = {}

    def opens_for(day):
        if day not in spy_cache:
            try:
                spy_cache[day] = spy_opens(day)
            except OSError:
                spy_cache[day] = {}
        return spy_cache[day]

    for r in rows:
        r["spy_A"] = spy_return(opens_for(r["day"]), et_minute(r["day"], r["T"]), et_minute(r["day"], r["x_ts"]))
    no_bid = Counter()
    for r in rows:
        key = f"{r['day']}|{r['sym']}|{r['k']}"
        if r["g1"] == "IN" and r["net_A"] is not None:
            if key not in data:
                raise SystemExit(f"arm-B path missing for {key} (C4): rerun --cuts")
            xs, trig, why, _held = arm_b_walk(r, data[key]["secs"], data[key]["b1m"], cfg, ring)
            if exit_beyond_fetch(xs, data[key]["fetched_to"], et_epoch(r["day"], EOD_MIN)):
                raise SystemExit(f"arm-B exit beyond the fetched prints for {key}")
            bid = XC.fetch_bid(cl, r["sym"], int(xs))
            if bid is None:
                no_bid[r["half"]] += 1
                r["net_B"] = None
                r["arm_b_drop"] = "no_bid"
            else:
                px = XC.exit_price(trig, bid, why)
                r["net_B"] = (px / r["ask_in"] - 1) if px else None
            r["spy_B"] = spy_return(opens_for(r["day"]), et_minute(r["day"], r["T"]), et_minute(r["day"], xs))
    out = {"arms": {}, "info": {}}
    for arm in ("A", "B"):
        key = f"net_{arm}"
        hv, ubs = [], []
        halves = {}
        for h in ("A", "B"):
            inn = [r for r in half_rows(h, "IN") if r.get(key) is not None]
            s = dstat(inn, lambda r, k=key: r[k])
            if arm == "A":
                o = dstat(half_rows(h, "OUT"), lambda r: r["net_A"])
                diff = (s["mean"] - o["mean"]) if s["mean"] is not None and o["mean"] is not None else None
            else:
                diff = 0.0
            dd = W.drop_days(inn, lambda r, k=key: r[k])
            dn = W.drop_names(inn, lambda r, k=key: r[k])
            ub98 = ub_at(inn, lambda r, k=key: r[k], UB_P)
            v = arm_half_verdict(s["mean"], s["t"], diff, dd, dn, len(inn), len({r["day"] for r in inn}))
            hv.append(v)
            ubs.append(ub98)
            io_t = None
            if arm == "A":
                both = W.per_day(half_rows(h, "IN"), lambda r: r["net_A"])
                other = W.per_day(half_rows(h, "OUT"), lambda r: r["net_A"])
                io_t = W.E.stats([both[d_] - other[d_] for d_ in both if d_ in other])["t"]
            halves[h] = {"verdict": v, "net": s, "ub_98_75": ub98, "in_minus_out": diff, "in_minus_out_t": io_t,
                         "drop3_days": dd, "drop5_names": dn, "n": len(inn)}
        out["arms"][arm] = {"verdict": rollup(hv, ubs), "halves": halves}
    out["arms"]["B"]["no_bid_drops"] = {"A": no_bid["A"], "B": no_bid["B"]}
    out["verdict"] = ("PASS" if any(v["verdict"] == "PASS" for v in out["arms"].values()) else
                      "FAIL" if all(v["verdict"] == "FAIL" for v in out["arms"].values()) else "UNDERPOWERED")
    for h in ("A", "B"):
        allr = [r for r in rows if r["half"] == h and r["net_A"] is not None]

        def side_net(side_key, side, net_key="net_A", h=h):
            pool = [r for r in rows if r["half"] == h and r.get(side_key) == side and r.get(net_key) is not None]
            return {"n": len(pool), "net": dstat(pool, lambda r, k=net_key: r[k])["mean"]}

        def side_spread(side, bid_k, ask_k, h=h):
            pool = [r for r in rows if r["half"] == h and r.get("g1") == side]
            return median_of([spread_bp(r.get(bid_k), r.get(ask_k)) for r in pool])

        hedged_a = [r for r in half_rows(h, "IN") if r.get("spy_A") is not None]
        hedged_b = [r for r in half_rows(h, "IN") if r.get("net_B") is not None and r.get("spy_B") is not None]
        out["info"][h] = {"all_sample_net_A": dstat(allr, lambda r: r["net_A"])["mean"],
                          "out_net_A": dstat(half_rows(h, "OUT"), lambda r: r["net_A"])["mean"],
                          "in_bar_gross": dstat(half_rows(h, "IN"), lambda r: r["bar_gross"])["mean"],
                          "in_net_entry_T3": dstat([r for r in half_rows(h, "IN") if r.get("ask_T3")],
                                                   lambda r: r["bid_out"] / r["ask_T3"] - 1)["mean"],
                          "in_net_entry_T11": dstat([r for r in half_rows(h, "IN") if r.get("ask_T11")],
                                                    lambda r: r["bid_out"] / r["ask_T11"] - 1)["mean"],
                          "out_bar_gross": dstat(half_rows(h, "OUT"), lambda r: r["bar_gross"])["mean"],
                          "g2_in": side_net("g2", "IN"), "g2_out": side_net("g2", "OUT"),
                          "g3_in": side_net("g3", "IN"), "g3_out": side_net("g3", "OUT"),
                          "spread_bp_median_by_side": {
                              "IN": {"entry": side_spread("IN", "bid_in", "ask_raw"),
                                     "exit": side_spread("IN", "bid_out", "ask_out")},
                              "OUT": {"entry": side_spread("OUT", "bid_in", "ask_raw"),
                                      "exit": side_spread("OUT", "bid_out", "ask_out")}},
                          "spy_hedged_g1_in": {
                              "A": dstat(hedged_a, lambda r: r["net_A"] - r["spy_A"])["mean"],
                              "B": dstat(hedged_b, lambda r: r["net_B"] - r["spy_B"])["mean"],
                              "drops_A": sum(1 for r in half_rows(h, "IN") if r.get("spy_A") is None),
                              "drops_B": sum(1 for r in half_rows(h, "IN")
                                             if r.get("net_B") is not None and r.get("spy_B") is None)}}
    out["info"]["dollar_10_20"] = "DROPPED: the sample is entry price >= $20, so the $10-$20 cell has no rows"
    out["declared"] = ["arm B: one pinned config (sha 837dfd79268ebf16), R = entry x 0.05009, spread_r = quoted "
                       "spread / R", "T = first round-lot print >= stop + 1c; entry = max(ask in [T-3s, T+1s], "
                       "trigger price)", "empty SIP condition lists are regular round lots for the trigger; "
                       "OFI60 still drops them", "G2, G3, quoted entry/exit spread by side, and SPY-hedged NET "
                       "(warehouse SIP open-to-open, split-adjusted) are information only",
                       "the $10-$20 cell is dropped: the sample is >= $20",
                       "the T2 and G1 ingredients were chosen on these sessions: a PASS is a re-read until "
                       "warehouse_confirm"]
    out["skeptic_ready"], out["module_sha256"] = a.skeptic_ready, module_shas()
    jwrite("result.json", out)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
