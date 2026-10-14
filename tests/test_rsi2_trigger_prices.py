"""RSI-2 next-bar trigger prices (operator 2026-10-14): the computed price really flips RSI-2 past 90 / 10."""
import ai_entry_watch as A


def _rsi_last(closes):
    return A.cm_rsi_series(closes, 2)[-1]


def test_trigger_prices_flip_rsi2_exactly():
    closes = [10.00, 10.02, 9.99, 10.01, 10.00, 10.03, 10.01]
    red, green = A.rsi2_trigger_prices(closes)
    assert _rsi_last(closes + [red + 0.0001]) > 90 and _rsi_last(closes + [red - 0.0005]) <= 90
    assert _rsi_last(closes + [green - 0.0001]) < 10 and _rsi_last(closes + [green + 0.0005]) >= 10


def test_already_extreme_needs_only_a_tick_and_short_series_is_none():
    rising = [10.0 + 0.01 * i for i in range(10)]               # no down moves: any up close is red
    red, _ = A.rsi2_trigger_prices(rising)
    assert abs(red - (rising[-1] + 0.01)) < 1e-9
    assert A.rsi2_trigger_prices([10.0, 10.1]) == (None, None)
