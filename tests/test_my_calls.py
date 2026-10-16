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
