#!/usr/bin/env python3
"""One seat test. Spec: docs/studies/seat_agent_prereg.json.

Default prints the pinned days and, if the archive is on this machine, whether
each day has a morning list. Counts only. --score computes round-trip returns.
Do not pass --score until a result-skeptic Mode 1 review says READY.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DAYS_A = (
    "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
)
DAYS_B = (
    "2026-10-07", "2026-10-09", "2026-10-12", "2026-10-13",
    "2026-10-14", "2026-10-15", "2026-10-16",
)
SUGGESTION_FILES = (
    "grok_suggestions.json",
    "agy_suggestions.json",
    "claude_suggestions.json",
)
OTHER_BOOK = ("on-", "wk-", "me-", "mir-")
MORNING_LO, MORNING_HI = 8 * 60 + 30, 9 * 60 + 30
ENTRY_LO, ENTRY_HI = 9 * 60 + 30, 15 * 60 + 50
LINK_SEC, SKEW_SEC = 120.0, 2.0
MIN_PAIRED_DAYS, MIN_TRIPS, T_BAR = 6, 20, 1.5
REPORT_RE = re.compile(r"(20\d{6})_(\d{6})")


def day_t(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    mean = statistics.mean(values)
    if len(values) < 2 or statistics.stdev(values) == 0:
        return mean, None
    return mean, mean / (statistics.stdev(values) / math.sqrt(len(values)))


def _minutes(ts: float, day: str) -> int | None:
    stamp = datetime.fromtimestamp(float(ts), ET)
    if stamp.strftime("%Y-%m-%d") != day:
        return None
    return stamp.hour * 60 + stamp.minute


def _report_ts(path: str) -> float | None:
    match = REPORT_RE.search(path or "")
    if not match:
        return None
    try:
        stamp = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return stamp.replace(tzinfo=ET).timestamp()


def publish_ts(data: dict) -> float | None:
    """Research-run clock. last_ok wins. The report path is only a fallback."""
    if data.get("last_ok") is not None:
        try:
            return float(data["last_ok"])
        except (TypeError, ValueError):
            return None
    return _report_ts(str(data.get("last_report_path") or ""))


def _symbols(data: dict) -> list[tuple[str, float | None]]:
    rows = data.get("rows") or data.get("suggestions") or data.get("items") or []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
        if not symbol or not symbol[0].isalpha():
            continue
        try:
            pct = float(row["pct_change"]) if row.get("pct_change") is not None else None
        except (TypeError, ValueError):
            pct = None
        out.append((symbol, pct))
    return out


def morning_list(records: list[dict], day: str) -> tuple[set[str], dict[str, float | None], set[str]]:
    """First qualifying snapshot per file. Returns symbols, pct, files used."""
    first: dict[str, tuple[float, dict]] = {}
    for rec in records:
        name = str(rec.get("file") or "").rsplit("/", 1)[-1]
        if name not in SUGGESTION_FILES:
            continue
        data = rec.get("data") if isinstance(rec.get("data"), dict) else None
        if not data:
            continue
        try:
            snap_ts = float(rec.get("ts"))
        except (TypeError, ValueError):
            continue
        clock = publish_ts(data)
        if clock is None:
            continue
        minute = _minutes(clock, day)
        if minute is None or minute < MORNING_LO or minute >= MORNING_HI:
            continue
        held = first.get(name)
        if held is None or snap_ts < held[0]:
            first[name] = (snap_ts, data)
    symbols: set[str] = set()
    pct: dict[str, float | None] = {}
    for name in SUGGESTION_FILES:
        held = first.get(name)
        if held is None:
            continue
        for symbol, change in _symbols(held[1]):
            symbols.add(symbol)
            pct.setdefault(symbol, change)
    return symbols, pct, set(first)


def movers_first(records: list[dict], day: str) -> dict[str, float]:
    """Earliest same-day movers data.ts for each symbol."""
    first: dict[str, float] = {}
    for rec in records:
        name = str(rec.get("file") or "").rsplit("/", 1)[-1]
        if name != "movers_stocks.json":
            continue
        data = rec.get("data") if isinstance(rec.get("data"), dict) else None
        if not data:
            continue
        try:
            published = float(data.get("ts"))
        except (TypeError, ValueError):
            continue
        if _minutes(published, day) is None:
            continue
        for symbol, _pct in _symbols(data):
            if symbol not in first or published < first[symbol]:
                first[symbol] = published
    return first


def _side(value) -> str:
    text = str(value or "").strip().lower()
    if "." in text and " " not in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _when(raw, fallback: float | None) -> float | None:
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            stamp = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return fallback
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.timestamp()
    return fallback


def _qty(raw) -> float:
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return qty if qty > 0 else 1.0


def closed_trips(events: list[dict]) -> tuple[list[dict], int]:
    """FIFO round trips from fill events. Returns trips and buys left open."""
    orders: dict[str, dict] = {}
    loose: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("event") not in (None, "fill"):
            continue
        side = _side(ev.get("side"))
        if side not in ("buy", "sell"):
            continue
        try:
            price = float(ev.get("filled_avg_price"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        when = _when(ev.get("filled_at"), _when(ev.get("ts"), None))
        if when is None:
            continue
        symbol = str(ev.get("symbol") or "").upper().strip()
        if not symbol:
            continue
        row = {
            "symbol": symbol,
            "side": side,
            "px": price,
            "ts": when,
            "qty": _qty(ev.get("filled_qty") if ev.get("filled_qty") is not None else ev.get("qty")),
            "cid": str(ev.get("client_order_id") or ""),
        }
        oid = str(ev.get("order_id") or "").strip()
        if not oid:
            loose.append(row)
            continue
        held = orders.get(oid)
        if held is None or when >= held["ts"]:
            orders[oid] = row
    rows = list(orders.values()) + loose
    rows.sort(key=lambda r: (r["ts"], 0 if r["side"] == "buy" else 1))
    lots: dict[str, list[dict]] = defaultdict(list)
    trips = []
    for row in rows:
        if row["side"] == "buy":
            lots[row["symbol"]].append(dict(row))
            continue
        left = row["qty"]
        book = lots[row["symbol"]]
        while left > 1e-9 and book:
            lot = book[0]
            take = min(lot["qty"], left)
            trips.append({
                "symbol": row["symbol"],
                "entry_ts": lot["ts"],
                "entry_px": lot["px"],
                "exit_px": row["px"],
                "cid": lot["cid"],
                "bp": (row["px"] / lot["px"] - 1.0) * 1e4,
            })
            lot["qty"] -= take
            left -= take
            if lot["qty"] <= 1e-9:
                book.pop(0)
    open_buys = sum(len(book) for book in lots.values())
    return trips, open_buys


def in_entry_window(ts: float, day: str) -> bool:
    minute = _minutes(ts, day)
    return minute is not None and ENTRY_LO <= minute < ENTRY_HI


def other_book(cid: str) -> bool:
    text = cid or ""
    return text.startswith(OTHER_BOOK)


def day_book(events: list[dict]) -> list[dict]:
    """Drop other-book fills before any pairing."""
    return [ev for ev in events if isinstance(ev, dict) and not other_book(str(ev.get("client_order_id") or ""))]


def linked(symbol: str, entry_ts: float, oks: list[tuple[str, float]]) -> bool:
    for name, ts in oks:
        if name == symbol and entry_ts - LINK_SEC <= ts <= entry_ts + SKEW_SEC:
            return True
    return False


def seat_of(symbol: str, entry_ts: float, agent: set[str], movers: dict[str, float]) -> tuple[str | None, bool]:
    on_movers = symbol in movers and movers[symbol] <= entry_ts + 1e-6
    if symbol in agent:
        return "agent", on_movers
    if on_movers:
        return "movers", False
    return None, False


def entry_oks(events: list[dict]) -> list[tuple[str, float]]:
    out = []
    for ev in events:
        if not isinstance(ev, dict) or ev.get("event_kind") != "entry_ok":
            continue
        symbol = str(ev.get("symbol") or "").upper().strip()
        try:
            ts = float(ev.get("ts"))
        except (TypeError, ValueError):
            continue
        if symbol:
            out.append((symbol, ts))
    return out


def _iter_snapshots(path: Path):
    if not path.exists():
        return
    try:
        with gzip.open(path, "rt", errors="replace") as handle:
            for line in handle:
                if "suggestions.json" not in line and "movers_stocks.json" not in line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except (EOFError, OSError):
        return


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def analyze_day(folder: Path, day: str) -> dict:
    """Classify one archived day. bp is computed here; callers that only need
    coverage should use morning_list on the records instead."""
    records = list(_iter_snapshots(folder / "state_snapshots.jsonl.gz"))
    agent, pct, files = morning_list(records, day)
    movers = movers_first(records, day)
    fills = _read_jsonl(folder / "fills.jsonl")
    n_other = sum(
        1 for ev in fills
        if isinstance(ev, dict) and other_book(str(ev.get("client_order_id") or ""))
    )
    raw, open_buys = closed_trips(day_book(fills))
    oks = entry_oks(_read_jsonl(folder / "decision_ledger.jsonl"))
    dropped = {"other_book": n_other, "outside_window": 0, "no_entry_ok": 0, "no_seat": 0}
    kept = []
    for trip in raw:
        if not in_entry_window(trip["entry_ts"], day):
            dropped["outside_window"] += 1
            continue
        if not linked(trip["symbol"], trip["entry_ts"], oks):
            dropped["no_entry_ok"] += 1
            continue
        arm, overlap = seat_of(trip["symbol"], trip["entry_ts"], agent, movers)
        if arm is None:
            dropped["no_seat"] += 1
            continue
        kept.append({
            "day": day,
            "arm": arm,
            "bp": trip["bp"],
            "symbol": trip["symbol"],
            "overlap": overlap,
            "pct": pct.get(trip["symbol"]),
        })
    return {
        "day": day,
        "n_agent_names": len(agent),
        "n_files": len(files),
        "trips": kept,
        "dropped": dropped,
        "open_buys": open_buys,
    }


def half_stats(trips: list[dict], days: tuple[str, ...]) -> dict:
    by_arm: dict[str, dict[str, list[float]]] = {"agent": defaultdict(list), "movers": defaultdict(list)}
    for trip in trips:
        if trip["day"] in days and trip["arm"] in by_arm:
            by_arm[trip["arm"]][trip["day"]].append(trip["bp"])
    diffs = []
    agent_means = []
    paired = []
    for day in days:
        if not by_arm["agent"][day] or not by_arm["movers"][day]:
            continue
        agent_mean = statistics.mean(by_arm["agent"][day])
        movers_mean = statistics.mean(by_arm["movers"][day])
        paired.append(day)
        agent_means.append(agent_mean)
        diffs.append(agent_mean - movers_mean)
    paired_set = set(paired)
    diff_mean, t_value = day_t(diffs)
    agent_mean = statistics.mean(agent_means) if agent_means else None
    return {
        "paired_days": len(paired),
        "agent_trips": sum(len(by_arm["agent"][d]) for d in paired_set),
        "movers_trips": sum(len(by_arm["movers"][d]) for d in paired_set),
        "unpaired_agent_trips": sum(len(by_arm["agent"][d]) for d in days if d not in paired_set),
        "unpaired_movers_trips": sum(len(by_arm["movers"][d]) for d in days if d not in paired_set),
        "agent_mean": agent_mean,
        "diff_mean": diff_mean,
        "t": t_value,
    }


def _powered(stats: dict) -> bool:
    return (
        stats["paired_days"] >= MIN_PAIRED_DAYS
        and stats["agent_trips"] >= MIN_TRIPS
        and stats["movers_trips"] >= MIN_TRIPS
    )


def _right(stats: dict) -> bool:
    return (
        stats["diff_mean"] is not None
        and stats["diff_mean"] > 0
        and stats["agent_mean"] is not None
        and stats["agent_mean"] > 0
    )


def grade(half_a: dict, half_b: dict, combined: dict) -> str:
    a_pow, b_pow = _powered(half_a), _powered(half_b)
    if a_pow and b_pow:
        if _right(half_a) and _right(half_b) and _right(combined):
            if combined["t"] is None:
                return "UNDERPOWERED"
            if combined["t"] >= T_BAR:
                return "PROMISING"
        return "FAIL"
    if a_pow and not _right(half_a):
        return "FAIL"
    if b_pow and not _right(half_b):
        return "FAIL"
    return "UNDERPOWERED"


def already_up(trips: list[dict], floor: float = 10.0) -> tuple[int, int, int]:
    """Agent trips with pct >= floor, trips with a pct, trips with no pct."""
    known = [t for t in trips if t["arm"] == "agent" and t.get("pct") is not None]
    missing = sum(1 for t in trips if t["arm"] == "agent" and t.get("pct") is None)
    return sum(1 for t in known if t["pct"] >= floor), len(known), missing


def _fmt(value) -> str:
    if value is None:
        return "na"
    return f"{value:.2f}"


def _line(name: str, stats: dict) -> str:
    return (
        f"{name} paired {stats['paired_days']} "
        f"agent_trips {stats['agent_trips']} movers_trips {stats['movers_trips']} "
        f"agent_mean_bp {_fmt(stats['agent_mean'])} diff_bp {_fmt(stats['diff_mean'])} "
        f"t {_fmt(stats['t'])}"
    )


def score(archive: Path) -> str:
    days = DAYS_A + DAYS_B
    analyzed = [analyze_day(archive / day, day) for day in days]
    trips = [t for day in analyzed for t in day["trips"]]
    half_a = half_stats(trips, DAYS_A)
    half_b = half_stats(trips, DAYS_B)
    combined = half_stats(trips, days)
    verdict = grade(half_a, half_b, combined)
    up, known, missing_pct = already_up(trips)
    overlap = sum(1 for t in trips if t["arm"] == "agent" and t["overlap"])
    one_model = sum(1 for day in analyzed if day["n_files"] == 1)
    no_list = sum(1 for day in analyzed if day["n_agent_names"] == 0)
    dropped = {key: 0 for key in ("other_book", "outside_window", "no_entry_ok", "no_seat")}
    for day in analyzed:
        for key in dropped:
            dropped[key] += day["dropped"][key]
    extra = None if combined["agent_mean"] is None else combined["agent_mean"] - 3.0
    open_buys = sum(day["open_buys"] for day in analyzed)
    lines = [
        "seat_agent",
        _line("half A", half_a),
        _line("half B", half_b),
        _line("combined", combined),
        f"grade {verdict}",
        (
            f"info no_morning_list {no_list} one_model {one_model} "
            f"overlap_agent_trips {overlap} already_up {up}/{known} pct_missing {missing_pct} "
            f"agent_mean_minus_3bp {_fmt(extra)} "
            f"unpaired_agent {combined['unpaired_agent_trips']} "
            f"unpaired_movers {combined['unpaired_movers_trips']} open_buys {open_buys} "
            f"dropped other_book {dropped['other_book']} outside_window {dropped['outside_window']} "
            f"no_entry_ok {dropped['no_entry_ok']} no_seat {dropped['no_seat']}"
        ),
    ]
    return "\n".join(lines)


def coverage(archive: Path) -> str:
    lines = ["seat_agent coverage (no returns)", f"A {','.join(DAYS_A)}", f"B {','.join(DAYS_B)}"]
    if not archive.is_dir():
        lines.append(f"archive missing {archive}")
        return "\n".join(lines)
    for day in DAYS_A + DAYS_B:
        folder = archive / day
        gz = folder / "state_snapshots.jsonl.gz"
        if not gz.exists():
            lines.append(f"{day} no_snapshot")
            continue
        symbols, _pct, files = morning_list(list(_iter_snapshots(gz)), day)
        lines.append(f"{day} morning_names {len(symbols)} files {len(files)}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="One seat test. See docs/studies/seat_agent_prereg.json.")
    parser.add_argument("--score", action="store_true", help="compute round-trip returns")
    parser.add_argument("--archive", default=os.getenv("SESSION_SNAPSHOT_DIR") or str(Path.home() / "session_snapshots"))
    args = parser.parse_args()
    archive = Path(args.archive)
    print(score(archive) if args.score else coverage(archive))


if __name__ == "__main__":
    main()
