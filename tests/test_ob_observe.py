"""Observe-only order blocks at the arm decision (ob_observe.py, 2026-10-05).

Two things must hold, per the operator's approval (docs/HANDOFF_2026-10-05_GROK.md):
  1. the knob never changes an arm decision, a placement, its size or its timing;
  2. the desk's numbers match the port (tools/order_blocks.py via the study's
     flags_for) on a recorded bar series.
"""
import copy
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "studies"))

import ob_observe  # noqa: E402
import ai_entry_watch as ew  # noqa: E402
from tests.test_ai_entry_watch import (  # noqa: E402
    _armable_rec, _last_cfg, _poll_cfg, _patch_trading_ready,
)

FIX = os.path.join(ROOT, "tests", "fixtures", "ob_bars_QQQ_2026-09-22.json")
OB_KEYS = set(ob_observe.FIELD_KEYS)


@pytest.fixture(autouse=True)
def _clean():
    ob_observe.reset()
    yield
    ob_observe.reset()


def _fixture():
    with open(FIX) as fh:
        return json.load(fh)


# ── default off ──────────────────────────────────────────────────────────────

def test_knob_defaults_off():
    import config
    assert config.DEFAULT_CONFIG["ai_watch_ob_observe"] is False
    assert ob_observe.enabled({}) is False
    assert ob_observe.enabled(None) is False
    assert ob_observe.enabled(dict(config.DEFAULT_CONFIG)) is False
    assert ob_observe.enabled({"ai_watch_ob_observe": True}) is True


def test_off_writes_nothing():
    fx = _fixture()
    ob_observe.absorb_rows("QQQ", fx["bars"])
    rec = {"symbol": "QQQ", "indicator": {"pctr": -5.0}}
    q = fx["queries"][0]
    assert ew._ob_observe_stamp(rec, "QQQ", q["px"], {}, q["t"]) == {}
    assert ew._ob_observe_stamp(rec, "QQQ", q["px"], {"ai_watch_ob_observe": False}, q["t"]) == {}
    assert rec == {"symbol": "QQQ", "indicator": {"pctr": -5.0}}


# ── (2) matches the port on a recorded bar series ───────────────────────────

def test_matches_port_on_recorded_series():
    """Every query on the recorded QQQ 2026-09-21/22 series (1-minute, 04:00-16:00
    ET) reproduces the study's flags_for (charted, within 0.3%), both as stored in
    the fixture and recomputed from the port now."""
    import order_block_gate as G
    fx = _fixture()
    bars = [tuple(b) for b in fx["bars"]]
    assert ob_observe.absorb_rows(fx["symbol"], bars) == len(bars)
    qs = fx["queries"]
    live = G.flags_for(bars, 60, [(q["t"], q["px"]) for q in qs])
    n_res = n_ins = n_room = 0
    for q, port in zip(qs, live):
        got = ob_observe.fields(fx["symbol"], q["px"], q["t"])
        # stored expectation == the port today (guards against port drift)
        assert (port[0], port[1], port[4]) == (q["resist"], q["inside"], q["room_above"])
        assert got["ob_resist_0.3"] is q["resist"]
        want_room = 0.0 if q["inside"] else q["room_above"]
        if want_room is None:
            assert got["ob_room_pct"] is None
        else:
            assert got["ob_room_pct"] == pytest.approx(want_room, abs=1e-3)
        closed = sum(1 for b in bars if b[0] + 60 <= q["t"] - q["t"] % 60)
        assert got["ob_bars"] == closed
        assert got["ob_prior_day"] is True
        n_res += q["resist"]
        n_ins += q["inside"]
        n_room += q["room_above"] is not None
    # the series exercises every branch
    assert 0 < n_res < len(qs) and n_ins > 0 and 0 < n_room < len(qs)


def test_absorb_df_equals_rows_and_drops_after_hours():
    pd = pytest.importorskip("pandas")
    fx = _fixture()
    bars = [tuple(b) for b in fx["bars"]]
    idx = pd.to_datetime([b[0] for b in bars], unit="s", utc=True)
    df = pd.DataFrame({"open": [b[1] for b in bars], "high": [b[2] for b in bars],
                       "low": [b[3] for b in bars], "close": [b[4] for b in bars],
                       "volume": 1.0}, index=idx)
    # an after-hours bar (16:30 ET on the day) must be ignored
    late = pd.Timestamp("2026-09-22 16:30", tz="America/New_York").tz_convert("UTC")
    df.loc[late] = [1.0, 1.0, 1.0, 1.0, 1.0]
    ob_observe.absorb_df("QQQ", df)
    assert ob_observe.bars("QQQ") == sorted(bars)


def test_only_blocks_known_at_the_decision():
    """A decision is scored on closed bars only: appending future bars must not
    change the reading for an earlier time."""
    fx = _fixture()
    bars = [tuple(b) for b in fx["bars"]]
    q = fx["queries"][len(fx["queries"]) // 2]
    ob_observe.absorb_rows("QQQ", [b for b in bars if b[0] + 60 <= q["t"]])
    early = ob_observe.fields("QQQ", q["px"], q["t"])
    ob_observe.reset()
    ob_observe.absorb_rows("QQQ", bars)
    assert ob_observe.fields("QQQ", q["px"], q["t"]) == early


def test_cached_per_symbol_per_minute(monkeypatch):
    from tools import order_blocks as OB
    fx = _fixture()
    ob_observe.absorb_rows("QQQ", fx["bars"])
    calls = []
    real = OB.order_blocks
    monkeypatch.setattr(OB, "order_blocks", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    q = fx["queries"][5]
    m = q["t"] - q["t"] % 60
    for dt, px in ((1, q["px"]), (20, q["px"] * 1.001), (59, q["px"] * 0.999)):
        ob_observe.fields("QQQ", px, m + dt)
    assert len(calls) == 1
    ob_observe.fields("QQQ", q["px"], m + 60)
    assert len(calls) == 2


def test_errors_are_swallowed(monkeypatch):
    monkeypatch.setattr(ob_observe, "_charted_at", lambda *a, **k: 1 / 0)
    assert ob_observe.fields("QQQ", 10.0, 1e9) == {}
    rec = {"indicator": {"pctr": 1.0}}
    assert ew._ob_observe_stamp(rec, "QQQ", 10.0, {"ai_watch_ob_observe": True}, 1e9) == {}
    assert rec == {"indicator": {"pctr": 1.0}}
    assert ob_observe.fields("", 10.0, 1e9) == {} and ob_observe.fields("X", None, 1e9) == {}


# ── (1) the flag never changes the arm decision ─────────────────────────────

_OB_VARIANTS = [
    {},
    {"ob_resist_0.3": True, "ob_room_pct": 0.0, "ob_bars": 900, "ob_prior_day": True},
    {"ob_resist_0.3": True, "ob_room_pct": 0.2, "ob_bars": 50, "ob_prior_day": False},
    {"ob_resist_0.3": False, "ob_room_pct": None, "ob_bars": 900, "ob_prior_day": True},
    {"ob_resist_0.3": False, "ob_room_pct": 4.0, "ob_bars": 12, "ob_prior_day": False},
]


@pytest.mark.parametrize("ob", _OB_VARIANTS)
@pytest.mark.parametrize("ask", [26.0, 27.5, 28.0, 28.4, 30.0])
@pytest.mark.parametrize("pctr", [True, False])
def test_flag_never_changes_should_arm_buy(ob, ask, pctr):
    base = _armable_rec(pctr=pctr)
    for cfg_off in (_last_cfg(), {"ai_entry_zone_pad_pct": 0.15, "ai_min_reward_risk": 0.5,
                                  "ai_watch_arm_require_indicators": False,
                                  "ai_watch_exhaustion_rules": False}):
        r_off = copy.deepcopy(base)
        r_on = copy.deepcopy(base)
        r_on["indicator"].update(ob)
        cfg_on = dict(cfg_off, ai_watch_ob_observe=True)
        off = ew.should_arm_buy(r_off, ask=ask, bid=ask - 0.01, cfg=dict(cfg_off), now=1e12)
        on = ew.should_arm_buy(r_on, ask=ask, bid=ask - 0.01, cfg=cfg_on, now=1e12)
        assert on == off


def _seed_resistance_at(sym, price, inside=True):
    """Seed `sym` with the recorded series rescaled so `price` sits INSIDE the
    newest charted resistance block (inside=False: a moment with NO resistance
    reading at the equivalent price)."""
    fx = _fixture()
    if not inside:
        q = next(q for q in fx["queries"] if not q["resist"])
        bars = [tuple(b) for b in fx["bars"] if b[0] + 60 <= q["t"]]
        k = price / q["px"]
        ob_observe.absorb_rows(sym, [(t, o * k, h * k, lo * k, c * k) for t, o, h, lo, c in bars])
        return
    q = next(q for q in fx["queries"] if q["inside"])
    bars = [tuple(b) for b in fx["bars"] if b[0] + 60 <= q["t"]]
    ob_observe.absorb_rows("TMP", bars)
    ch, _, _ = ob_observe._charted_at("TMP", 1e12)
    res = [b for b in ch if (b.kind == "bear" and not b.breaker) or (b.kind == "bull" and b.breaker)]
    assert res
    k = price / ((res[0].top + res[0].btm) / 2)
    ob_observe.absorb_rows(sym, [(t, o * k, h * k, lo * k, c * k) for t, o, h, lo, c in bars])


def _run_poll(tmp_path, monkeypatch, cfg_extra, sig):
    import ai_positions as cp
    import ai_trading as gt
    monkeypatch.setattr(ew, "WATCH_STATE_PATH", tmp_path / "watch.json")
    ew._structure_call_ts.clear()
    ew.save_watch({
        "SMCI": {
            "symbol": "SMCI", "status": "watching", "agreement": True,
            "reason": "test", "score": 8.0, "structure_ts": 1e12,
            "structure": {
                "decision": "WAIT", "wait_kind": "wait_for_zone",
                "entry_low": 27.0, "entry_high": 29.0,
                "stop_price": 25.0, "target_1": 36.0, "reward_risk": 3.5,
                "scale_out_pct": 40,
            },
        }
    })
    _patch_trading_ready(monkeypatch, gt)
    if sig is not None:
        monkeypatch.setattr(ew, "_engine_indicator_map", lambda: {"SMCI": dict(sig)})
    placed = []

    def fake_place(sym, decision, equity, **kw):
        placed.append((sym, copy.deepcopy(decision), equity, dict(kw)))
        return {"ok": True, "stop_price": 25.0, "target_1": 36.0}

    monkeypatch.setattr(cp, "place_scaled_entry", fake_place)
    ew.poll_once(cfg=_poll_cfg(**cfg_extra), now=1e12 + 10)
    return placed, ew.load_watch()


def _strip(d):
    d = {k: v for k, v in d.items() if k not in OB_KEYS}
    if isinstance(d.get("features"), dict):
        d["features"] = {k: v for k, v in d["features"].items() if k not in OB_KEYS}
    return d


_SIG = {"status": "ok", "cm_ok": True, "pctr_ok": True, "cm_rsi_rising": True,
        "macd_ok": True, "pctr": -5.0, "pctr_slow": -8.0, "pctr_rising": True,
        "pctr_slow_rising": True, "sell_signal": False, "buy_signal": True,
        "proximity_pct": 100.0, "bars_src": "realtime", "bars_age_sec": 1.0}


@pytest.mark.parametrize("sig", [None, _SIG])
def test_flag_never_changes_the_placement(tmp_path, monkeypatch, sig):
    """Same poll with the knob off and on, price INSIDE a resistance block:
    the same name is placed with the same decision, equity and kwargs (price,
    size inputs); only the observe-only fields are added."""
    _seed_resistance_at("SMCI", 28.0)
    off, st_off = _run_poll(tmp_path, monkeypatch, {}, sig)
    on, st_on = _run_poll(tmp_path, monkeypatch, {"ai_watch_ob_observe": True}, sig)
    assert [p[0] for p in off] == [p[0] for p in on] == ["SMCI"]
    (_, d_off, eq_off, kw_off), (_, d_on, eq_on, kw_on) = off[0], on[0]
    assert eq_on == eq_off and kw_on == kw_off
    assert _strip(d_on) == _strip(d_off)
    assert not (OB_KEYS & set(d_off)) and not (OB_KEYS & set(d_off.get("features") or {}))
    # on: the fields are there, on the decision and on the feature vector
    assert d_on["ob_resist_0.3"] is True and d_on["ob_room_pct"] == 0.0
    assert d_on["features"]["ob_resist_0.3"] is True
    assert st_on["SMCI"]["status"] == st_off["SMCI"]["status"]
    ind_on = st_on["SMCI"].get("indicator") or {}
    ind_off = st_off["SMCI"].get("indicator") or {}
    assert not (OB_KEYS & set(ind_off))
    if sig is None:
        # never creates an indicator map that was not there
        assert "indicator" not in st_on["SMCI"] or not (OB_KEYS & set(ind_on))
    else:
        # whitelist: the poll's indicator dict carries the fields
        assert ind_on.get("ob_resist_0.3") is True


def test_shadow_and_arm_pass_rows_carry_fields_only_when_present():
    rec = {"symbol": "AAA", "status": "watching", "structure": {},
           "indicator": {"pctr": -5.0, "ob_resist_0.3": True, "ob_room_pct": 0.0,
                         "ob_bars": 10, "ob_prior_day": False}}
    row = ew._shadow_row(rec, price=10.0, price_src="quote", arm_ok=True,
                         arm_why="x", now=1e12)
    assert row["ob_resist_0.3"] is True and row["ob_room_pct"] == 0.0
    rec2 = {"symbol": "AAA", "status": "watching", "structure": {}, "indicator": {"pctr": -5.0}}
    row2 = ew._shadow_row(rec2, price=10.0, price_src="quote", arm_ok=True,
                          arm_why="x", now=1e12)
    assert not (OB_KEYS & set(row2))


# ── the HARD skip (operator override 2026-10-05): ai_watch_ob_resist_skip ────

def test_skip_knob_defaults_off():
    import config
    assert config.DEFAULT_CONFIG["ai_watch_ob_resist_skip"] is False
    assert ew._ob_resist_skip_on({}) is False
    assert ew._ob_resist_refusal({"ob_resist_0.3": True}, {}) is False
    assert ew._ob_resist_refusal({"ob_resist_0.3": True}, {"ai_watch_ob_resist_skip": True}) is True
    assert ew._ob_resist_refusal({"ob_resist_0.3": False}, {"ai_watch_ob_resist_skip": True}) is False
    assert ew._ob_resist_refusal({}, {"ai_watch_ob_resist_skip": True}) is False      # fail open
    assert ew._ob_resist_refusal(None, {"ai_watch_ob_resist_skip": True}) is False


@pytest.mark.parametrize("sig", [None, _SIG])
def test_skip_refuses_inside_resistance(tmp_path, monkeypatch, sig):
    _seed_resistance_at("SMCI", 28.0)
    off, _ = _run_poll(tmp_path, monkeypatch, {}, sig)
    on, st = _run_poll(tmp_path, monkeypatch, {"ai_watch_ob_resist_skip": True}, sig)
    assert [p[0] for p in off] == ["SMCI"]
    assert on == []
    assert st["SMCI"]["status"] == "watching"
    assert st["SMCI"].get("block_code") == "ob_resist"


@pytest.mark.parametrize("sig", [None, _SIG])
def test_skip_does_not_touch_names_without_resistance(tmp_path, monkeypatch, sig):
    _seed_resistance_at("SMCI", 28.0, inside=False)
    off, _ = _run_poll(tmp_path, monkeypatch, {}, sig)
    on, _ = _run_poll(tmp_path, monkeypatch, {"ai_watch_ob_resist_skip": True}, sig)
    assert [p[0] for p in off] == [p[0] for p in on] == ["SMCI"]
    (_, d_off, eq_off, kw_off), (_, d_on, eq_on, kw_on) = off[0], on[0]
    assert eq_on == eq_off and kw_on == kw_off and _strip(d_on) == _strip(d_off)
    assert d_on["ob_resist_0.3"] is False


def test_skip_fails_open_without_bars(tmp_path, monkeypatch):
    off, _ = _run_poll(tmp_path, monkeypatch, {}, None)
    on, _ = _run_poll(tmp_path, monkeypatch, {"ai_watch_ob_resist_skip": True}, None)
    assert [p[0] for p in off] == [p[0] for p in on] == ["SMCI"]
    assert _strip(on[0][1]) == _strip(off[0][1]) and on[0][3] == off[0][3]


def test_breakout_dist_recent_bear_breaker_only():
    from tools.order_blocks import Block
    now = 10_000.0
    recent = Block("bear", top=100.0, btm=99.0, origin_ts=0.0, known_ts=1.0, breaker=True, break_ts=now - 300)
    old = Block("bear", top=99.5, btm=99.0, origin_ts=0.0, known_ts=1.0, breaker=True, break_ts=now - 2000)
    live = Block("bear", top=101.0, btm=100.5, origin_ts=0.0, known_ts=1.0)
    assert ob_observe.breakout_dist([recent, old, live], 100.2, now) == pytest.approx(0.2)
    assert ob_observe.breakout_dist([old, live], 100.2, now) is None          # break older than 15 min
    assert ob_observe.breakout_dist([recent], 99.9, now) is None               # price back under the top
