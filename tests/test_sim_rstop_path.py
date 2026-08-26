"""1-minute RSTOP path sim — synthetic bars, no Alpaca."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools.sim_rstop_path import path_cfg, summarize, walk_symbol  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(hour: int, minute: int, day: str = "2026-08-14") -> float:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(
        tzinfo=ET).timestamp()


def _bars(pxs: list[float], *, start_h: int = 10, start_m: int = 0) -> list:
    out = []
    for i, c in enumerate(pxs):
        m = start_m + i
        ts = _ts(start_h + m // 60, m % 60)
        o = pxs[i - 1] if i else c
        h = max(o, c) + 0.01
        low = min(o, c) - 0.01
        out.append((ts, o, h, low, c))
    return out


def _cfg(**over):
    """Config for the tests below, which are about the TRAIL, not the gates.

    Both entry filters are off. The exhaustion one always was; the CM RSI-2 one
    is off for the same reason and had to be named once the local RSI path
    started publishing a direction: these fixtures are three and four bars long,
    which is under the RSI-2 warmup, so every bar answered no_rsi_data and no
    trade ever opened to trail. Arm-gate behaviour is covered in
    test_ai_entry_watch.py against a series long enough to have one.

    MACD is the third gate to need naming here, for the identical reason and
    on a larger margin: it needs 26 + 9 bars before it produces anything at
    all, plus confirm_window + trend_lookback before it has a direction, so a
    three-bar fixture answers no_macd_data on every bar. Left on, these four
    tests fail with `assert []` — no trade ever opens, so there is no trail
    to measure and the verdict is vacuous rather than red. That is the exact
    failure mode HANDOFF §6 warns about for replay output.

    The MACD gate's own behaviour is covered in test_macd_gap_direction.py,
    which builds records directly instead of walking synthetic bars.
    """
    return path_cfg(
        ai_watch_exhaustion_rules=False,
        ai_watch_arm_require_cm_rsi=False,
        ai_watch_arm_require_macd=False,
        ai_watch_synth_stop_pct=5.0,
        ai_local_trail_give_r=0.10,
        ai_local_trail_give_open_r=0.10,
        ai_local_trail_min_give_px=0.0,
        # The give these tests arithmetic against is 0.10R = $0.05 on a $10
        # name. The live percent cap is 0.1% = $0.01, which overrides it and
        # parks the shelf one cent under entry — and _bars() draws every low
        # one cent under the previous close, so every fixture stopped out on
        # the bar after entry. The R knobs above are only the whole give with
        # the percent sibling named too (see ai_positions._trail_give).
        ai_local_trail_give_max_pct=0.0,
        ai_dead_trade_min=0,
        ai_reentry_cooldown_sec=0,
        **over,
    )


def test_flatten_through_seeded_rstop_same_session():
    # $10, 5% R=$0.50, 0.10R give=$0.05, shelf=$9.95.
    # Flat on purpose: the shelf under test is the SEEDED one, so the tape must
    # not ratchet it. The old fixture drifted up a cent a bar, which raised the
    # shelf above entry before the flush and made the exit a winner.
    pxs = [10.00, 10.00, 10.00, 10.00]
    bars = _bars(pxs)
    # Third-to-last stays above; last bar spikes the low through 9.95.
    ts, o, h, _l, c = bars[-1]
    bars[-1] = (ts, o, h, 9.90, c)
    res = walk_symbol("AAA", bars, _cfg(), fill="close", max_trades=1)
    assert len(res["trades"]) == 1
    t = res["trades"][0]
    assert t["reason"] == "local_trail"
    # Shelf may have already raised off last; still a scratch through it.
    assert t["exit"] < t["entry"]
    assert t["r"] < 0


def test_wash_never_enters():
    bars = _bars([10.0 + 0.05 * i for i in range(15)])
    res = walk_symbol(
        "DUMP", bars, _cfg(), look="WASH", fill="close", max_trades=1)
    assert res["trades"] == []
    assert res["refuse"].get("look_wash", 0) > 0


def test_runner_banks_positive_r_then_trails_out():
    # Grind up ~4% then slap through the raised shelf.
    pxs = [10.00 + 0.04 * i for i in range(12)]
    bars = _bars(pxs)
    ts, o, h, _l, c = bars[-1]
    bars[-1] = (ts, o, h, c - 0.40, c - 0.30)
    res = walk_symbol("RUN", bars, _cfg(), fill="close", t1_rr=0.0)
    assert res["trades"]
    t = res["trades"][0]
    assert t["reason"] == "local_trail"
    assert t["r"] > 0
    assert t["mfe_r"] > 0.5


def test_t1_blends_when_high_clears_target_before_trail():
    pxs = [10.00, 10.20, 10.25, 10.22]
    bars = _bars(pxs)
    ts, o, h, _l, c = bars[-1]
    bars[-1] = (ts, o, h, 10.00, 10.05)
    res = walk_symbol(
        "T1", bars, _cfg(), fill="close", t1_rr=0.30, max_trades=1)
    t = res["trades"][0]
    assert t["t1_hit"] is True
    # Half at +0.30R, half at the trail exit (near scratch) → still green-ish.
    assert t["r"] > 0.0


def test_eod_flattens_open_long():
    pxs = [10.00, 10.02, 10.03]
    bars = _bars(pxs, start_h=15, start_m=48)
    res = walk_symbol("EOD", bars, _cfg(), fill="close", t1_rr=0.0)
    assert res["trades"]
    assert res["trades"][0]["reason"] == "eod_flatten"


def test_summarize_empty_and_scratches():
    assert summarize([])["n"] == 0
    s = summarize([
        {"r": -0.1, "reason": "local_trail", "t1_hit": False},
        {"r": 0.4, "reason": "local_trail", "t1_hit": True},
    ])
    assert s["n"] == 2
    assert s["scratch"] == 1
    assert s["t1_hit"] == 1


def test_bars_file_round_trip(tmp_path):
    bars = _bars([10.0, 10.01, 9.5])
    ts, o, h, _l, c = bars[-1]
    bars[-1] = (ts, o, h, 9.50, 9.55)
    path = tmp_path / "bars.json"
    path.write_text(json.dumps({
        "AAA": [
            {"ts": b[0], "o": b[1], "h": b[2], "l": b[3], "c": b[4]}
            for b in bars
        ],
    }))
    from tools.sim_rstop_path import load_bars_file, main
    book = load_bars_file(path)
    assert "AAA" in book
    rc = main(["--bars-file", str(path), "--exh", "off", "--json"])
    assert rc == 0
