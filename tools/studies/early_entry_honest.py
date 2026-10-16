#!/usr/bin/env python3
"""Honest early-entry test. Spec: docs/studies/early_entry_honest_prereg.json.

early_entry_sim.py bought the desk's own fills N minutes early, which assumes
we knew at M-N that the signal was coming. This study fires rules that read
only completed bars (pre-square, ignition; full square as information) on
every board name, and scores each fire against a random LATER minute of the
same name (entry+1..+60) held the same way. Same-hour controls sit on the
run-up a momentum rule fires after and score it ~20 bp low on pure noise.

  --fetch    build ai_reports/early_entry_honest/{day}.pkl (mini only: Alpaca keys)
  (default)  print the data inventory and the half split; no returns
  --score    score. Do not run until the prereg has a result-skeptic READY.

Rule flags, onset/refractory, markout and control choice are pure and tested
in tests/test_early_entry_honest.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.environ.get("EEH_ROOT", ROOT)   # run a staged copy against the mini's repo
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

ET = ZoneInfo("America/New_York")
OUT_DIR = os.path.join(ROOT, "ai_reports", "early_entry_honest")
LEDGER_DIR = os.path.join(ROOT, "ai_reports", "decision_ledger")
OUTCOMES = os.path.join(ROOT, "ai_reports", "outcomes.jsonl")

PRE_THR, OB_THR, TIGHT = 35.0, 20.0, 15.0
EPS = 0.05          # rte_direction_eps: live "rising" = above the previous minute + eps
IGN_RISE, IGN_VOL_X, IGN_BACK, IGN_VOL_N = 0.0025, 2.0, 3, 20
REFRACT = 15
WIN_START, WIN_END = 9 * 60 + 50, 15 * 60 + 30
RTH_START, RTH_END = 9 * 60 + 30, 16 * 60
HOLD = 10
CTRL_SPAN = 60
INFO_HOLDS = (5, 15, 30)
COST = 0.0010
LIFT_BAR = 0.0005
MIN_SIGNALS, MIN_DAYS = 100, 6
MAX_MISS = 0.05
MID_LEVEL, RSI_CAP, RSI_OS = -50.0, 65.0, 35.0
# Two pre-registered studies share this harness. Each grades its own rules;
# SQ is information in both.
STUDIES = {
    "pre_ign": {"rules": ("PRE", "IGN", "SQ"), "graded": ("PRE", "IGN"),
                "prereg": "early_entry_honest_prereg.json", "out": "result.md"},
    "mid_rsi2": {"rules": ("MID", "RSI2T", "MIDCAP", "SQ"), "graded": ("MID", "RSI2T"),
                 "prereg": "early_entry_mid_rsi2_prereg.json", "out": "result_mid_rsi2.md"},
    "connors": {"rules": ("CON", "SQ"), "graded": ("CON",),
                "prereg": "connors_green_prereg.json", "out": "result_connors.md"},
    "os_leave": {"rules": ("OSL", "SQ"), "graded": ("OSL",),
                 "prereg": "os_leave_prereg.json", "out": "result_os_leave.md"},
    "steep_turn": {"rules": ("STEEP", "MID", "SQ"), "graded": ("STEEP",),
                   "prereg": "steep_turn_prereg.json", "out": "result_steep_turn.md"},
    "confirm": {"rules": ("CONF1", "CONF2", "DIP1", "DIP2", "HELD1", "HELD2", "PRE", "SQ"),
                "graded": ("CONF1", "CONF2"),
                "prereg": "confirm_entry_prereg.json", "out": "result_confirm.md"},
}
RULES = STUDIES["pre_ign"]["rules"]
GRADED = STUDIES["pre_ign"]["graded"]
CFG = {
    "cm_rsi_length": 2,
    "rte_fast_length": 21,
    "rte_fast_ewm_span": 7,
    "rte_slow_native_length": 112,
    "rte_slow_ewm_span": 3,
    "rte_minute_grid": True,
    "rte_slow_timeframe": None,
}


# ── pure rule logic ──────────────────────────────────────────────────────────

def _fin(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def rising(s, i: int) -> bool:
    """Live _live_percent_r_line: smoothed line above the previous minute's by
    more than EPS. Rows are minutes (the frame is gridded first)."""
    return i >= 1 and _fin(s[i - 1]) and _fin(s[i]) and s[i] > s[i - 1] + EPS


def pre_flag(fast, slow, i: int) -> bool:
    f, s = fast[i], slow[i]
    if not (_fin(f) and _fin(s)):
        return False
    tight = abs(f - s) <= TIGHT + 1e-9
    full = f >= -OB_THR and s >= -OB_THR
    return f >= -PRE_THR and s >= -PRE_THR and tight and not full and rising(fast, i)


OS_LEAVE_BARS = 3   # ai_watch_os_leave_max_age_sec 180 s on 1-minute bars


def os_tri(fast, slow, i: int) -> bool:
    """Oversold triangle: both lines <= -(100 - OB_THR) (-80) and within TIGHT."""
    f, s = fast[i], slow[i]
    return _fin(f) and _fin(s) and f <= -100 + OB_THR + 1e-9 and s <= -100 + OB_THR + 1e-9 \
        and abs(f - s) <= TIGHT + 1e-9


def os_leave_flag(fast, slow, i: int) -> bool:
    """Live _os_leave_allows_buy at bar close: an oversold triangle on one of the previous OS_LEAVE_BARS minutes,
    this minute out of it (a line back above -80, not 'both deep but wide'), and fast %R rising."""
    f, s = fast[i], slow[i]
    if not (_fin(f) and _fin(s)) or os_tri(fast, slow, i):
        return False
    deep = f <= -100 + OB_THR + 1e-9 and s <= -100 + OB_THR + 1e-9
    if deep:
        return False                      # both still deep but wide: live 'exh_not_tight', not a leave
    if not any(os_tri(fast, slow, k) for k in range(max(0, i - OS_LEAVE_BARS), i)):
        return False
    return rising(fast, i)


STEEP_RISE, STEEP_BARS, STEEP_DIP, STEEP_LOOK = 15.0, 3, -50.0, 10


def steep_flag(fast, slow, i: int) -> bool:
    """Operator 10/16 (TGL chart: 'sharp trending angles', not 'smooth sideways'): fast %R up >= STEEP_RISE points over
    the last STEEP_BARS minutes, the slow line rising, and fast %R was <= STEEP_DIP within the last STEEP_LOOK minutes
    (the turn comes out of a dip). Completed bars only."""
    if i < STEEP_LOOK or not (_fin(fast[i]) and _fin(fast[i - STEEP_BARS])):
        return False
    if fast[i] - fast[i - STEEP_BARS] < STEEP_RISE - 1e-9 or not rising(slow, i):
        return False
    return any(_fin(fast[k]) and fast[k] <= STEEP_DIP + 1e-9 for k in range(i - STEEP_LOOK, i))


def sq_flag(fast, slow, i: int) -> bool:
    f, s = fast[i], slow[i]
    if not (_fin(f) and _fin(s)):
        return False
    return f >= -OB_THR and s >= -OB_THR and abs(f - s) <= TIGHT + 1e-9 and rising(fast, i)


def mid_flag(fast, slow, rsi, i: int, cap: bool = False) -> bool:
    """Live _mid_rise_allows_buy at bar close: fast crosses up through -50 from the
    previous minute while the slow line rises. Live has no RSI term (the CM RSI-2
    cap is admission-only, config.py ai_watch_arm_cm_rsi_max); cap=True is the
    information-only MIDCAP variant."""
    if i < 1 or not (_fin(fast[i - 1]) and _fin(fast[i])):
        return False
    if not (fast[i - 1] <= MID_LEVEL < fast[i] and rising(slow, i)):
        return False
    return not cap or (_fin(rsi[i]) and rsi[i] <= RSI_CAP)


def rsi2_turn_flag(fast, rsi, i: int) -> bool:
    """CM RSI-2 was <= 35 on the previous minute and turns up, with fast %R rising."""
    if i < 1 or not (_fin(rsi[i - 1]) and _fin(rsi[i])):
        return False
    return rsi[i - 1] <= RSI_OS and rsi[i] > rsi[i - 1] and rising(fast, i)


CON_SMA_LONG, CON_SMA_SHORT, CON_RSI = 200, 5, 10.0


def con_flags(closes, rsi) -> list[bool]:
    """Connors RSI-2 'green' per bar, as ai_entry_watch.live_cm_rsi: close > SMA(200) and close < SMA(5)
    and RSI-2 < 10, on the same grid-filled IEX series (SMA over the last 200 / 5 rows, this bar included).
    False until 200 rows exist."""
    n = len(closes)
    pre, bad = [0.0], [0]
    for c in closes:
        pre.append(pre[-1] + (c if _fin(c) else 0.0))
        bad.append(bad[-1] + (0 if _fin(c) else 1))
    out = []
    for i in range(n):
        c, r = closes[i], rsi[i]
        if (i + 1 < CON_SMA_LONG or not (_fin(c) and _fin(r))
                or bad[i + 1] - bad[i + 1 - CON_SMA_LONG] > 0):     # a missing close in the window (skeptic #4)
            out.append(False)
            continue
        sma_l = (pre[i + 1] - pre[i + 1 - CON_SMA_LONG]) / CON_SMA_LONG
        sma_s = (pre[i + 1] - pre[i + 1 - CON_SMA_SHORT]) / CON_SMA_SHORT
        out.append(c > sma_l and c < sma_s and r < CON_RSI)
    return out


CON_PRIOR_BARS = 220
CON_PRIOR_MAX_MISS = 0.05  # a day with more board names lacking prior bars is excluded from CON (skeptic round 2 #5)


def prior_miss_share(data: dict) -> float:
    n = len(data.get("board") or {})
    pr = data.get("iex_prior") or {}
    return (sum(1 for s_ in data["board"] if pr.get(s_) is None) / n) if n else 1.0      # live symbol_ohlc loads the last 220 raw IEX bars (alpaca_api, config ai bars limit)


def rsi2_series(closes: list[float]) -> list[float]:
    """Wilder RSI-2 over the whole series, aligned to closes (index 0 = None), as ai_entry_watch.cm_rsi_series."""
    out: list = [None]
    up = down = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        g, l_ = (d if d > 0 else 0.0), (-d if d < 0 else 0.0)
        if i == 1:
            up, down = g, l_
        else:
            up, down = 0.5 * g + 0.5 * up, 0.5 * l_ + 0.5 * down
        out.append(100.0 if down == 0 else 0.0 if up == 0 else 100.0 - 100.0 / (1.0 + up / down))
    return out


def con_live_series(prior_frame, today_frame) -> tuple[list[int], list[bool]]:
    """The live CON series (skeptic round 1 #1-2): RAW IEX bars, no gap fill, the last CON_PRIOR_BARS bars of
    earlier sessions prepended for warm-up (as symbol_ohlc), RSI-2 and both averages over the joined series.
    Returns (today's bar minutes, flags for today's bars)."""
    pc = [float(x) for x in prior_frame["close"]][-CON_PRIOR_BARS:] if prior_frame is not None else []
    tc = [float(x) for x in today_frame["close"]]
    closes = pc + tc
    rsi = rsi2_series(closes)
    flags = con_flags(closes, [float("nan") if r is None else r for r in rsi])
    minutes = [_et_min(t.timestamp()) for t in today_frame["time"]]
    return minutes, flags[len(pc):]


def ign_flag(minutes, closes, vols, i: int, vol_at: dict | None = None) -> bool:
    """IEX bar i. Missing RTH minutes in the volume window count as 0 volume.
    The window is m-20..m-1 only, so a whole-day vol_at map reads nothing later."""
    m = minutes[i]
    if m - IGN_VOL_N < RTH_START:
        return False
    if vol_at is None:
        vol_at = dict(zip(minutes, vols))
    window = [vol_at.get(mm, 0.0) for mm in range(m - IGN_VOL_N, m)]
    mean = sum(window) / IGN_VOL_N
    if mean <= 0 or vols[i] < IGN_VOL_X * mean:
        return False
    ref = None
    for k in range(i - 1, -1, -1):
        if minutes[k] <= m - IGN_BACK:
            ref = closes[k]
            break
    return ref is not None and ref > 0 and closes[i] / ref - 1.0 >= IGN_RISE


def fires(minutes, flags, board: set, start: int = WIN_START, end: int = WIN_END,
          refract: int = REFRACT) -> tuple[list[int], int]:
    """Onset fires (flag true, previous bar false) in the window, on the board,
    with a refractory. Returns (signal minutes, off-board onsets)."""
    out, off = [], 0
    last = None
    for i, m in enumerate(minutes):
        if not flags[i] or (i > 0 and flags[i - 1]):
            continue
        if not (start <= m <= end):
            continue
        if last is not None and m - last < refract:
            continue
        if m not in board:
            off += 1
            continue
        out.append(m)
        last = m
    return out, off


def markout(sip: dict, entry_min: int, hold: int = HOLD) -> float | None:
    """Gross return: open of entry_min to close of entry_min + hold - 1."""
    if entry_min + hold - 1 >= RTH_END:
        return None
    a, b = sip.get(entry_min), sip.get(entry_min + hold - 1)
    if a is None or b is None or not a[0]:
        return None
    return b[1] / a[0] - 1.0


def _rng(*parts) -> random.Random:
    key = "|".join(str(p) for p in parts) + "|early-honest-v1"
    return random.Random(int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big"))


def control_minute(sym, day, rule, sip: dict, board: set, entry_min: int,
                   hold: int = HOLD) -> int | None:
    """A random later entry minute of the same name: entry_min+1 .. entry_min+CTRL_SPAN,
    RTH, with its own exit bar. Later only: an earlier window can contain the
    run-up that made the rule fire, which scores any momentum trigger about
    20 bp below a random minute on pure noise (null calibration 2026-10-13).
    `board` is unused on purpose: staying on the board depends on the path."""
    elig = [k for k in range(entry_min + 1, entry_min + CTRL_SPAN + 1)
            if RTH_START <= k and k + hold - 1 < RTH_END and k in sip and (k + hold - 1) in sip]
    if not elig:
        return None
    return _rng(sym, day, entry_min, rule).choice(elig)


def day_equal(by_day: dict) -> tuple[float | None, float | None, int, int]:
    means = [statistics.mean(v) for v in by_day.values() if v]
    n = sum(len(v) for v in by_day.values())
    if len(means) < 3:
        return (statistics.mean(means) if means else None), None, n, len(means)
    mu, sd = statistics.mean(means), statistics.stdev(means)
    return mu, (mu / (sd / math.sqrt(len(means))) if sd > 0 else None), n, len(means)


def markout_close(sip: dict, entry_min: int, hold: int = HOLD) -> float | None:
    """Information only: enter at the CLOSE of entry_min (IEX bar m is surely
    published by then), exit at the same exit bar."""
    if entry_min + hold - 1 >= RTH_END:
        return None
    a, b = sip.get(entry_min), sip.get(entry_min + hold - 1)
    if a is None or b is None or not a[1]:
        return None
    return b[1] / a[1] - 1.0


def cross_control(sym, day, rule, boards: dict, sips: dict, signal_min: int,
                  hold: int = HOLD) -> float | None:
    """Information only: a random OTHER board name at the same minute, same hold."""
    names = sorted(n for n, b in boards.items()
                   if n != sym and signal_min in b and n in sips
                   and markout(sips[n], signal_min + 1, hold) is not None)
    if not names:
        return None
    pick = _rng(sym, day, signal_min, rule, "cross").choice(names)
    return markout(sips[pick], signal_min + 1, hold)


def rule_verdict(halves: list[str]) -> str:
    """PASS only if both halves pass. A half that is UNDERPOWERED makes the rule
    UNDERPOWERED (unless the other half is a real FAIL): that is no evidence
    either way, and it does not trigger the prereg's if_fail."""
    if all(h == "PASS" for h in halves):
        return "PASS"
    if "FAIL" in halves:
        return "FAIL"
    return "UNDERPOWERED"


def half_verdict(lift, t, net, n, days) -> str:
    if n < MIN_SIGNALS or days < MIN_DAYS or lift is None or t is None or net is None:
        return "UNDERPOWERED"
    return "PASS" if lift >= LIFT_BAR and t >= 2.0 and net > 0 else "FAIL"


# ── data ─────────────────────────────────────────────────────────────────────

def _et_min(ts: float) -> int:
    d = datetime.fromtimestamp(float(ts), ET)
    return d.hour * 60 + d.minute


def load_board(day: str) -> dict[str, set]:
    path = os.path.join(LEDGER_DIR, f"{day}.jsonl")
    board: dict[str, set] = defaultdict(set)
    if not os.path.exists(path):
        return board
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
            if r.get("stage") not in ("watch", "arm") or not r.get("symbol"):
                continue
            board[str(r["symbol"]).upper()].add(_et_min(float(r["ts"])))
        except (ValueError, TypeError, KeyError):
            continue
    return board


def ledger_days() -> list[str]:
    if not os.path.isdir(LEDGER_DIR):
        return []
    return sorted(f[:-6] for f in os.listdir(LEDGER_DIR)
                  if f.endswith(".jsonl") and len(f) == 16)


def _fetch_feed(cl, syms: list[str], day: str, feed: str, errors: list | None = None,
                start=None, end=None) -> dict:
    import pandas as pd
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    got: dict = {}
    bad: set[str] = set()
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        df = None
        for attempt in range(5):
            chunk = [c for c in chunk if c not in bad]
            if not chunk:
                break
            try:
                df = cl.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=chunk, timeframe=TimeFrame(1, TimeFrameUnit.Minute),
                    start=(start or d.replace(hour=4)).astimezone(timezone.utc),
                    end=(end or d.replace(hour=16, minute=5)).astimezone(timezone.utc),
                    limit=5_000_000, extended_hours=True,
                    feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX)).df
                break
            except Exception as e:  # noqa: BLE001 — 429s retried, then counted as misses
                msg = str(e)
                m = re.search(r"invalid symbol: ([A-Za-z0-9.\-]+)", msg)
                if m:   # one junk ledger symbol (e.g. SMOKE1) rejects the whole chunk
                    bad.add(m.group(1).upper())
                    print(f"  {day} {feed}: dropping invalid symbol {m.group(1)}", file=sys.stderr)
                    continue
                print(f"  {day} {feed} try {attempt}: {msg[:80]}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))
        if df is None and chunk and errors is not None:
            errors.append((day, feed, len(chunk)))      # every try failed: caller must not cache
        if df is not None and not df.empty:
            if not isinstance(df.index, pd.MultiIndex):
                df = pd.concat({chunk[0]: df}, names=["symbol"])
            for s in df.index.get_level_values("symbol").unique():
                sub = df.xs(s, level="symbol").sort_index().reset_index()
                sub = sub.rename(columns={sub.columns[0]: "time"})
                got[str(s).upper()] = sub[["time", "open", "high", "low", "close", "volume"]]
        time.sleep(0.4)
    return got


def fetch_day(day: str) -> dict:
    import bars
    cl = bars.client()
    if cl is None:
        raise SystemExit("no Alpaca data client (config/secrets.json); run on the mini")
    board = load_board(day)
    syms = sorted(board)
    data = {"day": day, "board": dict(board),
            "iex": _fetch_feed(cl, syms, day, "iex"),
            "sip": _fetch_feed(cl, syms, day, "sip")}
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, f"{day}.pkl"), "wb") as f:
        pickle.dump(data, f)
    return data


def load_days() -> tuple[list[dict], list[tuple[str, str]]]:
    """Usable day caches, and (day, reason) for each excluded one."""
    if not os.path.isdir(OUT_DIR):
        return [], []
    keep, excluded = [], []
    for fn in sorted(os.listdir(OUT_DIR)):
        if not fn.endswith(".pkl"):
            continue
        data = pickle.load(open(os.path.join(OUT_DIR, fn), "rb"))
        n = len(data["board"])
        if n == 0:
            excluded.append((data["day"], "empty board"))
            continue
        miss = {f: sum(1 for s in data["board"] if s not in data[f]) / n for f in ("iex", "sip")}
        if max(miss.values()) > MAX_MISS:
            excluded.append((data["day"], f"miss iex {miss['iex']:.0%} sip {miss['sip']:.0%}"))
            continue
        keep.append(data)
    return keep, excluded


def split(days: list[str]) -> str | None:
    return days[(len(days) - 1) // 2] if days else None


# ── scoring ──────────────────────────────────────────────────────────────────

def grid_fill(frame):
    """One row per minute: a missing minute inside a gap of <= MINUTE_GAP_FILL_MAX
    gets a flat bar at the previous close and 0 volume, as signals._minute_grid_pr
    does internally. Then row i-1 is minute m-1, which is what live 'rising' reads."""
    import pandas as pd
    import signals
    gap_max = int(getattr(signals, "MINUTE_GAP_FILL_MAX", 15))
    t = pd.to_datetime(frame["time"], utc=True).dt.floor("min")
    rows, prev_t, prev_c = [], None, None
    for ts, o, h, l, c, v in zip(t, frame["open"], frame["high"], frame["low"],
                                 frame["close"], frame["volume"]):
        if prev_t is not None:
            gap = int((ts - prev_t).total_seconds() // 60)
            if 1 < gap <= gap_max:
                for k in range(1, gap):
                    rows.append((prev_t + pd.Timedelta(minutes=k), prev_c, prev_c, prev_c, prev_c, 0.0))
        rows.append((ts, float(o), float(h), float(l), float(c), float(v)))
        prev_t, prev_c = ts, float(c)
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])


# os_leave_prereg (skeptic 10/15 #1): warm both %R lines on the prior sessions' IEX bars, as live loads 5 days, then
# keep only the day's minutes. Off for the older studies so their recorded results stay reproducible.
WARM_LINES = False


def name_day_series(iex_frame, prior_frame=None) -> dict:
    import pandas as pd
    import signals
    if WARM_LINES and prior_frame is not None and len(prior_frame):
        day0 = pd.to_datetime(iex_frame["time"], utc=True).min()
        both = pd.concat([prior_frame[["time", "open", "high", "low", "close", "volume"]],
                          iex_frame[["time", "open", "high", "low", "close", "volume"]]])
        both = both.assign(time=pd.to_datetime(both["time"], utc=True)).sort_values("time")
        both = both.drop_duplicates("time", keep="last").reset_index(drop=True)
        df = signals.compute_percent_r_exhaustion(grid_fill(both), CFG)
        df = signals.compute_cm_rsi_lower(df, CFG)
        df = df[pd.to_datetime(df["time"], utc=True) >= day0].reset_index(drop=True)
    else:
        df = signals.compute_percent_r_exhaustion(grid_fill(iex_frame), CFG)
        df = signals.compute_cm_rsi_lower(df, CFG)
    minutes = [_et_min(t.timestamp()) for t in df["time"]]
    con_m, con_f = con_live_series(prior_frame, iex_frame)
    return {
        "con_minutes": con_m,
        "con_flags": con_f,
        "minutes": minutes,
        "close": [float(x) for x in df["close"]],
        "vol": [float(x) for x in df["volume"]],
        "fast": [float(x) for x in df["s_percentR"]],
        "slow": [float(x) for x in df["l_percentR"]],
        "rsi": [float(x) for x in df["cm_rsi"]],
    }


def sip_map(frame) -> dict:
    return {_et_min(t.timestamp()): (float(o), float(c))
            for t, o, c in zip(frame["time"], frame["open"], frame["close"])}


CONF_STATS: Counter = Counter()    # per k: decided / ties / missing / off-board at the decision minute


def confirm_sigs(minutes, closes, base_sigs, k: int, held: bool = True, vols=None, board: set | None = None,
                 tag: str = "") -> list[int]:
    """Confirm-then-buy (confirm_entry_prereg amended_1): a base signal at completed bar sm is CONFIRMED if the
    IEX close of bar sm+k is >= the IEX close of bar sm (the price held); the returned signal minute is sm+k, so
    the harness enters at the SIP open of sm+k+1. held=False returns the signals that dipped (what the rule skips).
    No decision (dropped, counted): a missing or non-finite bar at sm or sm+k, or a zero-volume bar there
    (grid_fill's flat no-print filler, which would otherwise read as a tie = held). The name must still be on
    the board at the decision minute sm+k (live cannot buy a name that left the watch); counted when not."""
    idx = {m: i for i, m in enumerate(minutes)}
    out = []
    for sm in base_sigs:
        i, j = idx.get(sm), idx.get(sm + k)
        if i is None or j is None or not (_fin(closes[i]) and _fin(closes[j])) or (
                vols is not None and (not vols[i] or not vols[j])):
            if held:
                CONF_STATS[f"{tag}k{k}_missing"] += 1
            continue
        if board is not None and (sm + k) not in board:
            if held:
                CONF_STATS[f"{tag}k{k}_off_board_at_decision"] += 1
            continue
        tie = abs(closes[j] - closes[i]) <= 1e-12
        if held:
            CONF_STATS[f"{tag}k{k}_decided"] += 1
            CONF_STATS[f"{tag}k{k}_ties"] += tie
            CONF_STATS[f"{tag}k{k}_held"] += closes[j] >= closes[i] - 1e-12
        if (closes[j] >= closes[i] - 1e-12) == held:
            out.append(sm + k)
    return out


def rule_fires(ser: dict, board: set, key: tuple = ("inventory",)) -> dict[str, tuple[list[int], int]]:
    m, f, s = ser["minutes"], ser["fast"], ser["slow"]
    n = len(m)
    vol_at = dict(zip(m, ser["vol"]))
    flags = {
        "PRE": [pre_flag(f, s, i) for i in range(n)],
        "SQ": [sq_flag(f, s, i) for i in range(n)],
        "IGN": [ign_flag(m, ser["close"], ser["vol"], i, vol_at) for i in range(n)],
        "MID": [mid_flag(f, s, ser["rsi"], i) for i in range(n)],
        "MIDCAP": [mid_flag(f, s, ser["rsi"], i, cap=True) for i in range(n)],
        "RSI2T": [rsi2_turn_flag(f, ser["rsi"], i) for i in range(n)],
        "OSL": [os_leave_flag(f, s, i) for i in range(n)],
        "STEEP": [steep_flag(f, s, i) for i in range(n)],
    }
    out = {r: fires(m, flags[r], board) for r in RULES if r in flags}
    if any(r[:4] in ("CONF", "DIP1", "DIP2", "HELD") for r in RULES):
        # The off-board count returned for CONF/DIP/HELD is 0: PRE's own off-board onsets are counted under PRE,
        # and the at-decision off-board drops are in CONF_STATS.
        pre, _pre_off = fires(m, flags["PRE"], board)
        vol = ser["vol"]
        rnd = []
        if any(r.startswith("HELD") for r in RULES):
            # Null for a universe 1-2 minute effect (skeptic 10/16): random board minutes, same name and day,
            # as many as PRE onsets, same held test and the same later control.
            cand = [x for x in sorted(board) if WIN_START <= x <= WIN_END]
            rnd = sorted(_rng(*key, "held-null").sample(cand, min(len(pre), len(cand)))) if pre else []
        for k in (1, 2):
            if f"CONF{k}" in RULES:
                out[f"CONF{k}"] = (confirm_sigs(m, ser["close"], pre, k, True, vol, board, "CONF"), 0)
            if f"DIP{k}" in RULES:
                out[f"DIP{k}"] = (confirm_sigs(m, ser["close"], pre, k, False, vol, board, "DIP"), 0)
            if f"HELD{k}" in RULES:
                out[f"HELD{k}"] = (confirm_sigs(m, ser["close"], rnd, k, True, vol, board, "HELD"), 0)
    if "CON" in RULES:
        out["CON"] = fires(ser["con_minutes"], ser["con_flags"], board)
    return out


def inventory_onsets(data: dict) -> dict:
    """Fire counts only (no returns): onsets with every minute treated as on board."""
    out = Counter()
    for sym in data["board"]:
        iex = data["iex"].get(sym)
        if iex is None or len(iex) < 30:
            continue
        try:
            ser = name_day_series(iex)
        except Exception:  # noqa: BLE001
            continue
        for r, (sigs, _off) in rule_fires(ser, set(ser["minutes"])).items():
            out[r] += len(sigs)
    return dict(out)


def desk_fills(days: set) -> dict:
    out = defaultdict(list)
    if not os.path.exists(OUTCOMES):
        return out
    for line in open(OUTCOMES):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        et = r.get("entry_time")
        if not et or r.get("entry_path") != "watch" or not r.get("total_qty"):
            continue
        day = datetime.fromtimestamp(float(et), ET).strftime("%Y-%m-%d")
        if day in days:
            out[(str(r["symbol"]).upper(), day)].append(float(et))
    return out


MEASURED_COST = 0.0003    # ~3 bp round trip on $10+ names (focus-is-day-trading, 10/9); PROMISING tier only


def score(datas: list[dict]) -> dict:
    CONF_STATS.clear()
    days = sorted(d["day"] for d in datas)
    cut = split(days)
    lift = {r: {h: defaultdict(list) for h in "AB"} for r in RULES}
    net = {r: {h: defaultdict(list) for h in "AB"} for r in RULES}
    gross = {r: {h: defaultdict(list) for h in "AB"} for r in RULES}
    info = {r: {H: defaultdict(list) for H in INFO_HOLDS} for r in RULES}
    x_info = {r: {k: defaultdict(list) for k in ("cross", "close_entry", "net20")} for r in RULES}
    overlap = Counter()
    board_mean = defaultdict(list)
    drops = Counter()
    fire_log = defaultdict(list)          # (sym, day) -> [(rule, signal_min)]
    artifact = defaultdict(list)
    con_excluded = []
    for data in datas:
        day = data["day"]
        half = "A" if day <= cut else "B"
        con_off_day = "CON" in RULES and prior_miss_share(data) > CON_PRIOR_MAX_MISS
        if con_off_day:
            con_excluded.append((day, round(prior_miss_share(data), 3)))
        if WARM_LINES and prior_miss_share(data) > CON_PRIOR_MAX_MISS:
            # os_leave_prereg amended: a day whose names lack warm-up bars is EXCLUDED, not scored cold.
            con_excluded.append((day, round(prior_miss_share(data), 3)))
            continue
        sips_day = {s_: sip_map(f) for s_, f in data["sip"].items() if f is not None}
        for sym, board in data["board"].items():
            iex, sipf = data["iex"].get(sym), data["sip"].get(sym)
            if iex is None or sipf is None or len(iex) < 30:
                drops["no_bars"] += 1
                continue
            if "CON" in RULES and (data.get("iex_prior") or {}).get(sym) is None:
                drops["CON_no_prior_bars"] += 1
            try:
                ser = name_day_series(iex, (data.get("iex_prior") or {}).get(sym))
            except Exception:  # noqa: BLE001 — counted, not fatal
                drops["indicator_fail"] += 1
                continue
            sip = sips_day.get(sym) or sip_map(sipf)
            for bm in sorted(board):
                if WIN_START <= bm <= WIN_END:
                    g0 = markout(sip, bm + 1)
                    if g0 is not None:
                        board_mean[day].append(g0)
            for rule, (sigs, off) in rule_fires(ser, board, (sym, day)).items():
                if rule == "CON" and con_off_day:
                    continue
                drops[f"{rule}_off_board"] += off
                for sm in sigs:
                    fire_log[(sym, day)].append((rule, sm))
                    em = sm + 1
                    g = markout(sip, em)
                    if g is None:
                        drops[f"{rule}_no_bar"] += 1
                        continue
                    km = control_minute(sym, day, rule, sip, board, em)
                    if km is None:
                        drops[f"{rule}_no_control"] += 1
                        continue
                    gc = markout(sip, km)
                    overlap[rule] += km < em + HOLD
                    overlap[rule + "_n"] += 1
                    lift[rule][half][day].append(g - gc)
                    x_info[rule]["net20"][day].append(g - 2 * COST)
                    xc = cross_control(sym, day, rule, data["board"], sips_day, sm)
                    if xc is not None:
                        x_info[rule]["cross"][day].append(g - xc)
                    gce, gcc = markout_close(sip, em), markout_close(sip, km)
                    if gce is not None and gcc is not None:
                        x_info[rule]["close_entry"][day].append(gce - gcc)
                    net[rule][half][day].append(g - COST)
                    gross[rule][half][day].append(g)
                    for H in INFO_HOLDS:
                        a = markout(sip, em, H)
                        kc = control_minute(sym, day, rule, sip, board, em, H)
                        if a is not None and kc is not None:
                            info[rule][H][day].append(a - markout(sip, kc, H))
            # artifact demo: AGY's shift on rule-free rising board minutes
            rng = _rng(sym, day, "artifact")
            cand = [m for m in sorted(board) if WIN_START <= m <= WIN_END]
            for m in rng.sample(cand, min(5, len(cand))):
                now, back = sip.get(m), sip.get(m - 5)
                ent, early, ex = sip.get(m + 1), sip.get(m - 4), sip.get(m + HOLD)
                if None in (now, back, ent, early, ex) or now[1] / back[1] - 1 < 0.0014:
                    continue
                artifact[day].append(ex[1] / early[0] - ex[1] / ent[0])
    res = {"days": days, "cut": cut, "drops": dict(drops), "rules": {}, "con_excluded": con_excluded,
           "conf_stats": dict(CONF_STATS)}
    for r in RULES:
        halves = {}
        for h in "AB":
            ml, tl, n, nd = day_equal(lift[r][h])
            mn = day_equal(net[r][h])[0]
            mg = day_equal(gross[r][h])[0]
            halves[h] = {"lift": ml, "t": tl, "net": mn, "gross": mg, "n": n, "days": nd,
                         "verdict": half_verdict(ml, tl, mn, n, nd)}
        pooled = defaultdict(list)
        for h in "AB":
            for dkey, v in lift[r][h].items():
                pooled[dkey].extend(v)
        pl, pt, pn, pd_ = day_equal(pooled)
        for h in "AB":
            g3 = {dk: [x - MEASURED_COST for x in v] for dk, v in gross[r][h].items()}
            halves[h]["net3"] = day_equal(g3)[0]
        g3all = defaultdict(list)
        for h in "AB":
            for dk, v in gross[r][h].items():
                g3all[dk].extend(x - MEASURED_COST for x in v)
        net3 = day_equal(g3all)[0]
        promising = (pl is not None and pt is not None and pl > 0 and pt >= 1.5 and net3 is not None and net3 > 0
                     and all(halves[h]["lift"] is not None and halves[h]["lift"] > 0
                             and halves[h]["n"] >= MIN_SIGNALS and halves[h]["days"] >= MIN_DAYS for h in "AB"))
        res["rules"][r] = {
            "pooled": {"lift": pl, "t": pt, "n": pn, "days": pd_, "net3": net3, "promising": promising},
            "halves": halves,
            "verdict": rule_verdict([halves[h]["verdict"] for h in "AB"]) if r in GRADED else "INFO",
            "info_holds": {H: day_equal(info[r][H])[:3] for H in INFO_HOLDS},
            "info_x": {k: day_equal(v)[:3] for k, v in x_info[r].items()},
            "ctrl_overlap": (overlap[r] / overlap[r + "_n"]) if overlap[r + "_n"] else None,
        }
    # confirm_entry_prereg amended_1.null_held (skeptic round 2): CONFk is PROMISING only above its HELDk null.
    for k in (1, 2):
        c, h = res["rules"].get(f"CONF{k}"), res["rules"].get(f"HELD{k}")
        if c is not None and h is not None:
            cl_, hl_ = c["pooled"]["lift"], h["pooled"]["lift"]
            c["pooled"]["promising"] = bool(c["pooled"]["promising"] and cl_ is not None and hl_ is not None
                                            and cl_ > hl_)
    res["board_mean"] = day_equal(board_mean)[:3]
    res["lead"] = lead_report(fire_log, desk_fills(set(days)))
    res["artifact"] = day_equal(artifact)[:3]
    return res


def lead_report(fire_log: dict, fills: dict) -> dict:
    out = {}
    for rule in RULES:
        leads, n_fills = [], 0
        hit, n_fire = 0, 0
        for key, tss in fills.items():
            sigs = [m for r, m in fire_log.get(key, []) if r == rule]
            for ts in tss:
                n_fills += 1
                fm = _et_min(ts)
                # signal bar m closes at m+1; usable lead = fill minute - (m + 1)
                prior = [m for m in sigs if 0 <= fm - (m + 1) <= 15]
                if prior:
                    leads.append(fm - (max(prior) + 1))
        for key, fl in fire_log.items():
            fms = [_et_min(ts) for ts in fills.get(key, [])]
            for r, m in fl:
                if r != rule:
                    continue
                n_fire += 1
                hit += any(0 <= fm - (m + 1) <= 10 for fm in fms)
        out[rule] = {
            "fills": n_fills, "with_prior_fire": len(leads),
            "median_lead_min": statistics.median(leads) if leads else None,
            "share_lead_ge3": (sum(1 for x in leads if x >= 3) / len(leads)) if leads else None,
            "fires": n_fire, "hit_rate_10m": (hit / n_fire) if n_fire else None,
        }
    return out


def _bp(x) -> str:
    return "—" if x is None else f"{x * 1e4:+.1f} bp"


def report(res: dict) -> str:
    L = [f"# Honest early entry — {res['days'][0]} .. {res['days'][-1]}", "",
         f"days {len(res['days'])}, half A ends {res['cut']}. Cost {COST * 1e4:.0f} bp RT, hold {HOLD} min.", ""]
    L += ["Scored signals per half: " + ", ".join(
        f"{r} A {v['halves']['A']['n']} / B {v['halves']['B']['n']}" for r, v in res["rules"].items()), ""]
    L += ["| rule | half | n | days | gross | net | lift | t | verdict |",
          "|---|---|---:|---:|---:|---:|---:|---:|---|"]
    for r, v in res["rules"].items():
        for h, x in v["halves"].items():
            t = "—" if x["t"] is None else f"{x['t']:+.2f}"
            L.append(f"| {r} | {h} | {x['n']} | {x['days']} | {_bp(x['gross'])} | {_bp(x['net'])} "
                     f"| {_bp(x['lift'])} | {t} | {x['verdict']} |")
    L += ["", "**Verdicts:** " + ", ".join(f"{r} {v['verdict']}" for r, v in res["rules"].items()), ""]
    if any(r.startswith("CONF") for r in res["rules"]):
        L += ["**PROMISING tier** (pooled day-equal lift > 0 with t >= 1.5, lift > 0 in each half, pooled net at "
              f"{MEASURED_COST * 1e4:.0f} bp > 0, >= {MIN_SIGNALS} signals and {MIN_DAYS} days per half):", ""]
        for r, v in res["rules"].items():
            p = v["pooled"]
            t = "—" if p["t"] is None else f"{p['t']:+.2f}"
            L.append(f"- {r}: pooled lift {_bp(p['lift'])} (t {t}, n {p['n']}, {p['days']} days); net@3bp pooled "
                     f"{_bp(p['net3'])}, A {_bp(v['halves']['A']['net3'])}, B {_bp(v['halves']['B']['net3'])}"
                     f" -> {'PROMISING' if p['promising'] else 'not promising'}")
        pre = res["rules"].get("PRE", {}).get("pooled", {}).get("lift")
        for k in (1, 2):
            c = res["rules"].get(f"CONF{k}", {}).get("pooled", {}).get("lift")
            h = res["rules"].get(f"HELD{k}", {}).get("pooled", {}).get("lift")
            if c is not None and pre is not None:
                L.append(f"- CONF{k} minus PRE (pooled lift difference, the value of waiting on the live arm): "
                         f"{_bp(c - pre)}; CONF{k} minus HELD{k} (confirmation of PRE beyond a universe "
                         f"{k}-minute effect): {_bp(None if h is None else c - h)}")
        cs = res.get("conf_stats") or {}
        for tag in ("CONF", "HELD"):
            for k in (1, 2):
                dcd = cs.get(f"{tag}k{k}_decided", 0)
                if dcd:
                    L.append(f"- {tag} k={k}: decided {dcd}, held {cs.get(f'{tag}k{k}_held', 0) / dcd:.0%}, ties "
                             f"{cs.get(f'{tag}k{k}_ties', 0) / dcd:.0%}, dropped missing/no-print "
                             f"{cs.get(f'{tag}k{k}_missing', 0)}, off board at decision "
                             f"{cs.get(f'{tag}k{k}_off_board_at_decision', 0)}")
        L.append("")
    if "OSL" in res["rules"]:
        L += [f"OSL warm-up coverage: days excluded (> {CON_PRIOR_MAX_MISS:.0%} of board names lacking prior bars) "
              f"{res.get('con_excluded') or []}", ""]
    if "CON" in res["rules"]:
        L += [f"CON prior-bar coverage: name-days without prior bars {res['drops'].get('CON_no_prior_bars', 0)}; "
              f"days excluded (> {CON_PRIOR_MAX_MISS:.0%} of board names lacking them) {res.get('con_excluded') or []}", ""]
    L += [
          "### Information only", "", "Lift at other holds (day-equal, t):"]
    for r, v in res["rules"].items():
        L.append(f"- {r}: " + "; ".join(
            f"{H}m {_bp(mu)} (t {'—' if t is None else f'{t:+.2f}'}, n {n})"
            for H, (mu, t, n) in v["info_holds"].items()))
    L += ["", "Lead before desk fills (latest fire within 15 min):"]
    for r, x in res["lead"].items():
        ml = "—" if x["median_lead_min"] is None else f"{x['median_lead_min']:.0f} min"
        sh = "—" if x["share_lead_ge3"] is None else f"{x['share_lead_ge3']:.0%}"
        hr = "—" if x["hit_rate_10m"] is None else f"{x['hit_rate_10m']:.0%}"
        L.append(f"- {r}: {x['with_prior_fire']}/{x['fills']} fills had a prior fire; median lead {ml}; "
                 f"lead >= 3 min {sh}; {x['fires']} fires, {hr} followed by a desk fill within 10 min")
    L += ["", "Other controls and costs (lift unless noted; day-equal, t, n):"]
    for r, v in res["rules"].items():
        ov = "—" if v["ctrl_overlap"] is None else f"{v['ctrl_overlap']:.0%}"
        L.append(f"- {r}: " + "; ".join(
            f"{k} {_bp(mu)} (t {'—' if t is None else f'{t:+.2f}'}, n {n})"
            for k, (mu, t, n) in v["info_x"].items()) + f"; later-control overlaps the hold {ov}")
    mu, t, n = res["board_mean"]
    L.append(f"- every board minute, gross {HOLD}m: {_bp(mu)} (t {'—' if t is None else f'{t:+.2f}'}, n {n})")
    mu, t, n = res["artifact"]
    L += ["", f"Artifact demo (shift-5 lift on rule-free rising board minutes): {_bp(mu)} "
          f"(t {'—' if t is None else f'{t:+.2f}'}, n {n})", "", f"Drops: {res['drops']}"]
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--fetch-prior", action="store_true",
                    help="add iex_prior (last 220 IEX bars before each day, 10 calendar days back) to cached days")
    ap.add_argument("--days", nargs="*", help="limit to these days (default: every ledger day)")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--study", choices=sorted(STUDIES), default="pre_ign")
    ap.add_argument("--warm", action="store_true",
                    help="warm both %%R lines on prior-session bars (live loads 5 days); result file gets _warm")
    a = ap.parse_args()
    global RULES, GRADED
    RULES, GRADED = STUDIES[a.study]["rules"], STUDIES[a.study]["graded"]
    global WARM_LINES
    WARM_LINES = a.study in ("os_leave", "confirm") or a.warm
    if a.fetch_prior:
        import bars
        from datetime import timedelta
        cl = bars.client()
        for fn in sorted(os.listdir(OUT_DIR)):
            if not fn.endswith(".pkl"):
                continue
            pth = os.path.join(OUT_DIR, fn)
            data = pickle.load(open(pth, "rb"))
            if (data.get("iex_prior") and prior_miss_share(data) <= CON_PRIOR_MAX_MISS) or (a.days and data["day"] not in set(a.days)):
                continue
            d0 = datetime.strptime(data["day"], "%Y-%m-%d").replace(tzinfo=ET)
            errs: list = []
            got = _fetch_feed(cl, sorted(data["board"]), data["day"], "iex", errs,
                              start=(d0 - timedelta(days=10)).replace(hour=4), end=d0.replace(hour=4))
            if errs:
                print(f"{data['day']}: prior fetch failed {errs}; not cached")
                continue
            data["iex_prior"] = {k: v.tail(CON_PRIOR_BARS).reset_index(drop=True) for k, v in got.items()}
            with open(pth, "wb") as f:
                pickle.dump(data, f)
            print(f"{data['day']}: prior bars for {len(got)}/{len(data['board'])} names")
        return
    if a.fetch:
        have = set(f[:-4] for f in os.listdir(OUT_DIR) if f.endswith(".pkl")) if os.path.isdir(OUT_DIR) else set()
        have -= {d for d, _why in load_days()[1]}       # refetch days excluded for misses
        for day in a.days or ledger_days():
            if day in have:
                continue
            d = fetch_day(day)
            print(f"{day}: board {len(d['board'])}, iex {len(d['iex'])}, sip {len(d['sip'])}")
        return
    datas, excluded = load_days()
    if a.days:
        datas = [d for d in datas if d["day"] in set(a.days)]
    days = sorted(d["day"] for d in datas)
    for d in sorted(datas, key=lambda x: x["day"]):
        n = len(d["board"])
        print(f"  {d['day']}: board {n}, iex miss {sum(1 for s_ in d['board'] if s_ not in d['iex']) / n:.1%}, "
              f"sip miss {sum(1 for s_ in d['board'] if s_ not in d['sip']) / n:.1%}, "
              f"onsets in window before the board filter {inventory_onsets(d)}")
    print(f"usable days {len(days)}: {days}")
    print(f"excluded: {excluded}")
    print(f"half A ends {split(days)}")
    if not a.score:
        print("(inventory only; --score after the prereg has a result-skeptic READY)")
        return
    res = score(datas)
    md = report(res)
    print(md)
    os.makedirs(OUT_DIR, exist_ok=True)
    out_name = STUDIES[a.study]["out"]
    if a.warm and a.study != "os_leave":
        out_name = out_name.replace(".md", "_warm.md")       # keep the original cold-start result
    with open(os.path.join(OUT_DIR, out_name), "w") as f:
        f.write(md + "\n")


if __name__ == "__main__":
    main()
