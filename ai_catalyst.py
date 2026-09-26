#!/usr/bin/env python3
"""AI catalyst shadow logger — forward-only news scoring, log only.

**Primary slice:** both models agree, direction = up, and both materiality >= 4
(`agree_strong` & up).

**Test:** after >= 40 trading days of logging, the primary slice must beat the
equal-weight liquid-universe baseline (`base_ew`) at the **5-trading-day
horizon (`h_d5`)**:
- by **more than 50 bp net** of an assumed 5 bp round trip;
- with a **date-clustered t >= 2**;
- with the mean net excess **also above 50 bp in each half** of the period,
  split chronologically by scoring day.

If all three hold, the verdict is GO. Otherwise it is NO-GO.

Every other slice (the SPY comparison, the other horizons, single models,
confidence buckets, down calls) is secondary. Secondary slices are reported
but never used for the go/no-go. The bar, the slice and the horizon are fixed
as of 2026-09-26 and must not be changed after data starts arriving.

Nothing here places an order or touches desk trading state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")

RUN_TIMES = ["08:45", "12:15"]
CATCHUP_MIN = {"08:45": 40, "12:15": 60}
MAX_NAMES_PER_RUN = 60
BATCH_SIZE = 5
MAX_CALLS_PER_MODEL_PER_RUN = 16
MODEL_CONCURRENCY = 2
CALL_TIMEOUT_SEC = 240
RUN_DEADLINE_SEC = 40 * 60
GAP_MIN_ABS_PCT = 3.0
GAP_MIN_PRICE = 20.0
GAP_MIN_PREV_DOLLARS = 20e6
GAP_MAX_TRADE_AGE_SEC = 900
GAP_TOP = 25
MAX_HEADLINES_PER_NAME = 8
SUMMARY_MAX_CHARS = 400
PROMPT_VERSION = "cat-v1"

CATALYST_TYPES = frozenset({
    "earnings", "guidance", "analyst", "M&A", "FDA/regulatory", "product",
    "macro/sector", "legal", "management", "other", "none",
})
DIRECTIONS = frozenset({"up", "down", "neutral"})
HORIZONS = frozenset({"intraday", "days", "weeks"})

_PROMPT_HEADER = (
    "You classify news catalysts for US stocks. Use ONLY the information "
    "given below. Do not browse, search or use tools. Do not use any "
    "knowledge of price moves after the timestamps shown. The only price "
    "information allowed is what is given here."
)

_RETRY_SUFFIX = (
    "Your previous reply was not valid JSON matching the schema. "
    "Reply with ONLY the JSON object."
)

# In-process schedule / child handles (ai_trader tick only).
_LAST_SLOT_MEM: str | None = None
_CHILD: subprocess.Popen | None = None
_CHILD_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _report_dir() -> Path:
    from ai_paths import resolve_report_dir
    return resolve_report_dir()


def catalyst_dir() -> Path:
    d = _report_dir() / "ai_catalyst"
    d.mkdir(parents=True, exist_ok=True)
    return d


def day_log_path(day: str) -> Path:
    return catalyst_dir() / f"{day}.jsonl"


def runs_log_path() -> Path:
    return catalyst_dir() / "runs.jsonl"


def last_slot_path() -> Path:
    return catalyst_dir() / "last_slot.txt"


def dryrun_request_path() -> Path:
    return catalyst_dir() / "dryrun.request"


def dryrun_taken_path() -> Path:
    return catalyst_dir() / "dryrun.taken"


def dryrun_out_dir() -> Path:
    d = catalyst_dir() / "dryrun"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_file_path() -> Path:
    p = ROOT / "logs" / "ai_catalyst.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def universe_path() -> Path:
    return ROOT / "config" / "liquid_universe.json"


def _py() -> str:
    v = ROOT / ".venv" / "bin" / "python"
    return str(v if v.exists() else sys.executable)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _cfg(cfg: dict | None, key: str, default: Any) -> Any:
    cfg = cfg if isinstance(cfg, dict) else {}
    val = cfg.get(key)
    return default if val is None else val


def enabled(cfg: dict | None) -> bool:
    return bool(_cfg(cfg, "ai_catalyst_log_enabled", True))


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def prompt_version_sha() -> str:
    return hashlib.sha256(PROMPT_VERSION.encode("utf-8")).hexdigest()


def row_id(day: str, slot: str, symbol: str, model: str) -> str:
    raw = f"{day}|{slot}|{symbol}|{model}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def headlines_sha256(items: list[dict]) -> str:
    blob = json.dumps(
        [{"id": i.get("id"), "ts": i.get("ts"), "h": i.get("headline")} for i in items],
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _et(ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(float(ts if ts is not None else time.time()), ET)


def _fmt_et(ts: float) -> str:
    return _et(ts).strftime("%Y-%m-%d %H:%M:%S %Z")


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
        fh.flush()


def _load_last_slot() -> str:
    global _LAST_SLOT_MEM
    if _LAST_SLOT_MEM is not None:
        return _LAST_SLOT_MEM
    path = last_slot_path()
    try:
        _LAST_SLOT_MEM = path.read_text(encoding="utf-8").strip()
    except OSError:
        _LAST_SLOT_MEM = ""
    return _LAST_SLOT_MEM or ""


def _save_last_slot(slot: str) -> None:
    global _LAST_SLOT_MEM
    _LAST_SLOT_MEM = slot
    path = last_slot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(slot + "\n", encoding="utf-8")


def load_universe(path: Path | None = None) -> dict:
    p = path or universe_path()
    data = json.loads(p.read_text(encoding="utf-8"))
    symbols = [str(s).upper() for s in (data.get("symbols") or [])]
    etfs = [str(s).upper() for s in (data.get("etfs") or [])]
    return {"symbols": symbols, "etfs": etfs, "raw": data}


# ---------------------------------------------------------------------------
# Alpaca credentials (never log)
# ---------------------------------------------------------------------------

def _alpaca_keys() -> tuple[str, str]:
    try:
        import desk_core
        desk_core.load_desk_env(ROOT / "signal_engine.env")
    except Exception:
        pass
    api = (os.getenv("ALPACA_API_KEY") or "").strip()
    sec = (os.getenv("ALPACA_SECRET_KEY") or "").strip()
    if api and sec:
        return api, sec
    try:
        secrets = json.loads((ROOT / "config" / "secrets.json").read_text(encoding="utf-8"))
        return str(secrets.get("api_key") or ""), str(secrets.get("secret_key") or "")
    except Exception:
        return "", ""


def _trading_client():
    api, sec = _alpaca_keys()
    if not api or not sec:
        raise RuntimeError("alpaca credentials missing")
    from alpaca.trading.client import TradingClient
    for paper in (True, False):
        try:
            return TradingClient(api, sec, paper=paper)
        except Exception:
            continue
    return TradingClient(api, sec, paper=True)


def _data_client():
    api, sec = _alpaca_keys()
    if not api or not sec:
        raise RuntimeError("alpaca credentials missing")
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(api, sec)


def _news_client():
    api, sec = _alpaca_keys()
    if not api or not sec:
        raise RuntimeError("alpaca credentials missing")
    from alpaca.data.historical.news import NewsClient
    return NewsClient(api_key=api, secret_key=sec)


# ---------------------------------------------------------------------------
# Calendar / session helpers
# ---------------------------------------------------------------------------

def session_days_around(day: str, *, lookback: int = 10) -> list[str]:
    """Trading session dates ending at *day* (inclusive when it is a session)."""
    tc = _trading_client()
    end = datetime.strptime(day, "%Y-%m-%d").date()
    start = end - timedelta(days=lookback + 14)
    cal = tc.get_calendar(start=start.isoformat(), end=end.isoformat())
    out = []
    for row in cal or []:
        d = getattr(row, "date", None)
        if d is None:
            continue
        if hasattr(d, "isoformat"):
            out.append(d.isoformat())
        else:
            out.append(str(d)[:10])
    return out


def is_trading_day(day: str) -> bool:
    days = session_days_around(day, lookback=5)
    return day in days


def previous_session_close_ts(day: str) -> float:
    """Unix ts of previous session's 16:00 ET."""
    days = session_days_around(day, lookback=14)
    prior = [d for d in days if d < day]
    if not prior:
        # Fallback: Friday before a Monday, or day-1.
        dt = datetime.strptime(day, "%Y-%m-%d")
        back = 3 if dt.weekday() == 0 else 1
        prev = (dt - timedelta(days=back)).strftime("%Y-%m-%d")
    else:
        prev = prior[-1]
    return datetime.strptime(prev, "%Y-%m-%d").replace(
        hour=16, minute=0, second=0, microsecond=0, tzinfo=ET,
    ).timestamp()


# ---------------------------------------------------------------------------
# Scan universe + snapshots (copied pattern; avoid movers_screener import)
# ---------------------------------------------------------------------------

def load_scan_universe_assets() -> list[tuple[str, str]]:
    """Return [(symbol, company_name), ...] for active common US equities."""
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    from ticker_filters import is_common, is_levered_etp

    tc = _trading_client()
    assets = tc.get_all_assets(
        GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
    )
    ok_ex = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
    out: list[tuple[str, str]] = []
    for a in assets or []:
        if not getattr(a, "tradable", False):
            continue
        ex = str(getattr(a, "exchange", "")).split(".")[-1]
        if ex not in ok_ex:
            continue
        sym = str(a.symbol).upper()
        name = str(getattr(a, "name", "") or "")
        if not is_common(sym) or is_levered_etp(sym, name):
            continue
        out.append((sym, name))
    out.sort(key=lambda x: x[0])
    return out


def fetch_snapshots(
    symbols: list[str],
    *,
    chunk: int = 500,
) -> dict[str, dict]:
    """IEX snapshots: last, last_ts, prior_close, prior_volume."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockSnapshotRequest

    cl = _data_client()
    now = time.time()
    out: dict[str, dict] = {}
    syms = [str(s).upper() for s in symbols]
    for i in range(0, len(syms), chunk):
        batch = syms[i:i + chunk]
        try:
            got = cl.get_stock_snapshot(StockSnapshotRequest(
                symbol_or_symbols=batch, feed=DataFeed.IEX,
            ))
        except Exception:
            continue
        for s, v in (got or {}).items():
            try:
                last = float(v.latest_trade.price)
                last_ts = float(v.latest_trade.timestamp.timestamp())
                prior = float(v.previous_daily_bar.close)
                prior_vol = float(v.previous_daily_bar.volume or 0)
            except Exception:
                continue
            out[str(s).upper()] = {
                "last": last,
                "last_ts": last_ts,
                "age_sec": max(0.0, now - last_ts),
                "prior_close": prior,
                "prior_volume": prior_vol,
                "source": "alpaca_iex_snapshot",
            }
        time.sleep(0.2)
    return out


def select_gappers(
    snaps: dict[str, dict],
    *,
    now: float | None = None,
    min_price: float = GAP_MIN_PRICE,
    min_abs_pct: float = GAP_MIN_ABS_PCT,
    max_trade_age: float = GAP_MAX_TRADE_AGE_SEC,
    min_prev_dollars: float = GAP_MIN_PREV_DOLLARS,
    top: int = GAP_TOP,
) -> list[dict]:
    """Filter and rank gappers by |gap|. Pure; used by run and tests."""
    t0 = float(now if now is not None else time.time())
    rows: list[dict] = []
    for sym, s in snaps.items():
        last = float(s.get("last") or 0)
        prior = float(s.get("prior_close") or 0)
        if last < min_price or prior <= 0:
            continue
        age = float(s.get("age_sec") if s.get("age_sec") is not None
                    else t0 - float(s.get("last_ts") or 0))
        if age > max_trade_age:
            continue
        prev_dollars = prior * float(s.get("prior_volume") or 0)
        if prev_dollars < min_prev_dollars:
            continue
        gap = (last - prior) / prior * 100.0
        if abs(gap) < min_abs_pct:
            continue
        rows.append({
            "symbol": str(sym).upper(),
            "last": last,
            "prior_close": prior,
            "gap_pct": gap,
            "abs_gap": abs(gap),
            "age_sec": age,
            "prev_dollars": prev_dollars,
        })
    rows.sort(key=lambda r: (-r["abs_gap"], r["symbol"]))
    return rows[:top]


# ---------------------------------------------------------------------------
# Desk boards (read-only)
# ---------------------------------------------------------------------------

def _board_day_ok(payload: dict, day: str) -> bool:
    for key in ("ts", "updated", "slot", "day", "et"):
        v = payload.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            try:
                return _et(float(v)).strftime("%Y-%m-%d") == day
            except Exception:
                continue
        s = str(v)
        if len(s) >= 10 and s[:10] == day:
            return True
        # "2026-09-26 09:25:00 EDT" etc.
        m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
        if m and m.group(1) == day:
            return True
    return False


def _board_symbols(path: Path, day: str) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict) or not _board_day_ok(data, day):
        return []
    rows = data.get("rows") or data.get("suggestions") or []
    out: list[str] = []
    for r in rows:
        if isinstance(r, dict):
            sym = str(r.get("symbol") or r.get("ticker") or "").upper().strip()
        else:
            sym = str(r).upper().strip()
        if sym:
            out.append(sym)
    return out


def load_seed_boards(day: str) -> dict[str, list[str]]:
    from ai_paths import AGY_SUGGESTIONS_FILE, GROK_SUGGESTIONS_FILE

    agy = AGY_SUGGESTIONS_FILE
    if not agy.is_file():
        agy = ROOT / "claude_suggestions.json"
    return {
        "seed_movers": _board_symbols(ROOT / "movers_stocks.json", day),
        "seed_trending": _board_symbols(ROOT / "trending_stocks.json", day),
        "seed_research_grok": _board_symbols(GROK_SUGGESTIONS_FILE, day),
        "seed_research_agy": _board_symbols(agy, day),
        "seed_rank": _board_symbols(ROOT / "seed_rank_gx.json", day),
    }


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------

def _news_item_from_alpaca(n: Any) -> dict | None:
    try:
        nid = getattr(n, "id", None)
        created = getattr(n, "created_at", None)
        if created is None:
            return None
        ts = created.timestamp() if hasattr(created, "timestamp") else float(created)
        symbols = [str(s).upper() for s in (getattr(n, "symbols", None) or [])]
        return {
            "id": f"alpaca:{nid}",
            "headline": str(getattr(n, "headline", "") or ""),
            "summary": str(getattr(n, "summary", "") or "")[:SUMMARY_MAX_CHARS],
            "url": str(getattr(n, "url", "") or ""),
            "source": str(getattr(n, "source", "") or ""),
            "created_at": ts,
            "ts": ts,
            "symbols": symbols,
        }
    except Exception:
        return None


def fetch_alpaca_news(
    symbols: list[str],
    start_ts: float,
    end_ts: float,
    *,
    limit: int = 50,
    batch_size: int = 50,
    client: Any | None = None,
) -> dict[str, list[dict]]:
    """Multi-symbol Alpaca news with pagination. Keys are upper symbols."""
    from alpaca.data.requests import NewsRequest

    nc = client or _news_client()
    start = datetime.fromtimestamp(start_ts, timezone.utc)
    end = datetime.fromtimestamp(end_ts, timezone.utc)
    want = sorted({str(s).upper() for s in symbols if s})
    by_sym: dict[str, list[dict]] = {s: [] for s in want}
    for i in range(0, len(want), batch_size):
        chunk = want[i:i + batch_size]
        token = None
        pages = 0
        while pages < 40:
            kw: dict[str, Any] = {
                "symbols": ",".join(chunk),  # alpaca-py NewsRequest.symbols is str
                "start": start,
                "end": end,
                "limit": limit,
                "include_content": False,
            }
            if token:
                kw["page_token"] = token
            try:
                resp = nc.get_news(NewsRequest(**kw))
            except Exception as e:  # noqa: BLE001
                # Child stdout is logs/ai_catalyst.log. Type + short message only.
                msg = str(e).replace("\n", " ")[:300]
                print(
                    f"[ai_catalyst] news fetch failed chunk={i // batch_size} "
                    f"n_symbols={len(chunk)} page={pages}: {type(e).__name__}: {msg}",
                    flush=True,
                )
                break
            items = getattr(resp, "news", None)
            if items is None:
                items = (getattr(resp, "data", {}) or {}).get("news", [])
            for n in items or []:
                item = _news_item_from_alpaca(n)
                if item is None:
                    continue
                if not (start_ts <= item["ts"] < end_ts):
                    # Strict: created_at < run start already via end_ts.
                    if item["ts"] >= end_ts or item["ts"] < start_ts:
                        continue
                for s in item["symbols"]:
                    if s in by_sym:
                        by_sym[s].append(item)
            token = getattr(resp, "next_page_token", None)
            pages += 1
            if not token:
                break
            time.sleep(0.25)
        time.sleep(0.25)
    # Dedupe per symbol by id, newest first.
    for s, rows in by_sym.items():
        seen: set[str] = set()
        uniq: list[dict] = []
        for r in sorted(rows, key=lambda x: -float(x["ts"])):
            rid = str(r.get("id") or "")
            if rid in seen:
                continue
            seen.add(rid)
            uniq.append(r)
        by_sym[s] = uniq
    return by_sym


def load_seen_news_ids(
    report_dir: Path | None = None,
    *,
    lookback_days: int = 5,
    asof_day: str | None = None,
) -> set[tuple[str, str, str]]:
    """Seen (symbol, model_family, news_id) from the last N day logs."""
    base = report_dir or catalyst_dir()
    asof = asof_day or _et().strftime("%Y-%m-%d")
    asof_d = datetime.strptime(asof, "%Y-%m-%d").date()
    seen: set[tuple[str, str, str]] = set()
    for back in range(0, lookback_days + 1):
        day = (asof_d - timedelta(days=back)).strftime("%Y-%m-%d")
        path = base / f"{day}.jsonl"
        if not path.is_file():
            continue
        try:
            for line in path.open(encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("kind") != "score":
                    continue
                sym = str(row.get("symbol") or "").upper()
                model = str(row.get("model_family") or "").lower()
                if not sym or model not in ("grok", "agy"):
                    continue
                for nid in row.get("news_ids") or []:
                    seen.add((sym, model, str(nid)))
                # Failed statuses with empty news still mark the attempt via
                # news_ids when present; when empty, nothing to dedupe.
        except OSError:
            continue
    return seen


def filter_new_news(
    by_sym: dict[str, list[dict]],
    seen: set[tuple[str, str, str]],
    model: str,
) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for sym, items in by_sym.items():
        fresh = [it for it in items if (sym, model, str(it.get("id"))) not in seen]
        if fresh:
            out[sym] = fresh
    return out


def last_run_news_end(day: str, slot_hm: str) -> float | None:
    """news_end of today's last completed run before this slot, from runs.jsonl."""
    path = runs_log_path()
    if not path.is_file():
        return None
    best = None
    best_slot = ""
    try:
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") not in (None, "run", "summary") and row.get("kind") == "skip":
                continue
            if str(row.get("day") or "") != day:
                # slot may encode day
                slot = str(row.get("slot") or "")
                if not slot.startswith(day):
                    continue
            slot = str(row.get("slot") or "")
            if slot_hm == "12:15" and "T08:45" in slot and slot > best_slot:
                if row.get("news_end") is not None:
                    best = float(row["news_end"])
                    best_slot = slot
            elif row.get("news_end") is not None and slot.startswith(day) and slot < f"{day}T{slot_hm}":
                if slot > best_slot:
                    best = float(row["news_end"])
                    best_slot = slot
    except OSError:
        return None
    return best


def morning_gappers_from_runs(day: str) -> list[str]:
    path = runs_log_path()
    if not path.is_file():
        return []
    best = None
    try:
        for line in path.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            slot = str(row.get("slot") or "")
            if f"{day}T08:45" in slot and row.get("gappers"):
                best = [str(s).upper() for s in row["gappers"]]
    except OSError:
        return []
    return best or []


# ---------------------------------------------------------------------------
# Prompt + parse
# ---------------------------------------------------------------------------

def build_prompt(
    names: list[dict],
    *,
    max_headlines: int = MAX_HEADLINES_PER_NAME,
    summary_max: int = SUMMARY_MAX_CHARS,
) -> str:
    """Build the cat-v1 prompt for a batch of name dicts.

    Each name: symbol, company, prior_close, last, last_ts, age_sec, news[list].
    """
    parts = [_PROMPT_HEADER, ""]
    for n in names:
        sym = str(n["symbol"]).upper()
        company = str(n.get("company") or "")
        prior = n.get("prior_close")
        last = n.get("last")
        last_ts = n.get("last_ts")
        age = n.get("age_sec")
        age_s = f"{float(age):.0f}s" if age is not None else "?"
        ts_s = _fmt_et(float(last_ts)) if last_ts else "?"
        parts.append(f"### {sym} — {company}".rstrip(" —"))
        parts.append(f"prior_close: {prior}")
        parts.append(f"last_trade: {last} at {ts_s} (age {age_s})")
        news = list(n.get("news") or [])
        news = sorted(news, key=lambda x: -float(x.get("ts") or 0))[:max_headlines]
        if not news:
            parts.append("news: (none)")
        else:
            parts.append("news (newest first):")
            for it in news:
                summary = str(it.get("summary") or "")[:summary_max]
                parts.append(
                    f"- [{_fmt_et(float(it['ts']))}] {it.get('source') or '?'} | "
                    f"{it.get('headline') or ''} | {summary}"
                )
        parts.append("")
    parts.append(
        "Reply with ONLY one JSON object of the form:\n"
        '{"results":[{"ticker":"AAPL","catalyst_type":"earnings|guidance|analyst|'
        "M&A|FDA/regulatory|product|macro/sector|legal|management|other|none\","
        '"direction":"up|down|neutral","materiality":1,"expected_horizon":'
        '"intraday|days|weeks","confidence":0.0,"rationale":"one sentence"}]}'
    )
    return "\n".join(parts)


def _norm_catalyst_type(v: Any) -> str | None:
    s = str(v or "").strip()
    if not s:
        return None
    lower_map = {c.lower(): c for c in CATALYST_TYPES}
    # Accept common aliases
    aliases = {
        "m&a": "M&A",
        "ma": "M&A",
        "fda": "FDA/regulatory",
        "fda/regulatory": "FDA/regulatory",
        "regulatory": "FDA/regulatory",
        "macro": "macro/sector",
        "sector": "macro/sector",
        "macro/sector": "macro/sector",
    }
    key = s.lower()
    if key in aliases:
        return aliases[key]
    return lower_map.get(key)


def _norm_direction(v: Any) -> str | None:
    s = str(v or "").strip().lower()
    return s if s in DIRECTIONS else None


def _norm_horizon(v: Any) -> str | None:
    s = str(v or "").strip().lower()
    return s if s in HORIZONS else None


def validate_result(row: dict, allowed: set[str]) -> dict | None:
    """Normalize one result object or return None if invalid."""
    ticker = str(row.get("ticker") or row.get("symbol") or "").upper().strip()
    if ticker not in allowed:
        return None
    ctype = _norm_catalyst_type(row.get("catalyst_type"))
    direction = _norm_direction(row.get("direction"))
    horizon = _norm_horizon(row.get("expected_horizon"))
    if ctype is None or direction is None or horizon is None:
        return None
    try:
        mat = int(row.get("materiality"))
    except (TypeError, ValueError):
        return None
    if mat < 1 or mat > 5:
        return None
    try:
        conf = float(row.get("confidence"))
    except (TypeError, ValueError):
        return None
    if conf < 0.0 or conf > 1.0:
        return None
    rationale = str(row.get("rationale") or "").strip()
    if not rationale:
        return None
    return {
        "ticker": ticker,
        "catalyst_type": ctype,
        "direction": direction,
        "materiality": mat,
        "expected_horizon": horizon,
        "confidence": conf,
        "rationale": rationale[:300],
    }


def parse_model_response(text: str, batch_symbols: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Return ({ticker: validated}, missing_tickers). Extra tickers dropped."""
    from ai_suggest import _iter_json_blobs

    allowed = {str(s).upper() for s in batch_symbols}
    blobs = _iter_json_blobs(text or "")
    results_list: list[dict] = []
    for blob in reversed(blobs):
        if isinstance(blob, dict) and isinstance(blob.get("results"), list):
            results_list = [r for r in blob["results"] if isinstance(r, dict)]
            break
        if isinstance(blob, list) and blob and isinstance(blob[0], dict):
            results_list = [r for r in blob if isinstance(r, dict)]
            break
    got: dict[str, dict] = {}
    for r in results_list:
        v = validate_result(r, allowed)
        if v:
            got[v["ticker"]] = v
    missing = sorted(allowed - set(got))
    return got, missing


# ---------------------------------------------------------------------------
# Model settings + CLI version
# ---------------------------------------------------------------------------

def resolve_model_settings(cfg: dict) -> dict[str, dict]:
    return {
        "grok": {
            "model": str(_cfg(cfg, "grok_model", "grok-4.6")),
            "cli_bin": str(_cfg(cfg, "grok_cli_bin", "grok")),
        },
        "agy": {
            "model": str(_cfg(cfg, "agy_model", _cfg(cfg, "claude_model", "gemini-3.1-pro-high"))),
            "cli_bin": str(_cfg(cfg, "agy_cli_bin", _cfg(cfg, "claude_cli_bin", "agy"))),
        },
    }


def cli_version(cli_bin: str) -> str:
    try:
        proc = subprocess.run(
            [cli_bin, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        out = (proc.stdout or proc.stderr or "").strip().splitlines()
        return (out[0] if out else "")[:80]
    except Exception:
        return ""


def probe_auth(cfg: dict) -> dict[str, bool]:
    from ai_suggest import agy_auth_status, cli_logged_in

    settings = resolve_model_settings(cfg)
    grok_ok = False
    agy_ok = False
    try:
        grok_ok = bool(cli_logged_in())
    except Exception:
        grok_ok = False
    try:
        agy_ok = bool(agy_auth_status(settings["agy"]["cli_bin"]).get("logged_in"))
    except Exception:
        agy_ok = False
    return {"grok": grok_ok, "agy": agy_ok}


def call_model(
    family: str,
    prompt: str,
    cfg: dict,
    *,
    phase: str = "ai_catalyst",
    timeout: float = CALL_TIMEOUT_SEC,
) -> str:
    from ai_suggest import call_agy_cli, call_grok_cli

    settings = resolve_model_settings(cfg)
    if family == "grok":
        return call_grok_cli(
            prompt,
            model=settings["grok"]["model"],
            timeout=timeout,
            max_turns=1,
            live_search=False,
            cli_bin=settings["grok"]["cli_bin"],
            phase=phase,
        )
    return call_agy_cli(
        prompt,
        model=settings["agy"]["model"],
        timeout=timeout,
        cli_bin=settings["agy"]["cli_bin"],
        phase=phase,
    )


# ---------------------------------------------------------------------------
# Scoring one batch
# ---------------------------------------------------------------------------

def score_batch(
    family: str,
    batch: list[dict],
    cfg: dict,
    *,
    day: str,
    slot: str,
    run_ts: float,
    spy: dict | None,
    company_names: dict[str, str],
    sources_map: dict[str, list[str]],
    cli_ver: str,
    phase: str = "ai_catalyst",
    call_fn=None,
    deadline_ts: float | None = None,
) -> list[dict]:
    """Score one batch for one model; returns row dicts (one per name)."""
    call_fn = call_fn or (lambda fam, prompt, **kw: call_model(fam, prompt, cfg, phase=phase))
    symbols = [str(n["symbol"]).upper() for n in batch]
    batch_id = hashlib.sha1(
        f"{day}|{slot}|{family}|{','.join(symbols)}".encode()
    ).hexdigest()[:12]
    settings = resolve_model_settings(cfg)
    model_id = settings[family]["model"]
    prompt = build_prompt(batch)
    psha = prompt_sha256(prompt)

    def _base_row(sym: str, status: str, **extra: Any) -> dict:
        name = next(n for n in batch if str(n["symbol"]).upper() == sym)
        news = list(name.get("news") or [])[:MAX_HEADLINES_PER_NAME]
        news = sorted(news, key=lambda x: -float(x.get("ts") or 0))
        px = {
            "last": name.get("last"),
            "ts": name.get("last_ts"),
            "age_sec": name.get("age_sec"),
            "source": name.get("price_source") or "alpaca_iex_snapshot",
        }
        spy_row = None
        if spy:
            spy_row = {
                "last": spy.get("last"),
                "ts": spy.get("last_ts"),
                "age_sec": spy.get("age_sec"),
                "prior_close": spy.get("prior_close"),
            }
        return {
            "row_id": row_id(day, slot, sym, family),
            "kind": "score",
            "day": day,
            "slot": slot,
            "run_ts": run_ts,
            "scored_ts": time.time(),
            "symbol": sym,
            "company": company_names.get(sym) or name.get("company") or "",
            "sources": list(sources_map.get(sym) or name.get("sources") or []),
            "model_family": family,
            "model_id": model_id,
            "cli_version": cli_ver,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": psha,
            "batch_id": batch_id,
            "batch_symbols": symbols,
            "news_ids": [str(it.get("id")) for it in news],
            "news_urls": [str(it.get("url") or "") for it in news],
            "headlines_sha256": headlines_sha256(news),
            "n_news": len(news),
            "newest_news_ts": float(news[0]["ts"]) if news else None,
            "oldest_news_ts": float(news[-1]["ts"]) if news else None,
            "price": px,
            "prior_close": name.get("prior_close"),
            "spy": spy_row,
            "status": status,
            "retry_used": False,
            "raw_response": None,
            "raw_response_retry": None,
            "error": None,
            "catalyst_type": None,
            "direction": None,
            "materiality": None,
            "expected_horizon": None,
            "confidence": None,
            "rationale": None,
            **extra,
        }

    if deadline_ts is not None and time.time() >= deadline_ts:
        return [_base_row(s, "deadline") for s in symbols]

    raw1 = None
    raw2 = None
    err = None
    try:
        raw1 = call_fn(family, prompt)
        got, missing = parse_model_response(raw1, symbols)
    except Exception as e:
        err_s = str(e)
        status = "timeout" if "timed out" in err_s.lower() else "call_error"
        return [_base_row(s, status, error=err_s[:300], raw_response=None) for s in symbols]

    need_retry = sorted(set(missing) | (
        set(symbols) - set(got)  # invalid ones already in missing
    ))
    # Also retry if parse produced nothing at all.
    if not got and symbols:
        need_retry = list(symbols)

    retry_used = False
    if need_retry:
        retry_used = True
        retry_prompt = prompt + "\n\n" + _RETRY_SUFFIX
        # If only some missing, ask only for those tickers.
        if got and need_retry != symbols:
            retry_prompt += (
                "\nInclude results ONLY for these tickers: "
                + ", ".join(need_retry)
            )
        try:
            raw2 = call_fn(family, retry_prompt)
            got2, _ = parse_model_response(raw2, need_retry)
            got.update(got2)
        except Exception as e:
            err = str(e)[:300]

    rows: list[dict] = []
    for sym in symbols:
        if sym in got:
            v = got[sym]
            row = _base_row(
                sym, "ok",
                catalyst_type=v["catalyst_type"],
                direction=v["direction"],
                materiality=v["materiality"],
                expected_horizon=v["expected_horizon"],
                confidence=v["confidence"],
                rationale=v["rationale"],
            )
            row["retry_used"] = retry_used and (
                raw2 is not None  # any retry attempted
            )
            row["raw_response"] = (raw1 or "")[:4000]
            row["raw_response_retry"] = (raw2 or "")[:4000] if raw2 else None
            rows.append(row)
        else:
            status = "parse_fail" if (raw1 or raw2) else ("timeout" if err and "timed out" in (err or "").lower() else "missing")
            if err and not raw1 and not raw2:
                status = "timeout" if "timed out" in err.lower() else "call_error"
            elif raw1 and sym not in got:
                status = "missing" if retry_used else "parse_fail"
                # After retry still missing → missing; if both attempts unparseable for all → parse_fail
                if retry_used and not got:
                    status = "parse_fail"
                elif retry_used:
                    status = "missing"
            row = _base_row(
                sym, status,
                error=err,
                raw_response=(raw1 or "")[:4000] if raw1 else None,
                raw_response_retry=(raw2 or "")[:4000] if raw2 else None,
                retry_used=retry_used,
            )
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Candidate assembly
# ---------------------------------------------------------------------------

def assemble_candidates(
    *,
    day: str,
    slot_hm: str,
    universe_syms: list[str],
    gappers: list[str],
    boards: dict[str, list[str]],
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Return (sources_map symbol→[tags], counts_by_source)."""
    sources: dict[str, list[str]] = {}
    counts: dict[str, int] = {}

    def _add(tag: str, syms: list[str]) -> None:
        counts[tag] = len(syms)
        for s in syms:
            s = str(s).upper()
            sources.setdefault(s, [])
            if tag not in sources[s]:
                sources[s].append(tag)

    _add("universe", universe_syms)
    if slot_hm == "08:45":
        _add("gapper", gappers)
    else:
        # 12:15 reuses morning gappers as candidates too
        _add("gapper", gappers)
    for tag, syms in boards.items():
        _add(tag, syms)
    return sources, counts


def pick_eligible(
    sources_map: dict[str, list[str]],
    news_by_model: dict[str, dict[str, list[dict]]],
    *,
    max_names: int = MAX_NAMES_PER_RUN,
) -> tuple[list[str], int]:
    """Names with ≥1 new item for any model; newest-first; capped."""
    newest: dict[str, float] = {}
    for _model, by_sym in news_by_model.items():
        for sym, items in by_sym.items():
            if not items:
                continue
            ts = max(float(it["ts"]) for it in items)
            newest[sym] = max(newest.get(sym, 0.0), ts)
    # Only candidates that were in the source union.
    eligible = [s for s in newest if s in sources_map]
    eligible.sort(key=lambda s: (-newest[s], s))
    kept = eligible[:max_names]
    dropped = max(0, len(eligible) - len(kept))
    return kept, dropped


# ---------------------------------------------------------------------------
# Child run
# ---------------------------------------------------------------------------

def news_window(day: str, slot_hm: str, run_ts: float) -> tuple[float, float]:
    end = run_ts
    if slot_hm == "08:45":
        start = previous_session_close_ts(day)
    else:
        start = last_run_news_end(day, slot_hm)
        if start is None:
            start = previous_session_close_ts(day)
    return start, end


def run_slot(
    slot: str,
    cfg: dict | None = None,
    *,
    now: float | None = None,
    call_fn=None,
    skip_network_setup: bool = False,
) -> dict:
    """Full scheduled run. Writes day log + runs.jsonl."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if not cfg and not skip_network_setup:
        from config import load_config
        cfg = load_config()
    if not enabled(cfg):
        return {"ok": False, "reason": "disabled"}

    t0 = float(now if now is not None else time.time())
    deadline = t0 + RUN_DEADLINE_SEC
    # slot like 2026-09-28T08:45
    day = slot[:10]
    slot_hm = slot[11:16] if len(slot) >= 16 else slot.split("T")[-1][:5]

    if not is_trading_day(day):
        append_jsonl(runs_log_path(), {
            "kind": "skip", "reason": "holiday", "slot": slot, "day": day, "ts": t0,
        })
        return {"ok": False, "reason": "holiday"}

    auth = probe_auth(cfg)
    settings = resolve_model_settings(cfg)
    versions = {
        "grok": cli_version(settings["grok"]["cli_bin"]) if auth.get("grok") else "",
        "agy": cli_version(settings["agy"]["cli_bin"]) if auth.get("agy") else "",
    }

    uni = load_universe()
    universe_syms = list(uni["symbols"])
    boards = load_seed_boards(day)

    gappers: list[str] = []
    gapper_rows: list[dict] = []
    company_names: dict[str, str] = {}

    # Company names from assets (best effort) + gapper scan on 08:45.
    try:
        assets = load_scan_universe_assets()
        company_names = {s: n for s, n in assets}
        scan_syms = [s for s, _ in assets]
    except Exception as e:
        print(f"[ai_catalyst] assets failed: {e}", flush=True)
        scan_syms = list(universe_syms)

    if slot_hm == "08:45":
        try:
            snaps = fetch_snapshots(scan_syms)
            gapper_rows = select_gappers(snaps, now=t0)
            gappers = [r["symbol"] for r in gapper_rows]
        except Exception as e:
            print(f"[ai_catalyst] gapper scan failed: {e}", flush=True)
    else:
        gappers = morning_gappers_from_runs(day)

    sources_map, source_counts = assemble_candidates(
        day=day, slot_hm=slot_hm, universe_syms=universe_syms,
        gappers=gappers, boards=boards,
    )
    candidates = sorted(sources_map)

    news_start, news_end = news_window(day, slot_hm, t0)
    try:
        news_all = fetch_alpaca_news(candidates, news_start, news_end)
    except Exception as e:
        print(f"[ai_catalyst] news fetch failed: {e}", flush=True)
        news_all = {s: [] for s in candidates}

    seen = load_seen_news_ids(asof_day=day)
    news_by_model: dict[str, dict[str, list[dict]]] = {}
    for fam in ("grok", "agy"):
        news_by_model[fam] = filter_new_news(news_all, seen, fam)

    eligible, dropped = pick_eligible(sources_map, news_by_model)

    # Prices for eligible + SPY
    price_syms = sorted(set(eligible) | {"SPY"})
    try:
        snaps = fetch_snapshots(price_syms)
    except Exception as e:
        print(f"[ai_catalyst] price snapshot failed: {e}", flush=True)
        snaps = {}
    spy = snaps.get("SPY")

    # Build name payloads per model (news may differ by dedupe).
    def _names_for(fam: str) -> list[dict]:
        out = []
        for sym in eligible:
            items = news_by_model[fam].get(sym) or []
            if not items:
                continue
            snap = snaps.get(sym) or {}
            out.append({
                "symbol": sym,
                "company": company_names.get(sym, ""),
                "prior_close": snap.get("prior_close"),
                "last": snap.get("last"),
                "last_ts": snap.get("last_ts"),
                "age_sec": snap.get("age_sec"),
                "price_source": "alpaca_iex_snapshot",
                "news": items[:MAX_HEADLINES_PER_NAME],
                "sources": sources_map.get(sym) or [],
            })
        out.sort(key=lambda n: n["symbol"])
        return out

    cost_start = t0
    calls: dict[str, int] = {"grok": 0, "agy": 0}
    failures: dict[str, int] = {"grok": 0, "agy": 0}
    all_rows: list[dict] = []

    def _run_model(fam: str) -> list[dict]:
        if not auth.get(fam):
            names = _names_for(fam)
            # Still mark as auth_skip so they count as scored / seen.
            rows = []
            for n in names:
                news = n.get("news") or []
                rows.append({
                    "row_id": row_id(day, slot, n["symbol"], fam),
                    "kind": "score",
                    "day": day,
                    "slot": slot,
                    "run_ts": t0,
                    "scored_ts": time.time(),
                    "symbol": n["symbol"],
                    "company": n.get("company") or "",
                    "sources": n.get("sources") or [],
                    "model_family": fam,
                    "model_id": settings[fam]["model"],
                    "cli_version": versions.get(fam) or "",
                    "prompt_version": PROMPT_VERSION,
                    "prompt_sha256": "",
                    "batch_id": "auth_skip",
                    "batch_symbols": [n["symbol"]],
                    "news_ids": [str(it.get("id")) for it in news],
                    "news_urls": [str(it.get("url") or "") for it in news],
                    "headlines_sha256": headlines_sha256(news),
                    "n_news": len(news),
                    "newest_news_ts": float(news[0]["ts"]) if news else None,
                    "oldest_news_ts": float(news[-1]["ts"]) if news else None,
                    "price": {
                        "last": n.get("last"), "ts": n.get("last_ts"),
                        "age_sec": n.get("age_sec"),
                        "source": "alpaca_iex_snapshot",
                    },
                    "prior_close": n.get("prior_close"),
                    "spy": ({
                        "last": spy.get("last"), "ts": spy.get("last_ts"),
                        "age_sec": spy.get("age_sec"),
                        "prior_close": spy.get("prior_close"),
                    } if spy else None),
                    "status": "auth_skip",
                    "retry_used": False,
                    "raw_response": None,
                    "raw_response_retry": None,
                    "error": "auth_skip",
                    "catalyst_type": None,
                    "direction": None,
                    "materiality": None,
                    "expected_horizon": None,
                    "confidence": None,
                    "rationale": None,
                })
            return rows

        names = _names_for(fam)
        batches = [names[i:i + BATCH_SIZE] for i in range(0, len(names), BATCH_SIZE)]
        rows_out: list[dict] = []
        call_count = 0

        def _one(batch: list[dict]) -> list[dict]:
            nonlocal call_count
            if time.time() >= deadline:
                return score_batch(
                    fam, batch, cfg, day=day, slot=slot, run_ts=t0, spy=spy,
                    company_names=company_names, sources_map=sources_map,
                    cli_ver=versions.get(fam) or "", call_fn=call_fn,
                    deadline_ts=deadline,
                )
            if call_count >= MAX_CALLS_PER_MODEL_PER_RUN:
                return score_batch(
                    fam, batch, cfg, day=day, slot=slot, run_ts=t0, spy=spy,
                    company_names=company_names, sources_map=sources_map,
                    cli_ver=versions.get(fam) or "", call_fn=call_fn,
                    deadline_ts=0,  # force deadline status
                )
            call_count += 1
            # score_batch may do a retry (=2nd call); count conservatively
            return score_batch(
                fam, batch, cfg, day=day, slot=slot, run_ts=t0, spy=spy,
                company_names=company_names, sources_map=sources_map,
                cli_ver=versions.get(fam) or "", call_fn=call_fn,
                deadline_ts=deadline,
            )

        with ThreadPoolExecutor(max_workers=MODEL_CONCURRENCY) as pool:
            futs = [pool.submit(_one, b) for b in batches]
            for fut in as_completed(futs):
                try:
                    part = fut.result()
                except Exception as e:
                    print(f"[ai_catalyst] batch failed: {e}", flush=True)
                    part = []
                rows_out.extend(part)
                # Append as we go
                for row in part:
                    append_jsonl(day_log_path(day), row)
                    if row.get("status") != "ok":
                        failures[fam] = failures.get(fam, 0) + 1
        calls[fam] = call_count
        return rows_out

    # Models run independently (sequential families; concurrency inside).
    for fam in ("grok", "agy"):
        all_rows.extend(_run_model(fam))

    cost = _sum_phase_cost("ai_catalyst", since=cost_start)
    wall = time.time() - t0
    summary = {
        "kind": "run",
        "slot": slot,
        "day": day,
        "ts": t0,
        "news_start": news_start,
        "news_end": news_end,
        "candidates_by_source": source_counts,
        "n_candidates": len(candidates),
        "eligible": len(eligible),
        "dropped": dropped,
        "gappers": gappers,
        "gapper_rows": gapper_rows[:GAP_TOP],
        "calls": calls,
        "failures": failures,
        "auth": auth,
        "wall_sec": round(wall, 2),
        "total_cost_usd": cost,
        "prompt_version": PROMPT_VERSION,
    }
    append_jsonl(runs_log_path(), summary)
    print(f"[ai_catalyst] done slot={slot} eligible={len(eligible)} "
          f"calls={calls} failures={failures} wall={wall:.0f}s cost={cost}",
          flush=True)
    return summary


def _sum_phase_cost(phase: str, *, since: float) -> float:
    path = _report_dir() / "token_metrics.jsonl"
    if not path.is_file():
        return 0.0
    total = 0.0
    try:
        for line in path.open(encoding="utf-8"):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("phase") != phase:
                continue
            if float(row.get("ts") or 0) < since - 1:
                continue
            c = row.get("total_cost_usd")
            if c is not None:
                try:
                    total += float(c)
                except (TypeError, ValueError):
                    pass
    except OSError:
        return total
    return round(total, 6)


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

def run_dry(
    symbols: list[str],
    cfg: dict | None = None,
    *,
    models: list[str] | None = None,
    since_hours: float = 72.0,
    out: Path | None = None,
    now: float | None = None,
    call_fn=None,
) -> list[dict]:
    cfg = cfg if isinstance(cfg, dict) else {}
    if not cfg:
        from config import load_config
        cfg = load_config()
    t0 = float(now if now is not None else time.time())
    day = _et(t0).strftime("%Y-%m-%d")
    slot = f"{day}Tdryrun"
    models = [m.strip().lower() for m in (models or ["grok", "agy"]) if m.strip()]
    syms = [str(s).upper().strip() for s in symbols if str(s).strip()]
    if not syms:
        return []

    auth = probe_auth(cfg)
    settings = resolve_model_settings(cfg)
    versions = {m: (cli_version(settings[m]["cli_bin"]) if auth.get(m) else "") for m in models}

    news_start = t0 - since_hours * 3600
    news_end = t0
    try:
        news_all = fetch_alpaca_news(syms, news_start, news_end)
    except Exception as e:
        print(f"[ai_catalyst] dry news failed: {e}", flush=True)
        news_all = {s: [] for s in syms}

    try:
        snaps = fetch_snapshots(sorted(set(syms) | {"SPY"}))
    except Exception:
        snaps = {}
    spy = snaps.get("SPY")

    # Best-effort company names
    company_names: dict[str, str] = {}
    try:
        for s, n in load_scan_universe_assets():
            if s in syms:
                company_names[s] = n
    except Exception:
        pass

    sources_map = {s: ["dryrun"] for s in syms}
    rows: list[dict] = []
    phase = "ai_catalyst_dryrun"

    for fam in models:
        if not auth.get(fam):
            print(f"[ai_catalyst] dry auth_skip model={fam}", flush=True)
            for s in syms:
                items = news_all.get(s) or []
                snap = snaps.get(s) or {}
                rows.append({
                    "row_id": row_id(day, slot, s, fam),
                    "kind": "score",
                    "day": day,
                    "slot": slot,
                    "run_ts": t0,
                    "scored_ts": time.time(),
                    "symbol": s,
                    "company": company_names.get(s, ""),
                    "sources": ["dryrun"],
                    "model_family": fam,
                    "model_id": settings[fam]["model"],
                    "cli_version": versions.get(fam) or "",
                    "prompt_version": PROMPT_VERSION,
                    "prompt_sha256": "",
                    "batch_id": "dryrun",
                    "batch_symbols": syms,
                    "news_ids": [str(it.get("id")) for it in items[:MAX_HEADLINES_PER_NAME]],
                    "news_urls": [str(it.get("url") or "") for it in items[:MAX_HEADLINES_PER_NAME]],
                    "headlines_sha256": headlines_sha256(items[:MAX_HEADLINES_PER_NAME]),
                    "n_news": min(len(items), MAX_HEADLINES_PER_NAME),
                    "newest_news_ts": float(items[0]["ts"]) if items else None,
                    "oldest_news_ts": float(items[min(len(items), MAX_HEADLINES_PER_NAME) - 1]["ts"]) if items else None,
                    "price": {
                        "last": snap.get("last"), "ts": snap.get("last_ts"),
                        "age_sec": snap.get("age_sec"), "source": "alpaca_iex_snapshot",
                    },
                    "prior_close": snap.get("prior_close"),
                    "spy": None,
                    "status": "auth_skip",
                    "retry_used": False,
                    "raw_response": None,
                    "raw_response_retry": None,
                    "error": "auth_skip",
                    "catalyst_type": None,
                    "direction": None,
                    "materiality": None,
                    "expected_horizon": None,
                    "confidence": None,
                    "rationale": None,
                })
            continue

        batch = []
        for s in sorted(syms):
            items = news_all.get(s) or []
            snap = snaps.get(s) or {}
            batch.append({
                "symbol": s,
                "company": company_names.get(s, ""),
                "prior_close": snap.get("prior_close"),
                "last": snap.get("last"),
                "last_ts": snap.get("last_ts"),
                "age_sec": snap.get("age_sec"),
                "price_source": "alpaca_iex_snapshot",
                "news": items[:MAX_HEADLINES_PER_NAME],
                "sources": ["dryrun"],
            })
        # Dry run: score even with zero news (still call model with empty news).
        if not batch:
            continue

        def _call(fam_, prompt, **_kw):
            if call_fn:
                return call_fn(fam_, prompt)
            return call_model(fam_, prompt, cfg, phase=phase)

        # Process in BATCH_SIZE chunks
        for i in range(0, len(batch), BATCH_SIZE):
            part = score_batch(
                fam, batch[i:i + BATCH_SIZE], cfg,
                day=day, slot=slot, run_ts=t0, spy=spy,
                company_names=company_names, sources_map=sources_map,
                cli_ver=versions.get(fam) or "",
                phase=phase, call_fn=_call,
            )
            rows.extend(part)

    pretty = json.dumps(rows, indent=2, default=str)
    print(pretty)
    if out is None:
        out = dryrun_out_dir() / f"{day}_{int(t0)}.json"
    else:
        out = Path(out)
        if not out.is_absolute():
            out = dryrun_out_dir() / out
        out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(pretty + "\n", encoding="utf-8")
    print(f"[ai_catalyst] dry-run wrote {out} ({len(rows)} rows)", flush=True)
    return rows


# ---------------------------------------------------------------------------
# Tick (inside ai_trader) — never blocks
# ---------------------------------------------------------------------------

def due(now: float | None = None, last_slot: str | None = None) -> str | None:
    from ai_suggest import due_slot, parse_research_times

    t0 = float(now if now is not None else time.time())
    last = last_slot if last_slot is not None else _load_last_slot()
    best: str | None = None
    for hm in RUN_TIMES:
        times = parse_research_times([hm])
        catch = int(CATCHUP_MIN.get(hm, 40))
        slot = due_slot(
            t0, times=times, weekdays_only=True,
            catchup_min=catch, last_slot=last,
        )
        if slot and (best is None or slot > best):
            best = slot
    return best


def _spawn(args: list[str]) -> bool:
    """Spawn child; return True if started. Skip if previous still alive."""
    global _CHILD
    with _CHILD_LOCK:
        if _CHILD is not None and _CHILD.poll() is None:
            print("[ai_catalyst] skip: previous run alive", flush=True)
            return False
        log = open(log_file_path(), "ab")
        _CHILD = subprocess.Popen(
            [_py(), "-u", str(ROOT / "ai_catalyst.py"), *args],
            cwd=str(ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True


def tick(cfg: dict | None, now: float | None = None) -> str | None:
    """Cheap scheduler hook for ai_trader. Spawns only; never blocks."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if not enabled(cfg):
        return None
    t0 = float(now if now is not None else time.time())

    # Dry-run request under launchd context (AGY Keychain).
    req = dryrun_request_path()
    if req.is_file():
        try:
            text = req.read_text(encoding="utf-8").strip()
            taken = dryrun_taken_path()
            req.replace(taken)
        except OSError as e:
            print(f"[ai_catalyst] dryrun.request failed: {e}", flush=True)
            return None
        syms = ",".join(
            s.strip().upper() for s in text.replace("\n", ",").split(",") if s.strip()
        )
        if not syms:
            return None
        if _spawn(["--dry-run", "--symbols", syms]):
            return "dryrun started"
        return None

    slot = due(t0)
    if not slot:
        return None
    # Write last_slot first so a crash cannot double-fire.
    _save_last_slot(slot)
    if _spawn(["--run", slot]):
        return f"run started slot={slot}"
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AI catalyst shadow logger (log-only)")
    ap.add_argument("--run", metavar="SLOT", help="Scheduled run slot YYYY-MM-DDTHH:MM")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--symbols", default="", help="Comma-separated symbols for dry-run")
    ap.add_argument("--models", default="grok,agy")
    ap.add_argument("--since-hours", type=float, default=72.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    if args.run:
        run_slot(args.run)
        return 0
    if args.dry_run:
        syms = [s for s in args.symbols.split(",") if s.strip()]
        models = [m for m in args.models.split(",") if m.strip()]
        run_dry(syms, models=models, since_hours=args.since_hours, out=args.out)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
