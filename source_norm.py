"""Normalize raw proposal/source labels to stable families.

Single table for proposal_ledger + source_scorecard. Call sites store both
the raw ``proposer`` and ``proposer_norm`` — tools never guess.

Families:
  momentum | trending | movers | research:<model> | research | bb_live | unknown
"""
from __future__ import annotations

from typing import Optional

# Desk heat aliases (ai_entry_watch._DESK_SOURCES + common shortenings).
_MOMENTUM = frozenset({"momentum", "mom", "st", "stocktwits"})
_TRENDING = frozenset({"trending"})
_MOVERS = frozenset({"movers"})

# Research model families. Bare letters mirror _RESEARCH_SOURCES.
_RESEARCH_AGY = frozenset({
    "agy", "anthropic", "claude", "google", "gemini", "a", "g", "ai",
})
_RESEARCH_XAI = frozenset({"xai", "grok", "x"})
# Model unknown — still research, not desk heat.
_RESEARCH_BARE = frozenset({"research", "ax", "gx"})

_BB_LIVE = frozenset({"bb_live", "bro", "bb"})

# Union of every alias we claim to cover (for tests).
ALL_KNOWN_ALIASES: frozenset[str] = (
    _MOMENTUM | _TRENDING | _MOVERS | _RESEARCH_AGY | _RESEARCH_XAI
    | _RESEARCH_BARE | _BB_LIVE
)


def normalize_proposer(raw: Optional[str]) -> str:
    """Raw label → family. Empty / unknown → ``unknown``."""
    s = str(raw or "").strip().lower()
    if not s:
        return "unknown"
    if s in _MOMENTUM:
        return "momentum"
    if s in _TRENDING:
        return "trending"
    if s in _MOVERS:
        return "movers"
    if s in _RESEARCH_AGY:
        return "research:agy"
    if s in _RESEARCH_XAI:
        return "research:xai"
    if s in _RESEARCH_BARE:
        return "research"
    if s.startswith("research:"):
        # Already normalized or model-tagged; keep as-is when well-formed.
        tail = s.split(":", 1)[1].strip()
        if tail in _RESEARCH_AGY or tail == "agy":
            return "research:agy"
        if tail in _RESEARCH_XAI or tail == "xai":
            return "research:xai"
        if tail:
            return f"research:{tail}"
        return "research"
    if s in _BB_LIVE:
        return "bb_live"
    return "unknown"


def is_research_family(norm: Optional[str]) -> bool:
    n = str(norm or "")
    return n == "research" or n.startswith("research:")


__all__ = [
    "ALL_KNOWN_ALIASES",
    "normalize_proposer",
    "is_research_family",
]
