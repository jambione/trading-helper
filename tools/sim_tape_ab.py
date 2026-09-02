#!/usr/bin/env python3
"""A/B entry filters + exit overlays on a packed day tape (off-hours).

Builds on ``sim_fill_replay`` (trail walk, bars cache, overlays) and adds the
entry counterfactual that fill-replay deliberately skips:

  1. Shadow admission — walk ``arm_ok`` rows; count how many NEW gates block.
  2. Fill filter — match each live fill to the latest prior arm_ok shadow row,
     drop fills that fail the variant's entry filters, then replay exits with
     the variant's give overlay.

Does not write ``bot_config.json``. Full bar-by-bar re-arm is out of scope.

USAGE
    # Named A/B (typical overnight question):
    .venv/bin/python tools/sim_tape_ab.py --day 2026-09-01 \\
        --tape ai_reports/tapes/2026-09-01 --no-fetch \\
        --bars-cache ai_reports/fill_replay/bars.json \\
        --a 'rest+give=0.2' --b 'stream+rt_macd+give=0.35'

    # Explicit flags (single variant; pair with a second run):
    .venv/bin/python tools/sim_tape_ab.py --day 2026-09-01 \\
        --tape ai_reports/tapes/2026-09-01 --no-fetch \\
        --bars-cache ai_reports/fill_replay/bars.json \\
        --require-stream-price --require-realtime-macd \\
        --overlay give_r=0.35

Variant string tokens (``+``-joined, case-insensitive):
  rest | allow_rest     allow REST / non-stream decision price
  stream                require last_ask_src == stream
  rt_macd               require macd_src == realtime
  rt_rsi                require cm_rsi_src == realtime
  give=0.20             overlay give_r (+ give_open_r via optimize_rstop)
  give_open_r=0.20      optional open-period give pin
  any other key=value   passed through as overlay (same as fill_replay)

Honesty
  • Live fills are the entry universe; we only drop ones that fail filters.
  • Arm provenance comes from the nearest prior arm_ok shadow (≤ match window).
  • Exit path is 1m OHLC (same caveats as sim_fill_replay).
  • Does not re-decide arms from scratch on every bar.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import desk_core  # noqa: E402
desk_core.load_desk_env(_ROOT / "signal_engine.env")

import desk_tape  # noqa: E402
import optimize_rstop as opt  # noqa: E402
import sim_fill_replay as sfr  # noqa: E402

DEFAULT_MATCH_SEC = 180.0


# ── variant / filters ────────────────────────────────────────────────────────

@dataclass
class EntryFilters:
    require_stream_price: bool = False
    require_realtime_macd: bool = False
    require_realtime_rsi: bool = False
    macd_max_age_sec: float = 0.0  # 0 = source-only (matches live default)

    def label_bits(self) -> list[str]:
        bits: list[str] = []
        bits.append("stream" if self.require_stream_price else "rest")
        if self.require_realtime_macd:
            bits.append("rt_macd")
        if self.require_realtime_rsi:
            bits.append("rt_rsi")
        return bits


@dataclass
class Variant:
    name: str
    filters: EntryFilters = field(default_factory=EntryFilters)
    overlay: dict[str, Any] = field(default_factory=dict)

    def short_label(self) -> str:
        bits = self.filters.label_bits()
        ov = {k: v for k, v in self.overlay.items() if k != "_extra"}
        ov.update(self.overlay.get("_extra") or {})
        if "give_r" in ov:
            bits.append(f"give={ov['give_r']}")
        elif "ai_local_trail_give_r" in ov:
            bits.append(f"give={ov['ai_local_trail_give_r']}")
        return "+".join(bits) if bits else self.name


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ask_src(row: dict) -> str:
    return str(
        row.get("last_ask_src") or row.get("price_src") or ""
    ).strip().lower()


def macd_src(row: dict) -> str:
    feats = row.get("features") if isinstance(row.get("features"), dict) else {}
    return str(
        row.get("macd_src") or feats.get("macd_src") or row.get("bars_src") or ""
    ).strip().lower()


def rsi_src(row: dict) -> str:
    feats = row.get("features") if isinstance(row.get("features"), dict) else {}
    return str(
        row.get("cm_rsi_src") or feats.get("cm_rsi_src") or ""
    ).strip().lower()


def macd_age(row: dict) -> float | None:
    feats = row.get("features") if isinstance(row.get("features"), dict) else {}
    return _num(
        row.get("macd_age_sec")
        if row.get("macd_age_sec") is not None
        else feats.get("macd_age_sec")
        if feats.get("macd_age_sec") is not None
        else row.get("bars_age_sec")
    )


def entry_filter_block(row: dict, filters: EntryFilters) -> str | None:
    """Return a block reason, or None if the row would still arm."""
    if filters.require_stream_price:
        src = ask_src(row)
        if src != "stream":
            return "stream_required"
    if filters.require_realtime_macd:
        src = macd_src(row)
        if src != "realtime":
            return "macd_not_realtime" if src else "macd_src_unknown"
        if filters.macd_max_age_sec > 0:
            age = macd_age(row)
            if age is None:
                return "macd_src_unknown"
            if age > filters.macd_max_age_sec:
                return "macd_stale_bars"
    if filters.require_realtime_rsi:
        src = rsi_src(row)
        if src != "realtime":
            return "rsi_not_realtime" if src else "rsi_src_unknown"
    return None


def parse_variant(spec: str, *, name: str = "") -> Variant:
    """Parse ``rest+give=0.2`` / ``stream+rt_macd+rt_rsi+give=0.35``."""
    raw = (spec or "").strip()
    if not raw:
        raise ValueError("empty variant spec")
    filters = EntryFilters()
    overlay_items: list[str] = []
    saw_price = False
    for tok in raw.replace(",", "+").split("+"):
        t = tok.strip()
        if not t:
            continue
        low = t.lower()
        if low in ("rest", "allow_rest", "allow-rest"):
            filters.require_stream_price = False
            saw_price = True
            continue
        if low in ("stream", "require_stream", "require-stream"):
            filters.require_stream_price = True
            saw_price = True
            continue
        if low in ("rt_macd", "realtime_macd", "require_realtime_macd"):
            filters.require_realtime_macd = True
            continue
        if low in ("rt_rsi", "realtime_rsi", "require_realtime_rsi"):
            filters.require_realtime_rsi = True
            continue
        if "=" in t:
            key, _, val = t.partition("=")
            key_l = key.strip().lower()
            # CLI shorthand: give=0.2 means give_r (and give_open via apply_overlay)
            if key_l == "give":
                t = f"give_r={val.strip()}"
            overlay_items.append(t)
            continue
        raise ValueError(f"unknown variant token {t!r} in {spec!r}")
    if not saw_price:
        # Default: allow REST unless stream is named.
        filters.require_stream_price = False
    overlay = sfr.overlay_from_items(overlay_items) if overlay_items else {}
    v = Variant(name=name or raw, filters=filters, overlay=overlay)
    return v


# ── shadow arms / matching ───────────────────────────────────────────────────

def arm_events(shadow_rows: list[dict]) -> list[dict]:
    """Chronological arm_ok=True shadow rows."""
    out: list[dict] = []
    for r in shadow_rows:
        if not isinstance(r, dict):
            continue
        if r.get("arm_ok") is not True:
            continue
        if _num(r.get("ts")) is None:
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        out.append(r)
    out.sort(key=lambda r: (float(r["ts"]), str(r.get("symbol") or "")))
    return out


def shadow_arms_by_sym(shadow_rows: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in arm_events(shadow_rows):
        by[str(r["symbol"]).upper()].append(r)
    return dict(by)


def match_arm_for_fill(
    fill: dict,
    arms_by_sym: dict[str, list[dict]],
    *,
    match_sec: float = DEFAULT_MATCH_SEC,
) -> dict | None:
    """Latest arm_ok for symbol with ts <= entry_ts within match_sec."""
    sym = str(fill.get("symbol") or "").upper()
    et = _num(fill.get("entry_ts"))
    if not sym or et is None:
        return None
    rows = arms_by_sym.get(sym) or []
    best: dict | None = None
    for r in rows:
        ts = float(r["ts"])
        if ts <= et + 1e-6:
            best = r
        else:
            break
    if best is None:
        return None
    if et - float(best["ts"]) > float(match_sec) + 1e-9:
        return None
    return best


def admission_report(
    shadow_rows: list[dict],
    filters: EntryFilters,
) -> dict[str, Any]:
    arms = arm_events(shadow_rows)
    n_blocked_stream = 0
    n_blocked_macd = 0
    n_blocked_rsi = 0
    n_pass = 0
    for r in arms:
        why = entry_filter_block(r, filters)
        if why is None:
            n_pass += 1
        elif why == "stream_required":
            n_blocked_stream += 1
        elif why.startswith("macd_"):
            n_blocked_macd += 1
        elif why.startswith("rsi_"):
            n_blocked_rsi += 1
        else:
            n_blocked_macd += 1  # defensive bucket
    return {
        "n_arms": len(arms),
        "n_blocked_by_stream": n_blocked_stream,
        "n_blocked_by_macd": n_blocked_macd,
        "n_blocked_by_rsi": n_blocked_rsi,
        "n_pass": n_pass,
    }


def filter_fills(
    fills: list[dict],
    arms_by_sym: dict[str, list[dict]],
    filters: EntryFilters,
    *,
    match_sec: float = DEFAULT_MATCH_SEC,
) -> tuple[list[dict], dict[str, int], list[dict]]:
    """Keep fills whose matched arm passes filters.

    Returns (kept_fills, block_counts, audit_rows).
    """
    kept: list[dict] = []
    counts: dict[str, int] = defaultdict(int)
    audit: list[dict] = []
    for fill in fills:
        arm = match_arm_for_fill(fill, arms_by_sym, match_sec=match_sec)
        if arm is None:
            counts["no_arm_match"] += 1
            audit.append({
                "symbol": fill["symbol"],
                "day": fill["day"],
                "entry_ts": fill["entry_ts"],
                "kept": False,
                "block": "no_arm_match",
            })
            continue
        why = entry_filter_block(arm, filters)
        row = {
            "symbol": fill["symbol"],
            "day": fill["day"],
            "entry_ts": fill["entry_ts"],
            "arm_ts": arm.get("ts"),
            "arm_lag_sec": round(float(fill["entry_ts"]) - float(arm["ts"]), 2),
            "last_ask_src": ask_src(arm),
            "macd_src": macd_src(arm),
            "cm_rsi_src": rsi_src(arm),
            "block": why,
            "kept": why is None,
        }
        audit.append(row)
        if why is None:
            kept.append(fill)
            counts["kept"] += 1
        elif why == "stream_required":
            counts["blocked_stream"] += 1
        elif why.startswith("macd_"):
            counts["blocked_macd"] += 1
        elif why.startswith("rsi_"):
            counts["blocked_rsi"] += 1
        else:
            counts[f"blocked_{why}"] += 1
    return kept, dict(counts), audit


# ── capture summary (hold MFE from the walk) ─────────────────────────────────

def capture_summary(rows: list[dict]) -> dict[str, Any]:
    """Median sim_r / mfe_r on walked rows with positive hold MFE."""
    ratios: list[float] = []
    mfes: list[float] = []
    sims: list[float] = []
    for r in rows:
        mfe = _num(r.get("mfe_r"))
        sim_r = _num(r.get("sim_r"))
        if mfe is None or sim_r is None or mfe <= 1e-9:
            continue
        ratios.append(sim_r / mfe)
        mfes.append(mfe)
        sims.append(sim_r)
    if not ratios:
        return {
            "n_capture": 0,
            "median_capture": None,
            "median_mfe_r": None,
            "median_sim_r": None,
        }
    return {
        "n_capture": len(ratios),
        "median_capture": round(statistics.median(ratios), 4),
        "median_mfe_r": round(statistics.median(mfes), 4),
        "median_sim_r": round(statistics.median(sims), 4),
    }


# ── run one / compare ────────────────────────────────────────────────────────

def run_variant(
    *,
    name: str,
    variant: Variant,
    days: list[str],
    cfg: dict,
    fills: list[dict],
    shadow_rows: list[dict],
    bar_cache: dict,
    shadow_by_sym: dict[str, list[dict]],
    risk_mode: str = "current",
    allow_shadow: bool = True,
    match_sec: float = DEFAULT_MATCH_SEC,
    min_n: int = sfr.MIN_N,
) -> dict[str, Any]:
    arms_by = shadow_arms_by_sym(shadow_rows)
    admit = admission_report(shadow_rows, variant.filters)
    kept, block_counts, audit = filter_fills(
        fills, arms_by, variant.filters, match_sec=match_sec,
    )
    cfg_used = (
        sfr.apply_plan_overlay(cfg, variant.overlay)
        if variant.overlay else dict(cfg)
    )
    scored = sfr.score_fills(
        kept, cfg_used,
        bar_cache=bar_cache,
        shadow_by_sym=shadow_by_sym,
        risk_mode=risk_mode,
        allow_shadow=allow_shadow,
        min_n=min_n,
    )
    cap = capture_summary(scored.get("rows") or [])
    live_kept = [
        f for f in kept if f.get("live_usd") is not None
    ]
    live_pnl = round(sum(float(f["live_usd"]) for f in live_kept), 2)
    overlay_pub = {k: v for k, v in variant.overlay.items() if k != "_extra"}
    overlay_pub.update(variant.overlay.get("_extra") or {})
    return {
        "name": name,
        "label": variant.short_label(),
        "filters": {
            "require_stream_price": variant.filters.require_stream_price,
            "require_realtime_macd": variant.filters.require_realtime_macd,
            "require_realtime_rsi": variant.filters.require_realtime_rsi,
            "macd_max_age_sec": variant.filters.macd_max_age_sec,
        },
        "overlay": overlay_pub,
        "days": days,
        "admission": admit,
        "n_fills_in": len(fills),
        "n_fills_kept": len(kept),
        "fill_blocks": block_counts,
        "n_arms": admit["n_arms"],
        "n_blocked_by_stream": admit["n_blocked_by_stream"],
        "n_blocked_by_macd": admit["n_blocked_by_macd"],
        "n_blocked_by_rsi": admit["n_blocked_by_rsi"],
        "n_scored": scored.get("n_scored"),
        "n_walked": scored.get("n_walked"),
        "live_pnl": live_pnl,
        "live_usd_scored": scored.get("live_usd"),
        "sim_pnl": scored.get("sim_usd"),
        "scored_sim_usd": scored.get("scored_sim_usd"),
        "delta_usd": scored.get("delta_usd"),
        "capture": cap,
        "skip": scored.get("skip"),
        "sessions": scored.get("sessions"),
        "rows": scored.get("rows"),
        "audit": audit,
        "risk_mode": risk_mode,
    }


def compare_payload(a: dict, b: dict) -> dict[str, Any]:
    def _d(key: str) -> Any:
        av, bv = a.get(key), b.get(key)
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            return round(float(bv) - float(av), 4)
        return None

    return {
        "a": a.get("label"),
        "b": b.get("label"),
        "delta_n_fills_kept": _d("n_fills_kept"),
        "delta_n_scored": _d("n_scored"),
        "delta_live_pnl": _d("live_pnl"),
        "delta_sim_pnl": _d("sim_pnl"),
        "delta_scored_sim_usd": _d("scored_sim_usd"),
    }


def print_variant(v: dict) -> None:
    admit = v.get("admission") or {}
    cap = v.get("capture") or {}
    print(f"--- {v.get('name')}  [{v.get('label')}] ---")
    print(
        f"  arms {admit.get('n_arms')}  "
        f"pass {admit.get('n_pass')}  "
        f"block stream×{admit.get('n_blocked_by_stream')}  "
        f"macd×{admit.get('n_blocked_by_macd')}  "
        f"rsi×{admit.get('n_blocked_by_rsi')}"
    )
    fb = v.get("fill_blocks") or {}
    print(
        f"  fills in {v.get('n_fills_in')}  kept {v.get('n_fills_kept')}  "
        f"blocks {fb}"
    )
    print(
        f"  walked {v.get('n_walked')}  scored {v.get('n_scored')}  "
        f"live_pnl ${v.get('live_pnl'):+.2f}  "
        f"sim_pnl ${float(v.get('sim_pnl') or 0):+.2f}  "
        f"scored_sim ${float(v.get('scored_sim_usd') or 0):+.2f}"
    )
    mc = cap.get("median_capture")
    mc_s = f"{mc:.3f}" if mc is not None else "n/a"
    print(
        f"  capture n={cap.get('n_capture')}  "
        f"median_capture={mc_s}  "
        f"median_mfe_r={cap.get('median_mfe_r')}  "
        f"median_sim_r={cap.get('median_sim_r')}"
    )
    print(f"  overlay {json.dumps(v.get('overlay') or {}, default=str)}")


def print_comparison(a: dict, b: dict, cmp: dict) -> None:
    print()
    print("=== A vs B ===")
    hdr = f"{'metric':<28}{'A':>14}{'B':>14}{'B−A':>12}"
    print(hdr)
    rows = [
        ("label", a.get("label"), b.get("label"), ""),
        ("n_arms", a.get("n_arms"), b.get("n_arms"), ""),
        ("n_blocked_by_stream", a.get("n_blocked_by_stream"),
         b.get("n_blocked_by_stream"), ""),
        ("n_blocked_by_macd", a.get("n_blocked_by_macd"),
         b.get("n_blocked_by_macd"), ""),
        ("n_fills_kept", a.get("n_fills_kept"), b.get("n_fills_kept"),
         cmp.get("delta_n_fills_kept")),
        ("n_scored", a.get("n_scored"), b.get("n_scored"),
         cmp.get("delta_n_scored")),
        ("live_pnl", a.get("live_pnl"), b.get("live_pnl"),
         cmp.get("delta_live_pnl")),
        ("sim_pnl", a.get("sim_pnl"), b.get("sim_pnl"),
         cmp.get("delta_sim_pnl")),
        ("scored_sim_usd", a.get("scored_sim_usd"), b.get("scored_sim_usd"),
         cmp.get("delta_scored_sim_usd")),
        ("median_capture",
         (a.get("capture") or {}).get("median_capture"),
         (b.get("capture") or {}).get("median_capture"),
         ""),
    ]
    for name, av, bv, dv in rows:
        def _fmt(x: Any) -> str:
            if x is None or x == "":
                return ""
            if isinstance(x, float):
                if "pnl" in name or "usd" in name or name == "live_pnl":
                    return f"{x:+.2f}"
                return f"{x:.4g}"
            return str(x)
        print(f"{name:<28}{_fmt(av):>14}{_fmt(bv):>14}{_fmt(dv):>12}")
    print()
    print("fills filtered by entry provenance then trail-walked; "
          "do not write config")


def write_artifacts(payload: dict) -> dict[str, str]:
    from ai_paths import resolve_report_dir
    rd = resolve_report_dir() / "tape_ab"
    rd.mkdir(parents=True, exist_ok=True)
    days = payload.get("days") or []
    label = sfr._label(days) if days else "run"
    json_path = rd / f"{label}.json"
    # Drop bulky audit/rows from default write? keep slim rows.
    slim = dict(payload)
    for key in ("a", "b"):
        if key in slim and isinstance(slim[key], dict):
            part = dict(slim[key])
            part.pop("audit", None)
            rows = part.get("rows") or []
            part["rows"] = [
                {k: r.get(k) for k in (
                    "symbol", "day", "live_usd", "sim_usd", "delta_usd",
                    "sim_r", "mfe_r", "sim_reason", "path",
                )}
                for r in rows
            ]
            slim[key] = part
    json_path.write_text(
        json.dumps(slim, indent=2, default=str) + "\n", encoding="utf-8")
    return {"json": str(json_path)}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _build_variants(args: argparse.Namespace) -> list[tuple[str, Variant]]:
    out: list[tuple[str, Variant]] = []
    if args.a or args.b:
        if not args.a or not args.b:
            raise ValueError("pass both --a and --b (or neither, use flags)")
        out.append(("A", parse_variant(args.a, name="A")))
        out.append(("B", parse_variant(args.b, name="B")))
        return out
    filters = EntryFilters(
        require_stream_price=bool(args.require_stream_price),
        require_realtime_macd=bool(args.require_realtime_macd),
        require_realtime_rsi=bool(args.require_realtime_rsi),
        macd_max_age_sec=float(args.macd_max_age_sec or 0),
    )
    if args.allow_rest_price:
        filters.require_stream_price = False
    overlay = sfr.overlay_from_items(args.overlay) if args.overlay else {}
    out.append(("run", Variant(name="run", filters=filters, overlay=overlay)))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("symbols", nargs="*", help="optional symbol filter")
    ap.add_argument("--day", action="append", default=[],
                    help="ET day YYYY-MM-DD (repeatable)")
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--from", dest="date_from", default="")
    ap.add_argument("--to", dest="date_to", default="")
    ap.add_argument("--tape", default="", help="packed tape directory")
    ap.add_argument("--outcomes", default="")
    ap.add_argument("--shadow", default="")
    ap.add_argument("--bars-cache", default="")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--require-bars", action="store_true")
    ap.add_argument("--feed", choices=("iex", "sip"), default="sip")
    ap.add_argument("--risk", choices=("current", "live"), default="current")
    ap.add_argument("--match-sec", type=float, default=DEFAULT_MATCH_SEC,
                    help="max seconds from arm_ok shadow to fill (default 180)")
    ap.add_argument("--a", default="", help="variant A spec, e.g. rest+give=0.2")
    ap.add_argument("--b", default="", help="variant B spec, e.g. stream+rt_macd+give=0.35")
    ap.add_argument("--require-stream-price", action="store_true",
                    help="single-run: require last_ask_src=stream")
    ap.add_argument("--allow-rest-price", action="store_true",
                    help="single-run: allow REST decision price")
    ap.add_argument("--require-realtime-macd", action="store_true")
    ap.add_argument("--require-realtime-rsi", action="store_true")
    ap.add_argument("--macd-max-age-sec", type=float, default=0.0)
    ap.add_argument("--overlay", action="append", default=[],
                    help="single-run overlay give_r=0.35 (repeatable)")
    ap.add_argument("--min-n", type=int, default=sfr.MIN_N)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--trades", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args(argv)

    try:
        days = sfr.resolve_days(
            day=args.day, date_from=args.date_from, date_to=args.date_to,
            days_n=args.days,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    try:
        variants = _build_variants(args)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    tape = Path(args.tape) if args.tape else None
    outcomes_path = sfr.resolve_outcomes_path(
        tape=tape,
        outcomes=Path(args.outcomes) if args.outcomes else None,
    )
    if not outcomes_path.exists() and tape is None:
        print(f"no outcomes at {outcomes_path}", file=sys.stderr)
        return 2

    shadow_rows: list[dict] = []
    rows: list[dict] = []
    if tape is not None:
        packed = desk_tape.load(tape)
        rows = packed.get("outcomes") or []
        shadow_rows = packed.get("shadow") or []
        if not rows and (Path(tape) / "outcomes.jsonl").exists():
            rows = sfr.load_jsonl(Path(tape) / "outcomes.jsonl")
        if not shadow_rows and (Path(tape) / "shadow.jsonl").exists():
            shadow_rows = sfr.load_jsonl(Path(tape) / "shadow.jsonl")
    else:
        rows = sfr.load_jsonl(outcomes_path)
    if args.shadow:
        shadow_rows = sfr.load_jsonl(Path(args.shadow))
    elif not shadow_rows and tape is not None:
        sp = Path(tape) / "shadow.jsonl"
        if sp.exists():
            shadow_rows = sfr.load_jsonl(sp)

    if not shadow_rows:
        print("no shadow rows — need --tape or --shadow for entry filters",
              file=sys.stderr)
        return 2

    want_sym = {s.upper() for s in args.symbols} if args.symbols else None
    fills, skip_load = sfr.load_fills(rows, days=days, symbols=want_sym)
    skip_load.pop("other_day", None)
    skip_load.pop("other_symbol", None)

    cfg = opt.live_cfg()
    bar_cache: dict = {}
    if args.bars_cache:
        bar_cache.update(sfr.load_bars_cache(Path(args.bars_cache)))

    allow_shadow = not args.require_bars
    if not args.no_fetch:
        bar_cache, _err = sfr.fetch_needed(
            fills, bar_cache, cfg, feed=args.feed)

    shadow_idx = sfr.shadow_index(shadow_rows)
    results: dict[str, dict] = {}
    for name, variant in variants:
        results[name] = run_variant(
            name=name,
            variant=variant,
            days=days,
            cfg=cfg,
            fills=fills,
            shadow_rows=shadow_rows,
            bar_cache=bar_cache,
            shadow_by_sym=shadow_idx,
            risk_mode=args.risk,
            allow_shadow=allow_shadow,
            match_sec=args.match_sec,
            min_n=args.min_n,
        )

    payload: dict[str, Any] = {
        "ok": True,
        "days": days,
        "tape": str(tape) if tape else "",
        "n_fills": len(fills),
        "skip_load": skip_load,
        "match_sec": args.match_sec,
        "desk_product": cfg.get("desk_product"),
    }
    if "A" in results and "B" in results:
        payload["a"] = results["A"]
        payload["b"] = results["B"]
        payload["compare"] = compare_payload(results["A"], results["B"])
    else:
        payload["run"] = results[variants[0][0]]

    if not args.no_write:
        payload["paths"] = write_artifacts(payload)

    if args.json:
        slim = dict(payload)
        for key in ("a", "b", "run"):
            if key in slim and isinstance(slim[key], dict):
                part = dict(slim[key])
                if not args.trades:
                    part.pop("audit", None)
                    part["rows"] = [
                        {k: r.get(k) for k in (
                            "symbol", "day", "live_usd", "sim_usd",
                            "sim_r", "mfe_r", "sim_reason", "path",
                        )}
                        for r in (part.get("rows") or [])
                    ]
                slim[key] = part
        print(json.dumps(slim, indent=2, default=str))
        return 0

    print()
    print(f"=== tape A/B  {sfr._label(days)}  fills={len(fills)} ===")
    for name, _v in variants:
        print_variant(results[name])
        if args.trades:
            for r in results[name].get("rows") or []:
                print(
                    f"    {r.get('symbol'):<6} {r.get('day')}  "
                    f"live {r.get('live_usd')}  sim {r.get('sim_usd')}  "
                    f"{r.get('sim_reason')}  mfe_r={r.get('mfe_r')}"
                )
    if "A" in results and "B" in results:
        print_comparison(results["A"], results["B"], payload["compare"])
    if payload.get("paths"):
        print("wrote", payload["paths"].get("json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
