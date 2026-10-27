#!/usr/bin/env python3
"""Score the operator's calls through the desk's own exits (operator 10/19: "build my calling correctly").

Each "Log call" click becomes a paper position:
  entry   the first SIP round-lot print (>= 100 shares) after the click, premarket included (operator: exits
          start from the click); no print within ENTRY_WAIT_S -> no fill, counted
  exits   the replay's mirror of the desk's exit code, tools/replay_session.FakeBroker._live_exit (local trail + ratchet leash, the
          trail_dc trend hold, T1 scale, profit capture, dead trade, the 15:50 flatten), with the live config,
          run on SIP prints sampled every TICK_S on a simulated clock. The trend hold reads SIP 1-minute bars that
          had completed by that moment (never a later bar).
  exit    fills at the first SIP print strictly after the exit decision
  cost    the full SIP quoted spread at the click, charged once (as the desk's replay costing); no quote at the
          click -> NO_QUOTE_BP, flagged spread_known false
  not modelled: stop-behind-fill and broker-side order behaviour (the live apply_local_trail extras). Open calls
          are re-scored with the config of each pass; final calls are frozen.
  size    $1,000 paper, for the panel's dollar figure
  lines   asis   = the live config exactly. Its breakeven floor is the fill + $0.01 (ai_breakeven_offset_px) once a
                   trade is up 0.15%; on a $2 name that floor is +0.5%, above the price, so the next tick sells it.
                   The desk never meets this (its $10 floor); the operator's calls are mostly $1-3 names.
          scaled = the same exits with that offset set to BE_SCALED_PCT of the fill (1 cent at $10, 0.2 cents at $2).

Never places, changes or cancels an order. Runs as its own process: it patches the clock inside ai_positions,
which must never happen inside the live desk or dashboard.

  TH_DATA_SECRETS=$PWD/secrets_data.json .venv/bin/python tools/call_exits.py            # score pending calls
  TH_DATA_SECRETS=$PWD/secrets_data.json .venv/bin/python tools/call_exits.py --report   # table of all calls
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time as _real_time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("REPO") or Path(__file__).resolve().parent.parent)
for p in (ROOT, ROOT / "tools", ROOT / "tools" / "studies"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

ET = ZoneInfo("America/New_York")
CALLS = ROOT / "ai_reports" / "my_calls.jsonl"
RESULTS = ROOT / "ai_reports" / "my_calls_exits.json"
NOTIONAL = 1000.0
TICK_S = 2.0
ROUND_LOT = 100
ENTRY_WAIT_S = 120.0
SIP_DELAY_S = 16 * 60          # free plan: SIP is queryable about 15 minutes after the fact
DEDUPE_S = 300
NO_QUOTE_BP = 20.0             # cost when there is no NBBO at the click (the old panel's 0.20%), flagged
RESCORE_S = 300                # an open call is re-scored at most every 5 minutes
CACHE_DIR = ROOT / "ai_reports" / "call_exits_cache"                 # repeat clicks on one name within 5 minutes count once
SESSION_END_MIN = 16 * 60
BE_SCALED_PCT = 0.10           # "scaled" line: breakeven offset = 0.10% of the fill instead of the live $0.01
VARIANTS = ("asis", "scaled")


# ── pure parts (tested) ──────────────────────────────────────────────────────

def sample_ticks(trades: list[tuple[float, float, float]], step: float = TICK_S) -> list[tuple[float, float]]:
    """Last round-lot print in each `step`-second bucket, in time order. trades: (ts, price, size)."""
    out: dict[int, tuple[float, float]] = {}
    for ts, px, sz in sorted(trades):
        if sz >= ROUND_LOT and px > 0:
            out[int(ts // step)] = (ts, px)
    return [out[k] for k in sorted(out)]


def first_print_after(trades: list[tuple[float, float, float]], t: float, wait: float | None = None):
    """(ts, price) of the first round-lot print strictly after t (within `wait` seconds if given), or None."""
    for ts, px, sz in sorted(trades):
        if ts > t and sz >= ROUND_LOT and px > 0:
            if wait is not None and ts - t > wait:
                return None
            return ts, px
    return None


def bars_upto(bars: list[tuple[float, float, float, float]], now: float):
    """1-minute bars (start ts, high, low, close) that had COMPLETED by `now`, in the trend hold's tuple format
    (stamps, highs, lows, closes); None under 20 bars (the desk sells rather than hold without bars)."""
    done = [b for b in bars if b[0] + 60 <= now]
    if len(done) < 20:
        return None
    return ([b[0] for b in done], [b[1] for b in done], [b[2] for b in done], [b[3] for b in done])


def dedupe(calls: list[dict]) -> list[dict]:
    kept, last = [], {}
    for r in sorted(calls, key=lambda r: float(r["ts"])):
        if r.get("retroactive"):
            continue
        if float(r["ts"]) - last.get(r["symbol"], -1e18) >= DEDUPE_S:
            kept.append(r)
            last[r["symbol"]] = float(r["ts"])
    return kept


def call_key(r: dict) -> str:
    return f"{r['symbol']}|{r['ts']}"


class Clock:
    def __init__(self, t: float = 0.0):
        self.t = t


class TimeProxy:
    """Stands in for the time module inside ai_positions (decay leash, dead trade, trend hold read time.time())."""

    def __init__(self, clock: Clock):
        self._c = clock

    def time(self):
        return self._c.t

    def monotonic(self):
        return self._c.t

    def sleep(self, _s):
        return None

    def __getattr__(self, name):
        return getattr(_real_time, name)


def variant_cfg(cfg: dict, variant: str, entry: float) -> dict:
    if variant == "scaled":
        return {**cfg, "ai_breakeven_offset_px": round(entry * BE_SCALED_PCT / 100.0, 6)}
    return cfg


def t1_info(p: dict) -> dict:
    """The T1 scale-out state: the share sold at target_1 when t1_hit (exit_r's blend)."""
    return {"t1_hit": bool(p.get("t1_hit")), "t1_px": float(p.get("target_1") or 0.0),
            "scale": float(p.get("scale") or 0.0)}


def blended_bp(entry: float, exit_px: float, res: dict) -> float:
    """Long return in bp with the T1 blend: `scale` of the shares at t1_px when T1 was hit, the rest at exit_px."""
    full = (exit_px / entry - 1) * 1e4
    if res.get("t1_hit") and res.get("t1_px"):
        sc = float(res.get("scale") or 0.0)
        return sc * (float(res["t1_px"]) / entry - 1) * 1e4 + (1 - sc) * full
    return full


def simulate(sym: str, click_ts: float, trades: list[tuple[float, float, float]],
             bars: list[tuple[float, float, float, float]], cfg: dict, horizon: float,
             spread_bp: float | None = None, variant: str = "asis") -> dict:
    """One call through the desk's exits on prints up to `horizon`. Returns status no_fill / open / final.
    spread_bp (the quoted spread at the click) is recorded on the position as the live desk records its entry
    spread, so the spread-aware trail rules see it."""
    import ai_positions as cp
    import replay_session as RS
    fill = first_print_after(trades, click_ts, ENTRY_WAIT_S)
    if fill is None:
        return {"status": "no_fill"}
    entry_ts, entry = fill
    cfg = variant_cfg(cfg, variant, entry)
    clock = Clock(entry_ts)
    old_time, old_bars, old_dc = cp.time, cp._double_check_store_bars, cp._symbol_bars_for_double_check
    cp.time = TimeProxy(clock)
    cp._double_check_store_bars = lambda _ticker: bars_upto(bars, clock.t)
    # double_check_sell and no_progress's double check read this one; unpatched it falls back to a fetch with no
    # end time (the whole day: look-ahead).
    cp._symbol_bars_for_double_check = lambda _ticker, _t=None: bars_upto(bars, clock.t)
    try:
        p = RS.open_position(cp, cfg, entry, entry_ts, None)
        if p is None:
            return {"status": "no_fill", "why": "no position"}
        if spread_bp is not None and float(p["risk_per_share"]) > 0:
            sr = spread_bp / 1e4 * entry / float(p["risk_per_share"])
            p["spread_r"] = sr
            p.setdefault("features", {})["spread_r"] = sr
        pos = {"symbol": sym, "entry_ts": entry_ts, "entry_px": entry, "pos": p, "lob": {"symbol": sym, "indicator": {}}}
        ticks = [t for t in sample_ticks(trades) if entry_ts < t[0] <= horizon]
        last_px, peak = entry, entry
        for ts, px in ticks:
            clock.t = ts
            last_px, peak = px, max(peak, px)
            why = RS.FakeBroker._live_exit(None, pos, px, ts, {"tickers": []}, cfg, None)
            if why:
                ex = first_print_after([t for t in trades if t[0] <= horizon], ts)
                if ex is None:
                    return {"status": "open", "entry": entry, "entry_ts": entry_ts, "mark": px,
                            "pending_exit": why, "peak": peak, **t1_info(p)}
                ets, epx = ex
                r = RS.exit_r(p, epx)
                ret = r * float(p["risk_per_share"]) / entry
                return {"status": "final", "entry": entry, "entry_ts": entry_ts, "exit": epx, "exit_ts": ets,
                        "reason": why, "gross_bp": round(ret * 1e4, 1), "peak": peak,
                        "mfe_bp": round((peak / entry - 1) * 1e4, 1), **t1_info(p)}
        return {"status": "open", "entry": entry, "entry_ts": entry_ts, "mark": last_px, "peak": peak, **t1_info(p)}
    finally:
        cp.time, cp._double_check_store_bars, cp._symbol_bars_for_double_check = old_time, old_bars, old_dc


# ── data ─────────────────────────────────────────────────────────────────────

def _client():
    path = os.environ.get("TH_DATA_SECRETS", "")
    if not path or not os.path.exists(path):
        sys.exit("REFUSING: set TH_DATA_SECRETS to the data-only account's secrets_data.json")
    import bars as B
    cl = B.client()
    if cl is None:
        sys.exit("REFUSING: the data-only client did not connect")
    return cl


def fetch_trades(cl, sym: str, start: float, end: float) -> list[tuple[float, float, float]]:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockTradesRequest
    r = cl.get_stock_trades(StockTradesRequest(
        symbol_or_symbols=sym, feed=DataFeed.SIP,
        start=datetime.fromtimestamp(start, timezone.utc), end=datetime.fromtimestamp(end, timezone.utc)))
    rows = (r.data or {}).get(sym) or []
    return [(t.timestamp.timestamp(), float(t.price), float(t.size)) for t in rows]


def fetch_bars(cl, sym: str, day: str, end: float) -> list[tuple[float, float, float, float]]:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    df = cl.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=sym, timeframe=TimeFrame(1, TimeFrameUnit.Minute), feed=DataFeed.SIP,
        start=d.replace(hour=4).astimezone(timezone.utc), end=datetime.fromtimestamp(end, timezone.utc))).df
    if df is None or df.empty:
        return []
    if "symbol" in (df.index.names or []):
        df = df.xs(sym, level="symbol")
    return [(t.timestamp(), float(h), float(lo), float(c)) for t, h, lo, c in zip(df.index, df["high"], df["low"], df["close"])]


def load_calls(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("symbol") and r.get("ts"):
            out.append(r)
    return dedupe(out)


def finalize_at_session_end(res: dict, horizon: float) -> dict:
    """An open run when the data reaches 16:00: final at the last print. A pending exit decision keeps its reason."""
    return {**res, "status": "final", "reason": res.get("pending_exit") or "session_end", "exit": res["mark"],
            "exit_ts": horizon, "gross_bp": round(blended_bp(res["entry"], res["mark"], res), 1)}


def after_hours(click: float) -> bool:
    """A click after 16:00 ET or on a weekend has no session left to trade in."""
    d = datetime.fromtimestamp(click, ET)
    return d.weekday() >= 5 or d.hour * 60 + d.minute >= SESSION_END_MIN


def cached_trades(cl, sym: str, key: str, click: float, horizon: float) -> list[tuple[float, float, float]]:
    """Trades from the click to `horizon`, fetching only what is newer than the cached part."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / (key.replace("|", "_").replace("/", "_") + ".json")
    have = json.loads(path.read_text()) if path.exists() else {"through": click, "trades": []}
    if horizon > have["through"]:
        new = fetch_trades(cl, sym, have["through"], horizon)
        have["trades"] += [list(t) for t in new if t[0] > have["through"]]
        have["through"] = horizon
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(have))
        tmp.replace(path)
    return [tuple(t) for t in have["trades"] if t[0] <= horizon]


def score_pending(now: float | None = None) -> dict:
    import config
    import replay_costing as RC
    now = float(now if now is not None else _real_time.time())
    results = json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}
    cfg = config.load_config_ro()
    cl, qcache, box = None, None, {}
    for r in load_calls(CALLS):
        key = call_key(r)
        prev = results.get(key, {})
        if prev.get("status") in ("final", "no_fill", "after_hours"):
            continue
        if prev.get("status") == "open" and now - float(prev.get("scored_at") or 0) < RESCORE_S:
            continue
        click = float(r["ts"])
        if after_hours(click):
            results[key] = {"status": "after_hours", "symbol": r["symbol"], "ts": click, "setup": r.get("setup")}
            continue
        day = datetime.fromtimestamp(click, ET).strftime("%Y-%m-%d")
        session_end = datetime.strptime(day, "%Y-%m-%d").replace(hour=16, tzinfo=ET).timestamp()
        horizon = min(session_end, now - SIP_DELAY_S)
        if horizon <= click:
            results[key] = {"status": "waiting"}
            continue
        if cl is None:
            cl, qcache = _client(), RC.load_cache()
        try:
            trades = cached_trades(cl, r["symbol"], key, click, horizon)
            bars = fetch_bars(cl, r["symbol"], day, horizon)
            sp_q = RC.entry_spread_bp(qcache, r["symbol"], click, box)
        except Exception as e:  # noqa: BLE001 - one bad request must not cost the other calls their pass
            print(f"[call_exits] {key}: {type(e).__name__} {str(e)[:160]}", file=sys.stderr)
            continue
        sp = NO_QUOTE_BP if sp_q is None else sp_q
        out = {"symbol": r["symbol"], "ts": click, "setup": r.get("setup"), "scored_at": now,
               "spread_bp": round(sp, 1), "spread_known": sp_q is not None,
               "session": "pre" if (datetime.fromtimestamp(click, ET).hour * 60
                                    + datetime.fromtimestamp(click, ET).minute) < 570 else "rth"}
        statuses = []
        for v in VARIANTS:
            res = simulate(r["symbol"], click, trades, bars, cfg, horizon, spread_bp=sp_q, variant=v)
            if res["status"] == "open" and horizon >= session_end:
                res = finalize_at_session_end(res, horizon)
            if res["status"] == "final":
                res["net_bp"] = round(res["gross_bp"] - sp, 1)
                res["pnl_usd"] = round(NOTIONAL * res["net_bp"] / 1e4, 2)
            out[v] = res
            statuses.append(res["status"])
        # The call is final when both lines are; no_fill is the same for both (same entry rule).
        out["status"] = "no_fill" if "no_fill" in statuses else ("final" if all(x == "final" for x in statuses) else "open")
        results[key] = out
    if qcache is not None:
        RC.save_cache(qcache)
    tmp = RESULTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(results, indent=1), encoding="utf-8")
    tmp.replace(RESULTS)
    return results


def report(results: dict) -> str:
    rows = sorted(results.values(), key=lambda x: float(x.get("ts") or 0))
    f = lambda v, nd=1: "—" if v is None else f"{v:+.{nd}f}"  # noqa: E731
    lines = [f"{'time':17} {'sym':6} {'setup':10} {'sess':4} {'status':8} {'spread':>7} | "
             f"{'as-is exit':14} {'move':>7} {'net':>7} | {'scaled exit':14} {'move':>7} {'net':>7}"]
    fin = {v: [] for v in VARIANTS}
    for x in rows:
        when = datetime.fromtimestamp(float(x["ts"]), ET).strftime("%m-%d %H:%M:%S")
        a, s_ = x.get("asis") or {}, x.get("scaled") or {}
        lines.append(f"{when:17} {x['symbol']:6} {str(x.get('setup') or '')[:10]:10} {x.get('session', ''):4} "
                     f"{x['status']:8} {f(x.get('spread_bp')):>7} | {str(a.get('reason') or '')[:14]:14} "
                     f"{f(a.get('gross_bp')):>7} {f(a.get('net_bp')):>7} | {str(s_.get('reason') or '')[:14]:14} "
                     f"{f(s_.get('gross_bp')):>7} {f(s_.get('net_bp')):>7}")
        for v in VARIANTS:
            y = x.get(v) or {}
            if y.get("status") == "final" and y.get("net_bp") is not None:
                fin[v].append(y)
    for v in VARIANTS:
        if fin[v]:
            nets = [y["net_bp"] for y in fin[v]]
            lines.append(f"{v}: final {len(nets)}, net mean {sum(nets) / len(nets):+.1f} bp, wins "
                         f"{sum(1 for n in nets if n > 0)}/{len(nets)}, paper ${sum(y['pnl_usd'] for y in fin[v]):+.2f} "
                         f"(move mean {sum(y['gross_bp'] for y in fin[v]) / len(nets):+.1f} bp)")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="print the table after scoring")
    a = ap.parse_args()
    res = score_pending()
    if a.report:
        print(report(res))


if __name__ == "__main__":
    main()
