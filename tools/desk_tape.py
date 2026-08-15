#!/usr/bin/env python3
"""desk_tape.py — freeze live logs into a reusable tape other tools can share.

``ai_reports/*.jsonl`` are append-only live files. They span sessions, they
are gitignored, and a rotation on the mini makes every number unfalsifiable.
That is why ``tests/fixtures/sim_2026-08-11`` exists, and why every sim was
re-opening the live files itself.

A tape is a directory any tool that respects ``AI_REPORT_DIR`` can consume:

    manifest.json
    shadow.jsonl
    outcomes.jsonl
    rejects.jsonl
    trades.jsonl              (if the live tree has it)
    position_shadow.jsonl     (if present)
    signal_shadow.jsonl       (if present)

Pack once after the close; then replay_ab, the one-off sims, and any new
test all read the same frozen rows.

USAGE
    venv/bin/python tools/desk_tape.py pack --day 2026-08-11
    venv/bin/python tools/desk_tape.py pack --days 10
    venv/bin/python tools/desk_tape.py list
    venv/bin/python tools/desk_tape.py show ai_reports/tapes/2026-08-11
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from ai_paths import resolve_report_dir  # noqa: E402

# name, timestamp keys used for day membership, required?
SOURCES: list[tuple[str, tuple[str, ...], bool]] = [
    ("shadow.jsonl", ("ts",), True),
    ("outcomes.jsonl", ("exit_time", "ts", "entry_time"), True),
    ("rejects.jsonl", ("ts",), True),
    ("trades.jsonl", ("ts",), False),
    ("position_shadow.jsonl", ("ts",), False),
    ("signal_shadow.jsonl", ("ts",), False),
]


def day_of(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def load_jsonl_day(path: Path, day: str, ts_keys: tuple[str, ...]) -> list[dict]:
    """A row belongs to *day* if any timestamp key falls on that day."""
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
        if not isinstance(row, dict):
            continue
        for k in ts_keys:
            if day_of(row.get(k)) == day:
                out.append(row)
                break
    return out


def available_days(report_dir: Path | None = None) -> list[str]:
    root = report_dir or resolve_report_dir()
    days: set[str] = set()
    for name, keys, _req in SOURCES[:3]:
        path = root / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            for k in keys:
                d = day_of(row.get(k))
                if d:
                    days.add(d)
                    break
    return sorted(days)


def _write_jsonl(path: Path, rows: list[dict]) -> int:
    path.write_text(
        "".join(json.dumps(r, default=str) + "\n" for r in rows),
        encoding="utf-8",
    )
    return len(rows)


def tape_label(days: list[str]) -> str:
    if not days:
        return "empty"
    if len(days) == 1:
        return days[0]
    return f"{days[0]}_{days[-1]}"


def pack(
    days: list[str],
    *,
    dest: Path | None = None,
    report_dir: Path | None = None,
    with_events: bool = False,
) -> dict[str, Any]:
    """Copy the requested days out of the live tree into a tape directory."""
    src = report_dir or resolve_report_dir()
    label = tape_label(days)
    out = dest or (resolve_report_dir() / "tapes" / label)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}
    for name, keys, required in SOURCES:
        rows: list[dict] = []
        seen: set[tuple] = set()
        for d in days:
            for r in load_jsonl_day(src / name, d, keys):
                key = (r.get("ts"), r.get("symbol"), r.get("entry_time"),
                       r.get("exit_time"), r.get("close_reason"), r.get("price"))
                if key in seen:
                    continue
                seen.add(key)
                rows.append(r)
        if not rows and not required:
            files[name] = {"rows": 0, "present": False}
            continue
        n = _write_jsonl(out / name, rows)
        files[name] = {"rows": n, "present": True}

    if with_events:
        ev_rows: list[dict] = []
        for d in days:
            ev_rows.extend(load_jsonl_day(src / "events.jsonl", d, ("ts",)))
        files["events.jsonl"] = {
            "rows": _write_jsonl(out / "events.jsonl", ev_rows),
            "present": True,
        }

    manifest = {
        "label": label,
        "days": list(days),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": str(src),
        "files": files,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(out), "manifest": manifest}


def load(tape_dir: Path) -> dict[str, Any]:
    """Read a packed tape into memory (same keys replay_ab.load_tape uses)."""
    tape_dir = Path(tape_dir)
    man_path = tape_dir / "manifest.json"
    manifest = json.loads(man_path.read_text(encoding="utf-8")) if man_path.exists() else {}
    days = list(manifest.get("days") or [])

    def _read(name: str) -> list[dict]:
        p = tape_dir / name
        if not p.exists():
            return []
        rows: list[dict] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict):
                rows.append(r)
        return rows

    return {
        "days": days,
        "shadow": _read("shadow.jsonl"),
        "outcomes": _read("outcomes.jsonl"),
        "rejects": _read("rejects.jsonl"),
        "trades": _read("trades.jsonl"),
        "position_shadow": _read("position_shadow.jsonl"),
        "signal_shadow": _read("signal_shadow.jsonl"),
        "available_days": days,
        "manifest": manifest,
        "paths": {"tape": str(tape_dir)},
    }


def list_tapes(root: Path | None = None) -> list[dict[str, Any]]:
    base = root or (resolve_report_dir() / "tapes")
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(base.iterdir()):
        man = child / "manifest.json"
        if not man.exists():
            continue
        try:
            data = json.loads(man.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        out.append({
            "path": str(child),
            "label": data.get("label") or child.name,
            "days": data.get("days") or [],
            "files": {
                k: v.get("rows") for k, v in (data.get("files") or {}).items()
                if isinstance(v, dict)
            },
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_pack = sub.add_parser("pack", help="freeze days from the live report dir")
    p_pack.add_argument("--day", help="single YYYY-MM-DD")
    p_pack.add_argument("--days", type=int, default=None, help="last N days")
    p_pack.add_argument("--dest", default=None, help="output directory")
    p_pack.add_argument("--with-events", action="store_true")
    p_pack.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="list packed tapes")
    p_list.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="print a tape manifest")
    p_show.add_argument("tape")
    p_show.add_argument("--json", action="store_true")

    args = ap.parse_args()
    if args.cmd == "pack":
        have = available_days()
        if args.day:
            days = [args.day]
        elif args.days:
            days = have[-args.days:] if args.days > 0 else have
        else:
            days = have[-1:] if have else []
        if not days:
            print("no days found in the live report dir", file=sys.stderr)
            return 1
        dest = Path(args.dest) if args.dest else None
        result = pack(days, dest=dest, with_events=args.with_events)
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            man = result["manifest"]
            print(f"packed {result['path']}")
            print(f"  days: {', '.join(man['days'])}")
            for name, info in (man.get("files") or {}).items():
                if info.get("present"):
                    print(f"  {name}: {info.get('rows')} rows")
        return 0

    if args.cmd == "list":
        rows = list_tapes()
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("no tapes under ai_reports/tapes/")
            return 0
        for r in rows:
            print(f"{r['label']:24} days={len(r['days'])}  {r['path']}")
        return 0

    tape = load(Path(args.tape))
    if args.json:
        print(json.dumps({
            "manifest": tape.get("manifest"),
            "n_shadow": len(tape["shadow"]),
            "n_outcomes": len(tape["outcomes"]),
            "n_rejects": len(tape["rejects"]),
        }, indent=2, default=str))
        return 0
    man = tape.get("manifest") or {}
    print(f"tape {tape['paths']['tape']}")
    print(f"  days: {', '.join(tape['days']) or '(none)'}")
    print(f"  shadow={len(tape['shadow'])} outcomes={len(tape['outcomes'])} "
          f"rejects={len(tape['rejects'])}")
    if man.get("created_at"):
        print(f"  packed: {man['created_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
