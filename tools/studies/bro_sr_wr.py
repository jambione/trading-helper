#!/usr/bin/env python3
"""bro_sr_wr.py — Trader Bro call-outs as BOOK names traded on the operator's S/R + %R signal.

Exactly per docs/studies/bro_sr_wr_prereg.json (registered 3114397, amended through cd8aa4d, all before any data).
Every rule below names the prereg clause it implements; ambiguities are resolved in the constants block and
listed in RESOLUTIONS (also written to result.json) so a reviewer can check each one.

Phases (on the mini, AFTER HOURS ONLY: every request shares the desk's Alpaca budget):
  clean     archive -> bleed drop -> OCR merge (60 s) -> OCR twins -> ticker validation (assets, ETF/index,
            warrant/unit/right) -> sessions                                        -> WORK/clean.json
  bars      IEX + SIP 1-min bars (prior session + day, 04:00-close) per name-day, SIP daily bars for ADV20
  score     >= 1 SIP trade, actionability, book intervals, group assignment, PASS series, PRIMARY,
            CONTROL_primary, CONTROL_secondary, information cells, statistics, verdict -> WORK/result.json
  report    WORK/report.md from result.json (power block first, then n / entry minutes, then means)
  all       clean, bars, score, report
  forward DAY          score one new session (>= 2026-10-08) and append it to WORK/forward/
  chartcheck SYM DAY HH:MM [iex|sip]   print the port's order blocks / %R at that grid minute close (ET)

Every API result is cached under WORK/cache (resumable). Requests are paced at <= 150/min. A failed request is
never cached and is logged in WORK/fetch_fails.json; score refuses to write a result while any failure remains.

USAGE (mini):  .venv/bin/python tools/studies/bro_sr_wr.py all
"""
from __future__ import annotations

import collections
import hashlib
import json
import math
import os
import random
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from zoneinfo import ZoneInfo

REPO = os.environ.get("REPO") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (os.path.join(REPO, "tools"), REPO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

WORK = os.environ.get("BRO_WORK") or os.path.join(REPO, "ai_reports", "bro_sr_wr")
ARCHIVE = os.environ.get("BB_LIVE_JSONL") or os.path.join(REPO, "ai_reports", "bb_live.jsonl")
ET = ZoneInfo("America/New_York")
DATA = "https://data.alpaca.markets"
TRADING = ("https://paper-api.alpaca.markets", "https://api.alpaca.markets")

# ------------------------------------------------------------------ fixed by the prereg (no_retuning)
HIST_HI = "2026-10-06"          # data: "41 sessions 2026-08-10..10-06 at registration"
FORWARD_START = "2026-10-08"    # forward: "logged on every session from 2026-10-08"
SEED = 53                       # design: controls drawn with seed 53
MERGE_SEC = 60                  # data: OCR re-reads of the same ticker within 60 s
LAT_SEC = 2                     # entry: first SIP trade at or after t + 2 s
LAT_INFO_SEC = 30               # information: latency +30 s
HOLD_SEC = 15 * 60              # primary_outcome: net15
MFE_SEC = 30 * 60               # information: MFE30 / MAE30
WIN_LO = 9 * 60 + 31            # design: grid minute closes in 09:30-15:30 (first RTH close is 09:31; see R2)
WIN_HI = 15 * 60 + 30
HOLD_TO = 15 * 60 + 50          # information: hold to 15:50
FAST, SLOW, SLOPE, WARM = (21, 7), (112, 3), 3, 112   # features_at_R.WR_trending
OB_LEN, OB_SHOW, RESIST_PCT = 10, 3, 0.3              # features_at_R.SR_ok
T_MIN_PX, T_MAX_SPR = 10.0, 0.0005                    # groups.T_tradeable
S_MIN_PX, S_MAX_PX, S_MAX_SPR = 2.0, 10.0, 0.01       # groups.S_floor_candidate
TICK_RT, TICK_UNDER = 0.01, 5.0                       # entry: + 1 cent round trip when price < $5
MEGA_ADV, ADV_DAYS, ADV_MIN_DAYS = 5e9, 20, 5         # groups.exclusions (R9)
HALT_MIN = 5                                          # halt inference (R6)
QUOTE_LOOKBACK = 300                                  # NBBO: last quote in the 300 s before t (exec_report model)
FALLBACK = (0, -1, 1, -2, 2)                          # CONTROL_primary: same minute, then +/-1, +/-2 (R12)
SECONDARY_TRIES = 5                                   # CONTROL_secondary candidates tried (information only)
MDE_K, UNDER_BP, MIN_N, CONTRAST_BP, FAIL_T, Z_BONF = 2.84, 150.0, 60, 50.0, -2.0, 2.24   # power / pass
RPM = float(os.environ.get("ALPACA_RPM", "150"))
OK_EX = {"NYSE", "NASDAQ", "AMEX", "ARCA"}

BLEED = re.compile(r"Starting|tunnel|watchdog|engine|http|https|Cloudflare", re.I)
FUZZY = re.compile(r"[il|]g\s*[ft]loat|tloat|avoi|not\s+enough\s+vol|p\s*&\s*d|penny", re.I)
ETF_NAME = re.compile(r"\b(ETFs?|ETNs?|FUNDS?|ISHARES|SPDR|PROSHARES|DIREXION|INDEX|VANGUARD|QQQ|LEVERAGED|INVERSE|"
                      r"[23]X)\b", re.I)
WARRANT_NAME = re.compile(r"\b(WARRANTS?|RIGHTS?)\b", re.I)
UNIT_NAME = re.compile(r"\bUNITS?\b", re.I)
MLP_UNITS = re.compile(r"COMMON\s+UNITS?|LIMITED\s+PARTNERSHIP|\bL\.?P\.?\b", re.I)
INDEX_SYMS = {"SPY", "QQQ", "IWM", "DIA", "VOO", "IVV", "VTI", "SPX", "NDX", "VIX", "UVXY", "VXX"}

CHART_CHECK_2026_10_07 = (   # verbatim from the prereg (cbd07d1); report.md header caveat
    "Operator TradingView screenshots (1-min, extended hours, LuxAlgo Order Blocks defaults) vs the port on SIP "
    "bars: JAGX 2026-10-06 07:00-07:20 TV shows NO order blocks (the red boxes are %R Trend Exhaustion overbought "
    "boxes, not OBs) while the port finds 3 (bull OB 5.52-5.75 known 07:15, breaker 6.00-6.64, bull OB 6.13-6.30 "
    "known 07:19); VEEA 2026-10-05 06:45-07:15 TV none, port 2 small zones (4.54-4.77); XHG 2026-10-06 08:30-09:00 "
    "TV shows a bullish OB carried from 10/02 (~1.85-2.00) the port cannot build (SIP has 5 one-minute bars on "
    "10/02, IEX 0). Verdict: MISMATCH on thin premarket names (bar coverage differs by feed); the port matched to "
    "the cent on liquid names on 10/05 (ETHA, EWZ, IHI, XP). Consequence: for group S the SR_ok half of PASS is "
    "'SR as the port computes on the desk's feed', not the operator's TradingView zones; this is stated in the "
    "result doc.")

RESOLUTIONS = {
    "R1_signal_time": "grid minute t = the CLOSE of the 1-min bar starting t-60 (bar start + 60 s); entry after t+2 s",
    "R2_window": "PRIMARY t ranges over grid closes 09:31..15:30 ET (09:31 is the first RTH close) with t >= a0",
    "R3_assignment_minute": "a0 = max(ceil-to-minute(R), 09:31); price = last regular SIP trade at or before a0, "
                            "spread = SIP NBBO at a0 (last valid quote in the prior 300 s)",
    "R4_warmup": ">= 112 gridded rows up to and including t-3 (so both slope endpoints have a full 112 window); "
                 "skipped minutes counted",
    "R5_point_in_time_grid": "desk grid fills gaps of 2..15 min with flat bars; inside a longer gap the minutes up to "
                             "14 after the last bar are evaluated with point-in-time trailing flats and never "
                             "enter later history (whether a gap is filled depends on a later bar)",
    "R6_halt": "halted at X if SIP 1-min bars miss >= 5 consecutive minutes covering X's minute, every one of the "
               "5 minutes before the gap printed, and a bar resumes later that day (halt_reopen_study rule)",
    "R7_regular_prints": "trades are tape_check.BAD_CONDITIONS-filtered regular prints (odd lots excluded); this "
                         "also drops extended-hours prints (condition T), so a premarket call's call-moment entry is "
                         "its own cell 'premarket call -> first regular RTH print', costed at the NBBO at that entry "
                         "trade's time; call_moment_* cells hold RTH calls only",
    "R8_twins": "a merge group (same ticker within 60 s of the group's first row) with both actionable and "
                "non-actionable rows is non-actionable AS OF ITS OWN TIME: before the first actionable line it is "
                "skipped (the next actionable call is R); after it, it is an exit line at its own time; a name-day "
                "with no actionable line and a twin is counted as ocr_twin_nonactionable (skeptic 2bfcf31 BLOCKER)",
    "R9_adv": "ADV20 = mean SIP daily close x volume over the 20 sessions ending D-1 (>= 5 required, else not mega); "
              "MEGA = would-be T name with ADV20 >= $5B",
    "R10_t_crit": "2.24-equivalent = Student-t quantile at the one-sided p of z=2.24 (0.01255) with df = sessions "
                  "with >= 1 entry in that half - 1 (pooled contrast: all sessions with a pair - 1)",
    "R11_underpowered": "UNDERPOWERED if MDE of PRIMARY in either half OR of the pooled contrast > 150 bp",
    "R12_control_fallback": "fallback order same minute, -1, +1, -2, +2; candidate must be warm and not PASS",
    "R13_T_skip": "a T (or MEGA) PASS minute whose entry spread > 0.05% is skipped and the search continues",
    "R14_T_historical": "T is declared UNDERPOWERED for the historical run: verdict FAIL if t <= -2 else UNDERPOWERED",
    "R15_halves": "historical halves = alternate archive sessions (index in the sorted list of sessions with any "
                  "archive row, <= 2026-10-06); forward halves = alternate forward sessions",
    "R16_fuzzy": "fuzzy OCR list read literally: [il|]g [ft]loat, tloat, avoi, not enough vol, p&d, penny",
    "R17_bleed_case": "terminal-bleed tokens matched case-insensitively",
    "R18_sip_trade_day": ">= 1 SIP trade that day = at least one SIP 1-min bar 04:00-close",
    "R19_s_price": "S is $2 <= price < $10 (T takes >= $10)",
    "R20_lat30_cost": "+30 s cell: quote at t+30, entry at t+32",
    "R22_phrase_regex": "hod = test/tests/testing [the] hod | near [the] hod | nhod (a bare 'hod' or 'break hod' "
                        "is 'other'); pop = pop/pops/popping/popped; micro = micro* | low float; news = pr | news "
                        "('following pr' is covered by pr); first match wins in that order",
    "R23_wr_only": "S only: PASS = WR_trending (warm, strict 3-min slopes); CONTROL_primary candidates must be warm "
                   "and not WR_trending at the control minute; same fallback, entry, exit, cost",
    "R21_p27": "9/27 method: entry = open of the first SIP 1-min bar after 'at' (the bar containing 'at' is skipped), "
               "exit = close of the 15th bar, cost from the NBBO at the entry bar's start; labelled as using 'at'",
}


def P_(*a):
    print(*a, flush=True)


def jload(p, d=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return d


def jsave(p, obj):
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, p)


# ------------------------------------------------------------------ time helpers
def et_day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")


def et_ts(day: str, hh: int, mm: int = 0) -> float:
    d = datetime.strptime(day, "%Y-%m-%d")
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=ET).timestamp()


def et_min(ts: float) -> int:
    d = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET)
    return d.hour * 60 + d.minute


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET).strftime("%H:%M:%S")


def ceil_min(ts: float) -> float:
    return math.ceil(float(ts) / 60.0) * 60.0


def iso(ts: float) -> str:
    d = datetime.fromtimestamp(float(ts), timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if float(ts) % 1 else d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str) -> float:
    s = str(s)
    if s.endswith("Z"):
        s = s[:-1]
    elif s.endswith("+00:00"):
        s = s[:-6]
    if "." in s:
        a, f = s.split(".", 1)
        s = f"{a}.{(f + '000000')[:6]}"
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


# ------------------------------------------------------------------ market-hours guard
def in_market_hours(now=None) -> bool:
    now = (now or datetime.now(timezone.utc)).astimezone(ET)
    return now.weekday() < 5 and 9 * 60 <= now.hour * 60 + now.minute < 16 * 60 + 30


def market_hours_guard(now=None):
    """Every request shares the desk's Alpaca budget: refuse 09:00-16:30 ET on weekdays (re-checked per unit)."""
    if in_market_hours(now):
        raise SystemExit("refusing to run 09:00-16:30 ET on a weekday (shares the desk's Alpaca budget); "
                         "run after hours")


# ------------------------------------------------------------------ actionability (data clause)
def _desk_actionable(text) -> bool:
    from ai_entry_watch import _bb_call_is_actionable
    return bool(_bb_call_is_actionable(text))


def actionable(text) -> bool:
    """ai_entry_watch._bb_call_is_actionable AND none of the prereg's fuzzy OCR variants (study-local)."""
    return _desk_actionable(text) and not FUZZY.search(str(text or ""))


# ------------------------------------------------------------------ cleaning
def clean_rows(rows: list[dict], keep_day=None):
    """Bleed drop, OCR merge and twins. Returns ({(day, sym): [merged line, ...]}, counts).

    A merged line: {sym, day, unix, at, text, n_rows, actionable, twin}. Groups do not chain: a row joins the open
    group only when it is within MERGE_SEC of that group's FIRST row."""
    counts = collections.Counter()
    counts["rows_total"] = len(rows)
    by = collections.defaultdict(list)
    for r in rows:
        try:
            unix = float(r.get("unix"))
        except (TypeError, ValueError):
            counts["drop_bad_row"] += 1
            continue
        sym = str(r.get("ticker") or "").upper().strip().lstrip("$")
        text = str(r.get("text") or "")
        if not sym:
            counts["drop_bad_row"] += 1
            continue
        if BLEED.search(text):
            counts["drop_bleed"] += 1
            continue
        day = et_day(unix)
        if keep_day is not None and not keep_day(day):
            counts["out_of_range_rows"] += 1
            continue
        try:
            at = float(r.get("at")) if r.get("at") not in (None, "") else None
        except (TypeError, ValueError):
            at = None
        by[(day, sym)].append({"unix": unix, "at": at, "text": text, "said": r.get("said")})
    out = {}
    for (day, sym), rs in by.items():
        rs.sort(key=lambda x: x["unix"])
        groups = []
        for r in rs:
            if groups and r["unix"] - groups[-1][0]["unix"] <= MERGE_SEC:
                groups[-1].append(r)
                counts["merged_rereads"] += 1
            else:
                groups.append([r])
        lines = []
        for g in groups:
            flags = [actionable(x["text"]) for x in g]
            lines.append({"sym": sym, "day": day, "unix": g[0]["unix"], "at": g[0]["at"], "said": g[0]["said"],
                          "text": g[0]["text"], "n_rows": len(g), "actionable": all(flags),
                          "twin": any(flags) and not all(flags)})
        out[(day, sym)] = lines
    counts["namedays_after_merge"] = len(out)
    return out, counts


def asset_reason(sym: str, a: dict | None) -> str | None:
    """None when the ticker is a valid listed common stock per the prereg's data/exclusions clauses."""
    from ticker_filters import is_common, is_levered_etp
    if a is None:
        return "not_an_alpaca_asset"
    if str(a.get("class") or "") != "us_equity":
        return "not_us_equity"
    name = str(a.get("name") or "")
    if sym in INDEX_SYMS or ETF_NAME.search(name) or is_levered_etp(sym, name):
        return "etf_or_index"
    if str(a.get("exchange") or "").upper() not in OK_EX:
        return "exchange_not_allowed"
    if not is_common(sym) or WARRANT_NAME.search(name) or (UNIT_NAME.search(name) and not MLP_UNITS.search(name)):
        return "warrant_unit_right"
    return None


def book_of(lines: list[dict]):
    """(reason, book), point in time (R8). Book: R = first actionable line's capture time; exit = first
    non-actionable line after R. An OCR twin group (a non-actionable near-copy within 60 s) is non-actionable as of
    its own time: before the first actionable line it is skipped like any other non-actionable line; after it, it
    is an exit line at its own time. It never reaches back to remove an earlier call."""
    first = next((x for x in lines if x["actionable"]), None)
    if first is None:
        return ("ocr_twin_nonactionable" if any(x["twin"] for x in lines) else "no_actionable_call"), None
    ex = next((x for x in lines if x["unix"] > first["unix"] and not x["actionable"]), None)
    return None, {"R": first["unix"], "at": first["at"], "text": first["text"],
                  "exit_unix": ex["unix"] if ex else None, "exit_text": ex["text"] if ex else None,
                  "exit_is_twin": bool(ex and ex["twin"]),
                  "twins_before_R": sum(1 for x in lines if x["twin"] and x["unix"] < first["unix"])}


def on_book(nd: dict, t: float, keep_exit: bool = False) -> bool:
    """ai_entry_watch rule: the newest line before t decides; an exit/avoid line received before t ends the name."""
    if t <= nd["R"]:
        return False
    if not keep_exit and nd.get("exit_unix") is not None and nd["exit_unix"] < t:
        return False
    return True


# ------------------------------------------------------------------ desk grid, %R and order blocks
def window_rows(rows, day: str, prior: str | None, cal: dict) -> list:
    """Bars (prior session + day) whose start lies in 04:00..close ET of their own session."""
    keep = []
    for d in (prior, day):
        if d is None or d not in cal:
            continue
        lo, hi = et_ts(d, 4), cal[d][1]
        keep += [list(r[:6]) if len(r) >= 6 else list(r[:5]) + [0] for r in rows if lo <= r[0] < hi]
    keep.sort(key=lambda r: r[0])
    out, seen = [], set()
    for r in keep:
        m = int(r[0] // 60 * 60)
        if m not in seen:
            seen.add(m)
            out.append([float(m)] + [float(x) for x in r[1:6]])
    return out


def desk_grid(rows) -> list:
    """signals._minute_grid_pr's fill: each minute missing inside a gap of 2..MINUTE_GAP_FILL_MAX minutes is a flat
    bar at the previous close. Rows: [start_ts, o, h, l, c(, v)] -> [start_ts, o, h, l, c, real]."""
    from signals import MINUTE_GAP_FILL_MAX
    out = []
    prev = None
    for r in rows:
        ts = float(r[0])
        if prev is not None:
            gap = int((ts - prev) // 60)
            if 1 < gap <= MINUTE_GAP_FILL_MAX:
                c = out[-1][4]
                for m in range(1, gap):
                    out.append([prev + 60 * m, c, c, c, c, False])
        out.append([ts, float(r[1]), float(r[2]), float(r[3]), float(r[4]), True])
        prev = ts
    return out


def features(grid, want=None):
    """fast/slow %R via the desk's signals._minute_grid_pr and SR_ok via tools/order_blocks.py, per grid row.
    want: a row index whose charted blocks / overhead resistance are returned for the chart check."""
    import pandas as pd
    import signals
    from order_blocks import charted, order_blocks, overhead_resistance
    df = pd.DataFrame({"time": pd.to_datetime([g[0] for g in grid], unit="s", utc=True),
                       "high": [g[2] for g in grid], "low": [g[3] for g in grid], "close": [g[4] for g in grid]})
    fast = signals._minute_grid_pr(df, FAST[0], FAST[1])
    slow = signals._minute_grid_pr(df, SLOW[0], SLOW[1])
    if fast is None or slow is None:
        raise ValueError("desk %R grid rejected the bars")
    sr_ok, detail = [], None
    bars = [(g[0], g[1], g[2], g[3], g[4]) for g in grid]
    for i, blocks in order_blocks(bars, length=OB_LEN, bar_sec=60):
        ch = charted(blocks, show=OB_SHOW)
        res = overhead_resistance(ch, grid[i][4], within_pct=RESIST_PCT)
        sr_ok.append(not res)
        if want is not None and i == want:
            detail = {"charted": ch, "resist": res}
    return list(fast.to_numpy(dtype=float)), list(slow.to_numpy(dtype=float)), sr_ok, detail


def _state(i, fast, slow, sr_ok, close):
    if i - SLOPE < WARM - 1:
        return {"warm": False}
    f0, f1, s0, s1 = fast[i - SLOPE], fast[i], slow[i - SLOPE], slow[i]
    wr = bool(all(not math.isnan(x) for x in (f0, f1, s0, s1)) and f1 > f0 and s1 > s0)
    return {"warm": True, "sr_ok": bool(sr_ok[i]), "wr": wr, "pass": bool(sr_ok[i] and wr), "px": close,
            "fast": f1, "slow": s1}


def pass_series(rows, day_close_of=None) -> dict:
    """{grid minute close ts: state} for the bars, point in time (R5). state: warm, sr_ok, wr, pass."""
    from signals import MINUTE_GAP_FILL_MAX
    out = {}
    if not rows:
        return out
    grid = desk_grid(rows)
    fast, slow, sr, _ = features(grid)
    for i, g in enumerate(grid):
        out[g[0] + 60] = dict(_state(i, fast, slow, sr, g[4]), real=bool(g[5]))
    # trailing point-in-time flats inside long gaps (and after the last bar)
    real_idx = [i for i, g in enumerate(grid) if g[5]]
    for n, k in enumerate(real_idx):
        nxt = grid[real_idx[n + 1]][0] if n + 1 < len(real_idx) else None
        if nxt is not None and (nxt - grid[k][0]) // 60 <= MINUTE_GAP_FILL_MAX:
            continue
        end = day_close_of(grid[k][0]) if day_close_of else grid[k][0] + 15 * 60
        c = grid[k][4]
        flats = []
        for m in range(1, MINUTE_GAP_FILL_MAX):
            ts = grid[k][0] + 60 * m
            if (nxt is not None and ts >= nxt) or ts >= end:
                break
            flats.append([ts, c, c, c, c, False])
        if not flats:
            continue
        ext = grid[:k + 1] + flats
        f2, s2, sr2, _ = features(ext)
        for j in range(len(flats)):
            out[flats[j][0] + 60] = dict(_state(k + 1 + j, f2, s2, sr2, c), real=False)
    return out


def state_at_or_before(series: dict, t: float):
    ks = [k for k in series if k <= t]
    return series[max(ks)] if ks else None


# ------------------------------------------------------------------ market access
class FetchFail(Exception):
    pass


class AlpacaMarket:
    """Paced, cached Alpaca REST access. A failure raises FetchFail, is logged, and is never cached."""

    def __init__(self, work=WORK, guard=True):
        self.work, self.guard = work, guard
        self.caches, self.dirty, self.fails = {}, collections.Counter(), {}
        self._last, self.requests, self.H = 0.0, 0, None

    # -- plumbing
    def _headers(self):
        if self.H is None:
            from config import load_config
            c = load_config() or {}
            self.H = {"APCA-API-KEY-ID": c.get("api_key"), "APCA-API-SECRET-KEY": c.get("secret_key")}
        return self.H

    def _get(self, url, params):
        import requests
        if self.guard:
            market_hours_guard()
        err = None
        for att in range(5):
            w = self._last + 60.0 / RPM - time.monotonic()
            if w > 0:
                time.sleep(w)
            self._last = time.monotonic()
            self.requests += 1
            try:
                r = requests.get(url, params=params, headers=self._headers(), timeout=60)
                if r.status_code == 429:
                    err = "429"
                    time.sleep(15 * (att + 1))
                    continue
                if r.status_code >= 400:
                    err = f"http {r.status_code} {r.text[:120]}"
                    time.sleep(3 * (att + 1))
                    continue
                return r.json()
            except Exception as e:  # noqa: BLE001
                err = str(e)[:160]
                time.sleep(5 * (att + 1))
        raise FetchFail(f"{url} {json.dumps(params)[:200]}: {err}")

    def _cache(self, kind):
        if kind not in self.caches:
            self.caches[kind] = jload(os.path.join(self.work, "cache", f"{kind}.json"), {}) or {}
        return self.caches[kind]

    def flush(self):
        for kind, n in list(self.dirty.items()):
            if n:
                jsave(os.path.join(self.work, "cache", f"{kind}.json"), self.caches[kind])
                self.dirty[kind] = 0
        old = jload(os.path.join(self.work, "fetch_fails.json"), {}) or {}
        old.update(self.fails)
        for k in list(old):
            kind, key = k.split("|", 1)
            if key in self._cache(kind):
                old.pop(k)
        jsave(os.path.join(self.work, "fetch_fails.json"), old)

    def _cached(self, kind, key, fn):
        c = self._cache(kind)
        if key in c:
            return c[key]
        try:
            v = fn()
        except FetchFail as e:
            self.fails[f"{kind}|{key}"] = str(e)[:300]
            raise
        c[key] = v
        self.fails.pop(f"{kind}|{key}", None)
        self.dirty[kind] += 1
        if self.dirty[kind] >= 50:
            self.flush()
        return v

    # -- endpoints
    def assets(self) -> dict:
        def fn():
            out = {}
            for st in ("active", "inactive"):
                js = None
                for base in TRADING:
                    try:
                        js = self._get(f"{base}/v2/assets", {"status": st, "asset_class": "us_equity"})
                        break
                    except FetchFail:
                        continue
                if js is None:
                    raise FetchFail(f"assets {st}")
                for a in js:
                    if st == "inactive" and a["symbol"] in out:
                        continue
                    out[a["symbol"]] = {"class": a.get("class"), "exchange": a.get("exchange"),
                                        "name": a.get("name") or "", "status": a.get("status")}
            return out
        return self._cached("assets", "all", fn)

    def calendar(self, lo: str, hi: str) -> dict:
        def fn():
            for base in TRADING:
                try:
                    js = self._get(f"{base}/v2/calendar", {"start": lo, "end": hi})
                    return {d["date"]: [et_ts(d["date"], *map(int, d["open"].split(":"))),
                                        et_ts(d["date"], *map(int, d["close"].split(":")))] for d in js}
                except FetchFail:
                    continue
            raise FetchFail("calendar")
        return self._cached("calendar", f"{lo}|{hi}", fn)

    def minute_bars(self, sym, feed, start, end) -> list:
        def fn():
            out, token = [], None
            while True:
                p = {"symbols": sym, "timeframe": "1Min", "start": iso(start), "end": iso(end), "feed": feed,
                     "adjustment": "raw", "limit": 10000}
                if token:
                    p["page_token"] = token
                js = self._get(f"{DATA}/v2/stocks/bars", p)
                for b in (js.get("bars") or {}).get(sym) or []:
                    out.append([parse_ts(b["t"]), b["o"], b["h"], b["l"], b["c"], b.get("v", 0)])
                token = js.get("next_page_token")
                if not token:
                    return out
        return self._cached("bars", f"{sym}|{feed}|{int(start)}|{int(end)}", fn)

    def daily_adv(self, syms, day: str) -> dict:
        """{sym: ADV20 or None}: mean close x volume over the ADV_DAYS SIP daily bars ending the session before day."""
        syms = sorted(set(syms))

        def fn():
            d = datetime.strptime(day, "%Y-%m-%d")
            start = (d - timedelta(days=45)).strftime("%Y-%m-%d")
            end = (d - timedelta(days=1)).strftime("%Y-%m-%dT23:59:00Z")
            got = collections.defaultdict(list)
            for i in range(0, len(syms), 100):
                token = None
                while True:
                    p = {"symbols": ",".join(syms[i:i + 100]), "timeframe": "1Day", "start": start, "end": end,
                         "feed": "sip", "adjustment": "raw", "limit": 10000}
                    if token:
                        p["page_token"] = token
                    js = self._get(f"{DATA}/v2/stocks/bars", p)
                    for s, bl in (js.get("bars") or {}).items():
                        got[s] += [[parse_ts(b["t"]), b["c"], b["v"]] for b in bl or []]
                    token = js.get("next_page_token")
                    if not token:
                        break
            out = {}
            for s in syms:
                b = sorted(x for x in got.get(s, []) if et_day(x[0]) < day)[-ADV_DAYS:]
                out[s] = (sum(x[1] * x[2] for x in b) / len(b)) if len(b) >= ADV_MIN_DAYS else None
            return out
        return self._cached("adv", f"{day}|{hashlib.sha1(','.join(syms).encode()).hexdigest()[:16]}", fn)

    def _trade_scan(self, sym, t0, t1, sort):
        from tape_check import BAD_CONDITIONS
        token = None
        while True:
            p = {"start": iso(t0), "end": iso(t1), "feed": "sip", "limit": 1000, "sort": sort}
            if token:
                p["page_token"] = token
            js = self._get(f"{DATA}/v2/stocks/{sym}/trades", p)
            for r in js.get("trades") or []:
                if set(r.get("c") or []) & BAD_CONDITIONS:
                    continue
                return [parse_ts(r["t"]), float(r["p"])]
            token = js.get("next_page_token")
            if not token:
                return None

    def first_trade(self, sym, t0, t1):
        """[ts, px] of the first regular SIP trade in [t0, t1], or None (no data)."""
        if t1 <= t0:
            return None
        return self._cached("trades", f"{sym}|a|{t0:.6f}|{t1:.6f}", lambda: self._trade_scan(sym, t0, t1, "asc"))

    def last_trade(self, sym, t0, t1):
        """[ts, px] of the last regular SIP trade in [t0, t1], or None (no data)."""
        if t1 < t0:
            return None
        return self._cached("trades", f"{sym}|d|{t0:.6f}|{t1:.6f}", lambda: self._trade_scan(sym, t0, t1, "desc"))

    def quote(self, sym, t):
        """[bid, ask] of the last valid SIP NBBO in [t - 300 s, t] (exec_report.nbbo_at model), or None."""
        def fn():
            token = None
            while True:
                p = {"start": iso(t - QUOTE_LOOKBACK), "end": iso(t), "feed": "sip", "limit": 1000, "sort": "desc"}
                if token:
                    p["page_token"] = token
                js = self._get(f"{DATA}/v2/stocks/{sym}/quotes", p)
                for q in js.get("quotes") or []:
                    b, a = float(q.get("bp") or 0), float(q.get("ap") or 0)
                    if b > 0 and a >= b:
                        return [b, a]
                token = js.get("next_page_token")
                if not token:
                    return None
        return self._cached("quotes", f"{sym}|{t:.6f}", fn)


# ------------------------------------------------------------------ trade simulation
def is_halted(sip_rows, X: float) -> bool:
    """R6: >= HALT_MIN consecutive missing SIP minutes covering X's minute, prints in each of the HALT_MIN minutes
    before the gap, and a bar after it (the halt_reopen_study inference)."""
    have = {int(r[0] // 60) for r in sip_rows}
    m = int(X // 60)
    if m in have or not have:
        return False
    s = m
    while s - 1 not in have and s - 1 >= min(have):
        s -= 1
    e = m
    while e + 1 not in have and e + 1 <= max(have):
        e += 1
    if e + 1 > max(have) or s - 1 < min(have):
        return False
    if e - s + 1 < HALT_MIN:
        return False
    return all(s - k in have for k in range(1, HALT_MIN + 1))


def cost_frac(bid, ask, entry_px) -> tuple[float, float]:
    """(spread fraction of mid, total cost fraction): full SIP spread at the entry minute + 1 cent RT under $5."""
    mid = (bid + ask) / 2.0
    spr = (ask - bid) / mid
    return spr, spr + (TICK_RT / entry_px if entry_px < TICK_UNDER else 0.0)


def simulate(mkt, sym, t_entry_after, q, sip_rows, end_ts, counts, hold=HOLD_SEC, exit_at=None, tag=""):
    """One long trade. Entry = first SIP trade at/after t_entry_after; exit = last trade at/before entry + hold, or
    the first trade after the resume when halted then. Returns a row or None (counted)."""
    e = mkt.first_trade(sym, t_entry_after, end_ts)
    if e is None:
        counts[f"{tag}drop_no_entry_trade"] += 1
        return None
    X = exit_at if exit_at is not None else e[0] + hold
    halt = exit_at is None and is_halted(sip_rows, X)
    if halt:
        x = mkt.first_trade(sym, X, et_ts(et_day(X), 20))
        counts[f"{tag}halt_exit"] += 1
    else:
        x = mkt.last_trade(sym, e[0], X)
    if x is None:
        counts[f"{tag}drop_no_exit_trade"] += 1
        return None
    spr, cost = cost_frac(q[0], q[1], e[1])
    gross = x[1] / e[1] - 1
    return {"entry_ts": e[0], "entry_px": e[1], "exit_ts": x[0], "exit_px": x[1], "halt": halt,
            "spread_bp": spr * 1e4, "cost_bp": cost * 1e4, "gross_bp": gross * 1e4, "net_bp": (gross - cost) * 1e4}


def mfe_mae(sip_rows, entry_ts, entry_px, secs=MFE_SEC):
    w = [r for r in sip_rows if r[0] >= entry_ts // 60 * 60 and r[0] < entry_ts + secs]
    if not w:
        return None, None
    return (max(r[2] for r in w) / entry_px - 1) * 1e4, (min(r[3] for r in w) / entry_px - 1) * 1e4


# ------------------------------------------------------------------ group assignment
def assign_group(mkt, nd, counts):
    """groups.assignment: ONCE, from the SIP last trade and NBBO at a0 = first RTH 1-min close >= max(R, 09:30)."""
    day = nd["day"]
    a0 = max(ceil_min(nd["R"]), et_ts(day, WIN_LO // 60, WIN_LO % 60))
    nd["a0"] = a0
    if et_min(a0) > WIN_HI or a0 > nd["close"]:
        return "excl_call_after_1530"
    if nd.get("exit_unix") is not None and nd["exit_unix"] < a0:
        return "excl_exit_line_before_rth"
    tr = mkt.last_trade(nd["sym"], et_ts(day, 4), a0)
    q = mkt.quote(nd["sym"], a0)
    if q is None:
        return "excl_no_quote_at_assignment"
    if tr is None:
        return "excl_no_trade_at_assignment"
    px = tr[1]
    spr = (q[1] - q[0]) / ((q[0] + q[1]) / 2)
    nd["assign_px"], nd["assign_spread_bp"] = px, spr * 1e4
    if px < S_MIN_PX:
        return "excl_under_2"
    if px >= T_MIN_PX:
        if spr > T_MAX_SPR:
            return "excl_spread_above_cap_T"
        return "MEGA" if (nd.get("adv20") or 0) >= MEGA_ADV else "T"
    if spr > S_MAX_SPR:
        return "excl_spread_above_cap_S"
    return "S"


def _tcap(group):
    return T_MAX_SPR if group in ("T", "MEGA") else None


# ------------------------------------------------------------------ PRIMARY, controls
def window_ok(t):
    return WIN_LO <= et_min(t) <= WIN_HI and (t % 60 == 0)


def find_primary(mkt, nd, series, counts, keep_exit=False, tag="", latency=LAT_SEC, key="pass"):
    """design.PRIMARY_on_book: first grid close t >= a0 in 09:31-15:30, on the book, PASS, group filter held
    (T re-checks the 0.05% cap at entry, skips counted); entry first SIP trade >= t + 2 s."""
    cap = _tcap(nd["group"])
    for t in sorted(series):
        if t < nd["a0"] or not window_ok(t) or not on_book(nd, t, keep_exit) or t > nd["close"]:
            continue
        st = series[t]
        if not st["warm"]:
            counts[f"{tag}warmup_skipped_minutes"] += 1
            continue
        if not st[key]:
            continue
        q = mkt.quote(nd["sym"], t)
        if q is None:
            counts[f"{tag}drop_no_quote_at_entry"] += 1
            return None
        spr = (q[1] - q[0]) / ((q[0] + q[1]) / 2)
        if cap is not None and spr > cap:
            counts[f"{tag}T_spread_skip"] += 1
            continue
        tr = simulate(mkt, nd["sym"], t + latency, q, nd["sip"], nd["close"], counts, tag=tag)
        if tr is None:
            return None
        tr.update({"t": t, "quote": q})
        return tr
    counts[f"{tag}no_pass_minute"] += 1
    return None


def control_eligible(c, t, series_key="iex", key="pass"):
    if t < c.get("a0", float("inf")) or not window_ok(t) or not on_book(c, t) or t > c["close"]:
        return False
    st = c[series_key].get(t) if isinstance(c[series_key], dict) else None
    return bool(st and st["warm"] and not st[key])


def control_primary(mkt, nd, entry, day_nds, counts, series_key="iex", key="pass", tag="ctrl_"):
    """design.CONTROL_primary: a random (seed 53) OTHER called name that day, on the book at t, warm and NOT PASS at
    t, in the same group (T re-checks the cap); same entry/exit/cost rule. Fallback +/-1, +/-2 min; drops counted.
    Uses only information at the control minute."""
    t = entry["t"]
    cands = sorted((c for c in day_nds if c["sym"] != nd["sym"] and c.get("group") == nd["group"]),
                   key=lambda c: c["sym"])
    rng = random.Random(f"{SEED}|{nd['day']}|{nd['sym']}|{int(t)}")
    rng.shuffle(cands)
    cap = _tcap(nd["group"])
    for off in FALLBACK:
        tt = t + 60 * off
        for c in cands:
            if not control_eligible(c, tt, series_key, key):
                continue
            q = mkt.quote(c["sym"], tt)
            if q is None:
                counts["ctrl_candidate_no_quote"] += 1
                continue
            if cap is not None and (q[1] - q[0]) / ((q[0] + q[1]) / 2) > cap:
                counts["ctrl_candidate_T_spread_skip"] += 1
                continue
            tr = simulate(mkt, c["sym"], tt + LAT_SEC, q, c["sip"], c["close"], counts, tag=f"{nd['group']}_{tag}")
            if tr is None:
                continue
            tr.update({"t": tt, "sym": c["sym"], "offset_min": off})
            if off:
                counts["ctrl_fallback_used"] += 1
            return tr
    counts["ctrl_pair_dropped_no_control"] += 1
    return None


def control_secondary(mkt, nd, entry, counts):
    """design.CONTROL_on_book (information): a random (seed 53) not-PASS minute of the same name-day that starts
    AFTER the PRIMARY exit and by 15:30, on the book; same rule."""
    series = nd["iex"]
    mins = [t for t in sorted(series) if t > entry["exit_ts"] and window_ok(t) and on_book(nd, t)
            and t <= nd["close"] and series[t]["warm"] and not series[t]["pass"]]
    rng = random.Random(f"{SEED}|{nd['day']}|{nd['sym']}|secondary")
    rng.shuffle(mins)
    cap = _tcap(nd["group"])
    for t in mins[:SECONDARY_TRIES]:
        q = mkt.quote(nd["sym"], t)
        if q is None or (cap is not None and (q[1] - q[0]) / ((q[0] + q[1]) / 2) > cap):
            continue
        tr = simulate(mkt, nd["sym"], t + LAT_SEC, q, nd["sip"], nd["close"], counts, tag=f"{nd['group']}_ctrl2_")
        if tr:
            tr["t"] = t
            return tr
    counts["ctrl2_dropped_no_control"] += 1
    return None


# ------------------------------------------------------------------ information cells
def info_cells(mkt, nd, prim, counts):
    out = {}
    sym, day = nd["sym"], nd["day"]
    pm = nd["R"] < et_ts(day, 9, 30)
    # call moment: entry at R + 2 s, split by SR_ok / WR_trending at the last grid close <= R.
    # A premarket call's entry is the first regular RTH print (R7), so it is its own cell, costed at the NBBO at
    # that entry trade's time, not at R (skeptic 2bfcf31 item 2).
    st = state_at_or_before(nd["iex"], nd["R"]) or {"warm": False}
    split = {"premarket_call": pm, "warm": st.get("warm"), "sr_ok": st.get("sr_ok"), "wr": st.get("wr")}
    if pm:
        e = mkt.first_trade(sym, nd["R"] + LAT_SEC, nd["close"])
        q = mkt.quote(sym, e[0]) if e is not None else None
        if e is None:
            counts["info_pm_call_drop_no_entry_trade"] += 1
        elif q is None:
            counts["info_pm_call_no_quote"] += 1
        else:
            tr = simulate(mkt, sym, e[0], q, nd["sip"], nd["close"], counts, tag="info_pm_call_")
            if tr:
                tr.update(split)
                out["premarket_call_first_rth_print"] = tr
    else:
        q = mkt.quote(sym, nd["R"])
        if q is None:
            counts["info_call_no_quote"] += 1
        else:
            tr = simulate(mkt, sym, nd["R"] + LAT_SEC, q, nd["sip"], nd["close"], counts, tag="info_call_")
            if tr:
                tr.update(split)
                out["call_moment"] = tr
    # the 9/27 method, labelled as using 'at' (spoken minute)
    if nd.get("at"):
        rows = [r for r in nd["sip"] if et_day(r[0]) == day]
        i0 = next((i for i, r in enumerate(rows) if r[0] >= nd["at"] - 1), None)
        if i0 is not None and rows[i0][0] < nd["at"] and i0 + 1 < len(rows):
            i0 += 1
        if i0 is not None:
            j = min(i0 + 14, len(rows) - 1)
            E, X = rows[i0][1], rows[j][4]
            q27 = mkt.quote(sym, rows[i0][0])
            if q27 is not None and E > 0:
                spr, cost = cost_frac(q27[0], q27[1], E)
                out["p27_uses_at"] = {"entry_ts": rows[i0][0], "entry_px": E, "exit_px": X,
                                      "net_bp": ((X / E - 1) - cost) * 1e4, "cost_bp": cost * 1e4}
    if prim is None:
        return out
    # +30 s latency
    t = prim["t"]
    q30 = mkt.quote(sym, t + LAT_INFO_SEC)
    if q30 is not None:
        tr = simulate(mkt, sym, t + LAT_INFO_SEC + LAT_SEC, q30, nd["sip"], nd["close"], counts, tag="info_lat30_")
        if tr:
            out["lat30"] = tr
    # hold to 15:50
    x = mkt.last_trade(sym, prim["entry_ts"], et_ts(day, HOLD_TO // 60, HOLD_TO % 60))
    if x is not None:
        out["hold_1550"] = {"net_bp": ((x[1] / prim["entry_px"] - 1) * 1e4) - prim["cost_bp"]}
    mfe, mae = mfe_mae(nd["sip"], prim["entry_ts"], prim["entry_px"])
    out["mfe30_bp"], out["mae30_bp"] = mfe, mae
    return out


# ------------------------------------------------------------------ statistics
def _betacf(a, b, x):
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > 1e-300 else 1e-300
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-12:
            break
    return h


def _betai(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    bt = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1 - x) / b


def t_cdf(t, df):
    x = df / (df + t * t)
    p = 0.5 * _betai(df / 2.0, 0.5, x)
    return 1 - p if t >= 0 else p


def t_crit(df, z=Z_BONF):
    """R10: the Student-t value with the same one-sided tail as z (2.24 -> p = 0.01255) at df."""
    if df is None or df < 1:
        return None
    target = NormalDist().cdf(z)
    lo, hi = 0.0, 1000.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if t_cdf(mid, df) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clustered(rows, key="net_bp"):
    """Mean with day-clustered (CR1) SE; df = clusters - 1; MDE = 2.84 x SE."""
    xs = [(r["day"], r[key]) for r in rows if r.get(key) is not None]
    n = len(xs)
    if n == 0:
        return {"n": 0, "sessions": 0, "mean": None, "se": None, "t": None, "df": None, "mde": None}
    mean = sum(x for _, x in xs) / n
    by = collections.defaultdict(float)
    for d, x in xs:
        by[d] += x - mean
    G = len(by)
    se = math.sqrt(G / (G - 1) * sum(v * v for v in by.values())) / n if G >= 2 else None
    t = mean / se if se else None
    return {"n": n, "sessions": G, "mean": mean, "se": se, "t": t, "df": G - 1 if G >= 2 else None,
            "mde": MDE_K * se if se is not None else None}


def verdict(halves: dict, contrast: dict, declared_underpowered=False) -> dict:
    """pass clause: PRIMARY net15 > 0 with t >= crit(df) in BOTH halves, >= 60 entries per half, AND PRIMARY minus
    CONTROL_primary >= +50 bp with t >= crit pooled. FAIL on t <= -2 in either half (even underpowered); otherwise
    UNDERPOWERED when any MDE > 150 bp (R11); otherwise FAIL."""
    why = []
    for h, s in halves.items():
        if s["t"] is not None and s["t"] <= FAIL_T:
            return {"verdict": "FAIL", "why": [f"half {h} t {s['t']:.2f} <= {FAIL_T}"]}
    ok = True
    for h, s in halves.items():
        c = t_crit(s["df"])
        s["t_crit"] = c
        if s["n"] < MIN_N:
            ok = False
            why.append(f"half {h} n {s['n']} < {MIN_N}")
        if s["mean"] is None or s["mean"] <= 0 or s["t"] is None or c is None or s["t"] < c:
            ok = False
            why.append(f"half {h} mean/t below bar")
    cc = t_crit(contrast["df"])
    contrast["t_crit"] = cc
    if contrast["mean"] is None or contrast["mean"] < CONTRAST_BP or contrast["t"] is None or cc is None \
            or contrast["t"] < cc:
        ok = False
        why.append("contrast below bar")
    if declared_underpowered:
        return {"verdict": "UNDERPOWERED", "why": ["declared underpowered for the historical run (groups.T)"] + why}
    if ok:
        return {"verdict": "PASS", "why": ["historical PASS ships nothing: a forward PASS is also required"]}
    mdes = [s["mde"] for s in halves.values()] + [contrast["mde"]]
    if any(m is None or m > UNDER_BP for m in mdes):
        return {"verdict": "UNDERPOWERED", "why": [f"MDE {['%.0f' % m if m is not None else 'n/a' for m in mdes]}"
                                                   f" > {UNDER_BP:.0f} bp"] + why}
    return {"verdict": "FAIL", "why": why}


# ------------------------------------------------------------------ pipeline
def read_archive(path=ARCHIVE):
    raw = open(path, "rb").read()
    rows = []
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"_bad": True})
    return rows, {"archive_rows": len(rows), "archive_sha256": hashlib.sha256(raw).hexdigest(),
                  "archive_path": path}


def prior_session(cal: dict, day: str):
    ds = sorted(d for d in cal if d < day)
    return ds[-1] if ds else None


def phase_clean(mkt, day_filter, out_path):
    """data clause: archive -> cleaned name-days with validated tickers. Writes out_path."""
    rows, meta = read_archive()
    P_(f"archive rows {meta['archive_rows']} sha256 {meta['archive_sha256'][:12]}")
    days_all = sorted({et_day(float(r["unix"])) for r in rows if isinstance(r.get("unix"), (int, float))})
    cal = mkt.calendar((datetime.strptime(days_all[0], "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d"),
                       days_all[-1])
    keep = lambda d: d in cal and day_filter(d)  # noqa: E731
    lines, counts = clean_rows(rows, keep_day=keep)
    counts["drop_not_session_or_out_of_range_rows"] = counts.pop("out_of_range_rows", 0)
    sessions = sorted({et_day(float(r["unix"])) for r in rows if isinstance(r.get("unix"), (int, float))
                       and keep(et_day(float(r["unix"])))})
    assets = mkt.assets()
    nds = []
    for (day, sym), ls in sorted(lines.items()):
        why = asset_reason(sym, assets.get(sym))
        if why:
            counts[f"drop_nameday_{why}"] += 1
            continue
        nds.append({"day": day, "sym": sym, "lines": ls})
    mkt.flush()
    jsave(out_path, {"meta": meta, "counts": dict(counts), "sessions": sessions, "namedays": nds,
                     "calendar": cal})
    P_(f"clean: {len(nds)} validated name-days on {len(sessions)} sessions; counts {dict(counts)}")
    return out_path


def load_nameday_bars(mkt, nd, cal):
    day = nd["day"]
    prior = prior_session(cal, day)
    start = et_ts(prior or day, 4)
    end = cal[day][1]
    sip = window_rows(mkt.minute_bars(nd["sym"], "sip", start, end), day, prior, cal)
    return prior, sip


def phase_bars(mkt, clean):
    cal = clean["calendar"]
    by_day = collections.defaultdict(list)
    for nd in clean["namedays"]:
        by_day[nd["day"]].append(nd)
    n = 0
    for day, nds in sorted(by_day.items()):
        try:
            market_hours_guard() if mkt.guard else None
            mkt.daily_adv([x["sym"] for x in nds], day)
        except FetchFail:
            pass
        for nd in nds:
            if mkt.guard:
                market_hours_guard()
            try:
                prior = prior_session(cal, day)
                mkt.minute_bars(nd["sym"], "sip", et_ts(prior or day, 4), cal[day][1])
                if book_of(nd["lines"])[1] is not None:
                    mkt.minute_bars(nd["sym"], "iex", et_ts(prior or day, 4), cal[day][1])
            except FetchFail:
                pass
            n += 1
            if n % 50 == 0:
                P_(f"  bars {n}/{len(clean['namedays'])}; requests {mkt.requests}; fails {len(mkt.fails)}")
    mkt.flush()
    P_(f"bars done: {n} name-days, requests {mkt.requests}, fetch failures {len(mkt.fails)}")


def prepare_day(mkt, day, nds_raw, cal, counts):
    """Per name-day: >= 1 SIP trade, actionability/book, ADV, IEX + SIP PASS series, group assignment."""
    adv = mkt.daily_adv([x["sym"] for x in nds_raw], day)
    close = cal[day][1]
    close_of = lambda ts: cal.get(et_day(ts), [0, ts + 900])[1]  # noqa: E731
    out = []
    for raw in nds_raw:
        if mkt.guard:
            market_hours_guard()
        prior, sip = load_nameday_bars(mkt, raw, cal)
        if not any(et_day(r[0]) == day for r in sip):
            counts["drop_nameday_no_sip_trade"] += 1
            continue
        why, book = book_of(raw["lines"])
        if why:
            counts[f"drop_nameday_{why}"] += 1
            continue
        nd = {"day": day, "sym": raw["sym"], "close": min(close, et_ts(day, 16)), **book,
              "adv20": adv.get(raw["sym"]), "sip": sip}
        iex = window_rows(mkt.minute_bars(raw["sym"], "iex", et_ts(prior or day, 4), cal[day][1]), day, prior, cal)
        try:
            nd["iex"] = pass_series(iex, close_of)
            nd["sip_series"] = pass_series(sip, close_of)
        except ValueError:
            counts["drop_nameday_grid_error"] += 1
            continue
        g = assign_group(mkt, nd, counts)
        nd["group"] = g
        counts[f"group_{g}"] += 1
        out.append(nd)
    return out


def score_day(mkt, day, nds, counts):
    entries, info = [], []
    groups = ("T", "S", "MEGA")
    for nd in nds:
        if nd["group"] not in groups:
            continue
        if mkt.guard:
            market_hours_guard()
        prim = find_primary(mkt, nd, nd["iex"], counts, tag=f"{nd['group']}_")
        row = {"day": day, "sym": nd["sym"], "group": nd["group"], "R": nd["R"], "a0": nd["a0"],
               "premarket_call": nd["R"] < et_ts(day, 9, 30), "exit_line": nd.get("exit_unix"),
               "phrase_class": phrase_class(nd.get("text")),
               "assign_px": nd.get("assign_px"), "assign_spread_bp": nd.get("assign_spread_bp"),
               "adv20": nd.get("adv20")}
        if prim:
            row["primary"] = {k: v for k, v in prim.items() if k != "quote"}
            row["primary"]["entry_min_et"] = hhmm(prim["t"])
            c1 = control_primary(mkt, nd, prim, nds, counts)
            row["control_primary"] = c1
            row["control_secondary"] = control_secondary(mkt, nd, prim, counts)
        if nd["group"] == "S":
            # information_only_added_2026-10-07 (1): PASS = WR_trending alone; control "not WR_trending at t"
            pw = find_primary(mkt, nd, nd["iex"], counts, tag="info_wronly_S_", key="wr")
            row["info_wr_only"] = None
            if pw:
                cw = control_primary(mkt, nd, pw, nds, counts, key="wr", tag="wronly_ctrl_")
                row["info_wr_only"] = {"primary": {k: v for k, v in pw.items() if k != "quote"},
                                       "control_primary": cw}
        # information: SIP-signal version, keep despite exit line
        ps = find_primary(mkt, nd, nd["sip_series"], counts, tag=f"info_sip_{nd['group']}_")
        row["info_sip_signal"] = {k: v for k, v in ps.items() if k != "quote"} if ps else None
        if nd.get("exit_unix") is not None:
            pk = find_primary(mkt, nd, nd["iex"], counts, keep_exit=True, tag=f"info_keep_{nd['group']}_")
            row["info_keep_after_exit"] = {k: v for k, v in pk.items() if k != "quote"} if pk else None
        else:
            row["info_keep_after_exit"] = row.get("primary")
        # information (skeptic 2bfcf31 item 4): PRIMARY evaluated only at minutes with a REAL IEX bar
        real = {t: st for t, st in nd["iex"].items() if st.get("real")}
        pr = find_primary(mkt, nd, real, counts, tag=f"info_realbar_{nd['group']}_")
        row["info_real_iex_bar_only"] = {k: v for k, v in pr.items() if k != "quote"} if pr else None
        row["info"] = info_cells(mkt, nd, prim, counts)
        entries.append(row)
    return entries


def run_score(mkt, clean, sessions_for_halves):
    cal = clean["calendar"]
    counts = collections.Counter(clean["counts"])
    by_day = collections.defaultdict(list)
    for nd in clean["namedays"]:
        by_day[nd["day"]].append(nd)
    rows, failed_days = [], []
    for day in sorted(by_day):
        try:
            nds = prepare_day(mkt, day, by_day[day], cal, counts)
            rows += score_day(mkt, day, nds, counts)
        except FetchFail as e:
            failed_days.append(day)
            P_(f"  {day}: fetch failure ({str(e)[:120]}); continuing to warm the cache")
        P_(f"  scored {day}: rows {len(rows)}; requests {mkt.requests}")
    mkt.flush()
    fails = dict(mkt.fails)
    if failed_days or fails:
        raise SystemExit(f"refusing to score: {len(fails)} fetch failures remain (days {failed_days}); "
                         f"see {os.path.join(mkt.work, 'fetch_fails.json')} and rerun")
    half_of = {d: ("A" if i % 2 == 0 else "B") for i, d in enumerate(sessions_for_halves)}
    for r in rows:
        r["half"] = half_of.get(r["day"])
    return rows, counts


def summarize(rows, counts, meta, mode="historical"):
    res = {"mode": mode, "meta": meta, "resolutions": RESOLUTIONS, "counts": dict(counts), "groups": {}}
    for g in ("T", "S", "MEGA"):
        gr = [r for r in rows if r["group"] == g]
        prim = [{"day": r["day"], "half": r["half"], **r["primary"]} for r in gr if r.get("primary")]
        pairs = [{"day": r["day"], "half": r["half"],
                  "diff": r["primary"]["net_bp"] - r["control_primary"]["net_bp"]}
                 for r in gr if r.get("primary") and r.get("control_primary")]
        halves = {h: clustered([p for p in prim if p["half"] == h]) for h in ("A", "B")}
        contrast = clustered(pairs, "diff")
        entry_minutes = collections.Counter()
        for p in prim:
            m = et_min(p["t"])
            entry_minutes[f"{p['half']} {m // 60:02d}:{'00' if m % 60 < 30 else '30'}"] += 1
        power = {"n_per_half": {h: halves[h]["n"] for h in halves},
                 "sessions_per_half": {h: halves[h]["sessions"] for h in halves},
                 "se_bp_per_half": {h: halves[h]["se"] for h in halves},
                 "mde_bp_per_half": {h: halves[h]["mde"] for h in halves},
                 "contrast_n": contrast["n"], "contrast_se_bp": contrast["se"], "contrast_mde_bp": contrast["mde"],
                 "entry_minute_distribution": dict(sorted(entry_minutes.items())),
                 "namedays_in_group": len(gr)}
        v = None
        if g in ("T", "S"):
            if mode == "forward" and any(halves[h]["n"] < MIN_N for h in halves):
                v = {"verdict": "PENDING", "why": [f"forward n per half {power['n_per_half']} < {MIN_N}"]}
            else:
                v = verdict(halves, contrast, declared_underpowered=(g == "T" and mode == "historical"))
        info = info_summary(gr)
        info["primary_by_phrase_class"] = phrase_table(gr)
        if g == "S":
            wp = [{"day": r["day"], "half": r["half"], **r["info_wr_only"]["primary"]}
                  for r in gr if r.get("info_wr_only")]
            wpairs = [{"day": r["day"], "diff": r["info_wr_only"]["primary"]["net_bp"]
                       - r["info_wr_only"]["control_primary"]["net_bp"]}
                      for r in gr if r.get("info_wr_only") and r["info_wr_only"].get("control_primary")]
            info["wr_only_primary"] = clustered(wp)
            info["wr_only_primary_half_A"] = clustered([x for x in wp if x["half"] == "A"])
            info["wr_only_primary_half_B"] = clustered([x for x in wp if x["half"] == "B"])
            info["wr_only_minus_control"] = clustered(wpairs, "diff")
        if g == "S":
            sp = [p["spread_bp"] for p in prim]
            info["S_share_entry_spread_within_desk_cap"] = (sum(1 for s in sp if s <= T_MAX_SPR * 1e4) / len(sp)
                                                            if sp else None)
        n_noq = counts.get("group_excl_no_trade_at_assignment", 0)
        halts = {leg: sum(1 for r in gr if (r.get(leg) or {}).get("halt"))
                 for leg in ("primary", "control_primary", "control_secondary", "info_sip_signal")}
        halts["primary_by_half"] = {h: sum(1 for p in prim if p["half"] == h and p.get("halt")) for h in ("A", "B")}
        power["halt_exit"] = halts
        if g == "S":
            # excl_no_trade_at_assignment names have no SIP price, so they cannot be grouped; the flag compares them
            # with the S name-day count (skeptic 2bfcf31 item 3)
            power["excl_no_trade_at_assignment"] = n_noq
            power["excl_no_trade_share_of_S"] = n_noq / len(gr) if gr else None
            power["excl_no_trade_flag"] = bool(n_noq > 0.10 * len(gr))
        res["groups"][g] = {"power": power, "primary": halves, "contrast": contrast, "verdict": v, "info": info,
                            "pooled_primary": clustered(prim)}
    res["entries"] = rows
    return res


PHRASE_CLASSES = (   # information_only_added_2026-10-07 (2): first match wins, in this order
    ("hod", re.compile(r"\btest(?:s|ing)?\s+(?:the\s+)?hod\b|\bnear\s+(?:the\s+)?hod\b|\bnhod\b", re.I)),
    ("pop", re.compile(r"\bpop(?:s|ping|ped)?\b", re.I)),
    ("micro", re.compile(r"\bmicro\w*|\blow\s+float\b", re.I)),
    ("news", re.compile(r"\bpr\b|\bnews\b", re.I)),
)


def phrase_class(text) -> str:
    t = str(text or "")
    for name, rx in PHRASE_CLASSES:
        if rx.search(t):
            return name
    return "other"


def phrase_table(gr) -> dict:
    """PRIMARY n, mean, median, win rate of net15 per phrase class (information only)."""
    out = {}
    for name in [c[0] for c in PHRASE_CLASSES] + ["other"]:
        xs = [r["primary"]["net_bp"] for r in gr if r.get("primary") and r.get("phrase_class") == name]
        out[name] = {"n": len(xs), "mean": statistics.mean(xs) if xs else None,
                     "median": statistics.median(xs) if xs else None,
                     "win_rate": sum(1 for x in xs if x > 0) / len(xs) if xs else None}
    return out


def info_summary(gr):
    def cell(xs):
        return clustered([x for x in xs if x and x.get("net_bp") is not None])
    out = {}
    out["control_secondary_within_name"] = cell([dict(r["control_secondary"], day=r["day"]) for r in gr
                                                 if r.get("control_secondary")])
    out["real_iex_bar_minutes_only"] = cell([dict(r["info_real_iex_bar_only"], day=r["day"]) for r in gr
                                             if r.get("info_real_iex_bar_only")])
    out["premarket_call_first_rth_print"] = cell([dict(r["info"]["premarket_call_first_rth_print"], day=r["day"])
                                                  for r in gr if r["info"].get("premarket_call_first_rth_print")])
    out["sip_signal"] = cell([dict(r["info_sip_signal"], day=r["day"]) for r in gr if r.get("info_sip_signal")])
    out["keep_despite_exit_line"] = cell([dict(r["info_keep_after_exit"], day=r["day"]) for r in gr
                                          if r.get("info_keep_after_exit")])
    out["latency_plus_30s"] = cell([dict(r["info"]["lat30"], day=r["day"]) for r in gr if r["info"].get("lat30")])
    out["hold_to_1550"] = cell([dict(r["info"]["hold_1550"], day=r["day"]) for r in gr
                                if r["info"].get("hold_1550")])
    out["p27_method_uses_at"] = cell([dict(r["info"]["p27_uses_at"], day=r["day"]) for r in gr
                                      if r["info"].get("p27_uses_at")])
    cm = [dict(r["info"]["call_moment"], day=r["day"]) for r in gr if r["info"].get("call_moment")]
    out["call_moment_rth_calls"] = cell(cm)
    for lab, f in (("sr_ok", lambda x: x.get("sr_ok") is True), ("not_sr_ok", lambda x: x.get("sr_ok") is False),
                   ("wr_trending", lambda x: x.get("wr") is True), ("not_wr_trending", lambda x: x.get("wr") is False),
                   ("unwarm", lambda x: not x.get("warm"))):
        out[f"call_moment_{lab}"] = cell([x for x in cm if f(x)])
    prim = [dict(r["primary"], day=r["day"], pm=r["premarket_call"]) for r in gr if r.get("primary")]
    out["primary_premarket_call"] = cell([x for x in prim if x["pm"]])
    out["primary_rth_call"] = cell([x for x in prim if not x["pm"]])
    mf = [r["info"].get("mfe30_bp") for r in gr if r["info"].get("mfe30_bp") is not None]
    ma = [r["info"].get("mae30_bp") for r in gr if r["info"].get("mae30_bp") is not None]
    out["mfe30_median_bp"] = statistics.median(mf) if mf else None
    out["mae30_median_bp"] = statistics.median(ma) if ma else None
    return out


def _f(x, nd=0):
    return "n/a" if x is None else f"{x:+.{nd}f}" if nd == 0 else f"{x:.{nd}f}"


def render_report(res) -> str:
    L = [f"# Bro S/R + %R call-out study ({res['mode']})", "",
         f"Prereg docs/studies/bro_sr_wr_prereg.json. Archive rows at run time: {res['meta'].get('archive_rows')} "
         f"(sha256 {str(res['meta'].get('archive_sha256'))[:12]}). Script {res['meta'].get('script_rev')}.", "",
         f"**Chart-check caveat (prereg chart_check_2026-10-07):** {CHART_CHECK_2026_10_07}", "",
         "## 1. Power (written before any mean)", ""]
    for g, G in res["groups"].items():
        p = G["power"]
        L.append(f"**{g}** name-days {p['namedays_in_group']}; PRIMARY n per half {p['n_per_half']}, sessions "
                 f"{p['sessions_per_half']}; SE bp {{A: {_f(p['se_bp_per_half']['A'], 1)}, "
                 f"B: {_f(p['se_bp_per_half']['B'], 1)}}}; MDE bp {{A: {_f(p['mde_bp_per_half']['A'], 1)}, "
                 f"B: {_f(p['mde_bp_per_half']['B'], 1)}}}; contrast n {p['contrast_n']} MDE "
                 f"{_f(p['contrast_mde_bp'], 1)} bp")
        L.append(f"  entry minutes: {p['entry_minute_distribution']}")
        L.append(f"  halt exits (first trade after resume): {p.get('halt_exit')}")
        if "excl_no_trade_at_assignment" in p:
            share = p.get("excl_no_trade_share_of_S")
            L.append(f"  excl_no_trade_at_assignment: {p['excl_no_trade_at_assignment']} "
                     f"({'n/a' if share is None else f'{share:.0%}'} of S name-days)"
                     + ("  **FLAG: > 10% of S — the unpriced names may differ from S**"
                        if p.get("excl_no_trade_flag") else ""))
    L += ["", "## 2. Drops and counts", "", "```"]
    L += [f"{k:<48}{v}" for k, v in sorted(res["counts"].items())]
    L += ["```", "", "## 3. Results (net15 bp after full SIP spread, +1c RT under $5)", ""]
    for g, G in res["groups"].items():
        L.append(f"### {g}" + ("  (information cell: ADV20 >= $5B, never pooled into T)" if g == "MEGA" else ""))
        for h, s in G["primary"].items():
            L.append(f"- PRIMARY half {h}: n {s['n']}, sessions {s['sessions']}, mean {_f(s['mean'], 1)}, "
                     f"t {_f(s['t'], 2)} (crit {_f(s.get('t_crit'), 2)})")
        c = G["contrast"]
        L.append(f"- PRIMARY - CONTROL_primary (pooled): n {c['n']}, mean {_f(c['mean'], 1)}, t {_f(c['t'], 2)} "
                 f"(crit {_f(c.get('t_crit'), 2)})")
        if G["verdict"]:
            L.append(f"- **VERDICT: {G['verdict']['verdict']}** — {'; '.join(G['verdict']['why'])}")
        L.append("- information only (never promoted):")
        for k, v in G["info"].items():
            if k == "primary_by_phrase_class":
                L.append("  - PRIMARY by call phrase class (net15 bp):")
                for cname, cv in v.items():
                    win = "n/a" if cv["win_rate"] is None else f"{cv['win_rate']:.0%}"
                    L.append(f"    - {cname}: n {cv['n']}, mean {_f(cv['mean'], 1)}, median {_f(cv['median'], 1)}, "
                             f"win {win}")
                continue
            k = {"premarket_call_first_rth_print": "premarket call -> first regular RTH print (quote at entry)",
                 "real_iex_bar_minutes_only": "PRIMARY at real IEX bar minutes only (no flat-filled minutes)"}.get(k, k)
            if isinstance(v, dict):
                L.append(f"  - {k}: n {v['n']}, mean {_f(v['mean'], 1)}, t {_f(v['t'], 2)}")
            else:
                L.append(f"  - {k}: {_f(v, 3) if isinstance(v, float) else v}")
        L.append("")
    L += ["## 4. Resolutions of ambiguous prereg wording", ""]
    L += [f"- {k}: {v}" for k, v in res["resolutions"].items()]
    L += ["", "Chart check (features_at_R.SR_ok): run `chartcheck SYM DAY HH:MM` on 3 archive names and record the "
          "TradingView comparison here before the run."]
    return "\n".join(L) + "\n"


def script_rev():
    try:
        return subprocess.check_output(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], text=True).strip()
    except Exception:  # noqa: BLE001
        return None


def main_historical(phases):
    mkt = AlpacaMarket()
    cp = os.path.join(WORK, "clean.json")
    if "clean" in phases:
        market_hours_guard()
        phase_clean(mkt, lambda d: d <= HIST_HI, cp)
    clean = jload(cp)
    if clean is None:
        raise SystemExit("run phase clean first")
    if "bars" in phases:
        market_hours_guard()
        phase_bars(mkt, clean)
    if "score" in phases:
        market_hours_guard()
        rows, counts = run_score(mkt, clean, clean["sessions"])
        res = summarize(rows, counts, {**clean["meta"], "script_rev": script_rev(),
                                       "requests_this_run": mkt.requests})
        jsave(os.path.join(WORK, "result.json"), res)
        P_(f"result.json written ({len(rows)} name-day rows)")
    if "report" in phases:
        res = jload(os.path.join(WORK, "result.json"))
        if res is None:
            raise SystemExit("no result.json")
        open(os.path.join(WORK, "report.md"), "w").write(render_report(res))
        P_(f"report.md written to {WORK}")


def uptime_proxy(day):
    """forward: OCR capture uptime proxy per session from the archive's own capture stamps."""
    rows, _ = read_archive()
    ts = sorted(float(r["unix"]) for r in rows if isinstance(r.get("unix"), (int, float))
                and et_day(float(r["unix"])) == day)
    rth = [t for t in ts if 9 * 60 + 30 <= et_min(t) < 16 * 60]
    gaps = [(b - a) / 60 for a, b in zip(rth, rth[1:])]
    return {"rows": len(ts), "first": hhmm(ts[0]) if ts else None, "last": hhmm(ts[-1]) if ts else None,
            "rth_rows": len(rth), "max_rth_gap_min": max(gaps) if gaps else None}


def main_forward(day):
    if day < FORWARD_START:
        raise SystemExit(f"forward sessions start {FORWARD_START}")
    market_hours_guard()
    fw = os.path.join(WORK, "forward")
    mkt = AlpacaMarket()
    cp = os.path.join(fw, f"clean_{day}.json")
    phase_clean(mkt, lambda d: d == day, cp)
    clean = jload(cp)
    phase_bars(mkt, clean)
    days_meta = jload(os.path.join(fw, "days.json"), {}) or {}
    days_meta[day] = {"uptime": uptime_proxy(day), "archive_rows": clean["meta"]["archive_rows"]}
    sessions = sorted(days_meta)
    rows, counts = run_score(mkt, clean, sessions)
    days_meta[day]["counts"] = dict(counts)
    jsave(os.path.join(fw, "days.json"), days_meta)
    allrows = [r for r in (jload(os.path.join(fw, "entries.json"), []) or []) if r["day"] != day] + rows
    half_of = {d: ("A" if i % 2 == 0 else "B") for i, d in enumerate(sessions)}
    for r in allrows:
        r["half"] = half_of.get(r["day"])
    jsave(os.path.join(fw, "entries.json"), allrows)
    with open(os.path.join(fw, "forward_log.jsonl"), "a") as f:
        f.write(json.dumps({"day": day, "rows": len(rows), "at": time.time(), **days_meta[day]}) + "\n")
    agg = collections.Counter()
    for d in days_meta.values():
        agg.update(d.get("counts") or {})
    res = summarize(allrows, agg, {"archive_rows": clean["meta"]["archive_rows"],
                                   "archive_sha256": clean["meta"]["archive_sha256"], "script_rev": script_rev(),
                                   "forward_sessions": sessions}, mode="forward")
    jsave(os.path.join(fw, "result.json"), res)
    open(os.path.join(fw, "report.md"), "w").write(render_report(res))
    P_(f"forward {day}: {len(rows)} name-day rows; forward sessions {len(sessions)}")


def chart_check(sym, day, at_hhmm, feed="iex", mkt=None):
    """Print the port's order-block levels (charted last 3 per side), overhead resistance, %R and PASS at the grid
    minute close DAY HH:MM ET, on the same gridded bars the study uses, for comparison against TradingView."""
    mkt = mkt or AlpacaMarket()
    sym = sym.upper()
    lo = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=10)).strftime("%Y-%m-%d")
    cal = mkt.calendar(lo, day)
    if day not in cal:
        raise SystemExit(f"{day} is not a session")
    prior = prior_session(cal, day)
    rows = window_rows(mkt.minute_bars(sym, feed, et_ts(prior or day, 4), cal[day][1]), day, prior, cal)
    hh, mm = map(int, at_hhmm.split(":"))
    t = et_ts(day, hh, mm)
    grid = desk_grid([r for r in rows if r[0] + 60 <= t])
    idx = next((i for i, g in enumerate(grid) if g[0] + 60 == t), None)
    if idx is None:
        if grid and (t - 60 - grid[-1][0]) // 60 < 15:
            c = grid[-1][4]
            while grid[-1][0] + 60 < t:
                grid.append([grid[-1][0] + 60, c, c, c, c, False])
            idx = len(grid) - 1
        else:
            P_(f"{sym} {day} {at_hhmm}: not a grid minute (no {feed} bar within 15 min)")
            mkt.flush()
            return None
    fast, slow, sr, det = features(grid, want=idx)
    st = _state(idx, fast, slow, sr, grid[idx][4])
    P_(f"{sym} {day} close of {at_hhmm} ET on {feed.upper()} gridded 1-min bars ({len(grid)} rows, "
       f"{'real' if grid[idx][5] else 'flat-filled'} bar), price {grid[idx][4]:.4f}")
    for b in det["charted"]:
        P_(f"  {'BREAKER ' if b.breaker else ''}{b.kind:<4} OB {b.btm:.4f}-{b.top:.4f}  origin "
           f"{datetime.fromtimestamp(b.origin_ts, ET):%m-%d %H:%M}  known {datetime.fromtimestamp(b.known_ts, ET):%m-%d %H:%M}")
    P_(f"  overhead resistance within {RESIST_PCT}%: {[(round(b.btm, 4), round(b.top, 4)) for b in det['resist']]}")
    if st["warm"]:
        P_(f"  %R fast {fast[idx]:.1f} (t-3 {fast[idx - 3]:.1f})  slow {slow[idx]:.1f} (t-3 {slow[idx - 3]:.1f})  "
           f"SR_ok {st['sr_ok']}  WR_trending {st['wr']}  PASS {st['pass']}")
    else:
        P_("  warm-up: fewer than 112 gridded minutes before t-3 (minute skipped)")
    mkt.flush()
    return {"state": st, "charted": det["charted"], "resist": det["resist"]}


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a:
        raise SystemExit(__doc__)
    cmd = a[0]
    if cmd in ("clean", "bars", "score", "report"):
        main_historical({cmd})
    elif cmd == "all":
        main_historical({"clean", "bars", "score", "report"})
    elif cmd == "forward" and len(a) == 2:
        main_forward(a[1])
    elif cmd == "chartcheck" and len(a) in (4, 5):
        market_hours_guard()
        chart_check(a[1], a[2], a[3], a[4] if len(a) == 5 else "iex")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
