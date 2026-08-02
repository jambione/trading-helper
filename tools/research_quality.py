"""Offline quality proxies for AI stock-research runs.

These are not a substitute for human judgment or live P&L. They score whether
a research response is *usable and structured* for the desk pipeline, and
whether it looks grounded (dates, prices, risks) rather than empty fluff.

Used by ``tools/research_ab.py`` and unit-tested offline on fixtures.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Shared with claude_suggest parse path — keep loose to avoid circular imports.
_DATE_RE = re.compile(
    r"\b(?:20\d{2}[-/]\d{1,2}[-/]\d{1,2}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s*20\d{2}"
    r"|Q[1-4]\s*20\d{2}"
    r"|20\d{2})\b",
    re.I,
)
_PRICE_RE = re.compile(r"\$\s?\d+(?:\.\d+)?|\b\d+\.\d{2}\b")
_PCT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?%")
_RISK_WORDS = re.compile(
    r"\b(risk|bear|downside|invalidat|stop|overvalued|dilut|debt|lawsuit|"
    r"competition|margin|slowdown|recession)\b",
    re.I,
)
_CATALYST_WORDS = re.compile(
    r"\b(earnings|catalyst|FDA|approval|contract|launch|guidance|buyback|"
    r"M&A|acquisition|tariff|rate.?cut|AI|datacenter)\b",
    re.I,
)


def _local_parse_rows(text: str) -> list[dict[str, Any]]:
    """Minimal JSON extract for offline fixtures (no claude_suggest import)."""
    text = text or ""
    # Prefer first JSON object in the string.
    start = text.find("{")
    if start < 0:
        return []
    # Brace match
    depth = 0
    end = -1
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return []
    try:
        payload = json.loads(text[start:end])
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("suggestions") or payload.get("stocks") or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or item.get("ticker") or "").upper().strip()
        if not sym:
            continue
        row = dict(item)
        row["symbol"] = sym
        out.append(row)
    return out


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def score_research_text(
    text: str,
    rows: list[dict[str, Any]] | None = None,
    *,
    max_price: float = 100.0,
) -> dict[str, Any]:
    """Return quality metrics + a 0–100 composite for ranking A/B variants.

    ``rows`` should be the output of ``claude_suggest.parse_model_text`` when
    available so we do not re-parse inconsistently. If omitted, a light local
    parse is attempted so fixtures work without importing claude_suggest.
    """
    text = text or ""
    rows = list(rows) if rows is not None else []
    if not rows and text.strip():
        rows = _local_parse_rows(text)

    n = len(rows)
    json_first = False
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        json_first = True

    # Schema richness on suggestion objects (desk + risk path care about these).
    rich_fields = (
        "reason", "summary", "invalidation", "p30", "p50", "p100", "position_pct",
    )
    field_hits = {f: 0 for f in rich_fields}
    scores: list[float] = []
    symbols: list[str] = []
    for r in rows:
        sym = str(r.get("symbol") or r.get("ticker") or "").upper()
        if sym:
            symbols.append(sym)
        sc = _f(r.get("score") if r.get("score") is not None else r.get("trending_score"))
        if sc is not None:
            scores.append(sc)
        for f in rich_fields:
            v = r.get(f)
            if v is None or v == "":
                continue
            if f in ("p30", "p50", "p100", "position_pct") and _f(v) is None:
                continue
            field_hits[f] += 1

    # Whole-document structure (prompt asks for these at top level).
    has_themes = bool(re.search(r'"themes"\s*:\s*\[', text))
    has_macro = bool(re.search(r'"macro_one_liner"\s*:', text))
    has_portfolio = bool(re.search(r'"portfolio"\s*:', text))

    n_dates = len(_DATE_RE.findall(text))
    n_prices = len(_PRICE_RE.findall(text))
    n_pcts = len(_PCT_RE.findall(text))
    n_risk = len(_RISK_WORDS.findall(text))
    n_catalyst = len(_CATALYST_WORDS.findall(text))

    # Prefer 5–7 ideas (prompt target); soft penalty outside.
    count_score = 0.0
    if 5 <= n <= 7:
        count_score = 20.0
    elif 3 <= n <= 9:
        count_score = 12.0
    elif n >= 1:
        count_score = 6.0

    parse_score = 15.0 if n >= 1 else 0.0
    json_first_score = 10.0 if json_first else 0.0

    # Fraction of rich fields filled across rows (0–20).
    if n:
        richness = sum(field_hits.values()) / (n * len(rich_fields))
        rich_score = 20.0 * min(1.0, richness)
    else:
        rich_score = 0.0

    structure_score = (
        (5.0 if has_themes else 0.0)
        + (5.0 if has_macro else 0.0)
        + (5.0 if has_portfolio else 0.0)
    )  # max 15

    # Grounding proxies (cap each).
    ground_score = min(20.0, (
        min(6.0, n_dates * 1.5)
        + min(5.0, n_prices * 0.4)
        + min(4.0, n_pcts * 0.3)
        + min(3.0, n_risk * 0.5)
        + min(2.0, n_catalyst * 0.4)
    ))

    composite = round(
        parse_score + json_first_score + count_score + rich_score
        + structure_score + ground_score,
        2,
    )  # max ~100

    unique = len(set(symbols))
    return {
        "n_suggestions": n,
        "n_unique_symbols": unique,
        "symbols": symbols,
        "parse_ok": n >= 1,
        "json_first": json_first,
        "mean_score": round(sum(scores) / len(scores), 3) if scores else None,
        "score_min": min(scores) if scores else None,
        "score_max": max(scores) if scores else None,
        "field_fill": field_hits,
        "field_fill_rate": round(
            (sum(field_hits.values()) / (n * len(rich_fields))) if n else 0.0, 3
        ),
        "has_themes": has_themes,
        "has_macro": has_macro,
        "has_portfolio": has_portfolio,
        "n_date_mentions": n_dates,
        "n_price_mentions": n_prices,
        "n_pct_mentions": n_pcts,
        "n_risk_mentions": n_risk,
        "n_catalyst_mentions": n_catalyst,
        "result_chars": len(text),
        "quality_0_100": composite,
        "max_price_filter": max_price,
    }


def efficiency_metrics(
    quality: dict[str, Any],
    usage: dict[str, Any] | None,
    *,
    wall_sec: float | None = None,
) -> dict[str, Any]:
    """Combine quality with cost/tokens into efficiency ratios.

    Ranking criteria (by design): **quality** and **token/cost impact**.
    Wall-clock is recorded for ops only — it is not an optimization target.
    """
    usage = usage or {}
    cost = usage.get("total_cost_usd")
    try:
        cost_f = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        cost_f = None

    in_tok = usage.get("input_tokens") or 0
    out_tok = usage.get("output_tokens") or 0
    cache_c = usage.get("cache_creation_input_tokens") or 0
    cache_r = usage.get("cache_read_input_tokens") or 0
    try:
        # Cache creation is real paid context; cache reads are cheaper but still
        # billable volume. Keep both in the "impact" total for A/B ranking.
        total_tok = int(in_tok) + int(out_tok) + int(cache_c) + int(cache_r)
        fresh_tok = int(in_tok) + int(out_tok) + int(cache_c)
    except (TypeError, ValueError):
        total_tok = None
        fresh_tok = None

    q = float(quality.get("quality_0_100") or 0)
    n = int(quality.get("n_suggestions") or 0)
    turns = usage.get("num_turns")

    out: dict[str, Any] = {
        "wall_sec": wall_sec,  # diagnostic only — not a ranking key
        "total_cost_usd": cost_f,
        "total_tokens_impact": total_tok,
        "fresh_tokens_impact": fresh_tok,
        "num_turns": turns,
        # Primary ranks: excellence per unit spend / token
        "quality_per_usd": None,
        "quality_per_1k_tokens": None,
        "quality_per_1k_fresh_tokens": None,
        "quality_per_turn": None,
        "suggestions_per_usd": None,
        "cost_per_suggestion": None,
        "tokens_per_suggestion": None,
    }
    if cost_f and cost_f > 0:
        out["quality_per_usd"] = round(q / cost_f, 2)
        out["suggestions_per_usd"] = round(n / cost_f, 2) if n else 0.0
        out["cost_per_suggestion"] = round(cost_f / n, 4) if n else None
    if total_tok and total_tok > 0:
        out["quality_per_1k_tokens"] = round(q / (total_tok / 1000.0), 2)
        out["tokens_per_suggestion"] = round(total_tok / n, 1) if n else None
    if fresh_tok and fresh_tok > 0:
        out["quality_per_1k_fresh_tokens"] = round(q / (fresh_tok / 1000.0), 2)
    try:
        if turns is not None and int(turns) > 0:
            out["quality_per_turn"] = round(q / int(turns), 2)
    except (TypeError, ValueError):
        pass
    return out
