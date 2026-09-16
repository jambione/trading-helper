#!/usr/bin/env python3
"""Half-split dig for entry RSI packages (read-only).

Reuses ``entry_arm_ab`` prepare / gate_keep / score_cell — does not fork scoring
and does not write ``config/bot_config.json``.

Usage (prefer mini + .venv)::

    .venv/bin/python tools/entry_arm_half_split_dig.py \\
      --from 2026-09-11 --to 2026-09-16
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import entry_arm_ab as ab  # noqa: E402

OUT_DIR = ab.OUT_DIR

# Pre-registered dig cells (narrower packages + control). Overlays resolved
# against LIVE via merge_cell when present in search.phase_a; else built here.
DIG_IDS = (
    "LIVE",
    "rsi_dir_only_fall_gt10",
    "rsi_mistimed_off",
    "rsi_fall_block_10",
    "rsi_max70",
    "rsi_max_off",
    "rsi_dir_mistimed_off_keep_max60",
)

# Fallback overlays if search.json is older than this dig brief.
FALLBACK_OVERLAYS: dict[str, dict[str, Any]] = {
    "rsi_dir_mistimed_off_keep_max60": {
        "id": "rsi_dir_mistimed_off_keep_max60",
        "rsi_max": 60.0,
        "rsi_require_rising": False,
        "rsi_allow_falling_below": 10.0,
        "mistimed_heat": False,
    },
}


def _fmt(x: Any, digits: int = 3) -> str:
    if x is None:
        return "—"
    if isinstance(x, bool):
        return str(x)
    try:
        return f"{float(x):+.{digits}f}" if digits else str(x)
    except (TypeError, ValueError):
        return str(x)


def _fmt_pct(x: Any, digits: int = 3) -> str:
    if x is None:
        return "—"
    return f"{float(x):+.{digits}f}%"


def half_map(days_sorted: list[str]) -> dict[str, str]:
    mid = max(1, len(days_sorted) // 2)
    return {d: ("A" if i < mid else "B") for i, d in enumerate(days_sorted)}


def resolve_cells(search: dict) -> list[dict]:
    live = search["live"]
    by_id = {c["id"]: c for c in search.get("phase_a") or [] if c.get("id")}
    cells: list[dict] = []
    for cid in DIG_IDS:
        if cid == "LIVE":
            cells.append(ab.merge_cell(live, {"id": "LIVE"}))
            continue
        overlay = by_id.get(cid) or FALLBACK_OVERLAYS.get(cid)
        if overlay is None:
            print(f"WARN: missing dig cell {cid}", file=sys.stderr)
            continue
        cells.append(ab.merge_cell(live, overlay))
    return cells


def day_stats(prepared: list[dict], cell: dict) -> dict[str, dict[str, Any]]:
    by_day: dict[str, list[float]] = defaultdict(list)
    for row in prepared:
        ok, _ = ab.gate_keep(cell, row["ind"])
        if not ok:
            continue
        by_day[row["day"]].append(float(row["mtm"]))
    out: dict[str, dict[str, Any]] = {}
    for day, vals in sorted(by_day.items()):
        st = ab._stats(vals)
        out[day] = st
    return out


def source_by_half(prepared: list[dict], cell: dict) -> dict[str, dict[str, int]]:
    out = {
        "A": {"shadow": 0, "ledger": 0, "other": 0},
        "B": {"shadow": 0, "ledger": 0, "other": 0},
    }
    for row in prepared:
        ok, _ = ab.gate_keep(cell, row["ind"])
        if not ok:
            continue
        h = row["half"]
        src = str(row.get("source") or "")
        if src == "shadow_arm":
            out[h]["shadow"] += 1
        elif src == "ledger_refuse":
            out[h]["ledger"] += 1
        else:
            out[h]["other"] += 1
    return out


def live_block_tokens(
    prepared: list[dict],
    live_cell: dict,
    cand_cell: dict,
) -> dict[str, Counter]:
    """Among cand keeps that LIVE blocks: refuse_token counts by half."""
    by_half: dict[str, Counter] = {"A": Counter(), "B": Counter()}
    for row in prepared:
        ok_c, _ = ab.gate_keep(cand_cell, row["ind"])
        if not ok_c:
            continue
        ok_l, why_l = ab.gate_keep(live_cell, row["ind"])
        if ok_l:
            continue
        tok = str(row.get("refuse_token") or "").strip().lower()
        if not tok:
            tok = ab._refuse_token(row.get("arm_why"), row.get("arm_bucket"))
        if not tok:
            tok = f"live_gate:{why_l}"
        by_half[row["half"]][tok] += 1
    return by_half


def score_all(
    cells: list[dict],
    prepared: list[dict],
    *,
    min_n: int,
    live: dict,
) -> list[dict]:
    live_cell = next(c for c in cells if c["id"] == "LIVE")
    live_row = ab.score_cell(
        live_cell, prepared, live_means=None, min_n=min_n, live=live)
    live_means = {
        "all": live_row["mean"],
        "A": (live_row["half_a"] or {}).get("mean"),
        "B": (live_row["half_b"] or {}).get("mean"),
    }
    rows = [live_row]
    for cell in cells:
        if cell["id"] == "LIVE":
            continue
        rows.append(ab.score_cell(
            cell, prepared, live_means=live_means, min_n=min_n, live=live))
    return rows


def ship_gate(row: dict, *, eps: float, min_n: int) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if not row.get("half_ok"):
        reasons.append("half_ok=False")
    lift = row.get("lift_vs_live")
    if lift is None or float(lift) < float(eps):
        reasons.append(f"lift<{eps}")
    if int(row.get("n") or 0) < int(min_n):
        reasons.append(f"n<{min_n}")
    return (len(reasons) == 0), reasons


def write_artifacts(
    *,
    day_from: str,
    day_to: str,
    days_sorted: list[str],
    halves: dict[str, str],
    skip: dict[str, int],
    n_prepared: int,
    rows: list[dict],
    per_day: dict[str, dict[str, dict]],
    src_half: dict[str, dict[str, dict[str, int]]],
    tokens: dict[str, dict[str, Counter]],
    eps: float,
    min_n: int,
) -> tuple[Path, Path]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"half_split_dig_{day_from}_{day_to}"
    md_path = OUT_DIR / f"{stem}.md"
    json_path = OUT_DIR / f"{stem}.json"

    by_id = {r["id"]: r for r in rows}
    live = by_id["LIVE"]
    dir_only = by_id.get("rsi_dir_only_fall_gt10")

    half_a_days = [d for d in days_sorted if halves[d] == "A"]
    half_b_days = [d for d in days_sorted if halves[d] == "B"]

    # Decision / ship winners
    candidates = [r for r in rows if r["id"] != "LIVE"]
    passers: list[dict] = []
    for r in candidates:
        ok, _ = ship_gate(r, eps=eps, min_n=min_n)
        if ok:
            passers.append(r)
    passers.sort(key=lambda r: (
        int(r.get("complexity") or 99),
        -(r.get("lift_vs_live") or -1e9),
    ))
    winner = passers[0] if passers else None

    # Dig paragraph inputs
    day_lifts: list[tuple[str, float | None, float | None]] = []
    if dir_only:
        for d in days_sorted:
            live_m = (per_day.get("LIVE") or {}).get(d, {}).get("mean")
            cand_m = (per_day.get("rsi_dir_only_fall_gt10") or {}).get(d, {}).get("mean")
            lift = None if live_m is None or cand_m is None else cand_m - live_m
            day_lifts.append((d, lift, cand_m))
    tank_b = [
        (d, lift) for d, lift, _ in day_lifts
        if halves.get(d) == "B" and lift is not None and lift < 0
    ]
    tank_b.sort(key=lambda x: x[1])
    help_b = [
        (d, lift) for d, lift, _ in day_lifts
        if halves.get(d) == "B" and lift is not None and lift > 0
    ]
    help_b.sort(key=lambda x: -x[1])

    tok_a = (tokens.get("rsi_dir_only_fall_gt10") or {}).get("A") or Counter()
    tok_b = (tokens.get("rsi_dir_only_fall_gt10") or {}).get("B") or Counter()
    top_tok_b = tok_b.most_common(5)
    top_tok_a = tok_a.most_common(5)

    wed = "2026-09-16"
    wed_line = "Wed 9/16 not in window."
    if wed in days_sorted and dir_only:
        w_live = (per_day.get("LIVE") or {}).get(wed, {})
        w_dir = (per_day.get("rsi_dir_only_fall_gt10") or {}).get(wed, {})
        w_lift = None
        if w_live.get("mean") is not None and w_dir.get("mean") is not None:
            w_lift = float(w_dir["mean"]) - float(w_live["mean"])
        wed_half = halves.get(wed, "?")
        wed_line = (
            f"Wed 9/16 is half **{wed_half}**: LIVE n={w_live.get('n', 0)} "
            f"mean={_fmt_pct(w_live.get('mean'))}; dir-only n={w_dir.get('n', 0)} "
            f"mean={_fmt_pct(w_dir.get('mean'))}; day lift={_fmt_pct(w_lift)}."
        )

    # Top narrower packages to re-AB (by half_ok then lift, else least-bad lift_b)
    ranked = sorted(
        candidates,
        key=lambda r: (
            1 if r.get("half_ok") else 0,
            r.get("lift_vs_live") if r.get("lift_vs_live") is not None else -1e9,
            r.get("lift_b") if r.get("lift_b") is not None else -1e9,
            -int(r.get("complexity") or 0),
        ),
        reverse=True,
    )
    top2 = [r["id"] for r in ranked[:2]]

    tank_days_s = ", ".join(f"{d} ({_fmt_pct(l)})" for d, l in tank_b[:3]) or "none"
    tok_dom = ", ".join(f"{t}×{n}" for t, n in top_tok_b[:3]) or "none"
    paragraph = (
        f"(a) Half-B day lift pain for dir-only: {tank_days_s}. "
        f"(b) Among dir-only keeps LIVE blocks, half-B refuse tokens dominated by "
        f"{tok_dom} (half-A top: "
        + (", ".join(f"{t}×{n}" for t, n in top_tok_a[:3]) or "none")
        + "). "
        f"(c) Top narrower packages to prefer: {', '.join(top2)}. "
        f"{wed_line} "
        + (
            f"**Ship winner:** `{winner['id']}`."
            if winner
            else "**No half-stable RSI package** — branch **TAPE** (late/stale seat "
            "in EXH 40–70), not another full dir-only push."
        )
    )

    lines: list[str] = [
        f"# Entry AB half-split dig — `{day_from}` → `{day_to}`",
        "",
        "Read-only dig. Reuses `entry_arm_ab` prepare/gate_keep/score_cell. "
        "**Does not** write `config/bot_config.json`.",
        "",
        f"Arms prepared: **{n_prepared}**  (skipped: `{skip}`).",
        "",
        "## A0. Half map (unique ET days)",
        "",
        f"- Days sorted: `{days_sorted}`",
        f"- Half A (first {len(half_a_days)}): `{half_a_days}`",
        f"- Half B (second {len(half_b_days)}): `{half_b_days}`",
        f"- mid = max(1, n_days // 2) = **{max(1, len(days_sorted) // 2)}**",
        "",
        "## Half A vs B (harness reprint)",
        "",
        "| id | n | mean | med | half_ok | lift | lift_a | lift_b | n_A | mean_A | n_B | mean_B | complexity |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        ha = r.get("half_a") or {}
        hb = r.get("half_b") or {}
        lines.append(
            f"| {r['id']} | {r.get('n')} | {_fmt(r.get('mean'))} | {_fmt(r.get('median'))} "
            f"| {r.get('half_ok')} | {_fmt(r.get('lift_vs_live'))} | {_fmt(r.get('lift_a'))} "
            f"| {_fmt(r.get('lift_b'))} | {ha.get('n')} | {_fmt(ha.get('mean'))} "
            f"| {hb.get('n')} | {_fmt(hb.get('mean'))} | {r.get('complexity')} |"
        )

    lines += [
        "",
        "## Per day (keeps / mean / lift vs LIVE that day)",
        "",
    ]
    ids = [r["id"] for r in rows]
    header = "| day | half | LIVE n | LIVE mean |"
    for i in ids:
        if i == "LIVE":
            continue
        header += f" | {i} n | mean | lift |"
    lines.append(header)
    n_cols = header.count("|") - 1
    lines.append("|" + "---|" * n_cols)
    for d in days_sorted:
        row_bits = [d, halves[d]]
        live_st = (per_day.get("LIVE") or {}).get(d, {})
        row_bits += [str(live_st.get("n") or 0), _fmt(live_st.get("mean"))]
        live_m = live_st.get("mean")
        for i in ids:
            if i == "LIVE":
                continue
            st = (per_day.get(i) or {}).get(d, {})
            m = st.get("mean")
            lift = None if live_m is None or m is None else float(m) - float(live_m)
            row_bits += [str(st.get("n") or 0), _fmt(m), _fmt(lift)]
        lines.append("| " + " | ".join(row_bits) + " |")

    lines += [
        "",
        "## Shadow vs ledger (keeps per half)",
        "",
        "| id | A shadow | A ledger | B shadow | B ledger |",
        "|---|---:|---:|---:|---:|",
    ]
    for i in ids:
        s = src_half.get(i) or {}
        a = s.get("A") or {}
        b = s.get("B") or {}
        lines.append(
            f"| {i} | {a.get('shadow', 0)} | {a.get('ledger', 0)} "
            f"| {b.get('shadow', 0)} | {b.get('ledger', 0)} |"
        )
    if dir_only:
        s = src_half.get("rsi_dir_only_fall_gt10") or {}
        b = s.get("B") or {}
        bn = int(b.get("shadow", 0)) + int(b.get("ledger", 0))
        if bn and int(b.get("ledger", 0)) / bn >= 0.85:
            lines.append(
                f"\n> Dir-only half B is **ledger-heavy**: "
                f"{b.get('ledger', 0)} ledger / {b.get('shadow', 0)} shadow "
                f"({100.0 * b.get('ledger', 0) / bn:.0f}% ledger)."
            )

    lines += [
        "",
        "## Ledger composition — cand keeps LIVE blocks (by refuse_token × half)",
        "",
    ]
    for cid in ids:
        if cid == "LIVE":
            continue
        ta = (tokens.get(cid) or {}).get("A") or Counter()
        tb = (tokens.get(cid) or {}).get("B") or Counter()
        if not ta and not tb:
            lines.append(f"### `{cid}` — no LIVE-blocked keeps")
            lines.append("")
            continue
        lines.append(f"### `{cid}`")
        lines.append("")
        lines.append("| token | half A | half B |")
        lines.append("|---|---:|---:|")
        all_toks = sorted(set(ta) | set(tb), key=lambda t: -(ta[t] + tb[t]))
        for t in all_toks[:20]:
            lines.append(f"| {t} | {ta[t]} | {tb[t]} |")
        lines.append("")

    lines += [
        "## Wed 9/16 alone",
        "",
        wed_line,
        "",
        "## A4. Decision table",
        "",
        f"Pass gate: `half_ok` + mean lift ≥ ε={eps} + n ≥ min_n={min_n}. "
        "Prefer lowest complexity among passers.",
        "",
        "| id | n | mean | med | half_ok | lift | lift_a | lift_b | complexity | ship? |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        if r["id"] == "LIVE":
            continue
        ok, reasons = ship_gate(r, eps=eps, min_n=min_n)
        flag = "PASS" if ok else "FAIL (" + "; ".join(reasons) + ")"
        lines.append(
            f"| {r['id']} | {r.get('n')} | {_fmt(r.get('mean'))} | {_fmt(r.get('median'))} "
            f"| {r.get('half_ok')} | {_fmt(r.get('lift_vs_live'))} | {_fmt(r.get('lift_a'))} "
            f"| {_fmt(r.get('lift_b'))} | {r.get('complexity')} | {flag} |"
        )

    lines += [
        "",
        "## Dig paragraph (pass criterion)",
        "",
        paragraph,
        "",
        "## Branch",
        "",
    ]
    if winner:
        lines.append(
            f"**SHIP** narrower package `{winner['id']}` "
            f"(complexity={winner.get('complexity')}, "
            f"lift={_fmt(winner.get('lift_vs_live'))}, half_ok=True)."
        )
    else:
        lines.append(
            "**TAPE** — no narrower RSI cell clears half_ok + lift + min_n. "
            "Park full dir-only and RSI kitchen-sink. Fri-prep timing lever: "
            "late/stale seat inside EXH 40–70 (stream-ready / `decision_max_age` "
            "border) — measure with admit/decision ledger age+freshness, not another "
            "RSI AB crown."
        )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    payload = {
        "window": {"from": day_from, "to": day_to},
        "days_sorted": days_sorted,
        "half_a_days": half_a_days,
        "half_b_days": half_b_days,
        "n_prepared": n_prepared,
        "skip": skip,
        "eps": eps,
        "min_n": min_n,
        "rows": rows,
        "per_day": per_day,
        "source_by_half": src_half,
        "live_block_tokens": {
            cid: {"A": dict(c["A"]), "B": dict(c["B"])}
            for cid, c in tokens.items()
        },
        "winner_id": None if winner is None else winner["id"],
        "branch": "SHIP" if winner else "TAPE",
        "dig_paragraph": paragraph,
    }
    json_path.write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return md_path, json_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="day_from", default="2026-09-11")
    ap.add_argument("--to", dest="day_to", default="2026-09-16")
    ap.add_argument("--search", type=Path, default=ab.SEARCH_PATH)
    ap.add_argument("--min-n", type=int, default=None)
    args = ap.parse_args()

    search = ab.load_search(args.search)
    min_n = int(args.min_n if args.min_n is not None else search.get("min_n") or 25)
    eps = float(search.get("epsilon_mtm_pct") or 0.02)
    horizon = float(search.get("horizon_min") or 30.0)
    tl = int(search.get("trend_lookback") or 2)
    rte = float(search.get("rte_threshold") or 20.0)

    day_from = args.day_from
    day_to = args.day_to
    print(f"half_split_dig  {day_from}..{day_to}  min_n={min_n}  eps={eps}")
    print("READ-ONLY — does not write bot_config.")

    cells = resolve_cells(search)
    live = search["live"]

    shadow_raw = ab.load_shadow_arms(day_from, day_to)
    shadow_rows = ab.dedupe_candidates(shadow_raw)
    ledger_rows, ledger_raw = ab.load_ledger_refuses(day_from, day_to)
    ledger_rows = ab.dedupe_candidates(ledger_rows)
    union = ab.dedupe_candidates(shadow_rows + ledger_rows)
    print(
        f"shadow={len(shadow_rows)}  ledger_raw={ledger_raw}→{len(ledger_rows)}  "
        f"union={len(union)}"
    )

    prepared, skip = ab.prepare_candidates(
        union, horizon_min=horizon, tl=tl, rte_threshold=rte)
    print(f"prepared={len(prepared)}  skip={skip}")

    days_sorted = sorted({r["day"] for r in prepared})
    halves = half_map(days_sorted)
    # Sanity: prepared halves must match
    for r in prepared:
        if r["half"] != halves[r["day"]]:
            print("ERROR: half mismatch vs harness map", r["day"], file=sys.stderr)
            return 2

    print(f"days={days_sorted}")
    print(f"half_A={[d for d in days_sorted if halves[d]=='A']}")
    print(f"half_B={[d for d in days_sorted if halves[d]=='B']}")

    rows = score_all(cells, prepared, min_n=min_n, live=live)
    live_cell = next(c for c in cells if c["id"] == "LIVE")
    per_day = {c["id"]: day_stats(prepared, c) for c in cells}
    src_half = {c["id"]: source_by_half(prepared, c) for c in cells}
    tokens = {
        c["id"]: live_block_tokens(prepared, live_cell, c)
        for c in cells if c["id"] != "LIVE"
    }

    # Console reprint vs FAIL run
    for r in rows:
        if r["id"] in ("LIVE", "rsi_dir_only_fall_gt10"):
            print(
                f"{r['id']}: n={r.get('n')} mean={_fmt(r.get('mean'))} "
                f"half_ok={r.get('half_ok')} lift={_fmt(r.get('lift_vs_live'))} "
                f"lift_a={_fmt(r.get('lift_a'))} lift_b={_fmt(r.get('lift_b'))} "
                f"sh={r.get('n_shadow')} led={r.get('n_ledger')}"
            )

    md_path, json_path = write_artifacts(
        day_from=day_from,
        day_to=day_to,
        days_sorted=days_sorted,
        halves=halves,
        skip=skip,
        n_prepared=len(prepared),
        rows=rows,
        per_day=per_day,
        src_half=src_half,
        tokens=tokens,
        eps=eps,
        min_n=min_n,
    )
    print(f"Wrote {md_path}")
    print(f"Wrote {json_path}")
    # Print dig paragraph
    data = json.loads(json_path.read_text(encoding="utf-8"))
    print("\n=== DIG PARAGRAPH ===")
    print(data["dig_paragraph"])
    print(f"\nBRANCH: {data['branch']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
