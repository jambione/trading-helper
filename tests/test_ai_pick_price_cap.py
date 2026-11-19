"""AI picks (seed_rank / gx_agree) are exempt from the $100 cap when ai_watch_ai_pick_max_price > 0 (operator 11/19)."""
import ai_entry_watch as ew


CFG = {"ai_max_price": 100.0, "ai_watch_ai_pick_max_price": 400.0, "ai_tight_max_price": 400.0}


def test_is_ai_pick():
    assert ew.is_ai_pick({"criteria": ["research", "seed_rank"]})
    assert ew.is_ai_pick({"admit_criteria": ["seed_rank_gx"]})
    assert ew.is_ai_pick({"gx_agree": True})
    assert not ew.is_ai_pick({"criteria": ["movers", "research"]})
    assert not ew.is_ai_pick(None)


def test_cap_lifts_only_for_ai_picks():
    ai = {"symbol": "TTWO", "criteria": ["seed_rank"]}
    plain = {"symbol": "XYZ", "criteria": ["movers"]}
    assert ew._max_price_for("movers", CFG, 100.0, row=ai) == 400.0
    assert ew._max_price_for("movers", CFG, 100.0, row=plain) == 100.0
    assert ew._max_price_for("movers", {**CFG, "ai_watch_ai_pick_max_price": 0.0}, 100.0, row=ai) == 100.0   # switch off
    assert ew._max_price_for("tight", CFG, 100.0, row=plain) == 400.0                                        # tight unchanged
    assert ew._max_price_for("movers", {**CFG, "ai_watch_ai_pick_max_price": 50.0}, 100.0, row=ai) == 100.0  # never lowers


def test_config_key_registered():
    import config
    assert config.DEFAULT_CONFIG["ai_watch_ai_pick_max_price"] == 0.0
    assert "ai_watch_ai_pick_max_price" in config.SAFE_CONFIG_KEYS
