#!/usr/bin/env python3
"""Capital-first auditor of desk reports and P&L.

Read-only. Does not arm, does not write bot_config, does not place orders.
It packages the numbers the operator already measures (paper vs live-equivalent,
session-level go-live, MFE − spread) and a keep / kill / measure brief aimed
at *improving* the project — not approving a live book.

    python3 tools/capital_auditor.py
    python3 tools/capital_auditor.py --days 10 --json
    python3 tools/capital_auditor.py --classify "widen the give to cut stomps"

The LLM skill (`.grok/skills/trading-capital-audit`) reads this brief plus
HANDOFF.md and writes improvement suggestions with anchors. This file is the
fact pack so the model cannot invent P&L.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import desk_product  # noqa: E402
import eod  # noqa: E402
from ai_paths import resolve_report_dir  # noqa: E402

LEGACY_REPORT_DIR = ROOT / "claude_reports"

# Families already measured out (HANDOFF.md §6). A suggestion matching these
# is a KILL unless it names a new universe + hold + cost + session test.
FORBIDDEN = (
    {
        "id": "indicator_permute",
        "label": "RSI / %R / RVOL / heat / zone overlay on the squeeze list",
        "pattern": r"\b(rsi|%r|percent[- ]?r|rvol|heat|in_zone|cm_ok|pctr)\b",
        "anchor": "HANDOFF.md §6; gate_screen",
    },
    {
        "id": "h4_h3_late",
        "label": "enable H4 / H3 / late-hold paper",
        "pattern": r"\b(h4_paper|h3_paper|late_hold|ai_h4|ai_h3|h4_swing)\b",
        "anchor": "HANDOFF.md §4, §6; PROFIT_REDESIGN superseded",
    },
    {
        "id": "widen_give",
        "label": "widen trail give expecting P&L",
        "pattern": r"\b(widen.{0,20}give|give_r|trail.{0,12}(wider|loosen)|stomp)\b",
        "anchor": "HANDOFF.md §2, §6 — give change cuts stomps, not the mean",
    },
    {
        "id": "admission_latency",
        "label": "close admission latency / captured / earlier seeds",
        "pattern": r"\b(admission latency|captured|freshness ranker|earlier seed|faster arm)\b",
        "anchor": "HANDOFF.md §5C, §6 — captured does not predict R",
    },
    {
        "id": "drift_only",
        "label": "grade a universe on drift / MFE alone",
        "pattern": r"\b(drift_screen without|mfe only|range without direction)\b",
        "anchor": "HANDOFF.md §6 — burst looks huge on MFE, MFE/MAE 0.91",
    },
)

GO_LIVE = {
    "sessions_need": eod.GO_LIVE_SESSIONS,
    "win_sessions_need": eod.GO_LIVE_WIN_SESSIONS,
    "trades_need": eod.GO_LIVE_MIN_TRADES,
}


def _jsonl(path: Path):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def report_roots(report_dir: Path | None = None, repo: Path | None = None) -> list[Path]:
    """Every tree that still holds desk records.

    Live writers use ``ai_reports/`` (or ``AI_REPORT_DIR``). The MacBook clone
    often has the historical trail still in ``claude_reports/``. An auditor that
    only opens one folder will report empty P&L and miss thousands of events.
    """
    repo = repo or ROOT
    out: list[Path] = []
    for p in (
        report_dir,
        resolve_report_dir(),
        repo / "ai_reports",
        repo / "claude_reports",
    ):
        if p is None:
            continue
        p = Path(p)
        if p not in out:
            out.append(p)
    return out


def _jsonl_stat(path: Path, *, max_kind: int = 80_000) -> dict[str, Any]:
    n = 0
    first_ts = last_ts = None
    kinds: dict[str, int] = defaultdict(int)
    ok = fail = 0
    r_sum = 0.0
    r_n = 0
    if not path.exists() or not path.is_file():
        return {"path": str(path), "exists": False, "lines": 0}
    for row in _jsonl(path):
        n += 1
        ts = row.get("ts") or row.get("exit_time") or row.get("entry_time")
        try:
            tsf = float(ts)
        except (TypeError, ValueError):
            tsf = None
        if tsf is not None:
            first_ts = tsf if first_ts is None else min(first_ts, tsf)
            last_ts = tsf if last_ts is None else max(last_ts, tsf)
        kind = row.get("kind") or row.get("action") or row.get("close_reason")
        if kind and n <= max_kind:
            kinds[str(kind)] += 1
        if row.get("ok") is True:
            ok += 1
        elif row.get("ok") is False:
            fail += 1
        r = row.get("realized_r_multiple")
        if r is not None:
            try:
                r_sum += float(r)
                r_n += 1
            except (TypeError, ValueError):
                pass
    st = path.stat()
    rec: dict[str, Any] = {
        "path": str(path),
        "exists": True,
        "lines": n,
        "bytes": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }
    if first_ts is not None:
        rec["first_day"] = eod.bars.day_of(first_ts)
        rec["last_day"] = eod.bars.day_of(last_ts)
    if kinds:
        rec["kinds"] = dict(sorted(kinds.items(), key=lambda kv: -kv[1])[:12])
    if ok or fail:
        rec["ok"] = ok
        rec["fail"] = fail
    if r_n:
        rec["n_scored_r"] = r_n
        rec["sum_r"] = round(r_sum, 4)
    return rec


def _list_newest(dir_path: Path, pattern: str, n: int = 8) -> list[dict[str, Any]]:
    if not dir_path.is_dir():
        return []
    files = [p for p in dir_path.glob(pattern) if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:n]:
        st = p.stat()
        out.append({
            "path": str(p.relative_to(ROOT) if ROOT in p.parents or p.parent == ROOT else p),
            "bytes": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        })
    return out


def inventory(report_dir: Path | None = None, repo: Path | None = None) -> dict[str, Any]:
    """Catalog every report pile the auditor is expected to look through."""
    repo = repo or ROOT
    roots = report_roots(report_dir, repo)
    jsonl_names = (
        "outcomes.jsonl", "events.jsonl", "trades.jsonl", "token_metrics.jsonl",
        "daily_ledger.jsonl", "shadow.jsonl", "rejects.jsonl",
        "signal_shadow.jsonl",
    )
    files = []
    seen: set[str] = set()
    for root in roots:
        for name in jsonl_names:
            p = root / name
            key = str(p.resolve()) if p.exists() else str(p)
            if key in seen:
                continue
            seen.add(key)
            if p.exists():
                files.append(_jsonl_stat(p))
    extras: list[dict[str, Any]] = []
    rab = repo / "benchmarks" / "research_ab" / "results.jsonl"
    if rab.exists():
        extras.append(_jsonl_stat(rab))
    extras.extend(_list_newest(repo / "benchmarks", "*.csv", n=8))
    extras.extend(_list_newest(repo / "momentum-monitor" / "journal", "*.jsonl", n=8))
    extras.extend(_list_newest(repo / "logs", "*.log", n=6))
    research = []
    for root in roots:
        research.extend(_list_newest(root, "*research*.md", n=6))
        research.extend(_list_newest(root / "daily", "*.md", n=4))
        research.extend(_list_newest(root / "screens", "*", n=4))
        research.extend(_list_newest(root, "eod_*.log", n=3))
        research.extend(_list_newest(root, "latest.md", n=1))
    docs = []
    for rel in ("HANDOFF.md", "docs/PROFIT_REDESIGN.md", "docs/DESK_ROADMAP.md",
                "BENCHMARKS.md"):
        p = repo / rel
        if p.exists():
            docs.append({
                "path": rel,
                "bytes": p.stat().st_size,
                "mtime": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat(),
            })
    n_out = sum(f.get("lines", 0) for f in files if f["path"].endswith("outcomes.jsonl"))
    n_ev = sum(f.get("lines", 0) for f in files if f["path"].endswith("events.jsonl"))
    gap = None
    if n_ev > 100 and n_out < 30:
        gap = (
            f"events={n_ev} but outcomes={n_out}. This clone is missing the "
            "closed-trade ledger (often still on the mini). Do not invent P&L."
        )
    return {
        "roots": [str(r) for r in roots if r.exists()],
        "jsonl": files,
        "related": extras,
        "research_and_daily": research[:20],
        "docs": docs,
        "gap": gap,
        "totals": {"outcomes_lines": n_out, "events_lines": n_ev},
    }


def load_sessions(days_back: int, report_dir: Path | None = None,
                  repo: Path | None = None):
    rows: dict[str, list] = defaultdict(list)
    seen: set[tuple] = set()
    paths = []
    for root in report_roots(report_dir, repo):
        p = root / "outcomes.jsonl"
        if p.exists() and p not in paths:
            paths.append(p)
    for path in paths:
        for d in _jsonl(path):
            ts = d.get("ts")
            if not ts:
                continue
            key = (eod.bars.day_of(ts), str(d.get("symbol") or ""),
                   d.get("realized_r_multiple"), d.get("exit_time"))
            if key in seen:
                continue
            seen.add(key)
            rows[eod.bars.day_of(ts)].append(d)
    days = sorted(rows)[-days_back:]
    return days, {k: rows[k] for k in days}


def go_live_check(scored: dict[str, dict]) -> dict[str, Any]:
    days = sorted(scored)
    wins = sum(1 for d in days if scored[d]["live_r"] > 0)
    trades = sum(scored[d]["n"] for d in days)
    mfes = [scored[d]["mfe_less_spread"] for d in days
            if scored[d]["mfe_less_spread"] is not None]
    ok_s = wins >= GO_LIVE["win_sessions_need"] and len(days) >= GO_LIVE["sessions_need"]
    ok_n = trades >= GO_LIVE["trades_need"]
    ok_m = bool(mfes) and statistics.median(mfes) > 0
    return {
        "n_sessions": len(days),
        "wins": wins,
        "trades": trades,
        "median_mfe_less_spread": (statistics.median(mfes) if mfes else None),
        "sessions_ok": ok_s,
        "trades_ok": ok_n,
        "mfe_ok": ok_m,
        "pass": ok_s and ok_n and ok_m,
        "bar": GO_LIVE,
    }


def classify_proposal(text: str) -> dict[str, Any]:
    """KILL if the text is a measured-out family; else MEASURE (never auto-KEEP)."""
    raw = text or ""
    hits = []
    for fam in FORBIDDEN:
        if re.search(fam["pattern"], raw, re.I | re.S):
            hits.append({k: fam[k] for k in ("id", "label", "anchor")})
    if hits:
        return {
            "verdict": "KILL",
            "reason": "matches a family already measured out",
            "families": hits,
        }
    return {
        "verdict": "MEASURE",
        "reason": "not a known-dead family; still needs universe + hold + cost + session test before any arm",
        "families": [],
    }


def posture(cfg: dict | None, golive: dict) -> dict[str, Any]:
    """Capital first: if go-live fails, the improvement is observe / size 0, not a new entry rule."""
    allows = desk_product.allows_new_entries(cfg)
    product = desk_product.product(cfg)
    if not golive["pass"]:
        stance = "observe"
        headline = (
            "Go-live is not met. Improve the project by not arming new size "
            "and by measuring a tape that can clear cost — not by retuning the scalp."
        )
    elif allows:
        stance = "armed_but_review"
        headline = (
            "Go-live bars are met on this window. Still do not treat that as "
            "approval to raise size; confirm the window is not one fat session."
        )
    else:
        stance = "observe"
        headline = "Product is observe. Keep it there until a universe_screen PASS."
    return {
        "stance": stance,
        "desk_product": product,
        "allows_new_entries": allows,
        "h4_paper": desk_product.h4_paper(cfg),
        "h3_paper": desk_product.h3_paper(cfg),
        "headline": headline,
    }


def _load_cfg() -> dict:
    try:
        from config import load_config
        return load_config()
    except Exception:
        return {}


def audit(days_back: int = 10, report_dir: Path | None = None,
          cfg: dict | None = None, repo: Path | None = None) -> dict[str, Any]:
    report_dir = report_dir or resolve_report_dir()
    repo = repo or ROOT
    cfg = cfg if cfg is not None else _load_cfg()
    corp = inventory(report_dir, repo)
    days, by_day = load_sessions(days_back, report_dir, repo)
    trig: dict = {}
    old_events = eod.EVENTS
    try:
        for root in report_roots(report_dir, repo):
            ev = root / "events.jsonl"
            if not ev.exists():
                continue
            eod.EVENTS = str(ev)
            for k, v in eod.exit_slip_by_trade(set(days) if days else set()).items():
                trig.setdefault(k, []).extend(v)
    finally:
        eod.EVENTS = old_events
    scored = {d: eod.score_session(by_day[d], trig) for d in days}
    golive = go_live_check(scored)
    pos = posture(cfg, golive)
    latest = days[-1] if days else None
    today = scored.get(latest) if latest else None
    paper_vs_live = None
    if today is not None:
        paper_vs_live = {
            "session": latest,
            "n": today["n"],
            "paper_r": today["paper_r"],
            "live_r": today["live_r"],
            "paper_usd": today["paper_usd"],
            "live_usd": today["live_usd"],
            "mfe_less_spread": today["mfe_less_spread"],
            "flatter": (today["paper_r"] - today["live_r"])
            if today["n"] else 0.0,
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_dir": str(report_dir),
        "corpus": corp,
        "days_back": days_back,
        "sessions": days,
        "scored": scored,
        "go_live": golive,
        "posture": pos,
        "latest": paper_vs_live,
        "keep": [
            {
                "item": "ratchet 0.10R as protective exit",
                "why": "beats hold and time exits on the same entries; truncates left tail",
                "anchor": "HANDOFF.md §2",
            },
            {
                "item": "session as unit of independence + live-equivalent P&L",
                "why": "paper under-charges the exit; pooling one green day lies",
                "anchor": "tools/eod.py; PROFIT_REDESIGN §1",
            },
            {
                "item": "universe_screen pay bar (do not edit constants to pass)",
                "why": "asks whether a watchlist is tradeable before building a gate",
                "anchor": "HANDOFF.md §7",
            },
        ],
        "kill": [
            {"item": fam["label"], "anchor": fam["anchor"]} for fam in FORBIDDEN
        ],
        "measure": [
            {
                "item": "desk_px:50- weekly universe_screen",
                "why": "only cell with rising MFE/MAE and 8/10 green; thin n; more tape not an arm",
                "anchor": "HANDOFF.md §5E",
            },
            {
                "item": "ai_max_spread_r once fill n is real",
                "why": "n=28 could not pick a threshold; a book wider than the move is unwinnable",
                "anchor": "HANDOFF.md §1, §8",
            },
        ],
        "improve_rules": [
            "Suggestions must improve the project: measurement, universe, cost honesty, or capital posture.",
            "A suggestion is not an approval to trade. PASS on go-live still is not a size-up.",
            "Every profit idea names universe + hold + cost model + session-level PASS/FAIL before code.",
            "Do not write bot_config, desk_product, or broker calls from this auditor.",
        ],
    }


def render_markdown(payload: dict) -> str:
    g = payload["go_live"]
    p = payload["posture"]
    latest = payload.get("latest")
    lines = [
        f"# Capital audit — improve the project",
        "",
        f"Generated `{payload['generated_at']}`  ·  days={payload['days_back']}",
        "",
        f"**Stance:** `{p['stance']}`  ·  desk_product=`{p['desk_product']}`  ·  "
        f"allows_new_entries={p['allows_new_entries']}",
        "",
        p["headline"],
        "",
        "## Go-live (live-equivalent, session-level)",
        "",
        f"- [{'x' if g['sessions_ok'] else ' '}] {g['wins']}/{g['n_sessions']} "
        f"sessions live-positive (need {g['bar']['win_sessions_need']} of "
        f"{g['bar']['sessions_need']})",
        f"- [{'x' if g['trades_ok'] else ' '}] {g['trades']} trades "
        f"(need {g['bar']['trades_need']})",
        f"- [{'x' if g['mfe_ok'] else ' '}] median MFE−spread "
        + (f"{g['median_mfe_less_spread']:+.3f}R" if g['median_mfe_less_spread'] is not None else "—")
        + " (need > 0)",
        f"- **pass={g['pass']}** — this is a capital gate, not an approval.",
        "",
    ]
    corp = payload.get("corpus") or {}
    if corp:
        lines += ["## Corpus (everything this agent looked through)", ""]
        tot = corp.get("totals") or {}
        lines.append(
            f"- outcomes_lines={tot.get('outcomes_lines', 0)}  "
            f"events_lines={tot.get('events_lines', 0)}"
        )
        if corp.get("gap"):
            lines.append(f"- **gap:** {corp['gap']}")
        lines.append(f"- roots: {', '.join(corp.get('roots') or [])}")
        lines.append("")
        for f in corp.get("jsonl") or []:
            extra = ""
            if f.get("last_day"):
                extra = f"  {f.get('first_day')}→{f.get('last_day')}"
            if f.get("sum_r") is not None:
                extra += f"  sum_r={f['sum_r']:+.3f} (n={f.get('n_scored_r')})"
            if f.get("kinds"):
                extra += "  kinds=" + ",".join(
                    f"{k}:{v}" for k, v in list(f["kinds"].items())[:5])
            rel = f["path"]
            try:
                rel = str(Path(f["path"]).relative_to(ROOT))
            except Exception:
                pass
            lines.append(f"- `{rel}`  {f['lines']} lines{extra}")
        newest = (corp.get("research_and_daily") or [])[:8]
        if newest:
            lines += ["", "Newest research / daily / screens:"]
            for it in newest:
                lines.append(f"- `{it['path']}`")
        benches = [x for x in (corp.get("related") or []) if "benchmarks" in x.get("path", "")]
        if benches:
            lines += ["", "Benchmarks (newest):"]
            for it in benches[:6]:
                lines.append(f"- `{it.get('path')}`")
        journals = [x for x in (corp.get("related") or [])
                    if "journal" in x.get("path", "")]
        if journals:
            lines += ["", "Monitor journal:"]
            for it in journals[:5]:
                n = it.get("lines")
                suffix = f"  {n} lines" if n else ""
                lines.append(f"- `{it.get('path')}`{suffix}")
        lines.append("")
        lines.append(
            "Read the newest files in that list; do not dump entire jsonl into a note."
        )
        lines.append("")
    if latest:
        lines += [
            f"## Latest session `{latest['session']}`",
            "",
            f"- n={latest['n']}",
            f"- paper {latest['paper_r']:+.3f} R  (${latest['paper_usd']:+.2f})",
            f"- live-equivalent {latest['live_r']:+.3f} R  (${latest['live_usd']:+.2f})",
            f"- paper flatter by {latest['flatter']:+.3f} R",
            "- MFE−spread "
            + (f"{latest['mfe_less_spread']:+.3f} R" if latest['mfe_less_spread'] is not None else "—")
            + "  (below 0 → no exit setting rescues the entry)",
            "",
        ]
    lines += ["## KEEP (do not rip out)", ""]
    for k in payload["keep"]:
        lines.append(f"- **{k['item']}** — {k['why']}  _{k['anchor']}_")
    lines += ["", "## KILL (do not re-propose as an improvement)", ""]
    for k in payload["kill"]:
        lines.append(f"- {k['item']}  _{k['anchor']}_")
    lines += ["", "## MEASURE (how to improve next)", ""]
    for k in payload["measure"]:
        lines.append(f"- **{k['item']}** — {k['why']}  _{k['anchor']}_")
    lines += ["", "## Rules for suggestions that improve the project", ""]
    for r in payload["improve_rules"]:
        lines.append(f"- {r}")
    lines.append("")
    return "\n".join(lines)


def write_artifacts(payload: dict, report_dir: Path | None = None) -> dict[str, str]:
    report_dir = report_dir or resolve_report_dir()
    out = report_dir / "audit"
    out.mkdir(parents=True, exist_ok=True)
    day = (payload.get("latest") or {}).get("session") or "none"
    md_path = out / f"{day}.md"
    json_path = out / f"{day}.json"
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    dump = {k: v for k, v in payload.items() if k != "scored"}
    dump["scored"] = payload["scored"]
    json_path.write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")
    latest = out / "latest.md"
    latest.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    return {"md": str(md_path), "json": str(json_path), "latest": str(latest)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--classify", default="", help="classify one proposal string")
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    if args.classify:
        print(json.dumps(classify_proposal(args.classify), indent=2))
        return 0
    payload = audit(days_back=args.days)
    if not args.no_write:
        paths = write_artifacts(payload)
        payload["wrote"] = paths
    if args.json:
        print(json.dumps({k: v for k, v in payload.items() if k != "scored"},
                         indent=2, default=str))
    else:
        print(render_markdown(payload))
        if payload.get("wrote"):
            print(f"wrote {payload['wrote']['latest']}")
    return 0 if payload["sessions"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
