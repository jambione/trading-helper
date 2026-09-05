"""Seed-rank: freeze desk seeds, parse in-list ranks, board → watch candidates."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import seed_rank as sr  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REPORT_DIR", str(tmp_path))
    monkeypatch.setattr(sr, "SEED_RANK_AGY", tmp_path / "seed_rank_agy.json")
    monkeypatch.setattr(sr, "SEED_RANK_GROK", tmp_path / "seed_rank_grok.json")
    monkeypatch.setattr(sr, "PROMPT_FILE", _ROOT / "ai_seed_rank_prompt.txt")
    yield


def test_parse_keeps_only_seed_symbols():
    text = json.dumps({
        "champion": "AAA",
        "suggestions": [
            {"symbol": "AAA", "score": 9, "reason": "hot"},
            {"symbol": "ZZZ", "score": 8, "reason": "not in list"},
            {"symbol": "BBB", "score": 7, "reason": "ok"},
            {"symbol": "CCC", "score": 6, "reason": "ok"},
            {"symbol": "DDD", "score": 5, "reason": "ok"},
            {"symbol": "EEE", "score": 4, "reason": "ok"},
            {"symbol": "FFF", "score": 3, "reason": "sixth — drop"},
        ],
    })
    out = sr.parse_rank_response(text, {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF"}, max_n=5)
    assert [r["symbol"] for r in out] == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    assert "ZZZ" not in {r["symbol"] for r in out}


def test_parse_empty_when_all_off_list():
    text = '{"suggestions":[{"symbol":"NVDA","score":9,"reason":"no"}]}'
    assert sr.parse_rank_response(text, {"AAA", "BBB"}) == []


def test_build_prompt_embeds_seeds():
    frozen = {
        "et": "2026-09-05 09:25:00 EDT",
        "n": 2,
        "rows": [
            {"symbol": "AAA", "source": "momentum", "price": 5.0},
            {"symbol": "BBB", "source": "movers", "price": 3.0},
        ],
    }
    prompt = sr.build_prompt(frozen, max_n=5)
    assert "AAA" in prompt and "BBB" in prompt
    assert "SEED_LIST" in prompt or "seeds" in prompt
    assert "buy now" not in prompt.lower() or "Do NOT say" in prompt


def test_freeze_drops_research_sources(monkeypatch):
    def fake_desk(_cfg=None):
        return [
            {"symbol": "MOM", "source": "momentum", "price": 4.0, "pct_change": 20, "rvol": 6},
            {"symbol": "RES", "source": "agy", "price": 4.0, "pct_change": 20, "rvol": 6},
            {"symbol": "MV", "source": "movers", "price": 5.0, "pct_change": 15, "rvol": 8},
            {"symbol": "BRO", "source": "bb_live", "price": 5.0, "pct_change": 15, "rvol": 8},
        ]

    monkeypatch.setattr("ai_entry_watch.desk_candidate_rows", fake_desk)
    frozen = sr.freeze_seed_union({})
    syms = {r["symbol"] for r in frozen["rows"]}
    assert syms == {"MOM", "MV"}
    assert frozen["n"] == 2


def test_agree_suggestions_intersection_only():
    a = [
        {"symbol": "AAA", "score": 9, "reason": "a1"},
        {"symbol": "BBB", "score": 8, "reason": "a2"},
        {"symbol": "CCC", "score": 7, "reason": "a3"},
    ]
    x = [
        {"symbol": "BBB", "score": 6, "reason": "x2"},
        {"symbol": "DDD", "score": 9, "reason": "x4"},
        {"symbol": "AAA", "score": 5, "reason": "x1"},
    ]
    out = sr.agree_suggestions(a, x, max_n=5)
    # AAA avg 7.0, BBB avg 7.0 — only the intersection, never solo picks.
    assert {r["symbol"] for r in out} == {"AAA", "BBB"}
    assert all(r.get("agreement") and r.get("source_mark") == "GX" for r in out)
    assert "CCC" not in {r["symbol"] for r in out}
    assert "DDD" not in {r["symbol"] for r in out}

def test_publish_agreement_writes_only_shared_names(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "SEED_RANK_AGY", tmp_path / "seed_rank_agy.json")
    monkeypatch.setattr(sr, "SEED_RANK_GROK", tmp_path / "seed_rank_grok.json")
    monkeypatch.setattr(sr, "SEED_RANK_AX", tmp_path / "seed_rank_ax.json")
    slot = "2026-09-05T09:25"
    frozen = {"ts": time.time(), "n": 3, "rows": []}
    sr.write_raw("agy", slot, [
        {"symbol": "AAA", "score": 9, "reason": "a"},
        {"symbol": "BBB", "score": 8, "reason": "b"},
    ], frozen=frozen)
    sr.write_raw("xai", slot, [
        {"symbol": "BBB", "score": 7, "reason": "bx"},
        {"symbol": "CCC", "score": 9, "reason": "c"},
    ], frozen=frozen)
    pub = sr.publish_agreement(
        {"ai_seed_rank_require_agreement": True, "ai_seed_rank_max": 5},
        slot, frozen,
    )
    assert pub["symbols"] == ["BBB"]
    ax = json.loads((tmp_path / "seed_rank_ax.json").read_text())
    assert [r["symbol"] for r in ax["rows"]] == ["BBB"]
    assert ax["rows"][0]["agreement"] is True


def test_write_board_is_picked_up_by_research_candidates(monkeypatch, tmp_path):
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "ROOT", tmp_path)
    for name in ("claude_suggestions.json", "suggestions.json", "grok_suggestions.json"):
        p = tmp_path / name
        if p.exists():
            p.unlink()

    frozen = {"ts": time.time(), "n": 1, "rows": [{"symbol": "AAA", "source": "momentum"}]}
    suggestions = [{"symbol": "AAA", "score": 8.5, "reason": "catalyst",
                    "agreement": True, "source_mark": "GX"}]
    sr.SEED_RANK_AX = tmp_path / "seed_rank_ax.json"
    sr.write_board("ax", suggestions, frozen=frozen, slot="2026-09-05T09:25")
    rows = ew.research_candidate_rows()
    assert any(r.get("symbol") == "AAA" for r in rows)

def test_stale_seed_rank_board_is_ignored(monkeypatch, tmp_path):
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "ROOT", tmp_path)
    stale = {
        "ts": time.time() - 3 * 3600,
        "kind": "seed_rank",
        "source": "agy",
        "rows": [{"symbol": "OLD", "score": 9, "reason": "stale"}],
    }
    (tmp_path / "seed_rank_agy.json").write_text(json.dumps(stale))
    rows = ew.research_candidate_rows()
    assert not any(r.get("symbol") == "OLD" for r in rows)
