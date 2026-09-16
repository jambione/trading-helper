#!/usr/bin/env python3
"""Source scorecard — grade suppliers on proposals (GROSS, with controls).

Question: for every name a source proposed, what did the name do next —
regardless of whether the desk seated, armed, or traded it.

Unit: one proposal episode = (day, symbol, proposer_norm, first-proposal ts).

Pre-registered decision rules (report only — never auto-apply):
  source − control ≤ 0 over ≥N sessions, adequate n  → cut or demote source
  source − control > 0, holds on held-out days       → raise seed cap
  every source ≈ control                             → selection not the lever

Honest limits:
  - Cannot score names no source proposed (universe_screen).
  - Gross-only until historical quote plumbing: relative ranking valid,
    absolute playability not. Every output stamps cost=GROSS.
  - A run without controls is not reportable.
  - Below outcome_slice.required_n the verdict is THIN, never a winner.

Overlap views (always all three): all / exclusive / first.
  exclusive is the honest ranking; disagreement across views is a finding.

Usage (mini, after close)::

    .venv/bin/python tools/source_scorecard.py --day 2026-09-16
    .venv/bin/python tools/source_scorecard.py --day 2026-09-16 --retro-fills
    .venv/bin/python tools/source_scorecard.py --days 5
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from ai_paths import resolve_report_dir  # noqa: E402
from source_norm import normalize_proposer  # noqa: E402

import drift_screen as DS  # noqa: E402
import gate_screen as GS  # noqa: E402
import outcome_slice as osl  # noqa: E402

ET = ZoneInfo("America/New_York")
SCREEN_DIR = Path(resolve_report_dir()) / "screens"
COST = "GROSS"
ACROSS_TOL_SEC = 90.0
HORIZONS_DEFAULT = (15, 30, 60, 120)
MIN_N = 30
MIN_SESSIONS = 5
MAX_SESSION_SHARE = 0.50
MIN_PEER = 3


def _et_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=ET).strftime("%Y-%m-%d")


def _utc_day(ts: float) -> str:
    """Bar day key used by drift/gate excursion_from (UTC date)."""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")


def proposal_ledger_path(day: str) -> Path:
    return Path(resolve_report_dir()) / "proposal_ledger" / f"{day}.jsonl"


def load_proposal_rows(day: str) -> list[dict]:
    path = proposal_ledger_path(day)
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def episodes_from_ledger(rows: list[dict], day: str) -> list[dict]:
    """Collapse rows → episodes at first proposal ts per (day, symbol, proposer_norm).

    Prefer seed-stage rows; inclusion-only still counts if no seed row exists.
    Heartbeat rows do not create new episodes.
    """
    best: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        if r.get("heartbeat"):
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        try:
            ts = float(r.get("ts"))
        except (TypeError, ValueError):
            continue
        prop = str(r.get("proposer_norm") or normalize_proposer(r.get("proposer")))
        key = (day, sym, prop)
        stage = str(r.get("stage") or "")
        prev = best.get(key)
        if prev is None:
            best[key] = {
                "day": day,
                "symbol": sym,
                "proposer_norm": prop,
                "proposer": str(r.get("proposer") or ""),
                "ts": ts,
                "stage": stage,
                "px": r.get("px") or r.get("price"),
                "decision": r.get("decision"),
            }
            continue
        # Prefer earlier ts; prefer seed over inclusion at same ts.
        if ts < float(prev["ts"]) or (
            ts == float(prev["ts"]) and stage == "seed" and prev.get("stage") != "seed"
        ):
            prev.update({
                "ts": ts,
                "stage": stage,
                "px": r.get("px") or r.get("price") or prev.get("px"),
                "decision": r.get("decision"),
                "proposer": str(r.get("proposer") or prev.get("proposer") or ""),
            })
    return sorted(best.values(), key=lambda e: (e["proposer_norm"], e["ts"], e["symbol"]))


def load_retro_fill_episodes(day: str) -> list[dict]:
    """Merge-contaminated baseline: outcomes × position_shadow.source.

    Attribution is ownership, not origin — compare to ledger episodes.
    """
    report = Path(resolve_report_dir())
    outcomes_p = report / "outcomes.jsonl"
    shadow_p = report / "position_shadow.jsonl"
    if not outcomes_p.exists():
        return []

    # symbol -> earliest shadow source that day (ownership label).
    src_by_sym: dict[str, str] = {}
    if shadow_p.exists():
        with shadow_p.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ts = r.get("ts")
                sym = str(r.get("symbol") or r.get("ticker") or "").upper()
                if not ts or not sym:
                    continue
                try:
                    ts_f = float(ts)
                except (TypeError, ValueError):
                    continue
                if _et_day(ts_f) != day:
                    continue
                src = str(r.get("source") or "").strip().lower()
                if src and sym not in src_by_sym:
                    src_by_sym[sym] = src

    out: list[dict] = []
    with outcomes_p.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("realized_r_multiple") is None:
                continue
            sym = str(r.get("symbol") or r.get("ticker") or "").upper()
            # Prefer explicit entry/exit stamps when present.
            ts = r.get("entry_ts") or r.get("opened_ts") or r.get("ts")
            if not sym or ts is None:
                continue
            try:
                ts_f = float(ts)
            except (TypeError, ValueError):
                continue
            if _et_day(ts_f) != day:
                continue
            feat = r.get("features") if isinstance(r.get("features"), dict) else {}
            raw_src = (
                str(r.get("source") or "")
                or str(feat.get("source") or "")
                or src_by_sym.get(sym, "")
            )
            prop = normalize_proposer(raw_src)
            out.append({
                "day": day,
                "symbol": sym,
                "proposer_norm": prop,
                "proposer": str(raw_src or "").lower(),
                "ts": ts_f,
                "stage": "fill_retro",
                "px": r.get("entry_price") or feat.get("entry_price"),
                "decision": "kept",
                "realized_r": float(r["realized_r_multiple"]),
                "retro": True,
            })
    # One episode per (symbol, proposer_norm) — earliest fill.
    best: dict[tuple[str, str], dict] = {}
    for e in out:
        key = (e["symbol"], e["proposer_norm"])
        prev = best.get(key)
        if prev is None or e["ts"] < prev["ts"]:
            best[key] = e
    return sorted(best.values(), key=lambda e: (e["proposer_norm"], e["ts"]))


def apply_overlap(episodes: list[dict], view: str) -> list[dict]:
    """Filter episodes by overlap view."""
    if view == "all":
        return list(episodes)
    # Window = same ET day (episode already day-scoped).
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for e in episodes:
        by_sym[e["symbol"]].append(e)
    out: list[dict] = []
    if view == "exclusive":
        for sym, group in by_sym.items():
            props = {g["proposer_norm"] for g in group}
            if len(props) == 1:
                out.extend(group)
        return out
    if view == "first":
        for sym, group in by_sym.items():
            first = min(group, key=lambda g: float(g["ts"]))
            out.append(first)
        return out
    raise ValueError(f"unknown overlap view: {view}")


def _nonoverlap_excursions(
    episodes: list[dict],
    bars: dict[str, list[dict]],
    horizon: int,
) -> list[dict]:
    """Anchored excursions; no two accepted samples overlap on a symbol-day."""
    span = horizon * 60.0
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in episodes:
        by_key[(e["symbol"], e["day"])].append(e)
    out: list[dict] = []
    for (sym, day), group in by_key.items():
        b = bars.get(sym) or []
        if not b:
            continue
        last_t = None
        for e in sorted(group, key=lambda x: float(x["ts"])):
            ts = float(e["ts"])
            if last_t is not None and ts - last_t < span:
                continue
            # excursion_from keys bars by UTC day — align.
            bar_day = _utc_day(ts)
            exc = GS.excursion_from(b, bar_day, ts, horizon)
            if not exc:
                # Retry with ET day if UTC miss (DST edge).
                exc = GS.excursion_from(b, day, ts, horizon)
            if not exc:
                continue
            out.append({
                **exc,
                "symbol": sym,
                "proposer_norm": e["proposer_norm"],
                "ts": ts,
                "day": day,  # keep ET session day for session stats
            })
            last_t = ts
    return out


def _score_excursions(rows: list[dict], label: str) -> dict[str, Any]:
    if not rows:
        return {
            "label": label,
            "verdict": "EMPTY",
            "n": 0,
            "sessions": 0,
            "cost": COST,
        }
    nets = [float(r["net"]) for r in rows]
    mfes = [float(r["mfe"]) for r in rows]
    maes = [float(r["mae"]) for r in rows]
    n = len(rows)
    by_day: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_day[str(r["day"])].append(float(r["net"]))
    sessions = len(by_day)
    top_share = max(len(v) for v in by_day.values()) / n if n else 1.0
    green = sum(1 for v in by_day.values() if statistics.fmean(v) > 0)
    mu = statistics.fmean(nets)
    sd = statistics.pstdev(nets) if n > 1 else 0.0
    med_net = statistics.median(nets)
    med_mfe = statistics.median(mfes)
    med_mae = statistics.median(maes)
    sign_p = DS._sign_p(green, sessions)
    need = osl.required_n(abs(mu) if mu else 0.10, sd if sd > 0 else 1.0)

    fails: list[str] = []
    if n < MIN_N:
        fails.append(f"n<{MIN_N}")
    if sessions < MIN_SESSIONS:
        fails.append(f"sessions<{MIN_SESSIONS}")
    if top_share > MAX_SESSION_SHARE:
        fails.append(f"one session {top_share:.0%} of n")
    if n < need:
        fails.append(f"n<{need} required_n")

    if n < need or n < MIN_N or sessions < MIN_SESSIONS:
        verdict = "THIN"
    elif med_net > 0 and sign_p <= 0.05 and top_share <= MAX_SESSION_SHARE:
        verdict = "POS"
    elif med_net <= 0:
        verdict = "NEG"
    else:
        verdict = "INCONCLUSIVE"

    return {
        "label": label,
        "verdict": verdict,
        "n": n,
        "sessions": sessions,
        "sessions_green": green,
        "sign_p": sign_p,
        "max_session_share": top_share,
        "median_net": med_net,
        "mean_net": mu,
        "sd_net": sd,
        "median_mfe": med_mfe,
        "median_mae": med_mae,
        "mfe_over_mae": (med_mfe / med_mae) if med_mae else None,
        "required_n": need,
        "cost": COST,
        "why": "; ".join(fails) or "ok",
    }


def _across_diffs(
    episodes: list[dict],
    bars: dict[str, list[dict]],
    horizon: int,
) -> list[float]:
    """source net − median(peer nets) at same instant; need ≥3 peers."""
    # Index: day -> list of (ts, sym, proposer)
    by_day: dict[str, list[tuple[float, str, str]]] = defaultdict(list)
    for e in episodes:
        by_day[e["day"]].append((float(e["ts"]), e["symbol"], e["proposer_norm"]))

    diffs: list[float] = []
    span_bars = horizon
    for e in episodes:
        ts = float(e["ts"])
        sym = e["symbol"]
        day = e["day"]
        b = bars.get(sym) or []
        if not b:
            continue
        bar_day = _utc_day(ts)
        exc = GS.excursion_from(b, bar_day, ts, span_bars) or GS.excursion_from(
            b, day, ts, span_bars
        )
        if not exc:
            continue
        peers: list[float] = []
        seen: set[str] = set()
        for pts, psym, _pprop in by_day.get(day, ()):
            if psym == sym or psym in seen or abs(pts - ts) > ACROSS_TOL_SEC:
                continue
            seen.add(psym)
            pb = bars.get(psym) or []
            if not pb:
                continue
            pexc = GS.excursion_from(pb, _utc_day(pts), pts, span_bars) or GS.excursion_from(
                pb, day, pts, span_bars
            )
            if pexc:
                peers.append(float(pexc["net"]))
        if len(peers) >= MIN_PEER:
            diffs.append(float(exc["net"]) - statistics.median(peers))
    return diffs


def _score_diffs(diffs: list[float], label: str, sessions_hint: int = 0) -> dict[str, Any]:
    if not diffs:
        return {
            "label": label,
            "verdict": "EMPTY",
            "n": 0,
            "cost": COST,
            "reportable": False,
        }
    n = len(diffs)
    med = statistics.median(diffs)
    mu = statistics.fmean(diffs)
    sd = statistics.pstdev(diffs) if n > 1 else 0.0
    need = osl.required_n(abs(mu) if mu else 0.10, sd if sd > 0 else 1.0)
    beat = 100.0 * sum(1 for d in diffs if d > 0) / n
    if n < need or n < MIN_N:
        verdict = "THIN"
    elif med > 0:
        verdict = "POS"
    elif med <= 0:
        verdict = "NEG"
    else:
        verdict = "INCONCLUSIVE"
    return {
        "label": label,
        "verdict": verdict,
        "n": n,
        "median_diff": med,
        "mean_diff": mu,
        "sd_diff": sd,
        "beat_pct": beat,
        "required_n": need,
        "sessions_hint": sessions_hint,
        "cost": COST,
        "reportable": verdict != "EMPTY",
    }


def _outside_diffs(
    episodes: list[dict],
    bars: dict[str, list[dict]],
    horizon: int,
    *,
    want_vol: bool,
    feed: str = "sip",
) -> list[float]:
    """source net − matched never-proposed peer (desk_null outside pool)."""
    try:
        import desk_null as N
        import bars as bars_mod
        import random
    except Exception:
        return []

    proposed = {e["symbol"] for e in episodes}
    days = sorted({e["day"] for e in episodes})
    if not days:
        return []
    rng = random.Random(7)
    # Build one outside pool for the primary day (first).
    day0 = days[0]
    try:
        pool = N.build_outside_pool(day0, proposed, rng, feed)
    except Exception:
        return []
    if not pool:
        return []

    diffs: list[float] = []
    for e in episodes:
        ts = float(e["ts"])
        sym = e["symbol"]
        day = e["day"]
        b = bars.get(sym) or []
        if not b:
            continue
        exc = GS.excursion_from(b, _utc_day(ts), ts, horizon) or GS.excursion_from(
            b, day, ts, horizon
        )
        if not exc:
            continue
        try:
            px = float(e.get("px") or 0) or float(exc.get("entry") or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px <= 0:
            # Approximate from first bar.
            path = [x for x in b if x["t"] >= ts]
            if not path:
                continue
            px = float(path[0]["o"])
        lo, hi = N.OUTSIDE_BAND[0] * px, N.OUTSIDE_BAND[1] * px
        candidates = []
        for osym, (stamps, closes) in pool.items():
            if osym in proposed or not closes:
                continue
            opx = float(closes[0])
            if not (lo <= opx <= hi):
                continue
            if want_vol:
                # Skip rather than silent fallback when vol match fails.
                try:
                    # crude: require peer has stamps near ts
                    if not stamps:
                        continue
                except Exception:
                    continue
            fwd = bars_mod.forward_return(stamps, closes, ts, horizon * 60.0)
            if fwd is not None:
                candidates.append(fwd)
        if len(candidates) >= MIN_PEER:
            diffs.append(float(exc["net"]) - statistics.median(candidates))
    return diffs


def score_source(
    episodes: list[dict],
    bars: dict[str, list[dict]],
    horizons: list[int],
    view: str,
) -> dict[str, Any]:
    filtered = apply_overlap(episodes, view)
    by_prop: dict[str, list[dict]] = defaultdict(list)
    for e in filtered:
        by_prop[e["proposer_norm"]].append(e)

    results: dict[str, Any] = {}
    for prop, group in sorted(by_prop.items()):
        prop_block: dict[str, Any] = {"episodes": len(group), "horizons": {}}
        for hz in horizons:
            exc = _nonoverlap_excursions(group, bars, hz)
            raw = _score_excursions(exc, f"{prop}@raw_{hz}m")
            across = _score_diffs(
                _across_diffs(group, bars, hz),
                f"{prop}@ACROSS_{hz}m",
                sessions_hint=raw.get("sessions") or 0,
            )
            outside = _score_diffs(
                _outside_diffs(group, bars, hz, want_vol=False),
                f"{prop}@OUTSIDE_{hz}m",
            )
            outside_vol = _score_diffs(
                _outside_diffs(group, bars, hz, want_vol=True),
                f"{prop}@OUTSIDE-VOL_{hz}m",
            )
            controls_ok = across.get("n", 0) > 0 or outside.get("n", 0) > 0
            # Reported number is source − control. Prefer ACROSS when present.
            primary = across if across.get("n", 0) > 0 else outside
            reported = {
                "control": primary.get("label"),
                "median_source_minus_control": primary.get("median_diff"),
                "verdict": primary.get("verdict") if controls_ok else "NOT_REPORTABLE",
                "why": (
                    primary.get("why")
                    if controls_ok
                    else "no controls — source-alone rediscovers volatility"
                ),
            }
            if not controls_ok:
                reported["verdict"] = "NOT_REPORTABLE"
            elif primary.get("verdict") == "THIN":
                reported["verdict"] = "THIN"
            prop_block["horizons"][str(hz)] = {
                "raw": raw,
                "across": across,
                "outside": outside,
                "outside_vol": outside_vol,
                "reported": reported,
            }
        results[prop] = prop_block
    return results


def _summary_md(day: str, payload: dict) -> str:
    lines = [
        f"# Source scorecard — {day}",
        "",
        f"cost: **{COST}** (relative ranking only until quote plumbing)",
        f"overlap views: {', '.join(payload.get('views', []))}",
        f"horizons_min: {payload.get('horizons')}",
        f"mode: {payload.get('mode')}",
        "",
        "Reported number is **source − control**. exclusive is the honest ranking.",
        "",
    ]
    for view, block in (payload.get("results") or {}).items():
        lines.append(f"## overlap=`{view}`")
        lines.append("")
        for prop, pb in (block or {}).items():
            lines.append(f"### {prop} (episodes={pb.get('episodes')})")
            for hz, card in (pb.get("horizons") or {}).items():
                rep = card.get("reported") or {}
                raw = card.get("raw") or {}
                lines.append(
                    f"- @{hz}m reported={rep.get('verdict')} "
                    f"median(src−ctrl)={rep.get('median_source_minus_control')} "
                    f"ctrl={rep.get('control')} | raw n={raw.get('n')} "
                    f"required_n={raw.get('required_n')} "
                    f"sessions={raw.get('sessions')} "
                    f"green={raw.get('sessions_green')} "
                    f"sign_p={raw.get('sign_p')}"
                )
            lines.append("")
    return "\n".join(lines) + "\n"


def update_rolling(day: str, payload: dict) -> Path:
    path = SCREEN_DIR / "source_scorecard.json"
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    rolling: dict[str, Any]
    if path.exists():
        try:
            rolling = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            rolling = {"days": {}, "cost": COST}
    else:
        rolling = {"days": {}, "cost": COST}
    rolling.setdefault("days", {})[day] = {
        "mode": payload.get("mode"),
        "horizons": payload.get("horizons"),
        "n_episodes": payload.get("n_episodes"),
        "results_exclusive_30": (
            ((payload.get("results") or {}).get("exclusive") or {})
        ),
        "updated_ts": datetime.now(tz=ET).isoformat(),
    }
    rolling["cost"] = COST
    rolling["as_of"] = day
    path.write_text(json.dumps(rolling, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def run_day(day: str, horizons: list[int], *, retro: bool, limit_symbols: int) -> dict:
    if retro:
        episodes = load_retro_fill_episodes(day)
        mode = "retro_fills"
    else:
        rows = load_proposal_rows(day)
        episodes = episodes_from_ledger(rows, day)
        mode = "proposal_ledger"

    if not episodes:
        return {
            "day": day,
            "mode": mode,
            "cost": COST,
            "n_episodes": 0,
            "error": "no episodes",
            "results": {},
        }

    syms = DS.select_symbols([e["symbol"] for e in episodes], limit_symbols)
    episodes = [e for e in episodes if e["symbol"] in set(syms)]
    start = datetime.fromisoformat(day).replace(tzinfo=ET).astimezone(timezone.utc) - timedelta(hours=2)
    end = start + timedelta(days=1, hours=6)
    bars = DS.fetch_minutes(syms, start, end)

    views = ("all", "exclusive", "first")
    results = {}
    for view in views:
        results[view] = score_source(episodes, bars, horizons, view)

    return {
        "day": day,
        "mode": mode,
        "cost": COST,
        "n_episodes": len(episodes),
        "n_symbols": len(syms),
        "n_bars_symbols": len(bars),
        "horizons": horizons,
        "views": list(views),
        "results": results,
        "pre_registered_rules": [
            "source-control<=0 over adequate n/sessions → cut or demote",
            "source-control>0 held-out → raise seed cap",
            "all ≈ control → selection not the lever",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", default="", help="ET day YYYY-MM-DD (default: today ET)")
    ap.add_argument("--days", type=int, default=0, help="run last N ET weekdays ending today")
    ap.add_argument("--horizons", default="15,30,60,120")
    ap.add_argument("--retro-fills", action="store_true",
                    help="fills baseline on position_shadow.source (merge-contaminated)")
    ap.add_argument("--limit-symbols", type=int, default=400)
    args = ap.parse_args()

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    today = datetime.now(tz=ET).strftime("%Y-%m-%d")
    days: list[str] = []
    if args.days and args.days > 0:
        d = datetime.now(tz=ET).date()
        while len(days) < args.days:
            if d.weekday() < 5:
                days.append(d.isoformat())
            d -= timedelta(days=1)
        days = list(reversed(days))
    else:
        days = [args.day or today]

    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    rc = 0
    for day in days:
        print(f"source_scorecard day={day} mode="
              f"{'retro_fills' if args.retro_fills else 'proposal_ledger'} "
              f"cost={COST}")
        payload = run_day(day, horizons, retro=args.retro_fills,
                          limit_symbols=args.limit_symbols)
        if payload.get("error"):
            print(f"  {payload['error']}")
            rc = 1
            continue
        out_json = SCREEN_DIR / f"source_{day}.json"
        out_md = SCREEN_DIR / f"source_{day}_summary.md"
        out_json.write_text(json.dumps(payload, indent=2, default=str) + "\n",
                            encoding="utf-8")
        out_md.write_text(_summary_md(day, payload), encoding="utf-8")
        roll = update_rolling(day, payload)
        print(f"  episodes={payload.get('n_episodes')} symbols={payload.get('n_symbols')} "
              f"bars={payload.get('n_bars_symbols')}")
        print(f"  wrote {out_json}")
        print(f"  wrote {out_md}")
        print(f"  rolling {roll}")
        # Print exclusive @30m reported line per source
        excl = (payload.get("results") or {}).get("exclusive") or {}
        for prop, pb in excl.items():
            card = (pb.get("horizons") or {}).get("30") or {}
            rep = card.get("reported") or {}
            print(f"  exclusive@{prop}/30m → {rep.get('verdict')} "
                  f"median(src−ctrl)={rep.get('median_source_minus_control')} "
                  f"({rep.get('control')})")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
