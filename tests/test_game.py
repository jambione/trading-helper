"""Call the P&L - prediction game rules, store, settlement, and the player gate."""
import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402
import game  # noqa: E402

DAY = "2026-11-18"          # a Wednesday, not a holiday


def at(hh, mm, ss=0, day=DAY):
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, ss, tzinfo=game.ET)


@pytest.fixture
def store(tmp_path):
    return game.Store(tmp_path / "g.sqlite", tmp_path / "snap.jsonl")


# ---------------------------------------------------------------- rules
def test_game_days_skip_weekends_holidays_and_half_days():
    assert game.is_game_day(DAY)
    assert not game.is_game_day("2026-11-21")          # Saturday
    assert not game.is_game_day("2026-11-26")          # Thanksgiving
    assert not game.is_game_day("2026-11-27")          # half day


@pytest.mark.parametrize("slot,t,expected", [
    ("noon", (9, 29, 59), "open"), ("noon", (9, 30, 0), "locked"), ("noon", (12, 0, 0), "scoring"),
    ("close", (11, 59, 59), "open"), ("close", (12, 0, 0), "locked"), ("close", (16, 0, 0), "scoring"),
])
def test_phase_boundaries(slot, t, expected):
    assert game.phase(DAY, slot, at(*t)) == expected


def test_rank_closest_wins_and_ties_go_to_the_earlier_guess():
    preds = [{"username": "a", "value": 10.0, "submitted_ts": 5},
             {"username": "b", "value": -10.0, "submitted_ts": 3},
             {"username": "c", "value": 50.0, "submitted_ts": 1}]
    r = game.rank(preds, 0.0)
    assert [p["username"] for p in r] == ["b", "a", "c"]
    assert [p["rank"] for p in r] == [1, 2, 3] and r[0]["abs_err"] == 10.0


def test_clean_value_rejects_nan_and_absurd():
    assert game.clean_value("-12.5") == -12.5
    for bad in ("nan", "1e9", "abc"):
        with pytest.raises((ValueError, TypeError)):
            game.clean_value(bad)


# ---------------------------------------------------------------- store
def test_submit_locks_at_0930_for_noon_and_noon_for_close(store):
    assert store.submit("a", "Ann", DAY, "noon", 12, at(9, 29, 59)) == (True, "")
    ok, err = store.submit("a", "Ann", DAY, "noon", 13, at(9, 30, 0))
    assert not ok and "locked" in err
    assert store.submit("a", "Ann", DAY, "close", -5, at(11, 59, 59))[0]
    assert not store.submit("a", "Ann", DAY, "close", -6, at(12, 0, 0))[0]
    assert store.mine("a", DAY) == {"noon": 12.0, "close": -5.0}


def test_no_guesses_on_a_holiday(store):
    ok, err = store.submit("a", "Ann", "2026-11-26", "noon", 1, at(8, 0, day="2026-11-26"))
    assert not ok and err == "no game today"


def test_edit_keeps_first_submit_time_for_the_tie_break(store):
    store.submit("a", "Ann", DAY, "noon", 10, at(8, 0))
    store.submit("b", "Bo", DAY, "noon", -10, at(8, 5))
    store.submit("a", "Ann", DAY, "noon", -10, at(9, 0))       # a edits onto b's number later
    rows = {p["username"]: p for p in store.predictions(DAY, "noon")}
    assert rows["a"]["submitted_ts"] == at(8, 0).timestamp() and rows["a"]["value"] == -10.0
    assert game.rank(list(rows.values()), -10.0)[0]["username"] == "a"


def test_board_hides_other_guesses_until_the_lock(store):
    store.submit("a", "Ann", DAY, "noon", 10, at(8, 0))
    store.submit("b", "Bo", DAY, "noon", 20, at(8, 1))
    before = store.board(DAY, "noon", at(9, 0), live_value=5.0, viewer="a")
    assert before["hidden"] and [r["name"] for r in before["rows"]] == ["Ann"] and before["n_players"] == 2
    assert len(store.board(DAY, "noon", at(9, 0), 5.0, viewer="a", reveal_all=True)["rows"]) == 2
    after = store.board(DAY, "noon", at(10, 0), live_value=18.0, viewer="a")
    assert not after["hidden"] and [r["name"] for r in after["rows"]] == ["Bo", "Ann"]
    assert all("username" not in r for r in after["rows"])          # display names only


# ---------------------------------------------------------------- settlement
def book(updated_dt, pl):
    return {"updated": updated_dt.timestamp(), "mode": "paper",
            "account": {"day_pl": pl, "equity": 10000 + pl, "last_equity": 10000}}


def test_pick_snapshot_needs_a_book_written_at_or_after_the_cutoff():
    cut = at(12, 0).timestamp()
    assert game.pick_snapshot(book(at(11, 59, 50), 5.0), cut) is None             # before the cutoff
    assert game.pick_snapshot(book(at(12, 0, 15), 5.0), cut)["day_pl"] == 5.0
    assert game.pick_snapshot(book(at(12, 4, 0), 5.0), cut) is None               # outside the grace window
    assert game.pick_snapshot({"updated": cut + 1, "account": {}}, cut) is None   # no number


def today_only(out):
    return [x for x in out if x[0] == DAY]          # settle_due also voids yesterday's unsnapshotted rounds


def test_settle_due_settles_from_the_first_fresh_book_and_ranks(store):
    store.submit("a", "Ann", DAY, "noon", 10, at(8, 0))
    store.submit("b", "Bo", DAY, "noon", -30, at(8, 1))
    assert today_only(game.settle_due(store, book(at(11, 59, 55), -25.0), at(12, 0, 5))) == []      # stale: wait
    assert today_only(game.settle_due(store, book(at(12, 0, 12), -25.0), at(12, 0, 15))) == [(DAY, "noon", "settled")]
    b = store.board(DAY, "noon", at(12, 1), live_value=99.0)
    assert b["phase"] == "settled" and b["target"] == -25.0 and b["rows"][0]["name"] == "Bo"
    assert today_only(game.settle_due(store, book(at(12, 0, 30), 1.0), at(12, 0, 35))) == []   # idempotent
    log = [json.loads(x) for x in store.snap_log.read_text().splitlines() if DAY in x]
    assert log[-1]["actual_pl"] == -25.0


def test_settle_due_voids_when_the_desk_is_silent(store):
    store.submit("a", "Ann", DAY, "close", 10, at(10, 0))
    out = game.settle_due(store, book(at(15, 50), 3.0), at(16, 3, 1))
    assert (DAY, "close", "void") in out
    assert store.round_status(DAY, "close", at(16, 5))["phase"] == "void"


def test_live_pl_hides_a_stale_number():
    now = at(10, 0).timestamp()
    assert game.live_pl(book(at(9, 59, 30), 7.0), now)["value"] == 7.0
    assert game.live_pl(book(at(9, 50), 7.0), now)["value"] is None


def test_standings_count_wins_and_average_error(store):
    store.submit("a", "Ann", DAY, "noon", 10, at(8, 0))
    store.submit("b", "Bo", DAY, "noon", 0, at(8, 1))
    store.submit("a", "Ann", DAY, "close", 0, at(10, 0))
    store.submit("b", "Bo", DAY, "close", 50, at(10, 1))
    store.settle(DAY, "noon", {"day_pl": 9.0, "updated": at(12, 0).timestamp()}, 0)
    store.settle(DAY, "close", {"day_pl": 40.0, "updated": at(16, 0).timestamp()}, 0)
    t = {r["name"]: r for r in store.standings("2026-11-01", "2026-11-30")}
    assert t["Ann"]["wins"] == 1 and t["Bo"]["wins"] == 1
    assert t["Ann"]["avg_err"] == round((1 + 40) / 2, 2) and t["Bo"]["avg_err"] == round((9 + 10) / 2, 2)


# ---------------------------------------------------------------- accounts and the player gate
def test_open_signup_makes_an_active_player(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    auth.request_account("owner", "Passw0rd!x", "o@example.com", "Owner")
    prof, err = auth.request_account("pat", "Passw0rd!x", "p@example.com", "Pat", open_signup=True)
    assert not err and prof["status"] == "active" and prof["role"] == "player"
    assert auth.is_player("pat") and not auth.is_player("owner")
    prof2, _ = auth.request_account("quin", "Passw0rd!x", "q@example.com", "Quin")
    assert prof2["status"] == "pending" and not auth.is_player("quin")


@pytest.fixture
def player_client(tmp_path, monkeypatch):
    import dashboard as d
    from fastapi.testclient import TestClient
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    auth.request_account("owner", "Passw0rd!x", "o@example.com", "Owner")
    auth.request_account("pat", "Passw0rd!x", "p@example.com", "Pat", open_signup=True)
    monkeypatch.setattr(d, "is_auth_required", lambda: True)
    monkeypatch.setattr(d, "record_login", lambda *a, **k: None)
    monkeypatch.setattr(d, "record_traffic_hit", lambda *a, **k: None)
    monkeypatch.setattr(d, "_game_store_obj", game.Store(tmp_path / "g.sqlite", None))
    c = TestClient(d.app)
    assert c.post("/auth/login", json={"username": "pat", "password": "Passw0rd!x"}).status_code == 200
    return c


def test_player_sees_the_game_and_nothing_of_the_desk(player_client):
    assert player_client.get("/api/game/state").status_code == 200
    assert player_client.get("/api/game/desk").status_code == 200
    for path in ("/api/config", "/api/state", "/api/login-log"):
        assert player_client.get(path).status_code == 403, path
    r = player_client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/play"


def test_player_is_refused_on_the_websocket_feed(player_client):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with player_client.websocket_connect("/ws") as ws:
            ws.receive_text()


def test_logged_out_play_redirects_to_login(tmp_path, monkeypatch):
    import dashboard as d
    from fastapi.testclient import TestClient
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(d, "is_auth_required", lambda: True)
    monkeypatch.setattr(d, "record_traffic_hit", lambda *a, **k: None)
    c = TestClient(d.app)
    r = c.get("/play", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert c.get("/api/game/state").status_code == 401


def test_desk_view_shows_positions_and_a_feed_without_strategy_internals():
    p = {"updated": 100.0,
         "entry_book": [
             {"symbol": "AAOI", "is_position": True, "qty": 10, "price": 41.5, "avg_entry": 41.2, "local_stop": 40.9,
              "pl": 3.0, "plpc": 0.73, "score": 9, "block_reason": "x", "reason": "secret", "source": "momentum"},
             {"symbol": "WATCH", "is_position": False, "status": "watch", "price": 5.0}],
         "day_history": {"rows": [
             {"symbol": "BE", "entry_price": 20.0, "entry_time": 10.0, "qty": 5,
              "exits": [{"price": 19.0, "time": 20.0, "qty": 5, "reason": "stop"}]}]}}
    v = game.desk_view(p)
    assert [x["symbol"] for x in v["positions"]] == ["AAOI"]
    assert v["positions"][0]["stop"] == 40.9 and v["positions"][0]["last"] == 41.5
    assert set(v["positions"][0]) == {"symbol", "qty", "last", "entry", "stop", "pl", "plpc"}
    assert [(e["side"], e["symbol"]) for e in v["feed"]] == [("sell", "BE"), ("buy", "BE")]
    assert v["feed"][0]["pl"] == -5.0 and v["feed"][0]["why"] == "stopped out"
    assert "secret" not in json.dumps(v) and "momentum" not in json.dumps(v)


def test_desk_view_falls_back_to_broker_positions():
    v = game.desk_view({"positions": {"COLD": {"qty": 39, "avg_entry": 16.81, "current": 16.65, "pl": -6.3, "plpc": -0.96}}})
    assert v["positions"][0]["symbol"] == "COLD" and v["positions"][0]["stop"] is None and v["feed"] == []
