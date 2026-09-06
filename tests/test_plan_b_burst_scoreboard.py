"""Plan B burst scoreboard: latency fields + counterfactual smoke."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import plan_b_burst_scoreboard as sb  # noqa: E402

FIXTURE = _ROOT / "tests" / "fixtures" / "plan_b_burst_scoreboard.json"


def test_mapper_requires_latency_honesty_fields():
    row = {
        "symbol": "AAA",
        "signal_bar_ts": 1.0,
        "decision_ts": 4.0,
        "latency_sec": 3.0,
        "fill_model": "next_open",
        "cm_rsi": 80,
    }
    mapped = sb.map_signal_row(row)
    assert mapped is not None
    assert sb.required_latency_fields(mapped) == []


def test_mapper_flags_missing_latency_fields():
    mapped = sb.map_signal_row({"symbol": "AAA", "bar_ts": 1.0})
    assert mapped is not None
    # decision_ts falls back to bar_ts; latency derived; fill_model defaulted
    # in map? fill_model defaults to next_open in map_signal_row.
    assert mapped["fill_model"] == "next_open"
    assert "latency_sec" in mapped


def test_counterfactual_models_differ_on_fixture():
    signals, bars = sb.load_fixture(str(FIXTURE))
    assert len(signals) == 2
    scored = [sb.score_signal(s, bars) for s in signals]
    table = sb.counterfactual_table(scored)

    for model in ("signal_close", "next_open", "plus_2_open"):
        assert model in table
        assert table[model]["n_trades"] >= 1

    # AAA trends up: signal_close and next_open should both be scorable.
    aaa = next(r for r in scored if r["symbol"] == "AAA")
    assert aaa["models"]["signal_close"]["ok"]
    assert aaa["models"]["next_open"]["ok"]
    assert aaa["models"]["plus_2_open"]["ok"]

    # BBB hits the hard stop after next_open fill into a dump.
    bbb = next(r for r in scored if r["symbol"] == "BBB")
    assert bbb["models"]["next_open"]["ok"]
    assert bbb["models"]["next_open"]["exit_reason"] == "hard_stop"
    assert bbb["models"]["next_open"]["r"] < 0


def test_pass_fail_uses_next_open_not_win_pct_alone():
    signals, bars = sb.load_fixture(str(FIXTURE))
    scored = [sb.score_signal(s, bars) for s in signals]
    primary = sb.summarize(scored, "next_open")
    v = sb.verdict(primary, min_signals=30)
    # Tiny fixture cannot pass n>=30; that is the frozen bar.
    assert v["pass"] is False
    assert any(r.startswith("n_trades_") for r in v["reasons"])
    assert v["pass_fill_model"] == "next_open"


def test_verdict_fails_when_only_optimistic_fill_works():
    """If next_open med R <= 0, FAIL even if signal_close looks fine."""
    summary = {
        "fill_model": "next_open",
        "n_trades": 40,
        "med_r": -0.1,
        "sum_pnl_pct": -5.0,
        "expectancy_pct": -0.12,
        "win_pct": 60.0,
        "med_latency_sec": 5.0,
    }
    v = sb.verdict(summary, min_signals=30)
    assert v["pass"] is False
    assert "med_r_not_positive" in v["reasons"]
    assert "win_pct_only_med_r_nonpositive" in v["reasons"]


def test_verdict_fails_when_median_latency_exceeds_one_bar():
    summary = {
        "fill_model": "next_open",
        "n_trades": 40,
        "med_r": 0.2,
        "sum_pnl_pct": 8.0,
        "expectancy_pct": 0.2,
        "win_pct": 55.0,
        "med_latency_sec": 90.0,
    }
    v = sb.verdict(summary, min_signals=30)
    assert v["pass"] is False
    assert "median_latency_gt_one_bar" in v["reasons"]


def test_cli_smoke_on_fixture(capsys):
    code = sb.main(["--fixture", str(FIXTURE), "--min-signals", "30"])
    captured = capsys.readouterr()
    assert "Plan B burst scoreboard" in captured.out
    assert "next_open" in captured.out
    assert code == 1  # n too small for PASS


def test_strength_signal_rows_always_have_latency_fields(tmp_path, monkeypatch):
    """Integration: strength_signal output is scoreboard-ready."""
    import strength_signal as ss

    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    ss.reset_state()
    with (tmp_path / "signal_shadow.jsonl").open("w") as fh:
        fh.write(json.dumps({
            "ts": 1788872400.0,  # ~09:00 ET on OPEN day used in other tests
            "ticker": "AAA",
            "signal": "mention_burst",
        }) + "\n")

    # Reuse the OPEN_TS convention from test_strength_signal (2026-09-08 ~10:00).
    open_ts = 1788876000.0
    n = 30
    rows = [(10.1, 9.9, 10.0) for _ in range(n)]
    stamps = [open_ts - 60.0 * (n - 1 - k) for k in range(n)]

    class EW:
        def symbol_ohlc(self, *a, **k):
            return rows

        def _cached_ohlc_stamps(self, *a, **k):
            return stamps

        def cm_rsi_series(self, closes, period):
            return [85.0] * len(closes)

    out = ss.evaluate(["AAA"], {}, open_ts, ew=EW())
    assert out
    for row in out:
        assert sb.required_latency_fields(sb.map_signal_row(row)) == []
        assert row["fill_model"] == "next_open"
    ss.reset_state()
