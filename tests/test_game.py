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
TEST_PW = "Passw0rd!x"  # pragma: allowlist secret  (throwaway login for a temp user file)


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
    """A desk book whose desk trades sum to `pl`, plus a carried-in holding the game must ignore."""
    return {"updated": updated_dt.timestamp(), "mode": "paper",
            "account": {"day_pl": pl - 40.0, "equity": 10000 + pl, "last_equity": 10000},
            "day_history": {"day": updated_dt.date().isoformat(), "rows": [
                {"symbol": "DESK", "entry_time": updated_dt.timestamp() - 600, "pl": pl},
                {"symbol": "CARRY", "entry_time": None, "pl": -40.0}]}}


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
    auth.request_account("owner", TEST_PW, "o@example.com", "Owner")
    prof, err = auth.request_account("pat", TEST_PW, "p@example.com", "Pat", open_signup=True)
    assert not err and prof["status"] == "active" and prof["role"] == "player"
    assert auth.is_player("pat") and not auth.is_player("owner")
    prof2, _ = auth.request_account("quin", TEST_PW, "q@example.com", "Quin")
    assert prof2["status"] == "pending" and not auth.is_player("quin")


@pytest.fixture
def player_client(tmp_path, monkeypatch):
    import dashboard as d
    from fastapi.testclient import TestClient
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    auth.request_account("owner", TEST_PW, "o@example.com", "Owner")
    auth.request_account("pat", TEST_PW, "p@example.com", "Pat", open_signup=True)
    monkeypatch.setattr(d, "is_auth_required", lambda: True)
    monkeypatch.setattr(d, "record_login", lambda *a, **k: None)
    monkeypatch.setattr(d, "record_traffic_hit", lambda *a, **k: None)
    monkeypatch.setattr(d, "_game_store_obj", game.Store(tmp_path / "g.sqlite", None))
    c = TestClient(d.app)
    assert c.post("/auth/login", json={"username": "pat", "password": TEST_PW}).status_code == 200
    return c


def test_player_sees_the_game_and_nothing_of_the_desk(player_client):
    assert player_client.get("/api/game/state").status_code == 200
    assert player_client.get("/api/game/desk").status_code == 200
    for path in ("/api/config", "/api/state", "/api/login-log"):
        assert player_client.get(path).status_code == 403, path
    r = player_client.get("/", follow_redirects=False)
    assert r.status_code == 200 and "Call the P&amp;L" in r.text            # the landing page IS the game
    r = player_client.get("/desk", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"         # the desk screen is not for players


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


def _settled_day(store, day, noon_actual, close_actual, guesses):
    """guesses: {username: (noon_value, close_value)}; display name = username title-cased."""
    for k, (u, (nv, cv)) in enumerate(guesses.items()):
        store.submit(u, u.title(), day, "noon", nv, at(8, k, day=day))
        store.submit(u, u.title(), day, "close", cv, at(10, k, day=day))
    store.settle(day, "noon", {"day_pl": noon_actual, "updated": at(12, 0, day=day).timestamp()}, 0)
    store.settle(day, "close", {"day_pl": close_actual, "updated": at(16, 0, day=day).timestamp()}, 0)


def test_crown_goes_to_the_latest_winner_and_marks_their_rows(store):
    _settled_day(store, DAY, -10.0, 25.0, {"ann": (-9.5, 0.0), "bo": (5.0, 24.0)})
    w = store.winners()
    assert [(x["slot"], x["name"]) for x in w] == [("close", "Bo"), ("noon", "Ann")]
    assert w[0]["abs_err"] == 1.0 and w[0]["margin"] == 24.0 and w[0]["n_players"] == 2
    assert store.crown()["name"] == "Bo"
    b = store.board(DAY, "noon", at(13, 0), None, crown_user="bo")
    assert [(r["name"], r["crown"]) for r in b["rows"]] == [("Ann", False), ("Bo", True)]


def test_hall_records_and_no_usernames(store):
    _settled_day(store, DAY, -10.0, 25.0, {"ann": (-9.5, 0.0), "bo": (5.0, 24.0), "cy": (-30.0, 26.5)})
    _settled_day(store, "2026-11-19", 3.0, 7.0, {"ann": (3.1, 7.2), "bo": (9.0, 0.0)})
    h = store.hall("2026-11-19")
    r = h["records"]
    assert r["closest_call"]["name"] == "Ann" and r["closest_call"]["abs_err"] == 0.1
    assert r["most_wins_ever"] == {"name": "Ann", "wins": 3}
    assert r["month_leader"]["name"] == "Ann" and r["biggest_field"]["n_players"] == 3
    assert len(h["winners"]) == 4 and all("username" not in w for w in h["winners"])


def test_hall_is_empty_before_any_round(store):
    assert store.hall(DAY) == {"winners": [], "records": {}} and store.crown() is None


def test_watch_view_is_plain_english_and_ordered():
    p = {"entry_book": [
        {"symbol": "FAR", "status": "watching", "price": 10.0, "pct_change": 1.2, "exhaustion": 40, "exhaustion_state": "heating",
         "exh_seat_class": "far", "g_spread": 0.3, "pctr": -70, "score": 0.9, "source": "movers"},
        {"symbol": "SQ", "status": "watching", "price": 20.0, "pct_change": 3.0, "exhaustion": 75, "exhaustion_state": "overbought",
         "exh_seat_class": "square"},
        {"symbol": "GO", "status": "watching", "ready": True, "price": 30.0, "exhaustion": 60, "exh_seat_class": "square"},
        {"symbol": "OWN", "is_position": True, "price": 5.0}]}
    w = game.watch_view(p)
    assert [x["symbol"] for x in w] == ["GO", "SQ", "FAR"]
    assert [x["stage"] for x in w] == ["buying now", "in the buy zone", "watching"]
    assert w[1]["trend"] == "hot" and w[2]["heat"] == 40 and w[2]["chg"] == 1.2
    assert set(w[0]) == {"symbol", "last", "chg", "heat", "trend", "stage", "ready"}
    assert "movers" not in json.dumps(w) and "pctr" not in json.dumps(w)


def test_desk_day_pl_ignores_carried_in_holdings_and_refuses_partial_numbers():
    t = at(11, 45)
    p = book(t, 13.19)
    assert game.desk_day_pl(p) == 13.19 and abs(p["account"]["day_pl"] + 26.81) < 1e-9
    p["day_history"]["rows"].append({"symbol": "OPEN", "entry_time": t.timestamp(), "pl": None})
    assert game.desk_day_pl(p) is None                                   # a desk row without P&L: no number
    stale = book(t, 5.0); stale["day_history"]["day"] = "2026-11-17"
    assert game.desk_day_pl(stale) is None                               # yesterday's history: no number


def test_landing_page_is_the_game_and_the_desk_moved(tmp_path, monkeypatch):
    import dashboard as d
    from fastapi.testclient import TestClient
    monkeypatch.setattr(auth, "_USERS_FILE", tmp_path / "users.json")
    auth.request_account("owner", TEST_PW, "o@example.com", "Owner")      # first account: admin
    monkeypatch.setattr(d, "is_auth_required", lambda: True)
    monkeypatch.setattr(d, "record_login", lambda *a, **k: None)
    monkeypatch.setattr(d, "record_traffic_hit", lambda *a, **k: None)
    c = TestClient(d.app)
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/desk", follow_redirects=False).headers["location"] == "/login"
    assert c.post("/auth/login", json={"username": "owner", "password": TEST_PW}).status_code == 200
    home = c.get("/")
    assert home.status_code == 200 and 'id="adm"' in home.text and "/desk#admin" in home.text
    desk = c.get("/desk")
    assert desk.status_code == 200 and "data-admin-btn" in desk.text and 'href="/play"' not in desk.text
