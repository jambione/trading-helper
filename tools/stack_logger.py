#!/usr/bin/env python3
"""stack_logger - record every layer the operator's stack model uses, once a minute, so stacks can be backtested.

Operator 11/20: "Everything in our system is not an isolated event. They all work together to prove a direction and a
movement" -> "build the logger". The trending SCORE, mention counts/rates and the desk's per-name readings were never
saved, so no stack using them could be tested. This polls the dashboard's /api/state (read-only, the same feed the
momentum monitor reads) and appends one compact snapshot per minute to ai_reports/stack_log/DAY.jsonl.gz.

It never trades, never writes to the dashboard, and only reads. Weekdays 04:00-20:00 ET.

    .venv/bin/python tools/stack_logger.py            # loop (run by the LaunchAgent com.jambi.stack-logger)
    .venv/bin/python tools/stack_logger.py --once     # one snapshot to stdout (check)

Snapshot record: {"ts", "et", "trending": [...], "movers": [...], "ai": [...], "tickers": [...], "catalysts": {sym: {...}},
"discord_n", "health"}. "catalysts" (operator 11/20: "We need to have multiple sources, such as catalysts outside of
price and volume") is read from the desk's local caches only - news_feed (headline count, minutes since, bullish /
bearish keyword flags, cache age) and float_feed (shares out) - so the logger makes no extra data-provider calls.

Added 11/20 (operator: "Yes, add those three to the logger"):
  "etfs"   - market and sector ETF day change / last price, one Alpaca snapshot call per minute (IEX, the live feed);
             a stock's sector/peer link is derived later from history, so no sector table is needed here
  "halts"  - today's trading halts from Nasdaq's public halt feed (symbol, time, reason code, resumption times)
  "ssr"    - today's short-sale-restriction list (Nasdaq Reg SHO file), the snapshot's symbols that are on it + count
  "short"  - per snapshot symbol, the latest FINRA short interest (short position, days to cover, settlement date)
             from data/finra_short_interest; new FINRA files are fetched at most hourly outside 04:00-20:00
"""
from __future__ import annotations

import gzip
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "ai_reports" / "stack_log"
ET = ZoneInfo("America/New_York")
PERIOD = 60.0

TRENDING_KEYS = ("symbol", "rank", "trending_score", "watchlist_count", "price", "pct_change", "vol_session", "rvol",
                 "price_age_sec", "high_52w", "low_52w", "avg_vol_consolidated")
MOVER_KEYS = ("symbol", "score", "pct_change", "price", "rvol", "float_m", "dollar_volume", "origin", "criteria", "live_pct")
AI_KEYS = ("symbol", "rank", "trending_score", "p30", "p50", "p100", "position_pct", "source")
TICKER_KEYS = ("ticker", "src", "price", "price_age_sec", "prev_close", "pct_change", "mention_count", "mention_window",
               "mention_burst", "price_stale")
SP_KEYS = ("status", "proximity_pct", "is_hot", "mention_velocity", "cm_rsi", "cm_ok", "cm_rsi_rising", "pctr",
           "pctr_ok", "pctr_rising", "pctr_slow", "in_position")
FUNNEL_KEYS = ("score", "state", "rvol", "rejects")


def _pick(d: dict, keys) -> dict:
    return {k: d.get(k) for k in keys if d.get(k) is not None}


def snapshot(state: dict, now: float) -> dict:
    """Compact, JSON-safe record of the layers in one /api/state payload. Pure."""
    tr = state.get("trending") or {}
    mv = state.get("movers") or {}
    ai = state.get("ai_suggestions") or {}
    dc = state.get("discord") or {}
    tickers = []
    for t in state.get("tickers") or []:
        row = _pick(t, TICKER_KEYS)
        sp = t.get("signal_proximity")
        if isinstance(sp, dict):
            row["sp"] = _pick(sp, SP_KEYS)
        fn = t.get("funnel")
        if isinstance(fn, dict):
            row["funnel"] = _pick(fn, FUNNEL_KEYS)
        tickers.append(row)
    syms = sorted({r.get("symbol") for k in ("trending", "movers", "ai_suggestions")
                   for r in ((state.get(k) or {}).get("rows") or []) if isinstance(r, dict) and r.get("symbol")}
                  | {t.get("ticker") for t in (state.get("tickers") or []) if t.get("ticker")})
    return {
        "ts": round(now, 1),
        "et": datetime.fromtimestamp(now, ET).strftime("%H:%M:%S"),
        "trending": [_pick(r, TRENDING_KEYS) for r in (tr.get("rows") or []) if isinstance(r, dict)],
        "movers": [_pick(r, MOVER_KEYS) for r in (mv.get("rows") or []) if isinstance(r, dict)],
        "ai": [_pick(r, AI_KEYS) for r in (ai.get("rows") or []) if isinstance(r, dict)],
        "tickers": tickers,
        "catalysts": catalysts(syms, now),
        "discord_n": dc.get("count"),
        "health": {"trending_updated": tr.get("updated"), "trending_error": tr.get("error") or None,
                   "trending_volume_error": tr.get("volume_error") or None, "rvol_coverage": tr.get("rvol_coverage"),
                   "movers_ts": mv.get("ts"), "discord_running": dc.get("running"), "discord_last_ts": dc.get("last_ts")},
    }


def catalysts(syms, now: float) -> dict:
    """Per-symbol catalyst layer from local caches (never the network). Missing caches -> {}. Pure apart from reading
    the two cache files."""
    out = {}
    try:
        import news_feed
        age = news_feed.cache_age_sec()
    except Exception:                               # noqa: BLE001
        news_feed, age = None, None
    try:
        import float_feed
        fcache = float_feed.load_cache()
    except Exception:                               # noqa: BLE001
        fcache = {}
    for s in syms:
        row = {}
        if news_feed is not None:
            f = news_feed.features_for(s, now)
            row.update({k: v for k, v in f.items() if v is not None})
        fl = fcache.get(s) if isinstance(fcache, dict) else None
        if isinstance(fl, dict):
            row.update({k: fl.get(k) for k in ("shares_out", "float") if fl.get(k) is not None})
        if row:
            out[s] = row
    if age is not None:
        out["_news_cache_age_sec"] = round(age, 1)
    return out


ETFS = ("SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC",
        "SMH", "XBI", "ARKK", "IBIT", "URA", "KRE", "GLD")
UA = {"User-Agent": "Mozilla/5.0 (research logger)"}
SI_DIR = ROOT / "data" / "finra_short_interest"
_slow = {"ssr_t": 0.0, "ssr": set(), "si_t": 0.0, "si": {}, "si_file": None, "finra_try_t": 0.0}


def etf_snapshot() -> dict:
    """{sym: {"px", "chg"}} for the market/sector ETFs from one Alpaca snapshot call (IEX). Errors -> {}."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockSnapshotRequest
        from alpaca.data.enums import DataFeed
        k = json.load(open(ROOT / "secrets_data.json"))
        cl = StockHistoricalDataClient(k["alpaca_data_api_key"], k["alpaca_data_secret_key"])
        snap = cl.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=list(ETFS), feed=DataFeed.IEX))
        out = {}
        for s, v in snap.items():
            px = getattr(getattr(v, "latest_trade", None), "price", None)
            pc = getattr(getattr(v, "previous_daily_bar", None), "close", None)
            if px and pc:
                out[s] = {"px": round(float(px), 4), "chg": round((float(px) / float(pc) - 1) * 100, 3)}
        return out
    except Exception as e:                          # noqa: BLE001
        return {"_error": str(e)[:120]}


def parse_halts(xml_text: str) -> list[dict]:
    """Nasdaq trade-halt RSS -> [{sym, date, time, reason, resume_quote, resume_trade}]. Pure."""
    import re
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml_text, re.S):
        g = lambda tag: (re.search(rf"<ndaq:{tag}>(.*?)</ndaq:{tag}>", item, re.S) or [None, None])[1]
        sym = g("IssueSymbol")
        if sym:
            out.append({"sym": sym.strip(), "date": (g("HaltDate") or "").strip(), "time": (g("HaltTime") or "").strip(),
                        "reason": (g("ReasonCode") or "").strip(), "resume_quote": (g("ResumptionQuoteTime") or "").strip(),
                        "resume_trade": (g("ResumptionTradeTime") or "").strip()})
    return out


def parse_ssr(text: str) -> set:
    """Nasdaq Reg SHO short-sale circuit-breaker file -> set of symbols. Pure."""
    rows = [r.split(",") for r in text.splitlines()[1:] if r.strip()]
    return {r[0].strip() for r in rows if r and r[0].strip() and r[0].strip().isascii()}


def _get(url: str, timeout: float = 8.0) -> str | None:
    try:
        with urlopen(Request(url, headers=UA), timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:                               # noqa: BLE001
        return None


def halts_now(now: float) -> list[dict]:
    txt = _get("https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts")
    if txt is None:
        return [{"_error": "fetch failed"}]
    today = datetime.fromtimestamp(now, ET).strftime("%m/%d/%Y")
    return [h for h in parse_halts(txt) if h["date"] == today]


def ssr_now(now: float) -> set:
    if now - _slow["ssr_t"] >= 300:                 # every 5 minutes
        txt = _get(f"https://www.nasdaqtrader.com/dynamic/symdir/shorthalts/shorthalts"
                   f"{datetime.fromtimestamp(now, ET).strftime('%Y%m%d')}.txt")
        if txt is not None:
            _slow["ssr"] = parse_ssr(txt)
        _slow["ssr_t"] = now
    return _slow["ssr"]


def short_interest_table() -> dict:
    """{sym: {short, dtc, settle}} from the newest local FINRA file (cached by file name)."""
    files = sorted(SI_DIR.glob("shrt*.csv"))
    if not files:
        return {}
    f = files[-1]
    if _slow["si_file"] != f.name:
        tab = {}
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        head = [h.strip('"') for h in lines[0].split("|")]
        ix = {h: i for i, h in enumerate(head)}
        for ln in lines[1:]:
            c = [x.strip('"') for x in ln.split("|")]
            try:
                tab[c[ix["symbolCode"]]] = {"short": int(float(c[ix["currentShortPositionQuantity"]] or 0)),
                                            "dtc": float(c[ix["daysToCoverQuantity"]] or 0),
                                            "settle": c[ix["settlementDate"]]}
            except (KeyError, ValueError, IndexError):
                continue
        _slow["si"], _slow["si_file"] = tab, f.name
    return _slow["si"]


def fetch_finra(now: float) -> list[str]:
    """Outside the logging window, at most hourly: fetch FINRA short-interest files for settlement dates in the last
    45 days that are missing locally (15th and last business day of each month). -> names saved."""
    import calendar
    from datetime import date, timedelta
    if now - _slow["finra_try_t"] < 3600:
        return []
    _slow["finra_try_t"] = now
    SI_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.fromtimestamp(now, ET).date()
    cands = set()
    for back in range(0, 60, 1):
        d = today - timedelta(days=back)
        if back > 45:
            break
        last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        for x in (date(d.year, d.month, 15), last):
            while x.weekday() >= 5:
                x -= timedelta(days=1)
            if x <= today:
                cands.add(x)
    saved = []
    for x in sorted(cands):
        name = f"shrt{x.strftime('%Y%m%d')}.csv"
        if (SI_DIR / name).exists():
            continue
        txt = _get(f"https://cdn.finra.org/equity/otcmarket/biweekly/{name}", timeout=30)
        if txt and txt.startswith('"accountingYearMonthNumber"'):
            (SI_DIR / name).write_text(txt, encoding="utf-8")
            saved.append(name)
    return saved


def slow_layers(syms, now: float) -> dict:
    """The three 11/20 layers for one snapshot. Never raises."""
    out = {}
    try:
        out["etfs"] = etf_snapshot()
    except Exception as e:                          # noqa: BLE001
        out["etfs"] = {"_error": str(e)[:120]}
    try:
        out["halts"] = halts_now(now)
    except Exception as e:                          # noqa: BLE001
        out["halts"] = [{"_error": str(e)[:120]}]
    try:
        ssr = ssr_now(now)
        out["ssr"] = {"n": len(ssr), "on_list": sorted(set(syms) & ssr)}
    except Exception as e:                          # noqa: BLE001
        out["ssr"] = {"_error": str(e)[:120]}
    try:
        si = short_interest_table()
        out["short"] = {s: si[s] for s in syms if s in si}
        out["short_file"] = _slow["si_file"]
    except Exception as e:                          # noqa: BLE001
        out["short"] = {"_error": str(e)[:120]}
    return out


def in_window(now: float) -> bool:
    d = datetime.fromtimestamp(now, ET)
    return d.weekday() < 5 and "0400" <= d.strftime("%H%M") < "2000"


def fetch_state() -> dict | None:
    import windows_agent as wa                      # the momentum monitor's auth path (token or desk secret)
    headers = {"Accept": "application/json", "User-Agent": wa._UA}
    try:
        wa._ensure_token()
        headers.update(wa._auth_header())
        with urlopen(Request("http://127.0.0.1:8888/api/state", headers=headers), timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception as e:                          # noqa: BLE001
        print(time.strftime("%H:%M:%S"), "fetch failed:", str(e)[:120], flush=True)
        return None


def append(rec: dict, now: float) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{datetime.fromtimestamp(now, ET).strftime('%Y-%m-%d')}.jsonl.gz"
    with gzip.open(p, "at") as f:                   # one gzip member per write; readers handle concatenated members
        f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
    return p


def main(argv) -> int:
    if "--once" in argv:
        st = fetch_state()
        now = time.time()
        if st:
            rec = snapshot(st, now)
            rec.update(slow_layers(sorted({r.get("symbol") for k in ("trending", "movers", "ai") for r in rec[k]
                                           if r.get("symbol")}), now))
            print(json.dumps({k: rec[k] for k in ("etfs", "halts", "ssr", "short", "short_file")}, default=str)[:3000])
        return 0 if st else 1
    while True:
        now = time.time()
        if in_window(now):
            st = fetch_state()
            if st is not None:
                rec = snapshot(st, now)
                syms = [k for k in rec["catalysts"] if not k.startswith("_")] + [t.get("ticker") for t in rec["tickers"]
                                                                                 if t.get("ticker")]
                syms = sorted(set(syms) | {r.get("symbol") for k in ("trending", "movers", "ai") for r in rec[k] if r.get("symbol")})
                rec.update(slow_layers(syms, now))
                append(rec, now)
            else:
                append({"ts": round(now, 1), "et": datetime.fromtimestamp(now, ET).strftime("%H:%M:%S"),
                        "health": {"fetch_failed": True}}, now)
        else:
            try:
                got = fetch_finra(now)
                if got:
                    print(time.strftime("%H:%M:%S"), "FINRA saved", got, flush=True)
            except Exception as e:                  # noqa: BLE001
                print(time.strftime("%H:%M:%S"), "FINRA fetch failed:", str(e)[:120], flush=True)
        time.sleep(max(1.0, PERIOD - (time.time() - now)))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
