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


def test_paper_pnl_totals_use_the_exit_scored_results(client, monkeypatch):
    import dashboard as dash
    c, path = client
    monkeypatch.setattr(dash, "MY_CALLS_EXITS_PATH", path.parent / "my_calls_exits.json")
    monkeypatch.setattr(dash, "_ensure_call_worker", lambda: None)
    monkeypatch.setattr(dash, "snapshot", lambda: {"tickers": []})
    c.post("/api/ai/my-call", json={"ticker": "AAA"})
    c.post("/api/ai/my-call", json={"ticker": "BBB"})
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    (path.parent / "my_calls_exits.json").write_text(json.dumps({
        f"AAA|{rows[0]['ts']}": {"status": "final", "spread_bp": 20.0,
                                 "asis": {"status": "final", "reason": "local_trail", "net_bp": -14.0, "pnl_usd": -1.4},
                                 "scaled": {"status": "final", "reason": "local_trail", "net_bp": 160.0, "pnl_usd": 16.0}},
    }))
    out = c.get("/api/ai/my-calls").json()
    assert out["total"]["asis"] == {"n": 1, "pnl_usd": -1.4, "wins": 0}
    assert out["total"]["scaled"] == {"n": 1, "pnl_usd": 16.0, "wins": 1}
    assert out["total"]["pending"] == 1
    by = {x["symbol"]: x for x in out["calls"]}
    assert by["AAA"]["status"] == "final" and by["AAA"]["scaled"]["reason"] == "local_trail"
    assert by["BBB"]["status"] == "waiting"


def test_the_worker_runs_call_exits_in_its_own_process(monkeypatch, tmp_path):
    import dashboard as dash
    seen = {}

    class Done(Exception):
        pass

    def fake_run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw.get("env") or {}
        raise Done
    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(dash, "MY_CALLS_PATH", tmp_path / "my_calls.jsonl")
    (tmp_path / "my_calls.jsonl").write_text("{}\n")
    real_exists = dash.Path.exists
    monkeypatch.setattr(dash.Path, "exists", lambda self: True if self.name == "secrets_data.json" else real_exists(self))

    def stop(_s):
        raise Done
    monkeypatch.setattr(dash.time, "sleep", stop)
    try:
        dash._call_worker()
    except Done:
        pass
    assert seen["cmd"][-1].endswith("tools/call_exits.py") and seen["env"]["TH_DATA_SECRETS"].endswith("secrets_data.json")
