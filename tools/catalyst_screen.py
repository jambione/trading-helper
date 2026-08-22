#!/usr/bin/env python3
"""Does knowing WHY a name moved select drift, when nothing else did?

Fourteen gates across three horizons all landed at the null, and every one
was a rearrangement of the same indicators on the same tape — RSI, %R, heat,
RVOL, zone, freshness. This is the first genuinely new input: the desk buys
a +50% mover with no idea what caused it, and "no idea" is a plausible
difference between the ones that continue and the ones that fade.

TEM 2026-08-20 is the motivating case. Benzinga published "Why Is Tempus AI
Stock Gaining Thursday?" at 08:35 ET; the desk admitted the name at 09:00
and filled at 09:33, never knowing a published reason existed.

Point-in-time is the whole game here. A headline is only allowed to inform
a sample if it was published STRICTLY BEFORE that instant, because a
catalyst screen that peeks at the afternoon's news is a machine for
inventing edges. Every feature below is computed from headlines with
``created_at < instant`` and nothing else.

Graded the same way as everything else: MFE-MAE for direction, MFE+MAE for
magnitude, session as the unit, desk_null's gates. No cost is charged — a
gate that clears here has earned a cost test, not an arm.

Read-only apart from its own news cache. Usage (mini, venv):

    .venv/bin/python tools/catalyst_screen.py --days 20
    .venv/bin/python tools/catalyst_screen.py --horizons 15,30,60 --refresh
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import drift_screen as DS  # noqa: E402

SCREEN_DIR = Path(ROOT) / "ai_reports" / "screens"
CACHE = Path(ROOT) / "ai_reports" / "news_cache.json"

# Headline shape, not sentiment. These are the words that reliably mean a
# specific corporate action on this kind of name; anything cleverer would be
# a text model whose failures nobody could audit after a losing week.
BEARISH = re.compile(
    r"\b(offering|dilut\w*|pricing of|registered direct|shelf|"
    r"S-1|S-3|ATM|warrant|reverse split|going concern|delist\w*|"
    r"downgrade[sd]?|cuts? (?:price )?target|halt\w*)\b", re.I)
BULLISH = re.compile(
    r"\b(FDA|approval|approved|clearance|beats?|tops?|raises? guidance|"
    r"upgrade[sd]?|contract|award\w*|partnership|acquisition|buyout|"
    r"positive (?:results|data)|phase [23])\b", re.I)


def fetch_news(symbols: list[str], start: datetime, end: datetime,
               refresh: bool) -> dict[str, list[dict]]:
    """symbol -> [{ts, headline}] oldest-first. Cached; the API is the slow part."""
    cache: dict[str, list[dict]] = {}
    if CACHE.exists() and not refresh:
        try:
            cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except ValueError:
            cache = {}
    need = [s for s in symbols if s not in cache]
    if need:
        try:
            from alpaca.data.historical.news import NewsClient
            from alpaca.data.requests import NewsRequest
            from config import load_config
            cfg = load_config()
            client = NewsClient(
                api_key=cfg.get("api_key") or cfg.get("alpaca_api_key"),
                secret_key=cfg.get("secret_key") or cfg.get("alpaca_secret_key"))
        except Exception as e:  # noqa: BLE001
            print(f"  no news client ({type(e).__name__}) — cache only")
            client = None
        if client is not None:
            for i, sym in enumerate(need, 1):
                try:
                    res = client.get_news(NewsRequest(
                        symbols=sym, start=start, end=end, limit=50))
                    items = res.data.get("news", []) if hasattr(res, "data") else []
                    cache[sym] = sorted(
                        ({"ts": n.created_at.timestamp(),
                          "headline": str(n.headline or "")} for n in items),
                        key=lambda r: r["ts"])
                except Exception:
                    cache[sym] = []
                if i % 25 == 0:
                    print(f"  news {i}/{len(need)}", flush=True)
            try:
                CACHE.parent.mkdir(parents=True, exist_ok=True)
                CACHE.write_text(json.dumps(cache), encoding="utf-8")
            except OSError:
                pass
    return {s: cache.get(s) or [] for s in symbols}


def features_at(news: list[dict], instant: float) -> dict:
    """Catalyst features knowable AT *instant*.

    Strictly ``ts < instant``. A headline published one second later is not
    information the desk had, and letting it in is how a screen invents an
    edge that evaporates live.
    """
    prior = [n for n in news if n["ts"] < instant]
    day_start = instant - 24 * 3600
    today = [n for n in prior if n["ts"] >= day_start]
    last = prior[-1] if prior else None
    text = " ".join(n["headline"] for n in today)
    return {
        "has_news_24h": bool(today),
        "n_news_24h": len(today),
        "mins_since": ((instant - last["ts"]) / 60.0) if last else None,
        "bearish": bool(BEARISH.search(text)),
        "bullish": bool(BULLISH.search(text)),
    }


GATES = {
    "all": lambda f: True,
    "has_catalyst_24h": lambda f: f["has_news_24h"],
    "no_catalyst_24h": lambda f: not f["has_news_24h"],
    "fresh_news_60m": lambda f: f["mins_since"] is not None and f["mins_since"] <= 60,
    "fresh_news_15m": lambda f: f["mins_since"] is not None and f["mins_since"] <= 15,
    "stale_news_4h+": lambda f: f["mins_since"] is not None and f["mins_since"] > 240,
    "heavy_news_3+": lambda f: f["n_news_24h"] >= 3,
    "bullish_words": lambda f: f["bullish"] and not f["bearish"],
    "bearish_words": lambda f: f["bearish"],
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--horizons", default="15,30,60")
    ap.add_argument("--source", default="all")
    ap.add_argument("--refresh", action="store_true", help="ignore the news cache")
    ap.add_argument("--limit-symbols", type=int, default=400)
    args = ap.parse_args()
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]

    plan = DS.load_shadow_universe(args.days, args.source)
    if not plan:
        print("no shadow universe")
        return 0
    syms = DS.select_symbols(
        [s for d in plan.values() for s in d], args.limit_symbols)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days + 5)
    print(f"catalyst screen  symbols={len(syms)}  window={start.date()}..{end.date()}")
    news = fetch_news(syms, start, end, args.refresh)
    have = sum(1 for s in syms if news.get(s))
    print(f"  news for {have}/{len(syms)} symbols "
          f"({sum(len(v) for v in news.values())} headlines)")
    bars = DS.fetch_minutes(syms, start, end)
    if not bars:
        print("  no bar client — run on the mini with .venv.")
        return 0
    print(f"  bars {len(bars)}/{len(syms)}\n")

    # One sample per admitted name-day, anchored at the admission instant.
    samples = []
    for day, members in plan.items():
        for sym, admit in members.items():
            if sym not in set(syms) or not admit:
                continue
            b = bars.get(sym)
            if not b:
                continue
            f = features_at(news.get(sym) or [], float(admit))
            path = [x for x in b if x["day"] == day and x["t"] >= float(admit)
                    and DS._in_rth(x)]
            if len(path) < max(horizons):
                continue
            samples.append({"day": day, "f": f, "path": path})
    print(f"  {len(samples)} admissions with a usable path\n")

    hdr = (f"{'gate':<20}{'horiz':>6}{'n':>6}{'sess':>6}{'medMFE':>9}{'medMAE':>9}"
           f"{'MFE/MAE':>9}{'dir':>8}{'sigma':>7}{'mag':>8}{'green':>7}{'verdict':>12}")
    payload = {}
    for hz in horizons:
        print(hdr)
        print("-" * len(hdr))
        for name, fn in GATES.items():
            rows = []
            for s in samples:
                if not fn(s["f"]):
                    continue
                w = s["path"][:hz]
                e = w[0]["o"]
                if e <= 0:
                    continue
                mfe = 100.0 * (max(x["h"] for x in w) - e) / e
                mae = 100.0 * (e - min(x["l"] for x in w)) / e
                rows.append({"day": s["day"], "mfe": mfe, "mae": mae,
                             "net": 100.0 * (w[-1]["c"] - e) / e})
            sc = DS.score(rows)
            payload[f"{name}@{hz}m"] = sc
            if sc["verdict"] == "EMPTY":
                print(f"{name:<20}{hz:>6}{0:>6}   (no samples)")
                continue
            mag = (sc["median_mfe"] + sc["median_mae"])
            print(f"{name:<20}{hz:>6}{sc['n']:>6}{sc['sessions']:>6}"
                  f"{sc['median_mfe']:>9.3f}{sc['median_mae']:>9.3f}"
                  f"{(sc['mfe_over_mae'] or 0):>9.2f}"
                  f"{sc['mean_mfe_minus_mae']:>8.3f}{sc['sigma']:>7.2f}"
                  f"{mag:>8.3f}{sc['sessions_green']}/{sc['sessions']:<4}"
                  f"{sc['verdict']:>12}")
        print()

    print("dir = mean MFE-MAE (direction). mag = median MFE+MAE (range).")
    print("Every feature is computed from headlines published STRICTLY before")
    print("the admission instant. DRIFT is permission to look, not a trade.")
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    (SCREEN_DIR / f"catalyst_{day}.json").write_text(
        json.dumps({"day": day, "horizons": horizons, "results": payload},
                   indent=2, default=str), encoding="utf-8")
    print(f"wrote {SCREEN_DIR / f'catalyst_{day}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
