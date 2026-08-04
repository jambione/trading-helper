"""Agreement-based session watch queue for AI paper entries.

Research (slow clock) upserts symbols that clear the agreement gate.
The poller arms/buys from stored structure; this module owns load/save,
upsert/invalidation, zone/spread arming, rate-limited structure refresh,
and poll_once paper entry placement.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_paths import resolve_report_dir  # noqa: E402

REPORT_DIR = resolve_report_dir()
WATCH_STATE_PATH = REPORT_DIR / "entry_watch_state.json"

_EMPTY_RECORD_DEFAULTS: dict[str, Any] = {
    "structure": None,
    "structure_ts": 0.0,
    "last_poll_ts": 0.0,
    "last_ask": None,
}

_ARMABLE_STATUSES = frozenset({"watching", "armed"})
_TERMINAL_STATUSES = frozenset({
    "filled", "submitted", "invalidated", "expired",
})

# Ring of structure LLM call timestamps (module-level budget window).
_structure_call_ts: list[float] = []
_STRUCTURE_BUDGET_WINDOW_SEC = 3600.0


def load_watch() -> dict[str, dict]:
    """Load symbol -> watch record; empty dict if missing/corrupt."""
    path = WATCH_STATE_PATH
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, val in raw.items():
        if not isinstance(val, dict):
            continue
        sym = str(key or val.get("symbol") or "").upper().strip()
        if not sym:
            continue
        rec = dict(val)
        rec["symbol"] = sym
        out[sym] = rec
    return out


def save_watch(state: dict) -> None:
    """Atomic write so a crash mid-write does not corrupt the watch file."""
    path = WATCH_STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state if isinstance(state, dict) else {}
    tmp = path.with_suffix(path.suffix + ".tmp")
    if path.suffix == ".json":
        tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def public_snapshot(state: dict | None = None) -> list[dict]:
    """Operator-facing watch queue rows for positions JSON.

    Each item: symbol, status, wait_kind, entry_low, entry_high, last_ask,
    score, agreement, reason, source, ready. Open queue only (watching/armed);
    terminal statuses are omitted. Ready = armed, or watching with ask in zone.
    Sorted: ready first, then score desc, then symbol.
    """
    if state is None:
        state = load_watch()
    if not isinstance(state, dict):
        return []
    # Exact zone (pad=0) for UI "ready" — matches default ai_entry_zone_pad_pct.
    # Avoid load_config here (snapshot is hot-path and must stay import-light).
    pad_pct = 0.0
    rows: list[dict] = []
    for key, rec in state.items():
        if not isinstance(rec, dict):
            continue
        sym = str(rec.get("symbol") or key or "").upper().strip()
        if not sym:
            continue
        status = str(rec.get("status") or "").lower().strip() or "watching"
        if status in _TERMINAL_STATUSES:
            continue
        if status and status not in _ARMABLE_STATUSES:
            continue
        structure = rec.get("structure")
        if not isinstance(structure, dict):
            structure = {}
        wait_kind = structure.get("wait_kind")
        if wait_kind is not None:
            wait_kind = str(wait_kind).lower().strip() or None
        entry_low = structure.get("entry_low")
        entry_high = structure.get("entry_high")
        # Prefer nested structure levels; fall back to top-level if present.
        if entry_low is None:
            entry_low = rec.get("entry_low")
        if entry_high is None:
            entry_high = rec.get("entry_high")
        try:
            entry_low_f = float(entry_low) if entry_low is not None else None
        except (TypeError, ValueError):
            entry_low_f = None
        try:
            entry_high_f = float(entry_high) if entry_high is not None else None
        except (TypeError, ValueError):
            entry_high_f = None
        last_ask = rec.get("last_ask")
        try:
            last_ask_f = float(last_ask) if last_ask is not None else None
        except (TypeError, ValueError):
            last_ask_f = None
        score = rec.get("score")
        try:
            score_f = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_f = None
        in_zone = False
        if (
            last_ask_f is not None
            and entry_low_f is not None
            and entry_high_f is not None
            and entry_low_f > 0
            and entry_high_f > 0
        ):
            in_zone = ask_in_zone(
                last_ask_f, entry_low_f, entry_high_f, pad_pct)
        ready = status == "armed" or (status == "watching" and in_zone)
        rows.append({
            "symbol": sym,
            "status": status or None,
            "wait_kind": wait_kind,
            "entry_low": entry_low_f,
            "entry_high": entry_high_f,
            "last_ask": last_ask_f,
            "score": score_f,
            "agreement": bool(rec.get("agreement")) if rec.get("agreement") is not None else None,
            "reason": str(rec.get("reason") or "")[:80] or None,
            "source": str(rec.get("source") or "research")[:24] or "research",
            "ready": bool(ready),
            "in_zone": bool(in_zone),
        })
    # Ready first, then higher score, then symbol for stable UI.
    rows.sort(key=lambda r: (
        0 if r.get("ready") else 1,
        -(r.get("score") or 0.0),
        r["symbol"],
    ))
    return rows


def _watch_row_from_record(sym: str, rec: dict, *, pad_pct: float = 0.0) -> dict:
    """Normalize one watch-state record for the book table."""
    structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else {}
    wait_kind = structure.get("wait_kind")
    if wait_kind is not None:
        wait_kind = str(wait_kind).lower().strip() or None
    entry_low = structure.get("entry_low", rec.get("entry_low"))
    entry_high = structure.get("entry_high", rec.get("entry_high"))
    try:
        entry_low_f = float(entry_low) if entry_low is not None else None
    except (TypeError, ValueError):
        entry_low_f = None
    try:
        entry_high_f = float(entry_high) if entry_high is not None else None
    except (TypeError, ValueError):
        entry_high_f = None
    last_ask = rec.get("last_ask")
    try:
        last_ask_f = float(last_ask) if last_ask is not None else None
    except (TypeError, ValueError):
        last_ask_f = None
    try:
        score_f = float(rec["score"]) if rec.get("score") is not None else None
    except (TypeError, ValueError, KeyError):
        score_f = None
    status = str(rec.get("status") or "watching").lower().strip() or "watching"
    in_zone = False
    if (
        last_ask_f is not None
        and entry_low_f is not None
        and entry_high_f is not None
        and entry_low_f > 0
        and entry_high_f > 0
    ):
        in_zone = ask_in_zone(last_ask_f, entry_low_f, entry_high_f, pad_pct)
    ready = status == "armed" or (status == "watching" and in_zone)
    if status == "armed" or ready:
        phase = "ready"
    elif status == "submitted":
        phase = "submitted"
    elif status == "filled":
        phase = "filled"  # upgraded to open if broker position present
    else:
        phase = "watching"
    src = str(rec.get("source") or "research").strip() or "research"
    return {
        "symbol": sym,
        "phase": phase,
        "status": status,
        "ready": bool(ready),
        "in_zone": bool(in_zone),
        "source": src,
        "score": score_f,
        "reason": str(rec.get("reason") or "")[:80] or None,
        "wait_kind": wait_kind,
        "entry_low": entry_low_f,
        "entry_high": entry_high_f,
        "last_ask": last_ask_f,
        "price": last_ask_f,
        "qty": None,
        "avg_entry": None,
        "pl": None,
        "plpc": None,
        "mkt_val": None,
        "is_position": False,
    }


def book_table_rows(
    *,
    positions: dict | None = None,
    watch_rows: list | None = None,
    state: dict | None = None,
) -> list[dict]:
    """Unified AI book rows for the dashboard Watch section.

    Sources include research plus desk heat (``momentum`` / ``trending``)
    when those were seeded into the watch queue. Open broker positions
    appear as ``phase=open`` with live P&L (watch metadata preserved when
    the symbol was on the queue). Sort: open → ready → submitted → watching.
    """
    pos_map = positions if isinstance(positions, dict) else {}
    by_sym: dict[str, dict] = {}

    # Prefer full watch state so submitted/filled stay visible until position
    # shows (or until expired/invalidated). Fall back to public_snapshot list.
    raw_state = state if isinstance(state, dict) else load_watch()
    if isinstance(raw_state, dict) and raw_state:
        for key, rec in raw_state.items():
            if not isinstance(rec, dict):
                continue
            sym = str(rec.get("symbol") or key or "").upper().strip()
            if not sym:
                continue
            status = str(rec.get("status") or "").lower().strip()
            if status in ("invalidated", "expired"):
                continue
            by_sym[sym] = _watch_row_from_record(sym, rec)
    elif isinstance(watch_rows, list):
        for w in watch_rows:
            if not isinstance(w, dict):
                continue
            sym = str(w.get("symbol") or "").upper().strip()
            if not sym:
                continue
            ready = bool(w.get("ready"))
            status = str(w.get("status") or "watching").lower().strip()
            if status == "armed" or ready:
                phase = "ready"
            elif status == "submitted":
                phase = "submitted"
            else:
                phase = "watching"
            by_sym[sym] = {
                "symbol": sym,
                "phase": phase,
                "status": status,
                "ready": ready,
                "in_zone": bool(w.get("in_zone")),
                "source": w.get("source") or "research",
                "score": w.get("score"),
                "reason": w.get("reason"),
                "wait_kind": w.get("wait_kind"),
                "entry_low": w.get("entry_low"),
                "entry_high": w.get("entry_high"),
                "last_ask": w.get("last_ask"),
                "price": w.get("last_ask"),
                "qty": None,
                "avg_entry": None,
                "pl": None,
                "plpc": None,
                "mkt_val": None,
                "is_position": False,
            }

    for sym_raw, p in pos_map.items():
        sym = str(sym_raw or "").upper().strip()
        if not sym or not isinstance(p, dict):
            continue
        prev = by_sym.get(sym) or {
            "symbol": sym,
            "source": "position",
            "score": None,
            "reason": None,
            "wait_kind": None,
            "entry_low": None,
            "entry_high": None,
            "last_ask": None,
        }
        current = p.get("current")
        if current is None:
            current = p.get("current_price")
        by_sym[sym] = {
            **prev,
            "phase": "open",
            "status": "open",
            "ready": False,
            "in_zone": False,
            "is_position": True,
            "price": current if current is not None else prev.get("price"),
            "last_ask": current if current is not None else prev.get("last_ask"),
            "qty": p.get("qty"),
            "avg_entry": p.get("avg_entry"),
            "pl": p.get("pl"),
            "plpc": p.get("plpc"),
            "mkt_val": p.get("mkt_val"),
        }

    rows = list(by_sym.values())

    def _sort_key(r: dict) -> tuple:
        phase = str(r.get("phase") or "")
        phase_rank = {"open": 0, "ready": 1, "submitted": 2, "watching": 3}.get(
            phase, 9)
        try:
            pl = abs(float(r.get("pl") or 0.0))
        except (TypeError, ValueError):
            pl = 0.0
        try:
            sc = float(r.get("score") or 0.0)
        except (TypeError, ValueError):
            sc = 0.0
        # Open: larger |P&L| first; others: higher score first.
        return (phase_rank, -pl if phase == "open" else -sc, r.get("symbol") or "")

    rows.sort(key=_sort_key)
    return rows


def _row_passes_agreement(row: dict, cfg: dict) -> bool:
    """Agreement gate: require both-book agreement unless single-source mode."""
    if not cfg.get("ai_watch_require_agreement", True):
        return True
    if bool(row.get("agreement")):
        return True
    if cfg.get("ai_watch_single_source", False):
        return True
    return False


def _score_from_row(row: dict) -> float:
    for key in ("trending_score", "score", "ai_score"):
        if key in row and row[key] is not None:
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return 0.0


def upsert_from_rows(
    rows: list[dict],
    *,
    cfg: dict,
    now: float,
) -> dict:
    """Merge research rows into watch state; save and return full state.

    Eligible rows become/stay ``watching`` with refreshed reason/score.
    Existing ``structure`` / poll fields are preserved when the symbol remains.
    """
    state = load_watch()
    if not isinstance(rows, list):
        save_watch(state)
        return state

    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _row_passes_agreement(row, cfg):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if not sym:
            continue

        prev = state.get(sym) if isinstance(state.get(sym), dict) else {}
        prev_status = str(prev.get("status") or "").lower().strip()
        # Never clobber in-flight / completed entries back to watching.
        if prev_status in ("submitted", "filled"):
            status = prev_status
        else:
            status = "watching"
        src = _merge_source(
            str(prev.get("source") or ""),
            str(row.get("source") or ""),
        )
        # Desk seeds refresh score/reason only when they own the row or are new;
        # research ownership keeps its thesis text.
        keep_research = (
            str(prev.get("source") or "").lower() in _RESEARCH_SOURCES
            and str(row.get("source") or "").lower() in _DESK_SOURCES
        )
        reason = (
            str(prev.get("reason") or "")
            if keep_research
            else str(row.get("reason") or prev.get("reason") or "")
        )
        score = (
            float(prev.get("score") or 0) if keep_research and prev.get("score") is not None
            else _score_from_row(row)
        )
        if keep_research and prev.get("score") is not None:
            try:
                score = float(prev.get("score"))
            except (TypeError, ValueError):
                score = _score_from_row(row)
        rec: dict[str, Any] = {
            "symbol": sym,
            "status": status,
            "agreement": bool(row.get("agreement") if "agreement" in row else prev.get("agreement")),
            "score": score,
            "reason": reason,
            "source": src or "research",
            "structure": prev.get("structure", _EMPTY_RECORD_DEFAULTS["structure"]),
            "structure_ts": float(
                prev.get("structure_ts", _EMPTY_RECORD_DEFAULTS["structure_ts"]) or 0.0
            ),
            "last_poll_ts": float(
                prev.get("last_poll_ts", _EMPTY_RECORD_DEFAULTS["last_poll_ts"]) or 0.0
            ),
            "last_ask": prev.get("last_ask", _EMPTY_RECORD_DEFAULTS["last_ask"]),
            "updated_ts": float(now),
        }
        state[sym] = rec

    save_watch(state)
    return state


def drop_missing(
    state: dict,
    active_symbols: set[str],
    now: float,
) -> dict:
    """Mark symbols not in *active_symbols* as invalidated; return state.

    Does not delete keys (history for events/debug); updates ``updated_ts``.
    """
    if not isinstance(state, dict):
        return {}
    active = {str(s).upper().strip() for s in (active_symbols or set()) if s}
    for sym, rec in list(state.items()):
        if not isinstance(rec, dict):
            continue
        key = str(sym or rec.get("symbol") or "").upper().strip()
        if not key or key in active:
            continue
        # Already terminal statuses stay as-is except still mark invalidated
        # when missing from research (thesis withdrawn).
        status = str(rec.get("status") or "")
        if status in ("filled", "submitted"):
            continue
        rec = dict(rec)
        rec["symbol"] = key
        rec["status"] = "invalidated"
        rec["updated_ts"] = float(now)
        state[key] = rec
        if key != sym:
            state.pop(sym, None)
    return state


def should_expire_watches_on_close(
    *,
    market_open: bool,
    day_key: str,
    seen_open: bool,
    expired_day: str,
) -> tuple[bool, bool, str]:
    """Edge-detect RTH open → closed for watch expiry.

    Only expires after the market was observed open and then closed, and at
    most once per ET *day_key*. Pre-market closed samples do not latch
    ``expired_day`` and do not trigger expiry.

    Returns ``(should_expire, seen_open_next, expired_day_next)``.
    """
    day = str(day_key or "")
    expired = str(expired_day or "")
    if market_open:
        return False, True, expired
    # Market closed: expire only on open→closed edge, once per day.
    if seen_open and expired != day:
        return True, False, day
    return False, bool(seen_open), expired


def expire_open_watches(now: float) -> dict:
    """Mark open (watching/armed) watches as expired; save and return state.

    Terminal statuses (filled, submitted, invalidated, expired) are left
    unchanged. Used at RTH close when ``ai_watch_expire_at_close`` is set.
    """
    state = load_watch()
    if not isinstance(state, dict):
        return {}
    t0 = float(now)
    for sym, rec in list(state.items()):
        if not isinstance(rec, dict):
            continue
        key = str(sym or rec.get("symbol") or "").upper().strip()
        if not key:
            continue
        status = str(rec.get("status") or "")
        if status in _TERMINAL_STATUSES:
            continue
        # Open queue: watching / armed (and empty default as open).
        if status and status not in _ARMABLE_STATUSES:
            continue
        rec = dict(rec)
        rec["symbol"] = key
        rec["status"] = "expired"
        rec["updated_ts"] = t0
        state[key] = rec
        if key != sym:
            state.pop(sym, None)
    save_watch(state)
    return state


def expire_stale_watches_for_new_day(now: float) -> dict:
    """Expire watching/armed leftover from a prior ET calendar day.

    Uses max(updated_ts, structure_ts) in America/New_York. Records with no
    usable timestamp are treated as stale. Terminal statuses are unchanged.
    Does not latch close-edge state; safe to call every poll_once.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    state = load_watch()
    if not isinstance(state, dict):
        return {}
    t0 = float(now)
    et = ZoneInfo("America/New_York")
    today = datetime.fromtimestamp(t0, tz=et).date()
    changed = False
    for sym, rec in list(state.items()):
        if not isinstance(rec, dict):
            continue
        key = str(sym or rec.get("symbol") or "").upper().strip()
        if not key:
            continue
        status = str(rec.get("status") or "").lower().strip()
        if status in _TERMINAL_STATUSES:
            continue
        if status and status not in _ARMABLE_STATUSES:
            continue
        try:
            updated_ts = float(rec.get("updated_ts") or 0.0)
        except (TypeError, ValueError):
            updated_ts = 0.0
        try:
            structure_ts = float(rec.get("structure_ts") or 0.0)
        except (TypeError, ValueError):
            structure_ts = 0.0
        ts = max(updated_ts, structure_ts)
        if ts > 0:
            rec_day = datetime.fromtimestamp(ts, tz=et).date()
            if rec_day >= today:
                continue
        # Prior day (or no ts) → expire leftover open watch.
        rec = dict(rec)
        rec["symbol"] = key
        rec["status"] = "expired"
        rec["updated_ts"] = t0
        state[key] = rec
        if key != sym:
            state.pop(sym, None)
        changed = True
    if changed:
        save_watch(state)
    return state


def _price_under_cap(px: Any, max_price: Any) -> bool:
    if max_price is None:
        return True
    try:
        cap = float(max_price)
        if cap <= 0:
            return True
        p = float(px)
    except (TypeError, ValueError):
        return True  # unknown price: keep candidate
    return p < cap


_RESEARCH_SOURCES = frozenset({
    "research", "xai", "anthropic", "grok", "claude", "a", "x", "ax", "ai",
})
_DESK_SOURCES = frozenset({"momentum", "trending", "mom", "st", "stocktwits"})


def _merge_source(prev_src: str, new_src: str) -> str:
    """Research beats desk heat; otherwise prefer the non-empty new source."""
    p = str(prev_src or "").strip().lower()
    n = str(new_src or "").strip().lower()
    if not n:
        return prev_src or "research"
    if not p:
        return new_src or "research"
    if p in _RESEARCH_SOURCES and n in _DESK_SOURCES:
        return prev_src  # keep AI thesis ownership
    if n in _RESEARCH_SOURCES:
        return new_src
    return new_src or prev_src


def _momentum_scored_from_signal(max_price: Any) -> list[tuple[float, dict]]:
    """Engine proximity map → scored momentum candidate rows."""
    path = ROOT / "signal_state.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, dict):
        return []
    tickers = raw.get("tickers") or raw.get("active") or {}
    if not isinstance(tickers, dict):
        return []
    scored: list[tuple[float, dict]] = []
    for sym, meta in tickers.items():
        s = str(sym or "").upper().strip()
        if not s or not s[0].isalpha():
            continue
        if not isinstance(meta, dict):
            meta = {}
        if not _price_under_cap(meta.get("price"), max_price):
            continue
        hot = bool(meta.get("is_hot"))
        try:
            prox = float(meta.get("proximity_pct") or 0.0)
        except (TypeError, ValueError):
            prox = 0.0
        score = 6.5 + min(2.0, (1.0 if hot else 0.0) + prox / 50.0)
        reason = "momentum HOT" if hot else "momentum desk"
        scored.append((score, {
            "symbol": s,
            "trending_score": round(score, 2),
            "score": round(score, 2),
            "reason": reason[:40],
            "agreement": True,
            "source": "momentum",
        }))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


def _momentum_from_watchlist(max_price: Any) -> list[tuple[float, dict]]:
    """Dashboard Momentum Stocks file (``transcription/wb_watchlist.json``)."""
    path = ROOT / "transcription" / "wb_watchlist.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    scored: list[tuple[float, dict]] = []
    # Newer entries first (file order is roughly recency after purge).
    for i, item in enumerate(raw):
        if isinstance(item, str):
            s = item.strip().upper()
        elif isinstance(item, dict):
            s = str(item.get("ticker") or item.get("symbol") or "").upper().strip()
        else:
            continue
        if not s or not s[0].isalpha() or len(s) > 5:
            continue
        # Mild recency bias: earlier list index → slightly higher score.
        score = 7.0 + max(0.0, 1.0 - i * 0.05)
        scored.append((score, {
            "symbol": s,
            "trending_score": round(score, 2),
            "score": round(score, 2),
            "reason": "momentum watchlist",
            "agreement": True,
            "source": "momentum",
        }))
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored


def desk_candidate_rows(cfg: dict | None = None) -> list[dict]:
    """Momentum + trending names as tradeable watch candidates.

    These are candidates only — the structure poller still must define zone /
    stop / target before arming a buy. Controlled by:
      ai_watch_seed_momentum (default True)
      ai_watch_seed_trending (default True)

    Momentum universe (union, scored):
      1) signal_engine ``signal_state.json`` actives (proximity / HOT)
      2) dashboard Momentum Stocks ``transcription/wb_watchlist.json``

    Trending universe: ``trending_stocks.json`` (Stocktwits heat).

    Reads wire files directly (no heavy ``ai_suggest`` import) so the poller
    stays lightweight and test-safe.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    rows: list[dict] = []
    seen: set[str] = set()
    max_price = cfg.get("ai_max_price", cfg.get("claude_max_price"))

    if cfg.get("ai_watch_seed_momentum", True):
        try:
            n = int(cfg.get("ai_watch_seed_momentum_n", 12) or 12)
            n = max(1, n)
            # Prefer engine actives; fill remaining slots from desk watchlist.
            scored = _momentum_scored_from_signal(max_price)
            if len(scored) < n:
                have = {r["symbol"] for _, r in scored}
                for sc, r in _momentum_from_watchlist(max_price):
                    if r["symbol"] in have:
                        continue
                    scored.append((sc, r))
            scored.sort(key=lambda t: t[0], reverse=True)
            for _, r in scored[:n]:
                if r["symbol"] in seen:
                    continue
                seen.add(r["symbol"])
                rows.append(r)
        except Exception:
            pass

    if cfg.get("ai_watch_seed_trending", True):
        try:
            path = ROOT / "trending_stocks.json"
            raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            tr_rows = raw.get("rows") or []
            if isinstance(tr_rows, list):
                n = int(cfg.get("ai_watch_seed_trending_n", 8) or 8)
                for r in tr_rows:
                    if not isinstance(r, dict):
                        continue
                    if r.get("is_crypto") is True:
                        continue
                    if r.get("is_equity") is False:
                        continue
                    s = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
                    if not s or not s[0].isalpha() or s in seen:
                        continue
                    if not _price_under_cap(r.get("price"), max_price):
                        continue
                    try:
                        score = float(r.get("trending_score", r.get("score") or 0) or 0)
                    except (TypeError, ValueError):
                        score = 0.0
                    if score > 10:
                        score = 6.0 + min(2.5, score / 20.0)
                    seen.add(s)
                    rows.append({
                        "symbol": s,
                        "trending_score": round(score, 2),
                        "score": round(score, 2),
                        "reason": "trending heat",
                        "agreement": True,
                        "source": "trending",
                    })
                    if len([x for x in rows if x.get("source") == "trending"]) >= max(1, n):
                        break
        except Exception:
            pass

    return rows


def rebuild_watch_from_book(
    rows: list[dict],
    cfg: dict,
    now: float,
) -> dict:
    """Rebuild entry watch from a research/open-bell book.

    Combines ``upsert_from_rows`` (eligible → watching) + ``drop_missing``
    (not in active set → invalidated) + ``save_watch``. Active symbols are
    those that pass the same agreement gate as upsert, plus optional desk
    momentum/trending seeds so day-trade heat is not dropped on rebuild.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    book = list(rows) if isinstance(rows, list) else []
    # Desk seeds after research rows so research wins on symbol collision
    # in upsert (later row overwrites — put seeds first, research last).
    seeds = desk_candidate_rows(cfg)
    merged = list(seeds) + book
    state = upsert_from_rows(merged, cfg=cfg, now=now)
    active: set[str] = set()
    for row in merged:
        if not isinstance(row, dict):
            continue
        if not _row_passes_agreement(row, cfg):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        if sym:
            active.add(sym)
    state = drop_missing(state, active, now)
    save_watch(state)
    return state


def ask_in_zone(
    ask: float,
    entry_low: float,
    entry_high: float,
    pad_pct: float,
) -> bool:
    """True if *ask* is inside ``[entry_low, entry_high]`` expanded by *pad_pct*.

    *pad_pct* is a percent (e.g. ``0.15`` = 0.15%): low is reduced and high
    is raised by that fraction of each bound.
    """
    try:
        a = float(ask)
        lo = float(entry_low)
        hi = float(entry_high)
        pad = max(0.0, float(pad_pct or 0.0))
    except (TypeError, ValueError):
        return False
    if a <= 0 or lo <= 0 or hi <= 0:
        return False
    if hi < lo:
        lo, hi = hi, lo
    frac = pad / 100.0
    low_bound = lo * (1.0 - frac)
    high_bound = hi * (1.0 + frac)
    return low_bound <= a <= high_bound


def spread_ok(
    bid: float | None,
    ask: float,
    max_spread_pct: float,
) -> bool:
    """True if bid/ask spread as % of mid is within *max_spread_pct*.

    When *max_spread_pct* <= 0, spread is not enforced (always OK).
    Missing/invalid bid with enforcement on → not OK.
    """
    try:
        a = float(ask)
        msp = float(max_spread_pct or 0.0)
    except (TypeError, ValueError):
        return False
    if a <= 0:
        return False
    if msp <= 0:
        return True
    if bid is None:
        return False
    try:
        b = float(bid)
    except (TypeError, ValueError):
        return False
    if b <= 0 or a < b:
        return False
    mid = (a + b) / 2.0
    if mid <= 0:
        return False
    spr = 100.0 * (a - b) / mid
    return spr <= msp + 1e-12


def _structure_levels(structure: dict) -> tuple[float, float, float, float, float] | None:
    """Parse entry/stop/target/rr from structure; None if incomplete for zone arm."""
    try:
        entry_low = float(structure.get("entry_low") or 0)
        entry_high = float(structure.get("entry_high") or 0)
        stop = float(structure.get("stop_price") or 0)
        target = float(structure.get("target_1") or 0)
        rr = float(structure.get("reward_risk") or 0)
    except (TypeError, ValueError):
        return None
    if entry_low <= 0 or entry_high <= 0 or stop <= 0 or target <= 0:
        return None
    return entry_low, entry_high, stop, target, rr


def should_arm_buy(
    record: dict,
    *,
    ask: float,
    bid: float | None,
    cfg: dict,
) -> tuple[bool, str]:
    """Whether a watch record may auto-arm a paper buy at *ask*.

    Returns ``(True, "zone")`` when armable, else ``(False, reason)`` where
    reason is one of: ``not_watching``, ``no_structure``, ``hard_no``,
    ``wait_setup``, ``spread``, ``above_zone``, ``below_zone``, ``reward_risk``.
    """
    if not isinstance(record, dict):
        return False, "not_watching"
    status = str(record.get("status") or "").lower().strip()
    if status not in _ARMABLE_STATUSES:
        return False, "not_watching"

    structure = record.get("structure")
    if not isinstance(structure, dict):
        return False, "no_structure"

    decision = str(structure.get("decision") or "").upper().strip()
    wait_kind = structure.get("wait_kind")
    wait_kind_s = (
        str(wait_kind).lower().strip() if wait_kind is not None else ""
    )

    if wait_kind_s == "hard_no":
        return False, "hard_no"
    if wait_kind_s == "wait_setup":
        return False, "wait_setup"

    # Arm only BUY (with levels) or WAIT + wait_for_zone
    is_buy = decision == "BUY"
    is_zone_wait = wait_kind_s == "wait_for_zone" or (
        decision == "WAIT" and wait_kind_s == "wait_for_zone"
    )
    if decision == "WAIT" and wait_kind_s and wait_kind_s != "wait_for_zone":
        return False, "wait_setup"
    if not is_buy and not is_zone_wait:
        # WAIT without explicit wait_for_zone (or other decisions) → no auto-buy
        if decision == "WAIT":
            return False, "wait_setup"
        return False, "no_structure"

    levels = _structure_levels(structure)
    if levels is None:
        return False, "no_structure"
    entry_low, entry_high, _stop, _target, rr = levels

    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        min_rr = float(cfg.get("ai_min_reward_risk", 0) or 0)
    except (TypeError, ValueError):
        min_rr = 0.0
    if min_rr > 0 and rr + 1e-12 < min_rr:
        return False, "reward_risk"

    try:
        max_spread = float(cfg.get("ai_max_spread_pct", 1.0) or 0.0)
    except (TypeError, ValueError):
        max_spread = 1.0
    if not spread_ok(bid, ask, max_spread):
        return False, "spread"

    try:
        pad = float(cfg.get("ai_entry_zone_pad_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        pad = 0.0

    try:
        a = float(ask)
    except (TypeError, ValueError):
        return False, "below_zone"

    if ask_in_zone(a, entry_low, entry_high, pad):
        return True, "zone"

    frac = max(0.0, pad) / 100.0
    high_bound = max(entry_low, entry_high) * (1.0 + frac)
    if a > high_bound:
        return False, "above_zone"
    return False, "below_zone"


def _prune_structure_budget(now: float) -> None:
    cutoff = float(now) - _STRUCTURE_BUDGET_WINDOW_SEC
    while _structure_call_ts and _structure_call_ts[0] < cutoff:
        _structure_call_ts.pop(0)


def structure_calls_remaining(cfg: dict, now: float | None = None) -> int:
    """How many structure LLM calls remain in the rolling 1h window."""
    t = float(now if now is not None else time.time())
    _prune_structure_budget(t)
    try:
        cap = int(cfg.get("ai_max_structure_calls_per_hour", 12) or 0)
    except (TypeError, ValueError):
        cap = 12
    if cap <= 0:
        return 0
    return max(0, cap - len(_structure_call_ts))


def _record_structure_call(now: float) -> None:
    _prune_structure_budget(now)
    _structure_call_ts.append(float(now))


def _structure_stale(record: dict, cfg: dict, now: float) -> bool:
    """True when structure is missing or older than TTL."""
    structure = record.get("structure")
    if not isinstance(structure, dict):
        return True
    try:
        ts = float(record.get("structure_ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0:
        return True
    try:
        ttl = float(cfg.get("ai_structure_ttl_sec", 5400) or 5400)
    except (TypeError, ValueError):
        ttl = 5400.0
    if ttl <= 0:
        return False
    return (float(now) - ts) > ttl


def _decision_for_place(structure: dict) -> dict[str, Any]:
    """Build a place_scaled_entry decision from stored structure levels.

    Zone-wait records store decision=WAIT; placement needs BUY + levels.
    """
    d = dict(structure)
    d["decision"] = "BUY"
    d["wait_kind"] = None
    return d


def ensure_structure(
    record: dict,
    cfg: dict,
    now: float,
) -> dict:
    """Resolve / refresh entry structure via ``ai_positions.evaluate_entry``.

    Mutates *record* in place: sets ``structure``, ``structure_ts``, and may
    set status to ``invalidated`` on ``hard_no``. Returns the (possibly
    empty) event dict from logging, or a skip event if budget/quote fails.
    Callers must enforce the structure call budget before invoking.
    """
    import ai_positions as cp
    import ai_trading as gt

    if not isinstance(record, dict):
        return {"kind": "structure_skip", "reason": "bad_record"}

    sym = str(record.get("symbol") or "").upper().strip()
    if not sym:
        return {"kind": "structure_skip", "reason": "no_symbol"}

    ask = record.get("last_ask")
    if ask is None or float(ask or 0) <= 0:
        try:
            ask = gt._latest_ask(sym)
        except Exception:
            ask = None
    try:
        ask_f = float(ask) if ask is not None else 0.0
    except (TypeError, ValueError):
        ask_f = 0.0
    if ask_f <= 0:
        return {
            "kind": "structure_skip",
            "symbol": sym,
            "reason": "no_ask",
        }

    acct = gt.get_account()
    equity = 0.0
    if isinstance(acct, dict) and acct.get("ok"):
        try:
            equity = float(acct.get("equity") or 0)
        except (TypeError, ValueError):
            equity = 0.0
    if equity <= 0:
        equity = 100_000.0  # paper default if account unavailable

    try:
        risk_pct = float(cfg.get("ai_risk_pct", 1.0) or 1.0)
    except (TypeError, ValueError):
        risk_pct = 1.0

    reason = str(record.get("reason") or "")
    backend = str(cfg.get("ai_entry_backend") or cfg.get("ai_backend") or "cli")
    model = cfg.get("ai_entry_model") or cfg.get("ai_model")
    cli_bin = cfg.get("ai_cli_bin") or cfg.get("cli_bin")

    _record_structure_call(now)
    try:
        decision = cp.evaluate_entry(
            sym,
            ask_f,
            equity,
            reason=reason,
            risk_pct=risk_pct,
            model=str(model) if model else "",
            cli_bin=str(cli_bin) if cli_bin else None,
            backend=backend,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "kind": "structure_fail",
            "symbol": sym,
            "reason": str(e)[:200],
        }

    if decision is None:
        return {
            "kind": "structure_fail",
            "symbol": sym,
            "reason": "evaluate_entry_none",
        }

    try:
        normalized = cp.normalize_entry_decision(decision) or decision
    except Exception:
        normalized = decision
    if not isinstance(normalized, dict):
        return {
            "kind": "structure_fail",
            "symbol": sym,
            "reason": "bad_decision",
        }

    record["structure"] = normalized
    record["structure_ts"] = float(now)
    record["last_ask"] = ask_f

    wait_kind = normalized.get("wait_kind")
    wait_kind_s = (
        str(wait_kind).lower().strip() if wait_kind is not None else ""
    )
    if wait_kind_s == "hard_no":
        record["status"] = "invalidated"

    event: dict[str, Any]
    if bool(cfg.get("ai_persist_entry_decisions", True)):
        try:
            event = cp.log_entry_decision(
                sym, normalized, reason="watch_structure")
        except Exception:
            event = {
                "kind": "entry_decision",
                "symbol": sym,
                "decision": normalized.get("decision"),
                "wait_kind": normalized.get("wait_kind"),
            }
    else:
        event = {
            "kind": "structure_ok",
            "symbol": sym,
            "decision": normalized.get("decision"),
            "wait_kind": normalized.get("wait_kind"),
        }

    dec = str(normalized.get("decision") or "").upper()
    if dec == "BUY":
        event = dict(event)
        event.setdefault("kind", "structure_buy")
    elif wait_kind_s:
        event = dict(event)
        if event.get("kind") in (None, "entry_decision"):
            pass
        event.setdefault("structure_kind", f"structure_{wait_kind_s}")
    return event


def poll_once(*, cfg: dict, now: float | None = None) -> list[dict]:
    """One RTH watch poll: refresh quotes, restructure if needed, arm/buy.

    Paper path only: placements go through ``place_scaled_entry`` and
    ``record_external_buy``. Returns a list of event dicts.
    """
    import ai_positions as cp
    import ai_trading as gt

    events: list[dict] = []
    cfg = cfg if isinstance(cfg, dict) else {}
    t0 = float(now if now is not None else time.time())

    if not cfg.get("ai_watch_enabled", True):
        return [{"kind": "watch_skip", "reason": "disabled"}]

    # Drop leftover open watches from a prior ET day (first RTH poll after roll).
    # Independent of open→closed close-edge expiry in the trader loop.
    try:
        expire_stale_watches_for_new_day(t0)
    except Exception:
        pass

    try:
        market_open = bool(gt.market_is_open())
    except Exception:
        market_open = False
    if not market_open:
        return [{"kind": "watch_skip", "reason": "market_closed"}]

    try:
        ready = bool(gt.is_ready())
    except Exception:
        ready = False
    if not ready:
        return [{"kind": "watch_skip", "reason": "trader_not_ready"}]

    # Continuously seed Momentum + Trending into the watch queue so they are
    # first-class trade sources between research runs. Upsert only (no
    # drop_missing) — structure LLM remains rate-limited below.
    try:
        seeds = desk_candidate_rows(cfg)
        if seeds:
            upsert_from_rows(seeds, cfg=cfg, now=t0)
    except Exception:
        pass

    state = load_watch()
    if not state:
        return events

    try:
        max_price = cfg.get("ai_max_price")
        max_price_f = float(max_price) if max_price is not None else None
    except (TypeError, ValueError):
        max_price_f = None

    try:
        risk_pct = float(cfg.get("ai_risk_pct", 1.0) or 1.0)
    except (TypeError, ValueError):
        risk_pct = 1.0

    equity_cache: float | None = None

    def _equity() -> float:
        nonlocal equity_cache
        if equity_cache is not None:
            return equity_cache
        acct = gt.get_account()
        if isinstance(acct, dict) and acct.get("ok"):
            try:
                equity_cache = float(acct.get("equity") or 0)
            except (TypeError, ValueError):
                equity_cache = 0.0
        else:
            equity_cache = 0.0
        return float(equity_cache or 0.0)

    for sym_key, rec in list(state.items()):
        if not isinstance(rec, dict):
            continue
        sym = str(rec.get("symbol") or sym_key or "").upper().strip()
        if not sym:
            continue
        rec = dict(rec)
        rec["symbol"] = sym
        status = str(rec.get("status") or "").lower().strip()
        if status in _TERMINAL_STATUSES:
            continue

        # Quotes
        try:
            ask = gt._latest_ask(sym)
        except Exception:
            ask = None
        try:
            bid = gt._latest_bid(sym)
        except Exception:
            bid = None
        try:
            ask_f = float(ask) if ask is not None else 0.0
        except (TypeError, ValueError):
            ask_f = 0.0
        if ask_f > 0:
            rec["last_ask"] = ask_f
        rec["last_poll_ts"] = t0

        # hard_no on stored structure → invalidate (do not poll/buy)
        structure = rec.get("structure") if isinstance(rec.get("structure"), dict) else None
        if structure is not None:
            wk = structure.get("wait_kind")
            if wk is not None and str(wk).lower().strip() == "hard_no":
                rec["status"] = "invalidated"
                state[sym] = rec
                try:
                    events.append(cp.log_event(
                        "invalidated", symbol=sym, reason="hard_no"))
                except Exception:
                    events.append({
                        "kind": "invalidated",
                        "symbol": sym,
                        "reason": "hard_no",
                    })
                continue

        # Structure refresh when missing/stale (budgeted)
        if _structure_stale(rec, cfg, t0):
            if structure_calls_remaining(cfg, t0) > 0 and ask_f > 0:
                sev = ensure_structure(rec, cfg, t0)
                if sev:
                    events.append(sev)
                if str(rec.get("status") or "").lower() in _TERMINAL_STATUSES:
                    state[sym] = rec
                    continue
            elif ask_f <= 0:
                try:
                    events.append(cp.log_event(
                        "watch_skip", symbol=sym, reason="no_ask"))
                except Exception:
                    events.append({
                        "kind": "watch_skip",
                        "symbol": sym,
                        "reason": "no_ask",
                    })
                state[sym] = rec
                continue

        if ask_f <= 0:
            state[sym] = rec
            continue

        if max_price_f is not None and ask_f >= max_price_f:
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason="above_max_price",
                    ask=ask_f, max_price=max_price_f))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": "above_max_price",
                })
            state[sym] = rec
            continue

        # Arm / buy
        try:
            bid_f: float | None
            if bid is None:
                bid_f = None
            else:
                bid_f = float(bid)
        except (TypeError, ValueError):
            bid_f = None

        ok_arm, why = should_arm_buy(rec, ask=ask_f, bid=bid_f, cfg=cfg)
        if not ok_arm:
            if why in ("wait_setup", "hard_no", "spread", "above_zone",
                       "below_zone", "reward_risk", "no_structure"):
                try:
                    events.append(cp.log_event(
                        "watch_skip", symbol=sym, reason=why, ask=ask_f))
                except Exception:
                    events.append({
                        "kind": "watch_skip",
                        "symbol": sym,
                        "reason": why,
                    })
            state[sym] = rec
            continue

        # Position / buy-cap gates (fail closed on errors — never place blind)
        try:
            if gt.has_open_position(sym):
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason="already_held"))
                state[sym] = rec
                continue
        except Exception as e:  # noqa: BLE001
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym,
                    reason=f"gate_error:has_open_position:{e}"[:200]))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": "gate_error:has_open_position",
                })
            state[sym] = rec
            continue
        try:
            if not gt.can_open_new_position(sym):
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason="max_positions"))
                state[sym] = rec
                continue
        except Exception as e:  # noqa: BLE001
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym,
                    reason=f"gate_error:can_open_new_position:{e}"[:200]))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": "gate_error:can_open_new_position",
                })
            state[sym] = rec
            continue
        try:
            if gt.buys_left_this_poll() <= 0:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason="buy_cap"))
                state[sym] = rec
                continue
        except Exception as e:  # noqa: BLE001
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym,
                    reason=f"gate_error:buys_left_this_poll:{e}"[:200]))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": "gate_error:buys_left_this_poll",
                })
            state[sym] = rec
            continue

        structure = rec.get("structure")
        if not isinstance(structure, dict):
            state[sym] = rec
            continue

        equity = _equity()
        if equity <= 0:
            try:
                events.append(cp.log_event(
                    "watch_skip", symbol=sym, reason="no_equity"))
            except Exception:
                events.append({
                    "kind": "watch_skip",
                    "symbol": sym,
                    "reason": "no_equity",
                })
            state[sym] = rec
            continue

        place_decision = _decision_for_place(structure)
        rec["status"] = "armed"
        try:
            result = cp.place_scaled_entry(
                sym,
                place_decision,
                equity,
                risk_pct=risk_pct,
                current_ask=ask_f,
            )
        except Exception as e:  # noqa: BLE001
            try:
                events.append(cp.log_event(
                    "entry_fail", symbol=sym, reason=str(e)[:200]))
            except Exception:
                events.append({
                    "kind": "entry_fail",
                    "symbol": sym,
                    "reason": str(e)[:200],
                })
            state[sym] = rec
            continue

        if isinstance(result, dict) and result.get("ok"):
            rec["status"] = "submitted"
            try:
                gt.record_external_buy(sym, {
                    "reason": str(rec.get("reason") or "")[:120],
                    "score": rec.get("score"),
                    "stop_price": result.get("stop_price"),
                    "target_1": result.get("target_1"),
                    "source": "entry_watch",
                })
            except Exception:
                pass
            try:
                events.append(cp.log_event(
                    "entry_ok",
                    symbol=sym,
                    stop_price=result.get("stop_price"),
                    target_1=result.get("target_1"),
                    ask=ask_f,
                ))
            except Exception:
                events.append({
                    "kind": "entry_ok",
                    "symbol": sym,
                    "ask": ask_f,
                })
        else:
            err = ""
            if isinstance(result, dict):
                err = str(result.get("error") or "place_failed")[:200]
            else:
                err = "place_failed"
            try:
                events.append(cp.log_event(
                    "entry_fail", symbol=sym, reason=err))
            except Exception:
                events.append({
                    "kind": "entry_fail",
                    "symbol": sym,
                    "reason": err,
                })
            # Stay armed/watching for retry next poll
            if str(rec.get("status") or "") == "armed":
                rec["status"] = "watching"

        state[sym] = rec

    save_watch(state)
    return events
