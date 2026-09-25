"""ai_watch_momentum_min_price: momentum names tested from $1, others keep $20."""
import ai_entry_watch as ew

BASE = {"ai_watch_min_price": 20.0, "ai_max_price": 100.0, "ai_watch_admit_arm_gates": True}
MOM = {**BASE, "ai_watch_momentum_min_price": 1.0}


def test_floor_is_per_source():
    assert ew._min_price_for("momentum", MOM) == 1.0
    assert ew._min_price_for("momentum_open", MOM) == 1.0
    assert ew._min_price_for("movers", MOM) == 20.0
    assert ew._min_price_for("momentum", BASE) == 20.0       # unset -> shared floor


def test_admit_gate_band_uses_the_source_floor():
    gate = lambda row, cfg: ew.admit_arm_gates(row, cfg, spread_fn=lambda *a, **k: None,
                                               gap_fn=lambda *a, **k: None)
    assert gate({"symbol": "JAGX", "price": 3.1, "source": "momentum"}, MOM) == (True, "")
    assert gate({"symbol": "JAGX", "price": 0.8, "source": "momentum"}, MOM) == (False, "below_min_price")
    assert gate({"symbol": "CLF", "price": 12.7, "source": "movers"}, MOM) == (False, "below_min_price")
    assert gate({"symbol": "JAGX", "price": 3.1, "source": "momentum"}, BASE) == (False, "below_min_price")


def test_push_filter_keeps_cheap_momentum_names(monkeypatch):
    monkeypatch.setattr(ew, "_live_quote_map", lambda: ({"JAGX": {"price": 3.1}, "CLF": {"price": 12.7}}, {}))
    monkeypatch.setattr(ew, "load_watch", lambda: {})
    monkeypatch.setattr(ew, "_MOMENTUM_SYMS", {"JAGX"})
    monkeypatch.setattr(ew, "_push_cfg", lambda: {**MOM, "ai_watch_slot_priority": True})
    assert ew._push_band_filter(["JAGX", "CLF"]) == ["JAGX"]
