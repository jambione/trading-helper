from desk_risk import dynamic_max_price


def test_ai_max_price_caps_the_equity_float():
    cfg = {"ai_position_slot_equity": 60.0, "ai_max_position_pct": 20.0, "ai_max_price": 100.0}
    assert dynamic_max_price(2264.73, cfg) == 100.0


def test_no_ceiling_keeps_the_float():
    cfg = {"ai_position_slot_equity": 60.0, "ai_max_position_pct": 20.0}
    assert dynamic_max_price(2264.73, cfg) > 100.0


def test_ceiling_above_float_does_not_raise_it():
    cfg = {"ai_position_slot_equity": 60.0, "ai_max_position_pct": 20.0, "ai_max_price": 10000.0}
    assert dynamic_max_price(2264.73, cfg) == dynamic_max_price(
        2264.73, {k: v for k, v in cfg.items() if k != "ai_max_price"})
