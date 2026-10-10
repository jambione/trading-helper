"""replay_session --universe (synthetic day) and synth_day_fidelity: the pure parts."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "studies"))
import replay_session as rp  # noqa: E402
import synth_day_fidelity as sf  # noqa: E402

DAY = "2026-10-08"


def test_held_out_days_are_refused_without_the_flag():
    assert rp.held_out_refusal("2026-09-23", "u.json", False)
    assert rp.held_out_refusal("2026-03-02", "u.json", False)
    assert rp.held_out_refusal("2026-09-23", "u.json", True) is None
    assert rp.held_out_refusal("2026-09-24", "u.json", False) is None
    assert rp.held_out_refusal("2026-09-23", None, False) is None      # recorded replay: not this guard


def test_fidelity_script_refuses_held_out_days():
    import pytest
    with pytest.raises(SystemExit):
        sf.check_days(["2026-10-01", "2026-09-23"])
    sf.check_days(["2026-09-24", "2026-10-09"])


def test_absolutize_argv(tmp_path):
    out = rp.absolutize_argv(["--day", DAY, "--universe", "u.json", "--out=/abs/r.json", "--set", "a=b"],
                             cwd=tmp_path)
    assert out[3] == str(tmp_path / "u.json")
    assert out[4] == "--out=/abs/r.json" and out[5:] == ["--set", "a=b"]


def test_load_universe_keeps_earliest_and_parses_et(tmp_path):
    p = tmp_path / "u.json"
    p.write_text(json.dumps({"rows": [
        {"symbol": "abc", "first_ts": rp.at(DAY, "10:00"), "source": "Movers", "rvol": 2.0},
        {"symbol": "ABC", "first_ts": rp.at(DAY, "09:45"), "source": "tight"},
        {"symbol": "XYZ", "first_ts": "09:31:30"},
        {"symbol": "", "first_ts": 1.0},
        {"symbol": "NOTS"},
    ]}))
    u = rp.load_universe(p, DAY)
    assert [r["symbol"] for r in u] == ["XYZ", "ABC"]
    assert u[0]["first_ts"] == rp.at(DAY, "09:31") + 30 and u[0]["source"] == "movers"
    assert u[1]["source"] == "tight" and u[1]["first_ts"] == rp.at(DAY, "09:45")


def test_still_offered_source_rules():
    cfg = {"ai_watch_movers_min_pct_change": 1.5, "ai_watch_movers_min_price": 10.0,
           "ai_tight_min_pct_change": 0.5, "ai_watch_min_pct_change": 0.0}
    t0 = 1000.0
    mv = {"symbol": "A", "first_ts": t0, "source": "movers"}
    assert not rp.still_offered(mv, t0 - 1, 20.0, 5.0, cfg)          # not yet
    assert rp.still_offered(mv, t0, 20.0, 5.0, cfg)
    assert not rp.still_offered(mv, t0 + 60, 20.0, 1.0, cfg)          # faded under the movers floor
    assert not rp.still_offered(mv, t0 + 60, 9.0, 5.0, cfg)           # under the price floor
    tg = {"symbol": "B", "first_ts": t0, "source": "tight"}
    assert rp.still_offered(tg, t0, 50.0, 0.6, cfg) and not rp.still_offered(tg, t0, 50.0, 0.4, cfg)
    mo = {"symbol": "C", "first_ts": t0, "source": "momentum"}
    assert rp.still_offered(mo, t0, 12.0, 0.1, cfg) and not rp.still_offered(mo, t0, 12.0, -0.1, cfg)
    assert not rp.still_offered(mo, t0, None, None, cfg)              # no print yet: no quote, no offer
    rs = {"symbol": "D", "first_ts": t0, "source": "agy"}
    assert rp.still_offered(rs, t0 + 9999, None, None, cfg)
    assert not rp.still_offered(dict(rs, last_ts=t0 + 10), t0 + 11, 5.0, 1.0, cfg)
    iv = dict(mv, intervals=[[t0, t0 + 100], [t0 + 500, t0 + 600]])
    assert rp.still_offered(iv, t0 + 550, 20.0, 0.0, cfg)             # intervals win over the pct rule
    assert not rp.still_offered(iv, t0 + 300, 20.0, 50.0, cfg)


def test_thin_prints_keeps_first_and_last_of_each_second():
    ts, px = rp.thin_prints([(10.1, 1.0), (10.5, 1.1), (10.9, 1.2), (11.2, 2.0), (13.0, 3.0), (13.4, 3.1)])
    assert ts == [10.1, 10.9, 11.2, 13.0, 13.4] and px == [1.0, 1.2, 2.0, 3.0, 3.1]
    assert rp.thin_prints([]) == ([], [])


def test_tape_last_at_and_next_after_are_one_event():
    tape = rp.TradeTape({"abc": ([10.0, 12.5, 80.0], [1.0, 1.1, 1.3])})
    assert tape.last_at("ABC", 9.9) is None
    assert tape.last_at("ABC", 12.5) == (1.1, 12.5)            # at the print counts as seen
    assert tape.next_after("ABC", 12.5) == (1.3, 80.0)          # strictly after the decision
    assert tape.next_after("ABC", 12.5, within=60.0) is None    # 67.5 s later: no fill
    assert tape.next_after("ABC", 10.0, within=60.0) == (1.1, 12.5)
    assert tape.next_after("NOPE", 0.0) is None


def test_fetch_sip_trades_paginates_and_drops_non_last_sale():
    pages = [
        {"trades": [{"t": "2026-10-08T13:30:00.5Z", "p": 10.0, "c": ["@"]},
                    {"t": "2026-10-08T13:30:01.123456789Z", "p": 99.0, "c": ["@", "Z"]}],
         "next_page_token": "x"},
        {"trades": [{"t": "2026-10-08T13:30:02Z", "p": 10.2, "c": ["@", "I"]}], "next_page_token": None},
    ]
    calls = []

    def get(url, params):
        calls.append(params.get("page_token"))
        return pages[len(calls) - 1]

    out = rp.fetch_sip_trades("ABC", 0.0, 1.0, get)
    assert calls == [None, "x"]
    assert [p for _t, p in out] == [10.0, 10.2]                # sold-out-of-sequence dropped, odd lot kept
    assert abs(out[0][0] - (rp.at(DAY, "09:30") + 0.5)) < 1e-6


def _synth_day(universe, prints, closes=None):
    class _Eng:
        def state(self, sym, t):
            return {"pctr": -40.0, "price": 0.0}
    bars = {s: {"d": [("2026-10-07", c, 1e6)], "m": [(rp.at(DAY, "09:30"), c, 5000.0)], "iex": []}
            for s, c in (closes or {}).items()}
    sd = rp.SynthDay(DAY, universe, rp.TradeTape(prints), bars, _Eng(),
                     [(0.0, {"x": 1}), (rp.at(DAY, "12:00"), {"x": 2})], 2000.0)
    return sd


def test_synth_day_dashboard_rows_price_and_clock_from_one_print():
    t0 = rp.at(DAY, "09:40")
    sd = _synth_day([{"symbol": "ABC", "first_ts": t0, "source": "movers", "rvol": 3.0},
                     {"symbol": "LATE", "first_ts": t0 + 3600, "source": "movers"}],
                    {"ABC": ([t0 - 5, t0 + 7], [10.5, 10.6]), "LATE": ([t0], [5.0])},
                    {"ABC": 10.0, "LATE": 5.0})
    st = sd.dashboard(t0 + 10)
    assert [r["ticker"] for r in st["tickers"]] == ["ABC"]
    r = st["tickers"][0]
    assert (r["price"], r["price_age_sec"]) == (10.6, 3.0)
    sp = r["signal_proximity"]
    assert (sp["rt_price"], sp["rt_price_age_sec"], sp["pctr"]) == (10.6, 3.0, -40.0)
    assert abs(r["pct_change"] - 6.0) < 1e-9
    assert st["ai_positions"]["account"]["equity"] == 2000.0


def test_synth_day_candidates_follow_the_seed_rule_and_config_stream():
    t0 = rp.at(DAY, "09:40")
    sd = _synth_day([{"symbol": "ABC", "first_ts": t0, "source": "movers", "rvol": 3.0, "spread_pct": 0.02}],
                    {"ABC": ([t0 - 5, t0 + 600], [10.5, 10.05])}, {"ABC": 10.0})
    cfg = {"ai_watch_movers_min_pct_change": 1.5}
    rows = sd.candidates(t0 + 10, cfg)
    assert len(rows) == 1 and rows[0]["source"] == "movers" and rows[0]["spread_pct"] == 0.02
    assert rows[0]["price"] == 10.5 and rows[0]["rvol"] == 3.0 and rows[0]["agreement"] is True
    assert sd.candidates(t0 + 700, cfg) == []                 # +0.5% now: under the movers floor
    sd.advance(rp.at(DAY, "09:30"))
    assert sd.get("config/bot_config.json")[1] == {"x": 1}
    sd.changed.clear()
    sd.advance(rp.at(DAY, "12:00"))
    assert sd.get("config/bot_config.json")[1] == {"x": 2} and "config/bot_config.json" in sd.changed


def test_fake_broker_exit_fills_at_next_print_when_synthetic():
    b = rp.FakeBroker(hold_sec=60, max_pos=5, equity=1e5)
    tape = rp.TradeTape({"ABC": ([100.0, 170.0], [10.0, 10.4])})
    b.fill_fn = lambda s, t: tape.next_after(s, t)
    b.enter("ABC", 10.0, 100.0, {})
    b.exits_due(165.0, {"tickers": [{"ticker": "ABC", "price": 10.2}]})
    c = b.closed[0]
    assert (c["exit_px"], c["exit_ts"], c["exit_decision_ts"]) == (10.4, 170.0, 165.0)
    assert abs(c["ret"] - 0.04) < 1e-9


def test_fake_broker_recorded_exit_unchanged():
    b = rp.FakeBroker(hold_sec=60, max_pos=5, equity=1e5)
    b.enter("ABC", 10.0, 100.0, {})
    b.exits_due(165.0, {"tickers": [{"ticker": "ABC", "price": 10.2}]})
    c = b.closed[0]
    assert (c["exit_px"], c["exit_ts"]) == (10.2, 165.0) and "exit_decision_ts" not in c


def test_arm_tally_counts_wr_refusals():
    t = rp.ArmTally()
    t.add([{"s": "A", "b": "wr_not_trending"}, {"s": "B", "b": "not_presquare"}])
    t.add([{"s": "A", "b": "wr_rsi_no_bars"}])
    s = t.summary()
    assert s["arm_polls"] == 2 and s["arm_checks"] == 3
    assert s["wr_refusals"]["wr_not_trending"] == 1 and s["wr_refusals"]["wr_rsi_no_bars"] == 1
    assert s["not_presquare"] == 1


def test_recorded_universe_first_admission_per_symbol():
    rows = [{"day": DAY, "symbol": "abc", "ts": 200.0, "source": "movers", "price": 11.0},
            {"day": DAY, "symbol": "ABC", "ts": 100.0, "source": "tight", "price": 10.0, "rvol": 1.2},
            {"day": "2026-10-07", "symbol": "XYZ", "ts": 50.0, "source": "movers"},
            {"day": DAY, "symbol": "Q", "ts": None}]
    u = sf.recorded_universe(rows, DAY)
    assert u == [{"symbol": "ABC", "first_ts": 100.0, "source": "tight", "raw_price": 10.0,
                  "pct_change": None, "rvol": 1.2}]


def test_candidate_intervals_merge_sources_and_close_at_end():
    rows = [{"ts": 10.0, "symbol": "A", "source": "movers", "event": "enter"},
            {"ts": 15.0, "symbol": "A", "source": "momentum", "event": "enter"},
            {"ts": 20.0, "symbol": "A", "source": "movers", "event": "leave"},
            {"ts": 25.0, "symbol": "A", "source": "momentum", "event": "leave"},
            {"ts": 40.0, "symbol": "A", "source": "movers", "event": "enter"},
            {"ts": 5.0, "event": "pool_note"}]
    assert sf.candidate_intervals(rows, 100.0) == {"A": [[10.0, 25.0], [40.0, 100.0]]}


def test_match_share_one_to_one_within_90s():
    a = [{"symbol": "A", "t": 100.0}, {"symbol": "A", "t": 130.0}, {"symbol": "B", "t": 100.0}]
    b = [{"symbol": "A", "t": 150.0}, {"symbol": "B", "t": 300.0}]
    assert sf.match_share(a, b) == (1, 3)
    assert sf.match_share(b, a) == (1, 2)


def test_gate_verdict():
    good = {"pooled_diff": 1.0, "day_diffs": [1.0] * 9 + [10.0] * 3, "recall": 0.7}
    v = sf.gate_verdict({"base": good}, {"diff": -1.2})
    assert v["base"]["pass"] and v["PASS"]
    v = sf.gate_verdict({"base": dict(good, day_diffs=[1.0] * 8 + [10.0] * 4)}, {"diff": 0.0})
    assert not v["base"]["days_within_ok"] and not v["PASS"]
    v = sf.gate_verdict({"base": good}, {"diff": 2.0})
    assert not v["paired"]["pass"] and not v["PASS"]
