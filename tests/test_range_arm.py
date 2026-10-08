"""Range entry / exit (docs/studies/range_arm_replay_prereg.json, docs/briefs/range_arm_build.md).

Pure-function tests for every entry and exit rule with the near-misses the brief names, the live and replay
wiring (the replay calls the same functions), the synchronous order-block store on recorded IEX bars, and the
control scorer. No network."""
import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import ai_entry_watch as ew
import ai_positions as cp
import config
import learn_stamps
import ob_observe

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools" / "studies"))
import range_arm_control as rac  # noqa: E402
import replay_session as rp  # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 9, 11, 0, 30, tzinfo=ET).timestamp()
CFG = {"ai_watch_range_arm": True, "ai_watch_range_sup_band_pct": 0.30, "ai_watch_range_min_room_pct": 0.40,
       "ai_watch_range_pr_max": -20.0, "ai_watch_range_engine_max_age_sec": 90.0}


@pytest.fixture(autouse=True)
def _clean():
    ob_observe.reset()
    ew.range_arm_reset()
    yield
    ob_observe.reset()
    ew.range_arm_reset()


def _ob(**kw):
    f = {"ob_bars": 500, "bar_age_sec": 30.0, "stale_bar": False, "stale_absorb": False,
         "known_ts": NOW - 600, "sup_pct": 0.10, "s_btm": 99.5, "room_pct": 0.80, "r_btm": 100.9}
    f.update(kw)
    return f


def _sig(pctr=-60.0, age=20.0):
    return {"pctr": pctr, "bars_age_sec": age}


PREV = (round(NOW - 80.0), -70.0)    # the previous distinct read: an older bar, lower %R


# ── config ───────────────────────────────────────────────────────────────────

def test_knobs_default_off_and_are_fingerprinted():
    d = config.DEFAULT_CONFIG
    assert d["ai_watch_range_arm"] is False and d["ai_exit_range"] is False
    assert (d["ai_watch_range_sup_band_pct"], d["ai_watch_range_min_room_pct"], d["ai_watch_range_pr_max"],
            d["ai_watch_range_engine_max_age_sec"], d["ai_exit_range_res_pad_pct"],
            d["ai_exit_range_time_min"]) == (0.30, 0.40, -20.0, 90.0, 0.02, 30.0)
    keys = ("ai_watch_range_arm", "ai_watch_range_sup_band_pct", "ai_watch_range_min_room_pct",
            "ai_watch_range_pr_max", "ai_watch_range_engine_max_age_sec", "ai_exit_range",
            "ai_exit_range_res_pad_pct", "ai_exit_range_time_min")
    for k in keys:
        assert k in learn_stamps._FINGERPRINT_KEYS and k in config._EFFECTIVE_KEYS and k in config.SAFE_CONFIG_KEYS
    base = {"paper": True}
    assert learn_stamps.config_fingerprint(base) != learn_stamps.config_fingerprint(
        dict(base, ai_watch_range_arm=True))


def test_bot_config_turns_nothing_on():
    live = json.loads((ROOT / "config" / "bot_config.json").read_text())
    assert not live.get("ai_watch_range_arm") and not live.get("ai_exit_range")


# ── entry: pure rule ─────────────────────────────────────────────────────────

def test_range_arm_ok_passes_on_the_prereg_case():
    assert ew.range_arm_ok(_ob(), _sig(), PREV, CFG, NOW) == (True, "range_arm")


@pytest.mark.parametrize("ob, sig, prev, why", [
    (_ob(sup_pct=0.31), _sig(), PREV, "range_sup_far"),                 # 0.31% above support
    (_ob(sup_pct=None, s_btm=None), _sig(), PREV, "range_no_support"),
    (_ob(room_pct=0.39), _sig(), PREV, "range_room"),                   # 0.39% room
    (_ob(room_pct=0.0, r_btm=None), _sig(), PREV, "range_in_resistance"),  # inside resistance
    (_ob(room_pct=None, r_btm=None), _sig(), PREV, "range_no_resistance"),
    (_ob(), _sig(pctr=-19.0), PREV, "range_pr_high"),                    # %R -19
    (_ob(), _sig(pctr=-20.0), PREV, "range_pr_high"),                    # < -20 is strict
    (_ob(), _sig(pctr=-75.0), PREV, "range_pr_not_rising"),              # %R not rising
    (_ob(), _sig(pctr=-70.0), PREV, "range_pr_not_rising"),              # equal is not rising
    (_ob(), _sig(), None, "range_pr_no_prev"),
    (_ob(), _sig(pctr=-60.0, age=20.0), (round(NOW - 20.0), -60.0), "range_pr_same_read"),  # same key twice
    (_ob(), _sig(age=91.0), PREV, "range_engine_stale"),                 # stale engine read
    (_ob(), {"pctr": -60.0}, PREV, "range_no_engine"),                   # no bars_age_sec = no read
    (_ob(stale_bar=True), _sig(), PREV, "range_ob_stale"),               # store bar > 240 s old
    (_ob(stale_absorb=True), _sig(), PREV, "range_ob_stale"),            # _ob_reading_stale
    ({}, _sig(), PREV, "range_no_ob"),
    (_ob(ob_bars=0), _sig(), PREV, "range_no_ob"),
])
def test_range_arm_near_misses(ob, sig, prev, why):
    ok, got = ew.range_arm_ok(ob, sig, prev, CFG, NOW)
    assert not ok and got == why


def test_boundaries_pass():
    assert ew.range_arm_ok(_ob(sup_pct=0.30), _sig(), PREV, CFG, NOW)[0]      # band is inclusive
    assert ew.range_arm_ok(_ob(sup_pct=0.0), _sig(), PREV, CFG, NOW)[0]       # inside support
    assert ew.range_arm_ok(_ob(room_pct=0.40), _sig(), PREV, CFG, NOW)[0]
    assert ew.range_arm_ok(_ob(), _sig(age=90.0), PREV, CFG, NOW)[0]
    assert ew.range_arm_ok(_ob(), _sig(pctr=-20.01), PREV, CFG, NOW)[0]


def test_parts_report_each_failure_alone():
    p = ew.range_arm_parts(_ob(sup_pct=0.5, room_pct=0.1), _sig(pctr=-10), PREV, CFG, NOW)
    assert (p["a"], p["b"], p["c"]) == ("range_sup_far", "range_room", "range_pr_high")


def test_engine_key_is_bar_time_plus_value_and_stable_under_replay_aging():
    row = {"pctr": -55.0, "bars_age_sec": 12.0}
    k1 = ew.range_engine_key(row, NOW)
    k2 = ew.range_engine_key(rp.aged(row, 30.0), NOW + 30.0)     # the replay ages bars_age_sec with its clock
    assert k1 == k2 == (round(NOW - 12.0), -55.0)
    assert ew.range_engine_key({"pctr": None, "bars_age_sec": 1}, NOW) is None


def test_note_read_keeps_the_previous_distinct_read():
    a, b = (100, -70.0), (160, -60.0)
    assert ew.range_note_read("X", a) is None
    assert ew.range_note_read("X", a) is None            # same key twice: still no previous read
    assert ew.range_note_read("X", b) == a
    assert ew.range_note_read("X", b) == a               # repeated checks compare to the same previous read
    assert ew.range_note_read("X", None) == a            # no read changes nothing
    assert ew.range_note_read("X", (160, -58.0)) == b    # value change = a new read


# ── entry: levels from ONE _charted_at call on recorded bars ────────────────

def _crwv_rows():
    p = ROOT / "tests/fixtures/crwv_iex_1min_20261006_1003.json.gz"
    data = json.loads(gzip.open(p, "rt").read())
    return [(datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp(), o, h, lo, c)
            for t, o, h, lo, c in data["bars"]]


CRWV_NOW = datetime(2026, 10, 6, 10, 3, 30, tzinfo=ET).timestamp()


def test_ob_reading_on_recorded_bars_matches_the_logged_fields(monkeypatch):
    ob_observe.absorb_rows("CRWV", _crwv_rows())
    calls = []
    real = ob_observe._charted_at
    monkeypatch.setattr(ob_observe, "_charted_at", lambda s, t: calls.append((s, t)) or real(s, t))
    f = ew.range_ob_reading("CRWV", 91.0, CRWV_NOW)
    assert len(calls) == 1                                    # ONE call at the check price
    assert f["r_btm"] == pytest.approx(91.575) and f["s_btm"] == pytest.approx(88.68)
    assert f["room_pct"] == ob_observe.fields("CRWV", 91.0, CRWV_NOW)["ob_room_pct"]
    assert f["sup_pct"] == ew._ob_support_pct("CRWV", 91.0, CRWV_NOW)
    assert f["known_ts"] <= CRWV_NOW - CRWV_NOW % 60          # no block known after the check minute
    assert f["bar_age_sec"] == 30.0 and not f["stale_bar"] and not f["stale_absorb"]


def test_ob_reading_inside_support_and_resistance():
    ob_observe.absorb_rows("CRWV", _crwv_rows())
    inside_s = ew.range_ob_reading("CRWV", 88.8, CRWV_NOW)
    assert inside_s["sup_pct"] == 0.0 and inside_s["s_btm"] == pytest.approx(88.68)
    inside_r = ew.range_ob_reading("CRWV", 91.6, CRWV_NOW)
    assert inside_r["room_pct"] == 0.0 and inside_r["r_btm"] is None
    assert ew.range_arm_parts(inside_r, _sig(), PREV, CFG, CRWV_NOW)["b"] == "range_in_resistance"


def test_ob_reading_goes_stale_after_240s_of_bar_time():
    ob_observe.absorb_rows("CRWV", _crwv_rows())            # newest bar starts 10:02
    late = datetime(2026, 10, 6, 10, 7, 30, tzinfo=ET).timestamp()   # newest closed bar ended 10:03 -> 270 s
    f = ew.range_ob_reading("CRWV", 91.0, late)
    assert f["stale_bar"] is True and f["bar_age_sec"] == 270.0
    assert ew.range_arm_ok(f, _sig(), PREV, CFG, late) == (False, "range_ob_stale")


def test_ob_reading_honours_ob_reading_stale(monkeypatch):
    ob_observe.absorb_rows("CRWV", _crwv_rows())
    monkeypatch.setattr(ew, "_ob_reading_stale", lambda s: True)
    f = ew.range_ob_reading("CRWV", 91.0, CRWV_NOW)
    assert f["stale_absorb"] is True and ew.range_arm_ok(f, _sig(), PREV, CFG, CRWV_NOW)[1] == "range_ob_stale"


# ── entry: the arm site ──────────────────────────────────────────────────────

def test_knob_off_keeps_the_plain_should_arm_buy_calls():
    """Every poll_once arm site is `if range_arm_enabled(cfg): range rule else: <the unchanged call>`."""
    src = (ROOT / "ai_entry_watch.py").read_text()
    for stage, plain in (("check", "ok_arm, why = should_arm_buy(rec, ask=ask_f, bid=bid_f, cfg=cfg, now=t0)"),
                         ("refresh", "ok_arm, why = should_arm_buy(rec, ask=ask_f, bid=bid_f, cfg=cfg, now=t0)"),
                         ("final", "ok_arm2, why2 = should_arm_buy(rec, ask=ask_f, bid=bid2_f, cfg=cfg, now=t0)")):
        assert f'indicators.get(sym), stage="{stage}")\n        else:\n            {plain}\n' in src
    assert not ew.range_arm_enabled({}) and not ew.range_arm_enabled(config.DEFAULT_CONFIG)


def test_range_decide_runs_the_same_rule_then_every_other_gate(monkeypatch):
    calls, gate_cfgs = [], []
    monkeypatch.setattr(ew, "range_ob_reading", lambda s, px, t: _ob())
    real_ok = ew.range_arm_ok
    monkeypatch.setattr(ew, "range_arm_ok", lambda *a: calls.append(a) or real_ok(*a))

    def gates(rec, *, ask, bid, cfg, now):
        gate_cfgs.append(cfg)
        return True, "last_exhaustion_off"
    monkeypatch.setattr(ew, "should_arm_buy", gates)
    ew.range_note_read("AAA", PREV)
    rec = {"symbol": "AAA"}
    cfg = dict(CFG, ai_watch_presquare_only=True)
    ok, why = ew.range_arm_decide(rec, "AAA", 100.0, None, cfg, NOW, _sig(), stage="final")
    assert (ok, why) == (True, "range_arm") and len(calls) == 1
    g = gate_cfgs[0]
    assert g["ai_watch_exhaustion_rules"] is False and g["ai_watch_presquare_only"] is False
    assert rec["range_arm"]["range_s_btm"] == 99.5 and rec["range_arm"]["range_r_btm"] == 100.9
    assert rec["range_arm"]["range_pctr_prev"] == -70.0
    assert set(rec["range_arm"]) == set(ew.RANGE_DECISION_KEYS) == set(cp.RANGE_POSITION_KEYS)
    assert ew.range_arm_stats()["arms"] == 1


def test_range_decide_refuses_when_another_gate_refuses(monkeypatch):
    monkeypatch.setattr(ew, "range_ob_reading", lambda s, px, t: _ob())
    monkeypatch.setattr(ew, "should_arm_buy", lambda rec, **kw: (False, "spread_wide"))
    ew.range_note_read("AAA", PREV)
    rec = {"symbol": "AAA", "range_arm": {"stale": 1}}
    assert ew.range_arm_decide(rec, "AAA", 100.0, None, CFG, NOW, _sig()) == (False, "spread_wide")
    assert "range_arm" not in rec


def test_range_rule_miss_never_reaches_the_square(monkeypatch):
    monkeypatch.setattr(ew, "range_ob_reading", lambda s, px, t: _ob(sup_pct=0.31))
    monkeypatch.setattr(ew, "should_arm_buy", lambda rec, **kw: pytest.fail("square path ran"))
    ew.range_note_read("AAA", PREV)
    assert ew.range_arm_decide({}, "AAA", 100.0, None, CFG, NOW, _sig()) == (False, "range_sup_far")


def test_per_input_counts_on_first_check_only(monkeypatch):
    monkeypatch.setattr(ew, "range_ob_reading", lambda s, px, t: _ob(sup_pct=0.5))
    ew.range_note_read("AAA", PREV)
    for stage in ("check", "refresh", "final"):
        ew.range_arm_decide({}, "AAA", 100.0, None, CFG, NOW, _sig(), stage=stage)
    st = ew.range_arm_stats()
    assert st["checks"] == 1 and st["fail_a_only"] == 1 and st["ob_reading"] == 1 and st["engine_fresh"] == 1


def test_range_arm_turns_on_the_ob_warm_fetch():
    assert ew._ob_observe_on({"ai_watch_range_arm": True}) is True


def test_place_carries_range_levels_onto_the_position():
    src = (ROOT / "ai_positions.py").read_text()
    assert src.count("for k in RANGE_POSITION_KEYS if isinstance(decision, dict) and k in decision") == 2
    assert 'place_decision.update(rec["range_arm"])' in (ROOT / "ai_entry_watch.py").read_text()


# ── exit: pure rule ──────────────────────────────────────────────────────────

XCFG = {"ai_exit_range": True, "ai_exit_range_res_pad_pct": 0.02, "ai_exit_range_time_min": 30.0}


def _pos(**kw):
    p = {"symbol": "AAA", "entry_price": 100.0, "entry_time": NOW, "range_s_btm": 99.5, "range_r_btm": 101.0,
         "entry_confirmed": True}
    p.update(kw)
    return p


def test_exit_target_stop_time():
    tgt = 101.0 * (1 - 0.0002)
    assert cp.range_exit_due(_pos(), tgt, XCFG, NOW + 60) == (True, "range_target")
    assert cp.range_exit_due(_pos(), tgt - 0.001, XCFG, NOW + 60) == (False, "")
    assert cp.range_exit_due(_pos(), 99.48, XCFG, NOW + 60) == (True, "range_stop")
    assert cp.range_exit_due(_pos(), 99.49, XCFG, NOW + 60) == (False, "")      # = S_btm - $0.01: not below
    assert cp.range_exit_due(_pos(), 100.2, XCFG, NOW + 1799) == (False, "")
    assert cp.range_exit_due(_pos(), 100.2, XCFG, NOW + 1800) == (True, "range_time")
    assert cp.range_exit_due(_pos(entry_time=None, entry_ts=NOW), None, XCFG, NOW + 1800) == (True, "range_time")


def test_exit_race_stop_wins():
    # Degenerate levels: one price is both through the stop and at the target. Stop wins.
    p = _pos(range_s_btm=101.5, range_r_btm=100.5)
    assert cp.range_exit_due(p, 101.0, XCFG, NOW + 60) == (True, "range_stop")


def test_exit_off_without_knob_or_levels():
    assert cp.range_exit_due(_pos(), 200.0, dict(XCFG, ai_exit_range=False), NOW) == (False, "")
    assert cp.range_exit_due(_pos(range_r_btm=None), 200.0, XCFG, NOW) == (False, "")
    assert not cp.range_exit_applies(_pos(range_r_btm=None), XCFG)


# ── exit: live wiring ────────────────────────────────────────────────────────

class _Broker:
    def __init__(self):
        self.closed, self.canceled = [], []

    def cancel_open_orders(self, t):
        self.canceled.append(t)

    def close_out(self, t, **k):
        self.closed.append(t)
        return {"ok": True, "order_id": "o1"}


def test_live_trail_tick_sells_only_on_the_range_rule(monkeypatch):
    br = _Broker()
    monkeypatch.setitem(sys.modules, "alpaca_trader", br)
    monkeypatch.setattr(cp, "_cfg_all", lambda: dict(XCFG, ai_local_trail_enabled=True))
    monkeypatch.setattr(cp.time, "time", lambda: NOW + 60)
    monkeypatch.setattr(cp, "local_profit_stop", lambda *a, **k: pytest.fail("ratchet ran"))
    monkeypatch.setattr(cp, "green_catchup_raise", lambda *a, **k: pytest.fail("decay leash ran"))
    pos = _pos(local_stop_price=100.4, last_seen_price=100.6)     # price under the old shelf: no local_trail
    ev, why = [], {}
    assert cp.apply_local_trail("AAA", pos, 100.3, ev, why) == (False, False)
    assert br.closed == [] and pos["local_stop_price"] == 100.4
    assert cp.apply_local_trail("AAA", pos, 101.0, ev, why) == (True, True)
    assert br.closed == ["AAA"] and pos["closing_reason"] == "range_target" and why["AAA"] == "range_target"


def test_live_trail_raise_only_never_sells(monkeypatch):
    br = _Broker()
    monkeypatch.setitem(sys.modules, "alpaca_trader", br)
    monkeypatch.setattr(cp, "_cfg_all", lambda: XCFG)
    assert cp.apply_local_trail("AAA", _pos(), 120.0, [], {}, raise_only=True) == (False, False)
    assert br.closed == []


def test_manage_skips_every_other_exit_for_a_range_position(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "tests"))
    import test_ai_positions as tap
    monkeypatch.setattr(cp, "refresh_open_position_quotes", lambda *_a, **_k: 0)
    monkeypatch.setattr(cp.time, "time", lambda: 1_000_100.0)    # the trail tick reads the wall clock

    def run(cfg, price):
        tap._seed_state(tmp_path, monkeypatch, entry_price=40.5, last_seen_price=price, peak_price=price,
                        mfe_r=0.0, local_stop_price=39.0, range_s_btm=40.0, range_r_btm=42.0,
                        entry_time=1_000_000.0, time_stop_days=None)
        monkeypatch.setattr(cp, "_cfg_all", lambda: cfg)
        monkeypatch.setattr(cp, "_entry_cfg", lambda: cfg)
        monkeypatch.setattr(cp, "_cfg_flag", lambda key, default=True: bool(cfg.get(key, default)))
        stub = tap._StubBrokerManage(order_status="new", current_price=price)
        monkeypatch.setitem(sys.modules, "alpaca_trader", stub)
        cp.manage_open_positions(now=1_000_100.0)
        return stub, json.loads(tap._state_path(tmp_path).read_text())["NVDA"]

    base = {"ai_dead_trade_min": 0.5, "ai_dead_trade_mfe_r": 0.10, "ai_position_shadow_enabled": False,
            "ai_watch_exhaustion_rules": False, "ai_sell_signal_breakeven": False, "ai_exit_min_hold_sec": 0}
    stub, st = run(base, 40.6)                                   # knob off: dead_trade (100 s > 30 s) fires
    assert st.get("closing_reason") == "dead_trade"
    stub, st = run(dict(base, **XCFG), 40.6)                     # range position: holds
    assert st.get("closing_reason") is None and "NVDA" not in stub.closed
    stub, st = run(dict(base, **XCFG), 42.0)                     # at resistance: the range target sells
    assert st.get("closing_reason") == "range_target" and "NVDA" in stub.closed


# ── replay ───────────────────────────────────────────────────────────────────

T0 = datetime(2026, 10, 7, 11, 0, tzinfo=ET).timestamp()
DEC = {"stop_price": 95.0, "range_s_btm": 99.5, "range_r_btm": 101.0, "range_sup_pct": 0.1}


def _rbroker(cfg):
    b = rp.FakeBroker(hold_sec=420, max_pos=5, equity=1e5, mode="live")
    b.enter("AAA", 100.0, T0, DEC, cfg=cfg)
    return b


class _Ew:
    def exhaustion_exit_now(self, rec, cfg, now=None):
        return True, "left_overbought"


def test_replay_range_exit_skips_every_other_exit(monkeypatch):
    cfg = dict(XCFG, ai_no_progress_flatten_enabled=True, ai_no_progress_sec=1, ai_no_progress_mfe_r=1.0,
               ai_dead_trade_min=0.01, ai_exit_rsi_dump_enabled=True, ai_exit_rsi_dump_points=1.0,
               ai_exit_rsi_dump_sec=60.0, ai_exit_rsi_dump_confirm_ticks=1)
    monkeypatch.setattr(cp, "local_profit_stop", lambda *a, **k: pytest.fail("ratchet ran"))
    monkeypatch.setattr(cp, "rsi_dump_due", lambda *a, **k: pytest.fail("%R dump ran"))
    calls = []
    real = cp.range_exit_due
    monkeypatch.setattr(cp, "range_exit_due", lambda *a: calls.append(a) or real(*a))
    b = _rbroker(cfg)
    pos = b.open["AAA"]
    assert pos["pos"]["range_r_btm"] == 101.0 and pos["range_s_btm"] == 99.5
    dash = {"tickers": [{"ticker": "AAA", "signal_proximity": {"pctr": -90.0}}]}
    assert b._live_exit(pos, 99.6, T0 + 900, dash, cfg, _Ew()) is None      # triangle / np / dead: not here
    assert calls                                                             # the desk's own function
    assert b._live_exit(pos, 101.0, T0 + 901, dash, cfg, _Ew()) == "range_target"
    assert b._live_exit(pos, 99.0, T0 + 902, dash, cfg, _Ew()) == "range_stop"
    assert b._live_exit(pos, 100.0, T0 + 1800, dash, cfg, _Ew()) == "range_time"
    eod = datetime(2026, 10, 7, 15, 50, tzinfo=ET).timestamp()
    b2 = rp.FakeBroker(hold_sec=420, max_pos=5, equity=1e5, mode="live")
    b2.enter("AAA", 100.0, eod - 60, DEC, cfg=cfg)
    assert b2._live_exit(b2.open["AAA"], 100.0, eod, dash, cfg, None) == "eod_flatten"


def test_replay_non_range_position_keeps_the_old_exits():
    cfg = dict(XCFG, ai_dead_trade_min=0.0)
    b = rp.FakeBroker(hold_sec=420, max_pos=5, equity=1e5, mode="live")
    b.enter("AAA", 100.0, T0, {"stop_price": 95.0}, cfg=cfg)
    assert b._live_exit(b.open["AAA"], 100.0, T0 + 10, {}, cfg, _Ew()) is None
    dash = {"tickers": [{"ticker": "AAA", "signal_proximity": {"pctr": -90.0}}]}
    assert b._live_exit(b.open["AAA"], 100.0, T0 + 10, dash, cfg, _Ew()) == "left_overbought"


def _day_bars_crwv():
    return {"CRWV": {"iex": [(r[0], r[1], r[2], r[3], r[4], 0.0) for r in _crwv_rows()]}}


def test_replay_ob_store_is_filled_synchronously_and_gives_readings():
    """Recorded IEX bars through the replay's feeder: the range rule has an order-block reading
    (non-empty ob_room_pct and support distance), completed minutes only."""
    feeder = rp.ObStoreFeeder(_day_bars_crwv())
    now = CRWV_NOW
    assert feeder.feed({"CRWV"}, now) == 1
    assert max(b[0] for b in ob_observe.bars("CRWV")) + 60 <= now - now % 60     # no forming minute
    f = ew.range_ob_reading("CRWV", 91.0, now)
    assert f["room_pct"] is not None and f["sup_pct"] is not None and not f["stale_bar"]
    assert ob_observe.fields("CRWV", 91.0, now)["ob_room_pct"] == f["room_pct"]
    assert feeder.feed({"CRWV"}, now + 60) == 0             # 120 s refresh cadence, as the warm worker
    assert feeder.feed({"CRWV"}, now + 120) == 1


def test_replay_ob_feed_is_deterministic():
    stores = []
    for _ in range(2):
        ob_observe.reset()
        fd = rp.ObStoreFeeder(_day_bars_crwv())
        for k in range(5):
            fd.feed({"CRWV", "NOPE"}, CRWV_NOW - 600 + 60 * k)
        stores.append((ob_observe.bars("CRWV"), ew.range_ob_reading("CRWV", 91.0, CRWV_NOW)))
    assert stores[0] == stores[1]


def test_replay_turns_the_warm_thread_off_and_blinds_pnl():
    src = (ROOT / "tools" / "replay_session.py").read_text()
    assert 'os.environ["TH_OB_WARM_OFF"] = "1"' in src
    assert "development runs are blind to P&L" in src
    assert rp.range_on({"ai_watch_range_arm": True}) and not rp.range_on({})


def test_nightly_has_the_range_arm_variant():
    sh = (ROOT / "scripts" / "tight_trail_replay.sh").read_text()
    assert "R range_arm --set ai_watch_range_arm=true --set ai_exit_range=true" in sh


# ── control and scoring ──────────────────────────────────────────────────────

DAY = "2026-10-09"


def _m(h, mi):
    return datetime(2026, 10, 9, h, mi, tzinfo=ET).timestamp()


def _trade(**kw):
    t = {"symbol": "AAA", "entry_ts": _m(11, 30) + 12, "entry_px": 100.0, "range_s_btm": 99.5,
         "range_r_btm": 100.6, "exit_ts": _m(11, 40), "exit_px": 100.2, "ret": 0.002, "reason": "range_time"}
    t.update(kw)
    return t


def _bars(start, n, px=100.0):
    return [(start + 60 * i, px, px + 0.05, px - 0.05, px, 1.0) for i in range(n)]


def test_legal_minutes_need_low_rising_pr_and_distance():
    tm = _m(11, 30)
    pr = {_m(11, 0): -50.0, _m(11, 1): -40.0, _m(11, 2): -45.0, _m(11, 25): -60.0, _m(11, 26): -50.0,
          _m(11, 40): -30.0, _m(11, 41): -10.0, _m(11, 50): -80.0, _m(11, 51): -70.0}
    # 11:01 rising; 11:02 falling; 11:26 within 10 min; 11:41 not < -20; 11:51 rising.
    assert rac.legal_minutes(pr, 11, tm) == [_m(11, 1), _m(11, 51)]


def test_pick_is_seeded_and_deterministic():
    c = [_m(11, 1), _m(11, 5), _m(11, 51)]
    a = rac.pick_minute(c, "AAA", DAY, _m(11, 30))
    assert a == rac.pick_minute(list(reversed(c)), "AAA", DAY, _m(11, 30)) and a in c
    assert rac.pick_minute([], "AAA", DAY, _m(11, 30)) is None


def test_control_falls_back_later_then_earlier_hour():
    bars = _bars(_m(10, 0), 180)
    pr_later = {_m(12, 10): -60.0, _m(12, 11): -50.0}
    assert rac.control_trade(_trade(), DAY, bars, pr_later)["fallback"] == "later_hour"
    pr_earlier = {_m(10, 10): -60.0, _m(10, 11): -50.0}
    assert rac.control_trade(_trade(), DAY, bars, pr_earlier)["fallback"] == "earlier_hour"
    assert rac.control_trade(_trade(), DAY, bars, {})["fallback"] == "none"


def test_control_entry_never_reads_bars_after_its_own_time():
    pr = {_m(11, 0): -60.0, _m(11, 1): -50.0}
    bars = _bars(_m(10, 50), 60)
    a = rac.control_trade(_trade(), DAY, bars, pr)["control"]
    assert a["minute"] == _m(11, 1) and a["entry_px"] == 100.0
    later = [(r[0], 50.0, 50.0, 50.0, 50.0, 1.0) if r[0] > _m(11, 1) else r for r in bars]
    b = rac.control_trade(_trade(), DAY, later, pr)["control"]
    assert (b["minute"], b["entry_px"]) == (a["minute"], a["entry_px"])          # entry from bar <= own minute
    earlier = [(r[0], 1.0, 1.0, 1.0, 1.0, 1.0) if r[0] < _m(11, 1) else r for r in bars]
    assert rac.control_trade(_trade(), DAY, earlier, pr)["control"] == a        # exit from later bars only


def test_control_stop_wins_a_same_bar_tie():
    pr = {_m(11, 0): -60.0, _m(11, 1): -50.0}
    bars = _bars(_m(10, 50), 12)
    bars.append((_m(11, 2), 100.0, 101.0, 99.0, 100.0, 1.0))                     # hits target AND stop
    c = rac.control_trade(_trade(), DAY, bars, pr)["control"]
    assert c["reason"] == "stop" and c["exit_px"] == pytest.approx(100.0 * (99.49 / 100.0))


def test_control_target_and_time():
    pr = {_m(11, 0): -60.0, _m(11, 1): -50.0}
    up = _bars(_m(10, 50), 12) + [(_m(11, 2), 100.0, 100.7, 99.9, 100.5, 1.0)]
    c = rac.control_trade(_trade(), DAY, up, pr)["control"]
    assert c["reason"] == "target" and c["exit_px"] == pytest.approx(100.6 * 0.9998)
    flat = _bars(_m(10, 50), 80)
    c2 = rac.control_trade(_trade(), DAY, flat, pr)["control"]
    assert c2["reason"] == "time"


def test_no_nbbo_is_charged_the_day_median_never_dropped():
    rows = [{"spr": 2.0}, {"spr": 6.0}, {"spr": 4.0}, {"spr": None}]
    assert rac.charge_spreads(rows) == 1
    assert rows[3]["spr"] == 4.0 and rows[3]["no_nbbo"] is True and len(rows) == 4
    lone = [{"spr": None}]
    assert rac.charge_spreads(lone, pooled_median=3.0) == 1 and lone[0]["spr"] == 3.0


def test_rescoring_shifts_entry_and_exit():
    t = _trade()
    prices = {("AAA", t["entry_ts"] + 10.0): 100.1, ("AAA", t["exit_ts"] + 10.0): 100.0}
    ret, ok = rac.rescored_ret(t, "plus10", prices, 10.0)
    assert ok and ret == pytest.approx(0.002 + (100.0 / 100.1 - 1.002))
    assert rac.rescored_ret(t, "fill", {}, 10.0) == (0.002, True)
    assert rac.rescored_ret(t, "next_poll", {}, 13.0) == (0.002, False)


def test_input_check_build_failure_and_determinism():
    good = {"range_arm_inputs": {"checks": 100, "ob_reading": 80, "engine_fresh": 60, "arms": 2},
            "closed": [_trade()]}
    out = rac.input_check(good, json.loads(json.dumps(good)))
    assert not out["build_failure"] and out["deterministic"] is True
    assert "ret" not in json.dumps(out) and "net" not in out                      # no P&L
    thin = {"range_arm_inputs": {"checks": 100, "ob_reading": 69, "engine_fresh": 60}, "closed": []}
    assert rac.input_check(thin)["build_failure"]
    stale = {"range_arm_inputs": {"checks": 100, "ob_reading": 80, "engine_fresh": 49}, "closed": []}
    assert rac.input_check(stale)["build_failure"]
    moved = dict(good, closed=[_trade(exit_px=100.3)])
    assert rac.input_check(good, moved)["build_failure"]


def test_score_without_read_prints_no_pnl(tmp_path, monkeypatch, capsys):
    import replay_costing as rc
    monkeypatch.setattr(rc, "load_cache", lambda: {})
    monkeypatch.setattr(rc, "save_cache", lambda c: None)
    monkeypatch.setattr(rc, "entry_spread_bp", lambda *a, **k: 3.0)
    monkeypatch.setattr(rac, "load_bars", lambda day: {})
    monkeypatch.setattr(rac.RecordedPrices, "lookup", lambda self, q: {})
    monkeypatch.setenv("SESSION_SNAPSHOT_DIR", str(tmp_path))
    (tmp_path / f"{DAY}-range_arm.json").write_text(json.dumps({"closed": [_trade()], "poll_sec": 10}))
    (tmp_path / f"{DAY}-base.json").write_text(json.dumps({"closed": [_trade(symbol="BBB")]}))
    assert rac.main(["score", DAY, "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "P&L withheld" in out and "net/trade" not in out and "VERDICT" not in out


def test_verdict_pass_and_fail_shapes():
    days = [f"2026-10-{d:02d}" for d in range(9, 21)]
    rows = [{"day": d, "symbol": f"S{i}", "net": 10.0 + (i % 3) + j % 4} for j, d in enumerate(days)
            for i in range(6)]
    good = {r: rows for r in rac.RULES}
    diffs = {r: [5.0 + 0.1 * i for i in range(12)] for r in rac.RULES}
    assert rac.verdict(good, diffs, diffs)[0] == "PASS"
    bad_rows = [dict(r, net=-r["net"]) for r in rows]
    bad = {r: bad_rows for r in rac.RULES}
    assert rac.verdict(bad, diffs, diffs)[0] == "FAIL"


def test_report_dir_untouched():
    assert os.path.exists(ROOT / "config" / "bot_config.json")


# ── poll_once end to end (the test_ob_observe poll harness) ──────────────────

def _range_poll(tmp_path, monkeypatch, cfg_extra, ob):
    from tests.test_ob_observe import _run_poll
    monkeypatch.setattr(ew, "range_ob_reading", lambda s, px, t: dict(ob))
    ew.range_note_read("SMCI", (round(1e12 + 10 - 70), -70.0))       # an earlier, lower engine read
    sig = {"pctr": -60.0, "bars_age_sec": 1.0, "bars_src": "realtime", "status": "ok"}
    return _run_poll(tmp_path, monkeypatch, cfg_extra, sig)


def test_poll_once_places_on_the_range_rule_with_levels(tmp_path, monkeypatch):
    ob = _ob(s_btm=27.9, sup_pct=0.1, r_btm=28.3, room_pct=1.071)
    placed, _st = _range_poll(tmp_path, monkeypatch, {"ai_watch_range_arm": True}, ob)
    assert [p[0] for p in placed] == ["SMCI"]
    dec = placed[0][1]
    assert dec["arm_why"] == "range_arm" and dec["range_s_btm"] == 27.9 and dec["range_r_btm"] == 28.3
    assert dec["range_pctr"] == -60.0 and dec["range_pctr_prev"] == -70.0
    st = ew.range_arm_stats()
    assert st["checks"] == 1 and st["arms"] == 1 and st["pass_abc"] == 1


def test_poll_once_range_near_miss_does_not_place(tmp_path, monkeypatch):
    placed, st = _range_poll(tmp_path, monkeypatch, {"ai_watch_range_arm": True}, _ob(sup_pct=0.31))
    assert placed == [] and st["SMCI"].get("block_code") == "range_sup_far"


def test_poll_once_knob_off_places_as_before_without_range_keys(tmp_path, monkeypatch):
    placed, _st = _range_poll(tmp_path, monkeypatch, {}, _ob())
    assert [p[0] for p in placed] == ["SMCI"]
    assert placed[0][1]["arm_why"] != "range_arm"
    assert not any(k.startswith("range_") for k in placed[0][1])
    assert ew.range_arm_stats() == {}
