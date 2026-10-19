"""turn_open_score: the pre-registered words and guards. No market data."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))
import turn_open_score as T  # noqa: E402


def _w(**k):
    base = dict(net_a=1.0, net_b=2.0, net_all_t=2.5, sip_a=1.0, sip_b=1.0, sip_all_t=2.2,
                val_a=5.0, val_b=5.0, val_t=1.6, days=9, trades=500, sval_a=4.0, sval_b=4.0, sval_t=1.6)
    base.update(k)
    return T.word(**base)


def test_words():
    assert _w() == "PASS"
    assert _w(val_b=-1.0) == "PROMISING"
    assert _w(sip_all_t=1.6) == "PROMISING"
    assert _w(sip_b=-0.5) == "FAIL"          # a dashboard-price edge that the SIP re-price does not keep
    assert _w(net_a=-1.0) == "FAIL"
    assert _w(sval_b=-1.0) == "PROMISING"      # SIP-priced VALUE must beat live too for a PASS
    assert _w(days=7) == "UNDERPOWERED" and _w(trades=99) == "UNDERPOWERED"


def test_the_lane_must_have_fired_and_alone():
    ok = {"d1": [{"arm_why": "last_any_turn"}], "d2": [{"arm_why": "any_turn"}]}
    assert T.lane_problems("turn5", ok) == []
    assert T.lane_problems("turn5", {"d1": []}) == ["turn5 d1: no turn-lane opens"]
    assert "non-turn" in T.lane_problems("turn5", {"d1": [{"arm_why": "last_any_turn"}, {"arm_why": "last_presquare"}]})[0]


def test_stop_exits_and_weights():
    assert T.stop_exit({"reason": "local_trail"}) and not T.stop_exit({"reason": "eod_flatten"})
    assert T.WEIGHT["turn20"] == 0.25 and T.WEIGHT["live"] == 1.0


def test_mde():
    assert abs(T.mde(10.0, 9) - 2 * 10 / 3) < 1e-9 and T.mde(None, 9) is None


def test_sip_reprice_uses_the_open_after(tmp_path):
    s = T.SipBars(str(tmp_path / "c.json"))
    s.cache["X|2026-10-06"] = [[1000.0, 10.0], [1060.0, 10.1], [1120.0, 10.2]]
    import datetime
    day = datetime.datetime.fromtimestamp(1000.0, T.ET).strftime("%Y-%m-%d")
    s.cache[f"X|{day}"] = s.cache["X|2026-10-06"]
    assert s.open_after("X", 1000.0) == 10.1 and s.open_after("X", 1130.0) is None
    r = T.trade_lines({"symbol": "X", "ret": 0.01, "entry_ts": 1000.0, "exit_ts": 1060.0, "reason": "local_trail"}, 3.0, s)
    assert abs(r["net"] - 97.0) < 1e-9 and abs(r["sip"] - ((10.2 / 10.1 - 1) * 1e4 - 3.0)) < 1e-9
    assert abs(r["slip"] - 90.0) < 1e-9


def test_halves_cover_the_sessions():
    assert len(T.HALF_A) + len(T.HALF_B) == 9 and not set(T.HALF_A) & set(T.HALF_B)
