"""Offline tests for research excellence / token-efficiency scoring."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.research_quality import efficiency_metrics, score_research_text  # noqa: E402

EXCELLENT = """{
  "macro_one_liner": "Soft landing bets firm into 2026 as cuts resume",
  "themes": ["AI infra", "power", "defense"],
  "suggestions": [
    {
      "symbol": "VRT",
      "score": 8.5,
      "reason": "data center power backlog",
      "p30": 0.55,
      "p50": 0.30,
      "p100": 0.12,
      "position_pct": 8,
      "invalidation": "break below $80 or order cuts",
      "summary": "Cooling leader; valuation risk if AI capex slows."
    },
    {
      "symbol": "CEG",
      "score": 8.0,
      "reason": "nuclear + AI power",
      "p30": 0.50,
      "p50": 0.28,
      "p100": 0.10,
      "position_pct": 7,
      "invalidation": "policy reverse or 2026 guidance cut",
      "summary": "Power scarcity thesis; regulatory risk."
    },
    {
      "symbol": "CRDO",
      "score": 7.5,
      "reason": "AEC interconnect ramp",
      "p30": 0.45,
      "p50": 0.25,
      "p100": 0.08,
      "position_pct": 6,
      "invalidation": "design-win losses",
      "summary": "High beta AI interconnect; competition risk."
    },
    {
      "symbol": "MOD",
      "score": 7.2,
      "reason": "EV thermal + data center",
      "p30": 0.40,
      "p50": 0.22,
      "p100": 0.08,
      "position_pct": 5,
      "invalidation": "OEM destock 2026",
      "summary": "Thermal content growth; auto cyclicality."
    },
    {
      "symbol": "FIX",
      "score": 7.0,
      "reason": "mech contractor backlog",
      "p30": 0.42,
      "p50": 0.20,
      "p100": 0.07,
      "position_pct": 5,
      "invalidation": "data center build pause",
      "summary": "Services leverage to AI builds; labor risk."
    }
  ],
  "portfolio": {"VRT": 8, "CEG": 7, "CASH": 40},
  "disclaimer": "research only"
}

Process notes: Q2 2025 earnings, Fed path, $90 support, 15% upside, bear case
competition and dilution risk if AI demand slows.
"""

EMPTY_FLUFF = """
Markets look interesting. Some stocks may go up. AI is big.
Consider diversification and talk to an advisor.
"""


def test_excellent_report_scores_high():
    q = score_research_text(EXCELLENT)
    assert q["parse_ok"] is True
    assert q["n_suggestions"] == 5
    assert q["json_first"] is True
    assert q["has_themes"] and q["has_macro"] and q["has_portfolio"]
    assert q["field_fill_rate"] > 0.7
    assert q["quality_0_100"] >= 70


def test_fluff_scores_low():
    q = score_research_text(EMPTY_FLUFF)
    assert q["parse_ok"] is False
    assert q["n_suggestions"] == 0
    assert q["quality_0_100"] < 25


def test_efficiency_prefers_quality_per_token_not_time():
    q = score_research_text(EXCELLENT)
    usage = {
        "total_cost_usd": 0.40,
        "input_tokens": 1000,
        "output_tokens": 5000,
        "cache_creation_input_tokens": 14000,
        "cache_read_input_tokens": 50000,
        "num_turns": 8,
    }
    eff = efficiency_metrics(q, usage, wall_sec=9999.0)
    assert eff["wall_sec"] == 9999.0  # recorded
    assert eff["quality_per_usd"] is not None
    assert eff["quality_per_1k_tokens"] is not None
    assert eff["quality_per_1k_fresh_tokens"] is not None
    assert eff["tokens_per_suggestion"] is not None
    # Higher quality at same tokens ⇒ higher efficiency
    weak = {**q, "quality_0_100": q["quality_0_100"] * 0.5}
    eff_weak = efficiency_metrics(weak, usage, wall_sec=1.0)
    assert eff["quality_per_1k_tokens"] > eff_weak["quality_per_1k_tokens"]


def test_matrix_ids_unique():
    from tools.research_ab import default_matrix
    ids = [c["id"] for c in default_matrix()]
    assert len(ids) == len(set(ids))
    assert "claude_xhigh_t8" in ids
    assert "grok_t8" in ids
