"""The burst+RSI entry path. Two gates, and the tests exist to pin them shut.

This is the only module in today's work that can reach the order path, on a
rule that is in-sample over ten sessions where four of five such results
died the same day. So the tests that matter most are not "does it place
correctly" but "can it place when it should not":

  * off by default
  * enabled but dry_run still places nothing
  * both gates open and it still refuses on slots, holdings, repeats, and
    time of day
  * a broken broker cannot take the poll down

The arithmetic tests are here too, because an order with a wrong stop is
worse than no order.
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import strength_trade as st  # noqa: E402

OPEN_TS = 1788876000.0        # 2026-09-08 ~10:00 ET
PRE_TS = OPEN_TS - 3600.0     # ~09:00 ET
LATE_TS = OPEN_TS + 6 * 3600  # ~16:00 ET

LIVE = {"ai_strength_trade_enabled": True, "ai_strength_trade_dry_run": False}
DRY = {"ai_strength_trade_enabled": True}


def _sig(sym="AAA", price=10.0):
    return {
        "symbol": sym,
        "price": price,
        "signal_bar_ts": OPEN_TS - 60,
        "bar_ts": OPEN_TS - 60,
        "decision_ts": OPEN_TS - 56.6,
        "latency_sec": 3.4,
        "cm_rsi": 85.0,
        "fill_model": "next_open",
        "burst_universe": 12,
        "burst_required": True,
        "rule": "premarket_burst_rsi2",
    }


class FakeCP:
    """Only what strength_trade touches on ai_positions."""

    def __init__(self, place=None, raises=False):
        self._place = place if place is not None else {"ok": True, "order_id": "x1"}
        self._raises = raises
        self.placed = []

    def qualifies_as_entry(self, decision, **k):
        return bool(decision and decision.get("decision") == "BUY"
                    and (decision.get("entry_low") or 0) > 0
                    and (decision.get("stop_price") or 0) > 0
                    and (decision.get("target_1") or 0) > 0)

    def place_scaled_entry(self, sym, decision, equity, **k):
        if self._raises:
            raise RuntimeError("broker down")
        self.placed.append((sym, decision, equity, k))
        return self._place


class FakeGT:
    """ai_trading's real surface: has_open_position / open_position_count /
    effective_max_positions. The first draft of this module called
    cp.open_positions() and cp.account_equity(), neither of which exists —
    it would have refused every signal in silence."""

    def __init__(self, holding=(), open_n=0, desk_max=5, raises=False):
        self._holding, self._open_n = set(holding), open_n
        self._desk_max, self._raises = desk_max, raises

    def has_open_position(self, sym):
        return sym in self._holding

    def open_position_count(self):
        if self._raises:
            raise RuntimeError("no broker")
        return self._open_n

    def effective_max_positions(self, equity=None):
        return self._desk_max


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    st.reset_state()
    yield
    st.reset_state()


# ── the gates ────────────────────────────────────────────────────────────

def test_it_does_nothing_by_default():
    cp = FakeCP()
    assert st.consider([_sig()], {}, OPEN_TS, cp=cp, gt=FakeGT()) == []
    assert cp.placed == []


def test_enabled_alone_still_places_nothing():
    """One flag is one typo away from an unintended order loop."""
    cp = FakeCP()
    out = st.consider([_sig()], DRY, OPEN_TS, cp=cp, gt=FakeGT())
    assert cp.placed == []
    assert out and out[0]["action"] == "would_place"
    assert out[0]["dry_run"] is True


def test_both_gates_open_places():
    cp = FakeCP()
    out = st.consider([_sig()], LIVE, OPEN_TS, cp=cp, gt=FakeGT(), equity=236.0)
    assert len(cp.placed) == 1
    assert out[0]["action"] == "placed"
    assert cp.placed[0][0] == "AAA"


# ── refusals that still apply with both gates open ───────────────────────

def test_it_refuses_a_name_it_already_holds():
    cp = FakeCP()
    out = st.consider([_sig()], LIVE, OPEN_TS, cp=cp, gt=FakeGT(holding=["AAA"]), equity=236.0)
    assert cp.placed == []
    assert out[0]["reason"] == "already_holding"


def test_it_refuses_when_slots_are_full():
    cp = FakeCP()
    out = st.consider([_sig()], LIVE, OPEN_TS, cp=cp, gt=FakeGT(open_n=3), equity=236.0)
    assert cp.placed == []
    assert out[0]["reason"].startswith("slots_full")


def test_it_places_a_name_only_once_a_day():
    cp = FakeCP()
    st.consider([_sig()], LIVE, OPEN_TS, cp=cp, gt=FakeGT(), equity=236.0)
    out = st.consider([_sig()], LIVE, OPEN_TS + 120, cp=cp, gt=FakeGT(), equity=236.0)
    assert len(cp.placed) == 1
    assert out[0]["reason"] == "already_placed_today"


def test_it_refuses_before_the_open():
    cp = FakeCP()
    out = st.consider([_sig()], LIVE, PRE_TS, cp=cp, gt=FakeGT())
    assert cp.placed == [] and out[0]["reason"] == "before_open"


def test_it_refuses_late_in_the_session():
    cp = FakeCP()
    out = st.consider([_sig()], LIVE, LATE_TS, cp=cp, gt=FakeGT())
    assert cp.placed == [] and out[0]["reason"] == "late_session"


def test_it_refuses_an_unpriceable_signal():
    cp = FakeCP()
    out = st.consider([_sig(price=0)], LIVE, OPEN_TS, cp=cp, gt=FakeGT(), equity=236.0)
    assert cp.placed == [] and out[0]["reason"] == "unpriceable"


def test_it_refuses_with_no_equity():
    cp = FakeCP()
    out = st.consider([_sig()], LIVE, OPEN_TS, cp=cp, gt=FakeGT(), equity=0)
    assert cp.placed == [] and out[0]["reason"] == "no_equity"


# ── the order's arithmetic ───────────────────────────────────────────────

def test_the_stop_and_target_match_the_tested_rule():
    d = st.build_decision(10.0, {})
    assert d["stop_price"] == pytest.approx(9.5)      # -5%
    assert d["target_1"] == pytest.approx(10.8)       # +8%
    assert d["reward_risk"] == pytest.approx(1.6, abs=0.01)
    assert d["decision"] == "BUY"


def test_no_ladder_by_default():
    """Scale-outs measured worse on this rule at every tier tested."""
    assert st.build_decision(10.0, {})["scale_out_pct"] == 0.0


def test_a_nonsense_price_yields_no_decision():
    for bad in (0, -1, None, "abc"):
        assert st.build_decision(bad, {}) is None


def test_stop_and_target_scale_with_config():
    cfg = {"ai_strength_stop_pct": 3.0, "ai_strength_target_pct": 6.0}
    d = st.build_decision(20.0, cfg)
    assert d["stop_price"] == pytest.approx(19.4)
    assert d["target_1"] == pytest.approx(21.2)


# ── it cannot take the poll down ─────────────────────────────────────────

def test_a_broken_broker_is_contained():
    cp = FakeCP(raises=True)
    out = st.consider([_sig()], LIVE, OPEN_TS, cp=cp, gt=FakeGT(), equity=236.0)
    assert out and out[0]["action"] == "error"


def test_a_broken_position_check_refuses_rather_than_placing():
    class Boom(FakeGT):
        def has_open_position(self, sym):
            raise RuntimeError("no broker")

    cp = FakeCP()
    out = st.consider([_sig()], LIVE, OPEN_TS, cp=cp, gt=Boom(), equity=236.0)
    assert cp.placed == []
    assert out[0]["reason"] == "position_check_failed"


def test_no_signals_is_a_no_op():
    assert st.consider([], LIVE, OPEN_TS, cp=FakeCP(), gt=FakeGT()) == []


# ── the log ──────────────────────────────────────────────────────────────

def test_every_decision_is_logged_with_its_latency():
    cp = FakeCP()
    st.consider([_sig()], DRY, OPEN_TS, cp=cp, gt=FakeGT())
    assert st.log_path().endswith("plan_b_burst.jsonl")
    rec = json.loads(Path(st.log_path()).read_text().splitlines()[0])
    assert rec["action"] == "would_place"
    assert rec["kind"] == "plan_b_burst"
    assert rec["latency_sec"] == 3.4        # carried from the signal
    assert rec["signal_bar_ts"] == OPEN_TS - 60
    assert rec["decision_ts"] == OPEN_TS
    assert rec["fill_model"] == "next_open"
    assert "burst_features" in rec
    assert rec["dry_run"] is True


def test_dry_run_never_calls_order_apis():
    """enable True + dry_run True must append a row and never place."""
    cp = FakeCP()
    out = st.consider([_sig()], DRY, OPEN_TS, cp=cp, gt=FakeGT())
    assert cp.placed == []
    assert out and out[0]["action"] == "would_place"
    assert Path(st.log_path()).is_file()


def test_a_broken_position_count_refuses_rather_than_placing():
    cp = FakeCP()
    out = st.consider([_sig()], LIVE, OPEN_TS, cp=cp,
                      gt=FakeGT(raises=True), equity=236.0)
    assert cp.placed == []
    assert out[0]["reason"] == "position_count_failed"


def test_the_desk_book_limit_wins_when_it_is_tighter():
    """A new entry path must never be the thing that fills the book."""
    cp = FakeCP()
    out = st.consider([_sig()], LIVE, OPEN_TS, cp=cp,
                      gt=FakeGT(open_n=1, desk_max=1), equity=236.0)
    assert cp.placed == []
    assert out[0]["reason"].startswith("slots_full")
