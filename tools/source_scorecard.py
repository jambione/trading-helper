#!/usr/bin/env python3
"""source_scorecard.py — what did each supplier's picks actually do next?

THE QUESTION
  For every name a source PROPOSED, what did the name do over the next 15 / 30
  / 60 / 120 minutes — regardless of whether the desk seated, armed or traded
  it.

WHY THIS AND NOT outcomes.jsonl
  Grading outcomes by ``position_shadow.source`` grades the ~12 names a day
  that became fills, and it grades them by the wrong label: that field is the
  output of ``_merge_source``, which resolves OWNERSHIP (research beats desk
  heat, newest wins otherwise), not origin. A momentum-labelled row may be
  wearing another seed's reason.

  The proposal ledger fixed that by recording ``proposer`` pre-merge alongside
  ``owner``. This tool scores those proposals. Two consequences: attribution is
  real, and the sample is ~12,000 rows a day instead of ~12.

CONTROLS ARE NOT OPTIONAL
  A source that proposes the day's biggest movers scores well even when its
  picks fade, because it selects on movement. Every source is therefore scored
  against same-instant controls and **the reported number is
  ``source − control``**. A run without controls is not reportable, and this
  tool refuses to print one.

    ACROSS   other names proposed by OTHER sources at the same instant
    SELF     the same source's other proposals at the same instant
             (diagnostic: is this episode special, or is the source just
             having a good minute?)

  OUTSIDE / OUTSIDE-VOL (never-proposed names, price- and vol-matched) are
  ``tools/desk_null.py``'s and are not rebuilt here; run that for the
  desk-vs-field question. This tool answers source-vs-source.

OVERLAP — three views, all reported
    all         every proposer credited (double-counts shared symbols)
    exclusive   only episodes where exactly ONE source proposed in the window
    first       credit the earliest proposer
  ``exclusive`` is the honest ranking. **When the three disagree, that
  disagreement is the finding** — it says whether a source adds anything
  beyond what it shares with the others.

COST
  Runs GROSS and stamps every output ``GROSS``. Never silently assumes zero
  cost. Gross is still decisive for the RELATIVE ranking this tool is for.

USAGE
    .venv/bin/python tools/source_scorecard.py --day 2026-09-18
    .venv/bin/python tools/source_scorecard.py --days 3 --json
    .venv/bin/python tools/source_scorecard.py --day 2026-09-18 --horizon 30

  Writes ai_reports/screens/source_<day>.json and updates the rolling
  ai_reports/screens/source_scorecard.json. Runs on the mini (needs an Alpaca
  data client and the proposal ledger).

HONEST LIMITS
  - Cannot score names NO source proposed. The field beyond the desk's
    suppliers is invisible here; that is universe_screen's job.
  - Gross of costs.
  - One episode per (day, symbol, proposer): a source that re-proposes a name
    all morning is credited once, at its first mention.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import drift_screen as DS
from gate_screen import excursion_from
from outcome_slice import required_n

HORIZONS = (15, 30, 60, 120)
DEFAULT_HORIZON = 30
# Two proposals of the same symbol inside this many seconds are one episode.
EPISODE_GAP_SEC = 3600.0
# How close in time a control proposal must be to count as "same instant".
CONTROL_WINDOW_SEC = 300.0
MIN_EPISODES = 30


def ledger_dir() -> Path:
    try:
        from ai_paths import resolve_report_dir
        return resolve_report_dir() / "proposal_ledger"
    except Exception:
        return _ROOT / "ai_reports" / "proposal_ledger"


def screens_dir() -> Path:
    try:
        from ai_paths import resolve_report_dir
        return resolve_report_dir() / "screens"
    except Exception:
        return _ROOT / "ai_reports" / "screens"


# ── Episodes ──────────────────────────────────────────────────────────────────

def load_proposals(days: list[str]) -> list[dict]:
    """Proposal rows for *days*. Heartbeat rows are state, not decisions."""
    out: list[dict] = []
    for day in days:
        p = ledger_dir() / f"{day}.jsonl"
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("heartbeat"):
                    continue
                sym = str(r.get("symbol") or "").upper().strip()
                src = str(r.get("proposer_norm") or r.get("proposer") or "").strip().lower()
                ts = r.get("ts")
                if not sym or not src or not isinstance(ts, (int, float)):
                    continue
                r["_sym"], r["_src"], r["_ts"] = sym, src, float(ts)
                r["_day"] = str(r.get("day") or day)
                out.append(r)
    return out


def build_episodes(rows: list[dict]) -> list[dict]:
    """One episode per (day, symbol, proposer), anchored at first proposal.

    A source that re-proposes the same name every poll would otherwise be
    credited hundreds of times for one opinion, and the loudest source would
    win by volume rather than by being right.
    """
    by_key: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_key[(r["_day"], r["_sym"], r["_src"])].append(r)

    episodes: list[dict] = []
    for (day, sym, src), hits in by_key.items():
        hits.sort(key=lambda x: x["_ts"])
        anchor = None
        for r in hits:
            if anchor is not None and (r["_ts"] - anchor) < EPISODE_GAP_SEC:
                continue
            anchor = r["_ts"]
            episodes.append({
                "day": day, "symbol": sym, "source": src, "ts": r["_ts"],
                "decision": r.get("decision"),
                "reason": r.get("reason"),
                "px": r.get("px"),
                "pct_change": r.get("pct_change"),
                "range_pos": r.get("range_pos"),
                "owner": r.get("owner"),
            })
    episodes.sort(key=lambda e: (e["day"], e["ts"], e["symbol"]))
    return episodes


def tag_overlap(episodes: list[dict]) -> None:
    """Stamp each episode with how many sources proposed it in the window."""
    by_sym: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in episodes:
        by_sym[(e["day"], e["symbol"])].append(e)
    for eps in by_sym.values():
        eps.sort(key=lambda x: x["ts"])
        for e in eps:
            near = [o for o in eps if abs(o["ts"] - e["ts"]) <= EPISODE_GAP_SEC]
            srcs = {o["source"] for o in near}
            e["n_proposers"] = len(srcs)
            e["exclusive"] = len(srcs) == 1
            e["is_first"] = (e["ts"] == min(o["ts"] for o in near)
                             and e["source"] == min(
                                 o["source"] for o in near
                                 if o["ts"] == min(x["ts"] for x in near)))


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_episodes(episodes: list[dict], bars: dict[str, list[dict]],
                   horizon: int) -> list[dict]:
    """Attach forward excursion to each episode. Drops what cannot be priced."""
    out = []
    for e in episodes:
        b = bars.get(e["symbol"])
        if not b:
            continue
        exc = excursion_from(b, e["day"], e["ts"], horizon)
        if not exc:
            continue
        out.append({**e, **exc})
    return out


def _control_for(ep: dict, scored_by_day: dict[str, list[dict]],
                 same_source: bool) -> float | None:
    """Mean net of OTHER episodes at the same instant.

    same_source=False -> ACROSS (other suppliers). This is the control that
    matters: it asks whether THIS source picked better than what the rest of
    the desk was looking at in the same minute, which removes "the whole tape
    was moving" from the comparison.
    """
    peers = []
    for o in scored_by_day.get(ep["day"], ()):
        if o["symbol"] == ep["symbol"]:
            continue
        if abs(o["ts"] - ep["ts"]) > CONTROL_WINDOW_SEC:
            continue
        if same_source and o["source"] != ep["source"]:
            continue
        if not same_source and o["source"] == ep["source"]:
            continue
        peers.append(o["net"])
    return statistics.fmean(peers) if peers else None


def scorecard(scored: list[dict], view: str, horizon: int) -> dict:
    """source -> verdict, for one overlap view. Reported value is net-control."""
    if view == "exclusive":
        pool = [e for e in scored if e.get("exclusive")]
    elif view == "first":
        pool = [e for e in scored if e.get("is_first")]
    else:
        pool = list(scored)

    by_day: dict[str, list[dict]] = defaultdict(list)
    for e in pool:
        by_day[e["day"]].append(e)

    rows: dict[str, dict] = {}
    by_src: dict[str, list[dict]] = defaultdict(list)
    for e in pool:
        by_src[e["source"]].append(e)

    for src, eps in sorted(by_src.items()):
        deltas, nets, ctrls = [], [], []
        for e in eps:
            c = _control_for(e, by_day, same_source=False)
            nets.append(e["net"])
            if c is None:
                continue
            ctrls.append(c)
            deltas.append(e["net"] - c)
        verdict = DS.score(eps)
        n_d = len(deltas)
        mu = statistics.fmean(deltas) if deltas else None
        sd = statistics.pstdev(deltas) if n_d > 1 else 0.0
        se = sd / (n_d ** 0.5) if n_d > 1 and sd else 0.0
        rows[src] = {
            "n_episodes": len(eps),
            "n_scored_vs_control": n_d,
            "sessions": verdict.get("sessions"),
            "sessions_green": verdict.get("sessions_green"),
            "max_session_share": verdict.get("max_session_share"),
            "sign_p": verdict.get("sign_p"),
            "mean_net_pct": round(statistics.fmean(nets), 4) if nets else None,
            "mean_control_pct": round(statistics.fmean(ctrls), 4) if ctrls else None,
            "delta_vs_control_pct": round(mu, 4) if mu is not None else None,
            "delta_sigma": round(mu / se, 2) if (mu is not None and se) else None,
            "required_n_for_delta": (
                required_n(abs(mu), sd) if (mu and sd) else None),
            "verdict": _verdict(n_d, mu, se, sd),
        }
    return {"view": view, "horizon_min": horizon, "cost": "GROSS",
            "sources": rows}


def _verdict(n: int, mu: float | None, se: float, sd: float) -> str:
    """THIN dominates. An underpowered positive is not a winner."""
    if mu is None or n == 0:
        return "EMPTY"
    if n < MIN_EPISODES:
        return "THIN"
    need = required_n(abs(mu), sd) if (mu and sd) else 0
    if need and n < need:
        return "THIN"
    if not se:
        return "THIN"
    sigma = mu / se
    if sigma >= 2.0:
        return "BETTER_THAN_PEERS"
    if sigma <= -2.0:
        return "WORSE_THAN_PEERS"
    return "NO_DIFFERENCE"


# ── CLI ───────────────────────────────────────────────────────────────────────

def _days_arg(args) -> list[str]:
    if args.day:
        return [args.day]
    avail = sorted(p.stem for p in ledger_dir().glob("*.jsonl"))
    return avail[-max(1, args.days):]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--day", help="ET date YYYY-MM-DD")
    ap.add_argument("--days", type=int, default=1, help="most recent N days")
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON,
                    choices=HORIZONS)
    ap.add_argument("--all-horizons", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    days = _days_arg(args)
    rows = load_proposals(days)
    if not rows:
        print(f"no proposal ledger rows for {days} in {ledger_dir()}",
              file=sys.stderr)
        return 2

    episodes = build_episodes(rows)
    tag_overlap(episodes)
    syms = sorted({e["symbol"] for e in episodes})

    start = datetime.strptime(min(days), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(max(days), "%Y-%m-%d").replace(
        tzinfo=timezone.utc) + timedelta(days=1)
    bars = DS.fetch_minutes(syms, start, end)
    if not bars:
        print("no Alpaca data client — this screen runs on the mini "
              "(config/secrets.json api_key)", file=sys.stderr)
        return 2

    horizons = HORIZONS if args.all_horizons else (args.horizon,)
    report = {
        "days": days,
        "proposal_rows": len(rows),
        "episodes": len(episodes),
        "symbols": len(syms),
        "cost": "GROSS",
        "control": "ACROSS (other suppliers, same instant)",
        "by_horizon": {},
    }
    for hz in horizons:
        scored = score_episodes(episodes, bars, hz)
        report["by_horizon"][str(hz)] = {
            "scored_episodes": len(scored),
            "views": {v: scorecard(scored, v, hz)
                      for v in ("all", "exclusive", "first")},
        }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_human(report)

    if not args.no_write:
        d = screens_dir()
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"source_{max(days)}.json"
        out.write_text(json.dumps(report, indent=2, default=str) + "\n")
        print(f"\nwrote {out}")
    return 0


def _dash(v) -> str:
    """None is missing; 0 is a number. `v or "—"` conflated them, and "0 of 5
    sessions green" is exactly the finding this tool exists to surface."""
    return "—" if v is None else str(v)


def _print_human(rep: dict) -> None:
    print(f"\nSOURCE SCORECARD — {', '.join(rep['days'])}   [{rep['cost']}]")
    print(f"  {rep['proposal_rows']:,} proposal rows -> {rep['episodes']:,} "
          f"episodes over {rep['symbols']} symbols")
    print(f"  control: {rep['control']}")
    print("  reported number is (source net - control net); source alone just "
          "rediscovers\n  which supplier names volatile stocks.")
    for hz, block in rep["by_horizon"].items():
        print(f"\n─── {hz} min ─── {block['scored_episodes']:,} scored")
        for view, card in block["views"].items():
            print(f"\n  [{view}]")
            print(f"    {'source':<16}{'n':>6}{'net%':>8}{'ctrl%':>8}"
                  f"{'delta%':>9}{'sigma':>7}{'sess':>6}{'grn':>5}  verdict")
            for src, r in sorted(card["sources"].items(),
                                 key=lambda kv: -(kv[1]["delta_vs_control_pct"]
                                                  or -99)):
                f = lambda v, p=2: ("—" if v is None else f"{v:.{p}f}")
                print(f"    {src:<16}{r['n_scored_vs_control']:>6}"
                      f"{f(r['mean_net_pct']):>8}{f(r['mean_control_pct']):>8}"
                      f"{f(r['delta_vs_control_pct']):>9}{f(r['delta_sigma']):>7}"
                      f"{_dash(r['sessions']):>6}"
                      f"{_dash(r['sessions_green']):>5}  {r['verdict']}")


if __name__ == "__main__":
    raise SystemExit(main())
