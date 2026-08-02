#!/usr/bin/env python3
"""A/B harness: excellent research vs token/cost impact.

Criteria (only these matter for ranking):
  1. Research excellence — structured, grounded, desk-usable output
  2. Token / $ impact — lower is better at a given quality level

Wall-clock is logged for ops but is **not** an optimization target.

Runs controlled variants of the stock-research call (Claude CLI and/or Grok
CLI via xAI subscription), scores each response offline, and appends records
under ``benchmarks/research_ab/``.

Does **not** trade. Does **not** write ``claude_suggestions.json``.

Usage::

    .venv/bin/python tools/research_ab.py --list
    .venv/bin/python tools/research_ab.py --only claude_xhigh_t8 --runs 1
    .venv/bin/python tools/research_ab.py --backend cli --runs 1   # Grok sub
    .venv/bin/python tools/research_ab.py --summarize

Agent note: cells can also be fanned out with parallel subagents — one cell
per agent — as long as each writes a row to results.jsonl (append is atomic
per line). Prefer sequential first so CLI auth/rate limits stay simple.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_env() -> None:
    path = ROOT / "signal_engine.env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.split(" #", 1)[0].strip()
        if k and k not in os.environ:
            os.environ[k] = v


_load_env()

from ai_suggest import (  # noqa: E402
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_MAX_TURNS,
    DEFAULT_PROMPT_FILE,
    DEFAULT_TIMEOUT,
    DEFAULT_XAI_MODEL,
    call_claude,
    last_usage,
    load_prompt,
    parse_model_text,
)
from tools.research_quality import efficiency_metrics, score_research_text  # noqa: E402

OUT_DIR = ROOT / "benchmarks" / "research_ab"
RESULTS_JSONL = OUT_DIR / "results.jsonl"


def default_matrix() -> list[dict[str, Any]]:
    """Levers that move *token impact* and *research excellence*.

    Time-to-complete is ignored as a goal. Search rounds and invocation count
    dominate spend (see prior token_metrics + cost-shape notes).
    """
    cells: list[dict[str, Any]] = []

    # Effort sweep — does higher effort buy excellence without much token cost?
    for effort in ("low", "medium", "xhigh"):
        cells.append({
            "id": f"claude_{effort}_t8",
            "backend": "claude_cli",
            "model": DEFAULT_CLAUDE_MODEL,
            "effort": effort,
            "max_turns": 8,
            "live_search": True,
            "use_prior_context": False,
        })

    # Turn budget — primary token lever (search rounds).
    for turns in (4, 6):
        cells.append({
            "id": f"claude_xhigh_t{turns}",
            "backend": "claude_cli",
            "model": DEFAULT_CLAUDE_MODEL,
            "effort": "xhigh",
            "max_turns": turns,
            "live_search": True,
            "use_prior_context": False,
        })

    # No web search — token floor; excellence floor (often low).
    cells.append({
        "id": "claude_xhigh_nosearch",
        "backend": "claude_cli",
        "model": DEFAULT_CLAUDE_MODEL,
        "effort": "xhigh",
        "max_turns": 4,
        "live_search": False,
        "use_prior_context": False,
    })

    # Prior context — does update-mode reduce fresh tokens for similar quality?
    cells.append({
        "id": "claude_xhigh_prior",
        "backend": "claude_cli",
        "model": DEFAULT_CLAUDE_MODEL,
        "effort": "xhigh",
        "max_turns": 8,
        "live_search": True,
        "use_prior_context": True,
    })

    # Grok via xAI subscription (CLI). Default production hypothesis: max_turns=4
    # (A/B 2026-08-02). Keep t8 only as a regression cell, not the default.
    for turns in (4, 8):
        cells.append({
            "id": f"grok_t{turns}",
            "backend": "cli",
            "model": DEFAULT_XAI_MODEL,
            "effort": "",
            "max_turns": turns,
            "live_search": True,
            "use_prior_context": False,
        })

    return cells


def _load_prompt(path: str | None) -> str:
    rel = path or DEFAULT_PROMPT_FILE
    p = Path(rel)
    if not p.is_absolute():
        p = ROOT / p
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return load_prompt(rel)


def run_cell(
    cell: dict[str, Any],
    *,
    prompt: str,
    timeout: float,
    experiment_id: str,
    run_idx: int,
) -> dict[str, Any]:
    t0 = time.time()
    err = ""
    text = ""
    last_usage.clear()
    try:
        text = call_claude(
            prompt,
            model=cell.get("model") or DEFAULT_CLAUDE_MODEL,
            timeout=timeout,
            live_search=bool(cell.get("live_search", True)),
            trading=False,
            max_turns=int(cell.get("max_turns") or DEFAULT_MAX_TURNS),
            use_prior_context=bool(cell.get("use_prior_context", False)),
            backend=str(cell.get("backend") or "claude_cli"),
            effort=(cell.get("effort") or None) or None,
        )
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    wall = round(time.time() - t0, 2)

    rows = parse_model_text(text) if text else []
    quality = score_research_text(text, rows)
    usage = dict(last_usage) if last_usage else {}
    eff = efficiency_metrics(quality, usage, wall_sec=wall)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_name = f"{experiment_id}_{cell['id']}_r{run_idx}_{stamp}.md"
    report_path = OUT_DIR / "reports" / report_name
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"# research_ab {cell['id']} run={run_idx}\n\n"
            f"experiment={experiment_id}\n"
            f"cell={json.dumps(cell)}\n"
            f"wall_sec={wall} error={err!r}\n\n---\n\n"
        )
        report_path.write_text(header + (text or ""), encoding="utf-8")
    except Exception:
        report_path = Path("")

    return {
        "ts": time.time(),
        "experiment_id": experiment_id,
        "run_idx": run_idx,
        "cell_id": cell["id"],
        "cell": cell,
        "error": err,
        "wall_sec": wall,
        "quality": quality,
        "usage": usage,
        "efficiency": eff,
        "report_path": str(report_path) if report_path else "",
        "n_suggestions": quality.get("n_suggestions"),
        "quality_0_100": quality.get("quality_0_100"),
        "total_cost_usd": eff.get("total_cost_usd"),
        "total_tokens_impact": eff.get("total_tokens_impact"),
        "quality_per_usd": eff.get("quality_per_usd"),
        "quality_per_1k_tokens": eff.get("quality_per_1k_tokens"),
    }


def append_result(row: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with RESULTS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _avg(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def summarize(path: Path = RESULTS_JSONL) -> str:
    """Rank cells by excellence / token impact (not wall time)."""
    if not path.is_file():
        return f"No results yet at {path}"
    by_cell: dict[str, list[dict]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        by_cell.setdefault(row.get("cell_id") or "?", []).append(row)

    header = (
        "cell_id\tn\tavg_quality\tavg_$\tavg_tok\tq_per_$\tq_per_1k_tok\t"
        "avg_sugs\terrors"
    )
    lines = [header, "-" * 100]
    ranked_q: list[tuple[float, str]] = []
    ranked_eff: list[tuple[float, str]] = []

    for cid, rows in sorted(by_cell.items()):
        qs = [float(r["quality_0_100"]) for r in rows
              if r.get("quality_0_100") is not None]
        cs = [float(r["total_cost_usd"]) for r in rows
              if r.get("total_cost_usd") is not None]
        ts = [float(r["total_tokens_impact"]) for r in rows
              if r.get("total_tokens_impact") is not None]
        ss = [float(r["n_suggestions"]) for r in rows
              if r.get("n_suggestions") is not None]
        errs = sum(1 for r in rows if r.get("error"))
        avg_q = _avg(qs) or 0.0
        avg_c = _avg(cs)
        avg_t = _avg(ts)
        avg_s = _avg(ss)
        q_per_usd = (avg_q / avg_c) if avg_c else None
        q_per_1k = (avg_q / (avg_t / 1000.0)) if avg_t else None
        line = "\t".join([
            cid,
            str(len(rows)),
            f"{avg_q:.1f}",
            f"{avg_c:.3f}" if avg_c is not None else "—",
            f"{avg_t:.0f}" if avg_t is not None else "—",
            f"{q_per_usd:.1f}" if q_per_usd is not None else "—",
            f"{q_per_1k:.1f}" if q_per_1k is not None else "—",
            f"{avg_s:.1f}" if avg_s is not None else "—",
            str(errs),
        ])
        lines.append(line)
        ranked_q.append((avg_q, cid))
        # Prefer token efficiency when $ missing (Grok CLI often has no $).
        eff_key = q_per_1k if q_per_1k is not None else (q_per_usd or -1.0)
        ranked_eff.append((eff_key if eff_key is not None else -1.0, cid))

    lines.append("")
    lines.append("Best excellence (avg quality_0_100):")
    for score, cid in sorted(ranked_q, reverse=True):
        lines.append(f"  {score:5.1f}  {cid}")
    lines.append("")
    lines.append("Best efficiency (quality per 1k tokens, else quality per $):")
    for score, cid in sorted(ranked_eff, reverse=True):
        lines.append(f"  {score:7.1f}  {cid}")
    lines.append("")
    lines.append(
        "Pareto tip: keep cells that are not dominated on (quality, tokens) — "
        "higher quality at same-or-lower token impact, or same quality cheaper."
    )
    return "\n".join(lines)


def write_csv_summary(path: Path = RESULTS_JSONL) -> Path | None:
    if not path.is_file():
        return None
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if not rows:
        return None
    csv_path = OUT_DIR / "results_summary.csv"
    fields = [
        "ts", "experiment_id", "cell_id", "run_idx", "error",
        "quality_0_100", "n_suggestions", "total_cost_usd", "total_tokens_impact",
        "quality_per_usd", "quality_per_1k_tokens",
        "backend", "model", "effort", "max_turns", "live_search", "use_prior_context",
        "wall_sec",  # diagnostic only
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            cell = r.get("cell") or {}
            w.writerow({
                **{k: r.get(k) for k in fields},
                "backend": cell.get("backend"),
                "model": cell.get("model"),
                "effort": cell.get("effort"),
                "max_turns": cell.get("max_turns"),
                "live_search": cell.get("live_search"),
                "use_prior_context": cell.get("use_prior_context"),
            })
    return csv_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Research excellence vs token impact A/B harness")
    ap.add_argument("--list", action="store_true", help="Print matrix and exit")
    ap.add_argument("--summarize", action="store_true", help="Summarize results")
    ap.add_argument("--only", action="append", default=[],
                    help="Run only this cell id (repeatable)")
    ap.add_argument("--backend", choices=("claude_cli", "cli", "all"), default="all")
    ap.add_argument("--runs", type=int, default=1, help="Repeats per cell")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--prompt-file", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.summarize:
        print(summarize())
        p = write_csv_summary()
        if p:
            print(f"\nCSV: {p}")
        return 0

    matrix = default_matrix()
    if args.backend != "all":
        matrix = [c for c in matrix if c["backend"] == args.backend]
    if args.only:
        want = set(args.only)
        matrix = [c for c in matrix if c["id"] in want]
        missing = want - {c["id"] for c in matrix}
        if missing:
            print(f"Unknown cell ids: {sorted(missing)}", file=sys.stderr)
            print("Known:", ", ".join(c["id"] for c in default_matrix()),
                  file=sys.stderr)
            return 2

    if args.list or args.dry_run:
        n_claude = sum(1 for c in matrix if c["backend"] == "claude_cli")
        n_grok = sum(1 for c in matrix if c["backend"] == "cli")
        print(f"{len(matrix)} cells × {args.runs} runs = {len(matrix) * args.runs} calls")
        print(f"  Claude cells: {n_claude}  Grok(CLI) cells: {n_grok}")
        print("  Ranking targets: quality_0_100  and  quality_per_1k_tokens / quality_per_$")
        print("  Wall time: recorded, not optimized\n")
        for c in matrix:
            print(f"  {c['id']:28} backend={c['backend']:10} "
                  f"effort={c.get('effort') or '-':6} turns={c['max_turns']} "
                  f"search={c['live_search']} prior={c['use_prior_context']}")
        print("\nApprox Claude $ if all Claude cells run once: "
              f"~${0.40 * n_claude * args.runs:.2f} (order-of-magnitude)")
        return 0

    experiment_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_" + uuid.uuid4().hex[:6]
    )
    prompt = _load_prompt(args.prompt_file)
    print(f"experiment_id={experiment_id}")
    print(f"cells={len(matrix)} runs_each={args.runs}")
    print(f"results → {RESULTS_JSONL}")
    print("criteria: excellence + token/$ impact (not wall time)")

    for cell in matrix:
        for run_idx in range(args.runs):
            print(f"\n=== {cell['id']} run {run_idx + 1}/{args.runs} ===",
                  flush=True)
            row = run_cell(
                cell,
                prompt=prompt,
                timeout=args.timeout,
                experiment_id=experiment_id,
                run_idx=run_idx,
            )
            append_result(row)
            print(
                f"  quality={row.get('quality_0_100')}  "
                f"cost={row.get('total_cost_usd')}  "
                f"tok={row.get('total_tokens_impact')}  "
                f"q/1k={row.get('quality_per_1k_tokens')}  "
                f"sugs={row.get('n_suggestions')}  "
                f"err={row.get('error')!r}",
                flush=True,
            )
            if row.get("quality", {}).get("symbols"):
                print(f"  symbols={row['quality']['symbols']}", flush=True)

    print("\n" + summarize())
    p = write_csv_summary()
    if p:
        print(f"\nCSV: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
