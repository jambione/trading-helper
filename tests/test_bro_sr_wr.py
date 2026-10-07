"""Tests for tools/studies/bro_sr_wr.py (prereg docs/studies/bro_sr_wr_prereg.json). No network: a fake market."""
from __future__ import annotations

import collections
import math
import os
import sys
from datetime import datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
import bro_sr_wr as B  # noqa: E402

DAY = "2026-09-15"   # a Tuesday


def T(hh, mm, ss=0):
    return B.et_ts(DAY, hh, mm) + ss


class FakeMarket:
    guard = False

    def __init__(self, trades=None, quotes=None):
        self.trades = trades or {}      # sym -> [(ts, px)]
        self.quotes = quotes or {}      # sym -> [(ts, bid, ask)]
        self.fails, self.requests, self.work = {}, 0, "/nonexistent"

    def first_trade(self, sym, t0, t1):
        xs = [x for x in self.trades.get(sym, []) if t0 <= x[0] <= t1]
        return list(min(xs)) if xs else None

    def last_trade(self, sym, t0, t1):
        xs = [x for x in self.trades.get(sym, []) if t0 <= x[0] <= t1]
        return list(max(xs)) if xs else None

    def quote(self, sym, t):
        xs = [x for x in self.quotes.get(sym, []) if t - B.QUOTE_LOOKBACK <= x[0] <= t]
        return [max(xs)[1], max(xs)[2]] if xs else None


def row(text, unix, sym="ABC", at=None):
    return {"ts": "", "unix": unix, "said": "", "at": at if at is not None else unix - 30, "ticker": sym,
            "text": text, "price": 999.0}


# ------------------------------------------------------------------ cleaning
def test_bleed_rows_dropped_and_row_count_recorded():
    rows = [row("ABC going here", T(10, 0)), row("Starting dashboard on port 8000", T(10, 5)),
            row("cloudflared TUNNEL up", T(10, 6), sym="XYZ"), row("Watchdog restarted engine", T(10, 7), sym="Q")]
    lines, c = B.clean_rows(rows)
    assert c["rows_total"] == 4
    assert c["drop_bleed"] == 3
    assert list(lines) == [(DAY, "ABC")]


def test_ocr_merge_within_60s_into_first_and_does_not_chain():
    rows = [row("ABC break hod", T(10, 0)), row("ABC break hod.", T(10, 0, 40)),
            row("ABC break hod!", T(10, 1, 10)),          # 70 s after the first: a new line, no chaining
            row("ABC break hod", T(10, 1, 50))]           # 40 s after the second group's first row
    lines, c = B.clean_rows(rows)
    ls = lines[(DAY, "ABC")]
    assert c["merged_rereads"] == 2
    assert [x["unix"] for x in ls] == [T(10, 0), T(10, 1, 10)]
    assert ls[0]["n_rows"] == 2 and ls[0]["text"] == "ABC break hod"


def test_ocr_twin_is_nonactionable_as_of_its_own_time():
    # a twin before any actionable line is skipped; with no later actionable call the name-day is dropped as a twin
    rows = [row("ABC on watch", T(10, 0)), row("ABC on watch lg tloat", T(10, 0, 20)),
            row("DEF on watch", T(10, 0), sym="DEF")]
    lines, _ = B.clean_rows(rows)
    assert B.book_of(lines[(DAY, "ABC")]) == ("ocr_twin_nonactionable", None)
    assert B.book_of(lines[(DAY, "DEF")])[1]["R"] == T(10, 0)
    # a twin before the first actionable line: the NEXT actionable call becomes R
    rows += [row("ABC retest hod", T(10, 30))]
    lines, _ = B.clean_rows(rows)
    why, bk = B.book_of(lines[(DAY, "ABC")])
    assert why is None and bk["R"] == T(10, 30) and bk["twins_before_R"] == 1 and bk["exit_unix"] is None


def test_later_twin_is_an_exit_line_and_does_not_remove_the_earlier_call():
    # skeptic probe: "ABC test hod" 07:00, then "ABC sold" / "ABC solo" ~7 h later
    rows = [row("ABC test hod", T(7, 0)), row("ABC sold", T(14, 0)), row("ABC solo", T(14, 0, 15))]
    lines, _ = B.clean_rows(rows)
    assert lines[(DAY, "ABC")][1]["twin"]
    why, bk = B.book_of(lines[(DAY, "ABC")])
    assert why is None and bk["R"] == T(7, 0)
    assert bk["exit_unix"] == T(14, 0) and bk["exit_is_twin"]
    assert B.on_book(bk, T(10, 0)) and B.on_book(bk, T(13, 59)) and B.on_book(bk, T(14, 0))
    assert not B.on_book(bk, T(14, 1))


@pytest.mark.parametrize("text", ["ABC Ig float", "ABC |g float", "ABC lg tloat", "ABC will avoi", "ABC not enough vol",
                                  "ABC p&d", "ABC penny junk", "ABC sold", "ABC not for me", "ABC will adjust or avoid"])
def test_fuzzy_and_desk_nonactionable(text):
    assert B.actionable(text) is False


def test_actionable_plain_call():
    assert B.actionable("ABC going over hod with vol") is True


def test_exit_line_removes_name_from_book_and_keep_variant():
    rows = [row("ABC sold earlier", T(9, 0)), row("ABC on watch", T(10, 0)), row("ABC retest hod", T(10, 30)),
            row("ABC sold lotto", T(11, 0)), row("ABC back on watch", T(12, 0))]
    lines, _ = B.clean_rows(rows)
    why, bk = B.book_of(lines[(DAY, "ABC")])
    assert why is None and bk["R"] == T(10, 0) and bk["exit_unix"] == T(11, 0)
    nd = {**bk}
    assert not B.on_book(nd, T(10, 0))            # t must be after R
    assert B.on_book(nd, T(10, 1))
    assert B.on_book(nd, T(11, 0))                # the exit line is not BEFORE t yet
    assert not B.on_book(nd, T(11, 1))
    assert not B.on_book(nd, T(12, 30))           # a later actionable line does not resurrect it
    assert B.on_book(nd, T(12, 30), keep_exit=True)


def test_archive_price_field_is_never_read():
    rows = [row("ABC going", T(10, 0))]
    rows[0]["price"] = "boom"   # would raise if anything parsed it
    lines, _ = B.clean_rows(rows)
    assert "price" not in lines[(DAY, "ABC")][0]


# ------------------------------------------------------------------ exclusions
@pytest.mark.parametrize("sym,a,why", [
    ("SPY", {"class": "us_equity", "exchange": "ARCA", "name": "State Street SPDR S&P 500 ETF Trust"}, "etf_or_index"),
    ("IBIT", {"class": "us_equity", "exchange": "NASDAQ", "name": "iShares Bitcoin Trust ETF"}, "etf_or_index"),
    ("QQQ", {"class": "us_equity", "exchange": "NASDAQ", "name": "Invesco QQQ Trust, Series 1"}, "etf_or_index"),
    ("SOXL", {"class": "us_equity", "exchange": "ARCA", "name": "Direxion Daily Semiconductor Bull 3X"}, "etf_or_index"),
    ("ABCDW", {"class": "us_equity", "exchange": "NASDAQ", "name": "ABC Corp Warrants"}, "warrant_unit_right"),
    ("ABCU", {"class": "us_equity", "exchange": "NASDAQ", "name": "ABC Acquisition Units"}, "warrant_unit_right"),
    ("ABC", {"class": "us_equity", "exchange": "OTC", "name": "ABC Inc"}, "exchange_not_allowed"),
    ("ABC", None, "not_an_alpaca_asset"),
    ("ET", {"class": "us_equity", "exchange": "NYSE", "name": "Energy Transfer LP Common Units"}, None),
    ("NVDA", {"class": "us_equity", "exchange": "NASDAQ", "name": "NVIDIA Corporation Common Stock"}, None),
])
def test_asset_validation_and_etf_exclusion(sym, a, why):
    assert B.asset_reason(sym, a) == why


# ------------------------------------------------------------------ group assignment
def _nd(sym="ABC", R=None, adv=None, exit_unix=None):
    return {"day": DAY, "sym": sym, "R": R or T(10, 3, 20), "close": T(16, 0), "adv20": adv, "exit_unix": exit_unix}


@pytest.mark.parametrize("px,bid,ask,adv,want", [
    (20.0, 19.995, 20.005, 1e8, "T"),           # 5 bp exactly at the cap
    (20.0, 19.99, 20.01, 1e8, "excl_spread_above_cap_T"),
    (200.0, 199.99, 200.01, 6e9, "MEGA"),       # ADV >= $5B: separate cell, never T
    (5.0, 4.98, 5.02, None, "S"),
    (5.0, 4.9, 5.1, None, "excl_spread_above_cap_S"),
    (1.5, 1.49, 1.51, None, "excl_under_2"),
    (2.0, 1.995, 2.005, None, "S"),
    (10.0, 9.9999, 10.0001, None, "T"),
])
def test_group_assignment(px, bid, ask, adv, want):
    nd = _nd(adv=adv)
    mkt = FakeMarket(trades={"ABC": [(T(10, 3), px)]}, quotes={"ABC": [(T(10, 3, 50), bid, ask)]})
    assert B.assign_group(mkt, nd, collections.Counter()) == want
    assert nd["a0"] == T(10, 4)                   # first 1-min close at or after R


def test_assignment_minute_premarket_call_and_no_quote():
    nd = _nd(R=T(7, 15))
    mkt = FakeMarket(trades={"ABC": [(T(9, 30, 30), 12.0)]}, quotes={})
    assert B.assign_group(mkt, nd, collections.Counter()) == "excl_no_quote_at_assignment"
    assert nd["a0"] == T(9, 31)


def test_assignment_uses_sip_last_trade_not_later_prices():
    # once assigned at a0 the group never changes, whatever the price does later
    nd = _nd()
    mkt = FakeMarket(trades={"ABC": [(T(10, 3), 12.0), (T(10, 30), 1.0)]},
                     quotes={"ABC": [(T(10, 4), 11.999, 12.001)]})
    assert B.assign_group(mkt, nd, collections.Counter()) == "T"
    assert nd["assign_px"] == 12.0


# ------------------------------------------------------------------ PASS logic
def _arr(n, f):
    return [f(i) for i in range(n)]


def test_state_strict_slope_and_warmup():
    n = 130
    fast = _arr(n, lambda i: -80 + i * 0.1)
    slow = _arr(n, lambda i: -90 + i * 0.1)
    sr = [True] * n
    assert B._state(113, fast, slow, sr, 1.0) == {"warm": False}      # only 111 rows up to t-3
    st = B._state(114, fast, slow, sr, 1.0)
    assert st["warm"] and st["wr"] and st["pass"]
    flat = [-50.0] * n
    assert not B._state(120, fast, flat, sr, 1.0)["wr"]                 # equal is not "strictly greater"
    nan = list(slow)
    nan[117] = float("nan")
    assert not B._state(120, fast, nan, sr, 1.0)["wr"]
    assert not B._state(120, fast, slow, [False] * n, 1.0)["pass"]     # in resistance -> not PASS


def _bars(closes, t0, step=60):
    return [[t0 + step * i, c, c + 0.01, c - 0.01, c, 100] for i, c in enumerate(closes)]


def test_pass_series_on_synthetic_bars_skips_warmup():
    closes = [10 - 0.01 * i for i in range(150)] + [8.5 + 0.02 * i for i in range(60)]
    rows = _bars(closes, T(4, 0))
    s = B.pass_series(rows)
    ks = sorted(s)
    assert len(ks) >= 210
    assert all(not s[k]["warm"] for k in ks[:114])
    assert s[ks[114]]["warm"]
    late = [s[k] for k in ks[160:209]]
    assert any(x["wr"] for x in late)
    assert not any(s[k].get("wr") for k in ks[114:150])                # falling: %R not trending up


def test_desk_grid_reuses_signals_function_unchanged():
    import pandas as pd
    import signals
    closes = [10 + math.sin(i / 7) for i in range(200)]
    rows = _bars(closes, T(4, 0))
    rows = [r for i, r in enumerate(rows) if i % 5 not in (1, 2)]        # IEX-style holes of 2 minutes
    df = pd.DataFrame({"time": pd.to_datetime([r[0] for r in rows], unit="s", utc=True),
                       "high": [r[2] for r in rows], "low": [r[3] for r in rows], "close": [r[4] for r in rows]})
    direct = signals._minute_grid_pr(df, 21, 7).to_numpy()
    grid = B.desk_grid(rows)
    fast, _, _, _ = B.features(grid)
    at_real = [f for f, g in zip(fast, grid) if g[5]]
    assert len(at_real) == len(direct)
    for a, b in zip(at_real, direct):
        assert (math.isnan(a) and math.isnan(b)) or abs(a - b) < 1e-9


def test_point_in_time_trailing_flats_inside_long_gap():
    closes = [10 + 0.01 * i for i in range(130)]
    rows = _bars(closes, T(8, 0))
    rows.append([rows[-1][0] + 40 * 60, 12, 12.01, 11.99, 12, 100])     # next bar 40 min later: not a desk-filled gap
    s = B.pass_series(rows)
    last = rows[-2][0]
    assert last + 60 * 14 + 60 in s                                    # 14 trailing minutes are evaluable
    assert last + 60 * 15 + 60 not in s                                # beyond that: not a grid minute


# ------------------------------------------------------------------ cost, halts, entries
def test_cost_rule_full_spread_plus_cent_under_5():
    spr, cost = B.cost_frac(9.99, 10.01, 10.0)
    assert abs(spr - 0.002) < 1e-12 and cost == spr
    spr, cost = B.cost_frac(3.99, 4.01, 4.0)
    assert abs(cost - (0.005 + 0.01 / 4.0)) < 1e-12


def _book_nd(group="S", sym="ABC", R=None, series=None, sip=None):
    nd = {"day": DAY, "sym": sym, "R": R or T(9, 50), "a0": T(9, 51), "close": T(16, 0), "exit_unix": None,
          "group": group, "iex": series or {}, "sip": sip or []}
    return nd


def _series(pass_at, lo=T(9, 31), hi=T(15, 30), warm=True):
    return {t: {"warm": warm, "sr_ok": True, "wr": t in pass_at, "pass": t in pass_at}
            for t in range(int(lo), int(hi) + 1, 60)}


def test_missing_quote_drops_the_call():
    nd = _book_nd(series=_series({T(10, 0)}))
    mkt = FakeMarket(trades={"ABC": [(T(10, 0, 5), 4.0), (T(10, 14), 4.2)]}, quotes={})
    c = collections.Counter()
    assert B.find_primary(mkt, nd, nd["iex"], c) is None
    assert c["drop_no_quote_at_entry"] == 1


def test_primary_entry_exit_and_net():
    nd = _book_nd(series=_series({T(10, 0), T(10, 5)}))
    mkt = FakeMarket(trades={"ABC": [(T(10, 0, 1), 3.9), (T(10, 0, 3), 4.0), (T(10, 14, 50), 4.4), (T(10, 15, 9), 9.0)]},
                     quotes={"ABC": [(T(9, 59, 58), 3.99, 4.01)]})
    tr = B.find_primary(mkt, nd, nd["iex"], collections.Counter())
    assert tr["t"] == T(10, 0)
    assert tr["entry_ts"] == T(10, 0, 3) and tr["entry_px"] == 4.0      # first trade at or after t + 2 s
    assert tr["exit_px"] == 4.4                                          # last trade at or before entry + 15 min
    assert abs(tr["net_bp"] - (1000 - 50 - 25)) < 1e-6


def test_T_spread_recheck_skips_and_continues():
    nd = _book_nd(group="T", series=_series({T(10, 0), T(10, 5)}))
    mkt = FakeMarket(trades={"ABC": [(T(10, 5, 2), 20.0), (T(10, 19), 20.2)]},
                     quotes={"ABC": [(T(10, 0), 19.9, 20.1), (T(10, 5), 19.995, 20.005)]})
    c = collections.Counter()
    tr = B.find_primary(mkt, nd, nd["iex"], c)
    assert tr["t"] == T(10, 5) and c["T_spread_skip"] == 1


def test_warmup_minutes_are_skipped_and_counted():
    s = _series({T(10, 0)})
    for t in [k for k in s if T(9, 51) <= k < T(10, 0)][:5]:
        s[t] = {"warm": False}
    nd = _book_nd(series=s)
    c = collections.Counter()
    mkt = FakeMarket(trades={"ABC": [(T(10, 0, 2), 5.0)]}, quotes={"ABC": [(T(10, 0), 4.99, 5.01)]})
    B.find_primary(mkt, nd, s, c)
    assert c["warmup_skipped_minutes"] == 5


def test_halt_exit_uses_first_trade_after_resume():
    entry = T(10, 0, 3)
    X = entry + 900
    sip = [[T(9, 50) + 60 * i, 4, 4, 4, 4, 100] for i in range(25)]             # 09:50..10:14 print every minute
    sip = [r for r in sip if not (T(10, 12) <= r[0] <= T(10, 18))]               # 10:12-10:18 missing...
    sip.append([T(10, 19), 5, 5, 5, 5, 100])                                     # ...resume at 10:19
    sip = [r for r in sip if r[0] < T(10, 12) or r[0] >= T(10, 19)]
    assert B.is_halted(sip, X)
    mkt = FakeMarket(trades={"ABC": [(entry, 4.0), (T(10, 11, 50), 4.5), (T(10, 19, 1), 5.0)]})
    c = collections.Counter()
    tr = B.simulate(mkt, "ABC", T(10, 0, 2), [3.99, 4.01], sip, T(16, 0), c)
    assert tr["halt"] and tr["exit_px"] == 5.0 and c["halt_exit"] == 1


def test_thin_gap_is_not_a_halt():
    entry = T(10, 0, 3)
    X = entry + 900
    sip = [[T(9, 50), 4, 4, 4, 4, 100], [T(10, 30), 4, 4, 4, 4, 100]]          # never printed every minute before
    assert not B.is_halted(sip, X)
    mkt = FakeMarket(trades={"ABC": [(entry, 4.0), (T(10, 30, 5), 5.0)]})
    tr = B.simulate(mkt, "ABC", T(10, 0, 2), [3.99, 4.01], sip, T(16, 0), collections.Counter())
    assert not tr["halt"] and tr["exit_px"] == 4.0


# ------------------------------------------------------------------ cross-name control
def _ctrl_setup():
    t = T(11, 0)
    prim = _book_nd(sym="AAA", series=_series({t}))
    names = {
        "LATE": dict(_book_nd(sym="LATE", R=T(11, 30)), a0=T(11, 31)),             # called after t: no hindsight
        "OFFB": dict(_book_nd(sym="OFFB"), exit_unix=T(10, 30)),                   # exit line before t
        "PASSX": _book_nd(sym="PASSX", series=_series({t})),                       # PASS at t
        "COLD": _book_nd(sym="COLD", series=_series(set(), warm=False)),           # not warm
        "OTHERG": _book_nd(group="T", sym="OTHERG"),                               # other group
        "GOOD": _book_nd(sym="GOOD"),
    }
    for k in ("LATE", "OFFB", "OTHERG", "GOOD"):
        names[k]["iex"] = _series(set())
    trades = {s: [(t + 2, 5.0), (t + 60 + 2, 5.0), (t + 900, 5.1), (t + 960, 5.1)] for s in names}
    quotes = {s: [(t - 61, 4.99, 5.01), (t - 1, 4.99, 5.01), (t + 59, 4.99, 5.01)] for s in names}
    return t, prim, names, FakeMarket(trades=trades, quotes=quotes)


def test_control_only_from_names_on_book_and_not_pass_at_t():
    t, prim, names, mkt = _ctrl_setup()
    picks = set()
    for i in range(20):        # every seed-53 draw lands on the only eligible name
        p = dict(prim, sym=f"AAA{i}")
        c = B.control_primary(mkt, p, {"t": t}, [p] + list(names.values()), collections.Counter())
        picks.add(c["sym"])
        assert c["offset_min"] == 0 and c["t"] == t
    assert picks == {"GOOD"}


def test_control_fallback_and_drop_counted():
    t, prim, names, mkt = _ctrl_setup()
    good = names["GOOD"]
    good["iex"] = _series({t})                     # PASS at t, not PASS at t - 1 min
    c = collections.Counter()
    pair = B.control_primary(mkt, prim, {"t": t}, [prim, good], c)
    assert pair["offset_min"] == -1 and c["ctrl_fallback_used"] == 1
    good["iex"] = _series({t + 60 * k for k in range(-2, 3)})
    c = collections.Counter()
    assert B.control_primary(mkt, prim, {"t": t}, [prim, good], c) is None
    assert c["ctrl_pair_dropped_no_control"] == 1


def test_control_is_seeded_and_deterministic():
    t, prim, names, mkt = _ctrl_setup()
    a = _book_nd(sym="GOOD2")
    a["iex"] = _series(set())
    mkt.trades["GOOD2"], mkt.quotes["GOOD2"] = mkt.trades["GOOD"], mkt.quotes["GOOD"]
    nds = [prim, names["GOOD"], a]
    r1 = B.control_primary(mkt, prim, {"t": t}, nds, collections.Counter())["sym"]
    r2 = B.control_primary(mkt, prim, {"t": t}, list(reversed(nds)), collections.Counter())["sym"]
    assert r1 == r2


# ------------------------------------------------------------------ statistics and verdict
def test_t_crit_is_the_224_equivalent():
    assert abs(B.t_crit(100000) - 2.24) < 1e-3
    assert 2.4 < B.t_crit(19) < 2.5
    assert B.t_crit(9) > B.t_crit(19)
    assert abs(B.t_cdf(2.0, 10) - 0.963306) < 1e-5


def _st(n, sessions, mean, se):
    return {"n": n, "sessions": sessions, "mean": mean, "se": se, "t": mean / se, "df": sessions - 1,
            "mde": 2.84 * se}


def test_verdict_pass_needs_both_halves_and_contrast():
    good = _st(80, 20, 100.0, 30.0)                       # t 3.33 > crit(19) ~2.43, MDE 85
    con = _st(150, 40, 80.0, 25.0)
    assert B.verdict({"A": dict(good), "B": dict(good)}, dict(con))["verdict"] == "PASS"
    weak = _st(80, 20, 70.0, 30.0)                        # t 2.33: above 2.24, below crit(19)
    assert B.verdict({"A": dict(good), "B": weak}, dict(con))["verdict"] == "FAIL"
    few = _st(59, 20, 100.0, 30.0)
    assert B.verdict({"A": dict(good), "B": few}, dict(con))["verdict"] == "FAIL"
    small_c = _st(150, 40, 45.0, 10.0)
    assert B.verdict({"A": dict(good), "B": dict(good)}, small_c)["verdict"] == "FAIL"


def test_verdict_t_le_minus_2_fails_even_underpowered():
    neg = _st(30, 10, -200.0, 90.0)                       # t -2.22, MDE 256
    assert B.verdict({"A": neg, "B": _st(30, 10, 10.0, 90.0)}, _st(40, 20, 0.0, 80.0))["verdict"] == "FAIL"
    assert B.verdict({"A": _st(30, 10, -200.0, 90.0), "B": _st(30, 10, 10.0, 90.0)}, _st(40, 20, 0.0, 80.0),
                     declared_underpowered=True)["verdict"] == "FAIL"


def test_verdict_underpowered_when_mde_above_150():
    a = _st(40, 10, 20.0, 60.0)                          # MDE 170
    assert B.verdict({"A": a, "B": _st(40, 10, 20.0, 40.0)}, _st(60, 20, 0.0, 30.0))["verdict"] == "UNDERPOWERED"
    con = _st(60, 20, 0.0, 60.0)                         # contrast MDE 170 alone also underpowers
    assert B.verdict({"A": _st(80, 20, 10.0, 30.0), "B": _st(80, 20, 10.0, 30.0)}, con)["verdict"] == "UNDERPOWERED"
    assert B.verdict({"A": _st(80, 20, 10.0, 30.0), "B": _st(80, 20, 10.0, 30.0)},
                     _st(60, 20, 0.0, 30.0))["verdict"] == "FAIL"


def test_T_declared_underpowered_historically():
    good = _st(80, 20, 100.0, 30.0)
    v = B.verdict({"A": dict(good), "B": dict(good)}, _st(150, 40, 80.0, 25.0), declared_underpowered=True)
    assert v["verdict"] == "UNDERPOWERED"


def test_clustered_by_day():
    rows = [{"day": "d1", "net_bp": 10.0}, {"day": "d1", "net_bp": 30.0}, {"day": "d2", "net_bp": -10.0}]
    s = B.clustered(rows)
    assert s["n"] == 3 and s["sessions"] == 2 and s["df"] == 1
    m = 10.0
    want = math.sqrt(2 * ((0 + 20) ** 2 + (-20) ** 2)) / 3
    assert abs(s["mean"] - m) < 1e-12 and abs(s["se"] - want) < 1e-12


# ------------------------------------------------------------------ market-hours guard
@pytest.mark.parametrize("et,want", [((2026, 10, 7, 10, 0), True), ((2026, 10, 7, 9, 0), True),
                                     ((2026, 10, 7, 8, 59), False), ((2026, 10, 7, 16, 29), True),
                                     ((2026, 10, 7, 16, 30), False), ((2026, 10, 10, 12, 0), False)])
def test_market_hours_window(et, want):
    now = datetime(*et, tzinfo=B.ET)
    assert B.in_market_hours(now) is want


def test_guard_refuses_and_every_request_rechecks(monkeypatch):
    with pytest.raises(SystemExit):
        B.market_hours_guard(datetime(2026, 10, 7, 11, 0, tzinfo=B.ET))
    B.market_hours_guard(datetime(2026, 10, 7, 20, 0, tzinfo=B.ET))
    monkeypatch.setattr(B, "in_market_hours", lambda now=None: True)
    m = B.AlpacaMarket(work="/nonexistent", guard=True)
    with pytest.raises(SystemExit):
        m._get("https://example.invalid", {})
    assert m.requests == 0


def test_fetch_failure_is_not_cached_and_is_logged(tmp_path):
    m = B.AlpacaMarket(work=str(tmp_path), guard=False)

    def boom():
        raise B.FetchFail("x")
    with pytest.raises(B.FetchFail):
        m._cached("quotes", "ABC|1", boom)
    assert "quotes|ABC|1" in m.fails and "ABC|1" not in m._cache("quotes")
    assert m._cached("quotes", "ABC|1", lambda: None) is None          # "no data" is a cached answer
    assert "quotes|ABC|1" not in m.fails


# ------------------------------------------------------------------ skeptic 2bfcf31 items 2-4
class RecordingMarket(FakeMarket):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.quote_calls = []

    def quote(self, sym, t):
        self.quote_calls.append(t)
        return super().quote(sym, t)


def test_premarket_call_cell_costs_at_first_rth_print():
    nd = _book_nd(R=T(7, 15))
    nd["iex"] = _series(set())
    # no regular premarket print in the fake tape: the first regular print is 09:30:05
    mkt = RecordingMarket(trades={"ABC": [(T(9, 30, 5), 4.0), (T(9, 45), 4.2)]},
                          quotes={"ABC": [(T(7, 14), 2.0, 6.0), (T(9, 30, 4), 3.99, 4.01)]})
    out = B.info_cells(mkt, nd, None, collections.Counter())
    assert "call_moment" not in out
    c = out["premarket_call_first_rth_print"]
    assert c["entry_ts"] == T(9, 30, 5) and abs(c["spread_bp"] - 50.0) < 1e-6   # not the 07:14 quote
    assert T(7, 15) not in mkt.quote_calls and T(9, 30, 5) in mkt.quote_calls


def test_rth_call_cell_unchanged():
    nd = _book_nd(R=T(10, 15))
    nd["iex"] = _series(set())
    mkt = RecordingMarket(trades={"ABC": [(T(10, 15, 3), 4.0), (T(10, 29), 4.2)]},
                          quotes={"ABC": [(T(10, 15), 3.99, 4.01)]})
    out = B.info_cells(mkt, nd, None, collections.Counter())
    assert out["call_moment"]["entry_ts"] == T(10, 15, 3) and "premarket_call_first_rth_print" not in out


def test_pass_series_marks_real_bars():
    closes = [10 + 0.01 * i for i in range(130)]
    rows = [r for i, r in enumerate(_bars(closes, T(8, 0))) if i % 4 != 2]
    s = B.pass_series(rows)
    real = {r[0] + 60 for r in rows}
    assert all(s[k]["real"] == (k in real) for k in s)
    assert any(not v["real"] for v in s.values())


def _summ_rows():
    base = {"day": DAY, "half": "A", "R": T(10, 0), "a0": T(10, 1), "premarket_call": False, "info": {}}
    p = {"t": T(10, 5), "net_bp": 10.0, "spread_bp": 4.0, "halt": True}
    return [dict(base, sym="A1", group="S", primary=p, control_primary=dict(p, halt=False), control_secondary=None),
            dict(base, sym="A2", group="S", primary=dict(p, halt=False), control_primary=None,
                 control_secondary=dict(p)),
            dict(base, sym="A3", group="T", primary=dict(p), control_primary=None, control_secondary=None)]


def test_summary_reports_halts_per_group_and_no_trade_flag():
    c = collections.Counter({"group_excl_no_trade_at_assignment": 1})
    res = B.summarize(_summ_rows(), c, {"archive_rows": 3})
    sp = res["groups"]["S"]["power"]
    assert sp["halt_exit"]["primary"] == 1 and sp["halt_exit"]["control_secondary"] == 1
    assert sp["halt_exit"]["control_primary"] == 0 and sp["halt_exit"]["primary_by_half"] == {"A": 1, "B": 0}
    assert res["groups"]["T"]["power"]["halt_exit"]["primary"] == 1
    assert sp["excl_no_trade_at_assignment"] == 1 and sp["excl_no_trade_flag"]       # 1 of 2 S name-days > 10%
    md = B.render_report(res)
    assert "FLAG: > 10% of S" in md and "halt exits" in md
    res = B.summarize(_summ_rows(), collections.Counter(), {"archive_rows": 3})
    assert not res["groups"]["S"]["power"]["excl_no_trade_flag"]
    assert "FLAG: > 10% of S" not in B.render_report(res)


# ------------------------------------------------------------------ information_only_added_2026-10-07
@pytest.mark.parametrize("text,want", [
    ("ABC test hod", "hod"), ("ABC testing the hod with vol", "hod"), ("ABC near hod", "hod"), ("ABC nhod", "hod"),
    ("ABC nhod pop micro pr", "hod"),            # first match wins, in order
    ("ABC nice pop", "pop"), ("ABC popping", "pop"), ("ABC pop on pr", "pop"),
    ("ABC micro float", "micro"), ("ABC low float runner", "micro"), ("ABC micro following pr", "micro"),
    ("ABC following pr", "news"), ("ABC news out", "news"), ("ABC PR", "news"),
    ("ABC break hod", "other"), ("ABC popular", "other"), ("ABC price ok", "other"), ("", "other"), (None, "other"),
])
def test_phrase_class(text, want):
    assert B.phrase_class(text) == want


def test_phrase_table_per_class():
    gr = [{"phrase_class": "hod", "primary": {"net_bp": 10.0}}, {"phrase_class": "hod", "primary": {"net_bp": -30.0}},
          {"phrase_class": "hod", "primary": {"net_bp": 50.0}}, {"phrase_class": "pop", "primary": None},
          {"phrase_class": "other", "primary": {"net_bp": -5.0}}]
    t = B.phrase_table(gr)
    assert list(t) == ["hod", "pop", "micro", "news", "other"]
    assert t["hod"]["n"] == 3 and abs(t["hod"]["mean"] - 10.0) < 1e-12 and t["hod"]["median"] == 10.0
    assert abs(t["hod"]["win_rate"] - 2 / 3) < 1e-12
    assert t["pop"]["n"] == 0 and t["pop"]["mean"] is None
    assert t["other"]["win_rate"] == 0.0


def _wr_series(wr_at, sr_ok=False, lo=T(9, 31), hi=T(15, 30)):
    return {t: {"warm": True, "sr_ok": sr_ok, "wr": t in wr_at, "pass": sr_ok and t in wr_at}
            for t in range(int(lo), int(hi) + 1, 60)}


def test_wr_only_primary_ignores_sr_and_control_must_not_be_wr():
    t = T(11, 0)
    nd = _book_nd(series=_wr_series({t}))                   # in resistance all day: never PASS, WR at 11:00
    wr_ctrl = _book_nd(sym="WRX", series=_wr_series({t}))   # WR_trending at t (but not PASS): ineligible here
    ok = _book_nd(sym="OK", series=_wr_series(set()))
    trades = {s: [(t + 2, 5.0), (t + 900, 5.1)] for s in ("ABC", "WRX", "OK")}
    quotes = {s: [(t - 1, 4.99, 5.01)] for s in ("ABC", "WRX", "OK")}
    mkt = FakeMarket(trades=trades, quotes=quotes)
    c = collections.Counter()
    assert B.find_primary(mkt, nd, nd["iex"], c) is None                    # PRIMARY (SR + WR) never fires
    pw = B.find_primary(mkt, nd, nd["iex"], c, key="wr")
    assert pw["t"] == t
    for i in range(10):
        p = dict(nd, sym=f"ABC{i}")
        cw = B.control_primary(mkt, p, pw, [p, wr_ctrl, ok], collections.Counter(), key="wr", tag="wronly_ctrl_")
        assert cw["sym"] == "OK"
    # under the PASS rule the WR-trending name is an eligible control (it is not PASS at t)
    assert B.control_eligible(wr_ctrl, t) and not B.control_eligible(wr_ctrl, t, key="wr")


def test_summary_carries_wr_only_and_phrase_cells_and_chart_caveat():
    rows = _summ_rows()
    rows[0]["phrase_class"] = "hod"
    rows[0]["info_wr_only"] = {"primary": {"t": T(10, 5), "net_bp": 20.0},
                               "control_primary": {"net_bp": 5.0}}
    res = B.summarize(rows, collections.Counter(), {"archive_rows": 3})
    si = res["groups"]["S"]["info"]
    assert si["wr_only_primary"]["n"] == 1 and si["wr_only_primary"]["mean"] == 20.0
    assert si["wr_only_minus_control"]["mean"] == 15.0
    assert si["primary_by_phrase_class"]["hod"]["n"] == 1
    assert "wr_only_primary" not in res["groups"]["T"]["info"]
    md = B.render_report(res)
    assert md.index("chart_check_2026-10-07") < md.index("## 1. Power")
    assert "MISMATCH on thin premarket names" in md and "PRIMARY by call phrase class" in md


# ------------------------------------------------------------------ end to end on a fake market
class FakeBarsMarket(FakeMarket):
    def __init__(self, bars, **kw):
        super().__init__(**kw)
        self.bars = bars

    def minute_bars(self, sym, feed, start, end):
        return [r for r in self.bars.get((sym, feed), []) if start <= r[0] <= end]

    def daily_adv(self, syms, day):
        return {s: 1e8 for s in syms}

    def flush(self):
        pass


def test_pipeline_end_to_end_on_synthetic_day():
    prior = "2026-09-14"
    cal = {prior: [B.et_ts(prior, 9, 30), B.et_ts(prior, 16)], DAY: [T(9, 30), T(16, 0)]}
    bars, trades, quotes = {}, {}, {}
    for k, sym in enumerate(("AAA", "BBB", "CCC")):
        rows = []
        for d in (prior, DAY):
            for m in range(4 * 60, 16 * 60):
                ts = B.et_ts(d, m // 60, m % 60)
                c = 5 + 0.3 * math.sin(m / (9.0 + k)) + 0.001 * m
                rows.append([ts, c, c + 0.01, c - 0.01, c, 1000])
        bars[(sym, "iex")] = bars[(sym, "sip")] = rows
        today = [r for r in rows if B.et_day(r[0]) == DAY]
        trades[sym] = sorted([(r[0] + 2, r[4]) for r in today] + [(r[0] + 59, r[4]) for r in today])
        quotes[sym] = [(r[0] + 1, r[4] - 0.005, r[4] + 0.005) for r in today]
    mkt = FakeBarsMarket(bars, trades=trades, quotes=quotes)
    rows_in = [row("AAA on watch", T(9, 45), sym="AAA"), row("BBB going", T(10, 5), sym="BBB"),
               row("CCC strong", T(8, 0), sym="CCC"), row("CCC sold", T(13, 0), sym="CCC")]
    lines, counts = B.clean_rows(rows_in)
    clean = {"calendar": cal, "counts": dict(counts),
             "namedays": [{"day": d, "sym": s, "lines": ls} for (d, s), ls in sorted(lines.items())]}
    rows, c = B.run_score(mkt, clean, [prior, DAY])
    assert {r["group"] for r in rows} == {"S"} and len(rows) == 3
    assert all(r["half"] == "B" for r in rows)
    prim = [r for r in rows if r.get("primary")]
    assert prim, c
    for r in prim:
        assert r["primary"]["t"] >= r["a0"] and B.WIN_LO <= B.et_min(r["primary"]["t"]) <= B.WIN_HI
        if r["sym"] == "CCC":
            assert r["primary"]["t"] <= T(13, 0)
    res = B.summarize(rows, c, {"archive_rows": 4, "archive_sha256": "x", "script_rev": "t"})
    md = B.render_report(res)
    assert md.index("Power") < md.index("Results")
    assert res["groups"]["S"]["info"]["S_share_entry_spread_within_desk_cap"] is not None
