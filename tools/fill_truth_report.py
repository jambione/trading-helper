#!/usr/bin/env python3
"""Fill-truth paper report — Alpaca fills as source of truth.

Unlike ``paper_report.py`` (engine signal_log mark-to-signal prices), this
pulls **actual paper fills** from Alpaca and attributes them to:

  * ``ai``     — symbol appears in AI managed state / trades.jsonl / outcomes
  * ``engine`` — symbol appears only in signal_log.json BUYs
  * ``manual`` — neither (desk hotkey or other)

Usage (from repo root, with paper keys in signal_engine.env)::

    python tools/fill_truth_report.py
    python tools/fill_truth_report.py --days 14
    python tools/fill_truth_report.py --json
    python tools/fill_truth_report.py --offline   # local files only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ET = ZoneInfo("America/New_York")
SIGNAL_LOG = ROOT / "signal_log.json"


import desk_core  # noqa: E402

_load_env = desk_core.load_env_file


def _read_jsonl(path: Path) -> list[dict]:
    if not path or not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


_META_KEYS = frozenset({
    "updated", "mode", "error", "positions", "open_orders", "performance",
    "book_owner", "trading", "recent_events", "last_reconcile", "ok",
    "n_pos", "equity", "source", "rows", "suggestions", "items",
})


def _looks_like_ticker(sym: str) -> bool:
    s = (sym or "").upper()
    if not s or s in _META_KEYS or len(s) > 6:
        return False
    if not s[0].isalpha():
        return False
    return all(c.isalnum() or c in ".-" for c in s)


def _collect_syms_from_state(raw: Any) -> set[str]:
    if not isinstance(raw, dict):
        return set()
    if isinstance(raw.get("positions"), dict):
        raw = raw["positions"]
    out: set[str] = set()
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        ku = k.upper()
        if not _looks_like_ticker(ku):
            continue
        if isinstance(v, dict) or v is None:
            out.add(ku)
    return out


def _ai_symbols() -> set[str]:
    from ai_paths import find_report_file, resolve_report_dir

    syms: set[str] = set()
    for name in ("positions_state.json",):
        p = find_report_file(name)
        if p is None:
            p = resolve_report_dir() / name
        try:
            state = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            state = {}
        syms |= _collect_syms_from_state(state)

    for name in ("trades.jsonl", "outcomes.jsonl", "events.jsonl"):
        p = find_report_file(name)
        if p is None:
            continue
        for row in _read_jsonl(p):
            s = str(row.get("symbol") or row.get("ticker") or "").upper()
            if _looks_like_ticker(s):
                syms.add(s)
    for wire in (ROOT / "ai_positions_state.json",
                 ROOT / "claude_positions_state.json"):
        try:
            if wire.exists():
                st = json.loads(wire.read_text(encoding="utf-8"))
                syms |= _collect_syms_from_state(st)
        except Exception:
            pass
    return syms


def _engine_symbols() -> set[str]:
    if not SIGNAL_LOG.exists():
        return set()
    try:
        entries = json.loads(SIGNAL_LOG.read_text(encoding="utf-8"))
    except Exception:
        return set()
    out: set[str] = set()
    for e in entries or []:
        if e.get("action") in ("BUY", "SELL"):
            t = str(e.get("ticker") or "").upper()
            if t:
                out.add(t)
    return out


def _pair_round_trips(fills: list[dict]) -> list[dict]:
    """Naive FIFO long round-trips from fill stream (paper book)."""
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for f in sorted(fills, key=lambda r: r.get("filled_at") or r.get("submitted_at") or ""):
        by_sym[f["symbol"]].append(f)

    closed: list[dict] = []
    open_lots: dict[str, list[dict]] = defaultdict(list)

    for sym, rows in by_sym.items():
        for f in rows:
            side = f.get("side")
            qty = float(f.get("filled_qty") or 0)
            px = float(f.get("filled_avg_price") or 0)
            if qty <= 0 or px <= 0:
                continue
            if side == "buy":
                open_lots[sym].append({"qty": qty, "px": px, "ts": f.get("filled_at")})
            elif side == "sell":
                remain = qty
                while remain > 1e-9 and open_lots[sym]:
                    lot = open_lots[sym][0]
                    take = min(remain, lot["qty"])
                    entry = lot["px"]
                    pnl_pct = (px - entry) / entry * 100.0 if entry else 0.0
                    closed.append({
                        "symbol": sym,
                        "qty": take,
                        "entry": entry,
                        "exit": px,
                        "pnl_pct": pnl_pct,
                        "buy_time": lot.get("ts"),
                        "sell_time": f.get("filled_at"),
                        "source": f.get("source") or "unknown",
                    })
                    lot["qty"] -= take
                    remain -= take
                    if lot["qty"] <= 1e-9:
                        open_lots[sym].pop(0)
    return closed


def tag_fills(fills: list[dict], ai_syms: set[str], eng_syms: set[str]) -> list[dict]:
    out = []
    for f in fills:
        sym = f.get("symbol") or ""
        if sym in ai_syms:
            src = "ai"
        elif sym in eng_syms:
            src = "engine"
        else:
            src = "manual"
        row = dict(f)
        row["source"] = src
        out.append(row)
    return out


def fetch_alpaca_fills(days: int, limit: int) -> list[dict]:
    _load_env()
    api = os.getenv("ALPACA_API_KEY", "").strip()
    sec = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not api or not sec:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY missing")
    import alpaca_trader
    alpaca_trader.init(
        mode="paper", api_key=api, secret_key=sec,
        trade_amount=1000.0, extended_hours=True,
    )
    if not alpaca_trader.is_active():
        raise RuntimeError("alpaca paper client not active")
    return alpaca_trader.get_filled_orders(limit=limit, days=days)


def _stats(closed: list[dict]) -> dict[str, Any]:
    if not closed:
        return {"trades": 0}
    pnls = [float(t["pnl_pct"]) for t in closed]
    wins = [p for p in pnls if p > 0]
    return {
        "trades": len(closed),
        "wins": len(wins),
        "win_rate": round(100.0 * len(wins) / len(closed), 1),
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3),
        "total_pnl_pct": round(sum(pnls), 3),
        "best": round(max(pnls), 3),
        "worst": round(min(pnls), 3),
    }


def render(fills: list[dict], closed: list[dict], open_pos: dict | None) -> None:
    print("=" * 78)
    print("  FILL-TRUTH PAPER REPORT (Alpaca fills)")
    print("=" * 78)
    print(f"  Filled order legs : {len(fills)}")
    by_src = defaultdict(int)
    for f in fills:
        by_src[f.get("source") or "?"] += 1
    print("  Legs by source   : " + ", ".join(
        f"{k}={v}" for k, v in sorted(by_src.items())))

    print("-" * 78)
    print("  ROUND-TRIPS (FIFO longs from fills)")
    overall = _stats(closed)
    if not overall.get("trades"):
        print("  (no completed long round-trips in window)")
    else:
        print(f"  Trades {overall['trades']}  win {overall['win_rate']}%  "
              f"avg {overall['avg_pnl_pct']:+.3f}%  "
              f"total {overall['total_pnl_pct']:+.3f}%  "
              f"best {overall['best']:+.3f}%  worst {overall['worst']:+.3f}%")

    print("-" * 78)
    print("  BY SOURCE")
    for src in ("ai", "engine", "manual"):
        subset = [t for t in closed if t.get("source") == src]
        s = _stats(subset)
        if not s.get("trades"):
            print(f"  {src:<8} (no trades)")
        else:
            print(f"  {src:<8} {s['trades']:>3} trades  win {s['win_rate']:>5.1f}%  "
                  f"avg {s['avg_pnl_pct']:+.3f}%  total {s['total_pnl_pct']:+.3f}%")

    if open_pos:
        print("-" * 78)
        print(f"  OPEN ALPACA POSITIONS ({len(open_pos)})")
        for sym, p in sorted(open_pos.items()):
            print(f"    {sym:<6} qty={p.get('qty')}  "
                  f"avg={p.get('avg_entry_price')}")
    print("=" * 78)
    print("  Note: source tags are symbol-level heuristics (AI state / signal_log).")
    print("  Prefer this over signal-time P&L for paper expectancy.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Alpaca fill-truth paper report")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="Skip Alpaca; print local AI symbols / files only")
    args = ap.parse_args()

    ai_syms = _ai_symbols()
    eng_syms = _engine_symbols()

    if args.offline:
        payload = {
            "ai_symbols": sorted(ai_syms),
            "engine_symbols": sorted(eng_syms),
            "fills": [],
            "closed": [],
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"AI symbols ({len(ai_syms)}): {', '.join(sorted(ai_syms)[:40])}")
            print(f"Engine symbols ({len(eng_syms)}): "
                  f"{', '.join(sorted(eng_syms)[:40])}")
        return 0

    try:
        fills = fetch_alpaca_fills(args.days, args.limit)
    except Exception as e:
        print(f"[fill_truth] Alpaca error: {e}", file=sys.stderr)
        print("Hint: set paper keys in signal_engine.env, or use --offline",
              file=sys.stderr)
        return 1

    fills = tag_fills(fills, ai_syms, eng_syms)
    closed = _pair_round_trips(fills)
    # Propagate source onto round-trips from exit leg
    for t in closed:
        if not t.get("source") or t["source"] == "unknown":
            sym = t["symbol"]
            if sym in ai_syms:
                t["source"] = "ai"
            elif sym in eng_syms:
                t["source"] = "engine"
            else:
                t["source"] = "manual"

    open_pos = None
    try:
        import alpaca_trader
        open_pos = alpaca_trader.get_open_positions()
    except Exception:
        pass

    if args.json:
        print(json.dumps({
            "fills": fills,
            "closed": closed,
            "stats": {
                "overall": _stats(closed),
                "by_source": {
                    s: _stats([t for t in closed if t.get("source") == s])
                    for s in ("ai", "engine", "manual")
                },
            },
            "open_positions": open_pos or {},
            "ai_symbols": sorted(ai_syms),
            "engine_symbols": sorted(eng_syms),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2, default=str))
    else:
        render(fills, closed, open_pos)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
