#!/usr/bin/env python3
"""Buy-stop entries filtered by buyer-initiated order flow, at quoted cost; arm A = %R turn-down exit, arm B = the
desk's live exit stack. Spec: docs/studies/buystop_flow_prereg.json (main + amended).

Phases (each resumable, data-only account for every fetch):
  --sample   REFUSED. The frozen 7,975 + 7,975 keys are not rebuilt.
  --probe    REFUSED. probe.json n_half stays 7,975.
  --fetch    REFUSED. Frozen fetched.json is not rewritten.
  --build-addon   ten warehouse sessions 2026-04-27 .. 2026-05-08 into addon_rows.json (no Alpaca)
  --fetch-addon   arm-A quotes for those rows only, at most 145 a day, into addon_fetched.json
  --cuts     arm-B prints for the enlarged G1-IN set, only after both halves have floors_met
  (default)  enlarged count (outcome-blind)   --score  only if both halves then have floors_met
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
# amended_5_power: ten sessions before the frozen sample. 2026-04-27 has no previous warehouse session.
ADDON_DAYS = (
    "2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-01",
    "2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07", "2026-05-08",
)
ADDON_CAP = 145
FROZEN_N_HALF = 7975
FROZEN_OFI = (0.2882797731568998, 0.057809488368586855)
FROZEN_QI = (0.2, 0.7704854285098521)
FROZEN_B_TRADES, FROZEN_B_DAYS, FROZEN_B_MDE_BP = 2319, 55, 2.439
ADDON_ROWS_NAME = "addon_rows.json"
ADDON_FETCHED_NAME = "addon_fetched.json"
PINNED_CFG_NAME = "pinned_bot_config_837dfd79.json"
HELD_OUT_PREV, WRAP_PREV = "2026-04-24", "2026-10-15"
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
    """Not used. The frozen 7,975 + 7,975 keys stay in sample.json."""
    raise SystemExit("refusing build_sample: the frozen sample is not rebuilt")
    import indicator_confirm_hist as H  # noqa: F401
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
    """Retired. Opening bot_config.json here would refuse the live book and could rewrite the pin."""
    raise SystemExit("desk_cfg is not used; arm B reads the pinned file and does not open bot_config.json")


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


def assert_sample_current(smp: dict | None) -> str | None:
    """Keep a frozen sample that still has its trades when module_shas changes.

    module_shas covers ai_entry_watch.py and config.py. Both changed on 2026-10-30.
    A hash mismatch is declared and the 7,975 + 7,975 keys are not rebuilt.
    A missing sample, or a sample with no trades, still refuses and is not rebuilt.
    """
    trades = smp.get("trades") if isinstance(smp, dict) else None
    if not trades:
        raise SystemExit("sample.json has no trades; the frozen sample is not rebuilt")
    if smp.get("module_sha256") != module_shas():
        return ("frozen sample kept: module_sha256 differs from current module_shas "
                "(covers ai_entry_watch.py and config.py, both changed 2026-10-30); "
                f"the frozen {FROZEN_N_HALF}+{FROZEN_N_HALF} keys are not rebuilt")
    return None


def prev_warehouse_session(day: str, days: list[str]) -> str | None:
    """Prior session in the sorted warehouse list. No wrap.

    2026-04-27 is defined to have none, so it is not warmed on 2026-10-15 or on 2026-04-24.
    A previous session before 2026-04-27, the held-out day, or the wrap day is refused.
    """
    if day == ADDON_DAYS[0]:
        return None
    ordered = sorted(days)
    if day not in ordered:
        return None
    i = ordered.index(day)
    if i == 0:
        return None
    prev = ordered[i - 1]
    if prev < ADDON_DAYS[0] or prev in (HELD_OUT_PREV, WRAP_PREV):
        raise SystemExit(f"refusing previous session {prev} for {day}")
    return prev


def cap_addon_day(rows: list, day: str, cap: int = ADDON_CAP) -> list:
    """Sort by (sym, entry minute), seeded shuffle, prefix. A short day is not backfilled."""
    pick = sorted(rows, key=lambda r: (r["sym"], r["k"]))
    seeded("bsf-sample", day).shuffle(pick)
    return pick[:cap]


def assert_addon_cap(trades: list) -> None:
    by = Counter(t["day"] for t in trades)
    over = sorted(d for d, n in by.items() if n > ADDON_CAP)
    if over:
        raise SystemExit(f"addon day over {ADDON_CAP}: {over}")
    extra = sorted(set(by) - set(ADDON_DAYS))
    if extra:
        raise SystemExit(f"addon day outside the ten sessions: {extra}")


def _sipf_of(raw) -> dict:
    return {int(x[0]): (float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in raw}


def _oc_of(sipf: dict) -> dict:
    return {m: (v[0], v[3]) for m, v in sipf.items()}


def addon_row(day: str, sym: str, e: int, px: float, x_min: int, stop, sip_oc: dict) -> dict | None:
    """One T2 fill. Drops a missing stop, a price under $20, and a missing bar gross."""
    if stop is None or px is None or px < MIN_PX:
        return None
    g = B2.gross(sip_oc, e, px, x_min)
    if g is None:
        return None
    hold = x_min - e
    return {"day": day, "sym": sym, "half": "A", "k": e, "px_bar": px, "stop": stop, "exit_min": x_min,
            "bar_gross": g, "ctrl_d": W.d_control(sip_oc, sym, day, e, hold, "b2-T2-d")}


def build_addon_day(day: str, days: list[str], load_raw) -> list:
    """T2 fills for one of the ten sessions. 2026-04-27 returns no trades and loads nothing."""
    if day == ADDON_DAYS[0]:
        return []
    if day not in ADDON_DAYS:
        raise SystemExit(f"{day} is outside the ten-day block")
    prev = prev_warehouse_session(day, days)
    if prev is None:
        raise SystemExit(f"{day} has no previous warehouse session")
    sip_raw = load_raw("sip", day)
    sip_prev = load_raw("sip", prev)
    iex = load_raw("iex", day)
    iex_prev = load_raw("iex", prev)
    rows = []
    for sym in sorted(sip_raw):
        raw = sip_raw[sym]
        if sym in sip_prev and len(sip_prev[sym]) and len(raw):
            pc, op = float(sip_prev[sym][-1][4]), float(raw[0][1])
            if pc > 0 and abs(op / pc - 1) > W.SPLIT_JUMP:
                continue
        if sym not in iex or sym not in iex_prev:
            continue
        W.REAL.clear()
        fast, slow = W.lines(iex_prev[sym], iex[sym])
        sipf = _sipf_of(raw)
        iexf = _sipf_of(iex[sym])
        for e, px, x_min, stop in t2_with_stops(fast, slow, sipf, iexf):
            row = addon_row(day, sym, e, px, x_min, stop, _oc_of(sipf))
            if row is not None:
                rows.append(row)
    return cap_addon_day(rows, day)


def build_addon_rows(days: list[str] | None = None, load_raw=None) -> dict:
    """The ten sessions only. Does not call batch2.main and does not write batch2/rows.json."""
    if W.FIRST != "2026-05-11":
        raise SystemExit("wr_trend_trade.FIRST must stay 2026-05-11")
    import indicator_confirm_hist as H
    if days is None:
        days = H.warehouse_days()
    if load_raw is None:
        load_raw = H.load_raw

    def guarded(feed, day):
        if day < ADDON_DAYS[0] or day in (HELD_OUT_PREV, WRAP_PREV):
            raise SystemExit(f"refusing to open {day}")
        return load_raw(feed, day)

    trades = []
    for day in ADDON_DAYS:
        trades.extend(build_addon_day(day, days, guarded))
    assert_addon_cap(trades)
    return {"trades": trades, "days": list(ADDON_DAYS), "cap": ADDON_CAP}


def pinned_arm_b_cfg(path: str | None = None):
    """DEFAULT_CONFIG updated by the pinned file so the pin wins. Drops _min_hold.

    Does not call desk_cfg, load_config, or load_config_ro, and does not open bot_config.json.
    """
    import config as cfgmod
    p = path or os.path.join(OUT, PINNED_CFG_NAME)
    raw = open(p, "rb").read()
    sha = hashlib.sha256(raw).hexdigest()
    if not sha.startswith(CONFIG_SHA_PREFIX):
        raise SystemExit(f"pinned config sha {sha[:16]} != {CONFIG_SHA_PREFIX}")
    pin = json.loads(raw.decode())
    if not isinstance(pin, dict):
        raise SystemExit("pinned config is not an object")
    cfg = copy.deepcopy(cfgmod.DEFAULT_CONFIG)
    cfg.update(pin)
    cfg.pop("_min_hold", None)
    ring = max(2, int(cfg.get("ai_local_trail_print_ring", 3) or 3))
    return cfg, ring


def frozen_intersection(trades: list, fetched: dict) -> tuple[list, list]:
    """Sample order. A missing key is a C4 refusal. Status other than ok is skipped."""
    a, b, missing = [], [], []
    for t in trades:
        key = f"{t['day']}|{t['sym']}|{t['k']}"
        if key not in fetched:
            missing.append(key)
            continue
        got = fetched[key]
        if got.get("status") != "ok":
            continue
        row = {**t, **got, "m": t["k"]}
        (a if t.get("half") == "A" else b).append(row)
    if missing:
        raise SystemExit(f"{len(missing)} frozen trades not fetched (C4)")
    return a, b


def addon_fetched_or_refuse(trades: list, fetched: dict) -> list:
    """Addon rows with a fetch. A missing key is a C4 refusal, not a silent drop."""
    missing, rows = [], []
    for t in trades:
        key = f"{t['day']}|{t['sym']}|{t['k']}"
        got = fetched.get(key)
        if got is None:
            missing.append(key)
            continue
        if got.get("status") != "ok":
            continue
        rows.append({**t, **got, "m": t["k"]})
    if missing:
        raise SystemExit(f"{len(missing)} addon trades not fetched (C4)")
    return rows


def _pair(got) -> tuple | None:
    if not got or len(got) != 2:
        return None
    return (float(got[0]), float(got[1]))


def frozen_cuts_or_refuse(original_half_a: list) -> None:
    """top_cut on the original half-A rows only. Refuse unless it is the frozen pair."""
    import order_flow_pilot as OF
    for r in original_half_a:
        r["m"] = r["k"]
    cut = OF.top_cut([(r["ofi60"], OF.tie_u(r)) for r in original_half_a if r.get("ofi60") is not None])
    cut_qi = OF.top_cut([(r["qi"], OF.tie_u(r)) for r in original_half_a if r.get("qi") is not None])
    if _pair(cut) != FROZEN_OFI or _pair(cut_qi) != FROZEN_QI:
        raise SystemExit("top_cut on the original half-A rows is not the frozen pair")


def label_rows(rows: list) -> None:
    """groups() at the frozen pairs. tie_u reads field m, the fill minute k."""
    import order_flow_pilot as OF
    for r in rows:
        r["m"] = r["k"]
        g = OF.groups(r, {"ofi60": FROZEN_OFI, "qi": FROZEN_QI})
        r["g1"], r["g2"], r["g3"] = g["G1"], g["G2"], g["G3"]
        ask, bid = r.get("ask_in"), r.get("bid_out")
        r["net_A"] = (bid / ask - 1) if ask and bid else None


def assemble_labeled(frozen_a: list, addon_rows: list, frozen_b: list) -> list:
    """Addon rows sit after the frozen half-A keys. Half B is the frozen half only."""
    frozen_cuts_or_refuse(frozen_a)
    rows = list(frozen_a) + list(addon_rows) + list(frozen_b)
    label_rows(rows)
    return rows


def control_mde(inn: list) -> float | None:
    """(2.24 + 0.84) * stdev(day means of ctrl_d) / sqrt(days that have a mean)."""
    per = defaultdict(list)
    for r in inn:
        if r.get("net_A") is None or r.get("ctrl_d") is None:
            continue
        per[r["day"]].append(r["ctrl_d"])
    xs = [statistics.mean(v) for v in per.values()]
    if len(xs) < 2:
        return None
    return (T_ARM + 0.84) * statistics.stdev(xs) / math.sqrt(len(xs))


def half_public(rows: list, half: str) -> dict:
    inn = [r for r in rows if r.get("half") == half and r.get("g1") == "IN" and r.get("net_A") is not None]
    mde = control_mde(inn)
    nd = len({r["day"] for r in inn})
    floors = len(inn) >= FLOOR_TRADES and nd >= FLOOR_DAYS and mde is not None and mde <= MDE_BAR
    return {"in_trades": len(inn), "days": nd, "mde": mde, "floors_met": floors}


def half_b_fingerprint(block: dict) -> None:
    """Half B stays the frozen count. A mismatch refuses the run."""
    mde = block.get("mde")
    bp = None if mde is None else round(float(mde) * 10000, 3)
    if not (block.get("in_trades") == FROZEN_B_TRADES and block.get("days") == FROZEN_B_DAYS
            and bp == FROZEN_B_MDE_BP and block.get("floors_met") is True):
        raise SystemExit("half B is not the frozen count (2319 trades, 55 days, 2.439 bp, floors_met true)")


def count_report(block_a: dict, block_b: dict, declaration: str | None, prereg_commit: str) -> dict:
    """Trade counts, day counts, the one MDE number, and floors_met. Nothing else from the returns."""
    keep = ("in_trades", "days", "mde", "floors_met")
    pub = {"A": {k: block_a[k] for k in keep}, "B": {k: block_b[k] for k in keep},
           "module_sha256": module_shas(), "prereg_commit": prereg_commit}
    half_b_fingerprint(pub["B"])
    if declaration:
        pub["sample_kept"] = declaration
    return pub


def _refuse_count_leak(text: str) -> None:
    for banned in ("net_A", "net_B", "bar_gross", "ctrl_d", "day_means"):
        if banned in text:
            raise SystemExit("count report would print a return")


def fetch_addon() -> None:
    """Arm-A quotes for the ten-day rows only. Does not write fetched.json."""
    blob = jload(ADDON_ROWS_NAME)
    if not blob or "trades" not in blob:
        raise SystemExit("addon_rows.json missing; run --build-addon")
    trades = blob["trades"]
    assert_addon_cap(trades)
    cl, done = client(), jload(ADDON_FETCHED_NAME, {}) or {}
    failed = []
    for i, t in enumerate(trades):
        key = f"{t['day']}|{t['sym']}|{t['k']}"
        if key in done:
            continue
        try:
            done[key] = fetch_one(cl, t)
        except RuntimeError:
            failed.append(key)
        if i % 100 == 0:
            jwrite(ADDON_FETCHED_NAME, done)
            print(f"  {i}/{len(trades)}", flush=True)
    jwrite(ADDON_FETCHED_NAME, done)
    jwrite("addon_fetch_log.json", {"failed": failed, "n": len(trades)})
    print(json.dumps({"failed": len(failed), "n": len(trades)}))


def chosen_trades(smp: dict) -> list:
    """Retired. select_trades would drop frozen keys once the ten days change the day count."""
    raise SystemExit("refusing chosen_trades: frozen keys are not passed through select_trades")
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


def _print_count(pub: dict) -> None:
    text = json.dumps(pub, indent=1, default=str)
    _refuse_count_leak(text)
    print(text)
    print(jwrite("count.json", pub))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for f_ in ("--sample", "--probe", "--fetch", "--cuts", "--score", "--build-addon", "--fetch-addon"):
        ap.add_argument(f_, action="store_true")
    ap.add_argument("--n-half", type=int, default=N_MAX)
    ap.add_argument("--skeptic-ready", default="")
    ap.add_argument("--prereg-commit", default="")
    a = ap.parse_args()
    if a.sample:
        raise SystemExit("refusing --sample: the frozen sample is not rebuilt")
    if a.probe:
        raise SystemExit("refusing --probe: probe.json n_half stays 7975 and is not rewritten")
    if a.fetch:
        raise SystemExit("refusing --fetch: frozen fetched.json is not rewritten; use --fetch-addon")
    if a.build_addon:
        built = build_addon_rows()
        jwrite(ADDON_ROWS_NAME, built)
        by = Counter(t["day"] for t in built["trades"])
        print(json.dumps({"trades": len(built["trades"]), "days": len(ADDON_DAYS), "cap": ADDON_CAP,
                          "by_day": {d: by.get(d, 0) for d in ADDON_DAYS}}))
        return
    if a.fetch_addon:
        fetch_addon()
        return
    smp = jload("sample.json")
    declaration = assert_sample_current(smp)
    addon = jload(ADDON_ROWS_NAME)
    if not addon or "trades" not in addon:
        raise SystemExit("addon_rows.json missing; run --build-addon")
    assert_addon_cap(addon["trades"])
    if jload("addon_fetch_log.json", {}).get("failed"):
        raise SystemExit("addon fetch failures (C4): rerun --fetch-addon")
    frozen_a, frozen_b = frozen_intersection(smp["trades"], jload("fetched.json", {}) or {})
    addon_rows = addon_fetched_or_refuse(addon["trades"], jload(ADDON_FETCHED_NAME, {}) or {})
    rows = assemble_labeled(frozen_a, addon_rows, frozen_b)
    pub = count_report(half_public(rows, "A"), half_public(rows, "B"), declaration, a.prereg_commit)
    _print_count(pub)
    if not a.score and not a.cuts:
        return
    if not all(pub[h]["floors_met"] for h in ("A", "B")):
        raise SystemExit("UNDERPOWERED at the count step: no outcome is read")
    if a.cuts:
        cfg, ring = pinned_arm_b_cfg()
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
    if not a.skeptic_ready:
        raise SystemExit("--score needs --skeptic-ready")
    if pub.get("module_sha256") != module_shas() or not pub.get("prereg_commit"):
        raise SystemExit("count.json from different code or without --prereg-commit")
    import exit_capture as XC
    cfg, ring = pinned_arm_b_cfg()
    half_rows = lambda h, g=None: [r for r in rows if r["half"] == h and (g is None or r["g1"] == g)  # noqa: E731
                                   and r["net_A"] is not None]
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
