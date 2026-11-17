"""game.py — "Call the P&L": players predict the desk's day P&L at noon and at the close.

Entertainment only: no entry fee, no prizes, no money in or out. The desk trades exactly as it
always does; this module only reads the number it already publishes.

Rules (v1, operator 2026-11-17):
  * The number is the paper account's day P&L in dollars (Alpaca equity - last_equity), as the desk
    publishes it in ai_positions_state.json ("account.day_pl"). Players watch that same number live,
    so the display and the scoring can never disagree.
  * Two rounds per trading day:  noon  - guesses lock 09:30, answer = the first desk snapshot at/after 12:00
                                 close - guesses lock 12:00, answer = the first desk snapshot at/after 16:00
  * One guess per player per round, editable until the lock. The server clock is the only clock.
  * Closest guess wins; a tie goes to whoever submitted first (an edit keeps the first submit time).
  * No round on weekends, NYSE holidays and half days. A round whose snapshot does not arrive within
    SNAPSHOT_GRACE_SEC of the cutoff (desk down, stale book) is VOID - never guessed at.
  * Other players' guesses stay hidden until the round locks (no sniping the leader).

Storage: one SQLite file (ai_reports/game/game.sqlite) + an append-only snapshot audit log.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
GAME_DIR = ROOT / "ai_reports" / "game"
DB_PATH = GAME_DIR / "game.sqlite"
SNAP_LOG = GAME_DIR / "snapshots.jsonl"

# slot -> (lock hh:mm, cutoff hh:mm), ET
SLOTS: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "noon": ((9, 30), (12, 0)),
    "close": ((12, 0), (16, 0)),
}
SLOT_LABEL = {"noon": "Noon", "close": "Close"}
SNAPSHOT_GRACE_SEC = 180.0      # the desk book refreshes ~every 20 s; 3 min without one = void
MAX_ABS_GUESS = 100_000.0

# NYSE full closures and 13:00 early closes. Upkeep: add each new year (published by NYSE a year+ ahead).
HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03",
    "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18", "2027-07-05",
    "2027-09-06", "2027-11-25", "2027-12-24",
}
HALF_DAYS = {"2026-11-27", "2026-12-24", "2027-11-26"}


# ---------------------------------------------------------------- pure rules
def et_now() -> datetime:
    return datetime.now(ET)


def is_game_day(day: str) -> bool:
    d = date.fromisoformat(day)
    return d.weekday() < 5 and day not in HOLIDAYS and day not in HALF_DAYS


def round_times(day: str, slot: str) -> tuple[datetime, datetime]:
    (lh, lm), (ch, cm) = SLOTS[slot]
    d = date.fromisoformat(day)
    return (datetime(d.year, d.month, d.day, lh, lm, tzinfo=ET), datetime(d.year, d.month, d.day, ch, cm, tzinfo=ET))


def phase(day: str, slot: str, now: datetime, status: str | None = None) -> str:
    """off (no game that day) | open (taking guesses) | locked (waiting for the cutoff) |
    scoring (cutoff passed, snapshot pending) | settled | void."""
    if status in ("settled", "void"):
        return status
    if not is_game_day(day):
        return "off"
    lock, cut = round_times(day, slot)
    if now < lock:
        return "open"
    if now < cut:
        return "locked"
    return "scoring"


def clean_value(raw) -> float:
    v = float(raw)
    if v != v or abs(v) > MAX_ABS_GUESS:          # NaN or absurd
        raise ValueError(f"guess must be a dollar amount within ±{MAX_ABS_GUESS:,.0f}")
    return round(v, 2)


def rank(preds: list[dict], actual: float) -> list[dict]:
    """preds: [{username, value, submitted_ts, ...}] -> same dicts + abs_err + rank (1 = winner).
    Closest wins; ties go to the earlier submit; ranks are strict (no shared places)."""
    out = [dict(p, abs_err=round(abs(float(p["value"]) - float(actual)), 2)) for p in preds]
    out.sort(key=lambda p: (p["abs_err"], p["submitted_ts"], p["username"]))
    for i, p in enumerate(out, 1):
        p["rank"] = i
    return out


def pick_snapshot(payload: dict | None, cutoff_ts: float) -> dict | None:
    """The answer for a cutoff from the desk book payload: usable only when the book was written at or after
    the cutoff and within the grace window, and carries a numeric account day P&L. A price and its clock are
    one event: the book's own `updated` stamp is the snapshot time."""
    if not isinstance(payload, dict):
        return None
    try:
        upd = float(payload.get("updated") or 0)
    except (TypeError, ValueError):
        return None
    if not (cutoff_ts <= upd <= cutoff_ts + SNAPSHOT_GRACE_SEC):
        return None
    acct = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    pl = acct.get("day_pl")
    if not isinstance(pl, (int, float)) or pl != pl:
        return None
    return {"day_pl": round(float(pl), 2), "updated": upd, "equity": acct.get("equity"),
            "last_equity": acct.get("last_equity"), "mode": payload.get("mode")}


def live_pl(payload: dict | None, now_ts: float, max_age: float = 120.0) -> dict:
    """Live number for the page: value + age; value None when the book is stale (never show an old number as live)."""
    if not isinstance(payload, dict):
        return {"value": None, "age_sec": None}
    try:
        upd = float(payload.get("updated") or 0)
    except (TypeError, ValueError):
        upd = 0.0
    acct = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    pl = acct.get("day_pl")
    age = now_ts - upd if upd else None
    ok = isinstance(pl, (int, float)) and age is not None and age <= max_age
    return {"value": round(float(pl), 2) if ok else None, "age_sec": None if age is None else round(age, 1)}


# ---------------------------------------------------------------- store
class Store:
    def __init__(self, path: Path | str = DB_PATH, snap_log: Path | str | None = SNAP_LOG):
        self.path = Path(path)
        self.snap_log = Path(snap_log) if snap_log else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS rounds(
                day TEXT, slot TEXT, status TEXT, actual_pl REAL, snap_ts REAL, note TEXT, settled_ts REAL,
                PRIMARY KEY(day, slot));
            CREATE TABLE IF NOT EXISTS predictions(
                day TEXT, slot TEXT, username TEXT, display_name TEXT, value REAL,
                submitted_ts REAL, updated_ts REAL, PRIMARY KEY(day, slot, username));
            """)

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    # -- rounds
    def round_row(self, day: str, slot: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM rounds WHERE day=? AND slot=?", (day, slot)).fetchone()
        return dict(r) if r else None

    def round_status(self, day: str, slot: str, now: datetime) -> dict:
        row = self.round_row(day, slot) or {}
        lock, cut = round_times(day, slot)
        return {"slot": slot, "label": SLOT_LABEL[slot], "day": day,
                "phase": phase(day, slot, now, row.get("status")),
                "lock_ts": lock.timestamp(), "cutoff_ts": cut.timestamp(),
                "actual_pl": row.get("actual_pl"), "snap_ts": row.get("snap_ts"), "note": row.get("note") or "",
                "n_players": self.count(day, slot)}

    def _finish(self, day, slot, status, actual, snap_ts, note, now_ts):
        with self._lock, self._conn() as c:
            c.execute("INSERT OR REPLACE INTO rounds(day, slot, status, actual_pl, snap_ts, note, settled_ts) "
                      "VALUES(?,?,?,?,?,?,?)", (day, slot, status, actual, snap_ts, note, now_ts))
        if self.snap_log:
            with open(self.snap_log, "a") as f:
                f.write(json.dumps({"day": day, "slot": slot, "status": status, "actual_pl": actual,
                                    "snap_ts": snap_ts, "note": note, "written_ts": now_ts}) + "\n")

    def settle(self, day, slot, snap: dict, now_ts: float):
        self._finish(day, slot, "settled", snap["day_pl"], snap["updated"], json.dumps(snap), now_ts)

    def void(self, day, slot, why: str, now_ts: float):
        self._finish(day, slot, "void", None, None, why, now_ts)

    # -- predictions
    def submit(self, username: str, display_name: str, day: str, slot: str, raw_value, now: datetime) -> tuple[bool, str]:
        if slot not in SLOTS:
            return False, "unknown round"
        ph = phase(day, slot, now, (self.round_row(day, slot) or {}).get("status"))
        if ph != "open":
            return False, {"off": "no game today", "locked": "this round is locked"}.get(ph, "this round is closed")
        try:
            value = clean_value(raw_value)
        except (TypeError, ValueError) as e:
            return False, str(e) if "within" in str(e) else "enter a dollar amount"
        ts = now.timestamp()
        with self._lock, self._conn() as c:
            c.execute("""INSERT INTO predictions(day, slot, username, display_name, value, submitted_ts, updated_ts)
                         VALUES(?,?,?,?,?,?,?)
                         ON CONFLICT(day, slot, username) DO UPDATE SET value=excluded.value,
                             display_name=excluded.display_name, updated_ts=excluded.updated_ts""",
                      (day, slot, username, display_name, value, ts, ts))
        return True, ""

    def predictions(self, day: str, slot: str) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM predictions WHERE day=? AND slot=?", (day, slot))]

    def count(self, day: str, slot: str) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM predictions WHERE day=? AND slot=?", (day, slot)).fetchone()[0]

    def mine(self, username: str, day: str) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT slot, value FROM predictions WHERE day=? AND username=?", (day, username))
            return {r["slot"]: r["value"] for r in rows}

    def board(self, day: str, slot: str, now: datetime, live_value: float | None, viewer: str = "",
              reveal_all: bool = False) -> dict:
        """Rows for display. Before the lock only the viewer's own guess is shown (others hidden).
        Locked: sorted by distance from the live number. Settled: final ranks."""
        st = self.round_status(day, slot, now)
        preds = self.predictions(day, slot)
        ph = st["phase"]
        target = st["actual_pl"] if ph == "settled" else live_value
        hidden = ph == "open" and not reveal_all
        if target is not None and not hidden:
            rows = rank(preds, target)
        else:
            rows = sorted(preds, key=lambda p: p["submitted_ts"])
        out = []
        for p in rows:
            me = p["username"] == viewer
            if hidden and not me:
                continue
            out.append({"name": p["display_name"], "value": p["value"], "mine": me,
                        "abs_err": p.get("abs_err"), "rank": p.get("rank") if not hidden else None})
        return dict(st, target=target, hidden=hidden, rows=out)

    def standings(self, start_day: str, end_day: str) -> list[dict]:
        """Season table over settled rounds: wins, rounds played, average error."""
        with self._conn() as c:
            rounds = [dict(r) for r in c.execute(
                "SELECT * FROM rounds WHERE status='settled' AND day BETWEEN ? AND ?", (start_day, end_day))]
        agg: dict[str, dict] = {}
        for r in rounds:
            for p in rank(self.predictions(r["day"], r["slot"]), r["actual_pl"]):
                a = agg.setdefault(p["username"], {"name": p["display_name"], "wins": 0, "played": 0, "err_sum": 0.0})
                a["name"] = p["display_name"]
                a["played"] += 1
                a["err_sum"] += p["abs_err"]
                a["wins"] += p["rank"] == 1
        table = [{"username": u, "name": a["name"], "wins": a["wins"], "played": a["played"],
                  "avg_err": round(a["err_sum"] / a["played"], 2)} for u, a in agg.items()]
        table.sort(key=lambda t: (-t["wins"], t["avg_err"], t["name"]))
        return table


def season_bounds(day: str) -> tuple[str, str]:
    """Season = calendar month."""
    d = date.fromisoformat(day)
    start = d.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start.isoformat(), (nxt - timedelta(days=1)).isoformat()


# ---------------------------------------------------------------- settlement
def settle_due(store: Store, payload: dict | None, now: datetime, lookback_days: int = 1) -> list[tuple[str, str, str]]:
    """Settle or void every round whose cutoff has passed and is not finished yet. Idempotent; safe to call
    every few seconds. Returns [(day, slot, outcome)] for what changed."""
    changed = []
    today = now.date()
    for back in range(lookback_days, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        if not is_game_day(day):
            continue
        for slot in SLOTS:
            row = store.round_row(day, slot)
            if row and row.get("status") in ("settled", "void"):
                continue
            _, cut = round_times(day, slot)
            cut_ts = cut.timestamp()
            if now.timestamp() < cut_ts:
                continue
            snap = pick_snapshot(payload, cut_ts)
            if snap is not None:
                store.settle(day, slot, snap, now.timestamp())
                changed.append((day, slot, "settled"))
            elif now.timestamp() > cut_ts + SNAPSHOT_GRACE_SEC:
                store.void(day, slot, "no desk snapshot within %ds of the cutoff" % SNAPSHOT_GRACE_SEC, now.timestamp())
                changed.append((day, slot, "void"))
    return changed


def start_settler(store: Store, load_payload, interval: float = 5.0, log=None) -> threading.Thread:
    """Daemon thread inside the dashboard: every `interval` s, settle what is due from the desk book."""
    def loop():
        while True:
            try:
                for day, slot, outcome in settle_due(store, load_payload(), et_now()):
                    if log:
                        log.info("[GAME] %s %s %s", day, slot, outcome)
            except Exception as e:  # noqa: BLE001
                if log:
                    log.warning("[GAME] settle failed: %s", e)
            time.sleep(interval)
    t = threading.Thread(target=loop, name="game-settler", daemon=True)
    t.start()
    return t
