#!/usr/bin/env python3
"""Seed-only AI ranker: recommend ≤N names from the live seed union.

Freezes momentum + trending + movers at prompt time, asks Google AGY and/or Grok
to rank from THAT list only. Default publish mode is **union**: take up to
``ai_seed_rank_per_model_max`` from each model, merge by symbol, cap at
``ai_seed_rank_max``, and tag each row with its primary source. When
``ai_seed_rank_second_opinion`` is on, attach a peer ``agree|caution|pass``
derived from the other model's raw ranks (informational; does not block).

Set ``ai_seed_rank_publish_mode="agreement"`` or
``ai_seed_rank_require_agreement=True`` to restore the legacy intersection path
(only names both models list).

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
PER_MODEL_MAX_DEFAULT = 3
BOTH_SCORE_BOOST = 0.5  # slight ranking boost when both models list a name
PROMPT_FILE = ROOT / "ai_seed_rank_prompt.txt"
SEED_RANK_AGY = ROOT / "seed_rank_agy.json"
SEED_RANK_CLAUDE = SEED_RANK_AGY  # legacy alias
SEED_RANK_GROK = ROOT / "seed_rank_grok.json"
SEED_RANK_GX = ROOT / "seed_rank_gx.json"
SEED_RANK_AX = SEED_RANK_GX  # legacy alias

_DESK_SRC = frozenset({
    "momentum", "trending", "mom", "st", "stocktwits", "movers",
})
_CAUTION_KEYWORDS = frozenset({
    "thin", "float", "trap", "fade", "chase", "extended", "illiquid",
    "halt", "dilution", "risk", "avoid", "pass", "crowded", "late",
})

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "fetching_g": False,
    "fetching_x": False,
    "slot_inflight": "",
    "last_publish_slot": "",
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


def publish_mode(cfg: dict | None) -> str:
    """Return ``agreement`` or ``union``.

    Legacy ``ai_seed_rank_require_agreement=True`` forces agreement mode even
    when ``ai_seed_rank_publish_mode`` says union.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if bool(_cfg(cfg, "ai_seed_rank_require_agreement", False)):
        return "agreement"
    mode = str(_cfg(cfg, "ai_seed_rank_publish_mode", "union") or "union").strip().lower()
    return "agreement" if mode == "agreement" else "union"


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
            "both": True,
            "primary_source": "both",
            "source_mark": "GX",
        }))
    scored.sort(key=lambda t: -t[0])
    return [row for _, row in scored[:max(0, int(max_n))]]


def _row_map(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if sym and sym not in out:
            out[sym] = r
    return out


def _reason_has_caution(reason: str) -> bool:
    low = str(reason or "").lower()
    return any(k in low for k in _CAUTION_KEYWORDS)


def derive_second_opinion(
    sym: str,
    *,
    primary: str,
    both: bool,
    a_full: dict[str, dict],
    x_full: dict[str, dict],
    a_top: set[str],
    x_top: set[str],
) -> dict[str, str]:
    """Peer opinion from raw ranks — no extra network call.

    - both listed in top-N → ``agree``
    - peer omitted entirely → ``pass``
    - peer listed outside top-N or reason has risk keywords → ``caution``
    """
    sym = str(sym or "").upper().strip()
    primary_l = str(primary or "").lower()
    if primary_l in ("xai", "grok"):
        peer, peer_full, peer_top = "agy", a_full, a_top
    else:
        # agy / both → peer is Grok
        peer, peer_full, peer_top = "xai", x_full, x_top

    if both or (sym in a_top and sym in x_top):
        note = str(peer_full.get(sym, {}).get("reason") or "")[:120]
        return {"from": peer, "status": "agree", "note": note}

    peer_row = peer_full.get(sym)
    if peer_row is None:
        return {"from": peer, "status": "pass", "note": "omitted by peer"}
    note = str(peer_row.get("reason") or "")[:120]
    if sym not in peer_top or _reason_has_caution(note):
        return {"from": peer, "status": "caution", "note": note or "outside peer top-N"}
    return {"from": peer, "status": "agree", "note": note}


def union_suggestions(
    a_rows: list[dict],
    x_rows: list[dict],
    *,
    per_model_max: int = PER_MODEL_MAX_DEFAULT,
    max_n: int = MAX_SUGGESTIONS,
    second_opinion: bool = True,
) -> list[dict]:
    """Union of per-model top-N, ranked by best score with a both-boost.

    Primary source when both list a name: the model with the higher individual
    score; on tie prefer AGY (first-seen). ``both=True`` and source_mark GX.
    """
    per_n = max(0, int(per_model_max))
    a_top_rows = [r for r in a_rows if isinstance(r, dict) and r.get("symbol")][:per_n]
    x_top_rows = [r for r in x_rows if isinstance(r, dict) and r.get("symbol")][:per_n]
    a_full = _row_map(a_rows)
    x_full = _row_map(x_rows)
    a_top = {str(r.get("symbol") or "").upper() for r in a_top_rows}
    x_top = {str(r.get("symbol") or "").upper() for r in x_top_rows}
    a_map = _row_map(a_top_rows)
    x_map = _row_map(x_top_rows)

    scored: list[tuple[float, dict]] = []
    for sym in set(a_map) | set(x_map):
        a = a_map.get(sym)
        x = x_map.get(sym)
        both = a is not None and x is not None
        if both:
            sa, sx = _score(a), _score(x)
            avg = (sa + sx) / 2.0
            rank_score = avg + BOTH_SCORE_BOOST
            # Both-listed → GX / gx_agree. Tie-break for peer note: higher score.
            mark = "GX"
            primary_out = "both"
            reason_a = str(a.get("reason") or "").strip()
            reason_x = str(x.get("reason") or "").strip()
            if reason_a and reason_x and reason_a != reason_x:
                reason = f"A:{reason_a} | X:{reason_x}"[:80]
            else:
                reason = (reason_a or reason_x or "GX agree")[:80]
            inv = str(a.get("invalidation") or x.get("invalidation") or "")[:120]
            summary = str(a.get("summary") or x.get("summary") or "")[:200]
            pub_score = round(avg, 2)
            agreement = True
        elif a is not None:
            primary_out, mark = "agy", "G"
            reason = str(a.get("reason") or "").strip()[:80]
            inv = str(a.get("invalidation") or "")[:120]
            summary = str(a.get("summary") or "")[:200]
            pub_score = round(_score(a), 2)
            rank_score = float(pub_score)
            agreement = False
        else:
            assert x is not None
            primary_out, mark = "xai", "X"
            reason = str(x.get("reason") or "").strip()[:80]
            inv = str(x.get("invalidation") or "")[:120]
            summary = str(x.get("summary") or "")[:200]
            pub_score = round(_score(x), 2)
            rank_score = float(pub_score)
            agreement = False

        row: dict[str, Any] = {
            "symbol": sym,
            "score": pub_score,
            "reason": reason,
            "invalidation": inv,
            "summary": summary,
            "agreement": agreement,
            "both": both,
            "primary_source": primary_out,
            "source_mark": mark,
        }
        if second_opinion:
            op = derive_second_opinion(
                sym,
                primary=primary_out,
                both=both,
                a_full=a_full,
                x_full=x_full,
                a_top=a_top,
                x_top=x_top,
            )
            row["second_opinion"] = op
            # Surface peer caution/pass in reason when solo (≤80 chars).
            if not both and op.get("status") in ("caution", "pass"):
                tag = "X" if op.get("from") == "xai" else "A"
                note = str(op.get("note") or op.get("status") or "").strip()
                # Avoid duplicating status when note already states it.
                if note and note != op.get("status"):
                    extra = f"{tag}:{op.get('status')} {note}"
                else:
                    extra = f"{tag}:{op.get('status')}"
                merged = f"{reason} | {extra}" if reason else extra
                row["reason"] = merged[:80]
        scored.append((rank_score, row))

    scored.sort(key=lambda t: (-t[0], t[1]["symbol"]))
    return [row for _, row in scored[:max(0, int(max_n))]]


def _criteria_for_row(row: dict, *, default_mark: str = "") -> list[str]:
    mark = str(row.get("source_mark") or default_mark or "").upper()
    if row.get("both") or row.get("agreement") or mark in ("GX", "AX", "BOTH"):
        return ["seed_rank", "gx_agree"]
    primary = str(row.get("primary_source") or "").lower()
    if primary in ("xai", "grok") or mark == "X":
        return ["seed_rank", "xai"]
    if primary in ("agy", "google", "gemini") or mark in ("G", "A"):
        return ["seed_rank", "agy"]
    return ["seed_rank"]


def write_board(
    source: str,
    suggestions: list[dict],
    *,
    frozen: dict,
    slot: str,
    now: float | None = None,
    agreement_only: bool = True,
    watch_facing: bool = True,
    publish_mode_label: str = "",
) -> Path:
    """Write a seed-rank board. ``watch_facing=False`` = audit-only (raw)."""
    t0 = float(now if now is not None else time.time())
    if source in ("ax", "both", "agreement", "gx"):
        src_label = "agy"  # watch tag; per-row source_mark carries GX/G/X
        path = SEED_RANK_AX
        mark = "GX"
    elif source in ("agy", "anthropic", "claude", "a", "google", "gemini"):
        src_label, path, mark = "agy", SEED_RANK_AGY, "G"
    else:
        src_label, path, mark = "xai", SEED_RANK_GROK, "X"
    rows_out = []
    for row in suggestions:
        row_mark = str(row.get("source_mark") or mark)
        reason = str(row.get("reason") or "")
        if not reason.startswith("seed_rank"):
            reason = f"seed_rank: {reason}".strip()[:80]
        else:
            reason = reason[:80]
        out = {
            **row,
            "source": str(row.get("primary_source") or src_label),
            "source_mark": row_mark,
            "agreement": bool(row.get("agreement", row_mark in ("GX", "AX"))),
            "criteria": row.get("criteria") or _criteria_for_row(row, default_mark=row_mark),
            "reason": reason,
        }
        rows_out.append(out)
    payload = {
        "ts": t0,
        "et": datetime.fromtimestamp(t0, ET).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "source": src_label,
        "kind": "seed_rank",
        "slot": slot,
        "seed_ts": frozen.get("ts"),
        "seed_n": frozen.get("n"),
        "agreement_only": bool(agreement_only),
        "watch_facing": bool(watch_facing),
        "publish_mode": publish_mode_label or ("agreement" if agreement_only else "union"),
        "rows": rows_out,
        "suggestions": suggestions,
    }
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def write_raw(source: str, slot: str, suggestions: list[dict], *, frozen: dict,
              now: float | None = None) -> Path:
    """Per-model raw ranks for a slot (audit; publish path decides watch boards)."""
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


def _publish_counts(rows: list[dict]) -> dict[str, int]:
    agy_solo = xai_solo = both = 0
    for r in rows:
        if r.get("both") or str(r.get("source_mark") or "").upper() in ("GX", "AX"):
            both += 1
        elif str(r.get("primary_source") or "").lower() in ("xai", "grok"):
            xai_solo += 1
        else:
            agy_solo += 1
    return {
        "agy_solo": agy_solo,
        "xai_solo": xai_solo,
        "both": both,
        "published_n": len(rows),
    }


def publish_agreement(
    cfg: dict | None,
    slot: str,
    frozen: dict,
    *,
    now: float | None = None,
) -> dict:
    """Publish seed-rank boards once both raw ranks for *slot* exist.

    ``union`` (default): capped union of per-model tops → ``seed_rank_gx.json``
    (watch-facing). Per-model boards get that model's raw list with
    ``watch_facing=False`` for audit.

    ``agreement``: intersection only (legacy); mirrors the same list onto
    gx/agy/grok boards.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    t0 = float(now if now is not None else time.time())
    mode = publish_mode(cfg)
    want_opinion = bool(_cfg(cfg, "ai_seed_rank_second_opinion", True))
    max_n = int(_cfg(cfg, "ai_seed_rank_max", MAX_SUGGESTIONS))
    per_max = int(_cfg(cfg, "ai_seed_rank_per_model_max", PER_MODEL_MAX_DEFAULT))
    a_enabled = bool(_cfg(cfg, "ai_seed_rank_agy", _cfg(cfg, "ai_seed_rank_claude", True)))
    x_enabled = bool(_cfg(cfg, "ai_seed_rank_grok", True))
    a_rows = _load_raw("agy", slot)
    x_rows = _load_raw("xai", slot)
    result: dict[str, Any] = {
        "ts": t0, "kind": "seed_rank_publish", "slot": slot,
        "publish_mode": mode, "seed_n": frozen.get("n"),
        "n": 0, "symbols": [], "error": None,
        "agy_solo": 0, "xai_solo": 0, "both": 0, "published_n": 0,
    }
    # Agreement always needs both raws. Union waits for each enabled side.
    need_a = a_enabled or mode == "agreement"
    need_x = x_enabled or mode == "agreement"
    if (need_a and a_rows is None) or (need_x and x_rows is None):
        result["error"] = "waiting_for_both"
        result["kind"] = "agreement" if mode == "agreement" else "seed_rank_publish"
        return result
    a_rows = a_rows or []
    x_rows = x_rows or []

    if mode == "agreement":
        published = agree_suggestions(a_rows, x_rows, max_n=max_n)
        if want_opinion:
            a_full, x_full = _row_map(a_rows), _row_map(x_rows)
            a_top = set(a_full)
            x_top = set(x_full)
            for row in published:
                row["second_opinion"] = derive_second_opinion(
                    row["symbol"],
                    primary="both",
                    both=True,
                    a_full=a_full,
                    x_full=x_full,
                    a_top=a_top,
                    x_top=x_top,
                )
        write_board(
            "ax", published, frozen=frozen, slot=slot, now=t0,
            agreement_only=True, watch_facing=True, publish_mode_label=mode,
        )
        write_board(
            "agy", published, frozen=frozen, slot=slot, now=t0,
            agreement_only=True, watch_facing=True, publish_mode_label=mode,
        )
        write_board(
            "xai", published, frozen=frozen, slot=slot, now=t0,
            agreement_only=True, watch_facing=True, publish_mode_label=mode,
        )
    else:
        published = union_suggestions(
            a_rows, x_rows,
            per_model_max=per_max,
            max_n=max_n,
            second_opinion=want_opinion,
        )
        # Canonical watch feed.
        write_board(
            "ax", published, frozen=frozen, slot=slot, now=t0,
            agreement_only=False, watch_facing=True, publish_mode_label=mode,
        )
        # Per-model raw snapshots for audit (not watch-facing).
        if a_enabled:
            write_board(
                "agy", a_rows[:max(per_max, max_n)], frozen=frozen, slot=slot, now=t0,
                agreement_only=False, watch_facing=False, publish_mode_label=mode,
            )
        if x_enabled:
            write_board(
                "xai", x_rows[:max(per_max, max_n)], frozen=frozen, slot=slot, now=t0,
                agreement_only=False, watch_facing=False, publish_mode_label=mode,
            )

    counts = _publish_counts(published)
    result.update(counts)
    result["n"] = len(published)
    result["symbols"] = [s["symbol"] for s in published]
    if not published:
        result["error"] = "no_agreement" if mode == "agreement" else "empty_union"

    # Emit seed_rank_publish once per slot (workers may call publish repeatedly).
    should_log = False
    with _LOCK:
        if _STATE.get("last_publish_slot") != slot:
            _STATE["last_publish_slot"] = slot
            should_log = True
    if should_log:
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

    # Parse up to max(per_model_max, max) so union can take per-model tops
    # even when the published board is smaller.
    parse_n = max(
        int(_cfg(cfg, "ai_seed_rank_max", MAX_SUGGESTIONS)),
        int(_cfg(cfg, "ai_seed_rank_per_model_max", PER_MODEL_MAX_DEFAULT)),
    )
    suggestions = parse_rank_response(text, allowed, max_n=parse_n)
    # Raw only — watch boards publish after both sides finish.
    write_raw(source, slot or "manual", suggestions, frozen=frozen, now=t0)
    result["n"] = len(suggestions)
    result["symbols"] = [s["symbol"] for s in suggestions]
    if not suggestions:
        result["error"] = "no_in_list_suggestions"
    append_log(result)
    if slot:
        pub = publish_agreement(cfg, slot, frozen, now=t0)
        result["publish"] = pub
        result["agreement"] = pub  # legacy key
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

    Watch suggestions publish after both raw ranks exist (union or agreement).
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    if not enabled(cfg):
        return []
    t0 = float(now if now is not None else time.time())
    sources = _sources_to_run(cfg)
    mode = publish_mode(cfg)
    if len(sources) < 2 and mode == "agreement":
        # Agreement needs both sides; do not publish solo ranks as suggestions.
        append_log({
            "ts": t0, "kind": "skip",
            "error": "agreement_needs_both_models",
            "sources": [s for s, _ in sources],
        })
        return []
    if not sources:
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
        _STATE["last_publish_slot"] = ""  # allow a fresh seed_rank_publish log
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
                    # Final publish + watch sync (idempotent if run_one already
                    # published when the second side finished).
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
