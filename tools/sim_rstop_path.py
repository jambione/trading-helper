#!/usr/bin/env python3
"""
sim_rstop_path.py — last-mode entry + live RSTOP on 1-minute bars.

Walks Alpaca 1m OHLC (or a JSON bars file) with the same gates and trail
the desk uses: should_arm_buy (last + EXH direction + WASH), seed shelf at
entry − 0.10R, raise via local_profit_stop / note_trail_print, flatten if
the bar low prints through the shelf, dead-trade, 15:50 ET flatten.

NOT a tape. 1m lows are better than 5s polls and still miss intra-bar
order. Sells at the shelf (or the open if the bar gaps through). No
spread, no seats, one name at a time. One day is a hypothesis.

USAGE
    venv/bin/python tools/sim_rstop_path.py UMAC ONDS --day 2026-08-14
    venv/bin/python tools/sim_rstop_path.py UMAC --day 2026-08-14 --sweep
    venv/bin/python tools/sim_rstop_path.py --bars-file /tmp/umac.json
    venv/bin/python tools/sim_rstop_path.py UMAC --day 2026-08-14 --json
    venv/bin/python tools/sim_rstop_path.py UMAC --day 2026-08-14 --feed sip
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

UTC = timezone.utc
ET = ZoneInfo("America/New_York")

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import desk_core  # noqa: E402
desk_core.load_desk_env(_ROOT / "signal_engine.env")

import ai_entry_watch as ew  # noqa: E402
import ai_positions as cp  # noqa: E402
import alpaca_api  # noqa: E402
from config import load_config  # noqa: E402

# (ts, open, high, low, close)
Bar = tuple[float, float, float, float, float]


def path_cfg(**over) -> dict[str, Any]:
    """Live last-mode + TV %R pair then CM RSI-2, ratchet on top."""
    try:
        cfg = dict(load_config() or {})
    except Exception:
        cfg = {}
    cfg.update({
        "ai_watch_arm_mode": "last",
        "ai_watch_tv_exh_rsi": True,
        "ai_watch_exhaustion_rules": True,
        "ai_watch_in_zone_ignore_fade": False,
        "ai_watch_require_exhaustion_data": True,
        "rte_threshold": 20,
        "rte_confluence_max": 15.0,
        "rte_require_tight": True,
        "rte_slow_timeframe": "",
        "rte_slow_native_length": 112,
        "cm_rsi_buy_max": 10.0,
        "ai_watch_exhaustion_heat_min_pct": 0.0,
        "ai_watch_exhaustion_heat_max_pct": 0.0,
        "ai_watch_ob_allow_hot": True,
        "ai_watch_arm_require_indicators": False,
        "ai_min_reward_risk": 0.5,
        "ai_watch_min_stop_pct": 0.0,
        "ai_watch_require_db_zone": False,
        "ai_local_trail_enabled": True,
        "ai_local_trail_arm_r": 0.0,
        "ai_local_trail_give_px": 0.0,
    })
    cfg.update(over)
    return cfg


def et_hm(ts: float) -> tuple[int, int]:
    dt = datetime.fromtimestamp(float(ts), ET)
    return dt.hour, dt.minute


def et_minutes(ts: float) -> int:
    h, m = et_hm(ts)
    return h * 60 + m


def in_rth(ts: float, *, start: int = 9 * 60 + 35, end: int = 15 * 60 + 50) -> bool:
    return start <= et_minutes(ts) < end


def prime_ohlc(symbol: str, closed: list[Bar], now: float) -> None:
    """Feed closed 1m bars into the same cache live_exhaustion reads."""
    rows = [(b[2], b[3], b[4]) for b in closed]
    stamps = [b[0] for b in closed]
    with ew._ohlc_cache_lock:
        ew._ohlc_cache[symbol] = (now, rows)
        ew._ohlc_ts_cache[symbol] = (now, stamps)


def make_rec(
    symbol: str,
    px: float,
    cfg: dict,
    *,
    look: str | None = None,
    source: str = "momentum",
) -> dict[str, Any]:
    struct = ew.build_last_zone_structure(px, cfg, reason=source)
    return {
        "symbol": symbol,
        "status": "watching",
        "source": source,
        "look_reason": look,
        "admit_look_reason": look,
        "structure": struct,
        "indicator": {},
    }


def replay_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Desk config with the knobs a replay cannot honour turned to bars.

    There is no signal engine behind a replay. The desk runs with
    ai_watch_cm_rsi_local=False because the engine publishes CM RSI-2 for it;
    carried into a sim that setting makes should_arm_buy answer no_rsi_data on
    every single bar. Bars are the only source here, so compute it from them.

    Idempotent, and applied inside try_arm so that every caller gets it —
    tools/optimize_rstop.py drives its own book walk and only shares this
    function, so a fix parked in walk_symbol left the sweep still armless.
    """
    if cfg.get("ai_watch_cm_rsi_local") is True:
        return cfg
    out = dict(cfg)
    out["ai_watch_cm_rsi_local"] = True
    return out


def try_arm(
    rec: dict,
    px: float,
    cfg: dict,
    now: float,
) -> tuple[bool, str]:
    cfg = replay_cfg(cfg)
    try:
        ew.apply_live_exhaustion(rec, px, cfg, now)
    except Exception:
        pass
    return ew.should_arm_buy(rec, ask=px, bid=None, cfg=cfg)


def _exit_px(bar: Bar, shelf: float) -> float:
    """Sell at the shelf, or the open if the bar gaps through it."""
    _ts, o, _h, low, _c = bar
    if o <= shelf + 1e-9:
        return float(o)
    return float(shelf)


def walk_symbol(
    symbol: str,
    bars: list[Bar],
    cfg: dict,
    *,
    look: str | None = None,
    source: str = "momentum",
    fill: str = "next_open",
    t1_rr: float | None = None,
    scale_pct: float | None = None,
    cooldown_sec: float | None = None,
    max_trades: int = 0,
    warmup: list[Bar] | None = None,
) -> dict[str, Any]:
    """Walk one symbol-day. Returns trades + refuse counts.

    *fill* ``next_open`` (default) arms on bar i close and fills at i+1 open.
    ``close`` fills on the same bar's close (optimistic).
    """
    # try_arm does this too; doing it here as well keeps the rest of the walk
    # reading the same config the gates do. See replay_cfg.
    cfg = replay_cfg(cfg)

    stop_pct = float(cfg.get("ai_watch_synth_stop_pct", 5.0) or 5.0) / 100.0
    if t1_rr is None:
        t1_rr = float(cfg.get("ai_watch_synth_rr", 0.6) or 0.0)
    t1_rr = max(0.0, float(t1_rr))
    if scale_pct is None:
        scale_pct = float(cfg.get("ai_watch_synth_scale_out_pct", 50.0) or 50.0)
    scale = max(0.0, min(100.0, float(scale_pct))) / 100.0
    if cooldown_sec is None:
        cooldown_sec = float(cfg.get("ai_reentry_cooldown_sec", 900.0) or 0.0)
    dead_min = float(cfg.get("ai_dead_trade_min", 22.0) or 0.0)
    dead_mfe = float(cfg.get("ai_dead_trade_mfe_r", 0.10) or 0.0)
    eod_min = 15 * 60 + 50
    abort_r = float(cfg.get("ai_fill_abort_r", 0.15) or 0.0)

    trades: list[dict[str, Any]] = []
    refuse: dict[str, int] = {}
    pos: dict[str, Any] | None = None
    cool_until = 0.0
    pending: dict[str, Any] | None = None

    def _open(entry: float, ts: float, why: str) -> dict[str, Any] | None:
        if entry <= 0:
            return None
        stop = entry * (1.0 - stop_pct)
        if stop <= 0 or stop >= entry:
            return None
        risk = entry - stop
        loc = cp.initial_local_stop(entry, risk, cfg) or stop
        target = entry + t1_rr * risk if t1_rr > 0 else 0.0
        return {
            "entry": entry,
            "entry_ts": ts,
            "entry_price": entry,
            "entry_stop_price": stop,
            "risk_per_share": risk,
            "local_stop_price": loc,
            "target_1": target,
            "scale": scale,
            "why_arm": why,
            "peak_price": entry,
            "mfe_r": 0.0,
            "mae_r": 0.0,
            "t1_hit": False,
            "t1_r": None,
            "trail_prints": [],
            "trail_last": None,
        }

    n = len(bars)
    for i, bar in enumerate(bars):
        ts, o, h, low, c = bar
        closed = list(warmup or []) + bars[:i]

        if pending is not None:
            intended = float(pending["px"])
            why = pending["why"]
            pending = None
            fill_px = o if fill == "next_open" else intended
            risk_guess = intended * stop_pct
            if (
                abort_r > 0
                and risk_guess > 0
                and fill_px + 1e-9 < intended - abort_r * risk_guess
            ):
                refuse["fill_abort"] = refuse.get("fill_abort", 0) + 1
            else:
                pos = _open(fill_px, ts, why)
                if pos is None:
                    refuse["no_structure"] = refuse.get("no_structure", 0) + 1

        if pos is not None:
            loc = float(pos["local_stop_price"])
            entry = float(pos["entry"])
            risk = float(pos["risk_per_share"])
            # Gap / print through the shelf — capital first, before T1.
            if low <= loc + 1e-9:
                px = _exit_px(bar, loc)
                trades.append(_close(pos, ts, px, "local_trail"))
                pos = None
                cool_until = ts + cooldown_sec
                if max_trades and len(trades) >= max_trades:
                    break
                continue
            if (
                t1_rr > 0
                and scale > 0
                and not pos["t1_hit"]
                and h + 1e-9 >= float(pos["target_1"])
            ):
                pos["t1_hit"] = True
                pos["t1_r"] = t1_rr
            peak = max(float(pos.get("peak_price") or entry), h)
            pos["peak_price"] = peak
            pos["mfe_r"] = max(float(pos["mfe_r"]), (peak - entry) / risk)
            pos["mae_r"] = min(float(pos["mae_r"]), (low - entry) / risk)
            pos["last_seen_price"] = h
            cp.note_trail_print(pos, c)
            want = cp.local_profit_stop(pos, cfg)
            if want is not None and want > loc + 1e-9:
                pos["local_stop_price"] = want
                loc = want
            age_min = (ts - float(pos["entry_ts"])) / 60.0
            profit_locked = loc > entry + 1e-9
            if (
                dead_min > 0
                and not pos["t1_hit"]
                and not profit_locked
                and age_min + 1e-9 >= dead_min
                and float(pos["mfe_r"]) < dead_mfe
            ):
                trades.append(_close(pos, ts, c, "dead_trade"))
                pos = None
                cool_until = ts + cooldown_sec
                if max_trades and len(trades) >= max_trades:
                    break
                continue
            if et_minutes(ts) >= eod_min:
                trades.append(_close(pos, ts, c, "eod_flatten"))
                pos = None
                break
            continue

        if not in_rth(ts):
            continue
        if ts < cool_until:
            continue
        if max_trades and len(trades) >= max_trades:
            break
        if fill == "next_open" and i + 1 >= n:
            continue

        prime_ohlc(symbol, closed, ts)
        rec = make_rec(symbol, c, cfg, look=look, source=source)
        ok, why = try_arm(rec, c, cfg, ts)
        if not ok:
            refuse[why] = refuse.get(why, 0) + 1
            continue
        if fill == "close":
            pos = _open(c, ts, why)
        else:
            pending = {"px": c, "why": why}

    if pos is not None:
        last = bars[-1]
        trades.append(_close(pos, last[0], last[4], "eod_flatten"))

    return {"symbol": symbol, "trades": trades, "refuse": refuse, "bars": n}


def _close(pos: dict, ts: float, px: float, reason: str) -> dict[str, Any]:
    entry = float(pos["entry"])
    risk = float(pos["risk_per_share"])
    r_exit = (px - entry) / risk if risk > 0 else 0.0
    if pos.get("t1_hit") and pos.get("t1_r") is not None:
        scale = float(pos.get("scale") or 0.5)
        r = scale * float(pos["t1_r"]) + (1.0 - scale) * r_exit
    else:
        r = r_exit
    return {
        "entry_ts": pos["entry_ts"],
        "exit_ts": ts,
        "entry": round(entry, 4),
        "exit": round(px, 4),
        "stop": round(float(pos["entry_stop_price"]), 4),
        "rstop": round(float(pos["local_stop_price"]), 4),
        "reason": reason,
        "r": round(r, 4),
        "mfe_r": round(float(pos["mfe_r"]), 4),
        "mae_r": round(float(pos["mae_r"]), 4),
        "hold_min": round((ts - float(pos["entry_ts"])) / 60.0, 1),
        "t1_hit": bool(pos.get("t1_hit")),
        "why_arm": pos.get("why_arm") or "",
    }


def fetch_day_ohlc(
    symbol: str,
    day: str,
    api_key: str,
    secret: str,
    *,
    feed: str = "iex",
) -> list[Bar]:
    """1-minute (ts, o, h, l, c) for one ET day. Raises on repeated 429."""
    import requests

    feed = alpaca_api.research_feed_rest(feed)
    start = datetime.fromisoformat(f"{day}T00:00:00").replace(tzinfo=ET)
    end = alpaca_api.research_bar_end(feed, requested_end=start + timedelta(days=1))
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}
    out: list[Bar] = []
    page_token = None
    while True:
        params = {
            "timeframe": "1Min",
            "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10_000,
            "feed": feed,
            "sort": "asc",
            "adjustment": "raw",
        }
        if page_token:
            params["page_token"] = page_token
        for attempt in range(6):
            r = requests.get(url, params=params, headers=headers, timeout=60)
            if r.status_code == 429:
                wait = 5 * (2 ** attempt)
                print(f"  [429] {symbol} {day} — waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError(f"{symbol} {day}: repeated 429s")
        data = r.json()
        for b in data.get("bars") or []:
            try:
                ts = datetime.fromisoformat(
                    str(b["t"]).replace("Z", "+00:00")).timestamp()
                out.append((
                    ts, float(b["o"]), float(b["h"]),
                    float(b["l"]), float(b["c"]),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        page_token = data.get("next_page_token")
        if not page_token:
            break
        time.sleep(0.35)
    out.sort()
    return out


def load_bars_file(path: Path) -> dict[str, list[Bar]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        raw = {"SIM": raw}
    out: dict[str, list[Bar]] = {}
    for sym, rows in raw.items():
        bars: list[Bar] = []
        for b in rows:
            if isinstance(b, dict):
                bars.append((
                    float(b["ts"]), float(b["o"]), float(b["h"]),
                    float(b["l"]), float(b["c"]),
                ))
            else:
                bars.append((
                    float(b[0]), float(b[1]), float(b[2]),
                    float(b[3]), float(b[4]),
                ))
        bars.sort()
        out[str(sym).upper()] = bars
    return out


def summarize(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {
            "n": 0, "win_pct": None, "mean_r": None, "total_r": 0.0,
            "scratch": 0, "t1_hit": 0, "dead": 0, "eod": 0, "trail": 0,
        }
    n = len(trades)
    wins = sum(1 for t in trades if t["r"] > 0)
    return {
        "n": n,
        "win_pct": round(100.0 * wins / n, 1),
        "mean_r": round(sum(t["r"] for t in trades) / n, 3),
        "total_r": round(sum(t["r"] for t in trades), 3),
        "scratch": sum(1 for t in trades if t["reason"] == "local_trail" and t["r"] <= 0),
        "t1_hit": sum(1 for t in trades if t.get("t1_hit")),
        "dead": sum(1 for t in trades if t["reason"] == "dead_trade"),
        "eod": sum(1 for t in trades if t["reason"] == "eod_flatten"),
        "trail": sum(1 for t in trades if t["reason"] == "local_trail"),
    }


def print_run(label: str, results: list[dict], *, show_trades: bool) -> None:
    all_tr: list[dict] = []
    print()
    print(f"=== {label} ===")
    for res in results:
        tr = res["trades"]
        all_tr.extend({**t, "symbol": res["symbol"]} for t in tr)
        s = summarize(tr)
        print(
            f"  {res['symbol']:<6} bars={res['bars']:<4} "
            f"trades={s['n']:<3} "
            + (f"win={s['win_pct']:.0f}%  meanR={s['mean_r']:+.3f}  "
               f"totR={s['total_r']:+.2f}  "
               f"scratch={s['scratch']} t1={s['t1_hit']} "
               f"dead={s['dead']} eod={s['eod']}"
               if s["n"] else "no trades")
        )
        if res["refuse"] and s["n"] == 0:
            top = sorted(res["refuse"].items(), key=lambda kv: -kv[1])[:4]
            bits = ", ".join(f"{k}×{v}" for k, v in top)
            print(f"         refuse: {bits}")
        if show_trades:
            for t in tr:
                when = datetime.fromtimestamp(t["entry_ts"], ET).strftime("%H:%M")
                print(
                    f"         {when}  {t['entry']:.2f}→{t['exit']:.2f}  "
                    f"{t['r']:+.2f}R  {t['reason']}  "
                    f"mfe={t['mfe_r']:+.2f}  arm={t['why_arm']}"
                )
    s = summarize(all_tr)
    if len(results) > 1:
        print(
            f"  ALL    trades={s['n']:<3} "
            + (f"win={s['win_pct']:.0f}%  meanR={s['mean_r']:+.3f}  "
               f"totR={s['total_r']:+.2f}  scratch={s['scratch']}"
               if s["n"] else "no trades")
        )


def run_book(
    book: dict[str, list[Bar]],
    cfg: dict,
    *,
    wash: set[str],
    fill: str,
    t1_rr: float | None,
    max_trades: int,
    warmup: dict[str, list[Bar]] | None = None,
) -> list[dict]:
    out = []
    warm = warmup or {}
    for sym, bars in book.items():
        look = "WASH" if sym.upper() in wash else None
        out.append(walk_symbol(
            sym, bars, cfg, look=look, fill=fill,
            t1_rr=t1_rr, max_trades=max_trades,
            warmup=warm.get(sym) or warm.get(sym.upper()),
        ))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("symbols", nargs="*", help="tickers (ignored with --bars-file)")
    p.add_argument("--day", default="", help="YYYY-MM-DD (ET)")
    p.add_argument("--bars-file", default="", help="JSON {SYM: [{ts,o,h,l,c}, ...]}")
    p.add_argument("--wash", default="", help="comma symbols forced LOOK=WASH")
    p.add_argument("--fill", choices=("next_open", "close"), default="next_open")
    p.add_argument("--give-r", type=float, default=None)
    p.add_argument("--t1-rr", type=float, default=None)
    p.add_argument("--exh", choices=("on", "off"), default="on")
    p.add_argument("--max-trades", type=int, default=0, help="0 = unlimited")
    p.add_argument("--sweep", action="store_true",
                   help="give 0.10/0.20 × T1 0.6/0.25/off × EXH on/off")
    p.add_argument("--json", action="store_true")
    p.add_argument("--trades", action="store_true", help="print each fill")
    p.add_argument("--feed", choices=("iex", "sip"), default="iex",
                   help="Historical tape. sip = free delayed SIP (15m lag).")
    p.add_argument("--warmup-days", type=int, default=1,
                   help="Prior ET sessions prepended so %%R(112) can form.")
    p.add_argument("--legacy-exh", action="store_true",
                   help="Old single-line heat gate instead of TV two-line+RSI.")
    args = p.parse_args(argv)

    wash = {s.strip().upper() for s in args.wash.split(",") if s.strip()}

    warmup_book: dict[str, list[Bar]] = {}
    if args.bars_file:
        book = load_bars_file(Path(args.bars_file))
    else:
        if not args.symbols or not args.day:
            print("need SYMBOLS --day YYYY-MM-DD, or --bars-file", file=sys.stderr)
            return 2
        cfg0 = path_cfg()
        key = cfg0.get("api_key") or cfg0.get("alpaca_key") or ""
        secret = cfg0.get("secret_key") or cfg0.get("alpaca_secret") or ""
        if not (key and secret):
            print("no Alpaca api_key/secret_key in config", file=sys.stderr)
            return 2
        book = {}
        day0 = datetime.fromisoformat(args.day).date()
        for raw in args.symbols:
            sym = raw.upper().strip()
            print(f"  fetching {sym} {args.day}…", file=sys.stderr)
            book[sym] = fetch_day_ohlc(sym, args.day, key, secret, feed=args.feed)
            warm: list[Bar] = []
            for back in range(1, max(0, int(args.warmup_days)) + 1):
                prev = (day0 - timedelta(days=back)).isoformat()
                print(f"  fetching {sym} warmup {prev}…", file=sys.stderr)
                try:
                    warm = fetch_day_ohlc(sym, prev, key, secret, feed=args.feed) + warm
                except Exception as e:
                    print(f"  {sym} warmup {prev}: {e}", file=sys.stderr)
            warmup_book[sym] = warm
            print(f"  {sym}: {len(book[sym])} bars + {len(warm)} warmup", file=sys.stderr)

    if args.sweep:
        rows = []
        for give in (0.10, 0.20):
            for t1 in (0.6, 0.25, 0.0):
                for exh in (True, False):
                    cfg = path_cfg(
                        ai_local_trail_give_r=give,
                        ai_local_trail_give_open_r=give,
                        ai_watch_exhaustion_rules=exh,
                        ai_watch_synth_rr=t1 if t1 > 0 else 0.6,
                    )
                    results = run_book(
                        book, cfg, wash=wash, fill=args.fill,
                        t1_rr=t1, max_trades=args.max_trades,
                        warmup=warmup_book,
                    )
                    trades = [t for r in results for t in r["trades"]]
                    s = summarize(trades)
                    s.update({
                        "give_r": give, "t1_rr": t1,
                        "exh": "on" if exh else "off",
                    })
                    rows.append(s)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        print("SWEEP  last-mode RSTOP path  "
              f"syms={','.join(book)}  fill={args.fill}")
        hdr = (f"{'give':>6}{'t1':>6}{'exh':>5}{'n':>5}{'win%':>7}"
               f"{'meanR':>8}{'totR':>8}{'scr':>5}{'t1h':>5}{'dead':>5}")
        print(hdr)
        print("-" * len(hdr))
        for s in rows:
            win = f"{s['win_pct']:.0f}" if s["win_pct"] is not None else "-"
            mean = f"{s['mean_r']:+.3f}" if s["mean_r"] is not None else "-"
            print(
                f"{s['give_r']:6.2f}{s['t1_rr']:6.2f}{s['exh']:>5}"
                f"{s['n']:5d}{win:>7}{mean:>8}{s['total_r']:+8.2f}"
                f"{s['scratch']:5d}{s['t1_hit']:5d}{s['dead']:5d}"
            )
        print("\nscr = local_trail with R≤0. 1m lows, not the IEX print.")
        return 0

    cfg = path_cfg(
        ai_watch_exhaustion_rules=(args.exh == "on"),
    )
    if args.legacy_exh:
        cfg["ai_watch_tv_exh_rsi"] = False
        cfg["ai_watch_require_exhaustion_data"] = False
    if args.give_r is not None:
        cfg["ai_local_trail_give_r"] = args.give_r
        cfg["ai_local_trail_give_open_r"] = args.give_r
    t1 = args.t1_rr
    results = run_book(
        book, cfg, wash=wash, fill=args.fill,
        t1_rr=t1, max_trades=args.max_trades,
        warmup=warmup_book,
    )
    if args.json:
        print(json.dumps(results, indent=2))
        return 0
    label = (
        f"last+EXH={args.exh}  give="
        f"{cfg.get('ai_local_trail_give_r', 0.10)}  "
        f"t1={t1 if t1 is not None else cfg.get('ai_watch_synth_rr')}  "
        f"fill={args.fill}"
    )
    print_run(label, results, show_trades=args.trades)
    print("\nHonesty: 1m bar lows; sell at the shelf (open if it gaps through).")
    print("No seats, no spread. WASH only if --wash lists the symbol.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
