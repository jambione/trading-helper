#!/usr/bin/env python3
"""Pin live fills, walk the current trail, print new vs live $.

Off-hours planner (evenings, weekends, any time the tape is frozen).
Does not write ``bot_config.json``.

Takes closed trades from ``outcomes.jsonl`` — the entries the desk actually
took — and re-runs each one from the logged fill under the *current*
``bot_config`` trail: seeded RSTOP, T1 scale, dead-trade, 15:50 flatten.
Entries are not re-decided. That is the gap the path sim and the tape tuner
leave open.

USAGE
    # After the close, on a machine with Alpaca keys (usually the mini):
    .venv/bin/python tools/sim_fill_replay.py --days 10 --feed sip \\
        --write-bars-cache ai_reports/fill_replay/bars.json

    # Any later off-hours session; keys not required if the cache is present:
    .venv/bin/python tools/sim_fill_replay.py --days 10 --no-fetch \\
        --bars-cache ai_reports/fill_replay/bars.json

    # Packed tape + cache (this MacBook clone often has no live outcomes):
    .venv/bin/python tools/sim_fill_replay.py --tape ai_reports/tapes/LABEL \\
        --bars-cache ai_reports/fill_replay/bars.json --no-fetch

    # One day, a range (weekdays), or named days:
    .venv/bin/python tools/sim_fill_replay.py --day 2026-08-27
    .venv/bin/python tools/sim_fill_replay.py --from 2026-08-20 --to 2026-08-27
    .venv/bin/python tools/sim_fill_replay.py --day 2026-08-20 --day 2026-08-27

    # Next-session overlay (still does not write config):
    .venv/bin/python tools/sim_fill_replay.py --days 10 \\
        --overlay give_r=0.10 --overlay dead_trade_min=22

HONESTY
  • Fills are pinned. This is not "would we take the trade today".
  • 1m OHLC is not the 5s poll tape. Same-minute stomps can be missed.
    Intra-bar high and low both print, so the shelf can win on a bar that
    never actually traded through it.
  • No spread on the bar path. ``give_spread_k`` uses the fill's recorded
    ``spread_r`` when the outcome has it; otherwise that floor is off.
  • Missing bars are skipped, not scored as 0R.
  • Shadow fallback has no intra-poll low. Every shadow-scored row says so.
  • One session is a hypothesis. ``candidate`` needs min_n and the same sign
    on both chronological halves — and even then this tool does not write
    config.
  • ``desk_product=observe`` does not zero this run (we do not re-arm).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import desk_core  # noqa: E402
desk_core.load_desk_env(_ROOT / "signal_engine.env")

import ai_entry_watch as ew  # noqa: E402
import ai_positions as cp  # noqa: E402
import desk_tape  # noqa: E402
import optimize_rstop as opt  # noqa: E402
import sim_edge_mode_ab as edge  # noqa: E402
from ai_paths import find_report_file, resolve_report_dir  # noqa: E402
from sim_rstop_path import (  # noqa: E402
    Bar,
    _close,
    _exit_px,
    et_minutes,
    fetch_day_ohlc,
)

ET = opt.ET
MIN_N = 30
EOD_MIN = 15 * 60 + 50
VERDICT_CANDIDATE = "candidate"
VERDICT_HYPOTHESIS = "hypothesis"
VERDICT_DO_NOT = "do_not_promote"
VERDICT_NO_SCORE = "no_score"


# ── fills ────────────────────────────────────────────────────────────────────

def resolve_days(
    *,
    day: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    days_n: int | None = None,
) -> list[str]:
    """Named days, an inclusive weekday range, or last N sessions."""
    named = [d.strip() for d in (day or []) if d and d.strip()]
    if named and (date_from or date_to or days_n):
        raise ValueError("use --day or --from/--to or --days, not mixed")
    if named:
        return sorted(set(named))
    if date_from or date_to:
        if not (date_from and date_to):
            raise ValueError("--from and --to are both required for a range")
        out = opt.days_between(date_from, date_to)
        if not out:
            raise ValueError(f"no weekday sessions in {date_from}..{date_to}")
        return out
    return opt.rth_days_back(days_n if days_n else 10)


def et_day(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts), ET).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return ""


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_fill(row: dict) -> tuple[dict[str, Any] | None, str | None]:
    """Usable pinned fill, or a skip reason. Missing live P&L is not a skip."""
    if not isinstance(row, dict):
        return None, "unparsed"
    sym = str(row.get("symbol") or "").upper().strip()
    if not sym:
        return None, "no_symbol"
    entry = _num(row.get("entry_price"))
    if entry is None or entry <= 0:
        return None, "no_entry"
    qty = _num(row.get("total_qty"))
    if qty is None or qty <= 0:
        return None, "no_qty"
    entry_ts = _num(row.get("entry_time") or row.get("ts"))
    if entry_ts is None or entry_ts <= 0:
        return None, "no_entry_time"
    day = et_day(entry_ts)
    if not day:
        return None, "no_entry_time"
    live_exit = _num(row.get("exit_price"))
    if live_exit is None:
        live_exit = _num(row.get("exit_price_approx"))
    feats = row.get("features") if isinstance(row.get("features"), dict) else {}
    return {
        "symbol": sym,
        "day": day,
        "entry": entry,
        "entry_ts": float(entry_ts),
        "qty": float(qty),
        "live_stop": _num(row.get("stop_price")),
        "live_target": _num(row.get("target_1")),
        "live_exit": live_exit,
        "live_r": _num(row.get("realized_r_multiple")),
        "live_usd": _num(row.get("realized_pl_usd")),
        "live_reason": str(row.get("close_reason") or ""),
        "exit_ts": _num(row.get("exit_time") or row.get("ts")),
        "spread_r": _num(feats.get("spread_r") or row.get("spread_r")),
        "features": feats,
    }, None


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def load_fills(
    rows: list[dict],
    *,
    days: list[str] | None = None,
    symbols: set[str] | None = None,
) -> tuple[list[dict], dict[str, int]]:
    want_days = set(days) if days else None
    want_sym = {s.upper() for s in symbols} if symbols else None
    fills: list[dict] = []
    skip: dict[str, int] = defaultdict(int)
    for row in rows:
        fill, why = parse_fill(row)
        if fill is None:
            skip[why or "unparsed"] += 1
            continue
        if want_days is not None and fill["day"] not in want_days:
            skip["other_day"] += 1
            continue
        if want_sym is not None and fill["symbol"] not in want_sym:
            skip["other_symbol"] += 1
            continue
        fills.append(fill)
    return fills, dict(skip)


# ── config / overlays ────────────────────────────────────────────────────────

def parse_overlay_item(item: str) -> tuple[str, Any]:
    key, sep, raw = item.partition("=")
    key = key.strip()
    if not sep or not key:
        raise ValueError(f"overlay must be key=value, got {item!r}")
    val = raw.strip()
    low = val.lower()
    if low in ("true", "yes", "on"):
        return key, True
    if low in ("false", "no", "off"):
        return key, False
    try:
        if "." in val or val.lower().startswith("e") or "e-" in val.lower():
            return key, float(val)
        return key, int(val)
    except ValueError:
        try:
            return key, float(val)
        except ValueError:
            return key, val


def overlay_from_items(items: list[str] | None) -> dict[str, Any]:
    """CLI ``give_r=0.10`` / ``ai_dead_trade_min=22`` → apply_overlay dict."""
    if not items:
        return {}
    raw: dict[str, Any] = {}
    for item in items:
        k, v = parse_overlay_item(item)
        raw[k] = v
    cfg_keys = {dest: src for src, dest in opt.OVERLAY_KEYS.items()}
    out: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for k, v in raw.items():
        if k in opt.OVERLAY_KEYS:
            out[k] = v
        elif k in cfg_keys:
            out[cfg_keys[k]] = v
        else:
            extra[k] = v
    out["_extra"] = extra
    return out


def apply_plan_overlay(base: dict, overlay: dict) -> dict:
    extra = dict(overlay.get("_extra") or {})
    cell = {k: v for k, v in overlay.items() if k != "_extra"}
    cfg = opt.apply_overlay(base, cell)
    cfg.update(extra)
    return cfg


def risk_per_share(fill: dict, cfg: dict, mode: str) -> float:
    entry = float(fill["entry"])
    stop_pct = float(cfg.get("ai_watch_synth_stop_pct", 5.0) or 5.0) / 100.0
    current = entry * stop_pct if entry > 0 else 0.0
    if mode == "live":
        st = fill.get("live_stop")
        if st is not None and st < entry:
            live = entry - float(st)
            if live > 0:
                return live
    if current > 0:
        return current
    return entry * 0.05 if entry > 0 else 0.0


def open_from_fill(fill: dict, cfg: dict, *, risk_mode: str) -> dict[str, Any] | None:
    entry = float(fill["entry"])
    ts = float(fill["entry_ts"])
    risk = risk_per_share(fill, cfg, risk_mode)
    if entry <= 0 or risk <= 0:
        return None
    stop = entry - risk
    if stop <= 0 or stop >= entry:
        return None
    loc = cp.initial_local_stop(
        entry, risk, cfg, spread_r=fill.get("spread_r")) or stop
    if not bool(cfg.get("ai_local_trail_enabled", True)):
        loc = stop
    t1_rr = max(0.0, float(cfg.get("ai_watch_synth_rr", 0.6) or 0.0))
    scale = max(0.0, min(100.0, float(
        cfg.get("ai_watch_synth_scale_out_pct", 50.0) or 50.0))) / 100.0
    target = entry + t1_rr * risk if t1_rr > 0 else 0.0
    feats = dict(fill.get("features") or {})
    if fill.get("spread_r") is not None:
        feats["spread_r"] = fill["spread_r"]
    return {
        "entry": entry,
        "entry_ts": ts,
        "entry_price": entry,
        "entry_stop_price": stop,
        "risk_per_share": risk,
        "local_stop_price": loc,
        "target_1": target,
        "scale": scale,
        "why_arm": "pinned_fill",
        "peak_price": entry,
        "mfe_r": 0.0,
        "mae_r": 0.0,
        "t1_hit": False,
        "t1_r": None,
        "trail_prints": [entry],
        "trail_last": None,
        "features": feats,
        "spread_r": fill.get("spread_r"),
    }


# ── path walk ────────────────────────────────────────────────────────────────

def _print_ring(cfg: dict) -> int:
    try:
        n = int(cfg.get("ai_local_trail_print_ring", 3) or 3)
    except (TypeError, ValueError):
        n = 3
    return max(2, n)


def _dead_params(cfg: dict) -> tuple[float, float]:
    dead_min = float(cfg.get("ai_dead_trade_min", 22.0) or 0.0)
    dead_mfe = float(cfg.get("ai_dead_trade_mfe_r", 0.10) or 0.0)
    return dead_min, dead_mfe


def _raise_shelf(pos: dict, cfg: dict, px: float) -> None:
    cp.note_trail_print(pos, px, n=_print_ring(cfg))
    want = cp.local_profit_stop(pos, cfg)
    loc = float(pos["local_stop_price"])
    if want is not None and want > loc + 1e-9:
        pos["local_stop_price"] = want


def _dead_now(pos: dict, ts: float, dead_min: float, dead_mfe: float) -> bool:
    if dead_min <= 0:
        return False
    if pos["t1_hit"]:
        return False
    loc = float(pos["local_stop_price"])
    entry = float(pos["entry"])
    if loc > entry + 1e-9:
        return False
    age_min = (ts - float(pos["entry_ts"])) / 60.0
    return age_min + 1e-9 >= dead_min and float(pos["mfe_r"]) < dead_mfe


def _lob_hit(
    ticks: list[dict],
    cfg: dict,
    probe: dict,
) -> tuple[float, float] | None:
    if not ew.left_overbought_exit_enabled(cfg):
        return None
    if not bool(cfg.get("ai_watch_exhaustion_rules", True)):
        return None
    for r in ticks:
        px = _num(r.get("price"))
        ts = _num(r.get("ts"))
        if px is None or ts is None or px <= 0:
            continue
        rec = edge.rec_from_shadow(r)
        rec["exh_was_overbought"] = probe.get("exh_was_overbought")
        hit, _why = ew.exhaustion_exit_now(rec, cfg)
        if rec.get("exh_was_overbought"):
            probe["exh_was_overbought"] = True
        if hit:
            return float(ts), float(px)
    return None


def bars_after_entry(bars: list[Bar], entry_ts: float) -> list[Bar]:
    """Bars whose start is at or after the fill. Skips the in-progress minute."""
    return [b for b in bars if b[0] + 1e-9 >= float(entry_ts)]


def walk_bars(
    fill: dict,
    bars: list[Bar],
    cfg: dict,
    *,
    risk_mode: str = "current",
    shadow_ticks: list[dict] | None = None,
) -> dict[str, Any] | None:
    """Walk 1m OHLC from the pinned fill. None if there is no path after entry."""
    pos = open_from_fill(fill, cfg, risk_mode=risk_mode)
    if pos is None:
        return None
    path = bars_after_entry(bars, fill["entry_ts"])
    if not path:
        return None
    t1_rr = max(0.0, float(cfg.get("ai_watch_synth_rr", 0.6) or 0.0))
    scale = float(pos["scale"])
    dead_min, dead_mfe = _dead_params(cfg)
    ticks = [r for r in (shadow_ticks or [])
             if _num(r.get("ts")) is not None and _num(r.get("price")) is not None]
    ticks.sort(key=lambda r: float(r["ts"]))
    probe: dict[str, Any] = {}
    prev_ts = float(fill["entry_ts"])

    for bar in path:
        ts, _o, h, low, c = bar
        window = [r for r in ticks if prev_ts - 1e-9 <= float(r["ts"]) <= ts + 1e-9]
        lob = _lob_hit(window, cfg, probe)
        if lob is not None:
            closed = _close(pos, lob[0], lob[1], "left_overbought")
            closed["path"] = "bars"
            return closed
        loc = float(pos["local_stop_price"])
        if low <= loc + 1e-9:
            closed = _close(pos, ts, _exit_px(bar, loc), "local_trail")
            closed["path"] = "bars"
            return closed
        entry = float(pos["entry"])
        risk = float(pos["risk_per_share"])
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
        _raise_shelf(pos, cfg, c)
        if _dead_now(pos, ts, dead_min, dead_mfe):
            closed = _close(pos, ts, c, "dead_trade")
            closed["path"] = "bars"
            return closed
        if et_minutes(ts) >= EOD_MIN:
            closed = _close(pos, ts, c, "eod_flatten")
            closed["path"] = "bars"
            return closed
        prev_ts = ts

    last = path[-1]
    why = "eod_flatten" if et_minutes(last[0]) >= EOD_MIN else "tape_end"
    closed = _close(pos, last[0], last[4], why)
    closed["path"] = "bars"
    return closed


def walk_ticks(
    fill: dict,
    ticks: list[dict],
    cfg: dict,
    *,
    risk_mode: str = "current",
) -> dict[str, Any] | None:
    """Shadow-poll fallback. No intra-poll low. None if no later print."""
    pos = open_from_fill(fill, cfg, risk_mode=risk_mode)
    if pos is None:
        return None
    path = []
    for r in ticks:
        ts = _num(r.get("ts"))
        px = _num(r.get("price"))
        if ts is None or px is None or px <= 0:
            continue
        if ts + 1e-9 < float(fill["entry_ts"]):
            continue
        path.append(r)
    if not path:
        return None
    path.sort(key=lambda r: float(r["ts"]))
    t1_rr = max(0.0, float(cfg.get("ai_watch_synth_rr", 0.6) or 0.0))
    scale = float(pos["scale"])
    dead_min, dead_mfe = _dead_params(cfg)
    probe: dict[str, Any] = {}

    for r in path:
        ts = float(r["ts"])
        px = float(r["price"])
        lob = _lob_hit([r], cfg, probe)
        if lob is not None:
            closed = _close(pos, lob[0], lob[1], "left_overbought")
            closed["path"] = "shadow"
            return closed
        loc = float(pos["local_stop_price"])
        if px <= loc + 1e-9:
            closed = _close(pos, ts, px, "local_trail")
            closed["path"] = "shadow"
            return closed
        entry = float(pos["entry"])
        risk = float(pos["risk_per_share"])
        if (
            t1_rr > 0
            and scale > 0
            and not pos["t1_hit"]
            and px + 1e-9 >= float(pos["target_1"])
        ):
            pos["t1_hit"] = True
            pos["t1_r"] = t1_rr
        peak = max(float(pos.get("peak_price") or entry), px)
        pos["peak_price"] = peak
        pos["mfe_r"] = max(float(pos["mfe_r"]), (peak - entry) / risk)
        pos["mae_r"] = min(float(pos["mae_r"]), (px - entry) / risk)
        pos["last_seen_price"] = px
        _raise_shelf(pos, cfg, px)
        if _dead_now(pos, ts, dead_min, dead_mfe):
            closed = _close(pos, ts, px, "dead_trade")
            closed["path"] = "shadow"
            return closed
        if et_minutes(ts) >= EOD_MIN:
            closed = _close(pos, ts, px, "eod_flatten")
            closed["path"] = "shadow"
            return closed

    last = path[-1]
    ts = float(last["ts"])
    px = float(last["price"])
    why = "eod_flatten" if et_minutes(ts) >= EOD_MIN else "tape_end"
    closed = _close(pos, ts, px, why)
    closed["path"] = "shadow"
    return closed


def sim_usd(fill: dict, closed: dict) -> float:
    """Counterfactual $ at the live size. T1 blend lives in closed['r']."""
    entry = float(closed["entry"])
    try:
        risk = entry - float(closed["stop"])
    except (TypeError, ValueError, KeyError):
        risk = 0.0
    if risk <= 0:
        risk = entry * 0.05
    return round(float(closed["r"]) * risk * float(fill["qty"]), 2)


# ── bars cache ───────────────────────────────────────────────────────────────

def cache_key(symbol: str, day: str) -> str:
    return f"{symbol.upper()}|{day}"


def load_bars_cache(path: Path) -> dict[str, list[Bar]]:
    """``{SYM: {day: [{ts,o,h,l,c}|[ts,o,h,l,c]]}}`` or flat SYM|day keys."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("bars", data)
    out: dict[str, list[Bar]] = {}
    if not isinstance(raw, dict):
        return out

    def _one(rows: list) -> list[Bar]:
        bars: list[Bar] = []
        for b in rows or []:
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
        return bars

    for k, v in raw.items():
        if isinstance(v, dict):
            for day, rows in v.items():
                out[cache_key(str(k), str(day))] = _one(rows)
        else:
            out[str(k).upper()] = _one(v)
    return out


def save_bars_cache(
    path: Path,
    cache: dict[str, list[Bar]],
    *,
    feed: str,
    days: list[str],
) -> None:
    nested: dict[str, dict[str, list]] = defaultdict(dict)
    for key, bars in cache.items():
        if "|" in key:
            sym, day = key.split("|", 1)
        else:
            sym, day = key, (days[0] if days else "unknown")
        nested[sym][day] = [
            {"ts": b[0], "o": b[1], "h": b[2], "l": b[3], "c": b[4]}
            for b in bars
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "feed": feed,
        "days": days,
        "bars": nested,
    }, indent=2) + "\n", encoding="utf-8")


def alpaca_keys(cfg: dict) -> tuple[str, str]:
    key = str(cfg.get("api_key") or cfg.get("alpaca_key") or "")
    secret = str(cfg.get("secret_key") or cfg.get("alpaca_secret") or "")
    if key and secret:
        return key, secret
    try:
        sec = json.loads((_ROOT / "config" / "secrets.json").read_text(encoding="utf-8"))
        return str(sec.get("api_key") or ""), str(sec.get("secret_key") or "")
    except Exception:
        return "", ""


def fetch_needed(
    fills: list[dict],
    cache: dict[str, list[Bar]],
    cfg: dict,
    *,
    feed: str,
) -> tuple[dict[str, list[Bar]], dict[str, str]]:
    """Fill missing symbol-days. errors maps cache_key → reason."""
    key, secret = alpaca_keys(cfg)
    errors: dict[str, str] = {}
    needed = {(f["symbol"], f["day"]) for f in fills}
    for sym, day in sorted(needed):
        ck = cache_key(sym, day)
        if cache.get(ck):
            continue
        if not (key and secret):
            errors[ck] = "no_keys"
            continue
        try:
            bars = fetch_day_ohlc(sym, day, key, secret, feed=feed)
        except Exception as e:
            msg = str(e)
            if "401" in msg or "403" in msg:
                errors[ck] = "feed_unauthorized"
                # Paper keys on this laptop 401 SIP. Do not hammer the rest.
                for s2, d2 in needed:
                    ck2 = cache_key(s2, d2)
                    if ck2 not in cache and ck2 not in errors:
                        errors[ck2] = "feed_unauthorized"
                print(
                    "Alpaca 401/403 — this machine cannot fetch bars. "
                    "On the mini after the close: "
                    "--write-bars-cache ai_reports/fill_replay/bars.json "
                    "then replay off-hours from that cache.",
                    file=sys.stderr,
                )
                return cache, errors
            errors[ck] = "fetch"
            continue
        cache[ck] = bars
        if not bars:
            errors[ck] = "empty_bars"
    return cache, errors


def summarize_fetch(errors: dict[str, str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for v in errors.values():
        counts[v.split(":", 1)[0]] += 1
    return dict(counts)


# ── scoring ──────────────────────────────────────────────────────────────────

def half_split_deltas(deltas: list[float]) -> dict[str, Any]:
    n = len(deltas)
    out: dict[str, Any] = {"n_deltas": n}
    if n < 4:
        out["holds_both_halves"] = None
        return out
    half = n // 2
    a, b = deltas[:half], deltas[half:]
    sa, sb = sum(a), sum(b)
    out["half_a"] = round(sa, 2)
    out["half_b"] = round(sb, 2)
    out["holds_both_halves"] = bool(sa * sb > 0)
    return out


def decide(
    *,
    n: int,
    delta_usd: float | None,
    holds: bool | None,
    min_n: int,
    n_sessions: int = 0,
    min_sessions: int = 5,
) -> str:
    """Session is the unit. Two green days are not a candidate."""
    if delta_usd is None:
        return VERDICT_NO_SCORE
    thin = n < min_n or n_sessions < min_sessions
    if thin:
        return VERDICT_HYPOTHESIS if delta_usd > 0 else VERDICT_DO_NOT
    if holds is False:
        return VERDICT_DO_NOT
    if delta_usd > 0 and holds is True:
        return VERDICT_CANDIDATE
    if delta_usd > 0:
        return VERDICT_HYPOTHESIS
    return VERDICT_DO_NOT


def replay_fill(
    fill: dict,
    cfg: dict,
    *,
    bars: list[Bar] | None,
    ticks: list[dict] | None,
    risk_mode: str,
    allow_shadow: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    if bars:
        closed = walk_bars(
            fill, bars, cfg, risk_mode=risk_mode, shadow_ticks=ticks)
        if closed is not None:
            return closed, None
    if allow_shadow and ticks:
        closed = walk_ticks(fill, ticks, cfg, risk_mode=risk_mode)
        if closed is not None:
            return closed, None
    if not bars and not (allow_shadow and ticks):
        return None, "no_path"
    return None, "no_path"


def score_fills(
    fills: list[dict],
    cfg: dict,
    *,
    bar_cache: dict[str, list[Bar]],
    shadow_by_sym: dict[str, list[dict]],
    risk_mode: str = "current",
    allow_shadow: bool = True,
    min_n: int = MIN_N,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    skip: dict[str, int] = defaultdict(int)
    for fill in fills:
        ck = cache_key(fill["symbol"], fill["day"])
        bars = bar_cache.get(ck) or []
        ticks = shadow_by_sym.get(fill["symbol"]) or []
        closed, why = replay_fill(
            fill, cfg, bars=bars, ticks=ticks,
            risk_mode=risk_mode, allow_shadow=allow_shadow,
        )
        if closed is None:
            skip[why or "no_path"] += 1
            continue
        usd = sim_usd(fill, closed)
        live_usd = fill.get("live_usd")
        delta = None if live_usd is None else round(usd - float(live_usd), 2)
        rows.append({
            "symbol": fill["symbol"],
            "day": fill["day"],
            "entry_ts": fill["entry_ts"],
            "entry": closed["entry"],
            "qty": fill["qty"],
            "live_exit": fill.get("live_exit"),
            "live_r": fill.get("live_r"),
            "live_usd": live_usd,
            "live_reason": fill.get("live_reason"),
            "sim_exit": closed["exit"],
            "sim_r": closed["r"],
            "sim_usd": usd,
            "sim_reason": closed["reason"],
            "delta_usd": delta,
            "path": closed.get("path"),
            "t1_hit": closed.get("t1_hit"),
            "mfe_r": closed.get("mfe_r"),
            "hold_min": closed.get("hold_min"),
            "rstop": closed.get("rstop"),
        })

    scored = [r for r in rows if r.get("delta_usd") is not None]
    live_total = round(sum(float(r["live_usd"]) for r in scored), 2)
    sim_total = round(sum(float(r["sim_usd"]) for r in rows), 2)
    scored_sim = round(sum(float(r["sim_usd"]) for r in scored), 2)
    delta_total = round(scored_sim - live_total, 2) if scored else None
    deltas = [float(r["delta_usd"]) for r in scored]
    halves = half_split_deltas(deltas)
    by_day: dict[str, dict[str, Any]] = {}
    for r in scored:
        d = by_day.setdefault(r["day"], {"n": 0, "live_usd": 0.0, "sim_usd": 0.0})
        d["n"] += 1
        d["live_usd"] += float(r["live_usd"])
        d["sim_usd"] += float(r["sim_usd"])
    sessions = []
    for day in sorted(by_day):
        d = by_day[day]
        sessions.append({
            "day": day,
            "n": d["n"],
            "live_usd": round(d["live_usd"], 2),
            "sim_usd": round(d["sim_usd"], 2),
            "delta_usd": round(d["sim_usd"] - d["live_usd"], 2),
        })
    n_sim_pos = sum(1 for s in sessions if s["sim_usd"] > 0)
    n_live_pos = sum(1 for s in sessions if s["live_usd"] > 0)
    sess_halves = half_split_deltas([s["delta_usd"] for s in sessions])
    verdict = decide(
        n=len(scored), n_sessions=len(sessions),
        delta_usd=delta_total,
        holds=sess_halves.get("holds_both_halves"), min_n=min_n,
    )
    n_shadow = sum(1 for r in rows if r.get("path") == "shadow")
    return {
        "n_fills": len(fills),
        "n_walked": len(rows),
        "n_scored": len(scored),
        "n_shadow": n_shadow,
        "skip": dict(skip),
        "live_usd": live_total,
        "sim_usd": sim_total,
        "scored_sim_usd": scored_sim,
        "delta_usd": delta_total,
        "verdict": verdict,
        **sess_halves,
        "trade_halves": halves,
        "sessions": sessions,
        "n_sessions": len(sessions),
        "n_sim_pos_sessions": n_sim_pos,
        "n_live_pos_sessions": n_live_pos,
        "rows": rows,
    }


def action_line(payload: dict) -> str:
    v = payload.get("verdict")
    d = payload.get("delta_usd")
    n = int(payload.get("n_scored") or 0)
    if payload.get("n_walked") == 0:
        return "nothing walked — no path for these fills (bars cache or keys)"
    if v == VERDICT_CANDIDATE:
        return (
            f"candidate vs live Δ${d:+.2f} n={n} — still do not write config; "
            "review the overlay before the next session"
        )
    if v == VERDICT_HYPOTHESIS:
        return (
            f"hypothesis Δ${d:+.2f} n={n} — underpowered or one-sided; "
            "do not change config"
        )
    if v == VERDICT_DO_NOT:
        return (
            f"do not promote Δ${d:+.2f} n={n} — keep live config for the next session"
        )
    return "no scored delta — do not change config"


# ── report ───────────────────────────────────────────────────────────────────

def _label(days: list[str]) -> str:
    if not days:
        return "empty"
    if len(days) == 1:
        return days[0]
    return f"{days[0]}_{days[-1]}"


def render_md(payload: dict) -> str:
    days = payload.get("days") or []
    lines = [
        f"# Fill replay {_label(days)}",
        "",
        "Pinned live fills, current (or overlaid) trail. Does not write config.",
        "",
        (
            f"- fills {payload.get('n_fills')}  walked {payload.get('n_walked')}  "
            f"scored {payload.get('n_scored')}  shadow-path {payload.get('n_shadow')}"
        ),
        (
            f"- live ${payload.get('live_usd'):+.2f}  "
            f"sim ${payload.get('scored_sim_usd'):+.2f}  "
            f"Δ ${payload.get('delta_usd') if payload.get('delta_usd') is not None else 'n/a'}"
        ),
        (
            f"- sessions {payload.get('n_sessions')}  "
            f"sim+ {payload.get('n_sim_pos_sessions')}/{payload.get('n_sessions')}  "
            f"live+ {payload.get('n_live_pos_sessions')}/{payload.get('n_sessions')}"
        ),
        f"- verdict **{payload.get('verdict')}**",
        f"- {payload.get('action')}",
        (
            f"- risk `{payload.get('risk_mode')}`  "
            f"product `{payload.get('desk_product')}`  "
            f"overlay `{json.dumps(payload.get('overlay') or {}, default=str)}`"
        ),
        "",
    ]
    skip = payload.get("skip") or {}
    if skip:
        bits = ", ".join(f"{k}×{v}" for k, v in sorted(skip.items(), key=lambda kv: -kv[1]))
        lines.append(f"Skipped: {bits}")
        lines.append("")
    lines.append("| day | n | live $ | sim $ | Δ $ |")
    lines.append("|---|---:|---:|---:|---:|")
    for s in payload.get("sessions") or []:
        lines.append(
            f"| {s['day']} | {s['n']} | {s['live_usd']:+.2f} | "
            f"{s['sim_usd']:+.2f} | {s['delta_usd']:+.2f} |"
        )
    lines += [
        "",
        "## Honesty",
        "",
        "- Fills are pinned. Entries are not re-decided.",
        "- 1m OHLC: same-minute stomps can be missed; high and low both print.",
        "- Missing bars are skipped, not 0R.",
        "- Shadow fallback has no intra-poll low.",
        "- This is not a go-live pass. A green overlay is a next-session hypothesis.",
        "",
    ]
    return "\n".join(lines) + "\n"


def print_report(payload: dict, *, show_trades: bool) -> None:
    days = payload.get("days") or []
    dlt = payload.get("delta_usd")
    dlt_s = f"{dlt:+.2f}" if dlt is not None else "n/a"
    print()
    print(f"=== fill replay  {_label(days)}  "
          f"walked={payload.get('n_walked')}  scored={payload.get('n_scored')} ===")
    print(f"live  ${payload.get('live_usd'):+.2f}  "
          f"sim  ${payload.get('scored_sim_usd'):+.2f}  Δ ${dlt_s}")
    print(f"sessions {payload.get('n_sessions')}  "
          f"sim+ {payload.get('n_sim_pos_sessions')}/{payload.get('n_sessions')}  "
          f"live+ {payload.get('n_live_pos_sessions')}/{payload.get('n_sessions')}")
    print(f"verdict {payload.get('verdict')}  risk={payload.get('risk_mode')}")
    print(payload.get("action"))
    skip = payload.get("skip") or {}
    if skip:
        bits = ", ".join(f"{k}×{v}" for k, v in sorted(skip.items(), key=lambda kv: -kv[1]))
        print(f"skip: {bits}")
    if payload.get("n_shadow"):
        print(f"shadow-path rows: {payload['n_shadow']} (no intra-poll low)")
    print()
    print(f"{'day':<12}{'n':>4}{'live$':>10}{'sim$':>10}{'Δ$':>10}")
    for s in payload.get("sessions") or []:
        print(f"{s['day']:<12}{s['n']:4d}{s['live_usd']:10.2f}"
              f"{s['sim_usd']:10.2f}{s['delta_usd']:10.2f}")
    if show_trades:
        print()
        for r in payload.get("rows") or []:
            when = datetime.fromtimestamp(float(r["entry_ts"]), ET).strftime("%H:%M")
            live_u = r.get("live_usd")
            live_s = f"{live_u:+.2f}" if live_u is not None else "n/a"
            d = r.get("delta_usd")
            ds = f"{d:+.2f}" if d is not None else "n/a"
            print(
                f"  {r['symbol']:<6} {r['day']} {when}  "
                f"{r['entry']:.2f}→{r['sim_exit']:.2f}  "
                f"live {live_s}  sim {r['sim_usd']:+.2f}  Δ {ds}  "
                f"{r['sim_reason']}  {r.get('path')}"
            )
    print()
    print("fills pinned; 1m bars; missing is not zero; do not write config")


def write_artifacts(payload: dict) -> dict[str, str]:
    rd = resolve_report_dir() / "fill_replay"
    rd.mkdir(parents=True, exist_ok=True)
    label = _label(payload.get("days") or [])
    json_path = rd / f"{label}.json"
    md_path = rd / f"{label}.md"
    slim = dict(payload)
    # Keep the json readable; full rows stay but dates are few hundred max.
    json_path.write_text(json.dumps(slim, indent=2, default=str) + "\n", encoding="utf-8")
    md_path.write_text(render_md(payload), encoding="utf-8")
    return {"json": str(json_path), "md": str(md_path)}


# ── driver ───────────────────────────────────────────────────────────────────

def resolve_outcomes_path(
    *,
    tape: Path | None,
    outcomes: Path | None,
) -> Path:
    if outcomes is not None:
        return outcomes
    if tape is not None:
        return Path(tape) / "outcomes.jsonl"
    found = find_report_file("outcomes.jsonl")
    return found or (resolve_report_dir() / "outcomes.jsonl")


def shadow_index(rows: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        if _num(r.get("price")) is None or _num(r.get("ts")) is None:
            continue
        by[sym].append(r)
    for rows in by.values():
        rows.sort(key=lambda r: float(r["ts"]))
    return dict(by)


def run(
    *,
    days: list[str],
    cfg: dict,
    fills: list[dict],
    skip_load: dict[str, int],
    bar_cache: dict[str, list[Bar]],
    shadow_by_sym: dict[str, list[dict]],
    overlay: dict,
    risk_mode: str,
    allow_shadow: bool,
    min_n: int = MIN_N,
    write: bool = False,
) -> dict[str, Any]:
    cfg_used = apply_plan_overlay(cfg, overlay) if overlay else dict(cfg)
    scored = score_fills(
        fills, cfg_used,
        bar_cache=bar_cache,
        shadow_by_sym=shadow_by_sym,
        risk_mode=risk_mode,
        allow_shadow=allow_shadow,
        min_n=min_n,
    )
    skip = dict(skip_load)
    for k, v in (scored.get("skip") or {}).items():
        skip[k] = skip.get(k, 0) + v
    overlay_pub = {k: v for k, v in overlay.items() if k != "_extra"}
    overlay_pub.update(overlay.get("_extra") or {})
    payload = {
        "ok": True,
        "days": days,
        "desk_product": cfg.get("desk_product"),
        "risk_mode": risk_mode,
        "overlay": overlay_pub,
        "allow_shadow": allow_shadow,
        "min_n": min_n,
        **scored,
        "skip": skip,
        "skip_load": skip_load,
    }
    payload["action"] = action_line(payload)
    if write:
        payload["paths"] = write_artifacts(payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("symbols", nargs="*", help="optional symbol filter")
    ap.add_argument("--days", type=int, default=None,
                    help="last N Mon–Fri ET sessions (default 10)")
    ap.add_argument("--day", action="append", default=[],
                    help="ET day YYYY-MM-DD (repeatable)")
    ap.add_argument("--from", dest="date_from", default="",
                    help="range start YYYY-MM-DD (inclusive, weekdays)")
    ap.add_argument("--to", dest="date_to", default="",
                    help="range end YYYY-MM-DD (inclusive, weekdays)")
    ap.add_argument("--tape", default="", help="packed tape directory")
    ap.add_argument("--outcomes", default="", help="outcomes.jsonl path")
    ap.add_argument("--shadow", default="", help="shadow.jsonl path")
    ap.add_argument("--bars-cache", default="",
                    help="JSON bars cache (off-hours, no keys needed)")
    ap.add_argument("--write-bars-cache", default="",
                    help="write/merge fetched bars here after the run")
    ap.add_argument("--bars-file", default="",
                    help="one-day {SYM: [{ts,o,h,l,c}]} file (tests / one-offs)")
    ap.add_argument("--feed", choices=("iex", "sip"), default="sip")
    ap.add_argument("--overlay", action="append", default=[],
                    help="knob=value (repeatable). give_r=0.10, trail_enabled=false, …")
    ap.add_argument("--risk", choices=("current", "live"), default="current",
                    help="current = 1R from synth_stop_pct; live = fill's stop")
    ap.add_argument("--require-bars", action="store_true",
                    help="do not fall back to shadow polls")
    ap.add_argument("--no-fetch", action="store_true",
                    help="never call Alpaca; bars-cache / shadow only (off-hours on this laptop)")
    ap.add_argument("--min-n", type=int, default=MIN_N)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--trades", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    try:
        days = resolve_days(
            day=args.day, date_from=args.date_from, date_to=args.date_to,
            days_n=args.days,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    tape = Path(args.tape) if args.tape else None
    outcomes_path = resolve_outcomes_path(
        tape=tape,
        outcomes=Path(args.outcomes) if args.outcomes else None,
    )
    if not outcomes_path.exists():
        print(
            f"no outcomes at {outcomes_path}\n"
            "this clone often has no live ledger (mini holds ai_reports/). "
            "pass --outcomes or --tape.",
            file=sys.stderr,
        )
        return 2

    rows = load_jsonl(outcomes_path)
    if tape is not None:
        packed = desk_tape.load(tape)
        if packed.get("outcomes"):
            rows = packed["outcomes"]
        shadow_rows = packed.get("shadow") or []
    else:
        shadow_rows = []
    if args.shadow:
        shadow_rows = load_jsonl(Path(args.shadow))
    elif not shadow_rows:
        sp = find_report_file("shadow.jsonl")
        if tape is not None and (Path(tape) / "shadow.jsonl").exists():
            sp = Path(tape) / "shadow.jsonl"
        if sp:
            shadow_rows = load_jsonl(sp)

    want_sym = {s.upper() for s in args.symbols} if args.symbols else None
    fills, skip_load = load_fills(rows, days=days, symbols=want_sym)
    # Drop the other_day census from the headline skip — it is the window, not a fault.
    skip_load.pop("other_day", None)
    skip_load.pop("other_symbol", None)

    cfg = opt.live_cfg()
    try:
        overlay = overlay_from_items(args.overlay)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    bar_cache: dict[str, list[Bar]] = {}
    if args.bars_cache:
        bar_cache.update(load_bars_cache(Path(args.bars_cache)))
    if args.bars_file:
        from sim_rstop_path import load_bars_file
        book = load_bars_file(Path(args.bars_file))
        day0 = days[0] if days else ""
        for sym, bars in book.items():
            bar_cache[cache_key(sym, day0)] = bars
            for f in fills:
                if f["symbol"] == sym:
                    bar_cache[cache_key(sym, f["day"])] = bars

    allow_shadow = not args.require_bars
    fetch_err: dict[str, str] = {}
    if not args.no_fetch and not args.bars_file:
        bar_cache, fetch_err = fetch_needed(
            fills, bar_cache, cfg, feed=args.feed)

    if args.write_bars_cache:
        save_bars_cache(
            Path(args.write_bars_cache), bar_cache, feed=args.feed, days=days)

    payload = run(
        days=days,
        cfg=cfg,
        fills=fills,
        skip_load=skip_load,
        bar_cache=bar_cache,
        shadow_by_sym=shadow_index(shadow_rows),
        overlay=overlay,
        risk_mode=args.risk,
        allow_shadow=allow_shadow,
        min_n=args.min_n,
        write=not args.no_write,
    )
    payload["outcomes_path"] = str(outcomes_path)
    payload["fetch_errors"] = summarize_fetch(fetch_err)
    if cfg.get("desk_product") == "observe":
        payload["observe_note"] = (
            "desk_product=observe does not zero this run; fills are pinned"
        )

    if args.json:
        slim = dict(payload)
        if not args.trades:
            slim["rows"] = [
                {k: r[k] for k in (
                    "symbol", "day", "live_usd", "sim_usd", "delta_usd",
                    "live_reason", "sim_reason", "path",
                ) if k in r}
                for r in (payload.get("rows") or [])
            ]
        print(json.dumps(slim, indent=2, default=str))
        return 0
    print_report(payload, show_trades=args.trades)
    if payload.get("paths"):
        print("wrote", payload["paths"].get("md"))
    if payload.get("observe_note"):
        print(payload["observe_note"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
