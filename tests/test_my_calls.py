"""The book's "Buy?" button logs the operator's call and never trades."""
import json

import pytest


@pytest.fixture
def client(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import dashboard as dash
    monkeypatch.setattr(dash, "is_auth_required", lambda: False)
    monkeypatch.setattr(dash, "MY_CALLS_PATH", tmp_path / "my_calls.jsonl")
    import ai_positions

    def _boom(*_a, **_k):
        raise AssertionError("the Buy? button must never reach desk_click")
    monkeypatch.setattr(ai_positions, "desk_click", _boom)
    return TestClient(dash.app), tmp_path / "my_calls.jsonl"


def test_call_is_appended(client):
    c, path = client
    r = c.post("/api/ai/my-call", json={"ticker": "oxy", "price": 57.95, "status": "watching", "exh": "■"})
    assert r.status_code == 200 and r.json()["ok"] is True
    r = c.post("/api/ai/my-call", json={"ticker": "RKLB", "price": "74.1"})
    assert r.status_code == 200
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["symbol"] for x in rows] == ["OXY", "RKLB"]
    assert rows[0]["shown_price"] == 57.95 and rows[1]["shown_price"] == 74.1
    assert rows[0]["shown_status"] == "watching" and rows[0]["ts"] > 0


@pytest.mark.parametrize("body", [{}, {"ticker": ""}, {"ticker": "RM -RF"}, {"ticker": "TOOLONGX"}, []])
def test_bad_ticker_is_refused_and_not_logged(client, body):
    c, path = client
    r = c.post("/api/ai/my-call", json=body)
    assert r.status_code == 400
    assert not path.exists()


def test_junk_price_is_stored_as_none(client):
    c, path = client
    assert c.post("/api/ai/my-call", json={"ticker": "OXY", "price": "abc"}).status_code == 200
    assert json.loads(path.read_text())["shown_price"] is None


def test_call_bar_fields_and_todays_list(client, monkeypatch):
    import dashboard as dash
    c, path = client
    monkeypatch.setattr(dash, "snapshot", lambda: {"tickers": [{"ticker": "TGL", "price": 0.50}]})
    r = c.post("/api/ai/my-call", json={"ticker": "tgl", "setup": "%R turn", "via": "bar",
                                         "snapshot": {"pctr": -12.5, "g_spread": 0.04, "phase": "watching", "x" * 40: 1}})
    assert r.status_code == 200
    rec = json.loads(path.read_text().splitlines()[-1])
    assert rec["setup"] == "%R turn" and rec["via"] == "bar" and rec["desk_price"] == 0.5 and rec["retroactive"] is False
    assert rec["snapshot"]["pctr"] == -12.5 and rec["snapshot"]["phase"] == "watching"
    monkeypatch.setattr(dash, "snapshot", lambda: {"tickers": [{"ticker": "TGL", "price": 0.51}]})
    out = c.get("/api/ai/my-calls").json()
    assert out["ok"] and out["calls"][0]["symbol"] == "TGL" and out["calls"][0]["move_bp"] == 200.0


def test_paper_pnl_totals_use_locked_results(client, monkeypatch):
    import dashboard as dash
    c, path = client
    monkeypatch.setattr(dash, "MY_CALLS_RESULTS_PATH", path.parent / "my_calls_results.json")
    monkeypatch.setattr(dash, "_ensure_call_worker", lambda: None)
    monkeypatch.setattr(dash, "snapshot", lambda: {"tickers": []})
    c.post("/api/ai/my-call", json={"ticker": "AAA"})
    c.post("/api/ai/my-call", json={"ticker": "BBB"})
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    (path.parent / "my_calls_results.json").write_text(json.dumps({
        f"AAA|{rows[0]['ts']}": {"status": "final", "net_bp": 25.0, "pnl_usd": 2.5},
    }))
    out = c.get("/api/ai/my-calls").json()
    assert out["total"]["n"] == 1 and out["total"]["pnl_usd"] == 2.5 and out["total"]["wins"] == 1
    assert out["total"]["pending"] == 1
    assert {x["symbol"]: x["status"] for x in out["calls"]} == {"AAA": "final", "BBB": "live"}


def test_score_call_charges_the_spread(monkeypatch):
    import types
    import pandas as pd
    import dashboard as dash
    import sys
    t0 = 1792087200   # a whole minute
    idx = pd.to_datetime([t0 + 60 * k for k in range(1, 17)], unit="s", utc=True)
    df = pd.DataFrame({"open": [10.0] * 16, "close": [10.0] * 15 + [10.2]}, index=idx)
    fake_bars = types.SimpleNamespace(client=lambda: types.SimpleNamespace(get_stock_bars=lambda req: types.SimpleNamespace(df=df)))
    fake_rc = types.SimpleNamespace(load_cache=lambda: {}, save_cache=lambda c: None, entry_spread_bp=lambda *a: 10.0)
    monkeypatch.setitem(sys.modules, "bars", fake_bars)
    monkeypatch.setitem(sys.modules, "replay_costing", fake_rc)
    res = dash._score_call({"symbol": "AAA", "ts": t0 + 5})
    assert res["status"] == "final" and res["entry"] == 10.0
    assert res["exit"] == 10.0 and res["net_bp"] == -10.0 and res["pnl_usd"] == -1.0   # 15 bars held, spread charged
