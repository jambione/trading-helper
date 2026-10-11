"""replay_session --universe (synthetic day) and synth_day_fidelity: the pure parts."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools" / "studies"))
import replay_session as rp  # noqa: E402
import synth_day_fidelity as sf  # noqa: E402

DAY = "2026-10-08"


def test_place_does_not_shadow_the_synthetic_day_flag():
    """place() reads the closed-over synthetic-day flag. Assigning to that name makes it
    local and every entry raises UnboundLocalError (0 trades on 2026-09-28..10-02)."""
    import inspect
    src = inspect.getsource(rp.run_inside)
    body = src[src.index("def place("):src.index("cp.place_scaled_entry = place")]
    assert "if synthetic:" in body
    assert "synthetic =" not in body


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


def test_load_universe_reads_admits_and_refuses_an_empty_dict(tmp_path):
    import pytest
    p = tmp_path / "h.json"
    p.write_text(json.dumps({"admits": [
        {"symbol": "qqq", "first_ts": rp.at(DAY, "10:05"), "source": "tight"},
    ], "spy_missing_share": 0.0}))
    u = rp.load_universe(p, DAY)
    assert [r["symbol"] for r in u] == ["QQQ"] and u[0]["source"] == "tight"
    empty = tmp_path / "e.json"
    empty.write_text("{}")
    with pytest.raises(SystemExit, match="no rows, universe, or admits"):
        rp.load_universe(empty, DAY)
    (tmp_path / "b.json").write_text("[]")
    assert rp.load_universe(tmp_path / "b.json", DAY) == []


def test_fake_broker_synthetic_exit_without_a_print_stays_open():
    prints = {"ABC": ([100.0], [10.0])}

    def fill(sym, t):
        return rp.TradeTape(prints).next_after(sym, t)

    b = rp.FakeBroker(hold_sec=60, max_pos=5, equity=1e5)
    b.fill_fn = fill
    b.enter("ABC", 10.0, 100.0, {})
    dash = {"tickers": [{"ticker": "ABC", "price": 10.2}]}
    assert b.exits_due(165.0, dash) == []
    assert b.closed == [] and "ABC" in b.open and b.exit_no_print == 1
    assert b.exits_due(170.0, dash) == [] and b.exit_no_print == 1
    prints["ABC"] = ([100.0, 180.0], [10.0, 10.5])
    assert b.exits_due(175.0, dash) == ["ABC"]
    assert (b.closed[0]["exit_px"], b.closed[0]["exit_ts"]) == (10.5, 180.0)
    assert b.exit_no_print == 1


def test_recall_pair_movers_tight_window_and_vacuous_zero():
    rec = [
        {"symbol": "A", "first_ts": 2000.0, "source": "movers"},
        {"symbol": "A", "first_ts": 1000.0, "source": "movers"},
        {"symbol": "B", "first_ts": 1000.0, "source": "momentum"},
        {"symbol": "C", "first_ts": 1000.0, "source": "tight"},
    ]
    recon = [
        {"symbol": "A", "first_ts": 1000.0 + 9 * 60, "source": "movers"},
        {"symbol": "C", "first_ts": 1000.0 + 11 * 60, "source": "tight"},
        {"symbol": "B", "first_ts": 1000.0, "source": "movers"},
    ]
    r = sf.recall_pair(rec, recon, traded_syms=["A", "C", "B"])
    assert r["admit_n"] == 2 and r["admit_hit"] == 1
    assert r["traded_n"] == 2 and r["traded_hit"] == 1
    assert abs(r["admit_recall"] - 0.5) < 1e-9
    z = sf.recall_pair([], [], [])
    assert z["admit_n"] == 0 and z["admit_recall"] is None and z["traded_recall"] is None


def test_part_b_pass_and_fidelity_document():
    diffs = {"base": 1.0, "st": -2.9, "wrrsi_st": 0.2}
    assert sf.part_b_pass(0.70, 0.70, diffs, 1.5)["pass"]
    assert not sf.part_b_pass(0.69, 0.70, diffs, 1.5)["pass"]
    assert not sf.part_b_pass(None, 0.80, diffs, 0.0)["pass"]
    assert not sf.part_b_pass(0.8, 0.8, {"base": 3.1}, 0.0)["pass"]
    assert not sf.part_b_pass(0.8, 0.8, {}, 0.0)["pass"]
    assert not sf.part_b_pass(0.8, 0.8, diffs, None)["pass"]
    assert sf.fidelity_document(True, False, {}, {}, {})["verdict"] == "FAIL"
    assert sf.fidelity_document(True, True, {"a": 1}, {"b": 1}, {"p": 1})["verdict"] == "PASS"


def test_held_out_calendar_and_mde():
    from datetime import datetime, timedelta
    hol = {"2026-04-03", "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07"}
    assert set(sf.HELD_OUT_HOLIDAYS) == hol
    d = datetime(2026, 3, 2)
    end = datetime(2026, 9, 23)
    n = 0
    while d <= end:
        if d.weekday() < 5 and d.strftime("%Y-%m-%d") not in hol:
            n += 1
        d += timedelta(days=1)
    assert n == 143
    days = sf.held_out_trading_days()
    assert len(days) == 143 and days[0] == "2026-03-02" and days[-1] == "2026-09-23"
    assert "2026-07-02" in days and "2026-07-03" not in days and "2026-03-07" not in days
    assert sf.mde_80(None, 143) is None and sf.mde_80(4.0, 1) is None
    assert abs(sf.mde_80(4.0, 143) - sf.Z_80 * 4.0 / (143 ** 0.5)) < 1e-9


def test_b_trade_pool_charges_the_day_median_across_variants(tmp_path, monkeypatch):
    day = "2026-10-08"
    for leg in ("rec", "syn_hist"):
        (tmp_path / leg).mkdir()
    def write(leg, variant, trades):
        (tmp_path / leg / f"{day}-{variant}.json").write_text(json.dumps({"closed": trades}))
    write("rec", "base", [{"symbol": "A", "entry_ts": 1.0, "ret": 0.01}])
    write("syn_hist", "base", [{"symbol": "A", "entry_ts": 1.0, "ret": 0.01}])
    write("rec", "st", [{"symbol": "B", "entry_ts": 2.0, "ret": 0.0}])
    write("syn_hist", "st", [{"symbol": "B", "entry_ts": 2.0, "ret": 0.0}])
    write("rec", "wrrsi_st", [{"symbol": "C", "entry_ts": 3.0, "ret": 0.002}])
    write("syn_hist", "wrrsi_st", [{"symbol": "C", "entry_ts": 3.0, "ret": 0.003}])
    spreads = {("A", 1.0): 10.0, ("B", 2.0): 30.0}

    def fake(_cache, sym, ts, _box):
        return spreads.get((sym, float(ts)))

    import replay_costing as RC
    monkeypatch.setattr(RC, "entry_spread_bp", fake)
    diffs, sd_bp, _sd_paired, paired = sf._b_trade_pool(tmp_path, [day], {}, {})
    assert diffs["base"] == 0.0 and diffs["st"] == 0.0
    assert diffs["wrrsi_st"] == 10.0          # (30 - 20) - (20 - 20); C had no NBBO
    assert paired == 10.0 and sd_bp is None   # one day: no per-day sd


def test_gate_writes_nothing_until_both_legs_exist(tmp_path):
    import pytest
    dest = tmp_path / "FIDELITY.json"
    with pytest.raises(SystemExit, match="part A score missing"):
        sf.main(["gate", "--out", str(tmp_path), "--dest", str(dest), "2026-10-08"])
    (tmp_path / "fidelity_first.json").write_text(json.dumps({"verdict": {"PASS": True}}))
    with pytest.raises(SystemExit, match="part B not ready"):
        sf.main(["gate", "--out", str(tmp_path), "--dest", str(dest), "2026-10-08"])
    assert not dest.exists()
    with pytest.raises(SystemExit):
        sf.main(["run", "--out", str(tmp_path), "--leg", "syn", "--universe-kind", "nope", "2026-10-08"])
