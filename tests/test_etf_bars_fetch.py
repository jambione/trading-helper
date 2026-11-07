import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import pandas as pd
from etf_bars_fetch import day_arrays


def test_day_arrays_keeps_regular_hours_only():
    idx = pd.MultiIndex.from_tuples([("SMH", pd.Timestamp("2026-10-14 13:29", tz="UTC")), ("SMH", pd.Timestamp("2026-10-14 13:30", tz="UTC")),
                                     ("SMH", pd.Timestamp("2026-10-14 20:00", tz="UTC"))], names=["symbol", "timestamp"])
    df = pd.DataFrame({"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3], "close": [1, 2, 3], "volume": [10, 20, 30]}, index=idx)
    a = day_arrays(df, "2026-10-14")
    assert list(a["syms"]) == ["SMH"] and list(a["minute"]) == [570] and list(a["c"]) == [2.0]
