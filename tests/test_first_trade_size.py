"""ai_first_trade_size_mult: the day's first buy in a symbol at reduced size (repeat_name_prereg.json)."""
from ai_positions import first_trade_qty


def test_first_trade_halved_whole_shares():
    assert first_trade_qty(78.0, 0, 0.5) == 39.0
    assert first_trade_qty(79.0, 0, 0.5) == 39.0          # rounds down
    assert first_trade_qty(1.0, 0, 0.5) == 1.0            # never below one share


def test_repeat_unknown_and_off_keep_size():
    assert first_trade_qty(78.0, 1, 0.5) == 78.0          # repeat name
    assert first_trade_qty(78.0, -1, 0.5) == 78.0         # count unknown
    assert first_trade_qty(78.0, 0, 1.0) == 78.0          # switch off
    assert first_trade_qty(78.0, 0, 0.0) == 78.0          # invalid -> off


def test_fractional():
    assert abs(first_trade_qty(3.3333, 0, 0.5, fractional=True) - 1.6666) < 1e-3


def test_fixed_qty_one_share_first_only():
    assert first_trade_qty(78.0, 0, 1.0, fixed_qty=1) == 1.0          # first buy -> exactly one share
    assert first_trade_qty(78.0, 0, 0.5, fixed_qty=1) == 1.0          # fixed wins over the mult
    assert first_trade_qty(78.0, 1, 0.5, fixed_qty=1) == 78.0         # second trade full size
    assert first_trade_qty(78.0, -1, 1.0, fixed_qty=1) == 78.0        # unknown count keeps size
    assert first_trade_qty(3.3333, 0, 1.0, fractional=True, fixed_qty=1) == 1.0
    assert first_trade_qty(0.5, 0, 1.0, fractional=True, fixed_qty=1) == 0.5   # never above the plan
    assert first_trade_qty(78.0, 0, 1.0, fixed_qty=0) == 78.0         # off


def test_fixed_qty_key_is_live_tunable():
    import config
    assert config.DEFAULT_CONFIG["ai_first_trade_fixed_qty"] == 0.0
    assert "ai_first_trade_fixed_qty" in config.SAFE_CONFIG_KEYS
