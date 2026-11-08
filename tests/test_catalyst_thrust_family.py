import json
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "studies"))

import catalyst_thrust_family as ctf

ET = ZoneInfo("America/New_York")


def test_prior_low_ratchet_is_strictly_causal_and_gaps_fill_at_open():
    """Prove that:
      1. On bar k, stop s only uses l[0..k-1], never l[k].
      2. If o[k] < s, the exit price is o[k], not s (no fantasy slippage-free fills).
    """
    bars = [
        {"t": "2026-03-02T14:30:00+00:00", "o": 100.0, "h": 102.0, "l": 99.0, "c": 101.0, "v": 1000},
        {"t": "2026-03-02T14:31:00+00:00", "o": 101.0, "h": 102.0, "l": 99.2, "c": 101.5, "v": 1000},
        {"t": "2026-03-02T14:32:00+00:00", "o": 101.5, "h": 102.0, "l": 99.8, "c": 101.8, "v": 1000},
        {"t": "2026-03-02T14:33:00+00:00", "o": 101.8, "h": 102.0, "l": 99.5, "c": 100.0, "v": 1000},
    ]

    res = ctf.ratchet_trade(bars, start_idx=0, H=3, cost_bp=0.0)
    assert res is not None
    assert res["stopped"] is True
    assert res["exit_bar"] == 3
    assert abs(res["exit_px"] - 99.8) < 1e-9

    bars_gap = [
        {"t": "2026-03-02T14:30:00+00:00", "o": 100.0, "h": 101.0, "l": 98.0, "c": 100.5, "v": 1000},
        {"t": "2026-03-02T14:31:00+00:00", "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1000},
        {"t": "2026-03-02T14:32:00+00:00", "o": 96.0, "h": 97.0, "l": 95.0, "c": 96.0, "v": 1000},
    ]
    res_gap = ctf.ratchet_trade(bars_gap, start_idx=0, H=2, cost_bp=0.0)
    assert res_gap is not None
    assert res_gap["stopped"] is True
    assert res_gap["exit_bar"] == 2
    assert abs(res_gap["exit_px"] - 96.0) < 1e-9, f"Expected gapped fill at 96.0, got {res_gap['exit_px']}"


def test_news_timestamp_strictly_enters_on_next_bar_open():
    """Prove that news at 09:30:01 cannot enter at 09:30:00; it must wait for 09:31:00."""
    bars = [
        {"t": "2026-03-02T09:30:00-05:00", "o": 100.0, "h": 101.0, "l": 99.5, "c": 100.5, "v": 1000},
        {"t": "2026-03-02T09:31:00-05:00", "o": 100.5, "h": 102.0, "l": 100.2, "c": 101.5, "v": 1000},
        {"t": "2026-03-02T09:32:00-05:00", "o": 101.5, "h": 102.5, "l": 101.0, "c": 102.0, "v": 1000},
    ]

    t_news = datetime.fromisoformat("2026-03-02T09:30:01-05:00")
    b_idx = ctf.entry_bar_for_news(t_news, bars)
    assert b_idx == 1, f"Expected entry at bar 1 (09:31:00), got {b_idx}"

    t_news_0 = datetime.fromisoformat("2026-03-02T09:30:00-05:00")
    b_idx_0 = ctf.entry_bar_for_news(t_news_0, bars)
    assert b_idx_0 == 1, f"Expected entry at bar 1 (09:31:00), got {b_idx_0}"

    t_news_intra = datetime.fromisoformat("2026-03-02T09:31:15-05:00")
    b_idx_intra = ctf.entry_bar_for_news(t_news_intra, bars)
    assert b_idx_intra == 2, f"Expected entry at bar 2 (09:32:00), got {b_idx_intra}"

    t_pre = datetime.fromisoformat("2026-03-02T09:24:59-05:00")
    assert ctf.is_premarket_catalyst(t_pre, "2026-03-02") is True

    t_late = datetime.fromisoformat("2026-03-02T09:25:01-05:00")
    assert ctf.is_premarket_catalyst(t_late, "2026-03-02") is False


def test_causal_control_pool_strictly_forbids_future_dates():
    """Prove that future-dated candidates (c['day'] > trig['day']) are NEVER drawn into ca or BASE."""
    ctrl_pool = [
        {"day": "2024-03-10", "sym": "PAST_1"},
        {"day": "2024-03-14", "sym": "PAST_2"},
        {"day": "2024-03-15", "sym": "SAME_DAY_1"},
        {"day": "2024-03-15", "sym": "SAME_DAY_2"},
        {"day": "2024-03-16", "sym": "FUTURE_1"},
        {"day": "2025-11-20", "sym": "FUTURE_2"},
    ]
    trig_day = "2024-03-15"
    trig_sym = "TRIG_SYM"

    pool = ctf.causal_control_pool(ctrl_pool, trig_day, trig_sym)
    for c in pool:
        assert c["day"] <= trig_day, f"Look-ahead violation: {c['day']} > {trig_day}"
        assert c["sym"] != trig_sym
    assert "FUTURE_1" not in [c["sym"] for c in pool]
    assert "FUTURE_2" not in [c["sym"] for c in pool]


def _synthetic_candidate(day: str, sym: str, half: str, is_primary: bool, gap: float, b0_green: bool = True, seed: int = 1):
    rng = np.random.default_rng(seed)
    bars = []
    base_px = 50.0
    dt_base = datetime.fromisoformat(f"{day}T09:30:00-05:00")
    
    b0_o = base_px
    b0_c = base_px * (1.005 if b0_green else 0.995)
    b0_h = max(b0_o, b0_c) + 0.10
    b0_l = min(b0_o, b0_c) - 0.10
    bars.append({
        "t": dt_base.isoformat(),
        "o": b0_o, "h": b0_h, "l": b0_l, "c": b0_c, "v": 10000.0,
    })

    cur_px = b0_c
    for m in range(1, 100):
        dt_m = datetime.fromtimestamp(dt_base.timestamp() + m * 60, tz=ET)
        ret = rng.normal(0.0002, 0.001)
        o_m = cur_px
        c_m = cur_px * (1.0 + ret)
        h_m = max(o_m, c_m) + 0.05
        l_m = min(o_m, c_m) - 0.05
        bars.append({
            "t": dt_m.isoformat(),
            "o": o_m, "h": h_m, "l": l_m, "c": c_m, "v": 5000.0,
        })
        cur_px = c_m

    return {
        "day": day,
        "sym": sym,
        "half": half,
        "gap": gap,
        "is_primary": is_primary,
        "has_premarket_news": is_primary,
        "entry_px": bars[1]["o"],
        "b0_is_green": b0_green,
        "b0_body_ratio": (b0_c - b0_o) / (b0_h - b0_l) if b0_green else 0.0,
        "bars": bars,
    }


def test_planted_and_null_canaries_pass_with_zero_control_shift():
    """Verify that deep-copy bar mutation detects +8 bp with zero control shift for ca and cb."""
    days = [f"2026-03-{d:02d}" for d in range(1, 20)]
    trigs = []
    ctrl_pool = []

    seed = 42
    for d_idx, day in enumerate(days):
        half = "A" if d_idx < 10 else "B"
        for s_idx in range(2):
            seed += 1
            trigs.append(_synthetic_candidate(day, f"TRIG_{s_idx}", half, is_primary=True, gap=0.06, seed=seed))
        for c_idx in range(5):
            seed += 1
            ctrl_pool.append(_synthetic_candidate(day, f"CTRL_{c_idx}", half, is_primary=False, gap=0.05, seed=seed))

    triggers_by_hyp = {"H_OPEN": trigs, "H_OPEN_GAP8": [t for t in trigs if t["gap"] >= 0.08] or trigs}
    ok, bad, summary = ctf.canary_check(triggers_by_hyp, ctrl_pool)
    assert ok is True, f"Canary failed: {bad}"
    assert "H_OPEN" in summary
    assert abs(summary["H_OPEN"]["planted_mean_bp"] - 8.0) < 0.5
    assert summary["H_OPEN"]["max_ca_shift_bp"] < 0.5
    assert summary["H_OPEN"]["max_cb_shift_bp"] < 0.5
    assert summary["H_OPEN"]["row_mismatch"] == 0


def test_run_refuses_second_read(tmp_path, monkeypatch):
    """Verify that run_single_read refuses if run.pkl or result.json exists, or if count.json is missing."""
    monkeypatch.setattr(ctf, "OUT", tmp_path)

    # 1. Missing count.json
    with pytest.raises(SystemExit) as exc:
        ctf.run_single_read()
    assert "count.json does not exist" in str(exc.value)

    # 2. Canary plan not OK
    (tmp_path / "count.json").write_text(json.dumps({"canary_plan_ok": False}))
    with pytest.raises(SystemExit) as exc:
        ctf.run_single_read()
    assert "Canary plan is not OK" in str(exc.value)

    # 3. run.pkl exists (anti-peeking lock)
    (tmp_path / "count.json").write_text(json.dumps({"canary_plan_ok": True}))
    (tmp_path / "run.pkl").write_text("locked")
    with pytest.raises(SystemExit) as exc:
        ctf.run_single_read()
    assert "CRITICAL P4 GATE VIOLATION" in str(exc.value)


def test_evaluator_verdicts():
    """Verify TEST_STANDARD verdict states: EDGE, PROFITABLE, UNDERPOWERED, VOID, FAIL."""
    rows_few = [{"day": "2026-03-01", "half": "A", "net": 10.0, "net_s": 6.0, "ca": 2.0, "cb": 2.0}]
    base_rows = [{"key": "H_OPEN", "net": 2.0}]
    res_under = ctf.evaluate(rows_few, base_rows, "H_OPEN")
    assert res_under["status"] == "UNDERPOWERED"

    rows_void = []
    for d in range(70):
        day_str = f"2026-01-{d:03d}"
        half = "A" if d < 35 else "B"
        for k in range(5):
            rows_void.append({
                "day": day_str, "half": half,
                "net": 20.0, "net_s": 16.0, "ca": 15.0, "cb": 10.0
            })
    base_rows_void = [{"key": "H_OPEN", "net": 2.0}]
    res_void = ctf.evaluate(rows_void, base_rows_void, "H_OPEN")
    assert res_void["status"] == "VOID"

    rng = np.random.default_rng(123)
    rows_prof = []
    base_rows_prof = [{"key": "H_OPEN", "net": 2.0}]
    for d in range(70):
        day_str = f"2026-01-{d:03d}"
        half = "A" if d < 35 else "B"
        day_noise = float(rng.normal(0, 1.0))
        for k in range(5):
            rows_prof.append({
                "day": day_str, "half": half,
                "net": 15.0 + day_noise, "net_s": 11.0 + day_noise, "ca": 2.0, "cb": 5.0
            })
    res_prof = ctf.evaluate(rows_prof, base_rows_prof, "H_OPEN")
    assert res_prof["status"] == "PROFITABLE"
    assert abs(res_prof["pooled_lift_bp"] - 13.0) < 0.5
    assert res_prof["t_stat"] > res_prof["t_crit"]
