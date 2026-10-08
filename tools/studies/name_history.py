#!/usr/bin/env python3
"""name_history.py - does a stock's OWN recent history (T1 follow-through, T2 15-min autocorrelation) and its higher
timeframe trend (HTF_UP) predict whether a new intraday high keeps going?  Also computes the ROUND-NUMBER groups.

Exactly per docs/studies/name_history_prereg.json (amended through 99c3585) and docs/studies/round_numbers_prereg.json
(amended 0f55822). Both were registered before any script or data. Every rule names the prereg clause it implements;
where a prereg is silent the choice is in RESOLUTIONS (also written to result.json).

Commands (on the mini; fetch AFTER HOURS ONLY - every request shares the desk's Alpaca budget):
  fetch    calendar, assets, daily bars (raw + adjusted), SIP 1-min bars, SIP NBBO quotes (events, controls, SPY)
  count    OUTCOME-BLIND: events, features, groups (FROZEN to WORK/frozen_groups.json, sha256 logged), n and distinct
           names per group per half; round_numbers' projected MDE from the same-name CONTROL arm (no event outcome)
  score    outcomes, power block (SE / MDE per half), statistics, verdicts            -> WORK/result.json
  report   WORK/report.md from result.json

Shared cache (bars, quotes) with structure_pullback.py: ai_reports/history_studies/cache. count/score/report never
touch the network (a cache miss is a fetch failure). A failed request is never cached.

USAGE (mini):  .venv/bin/python tools/studies/name_history.py fetch|count|score|report
"""
from __future__ import annotations

import collections
import gzip
import hashlib
import json
import math
import os
import random
import statistics
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "tools", "studies")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import bars_structure as BS  # noqa: E402
import bro_sr_wr as BRO  # noqa: E402

CACHE_WORK = os.environ.get("HIST_CACHE") or os.path.join(ROOT, "ai_reports", "history_studies")
WORK = os.environ.get("NAME_HISTORY_WORK") or os.path.join(ROOT, "ai_reports", "name_history")
PREREG = ("docs/studies/name_history_prereg.json", "docs/studies/round_numbers_prereg.json")

# ------------------------------------------------------------------ prereg constants
DATA_LO, TEST_HI = "2026-05-26", "2026-10-07"   # data: sessions 2026-05-26..2026-10-07
LOOKBACK = 20                                   # first 20 look-back only; T1/T2/T3/beta over the prior 20 sessions
TOP_N, MIN_PX = 400, 10.0                       # universe
SPY = "SPY"
ENTRY_LAG, HOLD30, HOLD15 = 5.0, 30 * 60.0, 15 * 60.0   # entry at the mid at t + 5 s; exits at entry + 30 / + 15 min
QUOTE_MAX_AGE = 5.0                             # quotes must be <= 5 s old
EV_LO, EV_HI = 10 * 60 + 30, 15 * 60            # 10:30 <= bar close time <= 15:00
T1_MIN_EVENTS = 8                               # T1: >= 8 events required
BETA_MIN_PAIRS = 100                            # beta: >= 100 pairs
CTRL_SEED, CTRL_LO, CTRL_HI, CTRL_GAP = 61, 9 * 60 + 45, 15 * 60, 30 * 60   # same-name control
Z_NH, Z_RN, Z_POS = 2.24, 1.96, 1.64            # Bonferroni 2 / one hypothesis / control POSITIVE
PASS_BP, UNDER_BP, FAIL_T, POWER_K = 5.0, 10.0, -2.0, 0.84
DROP_NAMES, DROP_SESSIONS = 5, 3
DROP_RATE_PP = 0.02                             # groups' drop rates differ by > 2 pp -> FAILED-DATA
FAIL_SHARE = 0.02                               # abort if > 2% of name-days fail
RN_BAND = 0.0015
PRICE_BANDS = ((10, 20, "10-20"), (20, 50, "20-50"), (50, 100, "50-100"), (100, 1e18, "100+"))
DAILY_BACK = 60                                 # daily bars, 60 sessions before each test session
SPLIT_TOL = 0.10

RESOLUTIONS = {
    "R1_bar_time": "an Alpaca 1-min bar is stamped at its START; its close time t = start + 60 s; 15/60-min bars are "
                   "built from RTH 1-min bars aligned to 09:30 and are completed when end <= t; a bucket with no "
                   "1-min bar is no bar",
    "R2_adjusted": "ADJUSTED 1-min prices = RAW 1-min prices x that session's factor (adjusted daily close / raw "
                   "daily close, SIP daily bars, adjustment=all): Alpaca's adjustment is one factor per date, so this "
                   "equals the adjusted bars and halves the fetch",
    "R3_split": "a split inside the look-back = the factor ratio between consecutive sessions (D-20-1..D) differs "
                "from 1 by > 10% (regular dividends move it far less); the name-day is dropped (counted)",
    "R4_early_close": "sessions whose calendar close is before 16:00 are excluded from the test sessions (counted): "
                      "the 10:30-15:00 window and the 30-min exit assume a 16:00 close",
    "R5_prior_close": "event (b) uses the RAW SIP daily close of D-1; the T1 look-back events use the ADJUSTED "
                      "daily close of the session before each look-back session (consistent with adjusted bars)",
    "R6_T1_window": "T1 race bars: start >= the event close time and end <= min(event close time + 30 min, 15:30)",
    "R7_T2_returns": "15-min returns close-to-close within the session, the 09:30 bar from its own open; pairs are "
                     "adjacent buckets only (a missing bucket breaks the pair); T2 = Pearson over all pooled pairs "
                     "(>= 3 pairs, else missing)",
    "R8_T3": "T3 over the prior sessions with RTH bars: |last close - first open| / (RTH high - RTH low) >= 0.6",
    "R9_trend_score_rank": "percentile rank = (n below + 0.5 x n equal incl. self) / n among that session's events "
                           "with T1 and T2 both defined",
    "R10_terciles": "terciles of TREND_SCORE pooled over all test-session events: rank percentile p of the score "
                    "among all scored events (same rule as R9); BOTTOM p < 1/3, TOP p >= 2/3",
    "R11_HTF_60": "the 60-min EMA series runs over ADJUSTED 60-min bars of the 20 look-back sessions + today; "
                  "missing (event excluded from H2, counted in count.json: htf_missing_no_factor_today, "
                  "htf_missing_ema_warmup_lt141_bars, htf_missing_lt3_15min_bars) when fewer than 140 completed "
                  "bars precede the last; "
                  "the 15-min comparisons use today's RAW 15-min bars",
    "R12_beta": "beta returns = aligned 30-min bars (09:30-10:00 ... 15:30-16:00), close-to-close within session, "
                "first bar from its open; pairs need both name and SPY bars in the same bucket",
    "R13_hedge_quotes": "SPY legs use SPY's SIP NBBO mid with the same <= 5 s rule; a stale SPY quote drops the event "
                        "(counted, as a stale name quote)",
    "R14_net15": "net15 is information: a stale +15 min quote only makes net15 missing (never drops the event)",
    "R15_control_minutes": "control minutes are CLOCK minutes (bar close times) 09:45..15:00 in the event's clock "
                           "hour with |minute - t| > 30 min; drawn with random.Random(int(sha256('61|SYM|DAY')"
                           "[:16], 16)).choice over the sorted legal minutes (no bar data is read to choose); hour "
                           "fallback: next later hour, then the earlier hour; none -> no control (counted); a stale "
                           "control quote drops the control (counted)",
    "R16_control_set": "controls are drawn for every event in H1 TOP, H2 HTF_UP true, and round-number ABOVE (one "
                       "per event, shared); round_numbers' projected SE uses the H1-TOP U HTF_UP controls (the "
                       "name_history control arm) only",
    "R17_fe_ols": "OLS with fixed effects by alternating projections (Frisch-Waugh); two-way clustered variance "
                  "V = V_session + V_name - V_session-x-name, each CR1-scaled G/(G-1); if V <= 0, max(V_session, "
                  "V_name); df = min(sessions, names) - 1",
    "R18_drop_share": "share = the unit's summed contribution to the simple group-mean difference "
                      "(sum_better y / n_better - sum_other y / n_other); the 5 names / 3 sessions with the largest "
                      "contribution are dropped and the SAME FE regression is re-run",
    "R19_better_group": "the better group is the hypothesised one (H1 TOP, H2 HTF_UP true, round numbers ABOVE)",
    "R20_halves": "halves = alternate test sessions in calendar order (index 0, 2, ... = A; 1, 3, ... = B)",
    "R21_power_nh": "name_history MDE per half = (t_crit(half df) + 0.84) x the half's two-way FE SE; computed and "
                    "printed in the power block before any mean, with n events and distinct names per group per "
                    "half after every drop (count.json carries them after the outcome-blind beta drops)",
    "R22_rn_projection": "round_numbers projected SE per half = SE_ctrl x sqrt(n_ctrl) x sqrt(1/n_ABOVE + "
                         "1/n_BELOW), SE_ctrl = two-way SE of the control arm's mean hedged net30 in that half; "
                         "t_crit at min(sessions, names) - 1 of that half's ABOVE U BELOW events; the frozen "
                         "projection decides 'powered' for round_numbers",
    "R23_rn_control": "'ABOVE beats its control' = mean(ABOVE event - its control, hedged net30) > 0 with two-way "
                      "t >= 1.64 (the POSITIVE rule); else a PASS is labelled RELATIVE ONLY",
    "R24_drop_rate": "drop rate = events dropped for stale/missing quotes (stale_quote, stale_spy_quote, "
                     "quote_fetch_fail) / events in the group that reached the quote step (beta_pairs drops are in "
                     "neither); a FAILED-DATA hypothesis's verdict reads FAILED-DATA, never PASS; FAILED-DATA when "
                     "the two groups of any hypothesis (H1 TOP vs BOTTOM, H2 true vs false, RN ABOVE vs BELOW) "
                     "differ by > 2 pp; for round_numbers it sets that verdict FAILED-DATA",
    "R25_levels": "LEVELS (raw): prior 5 sessions' RTH highs/lows from raw 1-min bars, prior raw daily close, "
                  "today's premarket (04:00-09:30) high, 20-session RTH high; round numbers $1 under $50 else $5; "
                  "nearest level strictly above the close; confluence = levels in (close, close x 1.002]",
    "R26_atr_sma": "ATR14 = simple mean true range of the prior 14 adjusted daily bars, scaled to raw by the D-1 "
                   "factor; SMA50 of the prior 50 adjusted daily closes vs the D-1 adjusted close",
    "R27_liquidity": "prior-day high = D-1 RTH high (raw 1-min); next older swing high = the most recent RAW 60-min "
                     "swing high known at t whose bar is before D-1 and whose price > the prior-day high",
    "R28_mirror": "short mirror: first bar 10:30-15:00 with close <= 0.999 x the RTH low through t-1 and RTH low "
                  "through t <= 0.995 x prior close; short net30 = -(mid return) - costs; hedged = -(return - beta x "
                  "SPY) - costs; TREND_SCORE ranked among that session's mirror events",
    "R29_rn_info_grids": "whole-dollar-only and half-dollar-only cells use $1 levels / x.50 levels at every price",
    "R30_range_budget_3R": "name_history has no stop/target: the '3R target beyond the remaining ATR budget' half of "
                           "the RANGE BUDGET cell is not computed here (it is in structure_pullback)",
    "R31_no_bars": "a top-400 name with no 1-min bar 04:00-16:00 on D is counted as no_bars (not a fetch failure); "
                   "a name-day with no event is counted as no_event",
    "R32_vol": "20-day realized vol = stdev of the prior 20 adjusted daily log returns",
    "R33_price_band_lt10": "an event whose close is below $10 (the universe is $10+ on D-1 only) gets its own "
                           "price-band FE level 'lt10'",
    "R34_rn_sample": "the round-number primary regression uses ABOVE and BELOW events only (NEUTRAL is information)",
}


def P_(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ market access
class StudyMarket(BRO.AlpacaMarket):
    """bro_sr_wr.AlpacaMarket (paced, cached, FetchFail, market_hours_guard) plus multi-symbol daily bars, per-symbol
    month files of 1-min bars, and a TIMESTAMPED quote. offline=True never requests (a miss is a FetchFail)."""

    QLOOK = 60.0

    def __init__(self, work=CACHE_WORK, offline=False):
        super().__init__(work=work, guard=not offline)
        self.offline = offline
        self._minfiles = collections.OrderedDict()

    def _get(self, url, params):
        if self.offline:
            raise BRO.FetchFail(f"offline (not cached): {url} {json.dumps(params)[:160]}")
        return super()._get(url, params)

    def daily_bars(self, syms, lo: str, hi: str, adjustment: str) -> dict:
        """{sym: {day: [o, h, l, c, v]}} SIP daily bars, chunks of 100 symbols (each chunk cached)."""
        syms = sorted(set(syms))
        out = {}
        for i in range(0, len(syms), 100):
            chunk = syms[i:i + 100]
            key = f"{lo}|{hi}|{adjustment}|{hashlib.sha1(','.join(chunk).encode()).hexdigest()[:16]}"

            def fn(chunk=chunk):
                got, token = collections.defaultdict(dict), None
                while True:
                    p = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": lo, "end": f"{hi}T23:59:00Z",
                         "feed": "sip", "adjustment": adjustment, "limit": 10000}
                    if token:
                        p["page_token"] = token
                    js = self._get(f"{BRO.DATA}/v2/stocks/bars", p)
                    for s, bl in (js.get("bars") or {}).items():
                        for b in bl or []:
                            got[s][BS.et_day(BRO.parse_ts(b["t"]))] = [b["o"], b["h"], b["l"], b["c"], b.get("v", 0)]
                    token = js.get("next_page_token")
                    if not token:
                        return dict(got)
            out.update(self._cached(f"daily_{adjustment}", key, fn))
        return out

    def _minfile(self, sym):
        if sym not in self._minfiles:
            p = os.path.join(self.work, "cache", "min", f"{sym}.json.gz")
            try:
                with gzip.open(p, "rt") as f:
                    self._minfiles[sym] = json.load(f)
            except Exception:  # noqa: BLE001
                self._minfiles[sym] = {}
            while len(self._minfiles) > 6:
                self._minfiles.popitem(last=False)
        self._minfiles.move_to_end(sym)
        return self._minfiles[sym]

    def _minsave(self, sym):
        p = os.path.join(self.work, "cache", "min", f"{sym}.json.gz")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with gzip.open(tmp, "wt") as f:
            json.dump(self._minfiles[sym], f)
        os.replace(tmp, p)

    def minute_month(self, sym: str, month: str, hi_day: str) -> list:
        """RAW SIP 1-min rows [start, o, h, l, c, v] of one calendar month (through hi_day), 04:00-16:00 ET only."""
        mf = self._minfile(sym)
        key = f"{month}|{hi_day}"
        if key in mf:
            return mf[key]
        y, m = map(int, month.split("-"))
        start = BS.et_ts(f"{month}-01", 4)
        nxt = f"{y + (m == 12):04d}-{(m % 12) + 1:02d}-01"
        end = min(BS.et_ts(nxt, 0), BS.et_ts(hi_day, 16))
        try:
            out, token = [], None
            while True:
                p = {"symbols": sym, "timeframe": "1Min", "start": BRO.iso(start), "end": BRO.iso(end),
                     "feed": "sip", "adjustment": "raw", "limit": 10000}
                if token:
                    p["page_token"] = token
                js = self._get(f"{BRO.DATA}/v2/stocks/bars", p)
                for b in (js.get("bars") or {}).get(sym) or []:
                    ts = BRO.parse_ts(b["t"])
                    hm = BS.et_hm(ts)
                    if 4 * 60 <= hm < 16 * 60:
                        out.append([ts, b["o"], b["h"], b["l"], b["c"], b.get("v", 0)])
                token = js.get("next_page_token")
                if not token:
                    break
        except BRO.FetchFail as e:
            self.fails[f"min|{sym}|{key}"] = str(e)[:300]
            raise
        mf[key] = out
        self.fails.pop(f"min|{sym}|{key}", None)
        self._minsave(sym)
        return out

    def quote_ts(self, sym: str, t: float):
        """[quote_ts, bid, ask] of the last valid SIP NBBO at or before t (looking back 60 s), or None. The caller
        applies the <= 5 s staleness rule (bro_sr_wr.quote returns no timestamp)."""
        def fn():
            token = None
            while True:
                p = {"start": BRO.iso(t - self.QLOOK), "end": BRO.iso(t), "feed": "sip", "limit": 1000,
                     "sort": "desc"}
                if token:
                    p["page_token"] = token
                js = self._get(f"{BRO.DATA}/v2/stocks/{sym}/quotes", p)
                for q in js.get("quotes") or []:
                    b, a = float(q.get("bp") or 0), float(q.get("ap") or 0)
                    if b > 0 and a >= b:
                        return [BRO.parse_ts(q["t"]), b, a]
                token = js.get("next_page_token")
                if not token:
                    return None
        return self._cached("quotes_ts", f"{sym}|{t:.3f}", fn)


class MarketData:
    """the data interface the studies read (tests pass a fake with the same methods)."""

    def __init__(self, mkt: StudyMarket, cal: dict, daily_raw: dict, daily_adj: dict, hi_day: str):
        self.mkt, self.cal, self.raw, self.adj, self.hi = mkt, cal, daily_raw, daily_adj, hi_day
        self._by_day = collections.OrderedDict()

    def sessions(self):
        return sorted(self.cal)

    def close_ts(self, day):
        return self.cal[day][1]

    def minutes(self, sym, day) -> list:
        k = (sym, day[:7])
        if k not in self._by_day:
            idx = collections.defaultdict(list)
            for r in self.mkt.minute_month(sym, day[:7], self.hi):
                idx[BS.et_day(r[0])].append(r)
            self._by_day[k] = idx
            while len(self._by_day) > 8:
                self._by_day.popitem(last=False)
        return self._by_day[k].get(day, [])

    def daily(self, sym, day, adjusted=False):
        return ((self.adj if adjusted else self.raw).get(sym) or {}).get(day)

    def factor(self, sym, day):
        a, r = self.daily(sym, day, True), self.daily(sym, day, False)
        if not a or not r or not r[3]:
            return None
        return a[3] / r[3]

    def quote(self, sym, t):
        return self.mkt.quote_ts(sym, t)


# ------------------------------------------------------------------ helpers
def sha_seed(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:16], 16)


def mid_at(data, sym, t, max_age=QUOTE_MAX_AGE):
    """(mid, spread fraction, age) from a quote <= max_age old at t, else None (stale or missing)."""
    q = data.quote(sym, t)
    if not q:
        return None
    ts, b, a = q
    if t - ts > max_age + 1e-6 or b <= 0 or a < b:
        return None
    m = (a + b) / 2.0
    return m, (a - b) / m, t - ts


def price_band(px):
    for lo, hi, lab in PRICE_BANDS:
        if lo <= px < hi:
            return lab
    return "lt10"


def halves_of(days: list) -> dict:
    return {d: ("A" if i % 2 == 0 else "B") for i, d in enumerate(sorted(days))}


def pct_ranks(vals: list) -> list:
    """(n below + 0.5 x n equal incl. self) / n (R9)."""
    n = len(vals)
    s = sorted(vals)
    import bisect
    return [(bisect.bisect_left(s, v) + 0.5 * (bisect.bisect_right(s, v) - bisect.bisect_left(s, v))) / n
            for v in vals]


def split_in(data, sym, days: list) -> bool:
    """R3: a split inside days (consecutive factor ratio off 1 by > 10%)."""
    fs = [data.factor(sym, d) for d in days]
    fs = [f for f in fs if f]
    return any(abs(b / a - 1.0) > SPLIT_TOL for a, b in zip(fs, fs[1:]))


def universe(daily_raw: dict, ok_syms, prev_day: str, n=TOP_N, min_px=MIN_PX) -> list:
    """top n by RAW SIP close x volume on D-1 over the bro_sr_wr asset set, raw close on D-1 >= $10."""
    xs = []
    for s in ok_syms:
        b = (daily_raw.get(s) or {}).get(prev_day)
        if b and b[3] >= min_px and b[4]:
            xs.append((b[3] * b[4], s))
    xs.sort(key=lambda x: (-x[0], x[1]))
    return [s for _, s in xs[:n]]


def ok_assets(assets: dict) -> list:
    return sorted(s for s, a in assets.items() if BRO.asset_reason(s, a) is None)


def test_sessions(cal: dict, lo=DATA_LO, hi=TEST_HI, lookback=LOOKBACK, counts=None):
    """the first `lookback` sessions (by count) of [lo, hi] are look-back only; early closes excluded (R4)."""
    ds = sorted(d for d in cal if lo <= d <= hi)
    out = []
    for d in ds[lookback:]:
        if BS.et_hm(cal[d][1]) < 16 * 60:
            if counts is not None:
                counts["early_close_session_excluded"] += 1
            continue
        out.append(d)
    return out


# ------------------------------------------------------------------ per-session summaries (look-back features)
def day_summary(data, sym, day, prev_day, counts):
    """outcome-free facts of one look-back session from RAW bars x factor (ADJUSTED) - or None."""
    rows = data.minutes(sym, day)
    if not rows:
        return None
    f = data.factor(sym, day)
    if not f:
        counts["lookback_day_no_factor"] += 1
        return None
    cl = data.close_ts(day)
    raw1 = BS.minute_bars(rows, day, cl)
    if not raw1:
        return None
    adj1 = BS.scale(raw1, f)
    pa = data.daily(sym, prev_day, True) if prev_day else None
    ev = BS.first_new_high(adj1, pa[3] if pa else None)
    return {
        "ev": ev is not None,
        "ft": BS.followthrough(adj1, ev, day) if ev else None,
        "pairs15": BS.lag1_pairs(BS.aggregate(adj1, day, 15, cl), 15),
        "r30": dict(BS.aligned_returns(BS.aggregate(adj1, day, 30, cl))),
        "b60adj": BS.aggregate(adj1, day, 60, cl),
        "b60raw": BS.aggregate(raw1, day, 60, cl),
        "trend": BS.trend_day(adj1),
        "rth_hl": (max(b.h for b in raw1), min(b.l for b in raw1)),
    }


class Summaries:
    def __init__(self, data, counts):
        self.data, self.counts, self.c = data, counts, {}
        self.sess = data.sessions()
        self.prev = {d: (self.sess[i - 1] if i else None) for i, d in enumerate(self.sess)}

    def get(self, sym, day):
        k = (sym, day)
        if k not in self.c:
            self.c[k] = day_summary(self.data, sym, day, self.prev[day], self.counts)
        return self.c[k]

    def prior(self, day, n):
        i = self.sess.index(day)
        return self.sess[max(0, i - n):i]

    def drop_sym(self, sym):
        for k in [k for k in self.c if k[0] == sym]:
            del self.c[k]


def beta_of(summ, sym, prior_days):
    pairs = []
    for d in prior_days:
        a, s = summ.get(sym, d), summ.get(SPY, d)
        if not a or not s:
            continue
        pairs += [(s["r30"][k], v) for k, v in a["r30"].items() if k in s["r30"]]
    if len(pairs) < BETA_MIN_PAIRS:
        return None, len(pairs)
    return BS.ols_slope(pairs), len(pairs)


# ------------------------------------------------------------------ events and features (OUTCOME-BLIND)
def nameday_features(data, summ, sym, day, counts, mirror=False):
    """the event and every feature known at t for one name-day, or None (reason counted)."""
    prior = summ.prior(day, LOOKBACK)
    prev = prior[-1] if prior else None
    if len(prior) < LOOKBACK or prev is None:
        counts["short_lookback"] += 1
        return None
    rows = data.minutes(sym, day)
    if not rows:
        counts["no_bars"] += 1
        return None
    if split_in(data, sym, summ.prior(day, LOOKBACK + 1) + [day]):
        counts["split_in_lookback"] += 1
        return None
    pr = data.daily(sym, prev)
    if not pr:
        counts["no_prior_close"] += 1
        return None
    cl = data.close_ts(day)
    raw1 = BS.minute_bars(rows, day, cl)
    ev = (BS.first_new_low if mirror else BS.first_new_high)(raw1, pr[3])
    if ev is None:
        counts["no_event"] += 1
        return None
    t = ev["t"]
    f_today = data.factor(sym, day)
    sums = [(d, summ.get(sym, d)) for d in prior]
    sums = [(d, s) for d, s in sums if s]
    # T1
    fts = [s["ft"] for _, s in sums if s["ev"]]
    T1 = (sum(fts) / len(fts)) if len(fts) >= T1_MIN_EVENTS else None
    # T2
    pairs = [p for _, s in sums for p in s["pairs15"]]
    T2 = BS.pearson(pairs)
    # T3
    tds = [s["trend"] for _, s in sums if s["trend"] is not None]
    T3 = (sum(tds) / len(tds)) if tds else None
    # HTF_UP (R11)
    htf = htf_feature(raw1, day, cl, f_today, [s["b60adj"] for _, s in sums], t, counts)
    # beta
    beta, npairs = beta_of(summ, sym, prior)
    # LEVELS_info (R25)
    pre = BS.minute_bars(rows, day, cl, rth=False)
    lv = BS.nh_levels([s["rth_hl"] for _, s in sums], pr[3], max((b.h for b in pre), default=None),
                      max((s["rth_hl"][0] for _, s in sums), default=None))
    li = BS.level_info(ev["close"], lv)
    # daily information cells (R26, R32)
    adj_prior = [data.daily(sym, d, True) for d in summ.prior(day, DAILY_BACK)]
    adj_prior = [x for x in adj_prior if x]
    f_prev = data.factor(sym, prev)
    atr = BS.atr14([(x[0], x[1], x[2], x[3]) for x in adj_prior])
    atr_raw = atr / f_prev if (atr and f_prev) else None
    sma50 = BS.sma([x[3] for x in adj_prior], 50)
    vol20 = BS.realized_vol([x[3] for x in adj_prior], 20)
    sess_low = min(b.l for b in raw1 if b.end <= t)
    # liquidity zones (R27)
    pdh = sums[-1][1]["rth_hl"][0] if sums and sums[-1][0] == prev else None
    raw_series = BS.concat_sessions([s["b60raw"] for _, s in sums] + [BS.aggregate(raw1, day, 60, cl)])
    older = [s for s in BS.swings_at(raw_series, t) if s.kind == "H" and BS.et_day(s.start) < prev
             and pdh and s.price > pdh]
    row = {
        "sym": sym, "day": day, "t": t, "hour": BS.et_hour(t), "close": ev["close"],
        "h_prev": ev.get("h_prev"), "l_prev": ev.get("l_prev"), "prior_close": pr[3],
        "band": price_band(ev["close"]), "T1": T1, "T1_n": len(fts), "T2": T2, "T2_pairs": len(pairs), "T3": T3,
        "htf_up": htf, "beta": beta, "beta_pairs": npairs,
        "lvl_dist": li["dist"], "lvl_conf": li["confluence"],
        "vol20": vol20, "atr_used": ((ev["close"] - sess_low) / atr_raw) if atr_raw else None,
        "bias_above_sma50": (adj_prior[-1][3] > sma50) if (sma50 and adj_prior) else None,
        "pdh_dist": ((pdh - ev["close"]) / ev["close"]) if pdh else None,
        "older_swing_dist": ((older[-1].price - ev["close"]) / ev["close"]) if older else None,
    }
    if not mirror:
        row["rn"] = BS.rn_group(ev["close"], ev["h_prev"], RN_BAND)
        row["rn_010"] = BS.rn_group(ev["close"], ev["h_prev"], 0.0010)
        row["rn_025"] = BS.rn_group(ev["close"], ev["h_prev"], 0.0025)
        row["rn_whole"] = BS.rn_group(ev["close"], ev["h_prev"], RN_BAND, "whole")
        row["rn_half"] = BS.rn_group(ev["close"], ev["h_prev"], RN_BAND, "half")
        row["log_brk"] = math.log(ev["close"] / ev["h_prev"])
    return row


def htf_feature(raw1, day, cl, f_today, prior_b60adj, t, counts):
    """HTF_UP at t (R11), or None with the reason counted (review round 1 FF2, 2026-10-08: R11 promised the
    missing count, the code dropped events from H2 silently)."""
    if not f_today:
        counts["htf_missing_no_factor_today"] += 1
        return None
    b15 = BS.aggregate(raw1, day, 15, cl)
    series = BS.concat_sessions(list(prior_b60adj) + [BS.aggregate(BS.scale(raw1, f_today), day, 60, cl)])
    if len(BS.completed(series, t)) < BS.EMA_WARM + 1:
        counts["htf_missing_ema_warmup_lt141_bars"] += 1
        return None
    htf = BS.htf_up(b15, series, t)
    if htf is None:
        counts["htf_missing_lt3_15min_bars"] += 1
    return htf


def assign_scores(rows):
    """TREND_SCORE = mean of per-session percentile ranks of T1 and T2 (both required); terciles pooled (R9, R10)."""
    by = collections.defaultdict(list)
    for r in rows:
        r["trend_score"] = None
        r["tercile"] = None
        if r["T1"] is not None and r["T2"] is not None:
            by[r["day"]].append(r)
    for d, rs in by.items():
        p1, p2 = pct_ranks([r["T1"] for r in rs]), pct_ranks([r["T2"] for r in rs])
        for r, a, b in zip(rs, p1, p2):
            r["trend_score"] = (a + b) / 2.0
    scored = [r for r in rows if r["trend_score"] is not None]
    if scored:
        ps = pct_ranks([r["trend_score"] for r in scored])
        for r, p in zip(scored, ps):
            r["tercile"] = "BOTTOM" if p < 1 / 3 else ("TOP" if p >= 2 / 3 else "MID")
    return rows


def control_minute(sym, day, t):
    """same-name control (R15): (minute close time, fallback tag) or (None, 'none')."""
    rng = random.Random(sha_seed(f"{CTRL_SEED}|{sym}|{day}"))
    h = BS.et_hour(t)
    base = BS.et_ts(day, 0)
    legal = [base + m * 60 for m in range(CTRL_LO, CTRL_HI + 1)]
    legal = [m for m in legal if abs(m - t) > CTRL_GAP]
    for tag, hh in (("same", h), ("later", h + 1), ("earlier", h - 1)):
        c = sorted(m for m in legal if BS.et_hour(m) == hh)
        if c:
            return rng.choice(c), tag
    return None, "none"


def build_events(data, sessions, uni: dict, counts, mirror=False):
    """all events (outcome-blind), by symbol so each symbol's bars load once."""
    summ = Summaries(data, counts)
    by_sym = collections.defaultdict(list)
    for d in sessions:
        for s in uni.get(d, []):
            by_sym[s].append(d)
    rows, fails = [], 0
    for sym in sorted(by_sym):
        for d in by_sym[sym]:
            counts["namedays"] += 1
            try:
                r = nameday_features(data, summ, sym, d, counts, mirror)
            except BRO.FetchFail:
                fails += 1
                counts["nameday_fetch_fail"] += 1
                continue
            if r:
                rows.append(r)
        summ.drop_sym(sym)
    assign_scores(rows)
    hv = halves_of(sessions)
    for r in rows:
        r["half"] = hv[r["day"]]
        r["ctrl_t"], r["ctrl_tag"] = control_minute(r["sym"], r["day"], r["t"])
    return rows, fails


def needs_control(r) -> bool:
    return r.get("tercile") == "TOP" or r.get("htf_up") is True or r.get("rn") == "ABOVE"


def nh_arm(r) -> bool:
    return r.get("tercile") == "TOP" or r.get("htf_up") is True


# ------------------------------------------------------------------ outcomes
def outcome(data, sym, t, beta, short=False):
    """entry at the mid at t + 5 s; exits at entry + 30 / + 15 min; cost = half spread at entry + half at exit.
    Returns {} with 'drop' set when the entry/exit30 or a SPY quote is stale."""
    te = t + ENTRY_LAG
    e, x30 = mid_at(data, sym, te), mid_at(data, sym, te + HOLD30)
    if not e or not x30:
        return {"drop": "stale_quote"}
    se, sx = mid_at(data, SPY, te), mid_at(data, SPY, te + HOLD30)
    if not se or not sx:
        return {"drop": "stale_spy_quote"}
    sg = -1.0 if short else 1.0
    ret = x30[0] / e[0] - 1.0
    spy = sx[0] / se[0] - 1.0
    cost = 0.5 * e[1] + 0.5 * x30[1]
    out = {"net30": 1e4 * (sg * ret - cost), "hedged30": 1e4 * (sg * (ret - beta * spy) - cost),
           "spread_bp": 1e4 * e[1], "entry_age": e[2]}
    x15, s15 = mid_at(data, sym, te + HOLD15), mid_at(data, SPY, te + HOLD15)
    if x15 and s15:
        r15, spy15 = x15[0] / e[0] - 1.0, s15[0] / se[0] - 1.0
        c15 = 0.5 * e[1] + 0.5 * x15[1]
        out["net15"] = 1e4 * (sg * r15 - c15)
        out["hedged15"] = 1e4 * (sg * (r15 - beta * spy15) - c15)
    return out


def attach_outcomes(data, rows, counts, short=False, events=True, controls=True, ctrl_filter=needs_control):
    for r in rows:
        if r["beta"] is None:
            r["drop"] = "beta_pairs"
            counts["drop_beta_pairs"] += 1
            continue
        if events:
            o = outcome(data, r["sym"], r["t"], r["beta"], short)
            if "drop" in o:
                counts[f"drop_{o['drop']}"] += 1
            r.update(o)
        if controls and ctrl_filter(r):
            if r["ctrl_t"] is None:
                counts["control_no_legal_minute"] += 1
                continue
            counts[f"control_hour_{r['ctrl_tag']}"] += 1
            c = outcome(data, r["sym"], r["ctrl_t"], r["beta"], short)
            if "drop" in c:
                counts["control_stale"] += 1
                r["ctrl_drop"] = c["drop"]
            else:
                r["ctrl"] = c
    return rows


# ------------------------------------------------------------------ statistics (two-way clustered FE OLS)
def _demean(cols: list, fes: list, iters=200, tol=1e-11):
    """alternating projections: demean every column within each FE dimension until stable."""
    cols = [list(c) for c in cols]
    if not fes:
        return cols
    idx = []
    for g in fes:
        m = {}
        idx.append([m.setdefault(v, len(m)) for v in g])
    for c in cols:
        for _ in range(iters):
            delta = 0.0
            for ix in idx:
                k = max(ix) + 1
                s, n = [0.0] * k, [0] * k
                for i, v in zip(ix, c):
                    s[i] += v
                    n[i] += 1
                mu = [s[j] / n[j] for j in range(k)]
                for j in range(len(c)):
                    c[j] -= mu[ix[j]]
                delta = max(delta, max(abs(x) for x in mu))
            if delta < tol:
                break
    return cols


def _solve(A, b):
    n = len(A)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for i in range(n):
        p = max(range(i, n), key=lambda r: abs(M[r][i]))
        M[i], M[p] = M[p], M[i]
        if abs(M[i][i]) < 1e-15:
            return None
        for r in range(n):
            if r != i:
                f = M[r][i] / M[i][i]
                M[r] = [a - f * c for a, c in zip(M[r], M[i])]
    return [M[i][n] / M[i][i] for i in range(n)]


def fe_ols(rows, y, x, fes=("day", "hour"), covs=(), cl=("day", "sym")):
    """coefficient on x (callable row -> 0/1 or value) with FE and covariates; two-way clustered SE (R17).
    x=None -> the mean (constant regressor, no FE)."""
    rs = [r for r in rows if r.get(y) is not None and all(r.get(c) is not None for c in covs)]
    n = len(rs)
    names = len({r[cl[1]] for r in rs})
    sess = len({r[cl[0]] for r in rs})
    base = {"n": n, "names": names, "sessions": sess, "coef": None, "se": None, "t": None, "df": None}
    if n < 3:
        return base
    Y = [float(r[y]) for r in rs]
    X = [1.0] * n if x is None else [float(x(r)) for r in rs]
    C = [[float(r[c]) for r in rs] for c in covs]
    use_fes = [] if x is None else [[r[f] for r in rs] for f in fes]
    cols = _demean([Y, X] + C, use_fes)
    Y, X, C = cols[0], cols[1], cols[2:]
    if C:
        k = len(C)
        A = [[sum(a * b for a, b in zip(C[i], C[j])) for j in range(k)] for i in range(k)]
        sol = []
        for v in (Y, X):
            g = _solve(A, [sum(a * b for a, b in zip(C[i], v)) for i in range(k)])
            if g is None:
                return base
            sol.append([v[j] - sum(g[i] * C[i][j] for i in range(k)) for j in range(n)])
        Y, X = sol
    sxx = sum(v * v for v in X)
    if sxx <= 1e-15:
        return base
    b = sum(a * c for a, c in zip(X, Y)) / sxx
    e = [yy - b * xx for yy, xx in zip(Y, X)]

    def V(keyf):
        g = collections.defaultdict(float)
        for r, xx, ee in zip(rs, X, e):
            g[keyf(r)] += xx * ee
        G = len(g)
        if G < 2:
            return None, G
        return G / (G - 1) * sum(v * v for v in g.values()) / (sxx * sxx), G

    v1, g1 = V(lambda r: r[cl[0]])
    v2, g2 = V(lambda r: r[cl[1]])
    v12, _ = V(lambda r: (r[cl[0]], r[cl[1]]))
    if v1 is None or v2 is None:
        return {**base, "coef": b}
    var = v1 + v2 - (v12 or 0.0)
    if var <= 0:
        var = max(v1, v2)
    se = math.sqrt(var)
    return {**base, "coef": b, "se": se, "t": b / se if se > 0 else None, "df": min(g1, g2) - 1}


def tmean(rows, key):
    """mean with two-way clustered SE (session, name)."""
    return fe_ols(rows, key, None)


def t_crit(df, z):
    return BRO.t_crit(df, z) if df and df >= 1 else None


def mde(res, z):
    c = t_crit(res.get("df"), z)
    return (c + POWER_K) * res["se"] if (c is not None and res.get("se") is not None) else None


def top_units(rows, y, better, unit, k):
    """the k units with the largest contribution to the simple group-mean difference (R18)."""
    A = [r for r in rows if r.get(y) is not None and better(r)]
    B = [r for r in rows if r.get(y) is not None and not better(r)]
    if not A or not B:
        return []
    c = collections.defaultdict(float)
    for r in A:
        c[r[unit]] += r[y] / len(A)
    for r in B:
        c[r[unit]] -= r[y] / len(B)
    return [u for u, _ in sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))[:k]]


def group_verdict(rows, y, better, z, fes=("day", "hour"), covs=(), under_bp=UNDER_BP, powered=None):
    """name_history / round_numbers pass clause for one hypothesis (rows = the two groups only).
    powered: None -> from the realized half MDEs; else the frozen projection (round numbers, R22)."""
    xf = lambda r: 1.0 if better(r) else 0.0  # noqa: E731
    pooled = fe_ols(rows, y, xf, fes, covs)
    halves = {h: fe_ols([r for r in rows if r["half"] == h], y, xf, fes, covs) for h in ("A", "B")}
    for h in halves.values():
        h["mde"] = mde(h, z)
    pooled["t_crit"] = t_crit(pooled["df"], z)
    bm = tmean([r for r in rows if better(r)], y)
    drops = {}
    for unit, k in (("sym", DROP_NAMES), ("day", DROP_SESSIONS)):
        us = set(top_units(rows, y, better, unit, k))
        kept = [r for r in rows if r[unit] not in us]
        dd = fe_ols(kept, y, xf, fes, covs)
        drops[unit] = {"dropped": sorted(map(str, us)), "coef": dd["coef"],
                       "better_mean": tmean([r for r in kept if better(r)], y)["coef"]}
    if powered is None:
        powered = all(h["mde"] is not None and h["mde"] <= under_bp for h in halves.values())
    c, t, tc = pooled["coef"], pooled["t"], pooled["t_crit"]
    why = []
    ok = True
    if c is None or c < PASS_BP:
        ok = False
        why.append("difference < +5 bp")
    if t is None or tc is None or t < tc:
        ok = False
        why.append("t < t_crit")
    if any(h["coef"] is None or h["coef"] <= 0 for h in halves.values()):
        ok = False
        why.append("not positive in both halves")
    if bm["coef"] is None or bm["coef"] <= 0:
        ok = False
        why.append("better group's net <= 0")
    for unit, d in drops.items():
        if d["coef"] is None or d["coef"] < 0 or d["better_mean"] is None or d["better_mean"] <= 0:
            ok = False
            why.append(f"drop-top-{unit} test")
    if t is not None and t <= FAIL_T:
        v = "FAIL"
        why = [f"pooled t {t:.2f} <= -2"]
    elif ok:
        v = "PASS"
    elif powered:
        v = "FAIL"
        why.append("powered (MDE <= %.0f bp in both halves) and not passed" % under_bp)
    else:
        v = "UNDERPOWERED"
    return {"verdict": v, "why": why, "pooled": pooled, "halves": halves, "better": bm, "drops": drops,
            "powered": powered, "n_groups": {"better": sum(1 for r in rows if better(r)),
                                             "other": sum(1 for r in rows if not better(r))}}


QUOTE_DROPS = ("stale_quote", "stale_spy_quote", "quote_fetch_fail")


def drop_rates(rows, better):
    """R24: QUOTE drops only. A beta_pairs drop never asks for a quote, so it is neither a drop nor in the base
    (review round 1 FF1, 2026-10-08: counting it let a beta-coverage gap read as FAILED-DATA)."""
    out = {}
    for lab, f in (("better", better), ("other", lambda r: not better(r))):
        g = [r for r in rows if f(r) and r.get("drop") != "beta_pairs"]
        out[lab] = (sum(1 for r in g if r.get("drop") in QUOTE_DROPS) / len(g)) if g else None
    a, b = out["better"], out["other"]
    out["failed_data"] = a is not None and b is not None and abs(a - b) > DROP_RATE_PP
    return out


def paired(rows, y):
    """event minus its control (same outcome key)."""
    out = []
    for r in rows:
        if r.get(y) is not None and r.get("ctrl") and r["ctrl"].get(y) is not None:
            out.append({**r, "d": r[y] - r["ctrl"][y]})
    return out


def positive(rows, y="hedged30"):
    res = tmean(paired(rows, y), "d")
    res["positive"] = bool(res["coef"] is not None and res["coef"] > 0 and res["t"] is not None and res["t"] >= Z_POS)
    return res


# ------------------------------------------------------------------ hypotheses
def H1_rows(rows):
    return [r for r in rows if r.get("tercile") in ("TOP", "BOTTOM")]


def H2_rows(rows):
    return [r for r in rows if r.get("htf_up") is not None]


def RN_rows(rows):
    return [r for r in rows if r.get("rn") in ("ABOVE", "BELOW")]


IS_TOP = lambda r: r.get("tercile") == "TOP"  # noqa: E731
IS_HTF = lambda r: r.get("htf_up") is True  # noqa: E731
IS_ABOVE = lambda r: r.get("rn") == "ABOVE"  # noqa: E731


def nh_hypothesis(rows, sel, better, z=Z_NH):
    g = sel(rows)
    kept = [r for r in g if not r.get("drop")]
    v = group_verdict(kept, "hedged30", better, z)
    v["groups_by_half"] = count_table(kept, list, better)     # power rule: n and names per group per half (FF3)
    v["raw_net30"] = group_verdict(kept, "net30", better, z)["pooled"]
    v["drop_rates"] = drop_rates(g, better)
    v["control"] = positive([r for r in kept if better(r)])
    if v["verdict"] == "PASS" and not v["control"]["positive"]:
        v["label"] = "NAME SELECTION (not an entry signal)"
    elif v["verdict"] == "PASS":
        v["label"] = "PASS (reruns on >= 60 earlier sessions and on IEX bars required before any forward test)"
    if v["drop_rates"]["failed_data"]:
        v["failed_data"] = True
        mark_failed_data(v)
    return v


def mark_failed_data(v):
    """R24: a FAILED-DATA hypothesis is not read - the verdict becomes FAILED-DATA (the unread one is kept in
    verdict_if_read) and no PASS label survives (review round 1 FF1)."""
    v["verdict_data"] = "FAILED-DATA"
    v["verdict_if_read"] = v["verdict"]
    v["verdict"] = "FAILED-DATA"
    v.pop("label", None)
    v["why"] = ["groups' quote-drop rates differ by > 2 pp: not read"] + v["why"]


def rn_hypothesis(rows, projection):
    g = RN_rows(rows)
    kept = [r for r in g if not r.get("drop")]
    powered = projection.get("powered")
    v = group_verdict(kept, "hedged30", IS_ABOVE, Z_RN, fes=("day", "hour", "band"), covs=("log_brk",),
                      powered=powered)
    v["projection"] = projection
    v["groups_by_half"] = count_table(kept, list, IS_ABOVE)
    v["drop_rates"] = drop_rates(g, IS_ABOVE)
    v["control"] = positive([r for r in kept if IS_ABOVE(r)])
    if v["verdict"] == "PASS":
        v["label"] = ("PASS (name_history earlier-period and IEX reruns required)" if v["control"]["positive"]
                      else "RELATIVE ONLY")
    if v["drop_rates"]["failed_data"]:
        mark_failed_data(v)
    return v


def count_table(rows, sel, better):
    out = {}
    for h in ("A", "B"):
        g = [r for r in sel(rows) if r["half"] == h]
        for lab, f in (("better", better), ("other", lambda r: not better(r))):
            gg = [r for r in g if f(r)]
            out[f"{h}_{lab}"] = {"n": len(gg), "names": len({r["sym"] for r in gg})}
    return out


def rn_projection(rows):
    """R22: OUTCOME-BLIND for events - reads only the name_history CONTROL arm's outcomes."""
    out, powered = {}, True
    for h in ("A", "B"):
        ctrl = [{"day": r["day"], "sym": r["sym"], "y": r["ctrl"]["hedged30"]} for r in rows
                if r["half"] == h and nh_arm(r) and r.get("ctrl")]
        cm = tmean(ctrl, "y")
        g = [r for r in RN_rows(rows) if r["half"] == h]
        nA, nB = sum(1 for r in g if IS_ABOVE(r)), sum(1 for r in g if not IS_ABOVE(r))
        df = min(len({r["day"] for r in g}), len({r["sym"] for r in g})) - 1 if g else None
        se = (cm["se"] * math.sqrt(cm["n"]) * math.sqrt(1.0 / nA + 1.0 / nB)) if (cm["se"] and nA and nB) else None
        c = t_crit(df, Z_RN)
        m = (c + POWER_K) * se if (se is not None and c is not None) else None
        out[h] = {"n_ctrl": cm["n"], "se_ctrl": cm["se"], "n_above": nA, "n_below": nB, "df": df,
                  "projected_se": se, "projected_mde": m}
        if m is None or m > UNDER_BP:
            powered = False
    out["powered"] = powered
    return out


# ------------------------------------------------------------------ information cells
def cell(rows, key="hedged30"):
    r = tmean(rows, key)
    return {"n": r["n"], "names": r["names"], "mean": r["coef"], "t": r["t"]}


def split_cells(rows, fn, key="hedged30"):
    by = collections.defaultdict(list)
    for r in rows:
        k = fn(r)
        if k is not None:
            by[str(k)].append(r)
    return {k: cell(v, key) for k, v in sorted(by.items())}


def terciles_of(rows, key):
    xs = [r for r in rows if r.get(key) is not None]
    ps = pct_ranks([r[key] for r in xs]) if xs else []
    return {id(r): ("low" if p < 1 / 3 else ("high" if p >= 2 / 3 else "mid")) for r, p in zip(xs, ps)}


def spearman(a, b):
    if len(a) < 3:
        return None
    return BS.pearson(list(zip(pct_ranks(a), pct_ranks(b))))


def info_cells(rows, mirror_rows):
    k = [r for r in rows if not r.get("drop")]
    out = {}
    out["control_H1_top"] = positive([r for r in k if IS_TOP(r)])
    out["control_H2_htf"] = positive([r for r in k if IS_HTF(r)])
    elig = [r for r in k if r.get("T1") is not None]
    out["H2_on_T1_eligible"] = group_verdict(H2_rows(elig), "hedged30", IS_HTF, Z_NH)["pooled"]
    out["T1_missing_rate_by_H2"] = {str(g): (sum(1 for r in rows if r.get("htf_up") is g and r["T1"] is None) /
                                             max(1, sum(1 for r in rows if r.get("htf_up") is g)))
                                    for g in (True, False)}
    by_day = collections.defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r["T1"] is None)
    out["T1_missing_rate_by_session"] = {d: sum(v) / len(v) for d, v in sorted(by_day.items())}
    both = [r for r in rows if r.get("trend_score") is not None and r.get("htf_up") is not None]
    out["corr_trend_score_htf"] = BS.pearson([(r["trend_score"], 1.0 if r["htf_up"] else 0.0) for r in both])
    out["H1_within_HTF"] = {str(g): fe_ols(H1_rows([r for r in k if r.get("htf_up") is g]), "hedged30",
                                           lambda r: 1.0 if IS_TOP(r) else 0.0)
                            for g in (True, False)}
    vt = terciles_of(k, "vol20")
    out["by_vol_tercile"] = {}
    for lab in ("low", "mid", "high"):
        sub = [r for r in k if vt.get(id(r)) == lab]
        out["by_vol_tercile"][lab] = {
            "H1": fe_ols(H1_rows(sub), "hedged30", lambda r: 1.0 if IS_TOP(r) else 0.0),
            "H2": fe_ols(H2_rows(sub), "hedged30", lambda r: 1.0 if IS_HTF(r) else 0.0)}
    out["net15"] = {"H1": fe_ols(H1_rows(k), "hedged15", lambda r: 1.0 if IS_TOP(r) else 0.0),
                    "H2": fe_ols(H2_rows(k), "hedged15", lambda r: 1.0 if IS_HTF(r) else 0.0)}
    out["raw_net30"] = {"H1": fe_ols(H1_rows(k), "net30", lambda r: 1.0 if IS_TOP(r) else 0.0),
                        "H2": fe_ols(H2_rows(k), "net30", lambda r: 1.0 if IS_HTF(r) else 0.0)}
    out["T_alone"] = {}
    for key in ("T1", "T2", "T3"):
        tt = terciles_of(k, key)
        sub = [r for r in k if tt.get(id(r)) in ("low", "high")]
        out["T_alone"][key] = fe_ols(sub, "hedged30", lambda r, tt=tt: 1.0 if tt[id(r)] == "high" else 0.0)
    hx = [r for r in k if r.get("tercile") is not None and r.get("htf_up") is not None
          and (IS_TOP(r) and IS_HTF(r) or r["tercile"] != "TOP" and r["htf_up"] is False)]
    out["H1xH2_both_vs_neither"] = fe_ols(hx, "hedged30", lambda r: 1.0 if IS_TOP(r) else 0.0)

    def band(r):
        d = r.get("lvl_dist")
        if d is None:
            return "none"
        return "<0.2%" if d < 0.002 else ("0.2-0.5%" if d <= 0.005 else ">0.5%")
    out["levels_by_distance"] = split_cells(k, band)
    out["levels_by_confluence"] = split_cells(k, lambda r: min(r["lvl_conf"], 2))
    km = [r for r in mirror_rows if not r.get("drop")]
    out["short_mirror_H1"] = fe_ols(H1_rows(km), "hedged30", lambda r: 1.0 if IS_TOP(r) else 0.0)
    out["short_mirror_H1"]["n_events"] = len(mirror_rows)
    # TREND_SCORE stability
    per = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        if r.get("trend_score") is not None:
            per[r["day"][:7]][r["sym"]].append(r["trend_score"])
    months = sorted(per)
    stab = {}
    for a, b in zip(months, months[1:]):
        common = sorted(set(per[a]) & set(per[b]))
        stab[f"{a}->{b}"] = {"names": len(common), "rank_corr": spearman(
            [statistics.mean(per[a][s]) for s in common], [statistics.mean(per[b][s]) for s in common])}
    out["trend_score_stability"] = stab
    ps = [r["T2_pairs"] for r in rows if r.get("T2_pairs")]
    out["T2_noise_se_mean"] = statistics.mean(1 / math.sqrt(p) for p in ps) if ps else None
    out["daily_bias_sma50"] = split_cells(k, lambda r: None if r.get("bias_above_sma50") is None else
                                          ("above(with-trend)" if r["bias_above_sma50"] else "below(contrarian)"))
    out["range_budget_atr_used"] = split_cells(k, lambda r: None if r.get("atr_used") is None else
                                               ("<50%" if r["atr_used"] < 0.5 else
                                                ("50-100%" if r["atr_used"] <= 1.0 else ">100%")))
    out["liquidity_within_0.3pct_under_pdh"] = split_cells(k, lambda r: None if r.get("pdh_dist") is None else
                                                           (0 <= r["pdh_dist"] <= 0.003))
    od = [r["older_swing_dist"] for r in k if r.get("older_swing_dist") is not None]
    out["liquidity_older_swing_dist_median"] = statistics.median(od) if od else None
    return out


def rn_info(rows):
    k = [r for r in rows if not r.get("drop") and r.get("rn")]
    out = {}
    for lab, other in (("BELOW_minus_NEUTRAL", "BELOW"), ("ABOVE_minus_NEUTRAL", "ABOVE")):
        sub = [r for r in k if r["rn"] in (other, "NEUTRAL")]
        out[lab] = fe_ols(sub, "hedged30", lambda r, o=other: 1.0 if r["rn"] == o else 0.0,
                          ("day", "hour", "band"), ("log_brk",))
    out["by_price_band"] = {}
    for b in [x[2] for x in PRICE_BANDS]:
        sub = [r for r in k if r["band"] == b]
        out["by_price_band"][b] = {"groups": split_cells(sub, lambda r: r["rn"]),
                                   "base_rates": dict(collections.Counter(r["rn"] for r in sub))}
    for key in ("rn_010", "rn_025", "rn_whole", "rn_half"):
        sub = [r for r in k if r[key] in ("ABOVE", "BELOW")]
        out[key] = fe_ols(sub, "hedged30", lambda r, kk=key: 1.0 if r[kk] == "ABOVE" else 0.0,
                          ("day", "hour", "band"), ("log_brk",))
    return out


# ------------------------------------------------------------------ pipeline
def load_inputs(mkt, lo=DATA_LO, hi=TEST_HI, lookback=LOOKBACK, counts=None):
    """calendar, asset set, raw daily bars (universe), adjusted daily bars (factors, ATR/SMA) -> MarketData,
    test sessions, universe per test session."""
    counts = counts if counts is not None else collections.Counter()
    d0 = (datetime.strptime(lo, "%Y-%m-%d") - timedelta(days=130)).strftime("%Y-%m-%d")
    cal = mkt.calendar(d0, hi)
    syms = ok_assets(mkt.assets())
    raw = mkt.daily_bars(syms + [SPY], d0, hi, "raw")
    tests = test_sessions(cal, lo, hi, lookback, counts)
    sess = sorted(cal)
    prev = {d: sess[i - 1] for i, d in enumerate(sess) if i}
    uni = {d: universe(raw, syms, prev[d]) for d in tests}
    union = sorted({s for v in uni.values() for s in v} | {SPY})
    adj = mkt.daily_bars(union, d0, hi, "all")
    return MarketData(mkt, cal, raw, adj, hi), tests, uni


def fetch_minutes(mkt, data, tests, uni, lookback):
    """every month of RAW 1-min bars each universe name (and SPY) needs: look-back + test sessions."""
    sess = data.sessions()
    need = collections.defaultdict(set)
    for d in tests:
        i = sess.index(d)
        months = {x[:7] for x in sess[max(0, i - lookback - 1):i + 1]}
        for s in uni[d] + [SPY]:
            need[s] |= months
    n, fails = 0, 0
    for s in sorted(need):
        for m in sorted(need[s]):
            n += 1
            try:
                mkt.minute_month(s, m, data.hi)
            except BRO.FetchFail:
                fails += 1
        mkt.flush()
    P_(f"minute months {n}, failures {fails}")


def frozen_path(work=WORK):
    return os.path.join(work, "frozen_groups.json")


def freeze(rows, work=WORK):
    keep = ("sym", "day", "t", "half", "tercile", "trend_score", "htf_up", "rn", "rn_010", "rn_025", "rn_whole",
            "rn_half", "band", "hour", "log_brk")
    g = [{k: r.get(k) for k in keep} for r in rows]
    blob = json.dumps(g, sort_keys=True).encode()
    BRO.jsave(frozen_path(work), {"sha256": hashlib.sha256(blob).hexdigest(), "groups": g})
    return hashlib.sha256(blob).hexdigest()


def check_frozen(rows, work=WORK):
    fz = BRO.jload(frozen_path(work))
    if not fz:
        raise SystemExit("run count first: groups must be FROZEN before any outcome is read")
    keyed = {(g["sym"], g["day"]): g for g in fz["groups"]}
    for r in rows:
        g = keyed.get((r["sym"], r["day"]))
        if g is None or any(g[k] != r.get(k) for k in ("tercile", "htf_up", "rn")):
            raise SystemExit(f"frozen groups differ for {r['sym']} {r['day']}")
    return fz["sha256"]


def run(mode, work=WORK, mkt=None):
    os.makedirs(work, exist_ok=True)
    counts = collections.Counter()
    if mode == "fetch":
        BRO.market_hours_guard()
    mkt = mkt or StudyMarket(offline=(mode != "fetch"))
    data, tests, uni = load_inputs(mkt, DATA_LO, TEST_HI, LOOKBACK, counts)
    if mode == "fetch":
        fetch_minutes(mkt, data, tests, uni, LOOKBACK)
    rows, fails = build_events(data, tests, uni, counts)
    mirror, _ = build_events(data, tests, uni, collections.Counter(), mirror=True)
    nd = counts["namedays"]
    if nd and fails / nd > FAIL_SHARE:
        raise SystemExit(f"ABORT: {fails}/{nd} name-days failed (> 2%)")
    if mode == "fetch":
        attach_outcomes(data, rows, counts)
        attach_outcomes(data, mirror, counts, short=True, controls=False)
        mkt.flush()
        P_(f"fetch done: {mkt.requests} requests, {len(mkt.fails)} failures; counts {dict(counts)}")
        return None
    if mode == "count":
        sha = freeze(rows, work)
        attach_outcomes(data, rows, counts, events=False, ctrl_filter=nh_arm)   # CONTROL arm only (R22)
        res = {"frozen_sha256": sha, "counts": dict(counts), "n_events": len(rows),
               "H1": count_table(rows, H1_rows, IS_TOP), "H2": count_table(rows, H2_rows, IS_HTF),
               "RN": count_table(rows, RN_rows, IS_ABOVE), "rn_projection": rn_projection(rows)}
        # the power rule's n / names per group per half after the drops known outcome-blind (beta_pairs); quote
        # drops are only known at score, whose power block repeats the table after every drop (review round 1 FF3)
        wb = [r for r in rows if r["beta"] is not None]
        res["after_beta_drop"] = {"H1": count_table(wb, H1_rows, IS_TOP), "H2": count_table(wb, H2_rows, IS_HTF),
                                  "RN": count_table(wb, RN_rows, IS_ABOVE)}
        BRO.jsave(os.path.join(work, "count.json"), res)
        P_(json.dumps(res, indent=1, default=str))
        return res
    if mode == "score":
        sha = check_frozen(rows, work)
        cnt = BRO.jload(os.path.join(work, "count.json")) or {}
        if cnt.get("frozen_sha256") != sha:
            raise SystemExit("count.json does not match the frozen groups: re-run count")
        attach_outcomes(data, rows, counts)
        attach_outcomes(data, mirror, counts, short=True, controls=False)
        res = summarize(rows, mirror, counts, cnt["rn_projection"], sha)
        BRO.jsave(os.path.join(work, "result.json"), res)
        P_(f"result.json written ({len(rows)} events)")
        return res
    raise SystemExit(__doc__)


def summarize(rows, mirror, counts, projection, sha):
    H1 = nh_hypothesis(rows, H1_rows, IS_TOP)
    H2 = nh_hypothesis(rows, H2_rows, IS_HTF)
    RN = rn_hypothesis(rows, projection)
    return {
        "prereg": PREREG, "script_rev": BRO.script_rev(), "resolutions": RESOLUTIONS, "frozen_sha256": sha,
        "counts": dict(counts), "n_events": len(rows), "sessions": len({r["day"] for r in rows}),
        "failed_data": bool(H1.get("failed_data") or H2.get("failed_data")),
        "verdict_data": {k: v.get("verdict_data") for k, v in (("H1", H1), ("H2", H2), ("RN", RN))},
        "power": {"H1": {h: H1["halves"][h] for h in "AB"}, "H2": {h: H2["halves"][h] for h in "AB"},
                  "RN_projection": projection,
                  "groups_after_drops": {"H1": H1["groups_by_half"], "H2": H2["groups_by_half"],
                                         "RN": RN["groups_by_half"]}},
        "H1": H1, "H2": H2, "RN": RN, "info": info_cells(rows, mirror), "rn_info": rn_info(rows),
        "overlap_rn_with_H1H2": {"ABOVE_in_H1_TOP": sum(1 for r in rows if IS_ABOVE(r) and IS_TOP(r)),
                                 "ABOVE_in_HTF": sum(1 for r in rows if IS_ABOVE(r) and IS_HTF(r))},
    }


def _f(x, nd=1):
    return "n/a" if x is None else (f"{x:.{nd}f}" if isinstance(x, (int, float)) else str(x))


def render_report(res) -> str:
    L = ["# Name history + round numbers - result", "",
         f"prereg {', '.join(res['prereg'])}; script {res['script_rev']}; frozen groups sha256 "
         f"{res['frozen_sha256'][:12]}", ""]
    if res["failed_data"]:
        L += ["**FAILED-DATA: the groups' quote-drop rates differ by > 2 pp - do not read.**", ""]
    L += ["## Power (before any mean)", "", "| hyp | half | n | names | sessions | SE bp | MDE bp |", "|---|---|---|---|---|---|---|"]
    for hyp in ("H1", "H2"):
        for h in "AB":
            x = res["power"][hyp][h]
            L.append(f"| {hyp} | {h} | {x['n']} | {x['names']} | {x['sessions']} | {_f(x['se'])} | {_f(x['mde'])} |")
    for h in "AB":
        x = res["power"]["RN_projection"][h]
        L.append(f"| RN (projected) | {h} | {x['n_above']}+{x['n_below']} | | | {_f(x['projected_se'])} | "
                 f"{_f(x['projected_mde'])} |")
    L += ["", "n events and distinct names per group per half, after drops (power rule):", "",
          "| hyp | half | better n | better names | other n | other names |", "|---|---|---|---|---|---|"]
    for hyp in ("H1", "H2", "RN"):
        g = res["power"]["groups_after_drops"][hyp]
        for h in "AB":
            L.append(f"| {hyp} | {h} | {g[h + '_better']['n']} | {g[h + '_better']['names']} | "
                     f"{g[h + '_other']['n']} | {g[h + '_other']['names']} |")
    L += ["", "## Verdicts", ""]
    for hyp in ("H1", "H2", "RN"):
        v = res[hyp]
        p = v["pooled"]
        L.append(f"- **{hyp}: {v.get('verdict_data') or v['verdict']}** {v.get('label', '')} - diff {_f(p['coef'])} bp, "
                 f"t {_f(p['t'], 2)} (crit {_f(p.get('t_crit'), 2)}), halves "
                 f"{_f(v['halves']['A']['coef'])} / {_f(v['halves']['B']['coef'])}, better group "
                 f"{_f(v['better']['coef'])} bp; {'; '.join(v['why'])}")
    L += ["", "Information cells are in result.json (info, rn_info); none can be promoted.", ""]
    return "\n".join(L)


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] not in ("fetch", "count", "score", "report"):
        raise SystemExit(__doc__)
    if a[0] == "report":
        res = BRO.jload(os.path.join(WORK, "result.json"))
        if res is None:
            raise SystemExit("no result.json")
        open(os.path.join(WORK, "report.md"), "w").write(render_report(res))
        P_(f"report.md written to {WORK}")
        return
    run(a[0])


if __name__ == "__main__":
    main()
