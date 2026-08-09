#!/usr/bin/env python3
"""
desk_replay_bench.py — replay the desk's entry/exit rules over the prices it
actually observed, and A/B the runner-exit model on the same paths.

WHERE THE PRICES COME FROM
    events.jsonl `watch_skip` rows carry {symbol, ts, reason, ask} — a real
    quote the desk really saw. It is used because it is what the desk actually
    observed at decision time: rs_cache.sqlite is daily bars with no `open`,
    which cannot sequence an intraday scalp, and refetching 1-minute bars now
    would replay a series the desk never saw. (Alpaca minute-bar fetches do
    work from this box as of 2026-08-08 — the older note here claiming a 401
    on data was stale. Trading stays gated on ai_trading_host.)

WHAT THE `reason` FIELD PROVES
    should_arm_buy checks indicators BEFORE zone membership, and only
    (wait_setup, hard_no, spread, above_zone, below_zone, reward_risk,
    no_structure) are logged as watch_skip. So a tick recorded `above_zone` or
    `below_zone` is PROOF the indicator gate passed at that moment — price was
    the only thing blocking. Those ticks are replayed as indicator-clear; no
    others are.

HONESTY LIMITS — read these before quoting any number
  • The series is not a tape. It samples only ticks where the name was on the
    book AND blocked, so it systematically UNDER-samples the moments price was
    inside the zone — the exact moments an entry happens.
  • A handful of sessions (2026-08-03..05), all of which predate the
    double-bottom zone work (landed 2026-08-08). Every zone the desk drew on
    those days was the offset fallback, so the double-bottom path here is a
    RECONSTRUCTION either way. --bars feeds the detector the same 1-minute bar
    lows the live desk reads, which is the only mode whose swing / min_sep /
    lookback numbers mean what config means by them.
  • Entry price = the observed ask. Exit price = the observed ask too, which
    flatters every exit — a real sell hits the bid.
  • Structure window is the first --split fraction of each series; the forward
    walk uses only later ticks, so no look-ahead.

THE --sweep MODE, AND THE ONE THING IT IS NOT
    `--sweep` grids the double-bottom detector's two loosest knobs —
    ai_watch_db_match_pct (how close two lows must sit to count as one shelf)
    and the lookback — and reports, per cell, how many symbol-days find a zone,
    how many enter, and the mean R of what enters. It exists to price the
    tradeoff behind ai_watch_require_db_zone: loosening the detector finds more
    structure, but the marginal shelf it finds may be worse than no trade.

    UNITS — the reason most of this grid does not paste into config. `lookback`,
    `swing` and `min_sep` are all BAR counts live (ai_watch_db_lookback_bars /
    _swing_bars / _min_sep_bars slice 1-minute bar LOWS inside
    _fetch_symbol_lows). This bench has no bars: it walks OBSERVED TICKS, which
    arrive at the watch poll rate (ai_watch_poll_sec, 20s), not at 1/minute. So
    a cell labelled look=90 is NOT "90 minutes" and swing=1 is NOT "1 minute
    either side". Read those three columns as an ORDERING — looser vs tighter
    pivot detection, shorter vs longer memory — and confirm any winner against
    bars before touching config. Only match_pct is unit-free (a percentage
    gap between two lows) and transfers directly.

    WHAT THIS GRID ESTABLISHED (2026-08-09, 10 symbol-days, 2026-08-03..05)
      • RUN IT WITH --bars. Tick mode and bar mode disagree about the answer,
        and bar mode is the one in live units. On ticks, swing=1 looked best
        (10/10 zones, +1.096R mean); on real 1-minute bars the SAME comparison
        reverses — swing=1 is the worst cell (6/10 entries, +0.170R) and the
        live swing=2 is the best (7/10, +0.249R). The tick result was an
        artifact of quote spacing, not a finding about the detector.
      • Tick mode only: match_pct and lookback do not bind at all — every cell
        is byte-identical. find_double_bottom_support walks pivots NEWEST-FIRST
        and returns the first match, so it picks the two most recent qualifying
        lows, and recent quotes already sit 0.03-0.23% apart. On real bars both
        knobs do bind: match_pct 0.4 -> 0.8 takes zones from 7/10 to 9/10.
      • Extra zones are not extra trades. That same 7 -> 9 zone gain converts to
        0 extra entries and slightly LOWERS mean R (+0.249 -> +0.242): the
        marginal shelf is one price never reaches.
      • The dominant blocker is the zone BUILDER, not the detector.
        build_double_bottom_zone_structure refuses to publish when price has
        broken back under the shelf (ai_watch_db_require_price_above). That is
        why `no_db_zone` was split into `no_shelf` vs `price_below_shelf` —
        opposite remedies, and the old label hid which one you had.
      • On real bars no trade in the sample reached the runner phase, so the
        R-trail and the %-trail score IDENTICALLY (vs old = +0.000). The
        +0.053R edge the tick replay showed for the R-trail does not survive
        real bars. It is untested here, not disproven — but do not cite the
        tick number as support for it.

    Every honesty limit above still applies, and small cells are labelled
    underpowered rather than quietly averaged. Findings here are hypotheses,
    not verdicts.

USAGE
    venv/bin/python tools/desk_replay_bench.py
    venv/bin/python tools/desk_replay_bench.py --n 10 --json
    venv/bin/python tools/desk_replay_bench.py --sweep
    venv/bin/python tools/desk_replay_bench.py --bars --sweep
    venv/bin/python tools/desk_replay_bench.py --bars --sweep \
        --sweep-match 0.4,0.8 --sweep-lookback 60,90,120 --sweep-swing 1,2,3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = timezone.utc

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ai_entry_watch as ew
from ai_paths import report_file
from config import load_config

ET = ZoneInfo("America/New_York")
# Ticks proving the indicator gate passed (they are evaluated after it).
PRICE_ONLY_BLOCKS = {"above_zone", "below_zone"}
# Test fixtures have leaked into the live events file before (see ai_paths.py);
# these are the seeded values from tests/test_ai_positions.py.
FIXTURE = {("NVDA", 40.5), ("NVDA", 46.0), ("NVDA", 38.0)}


def load_series(path: Path) -> dict[tuple[str, str], list[tuple]]:
    out: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("kind") != "watch_skip":
            continue
        sym, ts, ask = d.get("symbol"), d.get("ts"), d.get("ask")
        if not (sym and ts and ask):
            continue
        if (sym, float(ask)) in FIXTURE:
            continue
        day = datetime.fromtimestamp(ts, ET).date().isoformat()
        out[(sym, day)].append((float(ts), float(ask), str(d.get("reason") or "")))
    for series in out.values():
        series.sort()
    return out


def et_minutes(ts: float) -> float:
    dt = datetime.fromtimestamp(ts, ET)
    return dt.hour * 60 + dt.minute


def walk_exits(entry, stop, target, path, cfg, *, eod_min, dead_min, dead_mfe):
    """Return {model: (exit_px, reason, blended_R)} for OLD and NEW runners.

    Both models share tranche A (target or stop) so only the runner differs.
    scale_out is read from config; qty cancels out of a blended R.
    """
    R = entry - stop
    scale = float(cfg.get("ai_watch_synth_scale_out_pct", 50.0)) / 100.0
    trail_pct = float(cfg.get("ai_watch_synth_trail_pct", 2.5)) / 100.0
    trail_r = float(cfg.get("ai_runner_trail_r", 1.0))

    a_done = False
    a_r = None
    peak = entry
    old_stop = new_stop = stop
    out: dict[str, tuple] = {}
    mfe_r = 0.0

    for ts, px, _reason in path:
        peak = max(peak, px)
        mfe_r = max(mfe_r, (peak - entry) / R)

        if not a_done:
            if px <= stop:                       # both tranches die together
                return {m: (stop, "stopped_out", -1.0) for m in ("old", "new")}
            if px >= target:
                a_done = True
                a_r = (target - entry) / R
                old_stop = peak * (1.0 - trail_pct)      # fixed-% trail
                new_stop = max(entry, peak - trail_r * R)  # R, breakeven floor
                continue
            if (
                dead_min > 0
                and (ts - path[0][0]) / 60.0 >= dead_min
                and mfe_r < dead_mfe
                and px <= entry * 1.001
            ):
                r = (px - entry) / R
                return {m: (px, "dead_trade", r) for m in ("old", "new")}
            if et_minutes(ts) >= eod_min:
                r = (px - entry) / R
                return {m: (px, "eod_flatten", r) for m in ("old", "new")}
            continue

        # Runner phase — ratchet each model, then test it.
        old_stop = max(old_stop, peak * (1.0 - trail_pct))
        new_stop = max(new_stop, entry, peak - trail_r * R)
        for name, lvl in (("old", old_stop), ("new", new_stop)):
            if name in out:
                continue
            if px <= lvl:
                r_run = (lvl - entry) / R
                out[name] = (lvl, "trailed_out",
                             scale * a_r + (1 - scale) * r_run)
        if len(out) == 2:
            return out
        if et_minutes(ts) >= eod_min:
            for name in ("old", "new"):
                out.setdefault(name, (px, "eod_flatten",
                                      scale * a_r + (1 - scale) * (px - entry) / R))
            return out

    last = path[-1][1] if path else entry
    for name in ("old", "new"):
        if name not in out:
            r_run = (last - entry) / R
            out[name] = (last, "path_end",
                         (scale * a_r + (1 - scale) * r_run) if a_done
                         else (last - entry) / R)
    return out


def replay_one(sym: str, day: str, obs: list[tuple], cfg: dict, *, split: float,
               match_pct: float, lookback: int, swing: int, min_sep: int,
               eod_min: int, dead_min: float, dead_mfe: float,
               bars: list[tuple] | None = None) -> dict:
    """Replay one symbol-day at one detector setting. Returns the result row.

    Two structure sources, selected by `bars`:
      • bars=None  — detector runs on OBSERVED TICKS, capped to the last
        `lookback` ticks. Ticks are not bars, so swing/min_sep/lookback are an
        ordering here, not config values (see module docstring).
      • bars=[(ts, low), ...] — detector runs on real 1-minute bar LOWS taken
        strictly before the cut, capped to the last `lookback` BARS. This is
        what _fetch_symbol_lows feeds the live detector, so swing, min_sep and
        lookback are in their true units and transfer to config directly.

    The forward walk is always the observed tick series: the arm gate fires on
    a quote, and those quotes are the only real path this box has.
    """
    cut = max(8, int(len(obs) * split))
    window, forward = obs[:cut], obs[cut:]
    rec: dict = {"symbol": sym, "day": day, "obs": len(obs), "fwd": len(forward)}

    if bars is None:
        lows = [p for _, p, _ in window]
        if lookback and lookback > 0:
            lows = lows[-lookback:]
    else:
        # Time-aligned to the same cut, so tick mode and bar mode split the
        # session at the same instant and stay comparable.
        cut_ts = window[-1][0] if window else 0.0
        lows = [lo for ts, lo in bars if ts <= cut_ts]
        # Live clamps db_lookback_bars to [20, 300]; mirror it so a swept value
        # cannot describe a window the desk would never actually use.
        lb = lookback if lookback and lookback > 0 else 0
        if lb:
            lows = lows[-max(20, min(300, lb)):]
        rec["bars"] = len(lows)
    rec["src"] = "ticks" if bars is None else "bars"

    # 1. Does the REAL detector find a shelf in the structure window?
    db = ew.find_double_bottom_support(
        lows, swing=swing, match_pct=match_pct, min_sep_bars=min_sep,
    )
    rec["shelf"] = round(db["support"], 4) if db else None
    anchor = window[-1][1] if window else None

    zone = None
    if db and anchor:
        zone = ew.build_double_bottom_zone_structure(
            db["support"], cfg, low_a=db["low_a"], low_b=db["low_b"],
            last_price=anchor)
    rec["zone"] = ([zone["entry_low"], zone["entry_high"]] if zone else None)
    if zone is None:
        # Two very different failures used to share the label "no_db_zone",
        # and they have OPPOSITE remedies. Either the detector found no pair of
        # matching lows at all (loosen match_pct / swing / min_sep), or it found
        # a shelf and build_double_bottom_zone_structure refused to publish a
        # long zone because price had already broken back under it
        # (ai_watch_db_require_price_above — loosening that means buying broken
        # support, which is what the flag exists to prevent).
        # The price test below mirrors the builder's own rule; it is a
        # diagnostic label only and does not gate anything.
        if db is None:
            rec["result"] = "no_shelf"
        elif (bool(cfg.get("ai_watch_db_require_price_above", True))
              and anchor and anchor < db["support"]):
            rec["result"] = "price_below_shelf"
            rec["anchor"] = round(anchor, 4)
        else:
            rec["result"] = "zone_rejected"
        return rec

    # 2. Replay the real arm gate forward. Only ticks whose recorded reason
    #    proves the indicator gate passed are eligible.
    bull = {"cm_ok": True, "pctr_ok": True, "cm_rsi_rising": True,
            "sell_signal": False, "proximity_pct": 100.0}
    watch = {"symbol": sym, "status": "watching", "structure": zone,
             "indicator": bull}
    entry_i = None
    for i, (ts, px, reason) in enumerate(forward):
        if reason not in PRICE_ONLY_BLOCKS:
            continue
        ok, _why = ew.should_arm_buy(watch, ask=px, bid=None, cfg=cfg)
        if ok:
            entry_i = i
            break
    if entry_i is None:
        rec["result"] = "zone_never_reached"
        return rec

    entry = forward[entry_i][1]
    dec = ew._decision_for_place(zone, ask=entry, cfg=cfg)
    stop, target = float(dec["stop_price"]), float(dec["target_1"])
    if stop <= 0 or stop >= entry or target <= entry:
        rec["result"] = "bad_levels"
        return rec

    res = walk_exits(entry, stop, target, forward[entry_i + 1:], cfg,
                     eod_min=eod_min, dead_min=dead_min, dead_mfe=dead_mfe)
    rec.update({
        "result": "entered",
        "entry": round(entry, 4),
        "stop": round(stop, 4),
        "target": round(target, 4),
        "stop_width_pct": round(100 * (entry - stop) / entry, 2),
        "old_R": round(res["old"][2], 3), "old_reason": res["old"][1],
        "new_R": round(res["new"][2], 3), "new_reason": res["new"][1],
    })
    return rec


def fetch_day_bars(symbol: str, day: str, api_key: str, secret: str,
                   *, feed: str = "iex") -> list[tuple]:
    """1-minute (ts, low) for one symbol-day, ET-day bounded. [] on failure.

    Paginates with explicit 429 backoff and RAISES if a page never succeeds.
    backtest_v2.fetch_bars swallows that case and returns the partial series,
    which silently handed a 3-month sweep one month of data on 2026-08-08. A
    short series here would silently change which lows the detector sees, so
    this fails loudly instead.
    """
    import requests

    start = datetime.fromisoformat(f"{day}T00:00:00").replace(tzinfo=ET)
    end = start + timedelta(days=1)
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"
    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}
    out: list[tuple] = []
    page_token = None

    while True:
        params = {
            "timeframe": "1Min",
            "start": start.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10_000, "feed": feed, "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token
        for attempt in range(6):
            r = requests.get(url, params=params, headers=headers, timeout=60)
            if r.status_code == 429:
                wait = 5 * (2 ** attempt)
                print(f"  [429] {symbol} {day} — waiting {wait}s",
                      file=sys.stderr)
                time.sleep(wait)
                continue
            r.raise_for_status()
            break
        else:
            raise RuntimeError(
                f"{symbol} {day}: repeated 429s, giving up with "
                f"{len(out)} bars — refusing to sweep a truncated series")
        data = r.json()
        for b in data.get("bars") or []:
            try:
                ts = datetime.fromisoformat(
                    str(b["t"]).replace("Z", "+00:00")).timestamp()
                out.append((ts, float(b["l"])))
            except (KeyError, TypeError, ValueError):
                continue
        page_token = data.get("next_page_token")
        if not page_token:
            break
        time.sleep(1.0)

    out.sort()
    return out


def load_bars(ranked: list, cfg: dict, cache_path: Path) -> dict:
    """(sym, day) -> [(ts, low)], cached on disk so a sweep fetches once."""
    cache: dict = {}
    if cache_path.exists():
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            cache = {tuple(k.split("|")): [tuple(x) for x in v]
                     for k, v in raw.items()}
        except (json.JSONDecodeError, OSError, ValueError):
            cache = {}

    api_key = cfg.get("api_key") or cfg.get("alpaca_key") or ""
    secret = cfg.get("secret_key") or cfg.get("alpaca_secret") or ""
    if not (api_key and secret):
        raise SystemExit("no Alpaca credentials in config — cannot fetch bars")

    fetched = 0
    for (sym, day), _obs in ranked:
        if (sym, day) in cache and cache[(sym, day)]:
            continue
        bars = fetch_day_bars(sym, day, api_key, secret)
        cache[(sym, day)] = bars
        fetched += 1
        print(f"  fetched {sym} {day}: {len(bars):,} 1-min bars", file=sys.stderr)
        time.sleep(0.5)

    if fetched:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(
            {f"{s}|{d}": v for (s, d), v in cache.items()}), encoding="utf-8")
    return cache


def summarize(rows: list[dict]) -> dict:
    """Collapse one sweep cell's rows into the numbers that decide the knob."""
    ent = [r for r in rows if r["result"] == "entered"]
    out = {
        "n": len(rows),
        "zone": sum(1 for r in rows if r.get("zone")),
        "no_shelf": sum(1 for r in rows if r["result"] == "no_shelf"),
        "below_shelf": sum(1 for r in rows if r["result"] == "price_below_shelf"),
        "never": sum(1 for r in rows if r["result"] == "zone_never_reached"),
        "entered": len(ent),
        "mean_new_R": None, "mean_old_R": None, "win_rate": None,
        "total_new_R": None,
    }
    if ent:
        out["mean_new_R"] = round(sum(r["new_R"] for r in ent) / len(ent), 3)
        out["mean_old_R"] = round(sum(r["old_R"] for r in ent) / len(ent), 3)
        # Total R is what the desk actually banks: a looser detector that finds
        # more mediocre trades can lose on mean while winning here, and vice
        # versa. Both are printed because they disagree, and the disagreement
        # IS the tradeoff being priced.
        out["total_new_R"] = round(sum(r["new_R"] for r in ent), 3)
        out["win_rate"] = round(
            100.0 * sum(1 for r in ent if r["new_R"] > 0) / len(ent), 1)
    return out


def run_sweep(args, cfg: dict, ranked: list, run_all, *,
              live_match: float, swing: int, min_sep: int,
              bars_mode: bool = False, live_lookback: int = 0) -> int:
    """Grid match_pct x lookback and print the tradeoff table."""
    matches = _parse_list(args.sweep_match, float)
    lookbacks = _parse_list(args.sweep_lookback, int)
    swings = _parse_list(args.sweep_swing, int)
    seps = _parse_list(args.sweep_minsep, int)
    if not (matches and lookbacks and swings and seps):
        print("empty sweep grid", file=sys.stderr)
        return 1

    cells = []
    for m in matches:
        for lb in lookbacks:
            for sw in swings:
                for ms in seps:
                    s = summarize(run_all(m, lb, sw, ms))
                    s.update({"match_pct": m, "lookback": lb,
                              "swing": sw, "min_sep": ms})
                    cells.append(s)

    if args.json:
        print(json.dumps(cells, indent=2))
        return 0

    n = cells[0]["n"] if cells else 0
    print(f"SWEEP — double-bottom detector, {n} symbol-days")
    print(f"live config: match_pct={live_match}  swing={swing}  min_sep={min_sep}")
    if bars_mode:
        print("structure source: REAL 1-MIN BAR LOWS — swing/sep/look are in "
              "live units and transfer to config\n")
    else:
        print("structure source: OBSERVED TICKS — swing/sep/look are an "
              "ordering only, do NOT paste into config (use --bars)\n")

    hdr = (f"{'match%':>7}{'look':>6}{'sw':>4}{'sep':>4}{'zone':>7}{'noShlf':>7}"
           f"{'belowS':>7}{'enter':>7}{'win%':>6}{'meanR':>8}{'totalR':>8}"
           f"{'vs old':>8}  note")
    print(hdr)
    print("-" * len(hdr))
    for c in cells:
        lb = "all" if not c["lookback"] else str(c["lookback"])
        mean_r = f"{c['mean_new_R']:+.3f}" if c["mean_new_R"] is not None else "-"
        tot_r = f"{c['total_new_R']:+.2f}" if c["total_new_R"] is not None else "-"
        win = f"{c['win_rate']:.0f}" if c["win_rate"] is not None else "-"
        delta = ("-" if c["mean_new_R"] is None
                 else f"{c['mean_new_R'] - c['mean_old_R']:+.3f}")
        notes = []
        if (abs(c["match_pct"] - live_match) < 1e-9
                and c["lookback"] == live_lookback
                and c["swing"] == swing and c["min_sep"] == min_sep):
            notes.append("LIVE")
        if 0 < c["entered"] < args.min_cell:
            notes.append("UNDERPOWERED")
        if c["entered"] == 0:
            notes.append("no entries")
        print(f"{c['match_pct']:>7.2f}{lb:>6}{c['swing']:>4}{c['min_sep']:>4}"
              f"{c['zone']:>4}/{c['n']:<2}{c['no_shelf']:>7}{c['below_shelf']:>7}"
              f"{c['entered']:>4}/{c['n']:<2}{win:>6}{mean_r:>8}{tot_r:>8}"
              f"{delta:>8}  {' '.join(notes)}")

    live = next((c for c in cells
                 if abs(c["match_pct"] - live_match) < 1e-9
                 and c["lookback"] == live_lookback
                 and c["swing"] == swing and c["min_sep"] == min_sep),
                None)
    scored = [c for c in cells if c["entered"] >= args.min_cell]
    print(f"\n{'':-<40}")
    if live:
        print(f"live cell : {live['entered']}/{live['n']} entered, "
              f"mean {live['mean_new_R']:+.3f}R, total {live['total_new_R']:+.2f}R"
              if live["entered"] else
              f"live cell : {live['entered']}/{live['n']} entered — nothing to score")
    if scored:
        def _tag(c: dict) -> str:
            return (f"match={c['match_pct']} look={c['lookback'] or 'all'} "
                    f"swing={c['swing']} sep={c['min_sep']}")
        best_mean = max(scored, key=lambda c: c["mean_new_R"])
        best_total = max(scored, key=lambda c: c["total_new_R"])
        print(f"best mean R : {_tag(best_mean)} "
              f"-> {best_mean['mean_new_R']:+.3f}R over {best_mean['entered']} entries")
        print(f"best total R: {_tag(best_total)} "
              f"-> {best_total['total_new_R']:+.2f}R over {best_total['entered']} entries")
        distinct = {(c["zone"], c["entered"], c["total_new_R"]) for c in cells}
        if len(distinct) == 1:
            print("\nEVERY CELL IS IDENTICAL — none of the swept knobs binds on "
                  "this data.\nThe detector returns the MOST RECENT qualifying "
                  "pair (it walks pivots\nnewest-first and returns the first "
                  "match), so a longer lookback only adds\ncandidates it never "
                  "reaches, and recent lows already sit well inside\nmatch_pct. "
                  "Look at the noShlf / belowS columns for the real blocker.")
    else:
        print(f"no cell reached --min-cell {args.min_cell} entries — "
              "the grid is too small to rank")

    print("\nOne session of observed asks, re-scored many ways. Every cell "
          "shares the\nsame path data, so the cells are NOT independent "
          "samples — a knob that wins\nhere has one session behind it. "
          "Confirm on more days before changing config.")
    return 0


def _parse_list(raw: str, cast) -> list:
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        out.append(cast(part))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--events", default=str(report_file("events.jsonl")))
    ap.add_argument("--n", type=int, default=10, help="symbols to replay")
    ap.add_argument("--split", type=float, default=0.4,
                    help="fraction of each series used to build structure")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sweep", action="store_true",
                    help="grid the detector over --sweep-match x --sweep-lookback")
    ap.add_argument("--sweep-match", default="0.2,0.4,0.6,0.8,1.2",
                    help="ai_watch_db_match_pct values, comma-separated")
    ap.add_argument("--sweep-lookback", default="30,60,90,0",
                    help="structure-window cap in OBSERVED TICKS (0 = all)")
    ap.add_argument("--sweep-swing", default="2",
                    help="ai_watch_db_swing_bars values, comma-separated")
    ap.add_argument("--sweep-minsep", default="3",
                    help="ai_watch_db_min_sep_bars values, comma-separated")
    ap.add_argument("--min-cell", type=int, default=5,
                    help="entries below this mark a sweep cell UNDERPOWERED")
    ap.add_argument("--bars", action="store_true",
                    help="run the detector on real 1-min bar lows (live units) "
                         "instead of observed ticks")
    ap.add_argument("--bars-cache",
                    default=str(ROOT / "benchmarks" / "replay_bars_cache.json"),
                    help="on-disk 1-min bar cache, so a sweep fetches once")
    args = ap.parse_args()

    ev = Path(args.events)
    if not ev.exists():
        print(f"no events file at {ev}", file=sys.stderr)
        return 1

    cfg = load_config() or {}
    eod = str(cfg.get("ai_eod_liquidate_time", "15:50")).split(":")
    eod_min = int(eod[0]) * 60 + int(eod[1])
    dead_min = float(cfg.get("ai_dead_trade_min", 90.0))
    dead_mfe = float(cfg.get("ai_dead_trade_mfe_r", 0.25))

    series = load_series(ev)
    ranked = sorted(series.items(), key=lambda kv: -len(kv[1]))[:args.n]

    swing = int(cfg.get("ai_watch_db_swing_bars", 2))
    min_sep = int(cfg.get("ai_watch_db_min_sep_bars", 3))
    live_match = float(cfg.get("ai_watch_db_match_pct", 0.4))

    bar_cache: dict = {}
    if args.bars:
        bar_cache = load_bars(ranked, cfg, Path(args.bars_cache))
        missing = [f"{s} {d}" for (s, d), _ in ranked if not bar_cache.get((s, d))]
        if missing:
            print(f"no bars for: {', '.join(missing)} — those rows will report "
                  f"no_shelf and are NOT evidence about the detector",
                  file=sys.stderr)

    def run_all(match_pct: float, lookback: int,
                sw: int | None = None, ms: int | None = None) -> list[dict]:
        return [
            replay_one(sym, day, obs, cfg, split=args.split,
                       match_pct=match_pct, lookback=lookback,
                       swing=swing if sw is None else sw,
                       min_sep=min_sep if ms is None else ms,
                       eod_min=eod_min, dead_min=dead_min, dead_mfe=dead_mfe,
                       bars=bar_cache.get((sym, day)) if args.bars else None)
            for (sym, day), obs in ranked
        ]

    if args.sweep:
        return run_sweep(args, cfg, ranked, run_all,
                         live_match=live_match, swing=swing, min_sep=min_sep,
                         bars_mode=args.bars,
                         live_lookback=(int(cfg.get("ai_watch_db_lookback_bars", 90))
                                        if args.bars else 0))

    rows = run_all(live_match,
                   int(cfg.get("ai_watch_db_lookback_bars", 90)) if args.bars
                   else 0)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(f"Replay of {len(rows)} symbol-days — prices the desk really observed")
    print(f"config: rr={cfg.get('ai_watch_synth_rr')} "
          f"scale_out={cfg.get('ai_watch_synth_scale_out_pct')}% "
          f"old_trail={cfg.get('ai_watch_synth_trail_pct')}% "
          f"new_trail={cfg.get('ai_runner_trail_r')}R\n")
    hdr = (f"{'sym':6}{'obs':>5}{'shelf':>9}{'zone':>18}{'result':>20}"
           f"{'stop%':>7}{'oldR':>7}{'newR':>7}  exit")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        zone = f"{r['zone'][0]:.2f}-{r['zone'][1]:.2f}" if r.get("zone") else "-"
        shelf = f"{r['shelf']:.2f}" if r.get("shelf") else "-"
        width = f"{r['stop_width_pct']:.2f}" if "stop_width_pct" in r else "-"
        old_r = f"{r['old_R']:+.2f}" if "old_R" in r else "-"
        new_r = f"{r['new_R']:+.2f}" if "new_R" in r else "-"
        print(f"{r['symbol']:6}{r['obs']:>5}{shelf:>9}{zone:>18}"
              f"{r['result']:>20}{width:>7}{old_r:>7}{new_r:>7}"
              f"  {r.get('new_reason', '')}")

    ent = [r for r in rows if r["result"] == "entered"]
    print(f"\n{'':-<40}")
    print(f"no shelf (detector found none): {sum(1 for r in rows if r['result'] == 'no_shelf')}/{len(rows)}")
    print(f"shelf found, price under it   : {sum(1 for r in rows if r['result'] == 'price_below_shelf')}/{len(rows)}")
    print(f"zone drawn but never reached  : {sum(1 for r in rows if r['result'] == 'zone_never_reached')}/{len(rows)}")
    print(f"entered                       : {len(ent)}/{len(rows)}")
    if ent:
        o = sum(r["old_R"] for r in ent) / len(ent)
        n = sum(r["new_R"] for r in ent) / len(ent)
        print(f"\nmean R  old (2.5% trail) {o:+.3f}   new (1R + breakeven floor) {n:+.3f}"
              f"   delta {n - o:+.3f}")
        diff = [r for r in ent if abs(r["new_R"] - r["old_R"]) > 1e-9]
        print(f"trades where the two models differ: {len(diff)}/{len(ent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
