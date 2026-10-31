"""Pure checks for the buystop_flow code-review fixes. No network, no warehouse, no outcomes."""
from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import buystop_flow as B  # noqa: E402
import leash_replay as L  # noqa: E402


def test_prereg_records_the_count_and_leaves_the_window_frozen():
    """The 10/24 count is on the record. The power-amendment sessions are not in the prereg yet."""
    prereg = json.loads((Path(__file__).resolve().parent.parent / "docs" / "studies"
                         / "buystop_flow_prereg.json").read_text())
    assert prereg["count_2026_10_24"]["A"]["mde_bp"] == 3.026
    assert prereg["count_2026_10_24"]["A"]["floors_met"] is False
    assert prereg["count_2026_10_24"]["B"]["floors_met"] is True
    assert prereg["count_2026_10_24"]["result"].startswith("UNDERPOWERED")
    assert "2026-05-11" in prereg["population"]
    assert "2026-04-27" not in json.dumps(prereg)


def test_empty_conditions_are_round_lots_and_odd_lots_are_not():
    assert B.round_lot([]) and B.round_lot(None) and B.round_lot([" "]) and B.round_lot(["@", "F"])
    assert not B.round_lot(["I"]) and not B.round_lot([" ", "I"]) and not B.round_lot(["Z"])


def test_trigger_takes_the_empty_condition_print_and_skips_the_odd_lot():
    k0 = 1_000_000.0
    trades = [(k0 + 1, 10.05, 100, [], "C"), (k0 + 2, 10.50, 100, ["I"], "C")]
    assert B.trigger(trades, k0, 10.0) == (k0 + 1, 10.05)


def test_merge_secs_keeps_the_lowest_low_and_the_later_last():
    earlier = [(100, 10.0, 9.5), (101, 10.2, 10.1)]
    later = [(101, 10.4, 10.0), (102, 10.5, 10.3)]
    assert B.merge_secs([earlier, later]) == [(100, 10.0, 9.5), (101, 10.4, 10.0), (102, 10.5, 10.3)]


def test_last_chunk_clears_a_time_cap_and_the_old_end_does_not():
    assert B.arm_b_horizons()[-1] == L.MAX_HOLD + 60
    t0 = 1_700_000_000.4
    eod = t0 + 8 * 3600
    fetched = B.chunk_end(t0, L.MAX_HOLD + 60, eod)
    xs = int(t0) + L.MAX_HOLD
    assert not B.exit_beyond_fetch(xs, fetched, eod)
    old = B.chunk_end(t0, L.MAX_HOLD, eod)
    assert B.exit_beyond_fetch(xs, old, eod)
    # a late entry is capped at 15:50, and that exit is not "beyond the fetch"
    late_end = B.chunk_end(t0, L.MAX_HOLD + 60, t0 + 100)
    assert late_end == t0 + 100
    assert not B.exit_beyond_fetch(t0 + 100, late_end, t0 + 100)


def test_upper_bound_quantile_is_98_75_percent(monkeypatch):
    seen = []
    monkeypatch.setattr(B.W.E, "t_ppf", lambda p, df: seen.append(p) or 2.0)
    rows = [{"day": "d1", "x": 0.02}, {"day": "d2", "x": 0.00}, {"day": "d3", "x": 0.01}]
    assert B.ub_at(rows, lambda r: r["x"], B.UB_P) is not None
    assert seen[-1] == 0.9875
    assert "0.99375" not in open(B.__file__).read()


def test_select_trades_keeps_each_days_prefix_and_ignores_later_rows():
    trades = []
    for h, days in (("A", ("d1", "d2")), ("B", ("d3", "d4"))):
        for d in days:
            for i in range(10):
                trades.append({"half": h, "day": d, "i": i, "bar_gross": 1.0 if i < 3 else -1.0})
    got = B.select_trades(trades, 5)
    assert len([t for t in got if t["half"] == "A"]) == 5
    assert len([t for t in got if t["half"] == "B"]) == 5
    assert all(t["i"] <= 2 for t in got)
    assert all(t["bar_gross"] == 1.0 for t in got)


def test_nine_hour_budget_keeps_the_full_sample_under_the_bar():
    assert B.BUDGET_H == 9.0
    out = B.budget_n_half(0.8355, 2.5065, 7975)
    assert out["hours_at_full_sample"] < B.BUDGET_H
    assert out["n_half"] == 7975
    assert out["projected_under_bar"]
    assert out["projected_mde"]["A"] == B.PREREG_MDE_AT_8000["A"] * math.sqrt(8000 / 7975)
    assert out["projected_mde"]["A"] <= B.MDE_BAR
    # Pace measured by the 2026-10-24 probe. Full sample is 8.82 hours, under 9.
    measured = B.budget_n_half(1.612563891844316, 1.1371237754821777, 7975)
    assert measured["n_half"] == 7975 and measured["projected_under_bar"]
    thin = B.budget_n_half(30.0, 30.0, 7975)
    assert thin["n_half"] == 405
    assert not thin["projected_under_bar"]


def test_probe_indexes_are_spread_across_the_sample():
    idx = B.probe_indexes(15_950)
    assert idx[0] == 0 and idx[-1] > 10_000 and len(idx) == B.PROBE_CAP
    assert idx[1] - idx[0] > 1


def test_probe_blocks_span_the_whole_sample():
    blocks = B.probe_blocks(15_950)
    assert len(blocks) == B.PROBE_MIN
    assert all(blocks)
    assert blocks[0][0] == 0
    assert blocks[-1][0] > 12_000
    assert blocks[B.PROBE_MIN // 2][0] > 15_950 * 0.4


def test_spy_return_is_open_to_open_and_counts_a_missing_bar():
    opens = {600: 100.0, 630: 101.0}
    assert math.isclose(B.spy_return(opens, 600, 630), 0.01)
    assert B.spy_return(opens, 600, 601) is None
    assert B.spy_return({}, 600, 630) is None


def test_quoted_spread_uses_the_touch():
    assert math.isclose(B.spread_bp(10.0, 10.02), (0.02 / 10.01) * 1e4)
    assert B.spread_bp(0, 10) is None


def test_arm_b_below_the_floor_is_underpowered_not_a_pass():
    assert B.arm_half_verdict(0.01, 5.0, 0.01, 0.01, 0.01, 10, 10) == "UNDERPOWERED"
    assert B.arm_half_verdict(0.01, 5.0, 0.01, 0.01, 0.01, 300, 20) == "PASS"
    assert B.arm_half_verdict(-0.01, -2.5, 0.0, 0.0, 0.0, 300, 20) == "FAIL"
    assert B.rollup(["UNDERPOWERED", "NOT_PASSED"], [-0.01, -0.01]) == "UNDERPOWERED"
    assert B.rollup(["NOT_PASSED", "NOT_PASSED"], [-0.01, -0.01]) == "FAIL"


def test_module_shas_cover_the_exit_stack():
    shas = B.module_shas()
    for name in ("ai_positions", "bars", "config", "leash_replay", "eod_reversal",
                 "indicator_confirm_hist", "exit_capture", "order_flow_pilot",
                 "sell_double_check", "ai_entry_watch"):
        assert len(shas[name]) == 64


def test_stale_sample_with_trades_is_kept_and_not_rebuilt():
    """A hash mismatch declares the freeze. No trades still refuses, and nothing asks for --sample."""
    with pytest.raises(SystemExit, match="not rebuilt"):
        B.assert_sample_current({"module_sha256": {}})
    with pytest.raises(SystemExit, match="not rebuilt"):
        B.assert_sample_current(None)
    with pytest.raises(SystemExit, match="not rebuilt"):
        B.assert_sample_current({"module_sha256": B.module_shas(), "trades": []})
    assert B.assert_sample_current({"module_sha256": B.module_shas(), "trades": [{"k": 1}]}) is None
    note = B.assert_sample_current({"module_sha256": {"buystop_flow": "old"}, "trades": [{"k": 1}]})
    assert note and "not rebuilt" in note and "7975" in note
    assert "rerun --sample" not in note


def test_april_27_contributes_nothing_and_does_not_wrap():
    def load_raw(feed, day):
        raise AssertionError((feed, day))

    wrapped = ["2026-04-27", "2026-04-28", "2026-10-15"]
    assert wrapped[wrapped.index("2026-04-27") - 1] == "2026-10-15"
    assert B.prev_warehouse_session("2026-04-27", wrapped) is None
    days = ["2026-10-15", "2026-04-24", "2026-04-27"]
    assert B.prev_warehouse_session("2026-04-27", days) is None
    assert B.build_addon_day("2026-04-27", days, load_raw) == []
    assert B.prev_warehouse_session("2026-04-28", ["2026-04-27", "2026-04-28", "2026-10-15"]) == "2026-04-27"
    with pytest.raises(SystemExit, match="refusing previous session"):
        B.prev_warehouse_session("2026-04-28", ["2026-04-24", "2026-04-28"])
    with pytest.raises(SystemExit, match="no previous"):
        B.build_addon_day("2026-05-01", ["2026-05-01"], load_raw)


def test_cap_is_a_stable_prefix_and_a_short_day_is_not_backfilled():
    rows = [{"sym": f"S{i:03d}", "k": 600 + (i % 5), "day": "2026-04-28"} for i in range(200)]
    first = B.cap_addon_day(rows, "2026-04-28")
    second = B.cap_addon_day(rows, "2026-04-28")
    assert first == second and len(first) == 145
    ordered = sorted(rows, key=lambda r: (r["sym"], r["k"]))
    B.seeded("bsf-sample", "2026-04-28").shuffle(ordered)
    assert first == ordered[:145]
    assert len(B.cap_addon_day(rows[:10], "2026-04-28")) == 10


def test_addon_row_drops_a_cheap_fill_and_a_missing_gross():
    sip = {600: (22.0, 22.0), 610: (23.0, 23.0)}
    assert B.addon_row("2026-04-28", "BBB", 600, 19.0, 610, 18.0, sip) is None
    assert B.addon_row("2026-04-28", "BBB", 600, 22.0, 610, None, sip) is None
    assert B.addon_row("2026-04-28", "BBB", 800, 21.0, 810, 20.0, sip) is None
    kept = B.addon_row("2026-04-28", "BBB", 600, 22.0, 610, 21.0, sip)
    assert kept["half"] == "A" and kept["k"] == 600 and kept["px_bar"] == 22.0
    assert kept["stop"] == 21.0 and "bar_gross" in kept and "ctrl_d" in kept


def test_build_skips_the_split_and_keeps_only_a_priced_fill(monkeypatch):
    seen = []

    def lines(prev, today):
        return ({600: -90}, {600: -85})

    def stops(fast, slow, sipf, iexf):
        seen.append(sorted(sipf))
        return [(600, 19.0, 610, 18.0), (800, 21.0, 810, 20.0), (600, 22.0, 610, 21.0)]

    monkeypatch.setattr(B.W, "lines", lines)
    monkeypatch.setattr(B, "t2_with_stops", stops)

    def bars(opens):
        return [(minute, px, px, px, px, 1) for minute, px in opens]

    store = {
        ("sip", "2026-04-27"): {"AAA": bars([(570, 10.0)]), "BBB": bars([(570, 21.0)]),
                                "CCC": bars([(570, 21.0)])},
        ("sip", "2026-04-28"): {"AAA": bars([(570, 20.0)]),
                                "BBB": bars([(570, 21.0), (600, 22.0), (610, 23.0)]),
                                "CCC": bars([(570, 21.0), (600, 22.0), (610, 23.0)])},
        ("iex", "2026-04-27"): {"AAA": bars([(570, 10.0)]), "BBB": bars([(570, 21.0)])},
        ("iex", "2026-04-28"): {"AAA": bars([(570, 20.0)]), "BBB": bars([(570, 21.0)])},
    }
    loaded = []

    def load_raw(feed, day):
        loaded.append(day)
        if day in ("2026-04-24", "2026-10-15"):
            raise AssertionError(day)
        return store[(feed, day)]

    rows = B.build_addon_day("2026-04-28", ["2026-04-27", "2026-04-28", "2026-10-15"], load_raw)
    assert len(seen) == 1
    assert [r["sym"] for r in rows] == ["BBB"]
    assert rows[0]["px_bar"] == 22.0
    assert "2026-04-24" not in loaded and "2026-10-15" not in loaded


def test_ten_day_build_leaves_first_and_batch2_alone(monkeypatch):
    monkeypatch.setattr(B.B2, "main", lambda *a, **k: (_ for _ in ()).throw(AssertionError("batch2.main")))
    loaded = []

    def load_raw(feed, day):
        loaded.append(day)
        if day in ("2026-04-24", "2026-10-15") or day < "2026-04-27":
            raise AssertionError(day)
        return {}

    out = B.build_addon_rows(list(B.ADDON_DAYS), load_raw)
    assert out["trades"] == [] and out["days"] == list(B.ADDON_DAYS)
    assert B.W.FIRST == "2026-05-11"
    assert "2026-04-24" not in loaded and "2026-10-15" not in loaded
    import inspect
    assert "days.index" not in inspect.getsource(B.build_addon_day)
    assert "days.index" not in inspect.getsource(B.prev_warehouse_session)


def test_pinned_cfg_pin_wins_and_does_not_open_bot_config(tmp_path, monkeypatch):
    import config
    pin = {"ai_watch_presquare_only": True, "ai_local_trail_print_ring": 7, "_min_hold": 15}
    assert config.DEFAULT_CONFIG["ai_watch_presquare_only"] is False
    raw = json.dumps(pin).encode()
    path = tmp_path / "pinned.json"
    path.write_bytes(raw)
    monkeypatch.setattr(B, "CONFIG_SHA_PREFIX", hashlib.sha256(raw).hexdigest()[:16])

    def boom(*_a, **_k):
        raise AssertionError("config loader called")

    monkeypatch.setattr(config, "load_config", boom)
    monkeypatch.setattr(config, "load_config_ro", boom)
    monkeypatch.setattr(B, "desk_cfg", boom)
    real_open = open

    def guard(file, *a, **k):
        if str(file).endswith("bot_config.json"):
            raise AssertionError(file)
        return real_open(file, *a, **k)

    monkeypatch.setattr("builtins.open", guard)
    cfg, ring = B.pinned_arm_b_cfg(str(path))
    assert cfg["ai_watch_presquare_only"] is True
    assert cfg["ai_local_trail_print_ring"] == 7
    assert "_min_hold" not in cfg and ring == 7
    monkeypatch.setattr(B, "CONFIG_SHA_PREFIX", "0000000000000000")
    with pytest.raises(SystemExit, match="pinned config sha"):
        B.pinned_arm_b_cfg(str(path))


def test_frozen_intersection_keeps_sample_order(monkeypatch):
    monkeypatch.setattr(B, "select_trades", lambda *a, **k: (_ for _ in ()).throw(AssertionError("select")))
    monkeypatch.setattr(B, "chosen_trades", lambda *a, **k: (_ for _ in ()).throw(AssertionError("chosen")))
    trades = [
        {"day": "d", "sym": "A", "k": 1, "half": "A"},
        {"day": "d", "sym": "B", "k": 2, "half": "A"},
        {"day": "d", "sym": "C", "k": 3, "half": "B"},
    ]
    fetched = {"d|A|1": {"status": "ok", "ofi60": 0.1}, "d|B|2": {"status": "no_trigger_in_tape"},
               "d|C|3": {"status": "ok", "ofi60": 0.2}}
    a, b = B.frozen_intersection(trades, fetched)
    assert [r["sym"] for r in a] == ["A"] and a[0]["m"] == 1
    assert [r["sym"] for r in b] == ["C"]
    with pytest.raises(SystemExit, match="C4"):
        B.frozen_intersection(trades, {"d|A|1": {"status": "ok"}})


def test_top_cut_sees_only_frozen_half_a_and_addon_is_appended(monkeypatch):
    import order_flow_pilot as OF
    calls = []

    def fake_top(values):
        calls.append(list(values))
        return B.FROZEN_OFI if len(calls) == 1 else B.FROZEN_QI

    seen = []

    def fake_groups(row, cuts):
        seen.append(cuts)
        return {"G1": "OUT", "G2": "OUT", "G3": "OUT"}

    monkeypatch.setattr(OF, "top_cut", fake_top)
    monkeypatch.setattr(OF, "groups", fake_groups)
    frozen_a = [
        {"day": "2026-06-01", "sym": "AAA", "half": "A", "k": 600, "ofi60": 0.1, "qi": 0.1},
        {"day": "2026-06-02", "sym": "BBB", "half": "A", "k": 601, "ofi60": 0.2, "qi": 0.2},
    ]
    addon = [{"day": "2026-04-28", "sym": "NEW", "half": "A", "k": 610, "ofi60": 9.9, "qi": 9.9,
              "ask_in": 20.0, "bid_out": 21.0}]
    frozen_b = [{"day": "2026-08-03", "sym": "CCC", "half": "B", "k": 620, "ofi60": 0.3, "qi": 0.3}]
    rows = B.assemble_labeled(frozen_a, addon, frozen_b)
    flat = [v[0] for call in calls for v in call]
    assert 9.9 not in flat
    assert [r["sym"] for r in rows] == ["AAA", "BBB", "NEW", "CCC"]
    assert all(c["ofi60"] == B.FROZEN_OFI and c["qi"] == B.FROZEN_QI for c in seen)


def test_missing_addon_fetch_is_a_hard_refusal():
    with pytest.raises(SystemExit, match="C4"):
        B.addon_fetched_or_refuse([{"day": "2026-04-28", "sym": "A", "k": 1}], {})
    with pytest.raises(SystemExit, match="145"):
        B.assert_addon_cap([{"day": "2026-04-28", "sym": "A", "k": i} for i in range(146)])


def test_mde_uses_the_days_that_have_a_mean():
    inn = []
    for day, xs in (("d1", [0.01, 0.03]), ("d2", [0.02]), ("d3", [-0.01]), ("d4", [0.0, 0.0])):
        for x in xs:
            inn.append({"day": day, "net_A": 0.0, "ctrl_d": x, "g1": "IN"})
    got = B.control_mde(inn)
    means = [0.02, 0.02, -0.01, 0.0]
    want = (2.24 + 0.84) * statistics.stdev(means) / math.sqrt(4)
    assert got == want
    assert got != (2.24 + 0.84) * statistics.stdev(means) / math.sqrt(65)


def test_half_b_fingerprint_and_the_public_count():
    ok = {"in_trades": 2319, "days": 55, "mde": 0.0002439410280593733, "floors_met": True}
    B.half_b_fingerprint(ok)
    with pytest.raises(SystemExit, match="frozen count"):
        B.half_b_fingerprint({"in_trades": 2318, "days": 55, "mde": ok["mde"], "floors_met": True})
    note = B.assert_sample_current({"module_sha256": {"buystop_flow": "old"}, "trades": [{"k": 1}]})
    pub = B.count_report(
        {"in_trades": 1, "days": 1, "mde": 0.01, "floors_met": False, "net_A": 1, "bar_gross": [1],
         "day_means": [0.1]},
        {"in_trades": 2319, "days": 55, "mde": ok["mde"], "floors_met": True, "net_B": 1, "ctrl_d": 1},
        note, "72b5cfb8")
    blob = json.dumps(pub)
    for banned in ("net_A", "net_B", "bar_gross", "ctrl_d", "day_means"):
        assert banned not in blob
    assert pub["A"] == {"in_trades": 1, "days": 1, "mde": 0.01, "floors_met": False}
    assert pub["B"]["in_trades"] == 2319 and pub["B"]["floors_met"] is True
    assert "not rebuilt" in pub["sample_kept"]


def test_retired_paths_refuse_without_opening_bot_config():
    import inspect
    assert "open(" not in inspect.getsource(B.desk_cfg)
    with pytest.raises(SystemExit, match="not rebuilt"):
        B.build_sample(8000)
    with pytest.raises(SystemExit, match="select_trades"):
        B.chosen_trades({"trades": []})
    with pytest.raises(SystemExit, match="does not open bot_config"):
        B.desk_cfg()


def test_main_refuses_to_rewrite_the_frozen_artifacts(monkeypatch):
    import inspect
    src = inspect.getsource(B.main)
    assert "desk_cfg(" not in src
    assert "chosen_trades(" not in src
    assert "select_trades(" not in src
    assert "load_config(" not in src
    assert "build_sample(" not in src
    for flag in ("--sample", "--probe", "--fetch"):
        monkeypatch.setattr(sys, "argv", ["buystop_flow.py", flag])
        with pytest.raises(SystemExit, match="not rebuilt|not rewritten"):
            B.main()


def test_arm_b_fetches_bars_through_req(monkeypatch):
    seen = {}

    def fake_req(fn, *a, **k):
        seen.setdefault("fns", []).append(getattr(fn, "__name__", str(fn)))
        return []

    monkeypatch.setattr(B, "_req", fake_req)
    monkeypatch.setattr(B, "pace", lambda: None)
    monkeypatch.setattr(B, "arm_b_walk", lambda *a, **k: (a[0]["T"] + 10, 1.0, "trail", 0))
    row = {"sym": "AAA", "day": "2026-06-02", "T": B.et_epoch("2026-06-02", 600),
           "ask_in": 20.0, "ask_raw": 20.0, "bid_in": 19.9}
    B.fetch_arm_b(object(), row, {}, 3)
    assert "fetch_bars" in seen["fns"]
