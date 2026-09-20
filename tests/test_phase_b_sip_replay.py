"""Phase B historical SIP square replay — Gate 1 fixtures."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import phase_b_sip_replay as sip  # noqa: E402

FIX = _ROOT / "tests" / "fixtures" / "phase_b_sip_replay"


def _bars_df(name: str) -> tuple[str, pd.DataFrame]:
    raw = json.loads((FIX / name).read_text(encoding="utf-8"))
    day = raw["day"]
    df = pd.DataFrame(raw["bars"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.set_index("ts").sort_index()
    return day, df


def test_clear_112_true_on_120_bars():
    day, df = _bars_df("bars_clear_120.json")
    n = sip.count_bars_in_window(df.index, day)
    assert n == 120
    assert sip.clear_112(n) is True
    m = sip.square_and_pre_square_minutes(df, day)
    assert m["sip_bars_0415_0920"] == 120
    assert m["clear_112"] is True


def test_clear_112_false_on_50_bars():
    day, df = _bars_df("bars_thin_50.json")
    n = sip.count_bars_in_window(df.index, day)
    assert n == 50
    assert sip.clear_112(n) is False
    m = sip.square_and_pre_square_minutes(df, day)
    assert m["sip_bars_0415_0920"] == 50
    assert m["clear_112"] is False


def test_square_minutes_positive_on_ob_grind():
    day, df = _bars_df("bars_square.json")
    m = sip.square_and_pre_square_minutes(df, day)
    assert m["clear_112"] is True
    assert m["square_minutes"] > 0
    assert m["pre_square_minutes"] >= m["square_minutes"]


def test_collect_admits_dedupes_and_skips_refuse():
    rows = [
        {"kind": "admit", "symbol": "AAA", "source": "momentum"},
        {"kind": "admit", "symbol": "aaa", "source": "momentum"},
        {"kind": "admit_refuse", "symbol": "BBB", "source": "momentum"},
        {"kind": "dry_armed", "symbol": "CCC", "source": "movers"},
        {"kind": "config_fingerprint"},
    ]
    got = sip.collect_admits_from_rows(rows)
    syms = [g["symbol"] for g in got]
    assert syms == ["AAA", "CCC"]
    assert got[0]["universe"] == "phase_b_admit"


def test_summary_go_fixture_is_thin_without_sample_floor(tmp_path):
    """1-day / 10-pair fixture rates look like GO but must not subscribe."""
    out = tmp_path / "out"
    code = sip.main([
        "--fixture-dir", str(FIX / "summary_go"),
        "--out", str(out),
    ])
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"]["decision"] == "thin"
    assert summary["admit"]["sip_clear_rate"] == pytest.approx(0.7)
    assert "provenance" in summary
    assert summary["provenance"].get("git_sha")
    assert code == 1


def test_verdict_go_when_sample_sufficient():
    m = {
        "n_pairs": 60,
        "n_days": 5,
        "sip_clear_rate": 0.70,
        "iex_clear_rate": 0.0,
        "median_sip_bars_on_clear": 250.0,
    }
    v = sip.verdict_from_metrics(m)
    assert v["decision"] == "go"


def test_verdict_thin_when_days_short():
    m = {
        "n_pairs": 60,
        "n_days": 2,
        "sip_clear_rate": 0.70,
        "iex_clear_rate": 0.0,
        "median_sip_bars_on_clear": 250.0,
    }
    v = sip.verdict_from_metrics(m)
    assert v["decision"] == "thin"


def test_summary_nogo_fixture_is_thin_then_unit_nogo(tmp_path):
    out = tmp_path / "out"
    code = sip.main([
        "--fixture-dir", str(FIX / "summary_nogo"),
        "--out", str(out),
    ])
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    # Fixture is 1 day — sample floor wins before rate bars.
    assert summary["verdict"]["decision"] == "thin"
    assert summary["admit"]["sip_clear_rate"] == pytest.approx(0.2)
    assert code == 1
    v = sip.verdict_from_metrics({
        "n_pairs": 60,
        "n_days": 5,
        "sip_clear_rate": 0.2,
        "iex_clear_rate": 0.0,
        "median_sip_bars_on_clear": 80.0,
    })
    assert v["decision"] == "no-go"


def test_summary_later_fixture_thin_then_unit_later(tmp_path):
    out = tmp_path / "out"
    code = sip.main([
        "--fixture-dir", str(FIX / "summary_later_iex"),
        "--out", str(out),
    ])
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"]["decision"] == "thin"
    assert code == 1
    v = sip.verdict_from_metrics({
        "n_pairs": 60,
        "n_days": 5,
        "sip_clear_rate": 0.55,
        "iex_clear_rate": 0.50,
        "median_sip_bars_on_clear": 200.0,
    })
    assert v["decision"] == "later"


def test_verdict_frozen_constants_documented():
    src = (_ROOT / "tools" / "phase_b_sip_replay.py").read_text(encoding="utf-8")
    assert "GO_SIP_CLEAR_MIN = 0.60" in src
    assert "GO_MEDIAN_BARS_ON_CLEAR = 150" in src
    assert "NOGO_SIP_CLEAR_MAX = 0.40" in src
    assert "MIN_SESSIONS_FOR_VERDICT = 5" in src
    assert "MIN_PAIRS_FOR_VERDICT = 50" in src
    assert "do not retune" in src.lower()


def test_score_symbol_day_offline_dfs():
    day, sip_df = _bars_df("bars_square.json")
    _, thin = _bars_df("bars_thin_50.json")
    rec = sip.score_symbol_day(
        "SQ",
        day,
        sip_df=sip_df,
        iex_df=thin,
    )
    assert rec["sip_clear_112"] is True
    assert rec["iex_clear_112"] is False
    assert rec["square_minutes"] > 0
    assert rec["sip_bars_0415_0920"] >= 112


def test_filter_pairs_by_prior_dollar_vol_annotated():
    pairs = [
        {"day": "2026-09-17", "symbol": "FAT", "universe": "phase_b_admit",
         "prior_dollar_vol": 5_000_000, "sip_clear_112": True,
         "sip_bars_0415_0920": 200, "iex_bars_0415_0920": 0,
         "iex_clear_112": False, "square_minutes": 1, "pre_square_minutes": 2},
        {"day": "2026-09-17", "symbol": "THIN", "universe": "phase_b_admit",
         "prior_dollar_vol": 100_000, "sip_clear_112": False,
         "sip_bars_0415_0920": 20, "iex_bars_0415_0920": 0,
         "iex_clear_112": False, "square_minutes": 0, "pre_square_minutes": 0},
        {"day": "2026-09-17", "symbol": "UNK", "universe": "phase_b_admit",
         "sip_clear_112": False, "sip_bars_0415_0920": 10,
         "iex_bars_0415_0920": 0, "iex_clear_112": False,
         "square_minutes": 0, "pre_square_minutes": 0},
    ]
    kept, dig = sip.filter_pairs_by_prior_dollar_vol(
        pairs, 2_000_000, fetch=False,
    )
    assert [r["symbol"] for r in kept] == ["FAT"]
    assert dig["n_dropped_below"] == 1
    assert dig["n_dropped_unknown"] == 1
    summary = sip.summarize_pairs(kept)
    # 1 day / 1 pair — rates can look GO; sample floor must refuse subscribe.
    assert summary["verdict"]["decision"] == "thin"
    assert summary["admit"]["sip_clear_rate"] == 1.0
