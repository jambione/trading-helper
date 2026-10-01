"""The engine's live-price inject must keep %R inside -100..0 (MNKD 2026-10-01)."""
import numpy as np
import pandas as pd

import signal_engine as se
import signals


def _df(n=150):
    t = pd.date_range("2026-10-01T13:30:00Z", periods=n, freq="min")
    c = 10 + np.sin(np.arange(n) / 9) * 0.2
    return pd.DataFrame({"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "open": c, "high": c + 0.02,
                         "low": c - 0.02, "close": c, "volume": 100.0})


CFG = {"rte_fast_length": 21, "rte_fast_ewm_span": 7, "rte_slow_native_length": 112,
       "rte_slow_ewm_span": 3, "rte_threshold": 20}


def test_inject_widens_the_last_bar_to_contain_the_price():
    df = _df()
    hi = se.inject_live_price(df, 12.0)
    assert hi["close"].iloc[-1] == 12.0 and hi["high"].iloc[-1] == 12.0
    lo = se.inject_live_price(df, 8.0)
    assert lo["low"].iloc[-1] == 8.0
    assert df["close"].iloc[-1] != 12.0            # the cached frame is untouched


def test_percent_r_stays_in_range_when_price_breaks_out():
    for px in (12.0, 8.0):                          # far above / below the whole window
        out = signals.compute_percent_r_exhaustion(se.inject_live_price(_df(), px), CFG).iloc[-1]
        for col in ("s_percentR", "l_percentR"):
            assert -100.0 - 1e-9 <= out[col] <= 1e-9, (px, col, out[col])
    # A real breakout still pushes the (EMA-smoothed) line up; it just cannot pass 0.
    base = signals.compute_percent_r_exhaustion(_df(), CFG).iloc[-1]["s_percentR"]
    up = signals.compute_percent_r_exhaustion(se.inject_live_price(_df(), 12.0), CFG).iloc[-1]["s_percentR"]
    assert up > base
