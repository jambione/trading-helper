"""call_exits: calls scored through the desk's own exit code on synthetic prints. No market data."""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import call_exits as C  # noqa: E402

ET = ZoneInfo("America/New_York")


def _t(hh, mm, ss=0):
    return datetime(2026, 10, 20, hh, mm, ss, tzinfo=ET).timestamp()


def _cfg():
    import config
    return config.load_config_ro()


def _path(start, prices, step=2.0, size=200):
    return [(start + i * step, p, size) for i, p in enumerate(prices)]


def test_sample_ticks_and_round_lots():
    tr = [(10.0, 1.0, 50), (10.5, 1.01, 100), (11.9, 1.02, 300), (12.1, 1.03, 100)]
    assert C.sample_ticks(tr) == [(11.9, 1.02), (12.1, 1.03)]
    assert C.first_print_after(tr, 10.0) == (10.5, 1.01)
    assert C.first_print_after(tr, 10.0, wait=0.1) is None


def test_bars_upto_never_reads_an_unfinished_bar():
    bars = [(1000.0 + 60 * i, 1.0, 1.0, 1.0) for i in range(30)]
    got = C.bars_upto(bars, 1000.0 + 60 * 25)
    assert got is not None and got[0][-1] == 1000.0 + 60 * 24
    assert C.bars_upto(bars, 1000.0 + 60 * 10) is None


def test_dedupe_and_retroactive():
    calls = [{"symbol": "A", "ts": 1.0}, {"symbol": "A", "ts": 100.0}, {"symbol": "A", "ts": 400.0},
             {"symbol": "B", "ts": 2.0, "retroactive": True}]
    assert [c["ts"] for c in C.dedupe(calls)] == [1.0, 400.0]


def test_no_print_after_the_click_is_no_fill():
    click = _t(7, 0)
    assert C.simulate("X", click, [(click + 500, 1.0, 200)], [], _cfg(), click + 3600)["status"] == "no_fill"


def test_a_falling_name_is_stopped_out_by_the_desk_trail():
    click = _t(10, 0)
    prices = [2.00] * 5 + [2.00 - 0.01 * k for k in range(1, 40)]
    tr = _path(click + 1, prices)
    res = C.simulate("X", click, tr, [], _cfg(), click + 3600)
    assert res["status"] == "final" and res["gross_bp"] < 0
    assert res["reason"] in ("local_trail", "dead_trade")


def test_the_live_breakeven_cent_scratches_a_2_dollar_runner_and_the_scaled_line_holds_it():
    click = _t(10, 0)
    up = [2.00 + 0.004 * k for k in range(0, 150)]          # +30% over five minutes
    down = [up[-1] - 0.01 * k for k in range(1, 60)]
    tr = _path(click + 1, up + down)
    asis = C.simulate("X", click, tr, [], _cfg(), click + 7200, spread_bp=20.0, variant="asis")
    scaled = C.simulate("X", click, tr, [], _cfg(), click + 7200, spread_bp=20.0, variant="scaled")
    # The live floor (fill + $0.01 = +0.5% at $2) sits above the price once the trade is up 0.15%: sold at once.
    assert asis["status"] == "final" and asis["gross_bp"] < 100
    assert scaled["status"] == "final" and scaled["gross_bp"] > asis["gross_bp"] + 200


def test_scaled_offset_is_a_tenth_of_a_percent():
    assert C.variant_cfg({}, "scaled", 2.0)["ai_breakeven_offset_px"] == 0.002
    assert C.variant_cfg({"x": 1}, "asis", 2.0) == {"x": 1}


def test_the_flatten_closes_by_15_50():
    click = _t(15, 40)
    res = C.simulate("X", click, _path(click + 1, [5.0 + 0.0001 * (k % 3) for k in range(900)]), [], _cfg(), _t(16, 0))
    assert res["status"] == "final" and res["exit_ts"] <= _t(15, 51)


def test_open_when_the_data_ends_first():
    click = _t(10, 0)
    res = C.simulate("X", click, _path(click + 1, [3.0] * 10), [], _cfg(), click + 25)
    assert res["status"] == "open"


def test_the_clock_patch_is_undone():
    import ai_positions as cp
    t0 = cp.time
    C.simulate("X", _t(10, 0), _path(_t(10, 0) + 1, [3.0] * 5), [], _cfg(), _t(10, 1))
    assert cp.time is t0


def test_after_hours_and_weekend_clicks():
    assert C.after_hours(_t(16, 5)) and not C.after_hours(_t(15, 59))
    sat = datetime(2026, 10, 24, 10, 0, tzinfo=ET).timestamp()
    assert C.after_hours(sat)


def test_session_end_keeps_a_pending_exit_reason():
    res = {"status": "open", "entry": 2.0, "mark": 2.1, "pending_exit": "local_trail"}
    out = C.finalize_at_session_end(res, _t(16, 0))
    assert out["status"] == "final" and out["reason"] == "local_trail" and abs(out["gross_bp"] - 500.0) < 1e-6
    assert C.finalize_at_session_end({"status": "open", "entry": 2.0, "mark": 2.0}, _t(16, 0))["reason"] == "session_end"


def test_trades_are_fetched_incrementally(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CACHE_DIR", tmp_path)
    calls = []

    def fake_fetch(_cl, _sym, start, end):
        calls.append((start, end))
        return [(start + 1, 1.0, 100), (end - 1, 1.1, 100)]
    monkeypatch.setattr(C, "fetch_trades", fake_fetch)
    a = C.cached_trades(None, "X", "X|1", 100.0, 200.0)
    b = C.cached_trades(None, "X", "X|1", 100.0, 300.0)
    c = C.cached_trades(None, "X", "X|1", 100.0, 300.0)
    assert calls == [(100.0, 200.0), (200.0, 300.0)]
    assert len(a) == 2 and len(b) == 4 and c == b


def test_one_failing_call_does_not_stop_the_others(tmp_path, monkeypatch):
    import json
    import types
    calls_file = tmp_path / "calls.jsonl"
    t1, t2 = _t(10, 0), _t(10, 30)
    calls_file.write_text(json.dumps({"symbol": "BAD", "ts": t1}) + "\n" + json.dumps({"symbol": "OK", "ts": t2}) + "\n")
    monkeypatch.setattr(C, "CALLS", calls_file)
    monkeypatch.setattr(C, "RESULTS", tmp_path / "res.json")
    monkeypatch.setattr(C, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(C, "_client", lambda: object())

    def fake_fetch(_cl, sym, start, end):
        if sym == "BAD":
            raise RuntimeError("429")
        return [(start + 1 + 2 * i, 3.0, 200) for i in range(50)]
    monkeypatch.setattr(C, "fetch_trades", fake_fetch)
    monkeypatch.setattr(C, "fetch_bars", lambda *a: [])
    import sys
    rc = types.SimpleNamespace(load_cache=lambda: {}, save_cache=lambda c: None, entry_spread_bp=lambda *a: None)
    monkeypatch.setitem(sys.modules, "replay_costing", rc)
    out = C.score_pending(now=_t(17, 0))
    assert "BAD|" + str(t1) not in out
    ok = out["OK|" + str(t2)]
    assert ok["status"] == "final" and ok["spread_known"] is False and ok["spread_bp"] == C.NO_QUOTE_BP
    assert ok["scaled"]["pnl_usd"] is not None
