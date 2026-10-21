"""Sampling, timing, the pre-step inventory and the after-hours rule for order_flow_pilot. No network."""
from __future__ import annotations

import os
import pytest
import sys
from datetime import datetime
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import order_flow_pilot as O  # noqa: E402

E = O.E


def test_decision_ts_is_the_start_of_minute_m_et():
    t = O.decision_ts("2026-07-01", 10 * 60 + 5)
    assert datetime.fromtimestamp(t, E.ET).strftime("%Y-%m-%d %H:%M:%S") == "2026-07-01 10:05:00"


def test_pre_decisions_window_and_offset(monkeypatch):
    mins = list(range(9 * 60 + 50, 15 * 60 + 35))
    fire = {9 * 60 + 58, 10 * 60 + 30, 15 * 60 + 26, 15 * 60 + 29}
    monkeypatch.setattr(E, "pre_flag", lambda f, s, i: mins[i] in fire)
    ser = {"minutes": mins, "fast": [0.0] * len(mins), "slow": [0.0] * len(mins)}
    assert O.pre_decisions(ser) == [631, 927]          # 09:58 before the window, 15:29 after it


def test_draw_is_seeded_and_stratified():
    onsets = [(f"2026-06-{d:02d}", f"N{i}", 600 + i) for d in range(1, 29) for i in range(400)] + \
             [(f"2026-08-{d:02d}", f"N{i}", 600 + i) for d in range(1, 29) for i in range(400)]
    a = O.draw(onsets, n=1000)
    assert [e["half"] for e in a].count("A") == 500 and [e["half"] for e in a].count("B") == 500
    assert a == O.draw(list(reversed(onsets)), n=1000)
    small = O.draw(onsets[:10], n=1000)
    assert len(small) == 10                              # a half with fewer entries is taken whole


def test_inventory_counts_codes_sizes_and_bad_quotes():
    trades = [NS(tape="C", conditions=["@", "I"], size=50, exchange="Q"), NS(tape="C", conditions=None, size=0,
                                                                             exchange="V"),
              NS(tape="A", conditions=["@", "T"], size=100, exchange="N")]
    quotes = [NS(tape="C", bid_price=10.0, ask_price=10.01, bid_size=3, ask_size=2, conditions=["R"]),
              NS(tape="C", bid_price=10.0, ask_price=10.0, bid_size=1, ask_size=1, conditions=None),
              NS(tape="C", bid_price=10.02, ask_price=10.01, bid_size=0, ask_size=4, conditions=None),
              NS(tape="A", bid_price=0.0, ask_price=20.0, bid_size=None, ask_size=5, conditions=None)]
    inv = O.inventory(trades, quotes)
    c = inv["C"]
    assert c["trades"] == 2 and c["codes"] == {"@": 1, "I": 1} and c["no_conditions"] == 1 and c["size_ok"] == 1
    assert c["quotes"] == 3 and c["locked"] == 1 and c["crossed"] == 1 and c["zero_size"] == 1 and c["bid_size_ok"] == 2
    assert inv["A"]["zero_price"] == 1 and inv["A"]["codes"] == {"@": 1, "T": 1}
    m = O.merge([inv, inv])
    assert m["C"]["trades"] == 4 and m["C"]["codes"] == {"@": 2, "I": 2}


def _q(t, b=10.0, a=10.01, bs=1, as_=1):
    return NS(timestamp=t, bid_price=b, ask_price=a, bid_size=bs, ask_size=as_, tape="C", conditions=["R"])


def test_keep_list_is_exact():
    assert O.keep_trade(NS(conditions=["@", "I"])) and O.keep_trade(NS(conditions=[" ", "F"]))
    assert not O.keep_trade(NS(conditions=["@", "4"])) and not O.keep_trade(NS(conditions=["@", "T"]))
    assert not O.keep_trade(NS(conditions=None)) and not O.keep_trade(NS(conditions=["X"]))   # unseen -> drop


def test_coverage_ages_and_counts():
    T = 1000.0
    trades = [NS(timestamp=T - 50, conditions=["@"]), NS(timestamp=T - 10, conditions=["@", "I"]),
              NS(timestamp=T - 5, conditions=["@", "W"])]
    quotes = [_q(T - 115), _q(T - 20), _q(T - 2, b=10.01, a=10.01)]           # the last one is locked: ignored
    c = O.coverage(trades, quotes, T)
    assert c["kept"] == 2 and c["dropped_codes"] == {"W": 1}
    assert c["signable"] == 1                     # T-10 has the T-20 quote; T-50 only the T-115 one (65 s > 60 s)
    assert c["qi_quote"] and c["entry_quote"] and c["quotes"] == 2 and c["requests"] == 2
    c2 = O.coverage([NS(timestamp=T - 50, conditions=["@"])], [_q(T - 115)], T)
    assert c2["signable"] == 0 and not c2["qi_quote"]                            # 65 s > 60 s; 115 s > 30 s


T0 = 10_000.0


def _tick(trades, quotes):
    return {"trades": trades, "quotes": quotes}


def test_sign_trades_lee_ready_and_tick_test():
    qs = O.clean_quotes([(T0 - 70, 10.00, 10.02, 5, 5), (T0 - 30, 10.00, 10.02, 5, 5)])
    trades = [(T0 - 65, 10.02, 100, ["@"], "C"),       # above mid 10.01 -> +1
              (T0 - 50, 10.00, 50, ["@", "I"], "C"),   # below -> -1
              (T0 - 40, 10.01, 30, ["@"], "C"),        # at mid, up from 10.00 -> +1
              (T0 - 35, 10.01, 20, ["@"], "C"),        # at mid, unchanged -> previous sign +1
              (T0 - 20, 10.03, 10, ["@", "4"], "C"),   # dropped by the keep-list
              (T0 - 61, 10.02, 999, ["@"], "C"),       # outside [T - 60, T)
              (T0, 10.02, 999, ["@"], "C")]            # at T: excluded
    assert O.sign_trades(trades, qs, T0) == [(50, -1), (30, 1), (20, 1)]   # T0 - 65 is outside the 60 s window


def test_unsignable_when_the_quote_is_stale():
    qs = O.clean_quotes([(T0 - 119, 10.0, 10.02, 5, 5)])
    assert O.sign_trades([(T0 - 50, 10.02, 10, ["@"], "C")], qs, T0) == []       # 69 s old > 60 s


def test_features_ofi_qi_micro_and_entry_quote():
    qs = [(T0 - 55, 10.00, 10.02, 8, 8), (T0 - 30, 10.00, 10.02, 30, 10), (T0 - 10, 10.00, 10.02, 30, 10),
          (T0 - 5, 10.01, 10.01, 1, 1)]
    trades = [(T0 - 50 + i, 10.02 if i % 4 else 10.00, 10, ["@"], "C") for i in range(8)]
    f = O.features(_tick(trades, qs), T0)
    assert f["n_signed"] == 8 and f["ofi60"] == pytest.approx((6 - 2) / 8)
    assert f["qi"] == pytest.approx((30 - 10) / 40)                               # the locked quote is ignored
    assert f["micro"] == pytest.approx(((10.00 * 10 + 10.02 * 30) / 40 - 10.01) / 10.01)
    assert f["entry_quote"]
    few = O.features(_tick(trades[:4], qs), T0)
    assert few["ofi60"] is None                                                   # < 5 signed trades
    stale = O.features(_tick(trades, [(T0 - 45, 10.0, 10.02, 3, 3)]), T0)
    assert stale["qi"] is None and not stale["entry_quote"]                      # > 30 s old at T and T + 1


def test_cuts_groups_and_g3():
    assert O.top_cut([1, 2, 3, 4, 5, 6]) == (5, 0.0) and O.top_cut([1, None]) is None
    cuts = {"ofi60": 0.5, "qi": 0.2}
    assert O.groups({"ofi60": 0.6, "qi": 0.3}, cuts) == {"G1": "IN", "G2": "IN", "G3": "IN"}
    assert O.groups({"ofi60": 0.6, "qi": 0.1}, cuts) == {"G1": "IN", "G2": "OUT", "G3": "OUT"}
    assert O.groups({"ofi60": None, "qi": 0.3}, cuts) == {"G1": None, "G2": "IN", "G3": None}


def test_control_return_is_seeded_later_three_bars():
    sip = {k: (10.0, 10.0 + 0.01 * (k % 7)) for k in range(570, 960)}
    r = O.control_return("AAA", "2026-06-01", 600, sip)
    assert r == O.control_return("AAA", "2026-06-01", 600, sip)
    assert O.control_return("AAA", "2026-06-01", 958, sip) is None


def test_count_step_floors_and_mde():
    import random as _r
    rng = _r.Random(3)
    rows = []
    for d in range(25):
        for i in range(60):
            g = "IN" if i < 20 else "OUT"
            rows.append({"day": f"2026-06-{d + 1:02d}", "half": "A", "ctrl": rng.gauss(0, 0.002),
                         "groups": {"G1": g, "G2": g, "G3": g}})
    c = O.count_step(rows)["G1"]["A"]
    assert c["n_in"] == 500 and c["days"] == 25 and c["projected_mde"] > 0 and not c["floors_met"]   # 500 < 1,000
    assert O.count_step(rows)["G3"]["A"]["floors_met"]                                                # G3 floor 300


class _FakeClient:
    def __init__(self, fail_every=0):
        self.n, self.fail_every = 0, fail_every

    def get_stock_trades(self, req):
        self.n += 1
        if self.fail_every and self.n % self.fail_every == 0:
            raise RuntimeError("boom")
        return NS(data={req.symbol_or_symbols: [NS(timestamp=datetime.fromtimestamp(1, E.ET), price=10.0, size=1,
                                                   conditions=["@"], tape="C")]})

    def get_stock_quotes(self, req):
        return NS(data={req.symbol_or_symbols: []})


def _entries(n):
    return [{"day": "2026-06-01", "sym": f"N{i}", "m": 600, "half": "A"} for i in range(n)]


def test_fetch_features_caches_and_resumes(tmp_path):
    cl = _FakeClient()
    st = O.fetch_features(_entries(5), cl, str(tmp_path), sleep=lambda s: None, log=lambda x: None)
    assert st["fetched"] == 5 and len(list(tmp_path.glob("*.pkl"))) == 5
    st2 = O.fetch_features(_entries(7), cl, str(tmp_path), sleep=lambda s: None, log=lambda x: None)
    assert st2["cached"] == 5 and st2["fetched"] == 2


def test_fetch_features_aborts_above_two_percent(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "FAIL_MIN_ATTEMPTS", 20)
    cl = _FakeClient(fail_every=1)                       # every trades call fails -> fetch_entry raises
    st = O.fetch_features(_entries(50), cl, str(tmp_path), sleep=lambda s: None, log=lambda x: None)
    assert st["aborted"] == 1 and st["attempted"] == 20


def test_tie_break_gives_an_exact_third_with_an_atom():
    rows = [{"sym": f"N{i}", "day": "2026-06-01", "m": 600 + i % 300, "qi": 0.0 if i % 4 else 0.5} for i in range(3000)]
    cut = O.top_cut([(r["qi"], O.tie_u(r)) for r in rows])
    ins = sum(1 for r in rows if O.groups({**r, "ofi60": None}, {"ofi60": None, "qi": cut})["G2"] == "IN")
    assert ins == 1000                                  # a third exactly, although 75% of values are 0.0


def test_sorting_is_by_time_only_and_stable():
    qs = O.clean_quotes([(5.0, 10.02, 10.04, 1, 1), (5.0, 10.00, 10.02, 1, 1)])
    assert [q[1] for q in qs] == [10.02, 10.00]         # same timestamp: delivery order kept
    assert O.last_quote(qs, 5.0, 30)[1] == 10.00        # the last delivered quote, not the highest bid


def test_capped_flags_the_row_limit():
    assert O.capped({"trades": [0] * O.ROW_CAP, "quotes": []}) and not O.capped({"trades": [], "quotes": [0] * 9999})


def test_module_shas_cover_the_feature_code():
    sh = O.module_shas()
    assert {"order_flow_pilot", "indicator_confirm_hist", "early_entry_honest", "signals"} <= set(sh)


def test_outcomes_for_entry_exit_and_horizons():
    T = T0
    qs = [(T - 10, 10.00, 10.02, 5, 5),          # entry quote (<= T + 1, 11 s old)
          (T + 1 + 60, 10.05, 10.07, 5, 5), (T + 1 + 180, 10.10, 10.12, 5, 5), (T + 1 + 250, 10.20, 10.20, 5, 5)]
    o = O.outcomes_for(qs, T)
    assert o["entry"] and o["spread"] == pytest.approx(0.02 / 10.01)
    assert o["gross60"] == pytest.approx(10.06 / 10.01 - 1) and o["net60"] == pytest.approx(10.05 / 10.02 - 1)
    assert o["gross180"] == pytest.approx(10.11 / 10.01 - 1)
    assert o["gross120"] is None                     # the last quote by T+121 (at T+61) is 60 s old > 30 s
    assert o["gross300"] is None                     # T+251 is locked (dropped); T+181 is 120 s old
    assert o["mfe_bid"] == pytest.approx(10.10 / 10.02 - 1)
    assert not O.outcomes_for([(T - 40, 10.0, 10.02, 5, 5)], T)["entry"]   # entry quote 41 s old


def _row(day, sym, half, side, gross, net=0.0001):
    return {"day": day, "sym": sym, "half": half, "groups": {"G1": side, "G2": side, "G3": side},
            "gross180": gross, "net180": net}


def test_gated_minus_rest_and_verdicts():
    import random as _r
    rng = _r.Random(5)
    rows = []
    for d in range(25):
        day = f"2026-06-{d + 1:02d}"
        for i in range(60):
            side = "IN" if i < 20 else "OUT"
            rows.append(_row(day, f"N{i}", "A", side, (0.0012 if side == "IN" else 0.0) + rng.gauss(0, 0.0005)))
    r = O.grade_group(rows, "G3", "A")
    assert r["diff"] == pytest.approx(0.0012, abs=0.0002) and r["days"] == 25 and r["n_in"] == 500
    assert r["drop_names"] is not None and r["drop_days"] is not None
    assert O.half_verdict(r, "G3") == "PASS"                           # floors: G3 300
    assert O.half_verdict(r, "G1") == "UNDERPOWERED"                   # G1 needs 1,000 IN
    assert O.group_verdict(["PASS", "PASS"], [0.002, 0.002]) == "PASS"
    assert O.group_verdict(["FAIL", "PASS"], [0.002, 0.002]) == "FAIL"
    assert O.group_verdict(["NOT_PASSED", "NOT_PASSED"], [0.0003, 0.0004]) == "FAIL"
    assert O.group_verdict(["NOT_PASSED", "NOT_PASSED"], [0.0003, 0.0007]) == "UNDERPOWERED"


def test_fetch_outcomes_caches_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "fetch_outcome_quotes", lambda cl, sym, T, sleep=None: [(T, 10.0, 10.02, 1, 1)])
    st = O.fetch_outcomes(_entries(4), object(), str(tmp_path), sleep=lambda s: None, log=lambda x: None)
    assert st["fetched"] == 4
    st2 = O.fetch_outcomes(_entries(6), object(), str(tmp_path), sleep=lambda s: None, log=lambda x: None)
    assert st2["cached"] == 4 and st2["fetched"] == 2


def test_ub_when_the_difference_is_exactly_zero():
    rows = []
    for d in range(5):
        for i in range(6):
            side = "IN" if i < 3 else "OUT"
            g = 0.001 * ((d % 2) * 2 - 1) if side == "IN" else 0.0
            rows.append(_row(f"2026-06-{d + 1:02d}", f"N{i}", "A", side, g))
    rows += [_row("2026-06-09", f"N{i}", "A", "IN" if i < 3 else "OUT", 0.0) for i in range(6)]
    r = O.gated_minus_rest(rows, "G1", "A", "gross180")
    assert r["ub"] is not None                                      # computed from the SE, not mu / t


def test_tercile_cuts_and_information_runs():
    assert O.tercile_cuts([1, 2, 3, 4, 5, 6]) == (3, 5) and O.tercile_cuts([1]) is None
    import random as _r
    rng = _r.Random(2)
    rows, feats = [], {"entries": []}
    for d in range(4):
        for i in range(30):
            e = {"day": f"2026-06-{d + 1:02d}", "sym": f"N{i}", "m": 600 + 30 * (i % 10), "half": "A",
                 "ofi60": rng.random(), "qi": rng.random() - 0.5, "micro": rng.gauss(0, 1e-4),
                 "groups": {"G1": "IN" if i % 3 == 0 else "OUT", "G2": "OUT", "G3": "OUT"}}
            feats["entries"].append(e)
            rows.append({**e, "gross60": 0.0, "gross120": 0.0, "gross180": rng.gauss(0, 1e-3), "gross300": 0.0,
                         "net180": -0.0003, "mfe_bid": 0.001, "spread": 0.0003})
    onset = {O.entry_key(r): rng.gauss(0, 1e-3) for r in rows}
    info = O.information(rows, feats, onset)
    assert "terciles cut within each hour" in info and "G1 within onset-bar-return terciles" in info
    assert set(info["G1 within onset-bar-return terciles"]) >= {"cuts (half A)", "onset return low", "onset return high"}
