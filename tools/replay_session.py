#!/usr/bin/env python3
"""replay_session.py — run the LIVE book and arm code over a recorded session.

The other replay tools score what happened (rehearse_snap) or re-walk logged
samples (replay_ab, sim_tape_ab). This one re-decides: it feeds a session
recorded by tools/session_snapshot.py into the desk's own
``sync_watch_from_source_panels`` and ``poll_once`` on a simulated clock, so a
code or config change can be judged off hours on the session it would have
faced: book size, armable, data-blocked, opens per 10 minutes, concurrency.

USAGE (on the mini, after hours — it needs the recording and Alpaca data keys)
    .venv/bin/python tools/replay_session.py --day 2026-09-25
    .venv/bin/python tools/replay_session.py --day 2026-09-25 --sha 44b08e3
    .venv/bin/python tools/replay_session.py --day 2026-09-25 --set ai_watch_min_price=5
    .venv/bin/python tools/replay_session.py --day 2026-09-24 --start 13:10 --warm-book

SAFETY
  * Runs from a throwaway export of the code (``git archive <sha>``), with
    AI_REPORT_DIR in a temp dir, so the desk code's own state writes never
    touch the live desk's files.
  * DNS is refused for every host except data.alpaca.markets (historical
    bars/quotes). The trading API and the live dashboard are unreachable.

WHAT IS REAL AND WHAT IS MODELLED
  real      the live code, the recorded source lists / research boards /
            engine signal_state (%R, prices, clocks) / dashboard tickers,
            the config in force, historical SIP spread / pace / gap
  modelled  fills (at the recorded price, no slippage or spread); exits run
            the desk's own exit functions (local trail + ratchet, T1 scale,
            left_overbought, dead trade, 15:50 flatten) every 2 s on the
            recorded price, as tools/sim_fill_replay.py does — broker-side
            stop orders are not simulated (--exits hold: fixed hold instead)
  synthetic names the replayed code pushes to the engine that the live desk
            was not watching get an engine row after --synth-warmup seconds
            (cap --synth-cap, the Finnhub 50): %R/RSI/MACD from the engine's
            own evaluate_state on IEX premarket + SIP session 1m bars, price =
            last completed bar close with its true age. Opens on synthetic
            data are counted separately.
  degraded  sessions recorded before 2026-09-25 have no /api/state: ticker
            rows are rebuilt from the engine's signal_state prices, with no
            Momentum-panel flags or dashboard pct/rvol.

FIDELITY  --fidelity replays the code that actually ran that day (read from
          the recording) and scores it against what live did: share of live
          buys reproduced within 90 s, share of replay opens live also took,
          and mean overlap of the seated book. Written to the archive dir.

Ages are advanced by the time since each record was captured, so a replayed
price is never younger than it was.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time as _real_time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
HERE = Path(os.getenv("REPLAY_REPO") or Path(__file__).resolve().parent.parent)
SNAP_BASE = Path(os.getenv("SESSION_SNAPSHOT_DIR") or Path.home() / "session_snapshots")
CACHE_DIR = Path(os.getenv("REPLAY_CACHE_DIR") or Path.home() / "replay_cache")
ALLOWED_HOSTS = ("data.alpaca.markets",)
# Files the desk code reads from its own folder; materialized from the recording.
DISK_FILES = (
    "trending_stocks.json", "movers_stocks.json", "signal_state.json",
    "transcription/wb_watchlist.json", "agy_suggestions.json",
    "claude_suggestions.json", "suggestions.json", "grok_suggestions.json",
    "seed_rank_gx.json", "seed_rank_ax.json",
)
AGE_KEYS = ("price_age_sec", "rt_price_age_sec", "bars_age_sec", "last_ask_age_sec")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", required=True)
    ap.add_argument("--start", default="09:30")
    ap.add_argument("--end", default="15:50")
    ap.add_argument("--sha", default="HEAD", help="code to replay (commit-ish)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="config override (JSON value or string), repeatable")
    ap.add_argument("--exits", choices=("live", "hold"), default="live")
    ap.add_argument("--hold-min", type=float, default=7.0, help="with --exits hold")
    ap.add_argument("--synth-warmup", type=float, default=30.0)
    ap.add_argument("--synth-cap", type=int, default=50)
    ap.add_argument("--no-synth", action="store_true")
    ap.add_argument("--fidelity", action="store_true",
                    help="replay the code that ran and score against live")
    ap.add_argument("--step", type=float, default=2.0, help="book tick, sim seconds")
    ap.add_argument("--warm-book", action="store_true",
                    help="start from the recorded book at --start instead of empty")
    ap.add_argument("--snapshots", default=None, help="archive dir (default ~/session_snapshots/DAY)")
    ap.add_argument("--out", default=None, help="write result JSON here")
    ap.add_argument("--trace", action="append", default=[], metavar="SYM",
                    help="print this symbol's arm inputs every poll")
    return ap.parse_args(argv)


# ── stage 1: export the code and re-exec inside it ─────────────────────────

def live_code_sha(day: str, snap_dir: Path) -> tuple[str | None, bool]:
    """The commit the live desk ran most of the day, and whether its tree was dirty."""
    from collections import Counter
    seen: Counter = Counter()
    for p in (snap_dir / "recorder_config.jsonl.gz",
              HERE / "ai_reports" / "sessions" / day / "config.jsonl.gz"):
        if p.exists():
            with gzip.open(p, "rt") as f:
                for line in f:
                    try:
                        seen[str(json.loads(line).get("git_sha") or "")] += 1
                    except ValueError:
                        pass
            break
    if not seen and (snap_dir / "fills.jsonl").exists():
        for line in open(snap_dir / "fills.jsonl"):
            try:
                seen[str(json.loads(line).get("git_version") or "")] += 1
            except ValueError:
                pass
    seen.pop("", None)
    seen.pop("unknown", None)
    if not seen:
        return None, False
    sha = seen.most_common(1)[0][0]
    return sha.rstrip("+"), sha.endswith("+")


def export_and_reexec(args) -> int:
    ref = args.sha
    if args.fidelity and ref == "HEAD":
        snap_dir = Path(args.snapshots) if args.snapshots else SNAP_BASE / args.day
        found, dirty = live_code_sha(args.day, snap_dir)
        if found:
            ref = found
            print(f"[replay] fidelity: live ran {found}{' (tree was dirty)' if dirty else ''}")
        else:
            print("[replay] fidelity: live commit unknown; replaying HEAD")
    sha = subprocess.check_output(
        ["git", "-C", str(HERE), "rev-parse", ref], text=True).strip()
    work = Path(tempfile.mkdtemp(prefix=f"replay_{sha[:8]}_"))
    code = work / "code"
    code.mkdir()
    archive = subprocess.Popen(["git", "-C", str(HERE), "archive", sha], stdout=subprocess.PIPE)
    subprocess.check_call(["tar", "-x", "-C", str(code)], stdin=archive.stdout)
    archive.wait()
    secrets = HERE / "config" / "secrets.json"
    if secrets.exists():
        (code / "config" / "secrets.json").symlink_to(secrets)
    # The driver itself always comes from this checkout, so older commits can
    # be replayed with today's harness.
    shutil.copy2(Path(__file__), code / "tools" / "replay_session.py")
    reports = work / "ai_reports"
    reports.mkdir()
    env = dict(os.environ, REPLAY_EXPORTED="1", REPLAY_SHA=sha, REPLAY_REPO=str(HERE),
               AI_REPORT_DIR=str(reports), REPLAY_WORK=str(work))
    py = str(HERE / ".venv" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable
    rc = subprocess.call([py, "-u", str(code / "tools" / "replay_session.py")] + sys.argv[1:],
                         cwd=str(code), env=env)
    print(f"[replay] work dir: {work}")
    return rc


# ── stage 2 helpers (run inside the export) ─────────────────────────────────

class SimClock:
    def __init__(self, t: float):
        self.t = t


def guard_network() -> dict:
    """Refuse DNS for everything but historical market data."""
    import traceback
    blocked: dict[str, int] = {}
    real = socket.getaddrinfo
    root = str(Path.cwd())

    def getaddrinfo(host, *a, **k):
        h = str(host or "")
        if any(h == x or h.endswith("." + x) for x in ALLOWED_HOSTS):
            return real(host, *a, **k)
        desk = [f for f in traceback.extract_stack()
                if f.filename.startswith(root) and "replay_session" not in f.filename]
        where = f"{Path(desk[-1].filename).name}:{desk[-1].name}" if desk else "?"
        key = f"{h} <- {where}"
        blocked[key] = blocked.get(key, 0) + 1
        raise socket.gaierror(f"replay: network to {h} is blocked")

    socket.getaddrinfo = getaddrinfo
    return blocked


class TimeProxy:
    """Stands in for the ``time`` module inside desk modules."""

    def __init__(self, clock: SimClock):
        self._clock = clock

    def time(self):
        return self._clock.t

    def monotonic(self):
        return self._clock.t

    def sleep(self, _s):
        return None

    def __getattr__(self, name):
        return getattr(_real_time, name)


def patch_clocks(clock: SimClock, root: Path, done: set) -> None:
    """Give every desk module (code under root) the simulated clock."""
    proxy = TimeProxy(clock)
    for name, mod in list(sys.modules.items()):
        if name in done or mod is None:
            continue
        f = getattr(mod, "__file__", None) or ""
        if not f.startswith(str(root)):
            continue
        done.add(name)
        if getattr(mod, "time", None) is _real_time:
            mod.time = proxy


class Recording:
    """Streams state_snapshots.jsonl.gz in time order; keeps the latest per file."""

    def __init__(self, path: Path):
        self._f = gzip.open(path, "rt")
        self._next = None
        self.latest: dict[str, tuple[float, dict]] = {}
        self.changed: set[str] = set()
        self._advance_line()

    def _advance_line(self):
        while True:
            try:
                line = self._f.readline()
            except (EOFError, OSError):
                line = ""
            if not line:
                self._next = None
                return
            try:
                self._next = json.loads(line)
                return
            except ValueError:
                continue

    def advance(self, t: float) -> None:
        while self._next is not None and float(self._next.get("ts") or 0) <= t:
            rec = self._next
            self.latest[rec["file"]] = (float(rec["ts"]), rec.get("data"))
            self.changed.add(rec["file"])
            self._advance_line()

    def get(self, name: str):
        hit = self.latest.get(name)
        return hit if hit else (None, None)


def aged(row: dict, dt: float) -> dict:
    out = dict(row)
    for k in AGE_KEYS:
        v = out.get(k)
        if isinstance(v, (int, float)):
            out[k] = round(float(v) + max(0.0, dt), 2)
    return out


class SynthEngine:
    """Engine rows for names the replayed code asked for but live never watched."""

    def __init__(self, bars: dict, day: str, warmup: float, cap: int, enabled: bool = True):
        self.bars, self.day = bars, day
        self.warmup, self.cap, self.enabled = warmup, cap, enabled
        self.requested: dict[str, float] = {}
        self.last_req: dict[str, float] = {}
        self.used: set[str] = set()
        self._cache: dict[tuple[str, float], dict | None] = {}
        self._params = None

    def request(self, syms, t: float) -> None:
        for s in syms or []:
            s = str(s or "").upper().strip()
            if not s:
                continue
            self.requested.setdefault(s, t)
            self.last_req[s] = t

    def live_slots(self, have: set[str], now: float, keep_sec: float = 600.0) -> int:
        """Recorded names the replayed code is still asking for: they hold slots.
        Names it stopped pushing would have aged out of the ticker log live."""
        return sum(1 for s in have if now - self.last_req.get(s, -1e18) <= keep_sec)

    def _p(self):
        if self._params is None:
            import strategy_three_indicator as sti
            # signal_engine.env on the mini sets THREE_IND_RTE_REQUIRE_SLOW=false.
            self._params = sti.params(rte_require_slow=False)
        return self._params

    def row(self, sym: str, now: float) -> dict | None:
        b = self.bars.get(sym) or {}
        open_t = at(self.day, "09:30")
        seq = [r for r in b.get("iex", []) if r[0] < open_t]
        seq += [r for r in b.get("s", []) if r[0] >= open_t]
        seq = [r for r in seq if r[0] + 60 <= now][-300:]
        if len(seq) < 40:
            return None
        key = (sym, seq[-1][0])
        if key not in self._cache:
            import pandas as pd
            import strategy_three_indicator as sti
            df = pd.DataFrame([r[1:] for r in seq],
                              columns=["open", "high", "low", "close", "volume"])
            try:
                a = sti.to_arrays(sti.compute_indicators(df, self._p()))
                st = sti.evaluate_state(a, len(seq) - 1, self._p())
                # numpy scalars -> plain JSON types, as the engine's published
                # signal_state.json carries (the desk saves these into state files).
                self._cache[key] = json.loads(json.dumps(
                    st, default=lambda o: o.item() if hasattr(o, "item") else None))
            except Exception:  # noqa: BLE001
                self._cache[key] = None
        st = self._cache[key]
        if st is None:
            return None
        px = seq[-1][4]
        age = round(now - (seq[-1][0] + 60), 1)
        sp = dict(st, rt_price=px, rt_price_age_sec=age, price=px, bars_src="realtime",
                  bars_age_sec=age, pctr_src="live", synthetic=True)
        return {"ticker": sym, "price": px, "price_age_sec": age,
                "signal_proximity": sp, "synthetic": True}

    def rows(self, have: set[str], now: float) -> list[dict]:
        if not self.enabled:
            return []
        out = []
        room = self.cap - self.live_slots(have, now)
        for s, t0 in sorted(self.requested.items(), key=lambda kv: kv[1]):
            if room <= 0:
                break
            if s in have or now - t0 < self.warmup:
                continue
            r = self.row(s, now)
            if r is not None:
                out.append(r)
                self.used.add(s)
                room -= 1
        return out


def build_dashboard_state(rec: Recording, now: float, synth: SynthEngine | None = None) -> dict:
    """What /api/state would have returned at ``now``."""
    sig_ts, sig = rec.get("signal_state.json")
    sig_t = (sig or {}).get("tickers") or {}
    sig_aged = {s: aged(v, now - sig_ts) for s, v in sig_t.items() if isinstance(v, dict)}
    api_ts, api = rec.get("api/state")
    if api:
        state = dict(api)
        rows = []
        for r in api.get("tickers") or []:
            if not isinstance(r, dict):
                continue
            r2 = aged(r, now - api_ts)
            sp = sig_aged.get(r2.get("ticker"))
            if sp:
                r2["signal_proximity"] = sp
            rows.append(r2)
        if synth is not None:
            rows += synth.rows({r.get("ticker") for r in rows} | set(sig_aged), now)
        state["tickers"] = rows
        return state
    # Degraded (pre-recorder sessions): rebuild rows from the engine's prints.
    rows = []
    for s, sp in sig_aged.items():
        px = sp.get("rt_price") or sp.get("price")
        rows.append({"ticker": s, "price": px,
                     "price_age_sec": sp.get("rt_price_age_sec"),
                     "signal_proximity": sp})
    _, mv = rec.get("movers_stocks.json")
    _, tr = rec.get("trending_stocks.json")
    if synth is not None:
        rows += synth.rows(set(sig_aged), now)
    # /api/state's ai_positions IS this file; the buy's price ceiling floats
    # on its account equity (desk_risk.dynamic_max_price).
    _, pos = rec.get("ai_positions_state.json")
    return {"tickers": rows, "movers": mv or {}, "trending": tr or {},
            "ai_positions": pos or {}}


class JsonCache:
    def __init__(self, name: str):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.path = CACHE_DIR / f"{name}.json"
        try:
            self.d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.d = {}
        self.dirty = 0

    def get(self, key, fn):
        if key in self.d:
            return self.d[key]
        val = fn()
        self.d[key] = val
        self.dirty += 1
        if self.dirty >= 50:
            self.save()
        return val

    def save(self):
        if self.dirty:
            self.path.write_text(json.dumps(self.d))
            self.dirty = 0


class FakeBroker:
    """Fills at the recorded price. Exits: the desk's exit functions, or a fixed hold."""

    def __init__(self, hold_sec: float, max_pos: int, equity: float, mode: str = "hold"):
        self.mode = mode
        self.hold = hold_sec
        self.max_pos = max_pos
        self.equity = equity
        self.open: dict[str, dict] = {}
        self.closed: list[dict] = []
        self.last_exit: dict[str, float] = {}
        self.entries: dict[str, int] = {}

    def price(self, dash: dict, sym: str):
        for r in dash.get("tickers") or []:
            if isinstance(r, dict) and r.get("ticker") == sym:
                try:
                    px = float(r.get("price") or 0)
                except (TypeError, ValueError):
                    px = 0.0
                return px if px > 0 else None
        return None

    def enter(self, sym: str, px: float, now: float, decision: dict,
              cfg: dict | None = None, synthetic: bool = False) -> dict:
        stop = decision.get("stop_price") if isinstance(decision, dict) else None
        self.open[sym] = {"symbol": sym, "entry_ts": now, "entry_px": px, "stop": stop,
                          "synthetic": synthetic}
        if self.mode == "live":
            import ai_positions as cp
            try:
                pos = open_position(cp, cfg or {}, px, now, stop)
            except AttributeError:
                pos = None  # commit predates the local trail: fixed hold
            self.open[sym]["pos"] = pos
            self.open[sym]["lob"] = {"symbol": sym, "indicator": {}}
        self.entries[sym] = self.entries.get(sym, 0) + 1
        return {"ok": True, "stop_price": self.open[sym]["stop"],
                "target_1": decision.get("target_1") if isinstance(decision, dict) else None}

    def _close(self, sym: str, now: float, px: float, why: str, r=None) -> None:
        pos = self.open.pop(sym)
        ret = px / pos["entry_px"] - 1
        if r is not None and pos.get("pos"):
            p = pos["pos"]
            ret = r * float(p["risk_per_share"]) / float(p["entry"])
        pos.pop("pos", None)
        pos.pop("lob", None)
        pos.update(exit_ts=now, exit_px=px, ret=ret, reason=why, r=r)
        self.closed.append(pos)
        self.last_exit[sym] = now

    def exits_due(self, now: float, dash: dict, cfg: dict | None = None, ew=None) -> list[str]:
        out = []
        for sym, pos in list(self.open.items()):
            px = self.price(dash, sym)
            if self.mode == "hold" or not pos.get("pos"):
                if now - pos["entry_ts"] >= self.hold:
                    self._close(sym, now, px or pos["entry_px"], "hold")
                    out.append(sym)
                continue
            if px is None:
                continue
            why = self._live_exit(pos, px, now, dash, cfg or {}, ew)
            if why:
                self._close(sym, now, px, why, r=exit_r(pos["pos"], px))
                out.append(sym)
        return out

    def _live_exit(self, pos: dict, px: float, now: float, dash: dict, cfg: dict, ew):
        """One tick of the desk's exits, as tools/sim_fill_replay.walk_ticks runs them."""
        import ai_positions as cp
        p = pos["pos"]
        sp = next((r.get("signal_proximity") for r in dash.get("tickers") or []
                   if r.get("ticker") == pos["symbol"]), None)
        if ew is not None and isinstance(sp, dict):
            lob = pos["lob"]
            lob["indicator"] = dict(sp)
            hit, _why = ew.exhaustion_exit_now(lob, cfg, now=now)
            if hit:
                return "left_overbought"
        if px <= float(p["local_stop_price"]) + 1e-9:
            return "local_trail"
        entry, risk = float(p["entry"]), float(p["risk_per_share"])
        t1_rr = max(0.0, float(cfg.get("ai_watch_synth_rr", 0.6) or 0.0))
        if t1_rr > 0 and float(p["scale"]) > 0 and not p["t1_hit"] and px >= float(p["target_1"]):
            p["t1_hit"], p["t1_r"] = True, t1_rr
        p["peak_price"] = max(float(p.get("peak_price") or entry), px)
        p["mfe_r"] = max(float(p["mfe_r"]), (p["peak_price"] - entry) / risk)
        p["mae_r"] = min(float(p["mae_r"]), (px - entry) / risk)
        p["last_seen_price"] = px
        cp.note_trail_print(p, px, n=max(2, int(cfg.get("ai_local_trail_print_ring", 3) or 3)))
        want = cp.local_profit_stop(p, cfg)
        if want is not None and want > float(p["local_stop_price"]) + 1e-9:
            p["local_stop_price"] = want
        dead_min = float(cfg.get("ai_dead_trade_min", 22.0) or 0.0)
        dead_mfe = float(cfg.get("ai_dead_trade_mfe_r", 0.10) or 0.0)
        if (dead_min > 0 and not p["t1_hit"] and float(p["local_stop_price"]) <= entry + 1e-9
                and (now - float(p["entry_ts"])) / 60 >= dead_min and p["mfe_r"] < dead_mfe):
            return "dead_trade"
        if et_min(now) >= EOD_MIN:
            return "eod_flatten"
        return None


def recording_symbols(path: Path) -> set[str]:
    """Every symbol the recording mentions (sources, engine, book, boards)."""
    syms: set[str] = set()

    def add(x):
        s = str(x or "").upper().strip()
        if s and s.replace(".", "").isalpha() and len(s) <= 6:
            syms.add(s)

    with gzip.open(path, "rt") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            d = rec.get("data")
            if not isinstance(d, dict):
                continue
            name = rec.get("file", "")
            if name == "signal_state.json":
                for k in (d.get("tickers") or {}):
                    add(k)
            elif name == "api/state":
                for r in d.get("tickers") or []:
                    if isinstance(r, dict):
                        add(r.get("ticker"))
            else:
                rows = d.get("rows") if isinstance(d.get("rows"), list) else None
                if rows is None and name.endswith("entry_watch_state.json"):
                    rows = [{"symbol": k} for k in d]
                for r in rows or []:
                    if isinstance(r, dict):
                        add(r.get("symbol") or r.get("ticker"))
    return syms


def day_bars(day: str, syms: set[str], client) -> dict:
    """{sym: {"m": [(ts, open, vol)], "d": [(date, close, vol)]}} for the day, cached."""
    import pickle
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"bars_v2_{day}.pkl"
    have = {}
    if path.exists():
        try:
            have = pickle.loads(path.read_bytes())
        except Exception:  # noqa: BLE001
            have = {}
    need = sorted(s for s in syms if s not in have)
    if need:
        from datetime import timedelta, timezone
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        o = datetime.strptime(day, "%Y-%m-%d").replace(hour=9, minute=30, tzinfo=ET)
        for i in range(0, len(need), 100):
            batch = need[i:i + 100]
            m = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Minute,
                start=o.astimezone(timezone.utc),
                end=o.replace(hour=16, minute=0).astimezone(timezone.utc),
                feed=DataFeed.SIP)).data
            d = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Day,
                start=(o - timedelta(days=45)).astimezone(timezone.utc),
                end=(o - timedelta(minutes=1)).astimezone(timezone.utc),
                feed=DataFeed.SIP)).data
            x = client.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Minute,
                start=(o - timedelta(days=4)).replace(hour=4).astimezone(timezone.utc),
                end=o.replace(hour=16, minute=0).astimezone(timezone.utc),
                feed=DataFeed.IEX)).data
            for s in batch:
                have[s] = {
                    "m": [(b.timestamp.timestamp(), float(b.open), float(b.volume))
                          for b in m.get(s) or []],
                    "s": [(b.timestamp.timestamp(), float(b.open), float(b.high),
                           float(b.low), float(b.close), float(b.volume))
                          for b in m.get(s) or []],
                    "d": [(b.timestamp.astimezone(ET).strftime("%Y-%m-%d"), float(b.close),
                           float(b.volume)) for b in d.get(s) or []],
                    "iex": [(b.timestamp.timestamp(), float(b.open), float(b.high),
                             float(b.low), float(b.close), float(b.volume))
                            for b in x.get(s) or []],
                }
            print(f"[replay] bars {min(i + 100, len(need))}/{len(need)}", flush=True)
        path.write_bytes(pickle.dumps(have))
    return have


class RecordedInputs:
    """Spread / pace / gap values the live desk computed that session."""

    MISSING = object()

    def __init__(self, day: str, snap_dir: Path):
        import bisect
        self._bisect = bisect
        self.by: dict[tuple[str, str], list[tuple[float, object]]] = {}
        repo = Path(os.getenv("REPLAY_REPO") or ".")
        for p in (snap_dir / "recorder_inputs.jsonl.gz",
                  repo / "ai_reports" / "sessions" / day / "inputs.jsonl.gz"):
            if p.exists():
                with gzip.open(p, "rt") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        self.by.setdefault((r["kind"], r["symbol"]), []).append(
                            (float(r["ts"]), r.get("value")))
                break
        for v in self.by.values():
            v.sort(key=lambda x: x[0])
        self.hits = self.misses = 0

    def at(self, kind: str, sym: str, t: float, ttl: float):
        rows = self.by.get((kind, str(sym).upper()))
        if rows:
            i = self._bisect.bisect_right([r[0] for r in rows], t) - 1
            if i >= 0 and t - rows[i][0] < ttl:
                self.hits += 1
                return rows[i][1]
        self.misses += 1
        return self.MISSING


# ── Harness glue, vendored so an old commit without these tools still replays.
# Book buckets: tools/rehearse_open.py. Exit tick: tools/sim_fill_replay.py
# walk_ticks + tools/sim_rstop_path._close. Desk functions come from the
# replayed commit; only this glue is today's.
SIGNAL_WAIT = {"wait_mid_rise", "mid_rise_lost", "mid_rise_stale"}
GATE = {"spread_wide", "gapped_down", "above_max_price", "below_min_price",
        "reentry_cooldown", "rvol_pace_low", "look_wash", "hard_no"}
DATA = {"stale_quote", "engine_stale", "spread_unknown", "gap_unknown",
        "no_rsi_data", "rvol_pace_unknown", "no_structure", "tape_only",
        "stale_tape", "confirm_stale"}
EOD_MIN = 15 * 60 + 50


def bucket(row: dict) -> str:
    why = str(row.get("arm_why") or row.get("block_code") or row.get("why") or "")
    if row.get("arm_ok") or row.get("status") in ("armed", "submitted", "filled"):
        return "armable"
    if why in SIGNAL_WAIT:
        return "armable"
    if why in GATE:
        return "gate"
    if why in DATA:
        return "data"
    return "other"


def watch_rows(data) -> dict[str, dict]:
    if not isinstance(data, dict):
        return {}
    if data and all(isinstance(v, dict) for v in data.values()):
        return {str(k).upper(): v for k, v in data.items()}
    for key in ("watches", "watch", "rows", "book"):
        w = data.get(key)
        if isinstance(w, dict):
            return {str(k).upper(): v for k, v in w.items() if isinstance(v, dict)}
    return {}


def open_position(cp, cfg: dict, entry: float, ts: float, stop) -> dict | None:
    """sim_fill_replay.open_from_fill with risk_mode='live'."""
    pct = float(cfg.get("ai_watch_synth_stop_pct", 5.0) or 5.0) / 100.0
    risk = entry - float(stop) if stop is not None and 0 < float(stop) < entry else entry * pct
    if entry <= 0 or risk <= 0:
        return None
    loc = cp.initial_local_stop(entry, risk, cfg, spread_r=None) or (entry - risk)
    if not bool(cfg.get("ai_local_trail_enabled", True)):
        loc = entry - risk
    t1_rr = max(0.0, float(cfg.get("ai_watch_synth_rr", 0.6) or 0.0))
    scale = max(0.0, min(100.0, float(cfg.get("ai_watch_synth_scale_out_pct", 50.0) or 50.0))) / 100
    return {"entry": entry, "entry_ts": ts, "entry_price": entry, "entry_stop_price": entry - risk,
            "risk_per_share": risk, "local_stop_price": loc,
            "target_1": entry + t1_rr * risk if t1_rr > 0 else 0.0, "scale": scale,
            "why_arm": "replay", "peak_price": entry, "mfe_r": 0.0, "mae_r": 0.0,
            "t1_hit": False, "t1_r": None, "trail_prints": [entry], "trail_last": None,
            "features": {}, "spread_r": None}


def exit_r(p: dict, px: float) -> float:
    """sim_rstop_path._close's R, with the T1 scale-out blend."""
    risk = float(p["risk_per_share"])
    r_exit = (px - float(p["entry"])) / risk if risk > 0 else 0.0
    if p.get("t1_hit") and p.get("t1_r") is not None:
        sc = float(p.get("scale") or 0.5)
        return sc * float(p["t1_r"]) + (1.0 - sc) * r_exit
    return r_exit


def et_min(t: float) -> int:
    d = datetime.fromtimestamp(t, ET)
    return d.hour * 60 + d.minute


def at(day: str, hhmm: str) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=ET).timestamp()


def run_inside(args) -> int:
    root = Path.cwd()
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tools"))
    snap_dir = Path(args.snapshots) if args.snapshots else SNAP_BASE / args.day
    rec = Recording(snap_dir / "state_snapshots.jsonl.gz")
    t_start, t_end = at(args.day, args.start), at(args.day, args.end)
    clock = SimClock(t_start)
    blocked = guard_network()

    # Config in force at the start, plus overrides.
    rec.advance(t_start)
    _, cfg_rec = rec.get("config/bot_config.json")
    if cfg_rec is None:
        cfg_rec = json.loads((root / "config" / "bot_config.json").read_text())
    overrides = {}
    for kv in args.set:
        k, _, v = kv.partition("=")
        try:
            overrides[k] = json.loads(v)
        except ValueError:
            overrides[k] = v

    def write_config(base: dict) -> None:
        (root / "config" / "bot_config.json").write_text(
            json.dumps({**base, **overrides}, indent=1))

    write_config(cfg_rec)

    def materialize():
        # The desk's knobs changed during the session; follow them.
        if "config/bot_config.json" in rec.changed:
            _, c = rec.get("config/bot_config.json")
            if isinstance(c, dict):
                write_config(c)
        for name in list(rec.changed):
            if name in DISK_FILES:
                _, data = rec.get(name)
                p = root / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps(data))
        rec.changed.clear()

    materialize()

    import ai_entry_watch as ew
    import ai_positions as cp
    import ai_trading as gt
    from config import load_config

    patched: set = set()
    patch_clocks(clock, root, patched)

    dash = {"state": {}}
    ew.dashboard_state = lambda *, force=False: dash["state"]
    # Pushes are how live asks the dashboard/engine to watch a name; the
    # synthetic engine serves the ones the recording has no data for.
    synth_box: dict = {}

    def push(symbols, *a, **k):
        syms = [str(x or "").upper().strip() for x in symbols or []]
        band = getattr(ew, "_push_band_filter", None)
        if band is not None:
            syms = band(syms)
        if "synth" in synth_box:
            synth_box["synth"].request(syms, clock.t)
        return {"ok": True, "added": len(syms)}

    ew.push_candidates_to_engine = push
    try:
        import finnhub_stream as fs
        fs.set_subscribe_priority = lambda *a, **k: None
        fs.request_subscribe = lambda *a, **k: None
    except Exception:  # noqa: BLE001
        pass

    _, pos_rec = rec.get("ai_positions_state.json")
    equity = 100_000.0
    try:
        acct = (pos_rec or {}).get("account") or (pos_rec or {}).get("_account") or {}
        equity = float(acct.get("equity") or equity)
    except (TypeError, ValueError):
        pass
    cfg = load_config()
    broker = FakeBroker(args.hold_min * 60, int(cfg.get("ai_max_positions", 5) or 5), equity,
                        mode=args.exits)

    gt.market_is_open = lambda: 570 <= et_min(clock.t) < 960
    gt.is_ready = lambda: True
    gt.has_open_position = lambda s: str(s).upper() in broker.open
    gt.can_open_new_position = lambda s=None: len(broker.open) < broker.max_pos
    gt.get_account = lambda: {"ok": True, "equity": broker.equity}
    gt._latest_ask = lambda s: broker.price(dash["state"], str(s).upper())
    gt._latest_bid = lambda s: None
    if hasattr(gt, "_cached_quote"):
        gt._cached_quote = lambda s: None
    for fn in ("record_external_buy", "prime_quotes", "invalidate_quotes", "refresh_quotes_now"):
        if hasattr(gt, fn):
            setattr(gt, fn, lambda *a, **k: 0)
    ew._recent_exit_ts = lambda s: broker.last_exit.get(str(s).upper())
    ew._entries_today = lambda s: broker.entries.get(str(s).upper(), 0)

    def place(sym, decision, equity_arg=None, **kw):
        s = str(sym).upper()
        px = broker.price(dash["state"], s)
        if px is None:
            return {"ok": False, "error": "replay: no recorded price"}
        synthetic = any(r.get("synthetic") for r in dash["state"].get("tickers") or []
                        if r.get("ticker") == s)
        return broker.enter(s, px, clock.t, decision, cfg=load_config(), synthetic=synthetic)

    cp.place_scaled_entry = place
    # The live book loop flattens overnight leftovers at the open and records
    # the day; entries wait for that (sod_liquidate_done). Same state here.
    cp.SOD_LIQUIDATE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cp.SOD_LIQUIDATE_STATE_PATH.write_text(json.dumps({"last_day": args.day}))

    # Historical inputs the arm reads. Pace and gap run through the live
    # functions (and their sim-clock caches) on bars fetched once per day;
    # the gap uses the SIP 09:30 open from 09:30 (live reads the IEX first
    # print until 09:46). Spreads are real SIP quotes, cached on disk in the
    # live function's 3-minute buckets so a rerun costs nothing.
    bars = day_bars(args.day, recording_symbols(snap_dir / "state_snapshots.jsonl.gz"),
                    ew._data_client())

    def pace_inputs(sym, t_end, end_et):
        b = bars.get(str(sym).upper()) or {}
        so_far = sum(v for ts, _o, v in b.get("m", []) if ts + 60 <= t_end)
        prior = [v for d, _c, v in b.get("d", []) if d < args.day]
        avg = sum(prior[-20:]) / 20 if len(prior) >= 20 else None
        return so_far, avg

    def gap_inputs(sym, t):
        if et_min(t) < 570:
            return None, None
        b = bars.get(str(sym).upper()) or {}
        m = b.get("m") or []
        prior = [c for d, c, _v in b.get("d", []) if d < args.day]
        if not m or not prior:
            return None, None
        return m[0][1], prior[-1]

    ew._rvol_pace_inputs, ew._gap_inputs_sip = pace_inputs, gap_inputs

    import alpaca_api as aa
    import pandas as pd

    def fetch_bars(_client, ticker, bar_cfg):
        rows = (bars.get(str(ticker).upper()) or {}).get("iex") or []
        cut = clock.t - clock.t % 60  # completed minutes only
        rows = [r for r in rows if r[0] + 60 <= cut][-int(bar_cfg.get("bar_count", 300) or 300):]
        if not rows:
            return None
        df = pd.DataFrame([r[1:] for r in rows], columns=["open", "high", "low", "close", "volume"],
                          index=pd.to_datetime([r[0] for r in rows], unit="s", utc=True))
        return df

    aa.fetch_bars = fetch_bars
    aa.fetch_bars_batch = lambda _c, tickers, bar_cfg: {
        t: df for t in tickers if (df := fetch_bars(_c, t, bar_cfg)) is not None}
    spread_cache = JsonCache("sip_spread")
    recorded = RecordedInputs(args.day, snap_dir)
    _sip, _pace, _gap = ew.sip_spread_pct, ew.rvol_pace_sip, ew.open_gap_pct

    def sip_spread(sym, *, now=None, ttl=180.0, **kw):
        t = float(now if now is not None else clock.t)
        v = recorded.at("sip_spread", sym, t, 180.0)
        if v is not recorded.MISSING:
            return v
        return spread_cache.get(f"{str(sym).upper()}|{int(t // 180)}",
                                lambda: _sip(sym, now=t, ttl=0))

    def pace(sym, *, now=None, **kw):
        t = float(now if now is not None else clock.t)
        v = recorded.at("rvol_pace", sym, t, 120.0)
        return _pace(sym, now=t, **kw) if v is recorded.MISSING else v

    def gap(sym, *, now=None, **kw):
        t = float(now if now is not None else clock.t)
        v = recorded.at("open_gap", sym, t, 6.5 * 3600)
        return _gap(sym, now=t, **kw) if v is recorded.MISSING else v

    ew.sip_spread_pct, ew.rvol_pace_sip, ew.open_gap_pct = sip_spread, pace, gap
    # The binding limit is the engine's push cap when it is below Finnhub's 50.
    push_max = int(cfg.get("ai_watch_engine_push_max", 0) or 0)
    synth = SynthEngine(bars, args.day, args.synth_warmup,
                        min(args.synth_cap, push_max) if push_max > 0 else args.synth_cap,
                        enabled=not args.no_synth)
    synth_box["synth"] = synth

    if args.warm_book:
        _, w = rec.get("ai_reports/entry_watch_state.json")
        if isinstance(w, dict):
            ew.save_watch(watch_rows(w))

    slots: dict[int, dict] = {}
    poll_sec = float(cfg.get("ai_watch_poll_sec", 10.0) or 10.0)
    last_poll = -1e18
    t = t_start
    wall0 = _real_time.time()
    while t <= t_end:
        clock.t = t
        rec.advance(t)
        materialize()
        dash["state"] = build_dashboard_state(rec, t, synth)
        live = load_config()
        for sym in broker.exits_due(t, dash["state"], cfg=live, ew=ew):
            with ew._WATCH_LOCK:
                w = ew.load_watch()
                if w.pop(sym, None) is not None:
                    ew.save_watch(w)
        try:
            ew.sync_watch_from_source_panels(live, now=t)
        except Exception as e:  # noqa: BLE001
            print(f"[replay] sync failed at {datetime.fromtimestamp(t, ET):%H:%M:%S}: {e}")
        n_before = sum(broker.entries.values())
        if t - last_poll >= poll_sec:
            last_poll = t
            try:
                ew.poll_once(cfg=live, now=t)
            except Exception as e:  # noqa: BLE001
                print(f"[replay] poll failed at {datetime.fromtimestamp(t, ET):%H:%M:%S}: {e}")
            for sym in args.trace:
                r = ew.load_watch().get(sym.upper())
                ind = (r or {}).get("indicator") or {}
                sp = next((x.get("signal_proximity") for x in dash["state"].get("tickers") or []
                           if x.get("ticker") == sym.upper()), None) or {}
                print(f"[trace] {datetime.fromtimestamp(t, ET):%H:%M:%S} {sym} "
                      f"seated={r is not None} status={(r or {}).get('status')} "
                      f"block={(r or {}).get('block_code')} "
                      f"rec.pctr={ind.get('pctr')} slow_rising={ind.get('pctr_slow_rising')} "
                      f"src={ind.get('pctr_src') or ind.get('exh_src')} "
                      f"eng.pctr={sp.get('pctr')} eng.age={sp.get('bars_age_sec')} "
                      f"latch={ew._MID_RISE_STATE.get(sym.upper())} "
                      f"px={(r or {}).get('last_ask')} age={(r or {}).get('last_ask_age_sec')}")
        patch_clocks(clock, root, patched)  # modules imported lazily this step
        slot = int((t - t_start) // 600)
        s = slots.setdefault(slot, {"t": t, "book": [], "armable": [], "data": [],
                                    "open": [], "opens": 0, "rec_book": [], "overlap": []})
        rows = watch_rows(ew.load_watch())
        buckets = [bucket(r) for r in rows.values()]
        s["book"].append(len(rows))
        s["armable"].append(sum(1 for b in buckets if b == "armable"))
        s["data"].append(sum(1 for b in buckets if b == "data"))
        s["open"].append(len(broker.open))
        s["opens"] += sum(broker.entries.values()) - n_before
        _, rw = rec.get("ai_reports/entry_watch_state.json")
        rec_rows = watch_rows(rw)
        s["rec_book"].append(len(rec_rows))
        a_set, b_set = set(rows), set(rec_rows)
        if a_set or b_set:
            s["overlap"].append(len(a_set & b_set) / len(a_set | b_set))
        t += args.step

    spread_cache.save()
    print(f"[replay] recorded inputs used {recorded.hits}, fetched/derived {recorded.misses}; "
          f"synthetic names served {len(synth.used)}")
    report(args, slots, broker, blocked, wall0)
    if args.fidelity:
        fidelity(args, snap_dir, slots, broker, t_start, t_end)
    return 0


def fidelity(args, snap_dir: Path, slots: dict, broker, t_start: float, t_end: float) -> dict:
    """How closely the replay of the code that ran reproduces what live did."""
    live = []
    fills = snap_dir / "fills.jsonl"
    if fills.exists():
        for line in open(fills):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("event") == "fill" and str(r.get("side")).lower() == "buy"
                    and t_start <= float(r.get("ts") or 0) <= t_end):
                live.append((float(r["ts"]), str(r["symbol"]).upper()))
    rep = sorted([(p["entry_ts"], p["symbol"]) for p in broker.closed + list(broker.open.values())])
    used, matched = set(), []
    for lt, ls in sorted(live):
        best = None
        for j, (rt, rsym) in enumerate(rep):
            if j in used or rsym != ls or abs(rt - lt) > 90:
                continue
            if best is None or abs(rt - lt) < abs(rep[best][0] - lt):
                best = j
        if best is not None:
            used.add(best)
            matched.append((ls, round(rep[best][0] - lt)))
    ov = [x for s in slots.values() for x in s.get("overlap", [])]
    out = {
        "day": args.day, "window": f"{args.start}-{args.end}", "sha": os.getenv("REPLAY_SHA"),
        "live_buys": len(live), "replay_opens": len(rep), "matched": len(matched),
        "recall": round(len(matched) / len(live), 3) if live else None,
        "precision": round(len(matched) / len(rep), 3) if rep else None,
        "book_overlap": round(sum(ov) / len(ov), 3) if ov else None,
        "matched_detail": matched,
        "live_only": sorted({s for _, s in live} - {s for s, _ in matched}),
    }
    print(f"\nFIDELITY {args.day}: live buys {out['live_buys']}, replay opens {out['replay_opens']}, "
          f"matched within 90s {out['matched']} (recall {out['recall']}, precision "
          f"{out['precision']}), book overlap {out['book_overlap']}")
    try:
        (snap_dir / "fidelity.json").write_text(json.dumps(out, indent=1))
    except OSError:
        pass
    return out


def report(args, slots, broker, blocked, wall0) -> None:
    def avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    ex = "live exits" if args.exits == "live" else f"hold {args.hold_min:g}m"
    print(f"\nREPLAY {args.day} {args.start}-{args.end}  code {os.getenv('REPLAY_SHA', '?')[:8]}"
          f"  overrides {args.set or '-'}  {ex}")
    print(f"{'slot':>5} {'book':>5} {'rec':>5} {'armable':>7} {'data':>5} {'opens':>5} {'open':>5}")
    all_open = []
    for k in sorted(slots):
        s = slots[k]
        all_open += s["open"]
        print(f"{datetime.fromtimestamp(s['t'], ET):%H:%M} {avg(s['book']):5.1f} {avg(s['rec_book']):5.1f} "
              f"{avg(s['armable']):7.1f} {avg(s['data']):5.1f} {s['opens']:5d} {avg(s['open']):5.2f}")
    n = len(broker.closed)
    rets = [p["ret"] for p in broker.closed]
    from collections import Counter
    why = Counter(p.get("reason") for p in broker.closed)
    syn = sum(1 for p in broker.closed + list(broker.open.values()) if p.get("synthetic"))
    holds = [(p["exit_ts"] - p["entry_ts"]) / 60 for p in broker.closed]
    n_slots = max(1, len(slots))
    print(f"\nopens {sum(broker.entries.values())} ({sum(broker.entries.values()) / n_slots:.2f}/10m)  "
          f"avg open {avg(all_open):.2f}  >=1 {sum(1 for x in all_open if x >= 1) / max(1, len(all_open)):.0%}  "
          f">=2 {sum(1 for x in all_open if x >= 2) / max(1, len(all_open)):.0%}  "
          f"closed {n}  gross/trade {1e4 * avg(rets):+.1f} bp  mean hold {avg(holds):.1f}m")
    print(f"exits {dict(why)}  opens on synthetic data {syn}")
    if blocked:
        print(f"blocked network: {dict(sorted(blocked.items(), key=lambda kv: -kv[1]))}")
    print(f"wall time {_real_time.time() - wall0:.0f}s")
    out = {"day": args.day, "start": args.start, "end": args.end, "sha": os.getenv("REPLAY_SHA"),
           "overrides": args.set, "hold_min": args.hold_min,
           "closed": broker.closed, "open_at_end": list(broker.open.values()),
           "slots": {str(k): {kk: (avg(v) if isinstance(v, list) else v)
                              for kk, v in s.items()} for k, s in slots.items()},
           "blocked": blocked}
    path = Path(args.out) if args.out else Path(os.environ.get("REPLAY_WORK", ".")) / "result.json"
    path.write_text(json.dumps(out, indent=1, default=str))
    print(f"result: {path}")


def main(argv=None) -> int:
    args = parse_args(argv)
    if os.getenv("REPLAY_EXPORTED") != "1":
        return export_and_reexec(args)
    return run_inside(args)


if __name__ == "__main__":
    raise SystemExit(main())
