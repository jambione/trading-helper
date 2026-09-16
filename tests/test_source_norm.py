"""source_norm — alias coverage for desk / research / bb_live frozensets."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import source_norm as sn


# Mirrors ai_entry_watch frozensets (keep in sync if those grow).
_RESEARCH_SOURCES = frozenset({
    "research", "xai", "agy", "google", "gemini", "anthropic", "grok", "claude",
    "a", "g", "x", "ax", "gx", "ai",
})
_DESK_SOURCES = frozenset({
    "momentum", "trending", "mom", "st", "stocktwits", "movers",
})
_BB_LIVE_SOURCES = frozenset({"bb_live", "bro", "bb"})


def test_every_entry_watch_alias_maps():
    for raw in _RESEARCH_SOURCES | _DESK_SOURCES | _BB_LIVE_SOURCES:
        norm = sn.normalize_proposer(raw)
        assert norm != "unknown", raw
        assert norm in sn.ALL_KNOWN_ALIASES or norm.startswith("research"), raw


def test_families():
    assert sn.normalize_proposer("momentum") == "momentum"
    assert sn.normalize_proposer("mom") == "momentum"
    assert sn.normalize_proposer("stocktwits") == "momentum"
    assert sn.normalize_proposer("trending") == "trending"
    assert sn.normalize_proposer("movers") == "movers"
    assert sn.normalize_proposer("agy") == "research:agy"
    assert sn.normalize_proposer("anthropic") == "research:agy"
    assert sn.normalize_proposer("claude") == "research:agy"
    assert sn.normalize_proposer("a") == "research:agy"
    assert sn.normalize_proposer("xai") == "research:xai"
    assert sn.normalize_proposer("grok") == "research:xai"
    assert sn.normalize_proposer("x") == "research:xai"
    assert sn.normalize_proposer("research") == "research"
    assert sn.normalize_proposer("ax") == "research"
    assert sn.normalize_proposer("bb_live") == "bb_live"
    assert sn.normalize_proposer("bro") == "bb_live"


def test_unknown_and_empty():
    assert sn.normalize_proposer(None) == "unknown"
    assert sn.normalize_proposer("") == "unknown"
    assert sn.normalize_proposer("totally_new_seed") == "unknown"


def test_already_normalized():
    assert sn.normalize_proposer("research:agy") == "research:agy"
    assert sn.normalize_proposer("research:xai") == "research:xai"
