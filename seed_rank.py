#!/usr/bin/env python3
"""Seed-only AI ranker: recommend ≤5 names from the live seed union.

Freezes momentum + trending + movers at prompt time, asks Google AGY and/or Grok
to rank from THAT list only. A name becomes a watch suggestion only when
**both** models list it (agreement). Solo picks are logged, not published.

Buying and selling stay with the mechanical book. This module only names.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

DEFAULT_TIMES = [
    "09:25",
    "10:00", "11:00", "12:00", "13:00", "14:00",
    "15:00",
]
MAX_SUGGESTIONS = 5
PROMPT_FILE = ROOT / "ai_seed_rank_prompt.txt"
SEED_RANK_AGY = ROOT / "seed_rank_agy.json"
SEED_RANK_CLAUDE = SEED_RANK_AGY  # legacy alias
SEED_RANK_GROK = ROOT / "seed_rank_grok.json"
SEED_RANK_GX = ROOT / "seed_rank_gx.json"
SEED_RANK_AX = SEED_RANK_GX  # legacy alias

_DESK_SRC = frozenset({
    "momentum", "trending", "mom", "st", "stocktwits", "movers",
})

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "fetching_g": False,
    "fetching_x": False,
    "slot_inflight": "",
}


def _report_dir() -> Path:
    from ai_paths import resolve_report_dir
    return resolve_report_dir()


def log_path() -> Path:
    return _report_dir() / "seed_rank.jsonl"


def schedule_state_path() -> Path:
    """One shared schedule for both models so they rank the same slot."""
    return _report_dir() / "schedule_state_seed_rank.json"


def raw_path(source: str, slot: str) -> Path:
    tag = "g" if source in ("agy", "anthropic", "claude", "a", "google", "gemini") else "x"
    safe = str(slot).replace(":", "").replace("T", "_")
    return _report_dir() / "seed_rank_raw" / f"{safe}_{tag}.json"


def board_path(source: str) -> Path:
    if source in ("ax", "both", "agreement"):
        return SEED_RANK_AX
    if source in ("agy", "anthropic", "claude", "a", "google", "gemini"):
        return SEED_RANK_AGY
    return SEED_RANK_GROK


def _cfg(cfg: dict | None, key: str, default):
    cfg = cfg if isinstance(cfg, dict) else {}
    val = cfg.get(key)
    return default if val is None else val


def enabled(cfg: dict | None) -> bool:
    return bool(_cfg(cfg, "ai_seed_rank_enabled", False))


def times_hm(cfg: dict | None) -> list[tuple[int, int]]:
    from ai_suggest import parse_research_times
    raw = _cfg(cfg, "ai_seed_rank_times", DEFAULT_TIMES)
    times = parse_research_times(raw)
    return times or parse_research_times(DEFAULT_TIMES)


def freeze_seed_union(cfg: dict | None = None, *, now: float | None = None) -> dict:
    """Point-in-time union of non-AI desk seeds (momentum/trending/movers).

    Drops research and bb_live. Optional mechanical stage-1 filter when
    ``ai_seed_rank_require_setup`` is true — fail closed on missing legs.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    t0 = float(now if now is not None else time.time())
    import ai_entry_watch as ew

    rows_in = ew.desk_candidate_rows(cfg)
    out_rows: list[dict] = []
    seen: set[str] = set()
    require_setup = bool(_cfg(cfg, "ai_seed_rank_require_setup", False))
    max_shares = float(_cfg(cfg, "ai_seed_rank_max_shares_m", 30.0))

    for r in rows_in:
        if not isinstance(r, dict):
            continue
        src = str(r.get("source") or "").lower().strip()
        if src not in _DESK_SRC:
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym or sym in seen:
            continue
        if require_setup and not _setup_ok(sym, r, cfg, max_shares):
            continue
        seen.add(sym)
        out_rows.append({
            "symbol": sym,
            "source": src,
            "price": r.get("price"),
            "pct_change": r.get("pct_change"),
            "rvol": r.get("rvol"),
            "score": r.get("score") if r.get("score") is not None
            else r.get("trending_score"),
            "reason": str(r.get("reason") or r.get("look_reason") or "")[:80],
            "dollar_volume": r.get("dollar_volume"),
        })

    return {
        "ts": t0,
        "et": datetime.fromtimestamp(t0, ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "n": len(out_rows),
        "require_setup": require_setup,
        "rows": out_rows,
    }


def _setup_ok(sym: str, row: dict, cfg: dict, max_shares: float) -> bool:
    import setup_rules as SR
    shares = None
    news_n = None
    news_mins = None
    try:
        import float_feed
        shares = float_feed.shares_out(sym)
    except Exception:
        shares = None
    try:
        news_path = _report_dir() / "news_cache.json"
        if news_path.exists():
            news = json.loads(news_path.read_text(encoding="utf-8"))
            items = news.get(sym) or []
            now = time.time()
            recent = [
                n for n in items
                if isinstance(n, dict) and n.get("ts") is not None
                and now - 24 * 3600 <= float(n["ts"]) < now
            ]
            news_n = len(recent) if recent else None
            if recent:
                news_mins = (now - max(float(n["ts"]) for n in recent)) / 60.0
    except Exception:
        pass
    return bool(SR.evaluate(
        pct_change=row.get("pct_change"),
        rvol=row.get("rvol"),
        price=row.get("price"),
        shares_out_m=shares,
        news_n_24h=news_n,
        news_mins_since=news_mins,
        max_shares_out_m=max_shares,
    ).get("ok"))


def build_prompt(frozen: dict, *, max_n: int = MAX_SUGGESTIONS) -> str:
    """Assemble the seed-rank prompt with the frozen list embedded."""
    template = PROMPT_FILE.read_text(encoding="utf-8") if PROMPT_FILE.exists() else (
        "Rank up to {max_n} scalp candidates from SEED_LIST only. "
        "JSON first. No buy/sell orders.\n\nSEED_LIST:\n{seed_json}\n"
    )
    seed_json = json.dumps(
        {"as_of": frozen.get("et"), "seeds": frozen.get("rows") or []},
        indent=2, default=str,
    )
    return (
        template.replace("{max_n}", str(int(max_n)))
        .replace("{seed_json}", seed_json)
        .replace("{seed_n}", str(int(frozen.get("n") or 0)))
    )


def parse_rank_response(text: str, allowed: set[str], *, max_n: int = MAX_SUGGESTIONS) -> list[dict]:
    """Parse model JSON; keep only symbols in *allowed*, max *max_n*."""
    allowed_u = {str(s).upper() for s in allowed}
    rows = _extract_suggestions(text)
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        sym = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
        if not sym or sym not in allowed_u or sym in seen:
            continue
        seen.add(sym)
        out.append({
            "symbol": sym,
            "score": _score(r),
            "reason": str(r.get("reason") or r.get("summary") or "seed_rank")[:80],
            "invalidation": str(r.get("invalidation") or "")[:120],
            "summary": str(r.get("summary") or "")[:200],
        })
        if len(out) >= max_n:
            break
    return out


def _extract_suggestions(text: str) -> list[dict]:
    text = text or ""
    start = text.find("{")
    if start < 0:
        return []
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
    raw = payload.get("suggestions") or payload.get("rows") or payload.get("ranks") or []
    return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def _score(r: dict) -> float:
    for k in ("score", "trending_score", "rank"):
        try:
            if r.get(k) is not None:
                return float(r[k])
        except (TypeError, ValueError):
            continue
    return 5.0


def agree_suggestions(
    a_rows: list[dict],
    x_rows: list[dict],
    *,
    max_n: int = MAX_SUGGESTIONS,
) -> list[dict]:
    """Intersection of both ranks, ordered by average score (desc), ≤ max_n."""
    a_map = {
        str(r.get("symbol") or "").upper(): r
        for r in a_rows if isinstance(r, dict) and r.get("symbol")
    }
    x_map = {
        str(r.get("symbol") or "").upper(): r
        for r in x_rows if isinstance(r, dict) and r.get("symbol")
    }
    shared = set(a_map) & set(x_map)
    scored: list[tuple[float, dict]] = []
    for sym in shared:
        a, x = a_map[sym], x_map[sym]
        avg = (_score(a) + _score(x)) / 2.0
        reason_a = str(a.get("reason") or "").strip()
        reason_x = str(x.get("reason") or "").strip()
        if reason_a and reason_x and reason_a != reason_x:
            reason = f"A:{reason_a} | X:{reason_x}"[:80]
        else:
            reason = (reason_a or reason_x or "GX agree")[:80]
        inv = str(a.get("invalidation") or x.get("invalidation") or "")[:120]
        summary = str(a.get("summary") or x.get("summary") or "")[:200]
        scored.append((avg, {
            "symbol": sym,
            "score": round(avg, 2),
            "reason": reason,
            "invalidation": inv,
            "summary": summary,
            "agreement": True,
            "source_mark": "GX",
        }))
    scored.sort(key=lambda t: -t[0])
    return [row for _, row in scored[:max(0, int(max_n))]]


def write_board(
    source: str,
    suggestions: list[dict],
    *,
    frozen: dict,
    slot: str,
    now: float | None = None,
    agreement_only: bool = True,
) -> Path:
    """Write a watch-facing board. Agreement boards carry source_mark GX."""
    t0 = float(now if now is not None else time.time())
    if source in ("ax", "both", "agreement"):
        src_label = "agy"  # watch tag; mark GX on each row
        path = SEED_RANK_AX
        mark = "GX"
    elif source in ("agy", "anthropic", "claude", "a", "google", "gemini"):
        src_label, path, mark = "agy", SEED_RANK_AGY, "G"
    else:
        src_label, path, mark = "xai", SEED_RANK_GROK, "X"
    payload = {
        "ts": t0,
        "et": datetime.fromtimestamp(t0, ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "source": src_label,
        "kind": "seed_rank",
        "slot": slot,
        "seed_ts": frozen.get("ts"),
        "seed_n": frozen.get("n"),
        "agreement_only": bool(agreement_only),
        "rows": [
            {
                **row,
                "source": src_label,
                "source_mark": row.get("source_mark") or mark,
                "agreement": bool(row.get("agreement", mark == "GX")),
                "criteria": ["seed_rank", "gx_agree"] if mark == "GX" else ["seed_rank"],
                "reason": (
                    str(row.get("reason") or "")
                    if str(row.get("reason") or "").startswith("seed_rank")
                    else f"seed_rank: {row.get('reason') or ''}"
                ).strip()[:80],
            }
            for row in suggestions
        ],
        "suggestions": suggestions,
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def write_raw(source: str, slot: str, suggestions: list[dict], *, frozen: dict,
              now: float | None = None) -> Path:
    """Per-model raw ranks for a slot (not watch-facing until agreement)."""
    t0 = float(now if now is not None else time.time())
    path = raw_path(source, slot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "ts": t0,
        "source": source,
        "slot": slot,
        "seed_ts": frozen.get("ts"),
        "seed_n": frozen.get("n"),
        "suggestions": suggestions,
    }, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def _load_raw(source: str, slot: str) -> list[dict] | None:
    path = raw_path(source, slot)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if str(raw.get("slot") or "") != str(slot):
        return None
    rows = raw.get("suggestions") or []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def publish_agreement(
    cfg: dict | None,
    slot: str,
    frozen: dict,
    *,
    now: float | None = None,
) -> dict:
    """If both raw ranks for *slot* exist, publish intersection to watch boards.

    Solo picks are never written to the watch-facing boards when agreement is
    required (default). Empty intersection clears the GX board.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    t0 = float(now if now is not None else time.time())
    require = bool(_cfg(cfg, "ai_seed_rank_require_agreement", True))
    a_rows = _load_raw("agy", slot)
    x_rows = _load_raw("xai", slot)
    result: dict[str, Any] = {
        "ts": t0, "kind": "agreement", "slot": slot,
        "seed_n": frozen.get("n"), "n": 0, "symbols": [], "error": None,
    }
    if a_rows is None or x_rows is None:
        result["error"] = "waiting_for_both"
        return result

    if require:
        agreed = agree_suggestions(
            a_rows, x_rows,
            max_n=int(_cfg(cfg, "ai_seed_rank_max", MAX_SUGGESTIONS)),
        )
    else:
        # Fallback: publish each side's full list (legacy / measure off).
        agreed = a_rows[: int(_cfg(cfg, "ai_seed_rank_max", MAX_SUGGESTIONS))]

    # Watch boards: only agreed names (or empty). Mirror onto A/X files so the
    # existing research_candidate_rows path picks them up without a new source.
    write_board("ax", agreed, frozen=frozen, slot=slot, now=t0, agreement_only=require)
    write_board("agy", agreed, frozen=frozen, slot=slot, now=t0, agreement_only=require)
    write_board("xai", agreed, frozen=frozen, slot=slot, now=t0, agreement_only=require)
    result["n"] = len(agreed)
    result["symbols"] = [s["symbol"] for s in agreed]
    if not agreed:
        result["error"] = "no_agreement"
    append_log({
        **result,
        "a_symbols": [str(r.get("symbol") or "").upper() for r in a_rows],
        "x_symbols": [str(r.get("symbol") or "").upper() for r in x_rows],
    })
    return result


def append_log(row: dict) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def _load_last_slot() -> str:
    path = schedule_state_path()
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("last_slot") or "")
    except Exception:
        return ""


def _save_last_slot(slot: str) -> None:
    path = schedule_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_slot": slot}, indent=2) + "\n", encoding="utf-8")


def due(cfg: dict | None, now: float | None = None) -> str | None:
    from ai_suggest import due_slot
    t0 = float(now if now is not None else time.time())
    return due_slot(
        t0,
        times=times_hm(cfg),
        weekdays_only=bool(_cfg(cfg, "ai_seed_rank_weekdays_only", True)),
        catchup_min=int(_cfg(cfg, "ai_seed_rank_catchup_min", 45)),
        last_slot=_load_last_slot(),
    )

def run_one(
    cfg: dict | None,
    source: str,
    *,
    now: float | None = None,
    slot: str = "",
    frozen: dict | None = None,
) -> dict:
    """Synchronous one-shot rank for *source*. Never places an order."""
    import ai_suggest as sug

    cfg = cfg if isinstance(cfg, dict) else {}
    t0 = float(now if now is not None else time.time())
    frozen = frozen if frozen is not None else freeze_seed_union(cfg, now=t0)
    allowed = {
        str(r.get("symbol") or "").upper()
        for r in (frozen.get("rows") or [])
        if isinstance(r, dict)
    }
    result: dict[str, Any] = {
        "ts": t0,
        "source": source,
        "slot": slot,
        "seed_n": frozen.get("n"),
        "n": 0,
        "symbols": [],
        "error": None,
    }
    if not allowed:
        result["error"] = "empty_seed_union"
        append_log(result)
        return result

    prompt = build_prompt(frozen, max_n=int(_cfg(cfg, "ai_seed_rank_max", MAX_SUGGESTIONS)))
    if source in ("agy", "anthropic", "claude", "a", "google", "gemini"):
        # Config keys still named claude_* historically; this slot is Google AGY.
        backend = str(_cfg(cfg, "agy_backend", _cfg(cfg, "claude_backend", "agy")))
        model = str(_cfg(cfg, "agy_model", _cfg(cfg, "claude_model", "gemini-3-pro-high")))
        cli_bin = str(_cfg(cfg, "agy_cli_bin", _cfg(cfg, "claude_cli_bin", "agy")))
        timeout = float(_cfg(cfg, "agy_request_timeout", _cfg(cfg, "claude_request_timeout", 600.0)))
        live_search = bool(_cfg(cfg, "agy_live_search", _cfg(cfg, "claude_live_search", True)))
        max_turns = int(_cfg(cfg, "agy_max_turns", _cfg(cfg, "claude_max_turns", 6)))
        effort = str(_cfg(cfg, "agy_effort", _cfg(cfg, "claude_effort", "high")))
        search_tools = str(_cfg(cfg, "agy_search_tools",
                                _cfg(cfg, "claude_search_tools", "web_x")))
    else:
        backend = str(_cfg(cfg, "grok_backend", "cli"))
        model = str(_cfg(cfg, "grok_model", "grok-4.5"))
        cli_bin = str(_cfg(cfg, "grok_cli_bin", "grok"))
        timeout = float(_cfg(cfg, "grok_request_timeout", 600.0))
        live_search = bool(_cfg(cfg, "grok_live_search", True))
        max_turns = int(_cfg(cfg, "grok_max_turns", 4))
        effort = "high"
        search_tools = str(_cfg(cfg, "grok_search_tools", "web_x"))

    try:
        text = sug.call_claude(
            prompt,
            model=model,
            timeout=timeout,
            live_search=live_search,
            trading=False,  # never paper-trade from seed rank
            max_turns=max_turns,
            max_output_tokens=int(_cfg(cfg, "ai_seed_rank_max_output_tokens", 4000)),
            search_tools=search_tools,
            use_prior_context=False,
            use_desk_snapshot=False,  # seeds are embedded; no free-range heat
            backend=backend,
            cli_bin=cli_bin,
            max_price=float(_cfg(cfg, "ai_max_price", 100.0) or 100.0),
            effort=effort,
        )
    except Exception as e:
        result["error"] = f"call_failed:{e}"[:200]
        append_log(result)
        return result

    suggestions = parse_rank_response(
        text, allowed, max_n=int(_cfg(cfg, "ai_seed_rank_max", MAX_SUGGESTIONS)))
    # Raw only — watch boards publish after both sides finish (agreement).
    write_raw(source, slot or "manual", suggestions, frozen=frozen, now=t0)
    result["n"] = len(suggestions)
    result["symbols"] = [s["symbol"] for s in suggestions]
    if not suggestions:
        result["error"] = "no_in_list_suggestions"
    append_log(result)
    if slot:
        pub = publish_agreement(cfg, slot, frozen, now=t0)
        result["agreement"] = pub
    return result


def _sources_to_run(cfg: dict) -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    if bool(_cfg(cfg, "ai_seed_rank_agy", _cfg(cfg, "ai_seed_rank_claude", True))):
        sources.append(("agy", "fetching_g"))
    if bool(_cfg(cfg, "ai_seed_rank_grok", True)):
        sources.append(("xai", "fetching_x"))
    return sources


def tick(cfg: dict | None, now: float | None = None) -> list[str]:
    """If a seed-rank slot is due, kick both models on the same frozen list.

    Watch suggestions appear only after both raw ranks exist and agree.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not enabled(cfg):
        return []
    t0 = float(now if now is not None else time.time())
    sources = _sources_to_run(cfg)
    if len(sources) < 2 and bool(_cfg(cfg, "ai_seed_rank_require_agreement", True)):
        # Agreement needs both sides; do not publish solo ranks as suggestions.
        append_log({
            "ts": t0, "kind": "skip",
            "error": "agreement_needs_both_models",
            "sources": [s for s, _ in sources],
        })
        return []

    with _LOCK:
        if _STATE.get("slot_inflight") or any(_STATE.get(f) for _, f in sources):
            return []

    slot = due(cfg, t0)
    if not slot:
        return []

    frozen = freeze_seed_union(cfg, now=t0)
    if not (frozen.get("rows") or []):
        append_log({
            "ts": t0, "slot": slot, "seed_n": 0, "n": 0, "symbols": [],
            "error": "empty_seed_union",
        })
        _save_last_slot(slot)
        return []

    _save_last_slot(slot)
    with _LOCK:
        _STATE["slot_inflight"] = slot
        for _, flag in sources:
            _STATE[flag] = True

    def _worker(src: str, fl: str) -> None:
        try:
            run_one(cfg, src, now=time.time(), slot=slot, frozen=frozen)
        finally:
            with _LOCK:
                _STATE[fl] = False
                still = any(_STATE.get(f) for _, f in sources)
                if not still:
                    _STATE["slot_inflight"] = ""
                    # Final agreement publish + watch sync (idempotent if
                    # run_one already published when the second side finished).
                    try:
                        publish_agreement(cfg, slot, frozen, now=time.time())
                    except Exception:
                        pass
                    try:
                        import ai_entry_watch as ew
                        ew.sync_watch_from_source_panels(
                            cfg=cfg, now=time.time())
                    except Exception:
                        pass

    started: list[str] = []
    for source, flag in sources:
        threading.Thread(
            target=_worker, args=(source, flag),
            name=f"seed-rank-{source}", daemon=True,
        ).start()
        started.append(source)
    return started
