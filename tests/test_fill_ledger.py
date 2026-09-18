"""Fill ledger — append-only broker fill truth, dedupe, and the pytest guard."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import fill_ledger as fl


@pytest.fixture(autouse=True)
def _ledger_tmp(tmp_path):
    path = tmp_path / "fills.jsonl"
    fl.set_ledger_path_for_tests(path)
    yield path
    fl.set_ledger_path_for_tests(None)


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _broker_row(**over):
    row = {
        "id": "ord-1",
        "client_order_id": "desk-1",
        "symbol": "VEEA",
        "side": "buy",
        "qty": 100.0,
        "filled_qty": 100.0,
        "filled_avg_price": 5.93,
        "type": "market",
        "status": "filled",
        "submitted_at": "2026-09-17T13:45:01+00:00",
        "filled_at": "2026-09-17T13:45:02+00:00",
        "order_class": "simple",
    }
    row.update(over)
    return row


# ── The guard that keeps tests out of the live ledger ─────────────────────────

def test_production_path_is_refused_under_pytest(monkeypatch):
    """The bug this module exists to not repeat.

    alpaca_trade_log.json carries 18 SELL rows whose order_status is a
    MagicMock repr — test runs wrote into the production log and nothing
    complained. Here that is a hard failure.
    """
    fl.set_ledger_path_for_tests(None)
    monkeypatch.delenv("AI_REPORT_DIR", raising=False)

    path = fl.ledger_path_for_ts(time.time())
    assert _ROOT in path.resolve().parents, "expected a repo-relative path"

    with pytest.raises(RuntimeError, match="refused a production path"):
        fl.log_submit(action="BUY", symbol="VEEA", order_id="ord-1")


def test_ai_report_dir_env_satisfies_the_guard(monkeypatch, tmp_path):
    """AI_REPORT_DIR is the existing escape hatch; it must silence the guard."""
    fl.set_ledger_path_for_tests(None)
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path / "reports"))

    assert fl.log_submit(action="BUY", symbol="VEEA", order_id="ord-1",
                         cfg={"ai_fill_ledger_enabled": True}) is True
    day = datetime.now(tz=fl.ET).strftime("%Y-%m-%d")
    assert (tmp_path / "reports" / "fills" / f"{day}.jsonl").exists()


# ── Submit events ─────────────────────────────────────────────────────────────

def test_log_submit_appends(_ledger_tmp):
    assert fl.log_submit(
        action="BUY", symbol="veea", order_id="ord-1",
        order_status="OrderStatus.PENDING_NEW", price=5.93, qty=100.0,
        trader_mode="paper", note="bracket",
        cfg={"ai_fill_ledger_enabled": True},
    ) is True

    rows = _read(_ledger_tmp)
    assert len(rows) == 1
    row = rows[0]
    assert row["event"] == "submit"
    assert row["symbol"] == "VEEA"          # upper-cased
    assert row["action"] == "BUY"
    assert row["submit_status"] == "pending_new"   # enum repr stripped
    assert row["order_id"] == "ord-1"
    assert row["price"] == 5.93
    assert row["trader_mode"] == "paper"


def test_log_submit_requires_order_id_and_symbol(_ledger_tmp):
    assert fl.log_submit(action="BUY", symbol="VEEA", order_id="") is False
    assert fl.log_submit(action="BUY", symbol="", order_id="ord-1") is False
    assert _read(_ledger_tmp) == []


def test_disabled_is_a_silent_no_op(_ledger_tmp):
    assert fl.log_submit(action="BUY", symbol="VEEA", order_id="ord-1",
                         cfg={"ai_fill_ledger_enabled": False}) is True
    assert _read(_ledger_tmp) == []


# ── Fill events and dedupe ────────────────────────────────────────────────────

def test_record_fills_writes_broker_truth(_ledger_tmp):
    written = fl.record_fills([_broker_row()],
                              cfg={"ai_fill_ledger_enabled": True})
    assert written == 1

    row = _read(_ledger_tmp)[0]
    assert row["event"] == "fill"
    assert row["symbol"] == "VEEA"
    assert row["filled_qty"] == 100.0
    assert row["filled_avg_price"] == 5.93
    assert row["status"] == "filled"
    assert row["side"] == "buy"
    # The thing the legacy log could never say.
    assert row["status"] != "pending_new"


def test_repolling_the_same_fill_appends_nothing(_ledger_tmp):
    cfg = {"ai_fill_ledger_enabled": True}
    assert fl.record_fills([_broker_row()], cfg=cfg) == 1
    assert fl.record_fills([_broker_row()], cfg=cfg) == 0
    assert fl.record_fills([_broker_row()], cfg=cfg) == 0
    assert len(_read(_ledger_tmp)) == 1


def test_partial_then_complete_appends_both(_ledger_tmp):
    cfg = {"ai_fill_ledger_enabled": True}
    partial = _broker_row(filled_qty=40.0, status="partially_filled",
                          filled_avg_price=5.90)
    assert fl.record_fills([partial], cfg=cfg) == 1
    assert fl.record_fills([_broker_row()], cfg=cfg) == 1

    rows = _read(_ledger_tmp)
    assert [r["filled_qty"] for r in rows] == [40.0, 100.0]
    # Append-only: the partial row is still there, unedited.
    assert rows[0]["status"] == "partially_filled"


def test_dedupe_survives_a_restart(_ledger_tmp):
    """A restart must not re-append fills already on record."""
    cfg = {"ai_fill_ledger_enabled": True}
    assert fl.record_fills([_broker_row()], cfg=cfg) == 1

    fl._seen.clear()   # simulate a fresh process with the day file on disk

    assert fl.record_fills([_broker_row()], cfg=cfg) == 0
    assert len(_read(_ledger_tmp)) == 1


def test_fill_day_follows_filled_at_not_poll_time(tmp_path, monkeypatch):
    """A 15:59 ET fill polled after midnight belongs to the session it happened in."""
    fl.set_ledger_path_for_tests(None)
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path / "reports"))

    row = _broker_row(filled_at="2026-09-17T19:59:30+00:00")   # 15:59 ET
    poll_at = datetime(2026, 9, 18, 0, 5, tzinfo=fl.ET).timestamp()
    assert fl.record_fills([row], ts=poll_at,
                           cfg={"ai_fill_ledger_enabled": True}) == 1

    fills = tmp_path / "reports" / "fills"
    assert (fills / "2026-09-17.jsonl").exists()
    assert not (fills / "2026-09-18.jsonl").exists()


# ── Folding ───────────────────────────────────────────────────────────────────

def test_fold_orders_collapses_to_current_truth(_ledger_tmp):
    cfg = {"ai_fill_ledger_enabled": True}
    fl.log_submit(action="BUY", symbol="VEEA", order_id="ord-1",
                  order_status="pending_new", price=5.95, cfg=cfg)
    fl.record_fills([_broker_row(filled_qty=40.0,
                                 status="partially_filled")], cfg=cfg)
    fl.record_fills([_broker_row()], cfg=cfg)

    folded = fl.fold_orders(_read(_ledger_tmp))
    assert set(folded) == {"ord-1"}
    order = folded["ord-1"]
    assert order["submitted"] is True
    assert order["filled_qty"] == 100.0        # last fill wins
    assert order["filled_avg_price"] == 5.93
    assert order["status"] == "filled"
    assert order["events"] == 3                # history is preserved


def test_fold_orders_ignores_junk():
    assert fl.fold_orders(None) == {}
    assert fl.fold_orders([{"event": "fill"}]) == {}      # no order_id
    assert fl.fold_orders(["not a dict"]) == {}


# ── Poll pacing ───────────────────────────────────────────────────────────────

def test_poll_fills_paces_itself(monkeypatch, _ledger_tmp):
    calls = {"n": 0}

    class _FakeTrader:
        @staticmethod
        def is_active():
            return True

        @staticmethod
        def get_filled_orders(limit=200, days=30):
            calls["n"] += 1
            return [_broker_row()]

    monkeypatch.setitem(sys.modules, "alpaca_trader", _FakeTrader)
    fl._last_poll_mono = 0.0
    cfg = {"ai_fill_ledger_enabled": True, "ai_fill_ledger_poll_sec": 60.0}

    first = fl.poll_fills(cfg)
    assert first["ok"] is True and first["written"] == 1

    second = fl.poll_fills(cfg)
    assert second["skipped"] == "paced"
    assert calls["n"] == 1, "broker must not be hit twice inside the pace window"


def test_poll_fills_is_quiet_when_the_trader_is_off(monkeypatch, _ledger_tmp):
    class _OffTrader:
        @staticmethod
        def is_active():
            return False

    monkeypatch.setitem(sys.modules, "alpaca_trader", _OffTrader)
    fl._last_poll_mono = 0.0

    out = fl.poll_fills({"ai_fill_ledger_enabled": True,
                         "ai_fill_ledger_poll_sec": 0.0})
    assert out["skipped"] == "trader_off"
    assert _read(_ledger_tmp) == []


# ── Reconciliation ────────────────────────────────────────────────────────────

def _fake_trader_with(rows):
    class _T:
        @staticmethod
        def is_active():
            return True

        @staticmethod
        def get_filled_orders(limit=200, days=30):
            return list(rows)
    return _T


def test_reconcile_clean_day(monkeypatch, _ledger_tmp):
    cfg = {"ai_fill_ledger_enabled": True}
    fl.record_fills([_broker_row()], cfg=cfg)
    monkeypatch.setitem(sys.modules, "alpaca_trader",
                        _fake_trader_with([_broker_row()]))

    rep = fl.reconcile_day("2026-09-17")
    assert rep["ok"] is True
    assert rep["matched"] == 1
    assert rep["broker_only"] == [] and rep["mismatched"] == []


def test_reconcile_flags_a_fill_the_ledger_missed(monkeypatch, _ledger_tmp):
    """The finding that matters: the broker filled it, the ledger never saw it."""
    monkeypatch.setitem(sys.modules, "alpaca_trader",
                        _fake_trader_with([_broker_row()]))

    rep = fl.reconcile_day("2026-09-17")
    assert rep["ok"] is False
    assert len(rep["broker_only"]) == 1
    assert rep["broker_only"][0]["symbol"] == "VEEA"


def test_reconcile_flags_a_price_mismatch(monkeypatch, _ledger_tmp):
    cfg = {"ai_fill_ledger_enabled": True}
    fl.record_fills([_broker_row(filled_avg_price=5.93)], cfg=cfg)
    monkeypatch.setitem(sys.modules, "alpaca_trader",
                        _fake_trader_with([_broker_row(filled_avg_price=6.40)]))

    rep = fl.reconcile_day("2026-09-17")
    assert rep["ok"] is False
    assert len(rep["mismatched"]) == 1
    assert rep["mismatched"][0]["ledger_px"] == 5.93
    assert rep["mismatched"][0]["broker_px"] == 6.40


# ── The alpaca_trader hook ────────────────────────────────────────────────────

def test_log_action_mirrors_submissions_into_the_ledger(_ledger_tmp, monkeypatch):
    """One hook in _log_action is what covers all 21 submit_order paths."""
    import alpaca_trader

    monkeypatch.setattr(alpaca_trader, "_TRADE_LOG",
                        _ledger_tmp.parent / "legacy_trade_log.json")
    monkeypatch.setattr(alpaca_trader, "_mode", "paper")

    alpaca_trader._log_action(
        "BUY", "VEEA", 5.93, 40.0, 0.01,
        order_id="ord-99", order_status="OrderStatus.PENDING_NEW", qty=100.0,
    )

    rows = _read(_ledger_tmp)
    assert len(rows) == 1
    assert rows[0]["event"] == "submit"
    assert rows[0]["order_id"] == "ord-99"
    assert rows[0]["symbol"] == "VEEA"
    assert rows[0]["trader_mode"] == "paper"


def test_log_action_without_an_order_id_writes_no_ledger_row(_ledger_tmp, monkeypatch):
    """Refusals and skips are not submissions — they stay out of the ledger."""
    import alpaca_trader

    monkeypatch.setattr(alpaca_trader, "_TRADE_LOG",
                        _ledger_tmp.parent / "legacy_trade_log.json")

    alpaca_trader._log_action("BUY_REFUSED", "VEEA", 5.93, 40.0, 0.01,
                              note="refused_unprotected:no_stop")

    assert _read(_ledger_tmp) == []


def test_parses_the_timestamp_format_alpaca_actually_returns(tmp_path, monkeypatch):
    """Live rows use a space separator, not 'T'.

    Verified against the paper account 2026-09-18: get_filled_orders returns
    filled_at as '2026-09-17 19:50:14.391999+00:00'. A parse failure here
    would silently file every fill under the poll day instead of the fill day.
    """
    assert fl._parse_ts("2026-09-17 19:50:14.391999+00:00") is not None

    fl.set_ledger_path_for_tests(None)
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path / "reports"))

    row = _broker_row(filled_at="2026-09-17 19:50:14.391999+00:00")
    poll_at = datetime(2026, 9, 18, 9, 0, tzinfo=fl.ET).timestamp()
    assert fl.record_fills([row], ts=poll_at,
                           cfg={"ai_fill_ledger_enabled": True}) == 1

    assert (tmp_path / "reports" / "fills" / "2026-09-17.jsonl").exists()


def test_unparseable_timestamp_falls_back_without_raising(_ledger_tmp):
    row = _broker_row(filled_at="not-a-date", submitted_at="")
    assert fl.record_fills([row], cfg={"ai_fill_ledger_enabled": True}) == 1
    assert _read(_ledger_tmp)[0]["filled_at"] == "not-a-date"
