"""hist_universe: pure rule logic + point-in-time guarantees (no network)."""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import hist_universe as HU  # noqa: E402

DAY = "2026-09-15"
I930 = HU.I_OPEN


def cfg(**kw):
    c = {
        "ai_movers_top": 50, "ai_movers_min_pct_change": 1.5, "ai_movers_min_price": 2.0,
        "ai_movers_max_price": 100.0, "ai_movers_min_dollar_vol": 1_000_000, "ai_movers_max_rows": 25,
        "ai_movers_session_max": 40, "ai_movers_session_append": True, "ai_movers_use_most_actives": True,
        "ai_movers_actives_top": 100, "ai_movers_universe_scan": False, "ai_movers_sip_delay_min": 15.0,
        "rvol_time_adjusted": True, "ai_watch_seed_movers": True, "ai_watch_seed_movers_n": 25,
        "ai_watch_movers_min_pct_change": 1.5, "ai_watch_movers_min_price": 10.0,
        "ai_watch_movers_min_dollar_volume": 2_000_000, "ai_watch_movers_min_rvol": 1.0,
        "ai_watch_hot_move_rvol_waive_pct": 20.0, "ai_watch_min_price": 10.0, "ai_max_price": 100.0,
        "ai_watch_soft_seed_enabled": False, "ai_watch_seed_tight": False,
        "ai_watch_admit_arm_gates": True, "ai_watch_max_sip_spread_pct": 0.05,
        "ai_watch_gap_down_block_pct": 1.0, "ai_movers_min_live_pct": 0.0,
    }
    c.update(kw)
    return c


# ── pure selectors ──

def test_gainers_rank_over_every_symbol_then_filter():
    pcts = {"AAAAW": 90.0, "BIG": 40.0, "MID": 12.0, "CHEAP": 50.0, "FLAT": 0.5}
    prices = {"AAAAW": 0.4, "BIG": 25.0, "MID": 30.0, "CHEAP": 1.0, "FLAT": 20.0}
    # top 3 = AAAAW, CHEAP, BIG: the warrant and the penny take ranks, then fail the filter
    got = HU.gainers_select(pcts, prices, top=3, min_pct=1.5, lo=2.0, hi=100.0, ok=HU.is_common)
    assert [g[0] for g in got] == ["BIG"]
    got = HU.gainers_select(pcts, prices, top=5, min_pct=1.5, lo=2.0, hi=100.0, ok=HU.is_common)
    assert [g[0] for g in got] == ["BIG", "MID"]


def test_actives_rank_includes_etfs_then_filter():
    vol = {"SOXL": 9e8, "SPY": 8e8, "F": 7e8, "PLTR": 6e8}
    assert HU.actives_select(vol, top=3, ok=lambda s: s not in ("SOXL",)) == ["SPY", "F"]


def test_measure_row_unpriced_uses_prior_bars_and_regates():
    c = cfg()
    prior = [(f"d{k}", 20.0, 1e6) for k in range(25)]
    seq = HU.daily_view(prior + [("y", 21.0, 1e6)], None)        # premarket: latest bar = yesterday
    r = HU.measure_row("ABC", None, None, seq, today_bar=False, mins_open=-200, cfg=c)
    assert r and abs(r["pct_change"] - 5.0) < 1e-9 and r["price"] == 21.0
    assert abs(r["rvol"] - 1.0) < 1e-9                         # not time-adjusted (yesterday's bar)
    seq2 = HU.daily_view(prior + [("y", 20.1, 1e6)], None)       # +0.5% < floor: discarded from session
    assert HU.measure_row("ABC", None, None, seq2, today_bar=False, mins_open=-200, cfg=c) == {"_discard": True}
    thin = HU.daily_view([(f"d{k}", 20.0, 100.0) for k in range(25)] + [("y", 21.0, 100.0)], None)
    assert HU.measure_row("ABC", None, None, thin, today_bar=False, mins_open=-200, cfg=c) is None


def test_movers_seed_gates():
    c = cfg(ai_watch_seed_movers_n=2)
    rows = [
        {"symbol": "LOW", "price": 8.0, "pct_change": 5.0, "rvol": 2.0, "dollar_volume": 5e6},
        {"symbol": "THIN", "price": 20.0, "pct_change": 5.0, "rvol": 0.5, "dollar_volume": 5e6},
        {"symbol": "HOT", "price": 20.0, "pct_change": 25.0, "rvol": 0.5, "dollar_volume": 5e6},
        {"symbol": "SMALL", "price": 20.0, "pct_change": 5.0, "rvol": 2.0, "dollar_volume": 1e6},
        {"symbol": "OK", "price": 20.0, "pct_change": 5.0, "rvol": None, "dollar_volume": 5e6},
        {"symbol": "LATE", "price": 20.0, "pct_change": 5.0, "rvol": 3.0, "dollar_volume": 5e6},
    ]
    got = [r["symbol"] for r in HU.movers_seed(rows, c, max_price=100.0)]
    assert got == ["HOT", "OK"]          # hot move waives thin rvol; unknown rvol abstains; n cap


def test_tight_seed_requires_known_tight_spread():
    c = cfg(ai_tight_min_pct_change=0.5, ai_tight_max_spread_pct=0.05, ai_tight_min_price=10.0,
            ai_tight_max_price=400.0, ai_watch_seed_tight_n=8)
    rows = [{"symbol": "A", "price": 150.0, "pct_change": 1.0, "spread_pct": None, "dollar_volume": 9e6},
            {"symbol": "B", "price": 150.0, "pct_change": 1.0, "spread_pct": 0.02, "dollar_volume": 9e6},
            {"symbol": "C", "price": 450.0, "pct_change": 1.0, "spread_pct": 0.02, "dollar_volume": 9e6}]
    assert [r["symbol"] for r in HU.tight_seed(rows, c)] == ["B"]


def test_door_gates_band_spread_timing_and_gap():
    c = cfg()
    row = {"symbol": "X", "source": "movers", "price": 20.0}
    assert HU.door_gates({**row, "price": 9.99}, c, i=I930 + 30, spread=None, gap=None) == "below_min_price"
    assert HU.door_gates({**row, "price": 100.0}, c, i=I930 + 30, spread=None, gap=None) == "above_max_price"
    # spread sits out until 09:30 + delay + 1
    assert HU.door_gates(row, c, i=I930 + 15, spread=0.5, gap=None) is None
    assert HU.door_gates(row, c, i=I930 + 16, spread=0.5, gap=None) == "spread_wide"
    assert HU.door_gates(row, c, i=I930 + 16, spread=None, gap=None) is None       # unknown abstains
    assert HU.door_gates(row, c, i=I930 + 16, spread=0.04, gap=-1.2) == "gapped_down"
    tight = {"symbol": "T", "source": "tight", "price": 250.0}
    assert HU.door_gates(tight, {**c, "ai_tight_max_price": 400.0}, i=I930 + 60, spread=0.01, gap=0.0) is None


# ── a synthetic market ──

def _market(sip, iex=None, prior_close=20.0, names=None, spread=0.01):
    syms = sorted(set(sip) | set(iex or {}))
    prior = {s: [(f"2026-08-{k + 10:02d}", prior_close, prior_close, 2e6) for k in range(24)] for s in syms}
    calls = []

    def sfn(ss, end_ts):
        calls.append((tuple(ss), end_ts))
        return {s: spread for s in ss}
    mk = HU.DayMarket(DAY, syms, names or {}, sip, iex or {}, prior, prior, spread_fn=sfn)
    mk._calls = calls
    return mk


def _climb(start_i, n, px0, px1, vol):
    return [(start_i + k, px0, px0 + (px1 - px0) * k / max(1, n - 1), vol) for k in range(n)]


def test_accessors_never_read_past_decision_minute():
    sip = {"ABC": [(I930 + 5, 20, 22.0, 1000)]}
    mk = _market(sip)
    assert mk.rt_price("ABC", I930 + 5) is None            # bar I930+5 closes at I930+6
    assert mk.rt_price("ABC", I930 + 6) == 22.0
    assert mk.delayed_today("ABC", I930 + 6) is None       # 15-min-late SIP daily bar
    assert mk.delayed_today("ABC", I930 + 5 + HU.SIP_DELAY - 1) is None
    assert mk.delayed_today("ABC", I930 + 5 + HU.SIP_DELAY) == (22.0, 1000.0)


def test_premarket_gap_is_yesterdays_iex_open_gap():
    sip = {"ABC": [(I930, 18.0, 18.0, 1000)]}
    mk = _market(sip)
    mk.iex_daily["ABC"] = mk.iex_daily["ABC"][:-1] + [("2026-09-14", 19.0, 20.0, 1e5)]
    # premarket: yesterday's open 19 vs the close before it (20) = -5%
    assert abs(mk.gap("ABC", 100) - (-5.0)) < 1e-9
    # after 09:46: SIP 09:30 open vs prior SIP close 20 -> -10%
    assert abs(mk.gap("ABC", I930 + 16) - (-10.0)) < 1e-9


def test_simulation_admits_a_mover_and_has_no_lookahead():
    big = _climb(I930, 120, 20.0, 24.0, 50_000)       # +20% over two hours, $1M+/min
    sip = {"RUNR": big, "DULL": _climb(I930, 120, 20.0, 20.0, 50_000)}
    c = cfg()
    res = HU.simulate_day(_market(sip), lambda i: c, {"soft_seed": False}, end_i=I930 + 120)
    adm = {a["symbol"]: a for a in res["admits"]}
    assert "RUNR" in adm and "DULL" not in adm
    first = adm["RUNR"]["first_et"]
    t_first = adm["RUNR"]["first_ts"]
    # Destroy everything after the admission minute: the first admission must not move.
    i_first = int(round((t_first - HU.et_ts(DAY, 0)) / 60))
    cut = {s: [b for b in bars if b[0] < i_first] + [(j, 1.0, 1.0, 1.0) for j in range(i_first, I930 + 120)]
           for s, bars in sip.items()}
    res2 = HU.simulate_day(_market(cut), lambda i: c, {"soft_seed": False}, end_i=I930 + 120)
    adm2 = {a["symbol"]: a for a in res2["admits"]}
    assert adm2["RUNR"]["first_et"] == first
    assert adm2["RUNR"]["raw_price"] == adm["RUNR"]["raw_price"]


def test_wide_spread_refuses_then_spread_fetch_is_16_min_back():
    # flat until 10:00 (spread gate live from 09:46), then +20%
    sip = {"RUNR": _climb(I930, 30, 20.0, 20.0, 50_000) + _climb(I930 + 30, 90, 20.0, 24.0, 50_000)}
    c = cfg()
    mk = _market(sip, spread=0.30)
    res = HU.simulate_day(mk, lambda i: c, {"soft_seed": False}, end_i=I930 + 120)
    assert not res["admits"] and res["refusals"].get("spread_wide")
    T, end = None, mk._calls[0][1]
    for i in range(I930, I930 + 120):
        if abs(HU.et_ts(DAY, i) - HU.SIP_DELAY * 60 - end) < 1e-6:
            T = i
    assert T is not None and T >= I930 + 16


def test_score_day_window_and_traded():
    rec = {(DAY, "A"): {"ts": 1000.0, "source": "movers"}, (DAY, "B"): {"ts": 1000.0, "source": "momentum"},
           (DAY, "C"): {"ts": 1000.0, "source": "trending"}, (DAY, "D"): {"ts": 1000.0, "source": "tight"}}
    admits = [{"symbol": "A", "first_ts": 1000.0 + 9 * 60, "source": "movers"},
              {"symbol": "B", "first_ts": 1000.0 - 5 * 60, "source": "movers"},
              {"symbol": "D", "first_ts": 1000.0 + 11 * 60, "source": "tight"},
              {"symbol": "Z", "first_ts": 1000.0, "source": "movers"}]
    trades = {(DAY, "A"): {"ts": 2000.0}, (DAY, "D"): {"ts": 2000.0}}
    r = HU.score_day(DAY, admits, rec, trades)
    assert r["prereg_n"] == 3 and r["prereg_win"] == 2          # A, B in window; D 11 min late
    assert r["covered_n"] == 2 and r["covered_win"] == 1
    assert r["traded_n"] == 2 and r["traded_win"] == 1 and r["traded_missed"] == ["D"]
    assert r["prec_any"] == 3 and r["prec_win"] == 2
    assert r["per_source"]["tight"]["late"] == 1
    assert math.isclose(r["uncovered_share"], 0.5)
