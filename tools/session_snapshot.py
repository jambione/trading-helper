#!/usr/bin/env python3
"""session_snapshot — record a trading session so it can be replayed off hours.

Started by ``./trading start`` with the rest of the stack (so launchd owns it,
never an ssh session). Weekdays 08:00-16:05 ET it writes every change to the
desk's state files, plus the dashboard's /api/state, to

    ~/session_snapshots/YYYY-MM-DD/state_snapshots.jsonl.gz

as records ``{ts, file, mtime, sha, data}`` (the format tools/rehearse_snap.py
reads). At 16:05 it hard-links the day's ledgers and the session recorder's
streams into the same folder and writes ``DONE``, so one directory holds
everything a replay needs.

What each source gives a replay:
  movers/trending/research boards   who the sources nominated, when
  signal_state.json                 the engine's %R, prices and their clocks
  api/state                         what the desk read: tickers (price, age,
                                    pct, rvol), Trader Bro, Discord, positions
  entry_watch_state / admit_funnel  the book the live code actually held
  bot_config.json                   the knobs in force

Files are polled every 2 s and kept only when their bytes change; /api/state
every 3 s. Writes are buffered and appended as gzip members every 10 s, so a
kill loses at most 10 s and the file stays readable.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ET = ZoneInfo("America/New_York")
OUT_BASE = Path(os.getenv("SESSION_SNAPSHOT_DIR") or Path.home() / "session_snapshots")
START_MIN, END_MIN = 8 * 60, 16 * 60 + 5
FILE_POLL_SEC, API_POLL_SEC, FLUSH_SEC = 2.0, 3.0, 10.0

FILES = (
    "signal_state.json",
    "trending_stocks.json",
    "movers_stocks.json",
    "ai_reports/entry_watch_state.json",
    "ai_reports/admit_funnel.json",
    "ai_reports/phase_b_book.json",
    "ai_reports/positions_state.json",
    "ai_positions_state.json",
    "claude_positions_state.json",
    "transcription/wb_watchlist.json",
    "config/bot_config.json",
    # Research boards (research_candidate_rows reads these).
    "agy_suggestions.json",
    "claude_suggestions.json",
    "suggestions.json",
    "grok_suggestions.json",
    "seed_rank_gx.json",
    "seed_rank_ax.json",
)
# Per-day ledgers under ai_reports/<name>/YYYY-MM-DD.jsonl.
LEDGERS = ("admit_ledger", "decision_ledger", "proposal_ledger", "fills",
           "phase_b_ledger", "premarket_scan", "book_server_shadow")
# /api/state parts a replay does not need, or that are recorded elsewhere.
API_DROP = ("news", "swing", "rs", "ai_token_metrics", "config", "version",
            "market_sentiment", "tradingview")

_stop = False


def _log(msg: str) -> None:
    print(f"{datetime.now(ET):%H:%M:%S} [SNAPSHOT] {msg}", flush=True)


def _on_term(_sig, _frm) -> None:
    global _stop
    _stop = True


class DayWriter:
    """Buffers records for one ET day and appends them as gzip members."""

    def __init__(self, day: str):
        self.day = day
        self.dir = OUT_BASE / day
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state_snapshots.jsonl.gz"
        self.buf: list[str] = []
        self.last_flush = time.monotonic()
        self.n = 0

    def add(self, rec: dict) -> None:
        self.buf.append(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        self.n += 1

    def flush(self, force: bool = False) -> None:
        if not self.buf:
            return
        if not force and time.monotonic() - self.last_flush < FLUSH_SEC:
            return
        payload = gzip.compress("".join(self.buf).encode("utf-8"))
        with open(self.path, "ab") as f:
            f.write(payload)
        self.buf = []
        self.last_flush = time.monotonic()


def _sha(raw: bytes) -> str:
    return hashlib.sha1(raw).hexdigest()[:16]


def _api_client():
    import desk_auth
    return desk_auth.for_process(
        "session_snapshot", ROOT,
        default_url=(os.getenv("DASHBOARD_URL") or "https://trading.jbrasfield.com").rstrip("/"),
        user_agent="trading-helper-snapshot/1.0", log_prefix="[snapshot/auth]",
        timeout=4.0)


def _trim_api(data: dict) -> dict:
    out = {k: v for k, v in data.items() if k not in API_DROP}
    rows = out.get("tickers")
    if isinstance(rows, list):
        # signal_proximity duplicates signal_state.json, recorded on its own.
        out["tickers"] = [
            {k: v for k, v in r.items() if k != "signal_proximity"}
            if isinstance(r, dict) else r for r in rows]
    return out


def finish_day(w: DayWriter) -> None:
    """Link the day's ledgers and recorder streams in, then mark it DONE."""
    w.flush(force=True)
    linked = []
    sources = [(ROOT / "ai_reports" / name / f"{w.day}.jsonl", f"{name}.jsonl")
               for name in LEDGERS]
    rec_dir = ROOT / "ai_reports" / "sessions" / w.day
    if rec_dir.is_dir():
        sources += [(p, f"recorder_{p.name}") for p in sorted(rec_dir.iterdir())]
    for src, name in sources:
        dst = w.dir / name
        if not src.is_file() or dst.exists():
            continue
        try:
            os.link(src, dst)
        except OSError:
            try:
                dst.write_bytes(src.read_bytes())
            except OSError as e:
                _log(f"could not archive {src.name}: {e}")
                continue
        linked.append(name)
    (w.dir / "DONE").write_text(f"done {w.n} records; linked {', '.join(linked)}\n")
    _log(f"{w.day} DONE: {w.n} records, linked {len(linked)} files")


def main() -> int:
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    api = _api_client()
    seen: dict[str, tuple[float, str]] = {}
    api_sha = ""
    api_errors = 0
    writer: DayWriter | None = None
    last_file = last_api = 0.0
    _log(f"started; writing under {OUT_BASE}")
    while not _stop:
        now = time.time()
        et = datetime.fromtimestamp(now, ET)
        day = et.strftime("%Y-%m-%d")
        mins = et.hour * 60 + et.minute
        active = et.weekday() < 5 and START_MIN <= mins < END_MIN

        if writer is not None and (writer.day != day or mins >= END_MIN):
            if not (writer.dir / "DONE").exists():
                finish_day(writer)
            writer = None
        if not active:
            # Down at 16:05 (restart, crash)? Close the day out on the way up.
            d = OUT_BASE / day
            if (et.weekday() < 5 and mins >= END_MIN and d.is_dir()
                    and not (d / "DONE").exists()):
                finish_day(DayWriter(day))
            time.sleep(15)
            continue
        if writer is None:
            if (OUT_BASE / day / "DONE").exists():
                time.sleep(15)
                continue
            writer = DayWriter(day)
            seen.clear()
            api_sha = ""
            _log(f"recording {day} -> {writer.path}")

        if now - last_file >= FILE_POLL_SEC:
            last_file = now
            for rel in FILES:
                p = ROOT / rel
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                prev = seen.get(rel)
                if prev and prev[0] == mt:
                    continue
                try:
                    raw = p.read_bytes()
                    sha = _sha(raw)
                    if prev and prev[1] == sha:
                        seen[rel] = (mt, sha)
                        continue
                    data = json.loads(raw)
                except (OSError, ValueError):
                    continue  # mid-write; the next poll gets it
                seen[rel] = (mt, sha)
                writer.add({"ts": now, "file": rel, "mtime": mt, "sha": sha, "data": data})

        if now - last_api >= API_POLL_SEC:
            last_api = now
            try:
                with api.urlopen(f"{api.load_creds()[0]}/api/state") as resp:
                    raw = resp.read()
                data = _trim_api(json.loads(raw))
                body = json.dumps(data, sort_keys=True, default=str).encode("utf-8")
                sha = _sha(body)
                if sha != api_sha:
                    api_sha = sha
                    writer.add({"ts": now, "file": "api/state", "mtime": now,
                                "sha": sha, "data": data})
                api_errors = 0
            except Exception as e:  # noqa: BLE001
                api_errors += 1
                if api_errors in (1, 10) or api_errors % 100 == 0:
                    _log(f"/api/state failed ({api_errors}x): {str(e)[:120]}")

        writer.flush()
        time.sleep(0.5)

    if writer is not None:
        writer.flush(force=True)
    _log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
