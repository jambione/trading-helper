"""desk_io: what the desk read live is exactly what a replay serves.

Three properties, each the reason a past replay silently diverged:
  * the /api/state delta codec rebuilds every payload exactly, ages included;
  * an alpaca-py call recorded live comes back byte-identical in replay, and a
    call that was never recorded is a counted miss, never a fetch;
  * a recording that restarts mid-day (new process) does not leak old state.
"""
import copy
import gzip
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

desk_io = pytest.importorskip("desk_io")


def _payload(t, px):
    return {
        "tickers": [
            {"ticker": "AAA", "price": px, "price_age_sec": 1.5,
             "funnel": {"rvol": 2.0}},
            {"ticker": "BBB", "price": 20.0, "price_age_sec": 30.0},
        ],
        "engine": {"signals": {"AAA": {"pctr": -48.0, "bars_age_sec": 4.0}}},
        "movers": [],
        "discord": "x",
    }


def _roundtrip(seq):
    enc, dec = desk_io.DashEncoder(), desk_io.DashDecoder()
    for t, p in seq:
        dec.apply(enc.encode(copy.deepcopy(p), t))
        got = dec.payload(t)
        assert got == p, (t, got, p)


def test_dash_codec_rebuilds_every_payload():
    seq = []
    t = 1_000_000.0
    for i in range(6):
        p = _payload(t, 10.0 + i * 0.01)
        if i == 3:
            p["tickers"].reverse()                      # order change
        if i == 4:
            p["tickers"].append({"ticker": "CCC", "price": 5.0})
            del p["discord"]                            # key disappears
        seq.append((t + i * 0.3, p))
    _roundtrip(seq)


def test_dash_codec_writes_only_changes():
    enc = desk_io.DashEncoder()
    t = 1_000_000.0
    first = enc.encode(_payload(t, 10.0), t)
    assert first["reset"] is True
    # Same prints 2 s later: ages grew by 2 s, the implied epochs did not.
    p = _payload(t, 10.0)
    for r in p["tickers"]:
        r["price_age_sec"] += 2.0
    p["engine"]["signals"]["AAA"]["bars_age_sec"] += 2.0
    assert enc.encode(p, t + 2.0) == {}


def test_dash_restart_resets_decoder():
    dec = desk_io.DashDecoder()
    a, b = desk_io.DashEncoder(), desk_io.DashEncoder()
    t = 1_000_000.0
    dec.apply(a.encode({"tickers": [{"ticker": "OLD", "price": 1.0}], "stale": 1}, t))
    dec.apply(b.encode({"tickers": [{"ticker": "NEW", "price": 2.0}]}, t + 1))
    assert dec.payload(t + 1) == {"tickers": [{"ticker": "NEW", "price": 2.0}]}


def test_alpaca_key_ignores_time_params_only():
    k1 = desk_io.alpaca_key("GET", "/v2/stocks/bars",
                            {"symbols": "AAA", "start": "a", "end": "b", "feed": "iex"})
    k2 = desk_io.alpaca_key("get", "/v2/stocks/bars",
                            {"feed": "iex", "symbols": "AAA", "start": "c"})
    k3 = desk_io.alpaca_key("GET", "/v2/stocks/bars", {"symbols": "BBB", "feed": "iex"})
    assert k1 == k2 and k1 != k3


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    import session_recorder
    session_recorder.set_enabled(True)
    yield tmp_path
    desk_io.uninstall()
    desk_io.served.clear()
    desk_io.missed.clear()
    desk_io.miss_callers.clear()


def _wire_file(root):
    files = list(root.rglob("wire.jsonl.gz"))
    assert len(files) == 1, files
    return files[0]


def test_alpaca_live_then_replay_is_identical(isolated, monkeypatch):
    from alpaca.common.rest import RESTClient
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest

    body = {"quotes": {"AAA": {"ap": 10.02, "as": 3, "ax": "V", "bp": 10.0, "bs": 1,
                               "bx": "V", "c": ["R"], "t": "2026-09-25T15:00:00.123Z",
                               "z": "C"}}}
    calls = []

    def fake_one(self, method, url, opts, retry):
        calls.append(url)
        return copy.deepcopy(body)

    monkeypatch.setattr(RESTClient, "_one_request", fake_one)
    desk_io.install_live()
    c = StockHistoricalDataClient("k", "s")
    live = c.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols="AAA"))
    import session_recorder
    session_recorder.flush(force=True)
    desk_io.uninstall()
    assert len(calls) == 1

    t_now = [__import__("time").time() + 1]
    desk_io.install_replay(_wire_file(isolated), clock=lambda: t_now[0])
    rep = c.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols="AAA"))
    assert len(calls) == 1, "replay must never reach the network"
    assert rep["AAA"].ask_price == live["AAA"].ask_price == 10.02
    assert rep["AAA"].timestamp == live["AAA"].timestamp
    assert desk_io.served and not desk_io.missed

    with pytest.raises(Exception):
        c.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols="ZZZ"))
    assert len(calls) == 1
    assert sum(desk_io.missed.values()) == 1


def test_replay_serves_the_read_live_had_then(isolated):
    """Two recorded reads of one key: the replay gets the latest one at or
    before its clock, and nothing before the first."""
    path = isolated / "wire.jsonl.gz"
    rows = [{"ts": 100.0, "ch": "alpaca", "m": "GET", "p": "https://x/v2/a", "q": {"s": 1}, "r": 1},
            {"ts": 200.0, "ch": "alpaca", "m": "GET", "p": "https://x/v2/a", "q": {"s": 1}, "r": 2}]
    with gzip.open(path, "wt") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    rec = desk_io.Recording(path)
    k = desk_io.alpaca_key("GET", "/v2/a", {"s": 1})
    assert rec.alpaca_at(k, 99.0) is None
    assert rec.alpaca_at(k, 150.0)["r"] == 1
    assert rec.alpaca_at(k, 250.0)["r"] == 2


def test_file_channel_live_then_replay(isolated, monkeypatch, tmp_path):
    """Input files come back as live read them, version by version; files the
    process wrote itself are its own state, not inputs; an unrecorded file is
    a counted miss that reads as absent."""
    import time as _t
    from pathlib import Path
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(desk_io, "ROOT", root)
    panel = root / "trending_stocks.json"
    panel.write_text(json.dumps({"stocks": [{"symbol": "AAA", "pct": 1.0}]}))

    desk_io.install_live(files=True)
    try:
        v1 = json.loads(panel.read_text(encoding="utf-8"))
        t1 = _t.time()
        with open(root / "own_state.json", "w") as f:
            f.write('{"mine": 1}')
        assert json.load(open(root / "own_state.json")) == {"mine": 1}
        _t.sleep(0.01)
        # Another process (the screener) rewrites the panel.
        with desk_io._orig_open(panel, "w") as f:
            f.write(json.dumps({"stocks": [{"symbol": "AAA", "pct": 2.0}]}))
        with open(panel) as f:
            v2 = json.load(f)
        t2 = _t.time()
    finally:
        import session_recorder
        session_recorder.flush(force=True)
        desk_io.uninstall()
    assert v1["stocks"][0]["pct"] == 1.0 and v2["stocks"][0]["pct"] == 2.0

    wire = _wire_file(isolated)
    rows = [json.loads(l) for l in gzip.open(wire, "rt")]
    files = [r["f"] for r in rows if r.get("ch") == "file"]
    assert files == ["trending_stocks.json", "trending_stocks.json"], files

    # Replay from a tree where the panel does not exist on disk at all.
    panel.unlink()
    clock = [t1]
    desk_io.install_replay(wire, clock=lambda: clock[0], root=root)
    try:
        assert panel.exists()
        assert json.loads(panel.read_text(encoding="utf-8")) == v1
        clock[0] = t2 + 1
        assert json.loads(panel.read_text(encoding="utf-8")) == v2
        assert panel.stat().st_mtime > 0
        assert not (root / "movers_stocks.json").exists()
        assert desk_io.missed["file movers_stocks.json"] >= 1
        with open(root / "own2.json", "w") as f:
            f.write("[1]")
        assert json.load(open(root / "own2.json")) == [1]
    finally:
        desk_io.uninstall()


def test_dash_replay_flags_reads_of_unrecorded_keys():
    desk_io.missed.clear()
    full = {"tickers": [{"ticker": "AAA", "price": 1.0}],
            "ai_positions": {"account": {"equity": 1000.0}, "entry_book": [1, 2]},
            "claude_positions": {"x": 1}, "discord": "d"}
    sl = desk_io.dash_slice(full)
    assert set(sl) == {"tickers", "ai_positions"} and sl["ai_positions"] == {"account": {"equity": 1000.0}}
    p = desk_io._tracked(sl)
    assert p.get("ai_positions", {}).get("account", {}).get("equity") == 1000.0
    assert p.get("tickers")[0]["ticker"] == "AAA"
    assert not desk_io.missed
    assert p.get("discord") is None
    assert p["ai_positions"].get("entry_book") is None
    assert desk_io.missed["dash key discord"] == 1
    assert desk_io.missed["dash ai_positions key entry_book"] == 1
    desk_io.missed.clear()


def test_missing_input_file_is_recorded_once_and_replays_as_absent(isolated, monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(desk_io, "ROOT", root)
    p = root / "suggestions.json"
    import time as _t
    desk_io.install_live(files=True)
    try:
        for _ in range(3):
            assert not p.exists()
        t1 = _t.time()
    finally:
        import session_recorder
        session_recorder.flush(force=True)
        desk_io.uninstall()
    rows = [json.loads(l) for l in gzip.open(_wire_file(isolated), "rt")]
    assert [r.get("absent") for r in rows if r.get("ch") == "file"] == [True]
    desk_io.install_replay(_wire_file(isolated), clock=lambda: t1, root=root)
    try:
        assert not p.exists()
        assert not desk_io.missed
    finally:
        desk_io.uninstall()


def test_report_dir_files_share_one_name(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    rep = tmp_path / "elsewhere"
    monkeypatch.setattr(desk_io, "ROOT", root)
    monkeypatch.delenv("AI_REPORT_DIR", raising=False)
    assert desk_io._rel(root / "ai_reports" / "x.json") == "ai_reports/x.json"
    monkeypatch.setenv("AI_REPORT_DIR", str(rep))
    assert desk_io._rel(rep / "x.json") == "ai_reports/x.json"
    assert desk_io._rel(rep / "sessions" / "d" / "y.json") is None
    nested = rep / "repo"
    monkeypatch.setattr(desk_io, "ROOT", nested)
    assert desk_io._rel(nested / "t.json") == "t.json"
    assert desk_io._rel(root / "config" / "bot_config.json") is None
    assert desk_io._rel(root / "a.txt") is None
