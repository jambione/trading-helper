#!/usr/bin/env python3
"""Walk delayed SIP through live last-mode + RSTOP and rank a declared grid.

Starts from bot_config.json (no heat/EXH stripping). Enforces the live book
(max positions, max buys per minute, reentry cooldown). Ranks overlays by
held-out session dollars. Does not write bot_config.json.

HONESTY
  • 1m OHLC is not the 5s poll tape. Shelf wins if high and low print in one bar.
  • No spread. WASH episodes from shadow are dropped. Without --admitted,
    any listed symbol can arm. With --admitted, only live book windows.
  • T1 blends R; it does not sell a live scale-out.
  • The grid is declared in tools/rstop_search.json — not a global maximum.
  • candidate = beats baseline $ on held-out days AND wins a majority of folds
    AND n >= min_n.

USAGE
    .venv/bin/python tools/optimize_rstop.py --feed sip --days 5 --default-pool
    .venv/bin/python tools/optimize_rstop.py --from 2026-08-10 --to 2026-08-14
    .venv/bin/python tools/optimize_rstop.py --only-baseline --days 1
    .venv/bin/python tools/optimize_rstop.py --admitted --from 2026-08-10 --to 2026-08-14
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import desk_core  # noqa: E402
desk_core.load_desk_env(_ROOT / "signal_engine.env")

import ai_positions as cp  # noqa: E402
from config import load_config  # noqa: E402
from ai_paths import report_file  # noqa: E402
from sim_rstop_path import (  # noqa: E402
    Bar,
    _close,
    _exit_px,
    et_minutes,
    fetch_day_ohlc,
    in_rth,
    make_rec,
    prime_ohlc,
    try_arm,
)

ET = ZoneInfo("America/New_York")
DEFAULT_SEARCH = _ROOT / "tools" / "rstop_search.json"
DEFAULT_POOL = [
    "BYAH", "VSME", "EDHL", "PIII", "SLE", "WOK",
    "AUUD", "QUCY", "TOPS", "TWAV", "IONZ",
]
OVERLAY_KEYS = {
    "give_r": "ai_local_trail_give_r",
    # Declare these two so a grid can hold them still. Sweeping give_r alone
    # cannot answer "how wide should the trail be": give_r moves the open-period
    # give with it (see apply_overlay), so a give_r column is really two knobs
    # at once, and on 2026-08-19 the only cell that beat baseline did so by
    # narrowing give_open_r from the live 0.2 — a variable that sweep never
    # meant to test.
    "give_open_r": "ai_local_trail_give_open_r",
    # Initial stop, independent of the trail. The pair could not be
    # swept apart before, so "is the stop too tight" was unanswerable.
    "initial_give_r": "ai_local_trail_initial_give_r",
    "arm_r": "ai_local_trail_arm_r",
    "arm_pct": "ai_local_trail_arm_pct",
    "give_max_pct": "ai_local_trail_give_max_pct",
    # The dollar floor under the give, and the R cap on that floor. Neither was
    # sweepable, and on the live book the floor is what BINDS: give_r × R and
    # the give_max_pct ceiling both ask for 1-2 cents on a $9-17 name, and the
    # 6-cent floor overrides them on essentially every raise. Every sweep run
    # so far held it fixed, which is why they kept reporting that no overlay
    # beat live — the constraining knob was the same in every cell.
    "min_give_px": "ai_local_trail_min_give_px",
    "min_give_max_r": "ai_local_trail_min_give_max_r",
    # Trail width as a multiple of the round-trip spread. The knob the
    # 2026-08-21 finding points at: the shelf sat $0.06 behind price against
    # an $0.08-0.18 book, so 62% of RTH moments had a spread wider than the
    # whole cushion and the quote tripped the stop without the market moving.
    # Sweepable from the start so k comes off the spread record, not a guess.
    "give_spread_k": "ai_local_trail_give_spread_k",
    # Same idea on the breakeven side: do not protect a fill until it has
    # cleared k round trips. Swept beside give_spread_k because fixing one
    # without the other just changes which mechanism ends the trade.
    "be_at_spread_k": "ai_local_trail_be_at_spread_k",
    "print_ring": "ai_local_trail_print_ring",
    "be_at_r": "ai_local_trail_be_at_r",
    "be_at_pct": "ai_local_trail_be_at_pct",
    "synth_rr": "ai_watch_synth_rr",
    # The size of 1R itself. Everything the desk expresses in R — give_r,
    # arm_r, be_at_r, and the T1 distance via synth_rr — is a fraction of this,
    # so sweeping it moves the whole frame at once and a win cannot be
    # attributed to any single knob. That is the point: on 2026-08-20 the
    # median trade's best moment was 0.046R, so the stop and the target both
    # sat ~20x further out than the move ever travelled and only the trail was
    # calibrated anywhere near the tape. Whether the frame fits the signal is
    # not answerable while the frame is pinned at 5%.
    "synth_stop_pct": "ai_watch_synth_stop_pct",
    # Entry side. 29 of 59 entries on 2026-08-20 were taken at overbought, and
    # the 90-100% exhaustion bucket returned -0.80% forward. heat_max_pct is
    # the ceiling that would refuse them and ships disabled at 0.
    "heat_max_pct": "ai_watch_exhaustion_heat_max_pct",
    "ob_allow_hot": "ai_watch_ob_allow_hot",
    # The $5 floor. Its rejects returned +4.80% forward against +0.63% for
    # admits (n=14, underpowered) — a hypothesis that needs a real fold, and
    # one that cannot be tested while the floor is fixed.
    "min_price": "ai_watch_min_price",
    "heat_min_pct": "ai_watch_exhaustion_heat_min_pct",
    "dead_trade_min": "ai_dead_trade_min",
    "exhaustion_rules": "ai_watch_exhaustion_rules",
    # Off = hard synth stop is the only price exit (plus dead-trade / 15:50).
    # The live 0.10R working shelf is what closed the 86-second scalp; a
    # last-hour hold cannot be tested while that shelf is seeded on every fill.
    "trail_enabled": "ai_local_trail_enabled",
}
VERDICT_CANDIDATE = "candidate"
VERDICT_HYPOTHESIS = "hypothesis"
VERDICT_DO_NOT = "do_not_promote"
VERDICT_SKIP = "skipped_rr"


def live_cfg(**over) -> dict[str, Any]:
    """Current desk config. Does not zero EXH heat or other live gates."""
    try:
        cfg = dict(load_config() or {})
    except Exception:
        cfg = {}
    cfg.update(over)
    return cfg


def apply_overlay(base: dict, overlay: dict) -> dict:
    """Copy *base* and apply a search cell. Does not mutate *base*.

    ``give_r`` deliberately moves the open-period give with it, so a grid that
    names only ``give_r`` sweeps the whole trail the way the desk ships it. That
    coupling is also a trap: it means such a grid cannot attribute a win to the
    trail width rather than to the open give. Naming ``give_open_r`` explicitly
    pins it — the OVERLAY_KEYS loop runs after this block, so an explicit value
    wins. To vary trail width alone, hold ``give_open_r`` at its live value.
    """
    cfg = dict(base)
    if "give_r" in overlay and overlay["give_r"] is not None:
        g = float(overlay["give_r"])
        cfg["ai_local_trail_give_r"] = g
        cfg["ai_local_trail_give_open_r"] = g
    for src, dest in OVERLAY_KEYS.items():
        if src == "give_r":
            continue
        if src in overlay and overlay[src] is not None:
            cfg[dest] = overlay[src]
    return cfg


def rr_ok(cfg: dict) -> bool:
    try:
        rr = float(cfg.get("ai_watch_synth_rr") or 0)
        need = float(cfg.get("ai_min_reward_risk") or 0)
    except (TypeError, ValueError):
        return True
    return rr + 1e-12 >= need


def load_search(path: Path | None = None) -> dict:
    p = path or DEFAULT_SEARCH
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("search"), dict):
        raise ValueError(f"bad search file: {p}")
    data.setdefault("min_n", 30)
    return data


def iter_grid(search: dict) -> list[dict]:
    keys = list(search.keys())
    vals = [list(search[k]) for k in keys]
    out = []
    for combo in itertools.product(*vals):
        out.append(dict(zip(keys, combo)))
    return out


def loo_folds(days: list[str]) -> list[tuple[list[str], list[str]]]:
    """Leave-one-out: each day held out once."""
    if not days:
        return []
    if len(days) == 1:
        return [([], days[:])]
    folds = []
    for i, d in enumerate(days):
        train = days[:i] + days[i + 1 :]
        folds.append((train, [d]))
    return folds


def rth_days_back(n: int, *, end: datetime | None = None) -> list[str]:
    """Last *n* Mon–Fri ET sessions, ending at *end* (default today ET)."""
    cur = end or datetime.now(ET)
    if cur.tzinfo is None:
        cur = cur.replace(tzinfo=ET)
    else:
        cur = cur.astimezone(ET)
    d = cur.date()
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    out.reverse()
    return out


def _et_day(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), ET).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def load_admit_windows(
    shadow_path: Path,
    days: list[str],
) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """(symbol, ET-day) -> [(admit_ts, last_shadow_ts), ...] from live shadow.

    WASH episodes are dropped. A name can be re-admitted; each admit_ts is
    its own window so we do not arm after the desk dropped it.
    """
    want = set(days)
    eps: dict[tuple[str, float], list[dict]] = defaultdict(list)
    if not shadow_path.exists():
        raise FileNotFoundError(f"no shadow file at {shadow_path}")
    with shadow_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper().strip()
            admit = row.get("admit_ts")
            if not sym or admit is None:
                continue
            try:
                admit_f = float(admit)
            except (TypeError, ValueError):
                continue
            if _et_day(admit_f) not in want:
                continue
            look = str(row.get("look_reason") or "").strip().upper()
            if look == "WASH":
                continue
            eps[(sym, admit_f)].append(row)
    windows: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for (sym, admit_f), rows in eps.items():
        day = _et_day(admit_f)
        last = admit_f
        for r in rows:
            try:
                last = max(last, float(r.get("ts") or admit_f))
            except (TypeError, ValueError):
                continue
        windows[(sym, day)].append((admit_f, last))
    for key in windows:
        windows[key].sort()
    return dict(windows)


def windows_for_day(
    windows: dict[tuple[str, str], list[tuple[float, float]]] | None,
    day: str,
) -> dict[str, list[tuple[float, float]]] | None:
    if windows is None:
        return None
    out: dict[str, list[tuple[float, float]]] = {}
    for (sym, d), spans in windows.items():
        if d == day:
            out[sym] = spans
    return out


def in_admit_window(
    symbol: str,
    ts: float,
    day_windows: dict[str, list[tuple[float, float]]] | None,
) -> bool:
    """None windows = unrestricted (old pool walk)."""
    if day_windows is None:
        return True
    for lo, hi in day_windows.get(symbol.upper(), ()):
        if lo - 1e-9 <= ts <= hi + 1e-9:
            return True
    return False


def parse_tod_range(spec: str) -> tuple[int, int]:
    """'14:00-15:30' -> (minutes, minutes). End is exclusive."""
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("empty TOD range")
    left, right = spec.split("-", 1)

    def _hm(part: str) -> int:
        hh, mm = part.strip().split(":")
        return int(hh) * 60 + int(mm)

    lo, hi = _hm(left), _hm(right)
    if hi <= lo:
        raise ValueError(f"TOD range end must be after start: {spec}")
    return lo, hi


def filter_windows_tod(
    windows: dict[tuple[str, str], list[tuple[float, float]]],
    start_min: int,
    end_min: int,
) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """Keep admit windows whose admit_ts clock is in [start, end)."""
    out: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for key, spans in windows.items():
        kept = [(lo, hi) for lo, hi in spans
                if start_min <= et_minutes(lo) < end_min]
        if kept:
            out[key] = kept
    return out


def days_between(start: str, end: str) -> list[str]:
    a = datetime.fromisoformat(start).date()
    b = datetime.fromisoformat(end).date()
    out = []
    cur = a
    while cur <= b:
        if cur.weekday() < 5:
            out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _open_pos(entry: float, ts: float, why: str, cfg: dict, t1_rr: float, scale: float):
    stop_pct = float(cfg.get("ai_watch_synth_stop_pct", 5.0) or 5.0) / 100.0
    if entry <= 0:
        return None
    stop = entry * (1.0 - stop_pct)
    if stop <= 0 or stop >= entry:
        return None
    risk = entry - stop
    loc = cp.initial_local_stop(entry, risk, cfg) or stop
    if not bool(cfg.get("ai_local_trail_enabled", True)):
        # Hold-to-flatten: do not seed the 0.10R working shelf. That shelf is
        # the 86-second scalp. Trail off = hard synth stop + EOD + dead-trade.
        loc = stop
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


def annotate(trade: dict, symbol: str, cfg: dict) -> dict:
    entry = float(trade["entry"])
    exit_px = float(trade["exit"])
    pnl_pct = (exit_px - entry) / entry * 100.0 if entry else 0.0
    # T1 blend is already in trade['r'] (plan-R). Rebuild $ from that R so
    # scale-out accounting matches sim_rstop_path.
    stop_pct = float(cfg.get("ai_watch_synth_stop_pct", 5.0) or 5.0) / 100.0
    give_r = float(cfg.get("ai_local_trail_give_r", 0.10) or 0.10)
    stake = float(cfg.get("ai_trade_amount", 1000.0) or 1000.0)
    r_plan = float(trade.get("r") or 0.0)
    dollar = r_plan * stop_pct * stake
    pnl_from_r = r_plan * stop_pct * 100.0
    give_w = give_r * stop_pct
    r_give = (pnl_from_r / 100.0) / give_w if give_w else 0.0
    out = dict(trade)
    out["symbol"] = symbol
    out["pnl_pct"] = round(pnl_from_r, 4)
    out["dollar"] = round(dollar, 2)
    out["r_plan"] = round(r_plan, 4)
    out["r_give"] = round(r_give, 4)
    return out


def score_trades(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {
            "n": 0, "win_pct": None, "total_dollar": 0.0, "mean_dollar": None,
            "max_dd_dollar": 0.0, "total_r_plan": 0.0,
        }
    dollars = [float(t["dollar"]) for t in trades]
    wins = sum(1 for d in dollars if d > 0)
    eq = peak = 0.0
    max_dd = 0.0
    for d in dollars:
        eq += d
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    return {
        "n": len(trades),
        "win_pct": round(100.0 * wins / len(trades), 1),
        "total_dollar": round(sum(dollars), 2),
        "mean_dollar": round(sum(dollars) / len(trades), 2),
        "max_dd_dollar": round(max_dd, 2),
        "total_r_plan": round(sum(float(t["r_plan"]) for t in trades), 3),
    }


def walk_book(
    book: dict[str, list[Bar]],
    cfg: dict,
    *,
    wash: set[str] | None = None,
    enforce_book: bool = True,
    admit_windows: dict[str, list[tuple[float, float]]] | None = None,
    arm_at_admit: bool = False,
) -> dict[str, Any]:
    """One session, all symbols, optional live seat caps.

    ``arm_at_admit`` buys the first in-window bar and skips should_arm_buy.
    That is the gate-1 screen (buy the admission, hold the horizon), not the
    live heat/RSI scalp. Requires admit_windows.
    """
    wash = wash or set()
    fill = "next_open"
    t1_rr = max(0.0, float(cfg.get("ai_watch_synth_rr", 0.6) or 0.0))
    scale = max(0.0, min(100.0, float(cfg.get("ai_watch_synth_scale_out_pct", 50.0) or 50.0))) / 100.0
    cooldown_sec = float(cfg.get("ai_reentry_cooldown_sec", 900.0) or 0.0)
    dead_min = float(cfg.get("ai_dead_trade_min", 30.0) or 0.0)
    dead_mfe = float(cfg.get("ai_dead_trade_mfe_r", 0.10) or 0.0)
    abort_r = float(cfg.get("ai_fill_abort_r", 0.15) or 0.0)
    stop_pct = float(cfg.get("ai_watch_synth_stop_pct", 5.0) or 5.0) / 100.0
    eod_min = 15 * 60 + 50
    max_pos = int(cfg.get("ai_max_positions", 8) or 8) if enforce_book else 10_000
    max_buys = int(cfg.get("ai_max_buys_per_poll", 2) or 2) if enforce_book else 10_000

    st: dict[str, dict] = {}
    by_ts: dict[float, list] = defaultdict(list)
    for sym, bars in book.items():
        st[sym] = {
            "bars": bars,
            "pos": None,
            "pending": None,
            "cool": 0.0,
            "refuse": {},
            "admit_taken": False,
        }
        for i, bar in enumerate(bars):
            by_ts[bar[0]].append((sym, i, bar))

    trades: list[dict] = []

    def n_seats() -> int:
        n = 0
        for s in st.values():
            if s["pos"] is not None or s["pending"] is not None:
                n += 1
        return n

    def bump(sym: str, why: str) -> None:
        r = st[sym]["refuse"]
        r[why] = r.get(why, 0) + 1

    for ts in sorted(by_ts):
        rows = by_ts[ts]
        buys = 0
        for sym, i, bar in rows:
            _ts, o, h, low, c = bar
            s = st[sym]
            bars = s["bars"]

            if s["pending"] is not None:
                intended = float(s["pending"]["px"])
                why = s["pending"]["why"]
                s["pending"] = None
                fill_px = o if fill == "next_open" else intended
                risk_guess = intended * stop_pct
                if (
                    abort_r > 0
                    and risk_guess > 0
                    and fill_px + 1e-9 < intended - abort_r * risk_guess
                ):
                    bump(sym, "fill_abort")
                else:
                    pos = _open_pos(fill_px, ts, why, cfg, t1_rr, scale)
                    if pos is None:
                        bump(sym, "no_structure")
                    else:
                        s["pos"] = pos

            pos = s["pos"]
            if pos is None:
                continue
            loc = float(pos["local_stop_price"])
            entry = float(pos["entry"])
            risk = float(pos["risk_per_share"])
            if low <= loc + 1e-9:
                px = _exit_px(bar, loc)
                trades.append(annotate(_close(pos, ts, px, "local_trail"), sym, cfg))
                s["pos"] = None
                s["cool"] = ts + cooldown_sec
                continue
            if (
                t1_rr > 0 and scale > 0 and not pos["t1_hit"]
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
            age_min = (ts - float(pos["entry_ts"])) / 60.0
            profit_locked = float(pos["local_stop_price"]) > entry + 1e-9
            if (
                dead_min > 0
                and not pos["t1_hit"]
                and not profit_locked
                and age_min + 1e-9 >= dead_min
                and float(pos["mfe_r"]) < dead_mfe
            ):
                trades.append(annotate(_close(pos, ts, c, "dead_trade"), sym, cfg))
                s["pos"] = None
                s["cool"] = ts + cooldown_sec
                continue
            if et_minutes(ts) >= eod_min:
                trades.append(annotate(_close(pos, ts, c, "eod_flatten"), sym, cfg))
                s["pos"] = None

        for sym, i, bar in rows:
            s = st[sym]
            if s["pos"] is not None or s["pending"] is not None:
                continue
            if not in_rth(ts):
                continue
            if not in_admit_window(sym, ts, admit_windows):
                bump(sym, "not_admitted")
                continue
            if ts < s["cool"]:
                continue
            if i + 1 >= len(s["bars"]):
                continue
            if n_seats() >= max_pos or buys >= max_buys:
                bump(sym, "book_full")
                continue
            if arm_at_admit:
                if s["admit_taken"]:
                    continue
                s["admit_taken"] = True
                s["pending"] = {"px": bar[4], "why": "admit"}
                buys += 1
                continue
            prime_ohlc(sym, s["bars"][:i], ts)
            look = "WASH" if sym.upper() in wash else None
            rec = make_rec(sym, bar[4], cfg, look=look)
            ok, why = try_arm(rec, bar[4], cfg, ts)
            if not ok:
                bump(sym, why)
                continue
            s["pending"] = {"px": bar[4], "why": why}
            buys += 1

    for sym, s in st.items():
        if s["pos"] is not None and s["bars"]:
            last = s["bars"][-1]
            trades.append(annotate(
                _close(s["pos"], last[0], last[4], "eod_flatten"), sym, cfg))
            s["pos"] = None

    refuse: dict[str, int] = {}
    for s in st.values():
        for k, v in s["refuse"].items():
            refuse[k] = refuse.get(k, 0) + v
    return {"trades": trades, "refuse": refuse, "bars": sum(len(b) for b in book.values())}


def walk_days(
    cache: dict[tuple[str, str], list[Bar]],
    days: list[str],
    symbols: list[str],
    cfg: dict,
    **kw,
) -> list[dict]:
    trades: list[dict] = []
    for day in days:
        book = {s: cache.get((s, day), []) for s in symbols}
        for t in walk_book(book, cfg, **kw)["trades"]:
            t["day"] = day
            trades.append(t)
    return trades


def verdict(
    *,
    held_dollar: float,
    base_held: float,
    fold_wins: int,
    n_folds: int,
    n: int,
    min_n: int,
    skipped: bool = False,
) -> str:
    if skipped:
        return VERDICT_SKIP
    if n < min_n:
        return VERDICT_HYPOTHESIS
    if held_dollar <= base_held:
        return VERDICT_DO_NOT
    if n_folds > 0 and fold_wins > n_folds / 2:
        return VERDICT_CANDIDATE
    return VERDICT_HYPOTHESIS


def fetch_cache(
    symbols: list[str],
    days: list[str],
    *,
    feed: str,
    key: str,
    secret: str,
) -> dict[tuple[str, str], list[Bar]]:
    cache: dict[tuple[str, str], list[Bar]] = {}
    for day in days:
        for sym in symbols:
            print(f"  fetch {sym} {day} {feed}…", flush=True)
            bars = fetch_day_ohlc(sym, day, key, secret, feed=feed)
            cache[(sym, day)] = bars
            print(f"    {len(bars)} bars", flush=True)
    return cache


def run_search(
    cache: dict,
    days: list[str],
    symbols: list[str],
    base: dict,
    grid: list[dict],
    *,
    min_n: int,
    wash: set[str],
    enforce_book: bool,
    only_baseline: bool,
    admit_windows: dict[tuple[str, str], list[tuple[float, float]]] | None = None,
    arm_at_admit: bool = False,
) -> dict:
    folds = loo_folds(days)
    cells: list[dict] = []

    def eval_overlay(name: str, overlay: dict, skipped: bool = False) -> dict:
        cfg = apply_overlay(base, overlay)
        by_day: dict[str, list[dict]] = {d: [] for d in days}
        # Why the arms that did NOT happen did not happen. walk_book has always
        # counted this and the caller has always dropped it, which is how a run
        # that armed nothing at all could still print "no candidate, keep live
        # config" — a sentence about the grid, from a walk that never tested it.
        refuse: dict[str, int] = {}
        if not skipped:
            for day in days:
                book = {s: cache.get((s, day), []) for s in symbols}
                walked = walk_book(
                    book, cfg, wash=wash, enforce_book=enforce_book,
                    admit_windows=windows_for_day(admit_windows, day),
                    arm_at_admit=arm_at_admit,
                )
                for k, v in (walked.get("refuse") or {}).items():
                    refuse[k] = refuse.get(k, 0) + int(v)
                for t in walked["trades"]:
                    t["day"] = day
                    by_day[day].append(t)
        all_tr = [t for d in days for t in by_day[d]]
        pooled = score_trades(all_tr)
        fold_rows = []
        fold_wins = 0
        held_tr: list[dict] = []
        for train, test in folds:
            tr_tr = [t for d in train for t in by_day[d]]
            tr_te = [t for d in test for t in by_day[d]]
            fold_rows.append({
                "train": train, "test": test,
                "train_score": score_trades(tr_tr),
                "test_score": score_trades(tr_te),
            })
            held_tr.extend(tr_te)
        held = score_trades(held_tr)
        return {
            "name": name,
            "overlay": overlay,
            "skipped": skipped,
            "pooled": pooled,
            "held": held,
            "folds": fold_rows,
            "fold_wins": fold_wins,
            "n_folds": len(folds),
            "refuse": refuse,
        }

    # Baseline first so fold_wins can compare.
    base_row = eval_overlay("baseline", {})
    cells.append(base_row)
    base_held = float(base_row["held"]["total_dollar"])
    base_fold_test = [
        float(f["test_score"]["total_dollar"]) for f in base_row["folds"]
    ]

    if not only_baseline:
        for i, ov in enumerate(grid):
            cfg = apply_overlay(base, ov)
            skipped = not rr_ok(cfg)
            name = ",".join(f"{k}={ov[k]}" for k in sorted(ov))
            row = eval_overlay(name, ov, skipped=skipped)
            wins = 0
            for fi, f in enumerate(row["folds"]):
                te = float(f["test_score"]["total_dollar"])
                if te > base_fold_test[fi]:
                    wins += 1
            row["fold_wins"] = wins
            cells.append(row)

    for row in cells:
        row["verdict"] = verdict(
            held_dollar=float(row["held"]["total_dollar"]),
            base_held=base_held,
            fold_wins=int(row["fold_wins"]),
            n_folds=int(row["n_folds"]),
            n=int(row["pooled"]["n"]),
            min_n=min_n,
            skipped=bool(row["skipped"]),
        )
    return {
        "days": days,
        "symbols": symbols,
        "baseline_held_dollar": base_held,
        "cells": cells,
        "min_n": min_n,
    }


def render(payload: dict) -> str:
    lines = [
        "SIP last-mode + RSTOP optimizer  (does not write bot_config.json)",
        "1m OHLC; shelf wins same bar; $ = plan-R × synth_stop × ai_trade_amount.",
        f"days={','.join(payload['days'])}  "
        f"syms={','.join(payload['symbols'])}  "
        f"baseline held $={payload['baseline_held_dollar']:+.2f}",
        "",
        f"{'verdict':<16}{'n':>5}{'win%':>7}{'pool$':>10}{'held$':>10}"
        f"{'mean$':>8}{'dd$':>8}{'folds':>7}  overlay",
        "-" * 100,
    ]
    ranked = sorted(
        payload["cells"],
        key=lambda r: (
            0 if r["name"] == "baseline" else 1,
            -float(r["held"]["total_dollar"]),
            -float(r["pooled"]["total_dollar"]),
        ),
    )
    # Print baseline then top 15 non-baseline by held $
    shown = [ranked[0]] if ranked and ranked[0]["name"] == "baseline" else []
    rest = [r for r in payload["cells"] if r["name"] != "baseline"]
    rest.sort(key=lambda r: (
        0 if r["verdict"] == VERDICT_CANDIDATE else 1,
        -float(r["held"]["total_dollar"]),
    ))
    shown.extend(rest[:15])
    for r in shown:
        p, h = r["pooled"], r["held"]
        win = f"{p['win_pct']:.0f}" if p["win_pct"] is not None else "-"
        ov = r["name"] if r["name"] == "baseline" else r["name"]
        lines.append(
            f"{r['verdict']:<16}{p['n']:5d}{win:>7}{p['total_dollar']:+10.2f}"
            f"{h['total_dollar']:+10.2f}"
            f"{(p['mean_dollar'] or 0):+8.2f}{p['max_dd_dollar']:8.1f}"
            f"{r['fold_wins']:3d}/{r['n_folds']:<3}  {ov}"
        )
    # The reason behind the n column. The EMPTY RUN refusal below tells the
    # reader to go check these counts; print them here so there is nothing to
    # go and check. One reason holding nearly every bar-decision is the shape
    # of a broken sim — the armless weeks were 100% rsi_not_rising.
    base_row = next(
        (r for r in payload["cells"] if r["name"] == "baseline"), None)
    refuse = (base_row or {}).get("refuse") or {}
    if refuse:
        lines.append("")
        lines.append(f"baseline arm refusals ({sum(refuse.values())} bar-decisions):")
        for why, cnt in sorted(refuse.items(), key=lambda kv: -kv[1])[:10]:
            lines.append(f"  {cnt:>6}x  {why}")
    cands = [r for r in payload["cells"] if r["verdict"] == VERDICT_CANDIDATE]
    lines.append("")
    # A run that placed no trades has no verdict to give. Saying "keep live
    # config" there reads as evidence the config is tuned, and for weeks it was
    # printed by a simulator that could not arm a single bar — the desk filled
    # 69 orders on a tape this tool walked with n=0 in every cell. Refuse.
    if not any(int(r["pooled"]["n"] or 0) > 0 for r in payload["cells"]):
        lines.append("EMPTY RUN — no cell placed a trade, baseline included.")
        lines.append("  This is not evidence that the live config is tuned; it")
        lines.append("  is a broken simulation. Say nothing about the config")
        lines.append("  until a baseline run reports n > 0. Check the refuse")
        lines.append("  counts from sim_rstop_path.walk_symbol for the reason.")
    elif cands:
        best = max(cands, key=lambda r: float(r["held"]["total_dollar"]))
        lines.append(f"BEST CANDIDATE  held ${best['held']['total_dollar']:+.2f}")
        lines.append(f"  overlay: {json.dumps(best['overlay'])}")
        lines.append("  apply by hand — this tool does not write bot_config.json")
    else:
        lines.append("No candidate. Keep live config; grid did not beat baseline out of sample.")
    return "\n".join(lines)


def write_artifacts(payload: dict, out_dir: Path, tag: str = "") -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ET).strftime("%Y-%m-%d")
    if tag:
        stamp = f"{stamp}_{tag}"
    path = out_dir / f"optimize_rstop_{stamp}.json"
    base_cell = next(
        (c for c in payload["cells"] if c["name"] == "baseline"), {})
    slim = {
        "days": payload["days"],
        "symbols": payload["symbols"],
        "baseline_held_dollar": payload["baseline_held_dollar"],
        "min_n": payload["min_n"],
        "cells": [
            {
                "name": c["name"],
                "overlay": c["overlay"],
                "verdict": c["verdict"],
                "pooled": c["pooled"],
                "held": c["held"],
                "fold_wins": c["fold_wins"],
                "n_folds": c["n_folds"],
            }
            for c in payload["cells"]
        ],
        # Baseline only — the reason an archived run armed what it armed. A
        # stored verdict with n=0 and no refusal counts cannot be audited later.
        "baseline_refuse": dict(sorted(
            (base_cell.get("refuse") or {}).items(), key=lambda kv: -kv[1])),
    }
    path.write_text(json.dumps(slim, indent=2))
    csv_path = out_dir / f"optimize_rstop_{stamp}.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "verdict", "n", "win_pct", "pool_dollar", "held_dollar",
            "mean_dollar", "max_dd", "fold_wins", "n_folds", "overlay",
        ])
        w.writeheader()
        for c in payload["cells"]:
            w.writerow({
                "verdict": c["verdict"],
                "n": c["pooled"]["n"],
                "win_pct": c["pooled"]["win_pct"],
                "pool_dollar": c["pooled"]["total_dollar"],
                "held_dollar": c["held"]["total_dollar"],
                "mean_dollar": c["pooled"]["mean_dollar"],
                "max_dd": c["pooled"]["max_dd_dollar"],
                "fold_wins": c["fold_wins"],
                "n_folds": c["n_folds"],
                "overlay": json.dumps(c["overlay"]),
            })
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--default-pool", action="store_true")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--from", dest="date_from", default="")
    ap.add_argument("--to", dest="date_to", default="")
    ap.add_argument("--feed", choices=("iex", "sip"), default="sip")
    ap.add_argument("--folds", "--rounds", dest="folds", type=int, default=0,
                    help="unused alias; leave-one-out uses all days")
    ap.add_argument("--search", default=str(DEFAULT_SEARCH))
    ap.add_argument("--only-baseline", action="store_true")
    ap.add_argument("--max-cells", type=int, default=0)
    ap.add_argument("--no-book", action="store_true",
                    help="drop seat caps (research only)")
    ap.add_argument("--wash", default="")
    ap.add_argument("--admitted", action="store_true",
                    help="only arm names/windows the live shadow book admitted")
    ap.add_argument("--admit-tod", default="",
                    help="keep admit windows whose admit clock is in HH:MM-HH:MM ET "
                         "(end exclusive). 14:00-15:30 is the late-hold PASS.")
    ap.add_argument("--arm-at-admit", action="store_true",
                    help="buy the first in-window bar; skip heat/RSI should_arm_buy. "
                         "Matches thesis_screen, not the live scalp.")
    ap.add_argument("--tag", default="",
                    help="suffix on the benchmarks/optimize_rstop_DATE json/csv")
    ap.add_argument("--shadow", default="",
                    help="shadow.jsonl path (default: ai_reports/shadow.jsonl)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    if args.date_from and args.date_to:
        days = days_between(args.date_from, args.date_to)
    else:
        n = args.days if args.days else 5
        days = rth_days_back(n)

    admit_windows = None
    if args.admitted:
        shadow = Path(args.shadow) if args.shadow else report_file("shadow.jsonl")
        admit_windows = load_admit_windows(shadow, days)
        admitted_syms = sorted({s for s, d in admit_windows})
        print(f"admitted windows: {len(admit_windows)} symbol-days  "
              f"{len(admitted_syms)} names from {shadow}", flush=True)
        if args.symbols or args.default_pool:
            allow = {s.upper() for s in args.symbols} if args.symbols else set(DEFAULT_POOL)
            admit_windows = {k: v for k, v in admit_windows.items() if k[0] in allow}
        if args.admit_tod:
            lo, hi = parse_tod_range(args.admit_tod)
            n_before = len(admit_windows)
            admit_windows = filter_windows_tod(admit_windows, lo, hi)
            print(f"admit TOD {args.admit_tod}: {n_before} symbol-days -> "
                  f"{len(admit_windows)}", flush=True)
        symbols = sorted({s for s, d in admit_windows})
        if not symbols:
            print("no admitted names in shadow for those days", file=sys.stderr)
            return 2
    elif args.admit_tod or args.arm_at_admit:
        ap.error("--admit-tod / --arm-at-admit require --admitted")
    else:
        symbols = [s.upper() for s in args.symbols] or (
            DEFAULT_POOL if args.default_pool else [])
        if not symbols:
            ap.error("give symbols, --default-pool, or --admitted")

    reg = load_search(Path(args.search))
    grid = iter_grid(reg["search"])
    if args.max_cells:
        grid = grid[: args.max_cells]

    base = live_cfg()
    key = base.get("api_key") or base.get("alpaca_key") or ""
    secret = base.get("secret_key") or base.get("alpaca_secret") or ""
    if not (key and secret):
        print("no Alpaca keys in live config / signal_engine.env", file=sys.stderr)
        return 2

    print(f"live heat_min={base.get('ai_watch_exhaustion_heat_min_pct')}  "
          f"give={base.get('ai_local_trail_give_r')}  "
          f"rr={base.get('ai_watch_synth_rr')}  "
          f"dead={base.get('ai_dead_trade_min')}  "
          f"trail={base.get('ai_local_trail_enabled')}  "
          f"feed={args.feed}  cells={0 if args.only_baseline else len(grid)}  "
          f"arm_at_admit={args.arm_at_admit}  book={not args.no_book}",
          flush=True)

    cache = fetch_cache(symbols, days, feed=args.feed, key=key, secret=secret)
    payload = run_search(
        cache, days, symbols, base, grid,
        min_n=int(reg.get("min_n") or 30),
        wash={s.strip().upper() for s in args.wash.split(",") if s.strip()},
        enforce_book=not args.no_book,
        only_baseline=args.only_baseline,
        admit_windows=admit_windows,
        arm_at_admit=args.arm_at_admit,
    )
    if args.json:
        print(json.dumps({
            "days": payload["days"],
            "baseline_held_dollar": payload["baseline_held_dollar"],
            "n_cells": len(payload["cells"]),
        }, indent=2))
    else:
        print(render(payload))
    if not args.no_write:
        path = write_artifacts(payload, _ROOT / "benchmarks", tag=args.tag)
        print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
