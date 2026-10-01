"""Seat release after a run of spread_wide refusals; uptrend grace around flat."""
import ai_entry_watch as ew


class _Cp:
    @staticmethod
    def log_event(kind, **kw):
        return {"kind": kind, **kw}


def _rec(code="spread_wide"):
    return {"status": "watching", "block_code": code}


def test_spread_wide_seat_is_freed_after_the_window(monkeypatch):
    dropped = []
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda syms: dropped.extend(syms))
    cfg = {"ai_watch_spread_wide_evict_sec": 120.0}
    rec, ev = _rec(), []
    kw = dict(sym="SDEV", cfg=cfg, events=ev, cp=_Cp, gt=None)
    assert ew._maybe_spread_wide_evict(rec, now=1000.0, **kw) is False   # clock starts
    assert ew._maybe_spread_wide_evict(rec, now=1100.0, **kw) is False
    assert ew._maybe_spread_wide_evict(rec, now=1121.0, **kw) is True
    assert dropped == ["SDEV"] and ev[-1]["reason"] == "spread_wide_seat"


def test_any_other_block_resets_the_clock(monkeypatch):
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda syms: None)
    cfg = {"ai_watch_spread_wide_evict_sec": 120.0}
    rec = _rec()
    kw = dict(sym="X", cfg=cfg, events=[], cp=_Cp, gt=None)
    ew._maybe_spread_wide_evict(rec, now=1000.0, **kw)
    rec["block_code"] = "tape_only"
    assert ew._maybe_spread_wide_evict(rec, now=1100.0, **kw) is False
    assert "spread_wide_since" not in rec
    rec["block_code"] = "spread_wide"
    assert ew._maybe_spread_wide_evict(rec, now=1200.0, **kw) is False   # restarts here
    assert ew._maybe_spread_wide_evict(rec, now=1300.0, **kw) is False


def test_off_by_default_and_open_positions_stay(monkeypatch):
    monkeypatch.setattr(ew, "drop_watch_symbols", lambda syms: None)
    rec = _rec()
    assert ew._maybe_spread_wide_evict(rec, sym="X", cfg={}, now=1.0, events=[], cp=_Cp, gt=None) is False

    class Gt:
        @staticmethod
        def has_open_position(sym):
            return True
    cfg = {"ai_watch_spread_wide_evict_sec": 1.0}
    rec = _rec()
    rec["spread_wide_since"] = 0.5
    assert ew._maybe_spread_wide_evict(rec, sym="X", cfg=cfg, now=100.0, events=[], cp=_Cp, gt=Gt) is False


def test_uptrend_grace_window(monkeypatch):
    monkeypatch.setattr(ew, "_UPTREND_LAST_UP", {})
    cfg = {"ai_watch_uptrend_grace_sec": 60.0}
    assert ew.uptrend_in_grace("SNXX", cfg, now=1000.0) is False      # never up
    ew.uptrend_note_up("SNXX", now=1000.0)
    assert ew.uptrend_in_grace("SNXX", cfg, now=1059.0) is True
    assert ew.uptrend_in_grace("SNXX", cfg, now=1061.0) is False
    assert ew.uptrend_in_grace("SNXX", {}, now=1001.0) is False      # off by default
