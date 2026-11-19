import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "studies"))
import options_callflow as O  # noqa: E402


def test_utc_iso_handles_dst():
    assert O.utc_iso("2026-09-02", 9, 30) == "2026-09-02T13:30:00Z"
    assert O.utc_iso("2026-11-17", 9, 30) == "2026-11-17T14:30:00Z"


def test_premium_series_buckets_calls_and_puts_by_minute():
    bars = {"A260904C00330000": [{"t": "2026-09-02T13:31:00Z", "v": 10, "vw": 2.0}],
            "A260904P00310000": [{"t": "2026-09-02T13:31:00Z", "v": 5, "vw": 1.0}, {"t": "2026-09-02T12:00:00Z", "v": 9, "vw": 9}]}
    C, P = O.premium_series(bars, {"A260904C00330000": "call", "A260904P00310000": "put"})
    assert C[1] == 2000.0 and P[1] == 500.0 and P.sum() == 500.0      # pre-open bar dropped


def test_signals_burst_ratio_floor_and_refractory():
    C, P = np.zeros(390), np.zeros(390)
    C[40:55] = 10_000.0                      # 150k over 15 min
    C[60:75] = 10_000.0                      # inside the 30-min refractory of the first burst
    C[120:135] = 10_000.0; P[120:135] = 5_000.0   # ratio 2 < 3
    sig = O.signals(C, P, base_c=20_000.0)   # 5x base = 100k
    # fires once 10 of the last 15 minutes carry 10k (t=49); the refractory holds until t=79 (tail of burst 2);
    # the put-balanced burst at 120-134 (ratio 2) never fires
    assert sig == [49, 79]
    assert O.signals(C, P, base_c=None) == []
    small = np.zeros(390); small[40:55] = 1000.0
    assert O.signals(small, np.zeros(390), base_c=100.0) == []           # under the $50k floor


def test_verdict():
    assert O.verdict({"n": 10})[0] == "UNDERPOWERED"
    assert O.verdict({"n": 300, "mean_net": 3.0, "t_day": 2.6, "half1": 1, "half2": 1})[0] == "PASS"
