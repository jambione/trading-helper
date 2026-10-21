"""Sampling, timing, the pre-step inventory and the after-hours rule for order_flow_pilot. No network."""
from __future__ import annotations

import os
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
