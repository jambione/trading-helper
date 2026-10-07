#!/usr/bin/env python3
"""Server-side "tight" name source: liquid, tight-spread stocks that are moving.

Publishes tight_stocks.json, which ai_entry_watch's tight seed reads on every
poll. Same producer/consumer shape as movers_screener.py: this process owns
the network calls and the file; the book just reads whatever snapshot is on
disk.

WHY THIS SOURCE
The desk's 0.05% SIP spread cap refuses ~30 of ~50 candidates per check
because movers / trending / momentum / research mostly supply wide-spread
gappers. tools/studies/tight_shadow.py (prereg docs/studies/
tight_shadow_prereg.json) scores this list nightly; on its 10/6 dry run it
gave 102 square opportunities vs 59 in the desk's own names, at 2.6 bp vs
4.4 bp spread. If that holds (gate G5) the operator switches this on as an
A/B. Until then it is OFF and does nothing.

SWITCH
Runs only while ai_watch_seed_tight is true. ./trading launches it only when
the flag is on; while running it re-reads config every pass and goes idle
(one config read a minute, no requests) the moment the flag is turned off.

SELECTION (same rules as tight_shadow.py)
  universe  common stocks (ticker_filters.is_common, not is_levered_etp, not
            overnight_book.FUNDISH on the issuer name) with prior SIP close
            >= ai_tight_min_price and prior-day SIP dollar volume >=
            ai_tight_min_dollar_volume; the ai_tight_universe_n largest by
            that dollar volume. Built once per ET day, cached in
            tight_universe.json.
  scan      every ai_tight_scan_sec, 09:46-16:00 ET: names up >=
            ai_tight_min_pct_change on the day (IEX latest trade, the
            movers_screener snapshot path, vs the prior SIP close) whose SIP
            spread is <= ai_tight_max_spread_pct. The spread is the desk's own
            helper, ai_entry_watch.sip_spread_pct (median over the minute
            ending 16 min ago, the number the 0.05% cap judges). Movers are
            looked up in %-gain order until ai_tight_top are found or
            ai_tight_max_spread_lookups requests are spent.
  rows      then measured from SIP daily bars exactly as movers rows are
            (time-adjusted rvol, today's SIP dollar volume) so the desk's
            rvol and $vol gates see the same statistic they see on movers.

One honest difference from the shadow: the shadow prices the spread off the
last quote at the knowable time (exec_report.nbbo_at); this uses the desk's
one-minute median, because the desk's cap is what the rows must survive.

REQUEST BUDGET (shared Alpaca data keys)
  per scan   1 IEX snapshot (<= 500 symbols, one call)
             <= ai_tight_max_spread_lookups SIP quote calls (default 40),
                paced 0.3 s apart
             1 SIP daily-bar call for the <= ai_tight_top picks
  per day    1 asset list + ~N/200 SIP daily-bar calls for the universe
             (N = common stocks, ~5-6k -> ~30 calls, paced 0.4 s)
  At the defaults that is <= 42 calls per 5 minutes (~8/min averaged).

    python3 tight_screener.py
"""
from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import desk_core
from config import load_config
from ticker_filters import is_common, is_levered_etp

TIGHT_FILE = ROOT / "tight_stocks.json"
UNIVERSE_FILE = ROOT / "tight_universe.json"
SCAN_LEDGER_DIR = ROOT / "ai_reports" / "tight_scan"
ET = ZoneInfo("America/New_York")
IDLE_SEC = 60.0
# A liquid top-400 name prints on IEX every few seconds in RTH; an IEX last
# trade older than this is not "today's price" for the day-change test.
MAX_TRADE_AGE_SEC = 300.0
SPREAD_PACE_SEC = 0.3      # between SIP quote calls (movers paces chunks 0.3 s)
UNIVERSE_PACE_SEC = 0.4    # between universe daily-bar chunks (as the shadow)
UNIVERSE_CHUNK = 200
UNIVERSE_RETRY_SEC = 900.0
_UNIVERSE_TRY: dict[str, float] = {}
_UNIVERSE_MEM: dict[str, tuple[str, list, bool]] = {}

_write_json = desk_core.write_json_atomic


def _et_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(ET)


def _f(cfg: dict, key: str, default: float) -> float:
    try:
        v = cfg.get(key, default)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return float(default)


def _i(cfg: dict, key: str, default: int) -> int:
    try:
        v = cfg.get(key, default)
        return int(default if v is None else v)
    except (TypeError, ValueError):
        return int(default)


def enabled(cfg: dict | None) -> bool:
    """The one switch. False means this process does nothing but wait."""
    return bool((cfg or {}).get("ai_watch_seed_tight", False))


def in_scan_window(now_et: datetime, cfg: dict | None = None) -> bool:
    """09:30 + SIP delay + 1 min through 16:00 ET, weekdays.

    Before that the 16-minute-delayed spread would be a premarket quote, the
    same reason admit_arm_gates sits its spread check out until then.
    """
    if now_et.weekday() >= 5:
        return False
    delay = _f(cfg or {}, "ai_movers_sip_delay_min", 15.0)
    m = now_et.hour * 60 + now_et.minute
    return 570 + delay + 1 <= m < 960


def universe_window(now_et: datetime) -> bool:
    """When the daily universe may be built: weekdays 08:00-16:00 ET, so the
    ~30 bar calls land well before the first scan rather than with it."""
    return now_et.weekday() < 5 and 8 * 60 <= now_et.hour * 60 + now_et.minute < 960


# ── pure selection (tested without a network) ───────────────────────────────

def common_symbols(names: dict[str, str]) -> list[str]:
    """Common stocks only: the movers / allsym_daily_fetch shape filters plus
    the overnight_book FUNDISH issuer-name screen (ETFs, funds, SPACs...)."""
    try:
        from overnight_book import FUNDISH
    except Exception:  # noqa: BLE001
        FUNDISH = None
    out = []
    for s, n in names.items():
        s = str(s or "").upper()
        nm = str(n or "")
        if not s.isalpha() or not is_common(s) or is_levered_etp(s, nm):
            continue
        if FUNDISH is not None and FUNDISH.search(nm.upper()):
            continue
        out.append(s)
    return sorted(out)


def universe_select(prev: dict[str, tuple[float, float]], *, min_price: float,
                    min_dollar_volume: float, top: int) -> list[dict]:
    """{sym: (prior_close, prior_dollar_volume)} -> the top-N liquid names."""
    rows = []
    for s, t in prev.items():
        try:
            c, dv = float(t[0]), float(t[1])
        except (TypeError, ValueError, IndexError):
            continue
        if c >= min_price and dv >= min_dollar_volume:
            rows.append({"symbol": s, "prior_close": c, "prior_dollar_volume": dv})
    rows.sort(key=lambda r: (-r["prior_dollar_volume"], r["symbol"]))
    return rows[:max(0, int(top))]


def movers_select(universe: Iterable[dict], quotes: dict[str, tuple], *,
                  now: float, min_pct: float,
                  max_trade_age: float = MAX_TRADE_AGE_SEC) -> list[dict]:
    """Universe names up >= min_pct, largest % gain first.

    quotes: {sym: (last_trade_price, last_trade_epoch)}. A name with no quote,
    or a quote older than max_trade_age, has no day change and is skipped.
    """
    out = []
    for u in universe:
        s = u.get("symbol")
        q = quotes.get(s)
        pc = u.get("prior_close")
        if not q or not pc:
            continue
        try:
            px, ts = float(q[0]), float(q[1])
            pc = float(pc)
        except (TypeError, ValueError, IndexError):
            continue
        if px <= 0 or pc <= 0 or now - ts > max_trade_age:
            continue
        pct = (px / pc - 1.0) * 100.0
        if pct + 1e-12 < min_pct:
            continue
        out.append({**u, "price": px, "pct_change": pct})
    out.sort(key=lambda r: (-r["pct_change"], r["symbol"]))
    return out


def tight_select(movers: list[dict], spread_fn: Callable[[str], float | None], *,
                 max_spread: float, top: int, max_lookups: int,
                 pace: float = 0.0, sleep=time.sleep) -> tuple[list[dict], int]:
    """Walk movers in order, keep those with a known spread <= max_spread.

    Stops at `top` keeps or `max_lookups` spread calls, whichever is first.
    An unknown spread is not tight. Returns (kept rows, lookups spent).
    """
    kept: list[dict] = []
    spent = 0
    for r in movers:
        if len(kept) >= max(0, top) or spent >= max(0, max_lookups):
            break
        if spent and pace > 0:
            sleep(pace)
        spent += 1
        try:
            sp = spread_fn(r["symbol"])
        except Exception:  # noqa: BLE001
            sp = None
        if sp is None or sp > max_spread + 1e-12:
            continue
        kept.append({**r, "spread_pct": float(sp)})
    return kept, spent


def build_row(pick: dict, *, bars_seq: list | None, today: str, mins_open: float,
              cfg: dict, min_rvol: float) -> dict:
    """One output row, measured from SIP daily bars the way movers rows are."""
    from movers_screener import _bar_day
    seq = list(bars_seq or [])
    vol = None
    avg = None
    if seq and _bar_day(seq[-1]) == today:
        vol = float(getattr(seq[-1], "volume", 0) or 0) or None
        prior = [float(getattr(b, "volume", 0) or 0) for b in seq[:-1]][-20:]
        avg = (sum(prior) / len(prior)) if prior else None
    rvol_raw = (vol / avg) if (vol and avg) else None
    rvol = rvol_raw
    if rvol_raw is not None and bool(cfg.get("rvol_time_adjusted", True)):
        try:
            import tools.morning_funnel as mf
            rvol = mf.rvol_pair(vol, avg, mins_open, time_adjusted=True)[0]
        except Exception:  # noqa: BLE001
            rvol = rvol_raw
    px = float(pick["price"])
    pct = float(pick["pct_change"])
    sp = float(pick["spread_pct"])
    dollar_vol = (vol * px) if vol else None
    crit = ["tight", "uptrend"]
    if rvol is not None and rvol >= min_rvol:
        crit.append("rvol")
    score = round(min(10.0, pct / 5.0), 2)
    return {
        "symbol": pick["symbol"],
        "source": "tight",
        "agreement": True,
        # Both names, as movers rows: the Scan renderer reads trending_score,
        # the book ranks on score.
        "score": score,
        "trending_score": score,
        "reason": f"tight {pct:+.1f}% sp {sp:.3f}%"[:48],
        "price": round(px, 4),
        "pct_change": round(pct, 3),
        "spread_pct": round(sp, 4),
        # TODAY's SIP dollar volume (delayed bar), the number the desk's $vol
        # gate reads on movers rows. None when today's bar is not served yet.
        "dollar_volume": round(dollar_vol) if dollar_vol else None,
        "prior_close": round(float(pick["prior_close"]), 4),
        "prior_dollar_volume": round(float(pick["prior_dollar_volume"])),
        "rvol": rvol,
        "rvol_raw": rvol_raw,
        "avg_vol_20d": round(avg) if avg else None,
        "criteria": crit,
    }


# ── network ─────────────────────────────────────────────────────────────────

def _client():
    """The desk's own data client (config keys, enlarged pool)."""
    import ai_entry_watch as ew
    return ew._data_client()


def _desk_spread(sym: str, now: float) -> float | None:
    """The desk's spread statistic, not recorded as a desk input."""
    import ai_entry_watch as ew
    return ew.sip_spread_pct(sym, now=now, record=False)


def _fetch_asset_names() -> dict[str, str]:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import AssetClass, AssetStatus
    from alpaca.trading.requests import GetAssetsRequest
    cfg = load_config() or {}
    api, sec = cfg.get("api_key"), cfg.get("secret_key")
    ok_ex = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}
    for paper in (True, False):
        try:
            assets = TradingClient(api, sec, paper=paper).get_all_assets(
                GetAssetsRequest(asset_class=AssetClass.US_EQUITY,
                                 status=AssetStatus.ACTIVE))
        except Exception:  # noqa: BLE001
            continue
        return {a.symbol: (a.name or "") for a in assets
                if a.tradable and str(a.exchange).split(".")[-1] in ok_ex}
    return {}


def _fetch_prior_day(syms: list[str], day: str) -> dict[str, tuple[float, float]]:
    """{sym: (prior SIP close, prior SIP dollar volume)} for the session before day."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    cl = _client()
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ET)
    out: dict[str, tuple[float, float]] = {}
    for i in range(0, len(syms), UNIVERSE_CHUNK):
        try:
            r = cl.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=syms[i:i + UNIVERSE_CHUNK], timeframe=TimeFrame.Day,
                start=(d - timedelta(days=7)).astimezone(timezone.utc),
                end=(d - timedelta(seconds=1)).astimezone(timezone.utc),
                feed=DataFeed.SIP, adjustment=Adjustment.RAW))
            data = getattr(r, "data", {}) or {}
        except Exception as e:  # noqa: BLE001
            print(f"[tight] universe chunk failed: {str(e)[:100]}", flush=True)
            data = {}
        for s, rows in data.items():
            rows = [b for b in rows if b.timestamp.astimezone(ET).date() < d.date()]
            if rows:
                c = float(rows[-1].close)
                out[s] = (c, c * float(rows[-1].volume))
        time.sleep(UNIVERSE_PACE_SEC)
    return out


def load_universe(cfg: dict, day: str, *, fetch_names=None, fetch_prev=None,
                  path: Path | None = None) -> list[dict]:
    """Today's universe from the cache file, or built (once) and cached."""
    path = path or UNIVERSE_FILE
    key = str(path)
    mem = _UNIVERSE_MEM.get(key)
    mem_rows = list(mem[1]) if (mem and mem[0] == day and mem[1]) else []
    if mem_rows and mem[2]:
        return mem_rows
    if not mem_rows:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("day") == day and raw.get("rows"):
                _UNIVERSE_MEM[key] = (day, list(raw["rows"]), True)
                return list(raw["rows"])
        except Exception:  # noqa: BLE001
            pass
    # A failed or partial build is not final, so the rebuild is throttled: it
    # is ~30 calls on the shared budget and must not repeat every pass. A
    # partial universe is used meanwhile; a failed one yields nothing.
    if _UNIVERSE_TRY.get(key) and \
            time.time() - _UNIVERSE_TRY[key] < UNIVERSE_RETRY_SEC:
        return mem_rows
    _UNIVERSE_TRY[key] = time.time()
    names = (fetch_names or _fetch_asset_names)()
    syms = common_symbols(names)
    if not syms:
        return mem_rows
    prev = (fetch_prev or _fetch_prior_day)(syms, day)
    rows = universe_select(
        prev, min_price=_f(cfg, "ai_tight_min_price", 10.0),
        min_dollar_volume=_f(cfg, "ai_tight_min_dollar_volume", 50e6),
        top=_i(cfg, "ai_tight_universe_n", 400))
    # A half-failed fetch must not be cached as the day's universe: it is
    # kept in memory as partial and rebuilt after UNIVERSE_RETRY_SEC.
    full = bool(rows) and len(prev) >= 0.5 * len(syms)
    if rows:
        _UNIVERSE_MEM[key] = (day, list(rows), full)
    else:
        return mem_rows
    if full:
        try:
            _write_json(path, {"day": day, "ts": time.time(),
                               "n_common": len(syms), "n_priced": len(prev),
                               "rows": rows})
        except Exception:  # noqa: BLE001
            pass
    print(f"[tight] universe {day}: {len(syms)} common, {len(prev)} priced "
          f"-> {len(rows)} liquid", flush=True)
    return rows


def _fetch_quotes(syms: list[str]) -> dict[str, tuple[float, float]]:
    """IEX snapshot latest trades — the movers_screener scan path."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockSnapshotRequest
    cl = _client()
    out: dict[str, tuple[float, float]] = {}
    for i in range(0, len(syms), 500):
        try:
            got = cl.get_stock_snapshot(StockSnapshotRequest(
                symbol_or_symbols=syms[i:i + 500], feed=DataFeed.IEX))
        except Exception as e:  # noqa: BLE001
            print(f"[tight] snapshot chunk failed: {str(e)[:100]}", flush=True)
            continue
        for s, v in (got or {}).items():
            try:
                out[s] = (float(v.latest_trade.price),
                          v.latest_trade.timestamp.timestamp())
            except Exception:  # noqa: BLE001
                continue
        if i + 500 < len(syms):
            time.sleep(0.3)
    return out


def _fetch_daily_bars(syms: list[str]) -> dict:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    df = _client().get_stock_bars(StockBarsRequest(
        symbol_or_symbols=syms, timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=datetime.now(timezone.utc) - timedelta(days=45), limit=10000,
        feed=DataFeed.SIP))
    return getattr(df, "data", {}) or {}


def scan(cfg: dict, *, now: float | None = None, universe: list[dict] | None = None,
         quotes_fn=None, spread_fn=None, bars_fn=None, sleep=time.sleep) -> list[dict] | None:
    """One pass. None = could not measure (caller keeps the old file)."""
    if not enabled(cfg):
        return None
    t = float(now if now is not None else time.time())
    now_et = datetime.fromtimestamp(t, ET)
    day = now_et.strftime("%Y-%m-%d")
    uni = universe if universe is not None else load_universe(cfg, day)
    if not uni:
        return None
    quotes = (quotes_fn or _fetch_quotes)([u["symbol"] for u in uni])
    if not quotes:
        return None
    movers = movers_select(uni, quotes, now=t,
                           min_pct=_f(cfg, "ai_tight_min_pct_change", 1.0))
    sfn = spread_fn or (lambda s: _desk_spread(s, t))
    picks, spent = tight_select(
        movers, sfn, max_spread=_f(cfg, "ai_tight_max_spread_pct", 0.03),
        top=_i(cfg, "ai_tight_top", 15),
        max_lookups=_i(cfg, "ai_tight_max_spread_lookups", 40),
        pace=SPREAD_PACE_SEC if spread_fn is None else 0.0, sleep=sleep)
    bars: dict = {}
    if picks:
        try:
            bars = (bars_fn or _fetch_daily_bars)([p["symbol"] for p in picks])
        except Exception as e:  # noqa: BLE001
            # Same rule as movers: no bars, no measured row; keep the last file.
            print(f"[tight] daily bars failed, keeping last list: {e}", flush=True)
            return None
    from movers_screener import sip_data_mins_open
    mins_open = sip_data_mins_open(now_et, cfg)
    min_rvol = _f(cfg, "ai_watch_movers_min_rvol", 1.0)
    rows = [build_row(p, bars_seq=bars.get(p["symbol"]), today=day,
                      mins_open=mins_open, cfg=cfg, min_rvol=min_rvol)
            for p in picks]
    try:
        SCAN_LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        with open(SCAN_LEDGER_DIR / f"{day}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": t, "universe": len(uni), "quotes": len(quotes),
                "movers": len(movers), "spread_lookups": spent,
                "picks": [{"symbol": r["symbol"], "pct": r["pct_change"],
                           "spread_pct": r["spread_pct"], "rvol": r["rvol"]}
                          for r in rows]}) + "\n")
    except Exception:  # noqa: BLE001
        pass
    print(f"[tight] {len(uni)} names, {len(movers)} up >= floor, "
          f"{spent} spread lookups -> {len(rows)} tight "
          f"{[r['symbol'] for r in rows]}", flush=True)
    return rows


def main() -> None:
    loaded = desk_core.load_desk_env(ROOT / "signal_engine.env")
    if loaded:
        print(f"[ENV] Loaded {len(loaded)} setting(s) from signal_engine.env",
              flush=True)
    print(f"[tight] producer up -> {TIGHT_FILE.name} "
          "(idle while ai_watch_seed_tight is false)", flush=True)
    while True:
        cfg = load_config() or {}
        if not enabled(cfg):
            time.sleep(IDLE_SEC)
            continue
        now_et = _et_now()
        wait = IDLE_SEC
        try:
            if universe_window(now_et):
                load_universe(cfg, now_et.strftime("%Y-%m-%d"))
            if in_scan_window(now_et, cfg):
                wait = max(30.0, _f(cfg, "ai_tight_scan_sec", 300.0))
                rows = scan(cfg)
                if rows is not None:
                    _write_json(TIGHT_FILE, {
                        "ts": time.time(),
                        "generated_et": _et_now().strftime("%Y-%m-%d %H:%M:%S"),
                        "rows": rows,
                    })
        except Exception as e:  # noqa: BLE001
            print(f"[tight] pass failed: {e}", flush=True)
        time.sleep(wait)


if __name__ == "__main__":
    main()
