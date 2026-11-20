import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import desk_vs_spy as dv  # noqa: E402


def _fill(ts, side, sym, px, qty):
    return {"event": "fill", "ts": ts, "side": side, "symbol": sym, "filled_avg_price": px, "filled_qty": qty}


def test_round_trips_fifo_and_partial_fills():
    rows = [_fill(1, "buy", "A", 10.0, 10), _fill(2, "buy", "A", 11.0, 5), _fill(3, "sell", "A", 12.0, 12),
            _fill(4, "sell", "A", 13.0, 3), {"event": "submit", "symbol": "A"}]
    t = dv.round_trips(rows, "2026-11-20")
    assert [(x["entry"], x["exit"], x["qty"]) for x in t] == [(10.0, 12.0, 10.0), (11.0, 12.0, 2.0), (11.0, 13.0, 3.0)]


def test_spy_ret_from_opens_uses_minute_of_each_timestamp():
    day = "2026-11-20"
    t0 = 1795173000.0 - (1795173000.0 % 60)
    from datetime import datetime
    open_ts = datetime.fromisoformat(day + "T09:30").replace(tzinfo=dv.ET).timestamp()
    o = np.full(390, 400.0)
    o[10:] = 404.0
    r = dv.spy_ret_from_opens(o, open_ts + 5 * 60 + 10, open_ts + 12 * 60 + 5, day)
    assert abs(r - (404 / 400 - 1)) < 1e-12
    assert dv.spy_ret_from_opens(np.full(390, np.nan), open_ts, open_ts + 600, day) is None


def test_spy_ret_from_snaps_nearest_within_three_minutes():
    snaps = [(1000.0, 100.0), (1300.0, 101.0)]
    assert abs(dv.spy_ret_from_snaps(snaps, 1010.0, 1290.0) - 0.01) < 1e-12
    assert dv.spy_ret_from_snaps(snaps, 5000.0, 5100.0) is None


def test_score_name_minus_spy_and_dollars():
    trips = [{"day": "d", "sym": "A", "entry": 100.0, "exit": 99.0, "qty": 10.0, "entry_ts": 0, "exit_ts": 1},
             {"day": "d", "sym": "B", "entry": 50.0, "exit": 50.5, "qty": 20.0, "entry_ts": 0, "exit_ts": 1}]
    s = dv.score(trips, lambda t: -0.005)                    # SPY fell 50 bp over both holds
    assert s["n"] == 2 and s["spy_bp"] == -50.0 and s["raw_bp"] == -0.0 + (-100 + 100) / 2
    assert s["vs_spy_bp"] == 50.0 and s["beat_spy_pct"] == 50.0       # A -50 bp vs SPY, B +150 bp vs SPY
    assert s["usd_raw"] == 0.0 and abs(s["usd_net_of_market"] - (1000 * -0.005 + 1000 * 0.015)) < 1e-9
    assert dv.score(trips, lambda t: None) == {"n": 0}
