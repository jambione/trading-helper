"""tools/replay_session — the pieces that decide what the replayed code sees."""
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import replay_session as rp  # noqa: E402


def _archive(tmp_path, recs):
    p = tmp_path / "state_snapshots.jsonl.gz"
    with gzip.open(p, "wt") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    return p


def test_ages_grow_with_time_since_capture():
    row = {"ticker": "DHT", "price": 21.6, "price_age_sec": 3.0, "rt_price_age_sec": 1.0}
    out = rp.aged(row, 12.0)
    assert out["price_age_sec"] == 15.0 and out["rt_price_age_sec"] == 13.0
    assert row["price_age_sec"] == 3.0  # input untouched


def test_recording_plays_in_time_order(tmp_path):
    rec = rp.Recording(_archive(tmp_path, [
        {"ts": 10.0, "file": "a.json", "data": {"v": 1}},
        {"ts": 20.0, "file": "a.json", "data": {"v": 2}},
    ]))
    rec.advance(15.0)
    assert rec.get("a.json") == (10.0, {"v": 1})
    rec.advance(25.0)
    assert rec.get("a.json") == (20.0, {"v": 2})


def test_dashboard_rebuilt_from_api_state_with_engine_indicators(tmp_path):
    rec = rp.Recording(_archive(tmp_path, [
        {"ts": 100.0, "file": "signal_state.json",
         "data": {"tickers": {"DHT": {"pctr": -48.0, "rt_price_age_sec": 1.0}}}},
        {"ts": 101.0, "file": "api/state",
         "data": {"tickers": [{"ticker": "DHT", "price": 21.6, "price_age_sec": 2.0}],
                  "bb_live": {"x": 1}}},
    ]))
    rec.advance(110.0)
    st = rp.build_dashboard_state(rec, 110.0)
    row = st["tickers"][0]
    assert row["price_age_sec"] == 11.0                       # 2 + (110 - 101)
    assert row["signal_proximity"]["pctr"] == -48.0
    assert row["signal_proximity"]["rt_price_age_sec"] == 11.0  # 1 + (110 - 100)
    assert st["bb_live"] == {"x": 1}


def test_degraded_dashboard_uses_engine_prints(tmp_path):
    rec = rp.Recording(_archive(tmp_path, [
        {"ts": 100.0, "file": "signal_state.json",
         "data": {"tickers": {"DHT": {"rt_price": 21.63, "rt_price_age_sec": 2.0}}}},
    ]))
    rec.advance(105.0)
    row = rp.build_dashboard_state(rec, 105.0)["tickers"][0]
    assert (row["ticker"], row["price"], row["price_age_sec"]) == ("DHT", 21.63, 7.0)


def test_recorded_inputs_honor_the_live_cache_window(tmp_path, monkeypatch):
    snap = tmp_path / "snap"
    snap.mkdir()
    with gzip.open(snap / "recorder_inputs.jsonl.gz", "wt") as f:
        f.write(json.dumps({"ts": 1000.0, "kind": "sip_spread", "symbol": "DHT",
                            "value": 0.04}) + "\n")
    monkeypatch.setenv("REPLAY_REPO", str(tmp_path / "nowhere"))
    ri = rp.RecordedInputs("2026-09-25", snap)
    assert ri.at("sip_spread", "dht", 1100.0, 180.0) == 0.04
    assert ri.at("sip_spread", "DHT", 1200.0, 180.0) is ri.MISSING   # expired
    assert ri.at("sip_spread", "DHT", 900.0, 180.0) is ri.MISSING    # not yet computed


def test_fake_broker_exits_after_the_hold():
    b = rp.FakeBroker(hold_sec=420, max_pos=5, equity=1e5)
    dash = {"tickers": [{"ticker": "DHT", "price": 21.60}]}
    b.enter("DHT", 21.60, 1000.0, {"stop_price": 21.2})
    assert b.exits_due(1300.0, dash) == []
    dash["tickers"][0]["price"] = 21.66
    assert b.exits_due(1420.0, dash) == ["DHT"]
    assert abs(b.closed[0]["ret"] - (21.66 / 21.60 - 1)) < 1e-12
    assert b.last_exit["DHT"] == 1420.0


def test_degraded_dashboard_carries_the_recorded_account(tmp_path):
    rec = rp.Recording(_archive(tmp_path, [
        {"ts": 100.0, "file": "ai_positions_state.json",
         "data": {"account": {"equity": 2253.32}, "positions": {}}},
    ]))
    rec.advance(105.0)
    st = rp.build_dashboard_state(rec, 105.0)
    assert st["ai_positions"]["account"]["equity"] == 2253.32


def test_synthetic_engine_row_is_plain_json(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    o = datetime(2026, 9, 25, 9, 30, tzinfo=et).timestamp()
    bars = {"ABC": {
        "iex": [(o - 60 * (200 - k), 30.0, 30.2, 29.8, 30.0 + 0.01 * (k % 7), 1000.0)
                for k in range(200)],
        "s": [(o + 60 * k, 30.0, 30.3, 29.9, 30.0 + 0.02 * (k % 5), 5000.0) for k in range(60)],
    }}
    eng = rp.SynthEngine(bars, "2026-09-25", warmup=30, cap=50)
    eng.request(["abc"], o + 600)
    assert eng.rows(set(), o + 610) == []                  # still warming up
    rows = eng.rows(set(), o + 60 * 30 + 5)
    assert rows and rows[0]["synthetic"] and rows[0]["price_age_sec"] == 5.0
    json.dumps(rows)                                       # numpy-free
    assert eng.rows({"ABC"}, o + 60 * 30 + 5) == []        # live already watches it


def test_fidelity_picks_the_build_that_ran_longest_not_the_most_records(tmp_path):
    """2026-09-25: seven config records on c1b54b0 in 40 min outnumbered the
    build that ran 11:52-16:00; the replay must pick the latter and its start."""
    from datetime import datetime
    day = "2026-09-25"

    def ts(h, m):
        return datetime(2026, 9, 25, h, m, tzinfo=rp.ET).timestamp()
    recs = [(ts(9, 59), "c1b54b0"), (ts(10, 3), "c1b54b0"), (ts(10, 12), "c1b54b0"),
            (ts(10, 14), "c1b54b0"), (ts(10, 15), "c1b54b0"), (ts(10, 27), "b5e12cf"),
            (ts(10, 39), "55e7ced"), (ts(11, 52), "b739543")]
    with gzip.open(tmp_path / "recorder_config.jsonl.gz", "wt") as f:
        for t, sha in recs:
            f.write(json.dumps({"ts": t, "git_sha": sha}) + "\n")
    sha, dirty, since = rp.live_code_sha(day, tmp_path, "09:00", "15:50")
    assert sha == "b739543" and dirty is False
    assert since == ts(11, 52)
