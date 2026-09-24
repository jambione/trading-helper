"""The arm's hard gates run at admission, so the book only seats buyable names."""
from datetime import datetime
from zoneinfo import ZoneInfo

import ai_entry_watch as w

ET = ZoneInfo("America/New_York")
ON = {"ai_watch_admit_arm_gates": True, "ai_watch_min_price": 20.0, "ai_max_price": 100.0,
      "ai_watch_max_sip_spread_pct": 0.20, "ai_watch_gap_down_block_pct": 1.0,
      "ai_movers_sip_delay_min": 15.0}


def _t(hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 9, 24, h, m, tzinfo=ET).timestamp()


def _gates(row, now="11:00", spread=0.05, gap=0.5, cfg=ON):
    return w.admit_arm_gates(row, cfg, now=_t(now),
                             spread_fn=lambda s, now=None: spread,
                             gap_fn=lambda s, now=None: gap)


def test_off_by_default_admits_everything():
    assert w.admit_arm_gates({"symbol": "NBIS", "price": 240.0}, {}) == (True, "")


def test_price_band_for_every_source():
    assert _gates({"symbol": "NBIS", "price": 240.78, "source": "agy"}) == (False, "above_max_price")
    assert _gates({"symbol": "CLF", "price": 12.74, "source": "xai"}) == (False, "below_min_price")
    assert _gates({"symbol": "PFE", "price": 28.64, "source": "movers"}) == (True, "")


def test_spread_and_gap_refuse_like_the_arm():
    assert _gates({"symbol": "QMCO", "price": 28.4}, spread=0.47) == (False, "spread_wide")
    assert _gates({"symbol": "IONQ", "price": 42.0}, gap=-2.4) == (False, "gapped_down")


def test_unknown_data_abstains_so_the_book_cannot_empty():
    assert _gates({"symbol": "PFE", "price": 28.6}, spread=None, gap=None) == (True, "")


def test_spread_sits_out_until_sip_covers_the_session():
    # 09:38: the 16-min-old SIP spread is premarket; do not judge it at the door
    assert _gates({"symbol": "AXTI", "price": 74.0}, now="09:38", spread=0.9) == (True, "")
    assert _gates({"symbol": "AXTI", "price": 74.0}, now="09:47", spread=0.9) == (False, "spread_wide")
    # the gap gate is not time-shifted
    assert _gates({"symbol": "IONQ", "price": 42.0}, now="09:38", gap=-2.4) == (False, "gapped_down")


def test_passes_inclusion_uses_the_gates(monkeypatch):
    monkeypatch.setattr(w, "sip_spread_pct", lambda s, now=None: 0.05)
    monkeypatch.setattr(w, "open_gap_pct", lambda s, now=None: 0.0)
    ok, _met, why = w.passes_inclusion({"symbol": "NBIS", "price": 240.78, "source": "agy"},
                                       dict(ON))
    assert (ok, why) == (False, "above_max_price")


def test_gate_block_evicts_a_seat_past_grace(monkeypatch):
    monkeypatch.setattr(w, "evaluate_arm_ready", lambda rec, cfg, now=None: (True, "ok"))
    monkeypatch.setattr(w, "sip_spread_pct", lambda s, now=None: 0.47)
    monkeypatch.setattr(w, "open_gap_pct", lambda s, now=None: 0.0)
    monkeypatch.setattr(w, "_is_protected_pin_seat", lambda rec, cfg, now=None: False)
    monkeypatch.setattr(w, "_within_subscribe_grace", lambda rec, cfg, now: False)
    dropped = []
    monkeypatch.setattr(w, "drop_watch_symbols", lambda syms: dropped.extend(syms))

    class _CP:
        @staticmethod
        def log_event(kind, **kw):
            return {"kind": kind, **kw}

    now = _t("11:00")
    rec = {"symbol": "QMCO", "status": "watching", "block_code": "spread_wide",
           "price": 28.4, "unarmable_since": now - 120}
    cfg = dict(ON, ai_watch_unarmable_evict_sec=30.0)
    events = []
    assert w._maybe_never_armable_evict(rec, sym="QMCO", cfg=cfg, now=now, events=events,
                                        cp=_CP, gt=None) is True
    assert dropped == ["QMCO"] and events[0]["reason"] == "never_armable"
    # with the knob off, the same seat is left alone
    dropped.clear()
    rec2 = dict(rec)
    assert w._maybe_never_armable_evict(rec2, sym="QMCO", cfg=dict(cfg, ai_watch_admit_arm_gates=False),
                                        now=now, events=[], cp=_CP, gt=None) is False
