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


def test_publish_mode_agreement_via_publish_mode_key(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "SEED_RANK_AX", tmp_path / "seed_rank_ax.json")
    monkeypatch.setattr(sr, "SEED_RANK_AGY", tmp_path / "seed_rank_agy.json")
    monkeypatch.setattr(sr, "SEED_RANK_GROK", tmp_path / "seed_rank_grok.json")
    slot = "2026-09-15T09:25"
    frozen = {"ts": time.time(), "n": 2, "rows": []}
    sr.write_raw("agy", slot, [
        {"symbol": "AAA", "score": 9, "reason": "a"},
        {"symbol": "BBB", "score": 8, "reason": "b"},
    ], frozen=frozen)
    sr.write_raw("xai", slot, [
        {"symbol": "BBB", "score": 7, "reason": "bx"},
        {"symbol": "CCC", "score": 9, "reason": "c"},
    ], frozen=frozen)
    pub = sr.publish_agreement(
        {
            "ai_seed_rank_require_agreement": False,
            "ai_seed_rank_publish_mode": "agreement",
            "ai_seed_rank_max": 5,
            "ai_seed_rank_second_opinion": False,
        },
        slot, frozen,
    )
    assert pub["publish_mode"] == "agreement"
    assert pub["symbols"] == ["BBB"]


def test_union_publishes_solo_agy_and_solo_grok(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "SEED_RANK_AX", tmp_path / "seed_rank_gx.json")
    monkeypatch.setattr(sr, "SEED_RANK_GX", tmp_path / "seed_rank_gx.json")
    monkeypatch.setattr(sr, "SEED_RANK_AGY", tmp_path / "seed_rank_agy.json")
    monkeypatch.setattr(sr, "SEED_RANK_GROK", tmp_path / "seed_rank_grok.json")
    slot = "2026-09-15T12:00"
    frozen = {"ts": time.time(), "n": 4, "rows": []}
    sr.write_raw("agy", slot, [
        {"symbol": "AAA", "score": 9, "reason": "agy hot"},
        {"symbol": "BBB", "score": 8, "reason": "shared"},
    ], frozen=frozen)
    sr.write_raw("xai", slot, [
        {"symbol": "BBB", "score": 7, "reason": "shared x"},
        {"symbol": "CCC", "score": 9, "reason": "grok only"},
    ], frozen=frozen)
    with sr._LOCK:
        sr._STATE["last_publish_slot"] = ""
    pub = sr.publish_agreement(
        {
            "ai_seed_rank_require_agreement": False,
            "ai_seed_rank_publish_mode": "union",
            "ai_seed_rank_per_model_max": 3,
            "ai_seed_rank_max": 5,
            "ai_seed_rank_second_opinion": True,
        },
        slot, frozen,
    )
    syms = set(pub["symbols"])
    assert "AAA" in syms and "CCC" in syms and "BBB" in syms
    assert pub["agy_solo"] == 1
    assert pub["xai_solo"] == 1
    assert pub["both"] == 1
    assert pub["published_n"] == 3
    gx = json.loads((tmp_path / "seed_rank_gx.json").read_text())
    assert gx["watch_facing"] is True
    assert gx["agreement_only"] is False
    by_sym = {r["symbol"]: r for r in gx["rows"]}
    assert by_sym["BBB"]["both"] is True
    assert "gx_agree" in by_sym["BBB"]["criteria"]
    assert by_sym["BBB"]["second_opinion"]["status"] == "agree"
    assert by_sym["AAA"]["primary_source"] == "agy"
    assert "agy" in by_sym["AAA"]["criteria"]
    assert by_sym["CCC"]["primary_source"] == "xai"
    assert "xai" in by_sym["CCC"]["criteria"]
    # Audit boards are not watch-facing.
    agy = json.loads((tmp_path / "seed_rank_agy.json").read_text())
    assert agy["watch_facing"] is False


def test_union_respects_ai_seed_rank_max():
    a = [
        {"symbol": "A1", "score": 9, "reason": "a"},
        {"symbol": "A2", "score": 8, "reason": "a"},
        {"symbol": "A3", "score": 7, "reason": "a"},
    ]
    x = [
        {"symbol": "X1", "score": 9.5, "reason": "x"},
        {"symbol": "X2", "score": 8.5, "reason": "x"},
        {"symbol": "X3", "score": 6, "reason": "x"},
    ]
    out = sr.union_suggestions(
        a, x, per_model_max=3, max_n=2, second_opinion=False,
    )
    assert len(out) == 2
    # Highest scores first (X1 9.5, A1 9).
    assert out[0]["symbol"] == "X1"
    assert out[1]["symbol"] == "A1"


def test_union_both_gets_boost_over_equal_solo():
    a = [
        {"symbol": "BOTH", "score": 8, "reason": "a"},
        {"symbol": "SOLO", "score": 8.2, "reason": "a only"},
    ]
    x = [
        {"symbol": "BOTH", "score": 8, "reason": "x"},
    ]
    out = sr.union_suggestions(
        a, x, per_model_max=3, max_n=5, second_opinion=False,
    )
    assert out[0]["symbol"] == "BOTH"
    assert out[0]["both"] is True
    assert out[0]["source_mark"] == "GX"


def test_second_opinion_pass_when_peer_omits():
    a = [{"symbol": "AAA", "score": 9, "reason": "hot"}]
    x = [{"symbol": "ZZZ", "score": 9, "reason": "other"}]
    out = sr.union_suggestions(
        a, x, per_model_max=3, max_n=5, second_opinion=True,
    )
    aaa = next(r for r in out if r["symbol"] == "AAA")
    assert aaa["second_opinion"]["from"] == "xai"
    assert aaa["second_opinion"]["status"] == "pass"
    assert "X:pass" in aaa["reason"] or "pass" in aaa["reason"]


def test_second_opinion_caution_outside_top_n():
    a = [
        {"symbol": "TOP", "score": 9, "reason": "a1"},
        {"symbol": "MID", "score": 8, "reason": "a2"},
        {"symbol": "LOW", "score": 7, "reason": "a3"},
        {"symbol": "DEEP", "score": 6, "reason": "thin float"},
    ]
    x = [
        {"symbol": "DEEP", "score": 9, "reason": "x likes deep"},
    ]
    # per_model_max=1 → AGY only contributes TOP; DEEP is AGY-outside-top.
    out = sr.union_suggestions(
        a, x, per_model_max=1, max_n=5, second_opinion=True,
    )
    deep = next(r for r in out if r["symbol"] == "DEEP")
    assert deep["primary_source"] == "xai"
    assert deep["second_opinion"]["from"] == "agy"
    assert deep["second_opinion"]["status"] == "caution"


def test_publish_mode_helper_legacy_require_wins():
    assert sr.publish_mode({"ai_seed_rank_require_agreement": True,
                            "ai_seed_rank_publish_mode": "union"}) == "agreement"
    assert sr.publish_mode({"ai_seed_rank_require_agreement": False,
                            "ai_seed_rank_publish_mode": "union"}) == "union"
    assert sr.publish_mode({}) == "union"


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


def test_audit_only_seed_rank_board_skipped_by_research(monkeypatch, tmp_path):
    import ai_entry_watch as ew

    monkeypatch.setattr(ew, "ROOT", tmp_path)
    audit = {
        "ts": time.time(),
        "kind": "seed_rank",
        "source": "agy",
        "watch_facing": False,
        "rows": [{"symbol": "RAW", "score": 9, "reason": "audit only"}],
    }
    (tmp_path / "seed_rank_agy.json").write_text(json.dumps(audit))
    rows = ew.research_candidate_rows()
    assert not any(r.get("symbol") == "RAW" for r in rows)


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
