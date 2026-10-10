#!/usr/bin/env python3
"""hist_universe.py — point-in-time reconstruction of the day desk's movers / tight admissions.

Workstream U of docs/studies/HIST_SIM_PLAN_2026-10-10.md, bound by docs/studies/hist_sim_prereg.json.
For a past session it answers: which names would the live book have admitted, WHEN (ET, first
admission) and from which source — so tools/replay_session.py can run the live code on days that
were never recorded.

WHAT IS RECONSTRUCTED (the live code path, ported, minute resolution)
  movers  movers_screener.fetch_rows every 60 s: Alpaca top-N gainers (real-time %, all listed
          symbols), the IEX universe scan (09:35-16:00, every ai_movers_scan_sec, scan_select of
          the live module), most-actives by share volume, the same-session carry; each candidate
          re-measured from the SIP daily bar AS THE FREE PLAN SERVES IT (15 min late; before
          ~09:46 the latest daily bar is YESTERDAY's, so pct/px/rvol are the prior session's —
          exactly what the live producer reads premarket). Then the book's movers seed
          (price cap/floor, pct, thin_rvol via the live rvol_blocks_admit, $vol, n cap) and the
          interval soft seed (first 40 file rows, pct > 0, soft_seed_max).
  tight   tight_screener.scan every ai_tight_scan_sec from 09:46: universe = top-N by PRIOR-day
          SIP $vol (prior close floor), IEX last trade (<= 300 s old) vs prior SIP close, desk
          spread statistic walked in % order (the live tight_select), rows by the live build_row;
          then the book's tight seed gates.
  door    admit_arm_gates as the live passes_inclusion runs them before admission_filter logs
          admit_range: price band (min_price_for / _max_price_for), SIP spread (median over the
          60 s ending 16 min earlier, the live sip_spread_pct statistic, 180 s cache) once
          mins_open >= delay+1, open-gap block (premarket: the IEX snapshot's LATEST daily bar,
          i.e. yesterday's open gap — what the live desk reads before 09:30; 09:30-09:46 today's
          IEX open; then SIP 09:30 open vs prior SIP close).

NOT RECONSTRUCTED (named, never guessed): momentum (Discord "[ELITE]" scanner alerts ->
dashboard ticker log -> _dashboard_tickers; there is no market-data rule behind it), trending
(Stocktwits), agy / xai (AI picks), bb_live (Trader Bro callouts), research boards. A recorded
momentum name still counts as recalled when the movers/tight rules admit it (label differs).
Also not modelled: per-name book state (dead_reentry, stale/no-stream strikes, seat churn), the
%R heat relief in rvol_blocks_admit (no indicators here), the desk live-quote override of seed
prices (quote_mode knob, calibrated), morning-flood ordering.

POINT IN TIME
  * every decision at minute boundary T reads only minute bars that CLOSED before T (real-time
    paths) or before T-16 (the 15-min-late SIP paths); daily data only from sessions before D.
    No same-day daily bar, no full-day volume, no pruning by the day's outcome.
  * symbol master: Alpaca asset list fetched with status ACTIVE and INACTIVE (delisted) on the
    build date, non-OTC, intersected per day with symbols that have a RAW SIP daily bar on the
    prior session (i.e. listed and trading then). Stated in each output file.
  * all prices RAW (adjustment='raw'): prior close, gap, pct_change, every floor/cap.
  * nothing after first_ts is used; no forward return / P&L / post-admission move is computed.

USAGE (Mac mini, repo on PYTHONPATH via REPO=):
  python hist_universe.py assets                      # PIT symbol master snapshot
  python hist_universe.py daily 2026-01-02 2026-10-09 # raw SIP+IEX daily bars cache
  python hist_universe.py build 2026-09-24 [DAY..]    # -> ai_reports/hist_universe/DAY.json
  python hist_universe.py build-range 2026-03-02 2026-09-23 [--newest-first]
  python hist_universe.py check 2026-09-24..2026-10-09 [--set k=v ...]
Calibration days (9/24-10/9) use the config the desk ran that day (sessions/DAY/config.jsonl.gz);
held-out days use ONE fixed config: config/bot_config.json as of the build (sha logged).
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import hashlib
import json
import math
import os
import pickle
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.environ.get("REPO") or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

ET = ZoneInfo("America/New_York")
RULE_VERSION = "hist_universe/u1"
OUT_DIR = os.path.join(ROOT, "ai_reports", "hist_universe")
CACHE_DIR = os.path.join(OUT_DIR, "cache")
CALIB_LO, CALIB_HI = "2026-09-24", "2026-10-09"
COVERED = ("movers", "tight")
PREREG_SOURCES = ("movers", "tight", "momentum")
UNCOVERED = ("momentum", "trending", "agy", "xai", "bb_live")

# Minute grid: index i = minutes since 04:00 ET; i covers [04:00+i, 04:01+i). 720 = 16:00.
DAY0_MIN = 240
N_MIN = 720
I_OPEN = 330          # 09:30
SIP_DELAY = 16        # live sip_spread_pct / free-plan bars: data older than ~15 min, minute granularity

# Simulation knobs (calibrated on 9/24-10/9 only; recorded in every output).
KNOBS_DEFAULT = {
    "gainers_top": 50,          # Alpaca top-N gainers; ranked over every listed symbol we hold bars for
    "actives_top": None,        # None = cfg ai_movers_actives_top
    "quote_mode": "file",       # seed price/pct: "file" (producer numbers) or "rt" (real-time SIP last)
    "soft_seed": True,          # interval soft seed of the first 40 movers rows
    "soft_seed_rth_only": False,
    "seed_start_min": 0,        # first decision minute index (0 = 04:00)
    "tight_phase": 0,           # tight scans at 09:46 + k*scan_sec + phase minutes
    "spread_ttl_min": 3,        # live sip_spread_pct cache ttl 180 s
}

# Config knobs that drive the rules (logged with every output).
CFG_KEYS = (
    "ai_movers_top", "ai_movers_min_pct_change", "ai_movers_min_price", "ai_movers_max_price",
    "ai_movers_min_dollar_vol", "ai_movers_live_window_min", "ai_movers_min_live_pct",
    "ai_movers_min_minute_dollars", "ai_movers_live_min_open_minutes", "ai_movers_max_rows",
    "ai_movers_session_append", "ai_movers_session_max", "ai_movers_use_most_actives",
    "ai_movers_actives_top", "ai_movers_universe_scan", "ai_movers_scan_sec",
    "ai_movers_scan_min_rvol", "ai_movers_scan_min_iex_dollars", "ai_movers_scan_top",
    "ai_movers_premarket_scan", "ai_movers_premarket_feed", "ai_movers_premarket_min_gap_pct",
    "ai_movers_premarket_max_trade_age_sec", "ai_movers_premarket_min_prev_dollars",
    "ai_movers_sip_delay_min", "rvol_time_adjusted",
    "ai_watch_seed_movers", "ai_watch_seed_movers_n", "ai_watch_movers_min_pct_change",
    "ai_watch_movers_min_price", "ai_watch_movers_min_dollar_volume", "ai_watch_movers_min_rvol",
    "ai_watch_hot_move_rvol_waive_pct", "ai_watch_min_price", "ai_max_price",
    "ai_watch_soft_seed_enabled", "ai_watch_soft_seed_movers", "ai_watch_soft_seed_interval_sec",
    "ai_watch_soft_seed_max", "ai_watch_seed_tight", "ai_watch_seed_tight_n", "ai_tight_scan_sec",
    "ai_tight_min_pct_change", "ai_tight_max_spread_pct", "ai_tight_min_price",
    "ai_tight_min_dollar_volume", "ai_tight_universe_n", "ai_tight_top",
    "ai_tight_max_spread_lookups", "ai_tight_max_price", "ai_watch_tight_min_rvol",
    "ai_watch_admit_arm_gates", "ai_watch_max_sip_spread_pct", "ai_watch_gap_down_block_pct",
    "ai_watch_momentum_spread_exempt",
)


def _f(cfg, k, d=0.0):
    try:
        v = cfg.get(k, d)
        return float(d if v is None else v)
    except (TypeError, ValueError):
        return float(d)


def _i(cfg, k, d=0):
    try:
        v = cfg.get(k, d)
        return int(d if v is None else v)
    except (TypeError, ValueError):
        return int(d)


def et_ts(day: str, i: int) -> float:
    """Unix ts of minute index i (minutes since 04:00 ET) on day."""
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    return (d + timedelta(minutes=DAY0_MIN + i)).timestamp()


def hhmm(i: int) -> str:
    m = DAY0_MIN + i
    return f"{m // 60:02d}:{m % 60:02d}"


# ═════════════════════════════ pure rule logic ═════════════════════════════

def is_common(sym):
    from ticker_filters import is_common as _ic
    return _ic(sym)


def is_levered(sym, name=""):
    from ticker_filters import is_levered_etp
    return is_levered_etp(sym, name or "")


def gainers_select(pcts: dict, prices: dict, *, top: int, min_pct: float, lo: float, hi: float,
                   ok=None) -> list[tuple[str, float, float]]:
    """Alpaca top-`top` gainers over EVERY symbol (warrants and ETFs take ranks too), then the
    producer's filter: pct >= min_pct, common, not levered, lo <= px <= hi. Ties by symbol."""
    ranked = sorted(((p, s) for s, p in pcts.items() if p is not None and not math.isnan(p)),
                    key=lambda t: (-t[0], t[1]))[:max(0, top)]
    out = []
    for p, s in ranked:
        px = prices.get(s)
        if px is None or p < min_pct or not (lo <= px <= hi):
            continue
        if ok is not None and not ok(s):
            continue
        out.append((s, p, px))
    return out


def actives_select(cumvol: dict, *, top: int, ok=None) -> list[str]:
    """Most actives by share volume (every symbol ranks), then common / not levered."""
    ranked = sorted(((v, s) for s, v in cumvol.items() if v and v > 0),
                    key=lambda t: (-t[0], t[1]))[:max(0, top)]
    return [s for _, s in ranked if ok is None or ok(s)]


def daily_view(prior: list[tuple], today: tuple | None):
    """The SIP daily-bar sequence the producer sees: prior raw (date, c, v) bars + today's partial
    bar (close, volume) when the delayed feed already serves one."""
    seq = [(c, v) for (_, c, v) in prior]
    if today is not None:
        seq.append(today)
    return seq


def measure_row(sym, pct, px, seq, *, today_bar: bool, mins_open: float, cfg) -> dict | None:
    """movers_screener.fetch_rows' per-candidate measurement. None = no row (re-gated out / no bars).
    `pct`/`px` None = unpriced candidate (scan/actives/carry): rebuilt from seq[-1] vs seq[-2]."""
    min_pct = _f(cfg, "ai_movers_min_pct_change", 10.0)
    lo, hi = _f(cfg, "ai_movers_min_price", 2.0), _f(cfg, "ai_movers_max_price", 20.0)
    if pct is None or px is None:
        if len(seq) < 2:
            return None
        px, prev = float(seq[-1][0] or 0), float(seq[-2][0] or 0)
        if px <= 0 or prev <= 0:
            return None
        pct = (px / prev - 1.0) * 100.0
        if pct < min_pct or not (lo <= px <= hi) or is_levered(sym):
            return {"_discard": True}
    vol = float(seq[-1][1] or 0) if seq else 0.0
    prior = [float(v or 0) for _, v in seq[:-1]][-20:]
    avg = (sum(prior) / len(prior)) if prior else 0.0
    rvol_raw = (vol / avg) if (avg > 0 and vol > 0) else None
    rvol = rvol_raw
    if rvol_raw is not None and bool(cfg.get("rvol_time_adjusted", True)) and today_bar:
        import morning_funnel as mf
        rvol = mf.rvol_pair(vol, avg, mins_open, time_adjusted=True)[0]
    dollar_vol = (vol * px) if vol else 0.0
    mdv = _f(cfg, "ai_movers_min_dollar_vol", 1_000_000)
    if mdv > 0 and dollar_vol < mdv:
        return None
    return {"symbol": sym, "pct_change": pct, "price": px, "rvol": rvol, "rvol_raw": rvol_raw,
            "dollar_volume": dollar_vol or None}


def movers_seed(rows: list[dict], cfg: dict, *, max_price: float) -> list[dict]:
    """ai_entry_watch movers seed: first ai_watch_seed_movers_n rows that pass the seed gates."""
    from ai_entry_watch import rvol_blocks_admit
    n = max(1, _i(cfg, "ai_watch_seed_movers_n", 12))
    mv_min_pct = _f(cfg, "ai_watch_movers_min_pct_change", 10.0)
    mv_min_px = _f(cfg, "ai_watch_movers_min_price", 0.0)
    mv_min_dv = _f(cfg, "ai_watch_movers_min_dollar_volume", 0.0)
    out = []
    for r in rows:
        s = r["symbol"]
        if is_levered(s):
            continue
        px, pct = r.get("seed_price", r.get("price")), r.get("seed_pct", r.get("pct_change"))
        if max_price and px is not None and not px < max_price:
            continue
        if mv_min_px > 0 and (px is None or px < mv_min_px):
            continue
        if pct is None or pct < mv_min_pct:
            continue
        if rvol_blocks_admit(r.get("rvol"), pct, cfg, source="movers", record=None) == "thin_rvol":
            continue
        dv = r.get("dollar_volume")
        if mv_min_dv > 0 and (dv is None or dv < mv_min_dv):
            continue
        out.append({**r, "price": px, "pct_change": pct, "source": "movers", "path": "seed"})
        if len(out) >= n:
            break
    return out


def soft_seed(rows: list[dict], cfg: dict) -> list[dict]:
    """maybe_soft_seed_rows' movers leg, simplified: the first 40 file rows with pct > 0, ranked
    by pct (the live scout score needs indicators), at most ai_watch_soft_seed_max."""
    cand = [r for r in rows[:40] if (r.get("pct_change") or 0) > 0]
    cand.sort(key=lambda r: -(r.get("pct_change") or 0))
    mx = _i(cfg, "ai_watch_soft_seed_max", 12)
    return [{**r, "source": "movers", "path": "soft"} for r in cand[:mx if mx > 0 else 0]]


def tight_seed(rows: list[dict], cfg: dict) -> list[dict]:
    """ai_entry_watch tight seed gates, first ai_watch_seed_tight_n passing rows."""
    n = max(1, _i(cfg, "ai_watch_seed_tight_n", 8))
    min_pct = _f(cfg, "ai_tight_min_pct_change", 1.0)
    min_px = max(_f(cfg, "ai_tight_min_price", 10.0), _f(cfg, "ai_watch_movers_min_price", 0.0))
    max_sp = _f(cfg, "ai_tight_max_spread_pct", 0.03)
    cap_sp = _f(cfg, "ai_watch_max_sip_spread_pct", 0.0)
    if cap_sp > 0:
        max_sp = min(max_sp, cap_sp) if max_sp > 0 else cap_sp
    min_dv = _f(cfg, "ai_watch_movers_min_dollar_volume", 0.0)
    cap = _f(cfg, "ai_tight_max_price", 0.0) or _f(cfg, "ai_max_price", 0.0)
    out = []
    for r in rows:
        if len(out) >= n:
            break
        s = r["symbol"]
        if is_levered(s):
            continue
        px, pct = r.get("seed_price", r.get("price")), r.get("seed_pct", r.get("pct_change"))
        if cap and px is not None and not px < cap:
            continue
        if min_px > 0 and (px is None or px < min_px):
            continue
        if pct is None or pct < min_pct:
            continue
        sp = r.get("spread_pct")
        if max_sp > 0 and (sp is None or sp > max_sp + 1e-12):
            continue
        dv = r.get("dollar_volume")
        if min_dv > 0 and (dv is None or dv < min_dv):
            continue
        out.append({**r, "price": px, "pct_change": pct, "source": "tight", "path": "seed"})
    return out


def door_gates(row: dict, cfg: dict, *, i: int, spread: float | None, gap: float | None) -> str | None:
    """admit_arm_gates as passes_inclusion runs them before admit_range is logged. None = admit."""
    if not bool(cfg.get("ai_watch_admit_arm_gates", False)):
        return None
    src = row.get("source")
    px = row.get("price")
    if px is not None:
        lo = _f(cfg, "ai_watch_min_price", 0.0)
        hi = _f(cfg, "ai_max_price", 0.0)
        if src == "tight" and _f(cfg, "ai_tight_max_price", 0.0) > 0:
            hi = _f(cfg, "ai_tight_max_price", 0.0)
        if lo > 0 and px + 1e-12 < lo:
            return "below_min_price"
        if hi > 0 and px + 1e-12 >= hi:
            return "above_max_price"
    delay = _f(cfg, "ai_movers_sip_delay_min", 15.0)
    max_sp = _f(cfg, "ai_watch_max_sip_spread_pct", 0.0)
    if max_sp > 0 and (i - I_OPEN) >= delay + 1 and spread is not None and spread > max_sp:
        return "spread_wide"
    block = _f(cfg, "ai_watch_gap_down_block_pct", 0.0)
    if block > 0 and gap is not None and gap < -block:
        return "gapped_down"
    return None


def needs_spread(cfg: dict, i: int) -> bool:
    return (bool(cfg.get("ai_watch_admit_arm_gates", False))
            and _f(cfg, "ai_watch_max_sip_spread_pct", 0.0) > 0
            and (i - I_OPEN) >= _f(cfg, "ai_movers_sip_delay_min", 15.0) + 1)


# ═════════════════════════════ market data for one day ═════════════════════════════

class DayMarket:
    """Minute matrices for one session + prior-session daily data + a lazy spread source.

    sip_c / iex_c   last trade close at or before minute i (forward filled; nan before first)
    sip_cv / iex_cv cumulative share volume through minute i (from 04:00)
    sip_rc          like sip_c but RTH bars only (the SIP daily bar's close)
    iex_li          index of the last IEX bar at or before i (-1 none)
    Accessors take the DECISION minute T and read column T-1 (real time) or T-SIP_DELAY (delayed),
    never later: the arrays hold the whole session but no accessor can see past T.
    """

    def __init__(self, day, syms, names, sip, iex, sip_daily, iex_daily, spread_fn=None, failed=None):
        import numpy as np
        self.np = np
        self.day = day
        self.syms = list(syms)
        self.idx = {s: k for k, s in enumerate(self.syms)}
        self.names = names or {}
        n = len(self.syms)
        self.sip_c, self.sip_cv, self.sip_rc, self.sip_o930 = self._mat(sip, rth_close=True)
        self.iex_c, self.iex_cv, _, _ = self._mat(iex, rth_close=False)
        self.iex_li = np.full((n, N_MIN), -1, dtype=np.int16)
        self.iex_first_rth = np.full(n, -1, dtype=np.int32)
        self.iex_open_rth = np.full(n, np.nan)
        for s, bars in (iex or {}).items():
            k = self.idx.get(s)
            if k is None:
                continue
            row = np.full(N_MIN, -1, dtype=np.int32)
            for b in bars:
                if 0 <= b[0] < N_MIN:
                    row[b[0]] = b[0]
            self.iex_li[k] = np.maximum.accumulate(row)
            for b in bars:
                if b[0] >= I_OPEN:
                    self.iex_first_rth[k] = b[0]
                    self.iex_open_rth[k] = b[1]
                    break
        self.sip_daily = sip_daily      # {sym: [(date, o, c, v), ...]} prior sessions, oldest first
        self.iex_daily = iex_daily
        self.spread_fn = spread_fn      # (syms, end_ts) -> {sym: median spread % or None}
        self.failed = set(failed or ())
        self._spread_cache: dict[str, tuple] = {}
        self.spread_requests = 0

    def _mat(self, data, rth_close):
        np = self.np
        n = len(self.syms)
        c = np.full((n, N_MIN), np.nan, dtype=np.float64)
        cv = np.zeros((n, N_MIN), dtype=np.float64)
        rc = np.full((n, N_MIN), np.nan, dtype=np.float64) if rth_close else None
        o930 = np.full(n, np.nan)
        for s, bars in (data or {}).items():
            k = self.idx.get(s)
            if k is None or not bars:
                continue
            row_c = np.full(N_MIN, np.nan)
            row_v = np.zeros(N_MIN)
            row_rc = np.full(N_MIN, np.nan)
            for (m, o, cl, v) in bars:
                if 0 <= m < N_MIN:
                    row_c[m] = cl
                    row_v[m] = v
                    if m >= I_OPEN:
                        row_rc[m] = cl
                    if m == I_OPEN:
                        o930[k] = o
            c[k] = _ffill(row_c)
            cv[k] = np.cumsum(row_v)
            if rth_close:
                rc[k] = _ffill(row_rc)
        return c, cv, rc, o930

    # ── accessors (decision minute T) ──
    def prior(self, s, feed="sip"):
        return (self.sip_daily if feed == "sip" else self.iex_daily).get(s) or []

    def prev_close(self, s, feed="sip"):
        p = self.prior(s, feed)
        return p[-1][2] if p else None

    def rt_price(self, s, T):
        k = self.idx.get(s)
        if k is None or T < 1:
            return None
        v = self.sip_c[k, min(T, N_MIN) - 1]
        return None if math.isnan(v) else float(v)

    def rt_cumvol(self, s, T):
        k = self.idx.get(s)
        return 0.0 if (k is None or T < 1) else float(self.sip_cv[k, min(T, N_MIN) - 1])

    def delayed_today(self, s, T):
        """(close, volume) of today's SIP daily bar as served at T, or None (not served yet).
        Today's bar exists once an RTH minute at or before T-16 has printed; its volume counts
        every trade since 04:00 (the SIP daily bar includes extended hours) up to T-16."""
        k = self.idx.get(s)
        j = min(T, N_MIN) - SIP_DELAY
        if k is None or j < I_OPEN:
            return None
        cl = self.sip_rc[k, j]
        if math.isnan(cl):
            return None
        return float(cl), float(self.sip_cv[k, j])

    def seq(self, s, T):
        prior = [(d, c, v) for (d, o, c, v) in self.prior(s)][-30:]
        today = self.delayed_today(s, T)
        return daily_view(prior, today), today is not None

    def iex_snapshot(self, s, T):
        """(latest IEX trade px, its minute index or None for a prior-day trade, prev IEX close,
        today's IEX volume, prev IEX volume)."""
        k = self.idx.get(s)
        pr = self.prior(s, "iex")
        if k is None or not pr:
            return None
        j = min(T, N_MIN) - 1
        li = int(self.iex_li[k, j]) if j >= 0 else -1
        if li >= 0:
            px, at, vol = float(self.iex_c[k, j]), li, float(self.iex_cv[k, j])
        else:
            px, at, vol = pr[-1][2], None, 0.0
        return px, at, pr[-1][2], vol, pr[-1][3]

    def gap(self, s, T):
        """open_gap_pct as the live desk computes it at T (raw)."""
        k = self.idx.get(s)
        if k is None:
            return None
        if T >= I_OPEN + SIP_DELAY:
            o, pc = self.sip_o930[k], self.prev_close(s)
            if not math.isnan(o) and pc:
                return (o / pc - 1) * 100
        pr = self.prior(s, "iex")
        fr = int(self.iex_first_rth[k])
        if T >= I_OPEN and 0 <= fr <= T - 1 and pr:
            return (float(self.iex_open_rth[k]) / pr[-1][2] - 1) * 100
        if len(pr) >= 2 and pr[-2][2]:
            return (pr[-1][1] / pr[-2][2] - 1) * 100   # premarket: the snapshot's daily bar is yesterday's
        return None

    def spreads(self, syms, T, ttl):
        """sip_spread_pct for each sym at T, live 180 s cache semantics, batched fetch."""
        need = [s for s in syms if s not in self._spread_cache
                or T - self._spread_cache[s][1] >= ttl]
        if need and self.spread_fn is not None:
            # live: median over the 60 s ending (decision time - 16 min)
            got = self.spread_fn(sorted(set(need)), et_ts(self.day, T) - SIP_DELAY * 60)
            self.spread_requests += 1
            for s in set(need):
                self._spread_cache[s] = (got.get(s), T)
        return {s: (self._spread_cache.get(s) or (None, 0))[0] for s in syms}


def _ffill(a):
    import numpy as np
    idx = np.where(~np.isnan(a), np.arange(len(a)), 0)
    np.maximum.accumulate(idx, out=idx)
    out = a[idx]
    if np.isnan(a[0]):
        first = np.argmax(~np.isnan(a)) if (~np.isnan(a)).any() else len(a)
        out[:first] = np.nan
    return out


# ═════════════════════════════ the day simulation ═════════════════════════════

def simulate_day(mk: DayMarket, cfg_at, knobs=None, *, end_i: int = N_MIN) -> dict:
    """Run the producers + book seeds + door at every minute; return first admissions.

    cfg_at(i) -> config dict in force at minute i. Returns {"admits": [...], "stats": {...}}.
    """
    import numpy as np
    kn = {**KNOBS_DEFAULT, **(knobs or {})}
    syms = mk.syms
    pc_sip = np.array([mk.prev_close(s) or np.nan for s in syms], dtype=np.float64)
    common = {s for s in syms if is_common(s) and not is_levered(s, mk.names.get(s, ""))}
    ok = common.__contains__
    session_syms: set[str] = set()
    admitted: dict[str, dict] = {}
    refusals = Counter()
    scan_last_i, scan_last = -10**6, []
    tight_rows, tight_rows_i = [], -10**6
    soft_last_i = -10**6
    tight_uni = None
    stats = Counter()

    for T in range(max(1, kn["seed_start_min"]), end_i):
        cfg = cfg_at(T)
        # ── movers producer (every 60 s, 04:00-20:00) ──
        top = int(kn["gainers_top"] or _i(cfg, "ai_movers_top", 50))
        min_pct = _f(cfg, "ai_movers_min_pct_change", 10.0)
        lo, hi = _f(cfg, "ai_movers_min_price", 2.0), _f(cfg, "ai_movers_max_price", 20.0)
        last = mk.sip_c[:, T - 1]
        pct_v = (last / pc_sip - 1.0) * 100.0
        good = ~np.isnan(pct_v)
        order = np.argsort(-np.where(good, pct_v, -np.inf), kind="stable")[:top]
        pcts = {syms[k]: float(pct_v[k]) for k in order if good[k]}
        prices = {syms[k]: float(last[k]) for k in order if good[k]}
        cand = [(s, p, px) for s, p, px in gainers_select(
            pcts, prices, top=top, min_pct=min_pct, lo=lo, hi=hi, ok=ok)]
        origin = {s: "gainers" for s, _, _ in cand}
        # universe scan (09:35-16:00)
        m_et = DAY0_MIN + T
        if bool(cfg.get("ai_movers_universe_scan", False)) and 9 * 60 + 35 <= m_et < 960:
            every = max(1, int(round(_f(cfg, "ai_movers_scan_sec", 120.0) / 60.0)))
            if T - scan_last_i >= every:
                scan_last_i = T
                scan_last = _scan(mk, T, cfg, lo, hi, min_pct, common)
            for r in scan_last:
                if r["symbol"] not in origin:
                    cand.append((r["symbol"], None, None))
                    origin[r["symbol"]] = "scan"
        if (bool(cfg.get("ai_movers_premarket_feed", False)) and bool(cfg.get("ai_movers_premarket_scan", False))
                and 8 * 60 <= m_et < 9 * 60 + 30):
            for r in _premarket(mk, T, cfg, common):
                if r["symbol"] not in origin:
                    cand.append((r["symbol"], None, None))
                    origin[r["symbol"]] = "premarket"
        if bool(cfg.get("ai_movers_use_most_actives", True)):
            atop = int(kn["actives_top"] or _i(cfg, "ai_movers_actives_top", 50))
            cvc = mk.sip_cv[:, T - 1]
            order = np.argsort(-cvc, kind="stable")[:atop]
            have = {x[0] for x in cand}
            for s in actives_select({syms[k]: float(cvc[k]) for k in order}, top=atop, ok=ok):
                if s not in have:
                    cand.append((s, None, None))
                    have.add(s)
                    origin.setdefault(s, "actives")
        ranked = {s for s, p, _ in cand if p is not None}
        max_session = _i(cfg, "ai_movers_session_max", 40)
        if bool(cfg.get("ai_movers_session_append", True)):
            session_syms |= ranked
            for s in sorted(session_syms - ranked)[:max(0, max_session - len(cand))]:
                cand.append((s, None, None))
        want = _i(cfg, "ai_movers_max_rows", 25)
        mins_open = max(1.0, (m_et - 570) - _f(cfg, "ai_movers_sip_delay_min", 15.0)) \
            if m_et > 570 else float(m_et - 570)
        live_ok = _continuity(mk, T, cfg, [c[0] for c in cand[:want + max_session]])
        rows = []
        for s, p, px in cand[:want + max_session]:
            seq, today_bar = mk.seq(s, T)
            r = measure_row(s, p, px, seq, today_bar=today_bar, mins_open=mins_open, cfg=cfg)
            if r is None:
                continue
            if r.get("_discard"):
                session_syms.discard(s)
                continue
            if live_ok is not None and live_ok.get(s) is False:
                continue
            r["origin"] = origin.get(s, "carry")
            rows.append(r)
        if kn["quote_mode"] == "rt":
            for r in rows:
                rp, pc = mk.rt_price(r["symbol"], T), mk.prev_close(r["symbol"])
                if rp is not None and pc:
                    r["seed_price"], r["seed_pct"] = rp, (rp / pc - 1) * 100
        stats["movers_rows"] += len(rows)

        # ── book seeds ──
        props = []
        if bool(cfg.get("ai_watch_seed_movers", True)):
            props += movers_seed(rows, cfg, max_price=_f(cfg, "ai_max_price", 0.0))
        if kn["soft_seed"] and bool(cfg.get("ai_watch_soft_seed_enabled", True)) \
                and bool(cfg.get("ai_watch_soft_seed_movers", True)):
            iv = max(1, int(round(_f(cfg, "ai_watch_soft_seed_interval_sec", 300.0) / 60.0)))
            if T - soft_last_i >= iv and (not kn["soft_seed_rth_only"] or m_et >= 570):
                soft_last_i = T
                props += soft_seed(rows, cfg)
        if bool(cfg.get("ai_watch_seed_tight", False)) and m_et >= 570 + _f(cfg, "ai_movers_sip_delay_min", 15.0) + 1:
            every = max(1, int(round(_f(cfg, "ai_tight_scan_sec", 300.0) / 60.0)))
            first = I_OPEN + int(_f(cfg, "ai_movers_sip_delay_min", 15.0)) + 1 + int(kn["tight_phase"])
            if T >= first and (T - first) % every == 0:
                if tight_uni is None:
                    tight_uni = _tight_universe(mk, cfg)
                tight_rows, tight_rows_i = _tight_scan(mk, T, cfg, tight_uni, kn), T
            if tight_rows and T - tight_rows_i <= _f(cfg, "ai_tight_max_age_sec", 900.0) / 60.0:
                trs = [dict(r) for r in tight_rows]
                if kn["quote_mode"] == "rt":
                    for r in trs:
                        rp, pc = mk.rt_price(r["symbol"], T), mk.prev_close(r["symbol"])
                        if rp is not None and pc:
                            r["seed_price"], r["seed_pct"] = rp, (rp / pc - 1) * 100
                props += tight_seed(trs, cfg)

        # ── the door ──
        fresh, seen = [], set()
        for r in props:
            s = r["symbol"]
            if s in admitted or s in seen:
                continue
            seen.add(s)
            fresh.append(r)
        if not fresh:
            continue
        sp = mk.spreads([r["symbol"] for r in fresh], T, kn["spread_ttl_min"]) if needs_spread(cfg, T) else {}
        for r in fresh:
            s = r["symbol"]
            why = door_gates(r, cfg, i=T, spread=sp.get(s), gap=mk.gap(s, T))
            if why:
                refusals[why] += 1
                continue
            admitted[s] = {
                "symbol": s, "first_ts": round(et_ts(mk.day, T), 1), "first_et": hhmm(T),
                "source": r["source"], "path": r.get("path"), "origin": r.get("origin"),
                "raw_price": _r(r.get("price")), "pct_change": _r(r.get("pct_change"), 3),
                "rvol": _r(r.get("rvol"), 3), "dollar_volume": _r(r.get("dollar_volume"), 0),
                "spread_pct": _r(sp.get(s) if sp else r.get("spread_pct"), 4),
                "open_gap_pct": _r(mk.gap(s, T), 3),
                "prior_close": _r(mk.prev_close(s)),
            }
    stats["spread_batches"] = mk.spread_requests
    return {"admits": sorted(admitted.values(), key=lambda a: (a["first_ts"], a["symbol"])),
            "refusals": dict(refusals), "stats": dict(stats)}


def _r(v, nd=4):
    try:
        return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), nd)
    except (TypeError, ValueError):
        return None


def _scan(mk, T, cfg, lo, hi, min_pct, common):
    from movers_screener import scan_select
    m = DAY0_MIN + T
    frac = max(0.05, min(1.0, (m - 570) / 390.0))
    snaps = {}
    for s in common:
        sn = mk.iex_snapshot(s, T)
        if sn is None:
            continue
        px, _, pc, vol, pv = sn
        snaps[s] = (px, pc, vol, pv)
    return scan_select(snaps, lo=lo, hi=hi, min_pct=min_pct,
                       min_rvol=_f(cfg, "ai_movers_scan_min_rvol", 1.0),
                       min_iex_dollars=_f(cfg, "ai_movers_scan_min_iex_dollars", 500_000),
                       frac=frac, top=_i(cfg, "ai_movers_scan_top", 25))


def _premarket(mk, T, cfg, common):
    from movers_screener import premarket_select
    snaps = {}
    for s in common:
        sn = mk.iex_snapshot(s, T)
        if sn is None or sn[1] is None:
            continue
        px, at, pc, _, pv = sn
        snaps[s] = (px, et_ts(mk.day, at) + 59, pc, pv)
    return premarket_select(
        snaps, now=et_ts(mk.day, T), lo=_f(cfg, "ai_movers_min_price", 2.0),
        hi=_f(cfg, "ai_movers_max_price", 20.0),
        min_gap=_f(cfg, "ai_movers_premarket_min_gap_pct", 1.5),
        max_trade_age=_f(cfg, "ai_movers_premarket_max_trade_age_sec", 300.0),
        min_prev_dollars=_f(cfg, "ai_movers_premarket_min_prev_dollars", 20e6),
        top=_i(cfg, "ai_movers_scan_top", 25))


def _continuity(mk, T, cfg, syms):
    """ai_movers_min_live_pct tape-continuity filter (off at 0). {sym: passes} or None."""
    mlp = _f(cfg, "ai_movers_min_live_pct", 0.0)
    if mlp <= 0:
        return None
    win = _i(cfg, "ai_movers_live_window_min", 60)
    m = DAY0_MIN + T
    open_min = max(0, min(m, 960) - max(m - win, 570))
    if open_min < _i(cfg, "ai_movers_live_min_open_minutes", 20):
        return None
    mmd = _f(cfg, "ai_movers_min_minute_dollars", 2000.0)
    out = {}
    for s in syms:
        k = mk.idx.get(s)
        if k is None:
            continue
        lo_i, hi_i = max(0, T - win), T - SIP_DELAY
        live = 0
        prev_cv = mk.sip_cv[k, lo_i - 1] if lo_i > 0 else 0.0
        for j in range(lo_i, hi_i + 1):
            v = mk.sip_cv[k, j] - prev_cv
            prev_cv = mk.sip_cv[k, j]
            c = mk.sip_c[k, j]
            if v > 0 and not math.isnan(c) and v * c >= mmd:
                live += 1
        out[s] = (live / float(max(1, open_min))) >= mlp
    return out


def _tight_universe(mk, cfg):
    from tight_screener import universe_select
    try:
        from overnight_book import FUNDISH
    except Exception:  # noqa: BLE001
        FUNDISH = None
    prev = {}
    for s in mk.syms:
        nm = mk.names.get(s, "")
        if not s.isalpha() or not is_common(s) or is_levered(s, nm):
            continue
        if FUNDISH is not None and FUNDISH.search(nm.upper()):
            continue
        p = mk.prior(s)
        if p:
            prev[s] = (p[-1][2], p[-1][2] * p[-1][3])
    return universe_select(prev, min_price=_f(cfg, "ai_tight_min_price", 10.0),
                           min_dollar_volume=_f(cfg, "ai_tight_min_dollar_volume", 50e6),
                           top=_i(cfg, "ai_tight_universe_n", 400))


def _tight_scan(mk, T, cfg, uni, kn):
    from tight_screener import movers_select, tight_select
    now = et_ts(mk.day, T)
    quotes = {}
    for u in uni:
        sn = mk.iex_snapshot(u["symbol"], T)
        if sn is None or sn[1] is None:
            continue
        quotes[u["symbol"]] = (sn[0], et_ts(mk.day, sn[1]) + 59)
    movers = movers_select(uni, quotes, now=now, min_pct=_f(cfg, "ai_tight_min_pct_change", 1.0))
    max_look = _i(cfg, "ai_tight_max_spread_lookups", 40)
    top = _i(cfg, "ai_tight_top", 15)
    max_sp = _f(cfg, "ai_tight_max_spread_pct", 0.03)
    # The live walk looks spreads up one by one in % order until `top` are kept; fetch in chunks
    # along the same order so the batch never reads past where the live walk would stop.
    sp: dict = {}
    picks: list = []
    upto = 0
    while upto < min(len(movers), max_look):
        nxt = min(len(movers), max_look, upto + max(5, top - len(picks)))
        sp.update(mk.spreads([r["symbol"] for r in movers[upto:nxt]], T, 0))
        upto = nxt
        picks, _ = tight_select(movers[:upto], lambda s: sp.get(s), max_spread=max_sp,
                                top=top, max_lookups=max_look)
        if len(picks) >= top:
            break
    rows = []
    m_et = DAY0_MIN + T
    mins_open = max(1.0, (m_et - 570) - _f(cfg, "ai_movers_sip_delay_min", 15.0))
    for p in picks:
        seq, today_bar = mk.seq(p["symbol"], T)
        vol = seq[-1][1] if (seq and today_bar) else None
        prior = [v for _, v in (seq[:-1] if today_bar else seq)][-20:]
        avg = (sum(prior) / len(prior)) if prior else None
        rvol = None
        if vol and avg:
            import morning_funnel as mf
            rvol = mf.rvol_pair(vol, avg, mins_open, time_adjusted=bool(cfg.get("rvol_time_adjusted", True)))[0]
        dv = vol * p["price"] if vol else None
        rows.append({"symbol": p["symbol"], "price": p["price"], "pct_change": p["pct_change"],
                     "spread_pct": p["spread_pct"], "rvol": rvol, "dollar_volume": dv,
                     "origin": "tight"})
    return rows


# ═════════════════════════════ network (Mac mini only) ═════════════════════════════

_LAST_CALL = [0.0]
PACE = 0.3


def _client():
    import bars
    cl = bars.client()
    if cl is None:
        raise SystemExit("no Alpaca data client (run on the Mac mini)")
    try:
        cl._use_raw_data = True
    except Exception:  # noqa: BLE001
        pass
    return cl


def _call(fn, *a, tries=6, **kw):
    """Paced call with backoff. Raises after `tries` failures (caller counts the loss)."""
    err = None
    for k in range(tries):
        wait = PACE - (time.time() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.time()
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            err = e
            msg = str(e)
            print(f"    api error try {k}: {msg[:120]}", flush=True)
            if "invalid symbol" in msg.lower() or "400" in msg[:40]:
                break
            time.sleep(min(60, (8 if "429" in msg or "rate" in msg.lower() else 3) * (2 ** k)))
    raise RuntimeError(f"failed after {tries}: {str(err)[:200]}")


def assets_path():
    return os.path.join(OUT_DIR, "assets_pit.json")


def cmd_assets():
    """Symbol master: every non-OTC US equity asset, ACTIVE and INACTIVE (delisted)."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    from config import load_config
    cfg = load_config() or {}
    out, src = {}, None
    for paper in (True, False):
        try:
            tc = TradingClient(cfg.get("api_key"), cfg.get("secret_key"), paper=paper)
            for st in (AssetStatus.ACTIVE, AssetStatus.INACTIVE):
                for a in tc.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=st)):
                    exch = str(getattr(a, "exchange", "") or "").split(".")[-1].upper()
                    if "OTC" in exch:
                        continue
                    out[a.symbol] = {"name": a.name or "", "exchange": exch,
                                     "status": str(getattr(a, "status", "")).split(".")[-1].lower()}
            src = "paper" if paper else "live"
            break
        except Exception as e:  # noqa: BLE001
            print(f"assets via {'paper' if paper else 'live'} failed: {e}", flush=True)
    if not out:
        import json as _j
        alls = _j.load(open(os.path.join(ROOT, "ai_reports", "allsym", "assets.json")))
        out = {s: {"name": n, "exchange": "", "status": "unknown"} for s, n in alls.items()}
        src = "allsym/assets.json"
    os.makedirs(OUT_DIR, exist_ok=True)
    meta = {"fetched_at": datetime.now(ET).isoformat(timespec="seconds"), "via": src,
            "n": len(out), "n_inactive": sum(1 for v in out.values() if v["status"] == "inactive"),
            "note": "Alpaca US_EQUITY, status ACTIVE + INACTIVE, non-OTC; per-day PIT = has a raw "
                    "SIP daily bar on the prior session"}
    json.dump({"meta": meta, "assets": out}, open(assets_path(), "w"))
    print(meta, flush=True)


def valid_symbol(s: str) -> bool:
    """Exchange tickers only (CUSIP-like CVR/escrow ids are not quotable symbols)."""
    import re
    return bool(re.fullmatch(r"[A-Z]{1,6}(\.[A-Z]{1,2})?", s or ""))


def load_assets():
    a = json.load(open(assets_path()))
    return a["meta"], {s: v for s, v in a["assets"].items() if valid_symbol(s)}


def _bisect_fetch(fetch, chunk, failed, label):
    """fetch(chunk) -> dict; on error split the chunk so one bad symbol cannot sink 199 others."""
    try:
        return fetch(chunk) or {}
    except Exception as e:  # noqa: BLE001
        if len(chunk) == 1:
            print(f"  {label} {chunk[0]} FAILED: {str(e)[:100]}", flush=True)
            failed.update(chunk)
            return {}
        h = len(chunk) // 2
        out = _bisect_fetch(fetch, chunk[:h], failed, label)
        out.update(_bisect_fetch(fetch, chunk[h:], failed, label))
        return out


def daily_path(feed):
    return os.path.join(CACHE_DIR, f"daily_raw_{feed}.pkl")


def cmd_daily(lo, hi):
    """Raw SIP + IEX daily bars for every asset in the master, lo..hi."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    os.makedirs(CACHE_DIR, exist_ok=True)
    _, assets = load_assets()
    syms = sorted(assets)
    cl = _client()
    start = datetime.strptime(lo, "%Y-%m-%d").replace(tzinfo=ET)
    end = datetime.strptime(hi, "%Y-%m-%d").replace(hour=23, tzinfo=ET)
    for feed in ("sip", "iex"):
        data, failed = {}, set()

        def fetch(chunk, feed=feed):
            return _call(cl.get_stock_bars, StockBarsRequest(
                symbol_or_symbols=chunk, timeframe=TimeFrame.Day, start=start, end=end,
                feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX, adjustment=Adjustment.RAW))
        for i in range(0, len(syms), 100):
            chunk = syms[i:i + 100]
            r = _bisect_fetch(fetch, chunk, failed, f"daily {feed}")
            for s, rows in r.items():
                data[s] = [(b["t"][:10], float(b["o"]), float(b["c"]), float(b["v"])) for b in rows]
            if i % 2000 == 0:
                print(f"  {feed} {i}/{len(syms)} -> {len(data)} with bars", flush=True)
        # Daily bars are stamped 04:00Z = the ET session date (00:00 ET).
        pickle.dump({"lo": lo, "hi": hi, "data": data, "failed": sorted(failed)}, open(daily_path(feed), "wb"))
        print(f"{feed}: {len(data)} symbols, {len(failed)} failed", flush=True)


_DAILY = {}


def daily(feed):
    if feed not in _DAILY:
        _DAILY[feed] = pickle.load(open(daily_path(feed), "rb"))
    return _DAILY[feed]


def trading_days(lo, hi):
    spy = daily("sip")["data"].get("SPY") or []
    return [d for d, *_ in spy if lo <= d <= hi]


def _prior_daily(feed, day, n=31):
    """{sym: [(date, o, c, v)...]} strictly before `day` (prior sessions only)."""
    out = {}
    for s, rows in daily(feed)["data"].items():
        k = bisect.bisect_left([r[0] for r in rows], day) if len(rows) > 64 else \
            sum(1 for r in rows if r[0] < day)
        if k:
            out[s] = rows[max(0, k - n):k]
    return out


def fetch_minutes(cl, syms, day, feed, failed):
    """{sym: [(i, o, c, v), ...]} for 04:00-16:00 of day; failed chunks appended to `failed`."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(hour=4, tzinfo=ET)
    out = {}
    for i in range(0, len(syms), 200):
        chunk = syms[i:i + 200]

        def fetch(ch):
            return _call(cl.get_stock_bars, StockBarsRequest(
                symbol_or_symbols=ch, timeframe=TimeFrame.Minute, start=d0,
                end=d0 + timedelta(hours=12), feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
                adjustment=Adjustment.RAW))
        r = _bisect_fetch(fetch, chunk, failed, f"{day} {feed} minute")
        base = d0.timestamp()
        for s, rows in (r or {}).items():
            bars = []
            for b in rows:
                t = datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp()
                bars.append((int((t - base) // 60), float(b["o"]), float(b["c"]), float(b["v"])))
            out[s] = bars
    return out


def make_spread_fn(cl, cache: dict, failed_q: list):
    """(syms, end_ts) -> {sym: median SIP (ask-bid)/mid % over [end-60s, end]} — sip_spread_pct."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockQuotesRequest

    def fn(syms, end_ts):
        key_end = int(end_ts)
        res, need = {}, []
        for s in syms:
            k = f"{s}|{key_end}"
            if k in cache:
                res[s] = cache[k]
            else:
                need.append(s)
        for i in range(0, len(need), 100):
            chunk = need[i:i + 100]
            end = datetime.fromtimestamp(key_end, tz=timezone.utc)
            try:
                q = _call(cl.get_stock_quotes, StockQuotesRequest(
                    symbol_or_symbols=chunk, start=end - timedelta(seconds=60), end=end,
                    feed=DataFeed.SIP))
            except Exception as e:  # noqa: BLE001
                failed_q.append((key_end, len(chunk), str(e)[:80]))
                q = None
            for s in chunk:
                val = None
                if q is not None:
                    sp = sorted((float(x["ap"]) - float(x["bp"])) / ((float(x["ap"]) + float(x["bp"])) / 2) * 100
                                for x in (q.get(s) or [])
                                if x.get("bp") and x.get("ap") and float(x["ap"]) >= float(x["bp"]))
                    val = sp[len(sp) // 2] if sp else None
                    cache[f"{s}|{key_end}"] = val
                res[s] = val
        return res
    return fn


def load_day_market(day, *, use_cache=False, cl=None):
    """Fetch (or load cached) everything simulate_day needs for one session."""
    import numpy as np  # noqa: F401
    meta, assets = load_assets()
    cache_p = os.path.join(CACHE_DIR, "min", f"{day}.pkl.gz")
    sip_prior = _prior_daily("sip", day)
    iex_prior = _prior_daily("iex", day)
    tdays = trading_days("2000-01-01", day)
    prev_day = tdays[-2] if len(tdays) >= 2 and tdays[-1] == day else (tdays[-1] if tdays else None)
    universe = sorted(s for s, rows in sip_prior.items() if s in assets and rows and rows[-1][0] == prev_day)
    failed: set[str] = set()
    if use_cache and os.path.exists(cache_p):
        blob = pickle.load(gzip.open(cache_p, "rb"))
        sip, iex, failed = blob["sip"], blob["iex"], set(blob["failed"])
    else:
        cl = cl or _client()
        t0 = time.time()
        sip = fetch_minutes(cl, universe, day, "sip", failed)
        iex = fetch_minutes(cl, universe, day, "iex", failed)
        print(f"  {day}: minute bars {len(universe)} syms, sip {len(sip)} iex {len(iex)}, "
              f"failed {len(failed)}, {time.time() - t0:.0f}s", flush=True)
        if use_cache:
            os.makedirs(os.path.dirname(cache_p), exist_ok=True)
            pickle.dump({"sip": sip, "iex": iex, "failed": sorted(failed)}, gzip.open(cache_p, "wb"))
    names = {s: assets[s].get("name", "") for s in universe}
    sq_path = os.path.join(CACHE_DIR, "spreads", f"{day}.json")
    sq_cache = json.load(open(sq_path)) if os.path.exists(sq_path) else {}
    failed_q: list = []
    spread_fn = make_spread_fn(cl or _client(), sq_cache, failed_q)
    mk = DayMarket(day, universe, names, sip, iex,
                   {s: sip_prior[s] for s in universe}, {s: iex_prior.get(s) or [] for s in universe},
                   spread_fn=spread_fn, failed=failed)
    mk._sq = (sq_path, sq_cache, failed_q)
    mk._prev_day = prev_day
    mk._asset_meta = meta
    return mk


def save_spread_cache(mk):
    p, c, _ = mk._sq
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(c, open(p, "w"))


# ═════════════════════════════ config ═════════════════════════════

def live_config():
    from config import load_config
    cfg = load_config() or {}
    raw = open(os.path.join(ROOT, "config", "bot_config.json"), "rb").read()
    return cfg, hashlib.sha256(raw).hexdigest()[:16]


def _defaults():
    from config import DEFAULT_CONFIG
    return dict(DEFAULT_CONFIG)


def recorded_config_fn(day):
    """cfg_at(i) from sessions/DAY/config.jsonl.gz (latest snapshot <= minute), nearest day else."""
    base = _defaults()
    sdir = os.path.join(ROOT, "ai_reports", "sessions")

    def load(d):
        p = os.path.join(sdir, d, "config.jsonl.gz")
        if not os.path.exists(p):
            return []
        out = []
        for line in gzip.open(p, "rt"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            c = r.get("config")
            if isinstance(c, dict):
                out.append((float(r.get("ts") or 0), {**base, **c}))
        return sorted(out, key=lambda t: t[0])

    snaps = load(day)
    note = f"sessions/{day}/config.jsonl.gz"
    if not snaps:
        days = sorted(d for d in os.listdir(sdir) if os.path.exists(os.path.join(sdir, d, "config.jsonl.gz")))
        before = [d for d in days if d < day]
        after = [d for d in days if d > day]
        if before:
            snaps = load(before[-1])[-1:]
            note = f"last snapshot of {before[-1]} (no config recorded {day})"
        elif after:
            snaps = load(after[0])[:1]
            note = f"first snapshot of {after[0]} (no config recorded {day})"
    ts_list = [t for t, _ in snaps]

    def cfg_at(i):
        t = et_ts(day, i)
        k = bisect.bisect_right(ts_list, t) - 1
        return snaps[max(0, k)][1]
    return cfg_at, note, snaps


# ═════════════════════════════ build ═════════════════════════════

def build_day(day, *, cfg_mode="live", knobs=None, use_cache=False, write=True, quiet=False):
    t0 = time.time()
    mk = load_day_market(day, use_cache=use_cache)
    if cfg_mode == "live":
        cfg, sha = live_config()
        cfg_at, cfg_note = (lambda i: cfg), f"config/bot_config.json sha256:{sha} (fixed, built {datetime.now(ET):%Y-%m-%d %H:%M})"
        cfg_used = {k: cfg.get(k) for k in CFG_KEYS}
    else:
        cfg_at, cfg_note, snaps = recorded_config_fn(day)
        cfg_used = {k: cfg_at(I_OPEN).get(k) for k in CFG_KEYS}
    res = simulate_day(mk, cfg_at, knobs)
    save_spread_cache(mk)
    n_uni = len(mk.syms)
    n_failed = len(mk.failed)
    out = {
        "day": day, "rule_version": RULE_VERSION, "knobs": {**KNOBS_DEFAULT, **(knobs or {})},
        "config": cfg_note, "config_values": cfg_used,
        "symbol_master": {**mk._asset_meta, "per_day": f"assets with a raw SIP daily bar on {mk._prev_day}"},
        "sources_covered": list(COVERED), "sources_uncovered": list(UNCOVERED),
        "universe_size": n_uni, "failed_fetch_names": n_failed,
        "failed_fetch_share": round(n_failed / n_uni, 4) if n_uni else None,
        "failed_names": sorted(mk.failed)[:500],
        "spread_fetch_failures": len(mk._sq[2]), "spread_batches": mk.spread_requests,
        "excluded_gt10pct_failed": bool(n_uni and n_failed / n_uni > 0.10),
        "n_admits": len(res["admits"]), "by_source": dict(Counter(a["source"] for a in res["admits"])),
        "refusals": res["refusals"], "stats": res["stats"],
        "built_at": datetime.now(ET).isoformat(timespec="seconds"), "secs": round(time.time() - t0, 1),
        "point_in_time": "decisions at minute T read bars closed before T (real time) or T-16 "
                         "(delayed SIP), prior-session daily bars only; raw prices",
        "admits": res["admits"],
    }
    if write:
        os.makedirs(OUT_DIR, exist_ok=True)
        sub = "" if cfg_mode == "live" else "calib_"
        json.dump(out, open(os.path.join(OUT_DIR, f"{sub}{day}.json"), "w"), indent=1)
    if not quiet:
        print(f"{day}: {len(res['admits'])} admits {out['by_source']} uni {n_uni} failed {n_failed} "
              f"spread_batches {mk.spread_requests} {out['secs']}s", flush=True)
    return out, mk


def cmd_build_range(lo, hi, newest_first=True, skip_existing=True):
    days = [d for d in trading_days(lo, hi)]
    if newest_first:
        days = days[::-1]
    log = os.path.join(OUT_DIR, "build_log.jsonl")
    for d in days:
        p = os.path.join(OUT_DIR, f"{d}.json")
        if skip_existing and os.path.exists(p):
            continue
        try:
            out, _ = build_day(d)
            rec = {k: out[k] for k in ("day", "universe_size", "failed_fetch_names", "failed_fetch_share",
                                        "excluded_gt10pct_failed", "n_admits", "by_source",
                                        "spread_fetch_failures", "secs", "config")}
        except Exception as e:  # noqa: BLE001
            rec = {"day": d, "error": str(e)[:300]}
            print(f"{d}: ERROR {e}", flush=True)
        with open(log, "a") as f:
            f.write(json.dumps(rec) + "\n")


# ═════════════════════════════ check (calibration days only) ═════════════════════════════

def recorded_admits(lo, hi):
    """{(day, sym): first admit_range row} for lo..hi."""
    first = {}
    for line in open(os.path.join(ROOT, "ai_reports", "admit_range.jsonl")):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        d = r.get("day") or ""
        if not (lo <= d <= hi):
            continue
        k = (d, str(r.get("symbol") or "").upper())
        if k not in first or float(r["ts"]) < float(first[k]["ts"]):
            first[k] = r
    return first


def recorded_trades(lo, hi):
    """{(day, sym): entry ts} from position_shadow.jsonl (first entry of the name-day)."""
    out = {}
    lo_ts = datetime.strptime(lo, "%Y-%m-%d").replace(tzinfo=ET).timestamp()
    hi_ts = (datetime.strptime(hi, "%Y-%m-%d").replace(tzinfo=ET) + timedelta(days=1)).timestamp()
    with open(os.path.join(ROOT, "ai_reports", "position_shadow.jsonl")) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = float(r.get("ts") or 0)
            if not (lo_ts <= ts < hi_ts):
                continue
            hs = r.get("hold_sec")
            ent = ts - max(0.0, float(hs)) if hs is not None else ts
            d = datetime.fromtimestamp(ent, ET).strftime("%Y-%m-%d")
            k = (d, str(r.get("symbol") or "").upper())
            if k not in out or ent < out[k]["ts"]:
                out[k] = {"ts": ent, "source": r.get("source")}
    return out


def score_day(day, admits, rec, trades, *, window_min=10):
    """Recall / precision of one reconstructed day against the recorded admit_range."""
    recon = {a["symbol"]: a for a in admits}
    rday = {s: r for (d, s), r in rec.items() if d == day}
    tday = {s: t for (d, s), t in trades.items() if d == day}
    res = {"day": day, "recorded": len(rday), "reconstructed": len(recon)}
    by_src = Counter(r.get("source") for r in rday.values())
    res["recorded_by_source"] = dict(by_src)
    res["uncovered_share"] = round(sum(v for k, v in by_src.items() if k not in COVERED) / max(1, len(rday)), 3)

    def hit(s, r):
        a = recon.get(s)
        if a is None:
            return None
        return (a["first_ts"] - float(r["ts"])) / 60.0

    per = {}
    for src in ("movers", "tight", "momentum", "trending", "agy", "xai", "bb_live"):
        names = [s for s, r in rday.items() if r.get("source") == src]
        dts = [hit(s, rday[s]) for s in names]
        per[src] = {"n": len(names),
                    "any": sum(1 for x in dts if x is not None),
                    "win": sum(1 for x in dts if x is not None and abs(x) <= window_min),
                    "early": sum(1 for x in dts if x is not None and x < -window_min),
                    "late": sum(1 for x in dts if x is not None and x > window_min)}
    res["per_source"] = per
    pre = [s for s, r in rday.items() if r.get("source") in PREREG_SOURCES]
    res["prereg_n"] = len(pre)
    res["prereg_win"] = sum(1 for s in pre if (lambda x: x is not None and abs(x) <= window_min)(hit(s, rday[s])))
    cov = [s for s, r in rday.items() if r.get("source") in COVERED]
    res["covered_n"] = len(cov)
    res["covered_win"] = sum(1 for s in cov if (lambda x: x is not None and abs(x) <= window_min)(hit(s, rday[s])))
    # traded names whose first admission came from a prereg source
    tr = [s for s in tday if s in rday and rday[s].get("source") in PREREG_SOURCES]
    res["traded_n"] = len(tr)
    res["traded_win"] = sum(1 for s in tr if (lambda x: x is not None and abs(x) <= window_min)(hit(s, rday[s])))
    res["traded_before_entry"] = sum(1 for s in tr if s in recon and recon[s]["first_ts"] <= tday[s]["ts"])
    trc = [s for s in tr if rday[s].get("source") in COVERED]
    res["traded_cov_n"] = len(trc)
    res["traded_cov_win"] = sum(1 for s in trc if (lambda x: x is not None and abs(x) <= window_min)(hit(s, rday[s])))
    res["traded_missed"] = sorted(s for s in tr if not (lambda x: x is not None and abs(x) <= window_min)(hit(s, rday[s])))
    # precision
    res["prec_any"] = sum(1 for s in recon if s in rday)
    res["prec_win"] = sum(1 for s, a in recon.items()
                          if s in rday and abs(a["first_ts"] - float(rday[s]["ts"])) / 60.0 <= window_min)
    res["recon_by_source"] = dict(Counter(a["source"] for a in admits))
    return res


def _pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def cmd_check(span, *, knobs=None, use_cache=True, out_name="check"):
    lo, hi = span.split("..") if ".." in span else (span, span)
    rec = recorded_admits(lo, hi)
    trades = recorded_trades(lo, hi)
    rows = []
    for d in trading_days(lo, hi):
        out, _ = build_day(d, cfg_mode="recorded", knobs=knobs, use_cache=use_cache, write=True, quiet=True)
        r = score_day(d, out["admits"], rec, trades)
        r["universe_size"], r["failed"] = out["universe_size"], out["failed_fetch_names"]
        rows.append(r)
        print(f"{d}: prereg recall {_pct(r['prereg_win'], r['prereg_n'])}% ({r['prereg_win']}/{r['prereg_n']}) "
              f"covered {_pct(r['covered_win'], r['covered_n'])}% traded {_pct(r['traded_win'], r['traded_n'])}% "
              f"({r['traded_win']}/{r['traded_n']}) prec {_pct(r['prec_win'], r['reconstructed'])}% "
              f"recon {r['reconstructed']} {r['recon_by_source']}", flush=True)
    pooled = {k: sum(r[k] for r in rows) for k in ("prereg_n", "prereg_win", "covered_n", "covered_win",
                                                   "traded_n", "traded_win", "traded_cov_n", "traded_cov_win",
                                                   "traded_before_entry", "reconstructed", "prec_any",
                                                   "prec_win", "recorded")}
    per_src = defaultdict(Counter)
    for r in rows:
        for s, v in r["per_source"].items():
            per_src[s].update(v)
    rep = {"span": span, "rule_version": RULE_VERSION, "knobs": {**KNOBS_DEFAULT, **(knobs or {})},
           "window_min": 10, "floors": {"prereg_recall": 70.0, "traded_recall": 70.0},
           "days": rows, "pooled": pooled, "per_source": {k: dict(v) for k, v in per_src.items()}}
    rep["pooled_pct"] = {
        "prereg_recall": _pct(pooled["prereg_win"], pooled["prereg_n"]),
        "covered_recall": _pct(pooled["covered_win"], pooled["covered_n"]),
        "traded_recall": _pct(pooled["traded_win"], pooled["traded_n"]),
        "traded_cov_recall": _pct(pooled["traded_cov_win"], pooled["traded_cov_n"]),
        "traded_admitted_before_entry": _pct(pooled["traded_before_entry"], pooled["traded_n"]),
        "precision_any": _pct(pooled["prec_any"], pooled["reconstructed"]),
        "precision_win": _pct(pooled["prec_win"], pooled["reconstructed"]),
        "uncovered_share": _pct(sum(r["recorded"] - r["covered_n"] for r in rows), pooled["recorded"]),
        "prereg_uncovered_share": _pct(sum(r["prereg_n"] - r["covered_n"] for r in rows), pooled["prereg_n"]),
    }
    for r in rows:
        r["pass_prereg"] = (_pct(r["prereg_win"], r["prereg_n"]) or 0) >= 70.0
        r["pass_traded"] = (_pct(r["traded_win"], r["traded_n"]) or 0) >= 70.0 if r["traded_n"] else None
    rep["pass_pooled"] = {"prereg": (rep["pooled_pct"]["prereg_recall"] or 0) >= 70.0,
                          "traded": (rep["pooled_pct"]["traded_recall"] or 0) >= 70.0}
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(rep, open(os.path.join(OUT_DIR, f"{out_name}.json"), "w"), indent=1)
    print(json.dumps({"pooled_pct": rep["pooled_pct"], "pass_pooled": rep["pass_pooled"],
                      "per_source": rep["per_source"]}, indent=1), flush=True)
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("assets")
    p = sub.add_parser("daily"); p.add_argument("lo"); p.add_argument("hi")
    p = sub.add_parser("build"); p.add_argument("days", nargs="+")
    p = sub.add_parser("build-range"); p.add_argument("lo"); p.add_argument("hi")
    p.add_argument("--oldest-first", action="store_true"); p.add_argument("--rebuild", action="store_true")
    p = sub.add_parser("check"); p.add_argument("span"); p.add_argument("--set", action="append", default=[])
    p.add_argument("--out", default="check"); p.add_argument("--no-cache", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "assets":
        cmd_assets()
    elif a.cmd == "daily":
        cmd_daily(a.lo, a.hi)
    elif a.cmd == "build":
        for d in a.days:
            if CALIB_LO <= d <= CALIB_HI:
                print(f"{d} is a calibration day: building with the fixed live config anyway", flush=True)
            build_day(d)
    elif a.cmd == "build-range":
        cmd_build_range(a.lo, a.hi, newest_first=not a.oldest_first, skip_existing=not a.rebuild)
    elif a.cmd == "check":
        lo, hi = a.span.split("..") if ".." in a.span else (a.span, a.span)
        if lo < CALIB_LO or hi > CALIB_HI:
            raise SystemExit(f"check is for calibration days {CALIB_LO}..{CALIB_HI} only (held-out days stay unscored)")
        kn = {}
        for kv in a.set:
            k, v = kv.split("=", 1)
            kn[k] = json.loads(v) if v[:1] in "0123456789-[{tfn" else v
        cmd_check(a.span, knobs=kn, use_cache=not a.no_cache, out_name=a.out)


if __name__ == "__main__":
    main()
