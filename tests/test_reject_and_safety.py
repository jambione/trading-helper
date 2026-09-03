"""Rejected-candidate logging and the unprotected-position invariant.

Both exist because of gaps 2026-08-06 exposed:
  • admission computed a reject list and discarded it, so a filter could only
    ever be observed on what it passed — which cannot tell a gate that removes
    losers from one that removes winners;
  • a naked 83%-of-equity position was caught by a human looking at the
    screen, while reconcile_unmanaged fired 384 times and aggregated to
    nothing.
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import ai_entry_watch as ew  # noqa: E402
import ai_positions as cp  # noqa: E402


# ── reject sampling ──────────────────────────────────────────────────────

def _rows(tmp_path, monkeypatch):
    written: list[dict] = []
    monkeypatch.setattr(cp, "log_reject_sample", lambda r: written.append(r))
    ew._reject_last_logged.clear()
    return written


def test_rejects_are_logged_with_the_features_they_were_rejected_with(
        tmp_path, monkeypatch):
    written = _rows(tmp_path, monkeypatch)
    by_symbol = {"AAA": {"symbol": "AAA", "source": "trending", "price": 12.5,
                         "score": 14.0, "rvol": 0.9, "pct_change": 3.0,
                         "look_reason": "WASH", "criteria": ["score"]}}
    ew._log_rejects([{"symbol": "AAA", "reason": "thin_rvol",
                      "criteria": ["score"]}], by_symbol, {}, now=1000.0)

    assert len(written) == 1
    r = written[0]
    assert r["symbol"] == "AAA" and r["reason"] == "thin_rvol"
    # Price and features must ride along — a reject with no price cannot be
    # scored against what it did next.
    assert r["price"] == 12.5 and r["rvol"] == 0.9
    assert r["source"] == "trending" and r["look_reason"] == "WASH"


def test_dwell_is_not_recorded_as_a_rejection(tmp_path, monkeypatch):
    """A name mid-admission was not turned away. Logging it would pollute the
    reject arm with names that go on to be admitted seconds later."""
    written = _rows(tmp_path, monkeypatch)
    ew._log_rejects([{"symbol": "AAA", "reason": "dwell_1/2"}],
                    {"AAA": {"symbol": "AAA", "price": 1.0}}, {}, now=1000.0)
    assert written == []


def test_reject_logging_is_throttled_per_symbol(tmp_path, monkeypatch):
    """The book rebuilds every 2s. Unthrottled this writes ~15k rows an hour
    and says nothing a 60s series does not."""
    written = _rows(tmp_path, monkeypatch)
    rej = [{"symbol": "AAA", "reason": "not_uptrend"}]
    by = {"AAA": {"symbol": "AAA", "price": 5.0}}

    ew._log_rejects(rej, by, {}, now=1000.0)
    ew._log_rejects(rej, by, {}, now=1002.0)   # 2s later — same sync loop
    ew._log_rejects(rej, by, {}, now=1030.0)   # still inside the window
    assert len(written) == 1

    ew._log_rejects(rej, by, {}, now=1000.0 + 61)
    assert len(written) == 2, "throttle must expire so a series accrues"


def test_reject_logging_can_be_disabled(tmp_path, monkeypatch):
    written = _rows(tmp_path, monkeypatch)
    ew._log_rejects([{"symbol": "AAA", "reason": "not_uptrend"}],
                    {"AAA": {"symbol": "AAA", "price": 5.0}},
                    {"ai_reject_log_enabled": False}, now=1000.0)
    assert written == []


# ── unprotected-position invariant ───────────────────────────────────────

class _FakeAT:
    def __init__(self, positions, orders, equity=10_000.0):
        self._p, self._o, self._e = positions, orders, equity
        self.closed = []
        self.stops = []
        self.canceled = []

    def get_positions_detail(self):
        return self._p

    def get_open_orders(self, limit=100):
        return self._o

    def get_equity(self):
        return self._e

    def cancel_open_orders(self, ticker=None):
        self.canceled.append(ticker)
        return {"ok": True, "canceled": 1}

    def close_out(self, ticker):
        self.closed.append(ticker)
        self._p.pop(str(ticker).upper(), None)
        return {"ok": True, "order_id": "c1"}

    def replace_stop(self, ticker, old_id, stop_price=None, trail_percent=None):
        self.stops.append((ticker, stop_price))
        oid = f"stop-{ticker}"
        self._o = [
            o for o in self._o
            if not (str(o.get("symbol", "")).upper() == str(ticker).upper()
                    and str(o.get("side", "")).lower() == "sell")
        ]
        self._o.append({
            "id": oid, "symbol": str(ticker).upper(),
            "side": "sell", "type": "stop", "stop": stop_price,
        })
        return {"ok": True, "order_id": oid}


def _reconcile(monkeypatch, tmp_path, positions, orders, state=None, **cfg):
    monkeypatch.setattr(cp, "POSITIONS_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(cp, "EVENTS_PATH", tmp_path / "events.jsonl")
    (tmp_path / "state.json").write_text(json.dumps(state or {}))
    if not (tmp_path / "events.jsonl").exists():
        (tmp_path / "events.jsonl").write_text("")
    # Capital-first actions need explicit opt-in in unit tests that only check
    # classification, so defaults here disable adopt/flatten unless asked.
    defaults = {
        "ai_heal_unprotected": False,
        "ai_adopt_unmanaged": False,
        "ai_flatten_unmanaged_unprotected": False,
    }
    defaults.update(cfg)

    def _flag(key, default=True):
        return bool(defaults.get(key, default))

    monkeypatch.setattr(cp, "_cfg_flag", _flag)
    fake = _FakeAT(positions, orders)
    monkeypatch.setitem(sys.modules, "alpaca_trader", fake)
    return cp.reconcile_broker(now=1000.0), fake


def test_position_with_no_resting_sell_is_flagged(tmp_path, monkeypatch):
    """The CELH shape: a live position, no protective order, huge relative to
    equity, and not in managed state."""
    rep, _ = _reconcile(
        monkeypatch, tmp_path,
        positions={"CELH": {"qty": 353.0, "mkt_val": 8403.0}},
        orders=[],
    )
    assert len(rep["unprotected"]) == 1
    u = rep["unprotected"][0]
    assert u["symbol"] == "CELH"
    assert u["pct_equity"] == 84.0, "concentration is why this is urgent"
    assert u["managed"] is False


def test_position_with_a_resting_sell_is_not_flagged(tmp_path, monkeypatch):
    rep, _ = _reconcile(
        monkeypatch, tmp_path,
        positions={"CELH": {"qty": 353.0, "mkt_val": 8403.0}},
        orders=[{"symbol": "CELH", "side": "sell", "type": "stop"}],
    )
    assert rep["unprotected"] == []


def test_take_profit_limit_alone_is_not_protection(tmp_path, monkeypatch):
    """RIOT shape: upper limit resting, no stop — still unprotected."""
    rep, _ = _reconcile(
        monkeypatch, tmp_path,
        positions={"RIOT": {"qty": 100.0, "mkt_val": 1200.0}},
        orders=[{"symbol": "RIOT", "side": "sell", "type": "limit"}],
    )
    assert [u["symbol"] for u in rep["unprotected"]] == ["RIOT"]


def test_stop_limit_sell_counts_as_protection(tmp_path, monkeypatch):
    rep, _ = _reconcile(
        monkeypatch, tmp_path,
        positions={"RIOT": {"qty": 100.0, "mkt_val": 1200.0}},
        orders=[{"symbol": "RIOT", "side": "sell", "type": "stop_limit"}],
    )
    assert rep["unprotected"] == []


def test_a_resting_buy_does_not_count_as_protection(tmp_path, monkeypatch):
    """Only a SELL exits a long. An open buy is the opposite of protection."""
    rep, _ = _reconcile(
        monkeypatch, tmp_path,
        positions={"CELH": {"qty": 353.0, "mkt_val": 8403.0}},
        orders=[{"symbol": "CELH", "side": "buy", "type": "limit"}],
    )
    assert [u["symbol"] for u in rep["unprotected"]] == ["CELH"]


def test_flat_account_reports_nothing(tmp_path, monkeypatch):
    rep, _ = _reconcile(monkeypatch, tmp_path, positions={}, orders=[])
    assert rep["unprotected"] == [] and rep["unmanaged"] == []


def test_adopt_unmanaged_from_entry_ok_then_heal_stop(tmp_path, monkeypatch):
    """MLTX shape: entry_ok + live fill, lost from state → re-home + stop."""
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({
        "ts": 900.0, "kind": "entry_ok", "symbol": "MLTX",
        "qty_a": 150, "stop_price": 16.136, "target_1": 16.83,
        "entry_price": 16.57, "strategy": "day_scalp_v0",
    }) + "\n")
    monkeypatch.setattr(cp, "EVENTS_PATH", events)
    rep, fake = _reconcile(
        monkeypatch, tmp_path,
        positions={"MLTX": {
            "qty": 150.0, "mkt_val": 2481.0,
            "avg_entry_price": 16.570267, "current": 16.54,
        }},
        orders=[{"symbol": "MLTX", "side": "sell", "type": "limit", "limit": 16.83}],
        state={},
        ai_adopt_unmanaged=True,
        ai_heal_unprotected=True,
        ai_flatten_unmanaged_unprotected=True,
    )
    st = json.loads((tmp_path / "state.json").read_text())
    assert "MLTX" in st
    assert st["MLTX"]["entry_confirmed"] is True
    assert st["MLTX"]["stop_price"] == 16.136
    assert st["MLTX"].get("adopted") is True
    assert rep.get("adopt_events")
    # TP-only was unprotected → heal clears TP and places stop.
    assert any(s[0] == "MLTX" for s in fake.stops)
    assert rep["unmanaged"] == []


def test_unmanaged_unprotected_without_trail_is_flattened(tmp_path, monkeypatch):
    """Capital first: naked orphan with no entry_ok / stop → close it."""
    rep, fake = _reconcile(
        monkeypatch, tmp_path,
        positions={"CELH": {"qty": 353.0, "mkt_val": 8403.0, "avg_entry_price": 23.8}},
        orders=[],
        state={},
        ai_adopt_unmanaged=True,
        ai_heal_unprotected=True,
        ai_flatten_unmanaged_unprotected=True,
    )
    assert "CELH" in fake.closed
    assert any(
        e.get("event") == "unmanaged_unprotected_flatten"
        for e in (rep.get("heal_events") or [])
    )


def test_concurrent_update_state_keeps_both_symbols(tmp_path, monkeypatch):
    """Two near-simultaneous entry_ok writes must not drop a symbol."""
    monkeypatch.setattr(cp, "POSITIONS_STATE_PATH", tmp_path / "state.json")
    (tmp_path / "state.json").write_text("{}")

    def add(sym):
        def mut(st):
            st[sym] = {"entry_price": 1.0, "stop_price": 0.9, "total_qty": 1}
        cp._update_state(mut)

    import threading
    t1 = threading.Thread(target=add, args=("RUM",))
    t2 = threading.Thread(target=add, args=("MLTX",))
    t1.start(); t2.start()
    t1.join(); t2.join()
    st = json.loads((tmp_path / "state.json").read_text())
    assert set(st) == {"RUM", "MLTX"}


# ── software shelf counts as protection when broker stops are off ─────────
# ai_broker_stop_enabled=false is the desk's deliberate setting: no long ever
# carries a resting stop, so this check fired on every managed position on
# every reconcile — 62 alarms on 2026-08-21, all "managed": true and every one
# carrying a live shelf. An alarm that cries wolf 62 times a day hides the one
# real naked position inside itself.


def test_shelf_counts_as_protection_when_broker_stops_are_off(
        tmp_path, monkeypatch):
    """A managed row with a live shelf is protected, not naked."""
    rep, _ = _reconcile(
        monkeypatch, tmp_path,
        positions={"BKKT": {"qty": 5.0, "mkt_val": 42.08}},
        orders=[],                                  # no resting stop, by design
        state={"BKKT": {"entry_price": 8.47, "local_stop_price": 8.40}},
        ai_broker_stop_enabled=False,
    )
    assert rep["unprotected"] == [], "the shelf is the protection in this mode"


def test_managed_row_without_a_shelf_still_alarms(tmp_path, monkeypatch):
    """The case worth seeing, and the reason this is not a blanket mute."""
    rep, _ = _reconcile(
        monkeypatch, tmp_path,
        positions={"BKKT": {"qty": 5.0, "mkt_val": 42.08}},
        orders=[],
        state={"BKKT": {"entry_price": 8.47}},      # no shelf at all
        ai_broker_stop_enabled=False,
    )
    assert [u["symbol"] for u in rep["unprotected"]] == ["BKKT"]


def test_unmanaged_position_still_alarms_with_broker_stops_off(
        tmp_path, monkeypatch):
    """A position the desk knows nothing about has no shelf by definition."""
    rep, _ = _reconcile(
        monkeypatch, tmp_path,
        positions={"CELH": {"qty": 353.0, "mkt_val": 8403.0}},
        orders=[],
        state={},
        ai_broker_stop_enabled=False,
    )
    assert [u["symbol"] for u in rep["unprotected"]] == ["CELH"]


def test_broker_stop_mode_is_unchanged(tmp_path, monkeypatch):
    """With broker stops ON, a shelf is not a substitute for a resting stop."""
    rep, _ = _reconcile(
        monkeypatch, tmp_path,
        positions={"BKKT": {"qty": 5.0, "mkt_val": 42.08}},
        orders=[],
        state={"BKKT": {"entry_price": 8.47, "local_stop_price": 8.40}},
        ai_broker_stop_enabled=True,
    )
    assert [u["symbol"] for u in rep["unprotected"]] == ["BKKT"]


def test_managed_without_shelf_stamps_from_known_stop(tmp_path, monkeypatch):
    """Local-stop desk: missing shelf + known R → stamp, not a broker slap."""
    monkeypatch.setattr(cp, "_cfg_all", lambda: {
        "ai_local_trail_give_r": 0.10,
        "ai_local_trail_min_give_px": 0,
        "ai_local_trail_enabled": True,
    })
    rep, fake = _reconcile(
        monkeypatch, tmp_path,
        positions={"BKKT": {"qty": 5.0, "mkt_val": 42.08}},
        orders=[],
        state={"BKKT": {
            "entry_price": 8.47, "stop_price": 8.00,
            "entry_stop_price": 8.00, "risk_per_share": 0.47,
            "entry_confirmed": True,
        }},
        ai_broker_stop_enabled=False,
        ai_heal_unprotected=False,
    )
    assert [u["symbol"] for u in rep["unprotected"]] == ["BKKT"]
    assert "BKKT" not in fake.closed
    heals = [e for e in (rep.get("heal_events") or [])
             if e.get("event") == "unprotected_local_healed"]
    assert heals, rep.get("heal_events")
    st = json.loads((tmp_path / "state.json").read_text())
    assert st["BKKT"]["local_stop_price"] > 0
    assert st["BKKT"].get("closing_reason") is None


def test_managed_without_shelf_or_stop_flattens_local(tmp_path, monkeypatch):
    """No shelf and no recoverable stop → unprotected_local flatten."""
    rep, fake = _reconcile(
        monkeypatch, tmp_path,
        positions={"BKKT": {"qty": 5.0, "mkt_val": 42.08}},
        orders=[],
        state={"BKKT": {"entry_price": 8.47, "entry_confirmed": True}},
        ai_broker_stop_enabled=False,
        ai_heal_unprotected=False,
    )
    assert [u["symbol"] for u in rep["unprotected"]] == ["BKKT"]
    assert "BKKT" in fake.closed
    assert any(
        e.get("event") == "unprotected_local"
        for e in (rep.get("heal_events") or [])
    )
