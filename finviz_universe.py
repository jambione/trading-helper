#!/usr/bin/env python3
"""
finviz_universe.py — optional candidate-list adapter for the RS screener.

OFF BY DEFAULT, and deliberately so. Read this before switching it on.

Everything the Finviz screener would filter on — price, average volume, SMA50,
SMA200, relative volume, quarterly performance — is already computable from the
split-adjusted daily bars rs_screener.py has to pull anyway for the RS math, and
Alpaca's tradable-asset list supplies the universe for free with no scraping.
So this module buys exactly one thing the Alpaca path cannot easily give: a
stocks-only universe (Finviz's `ind_stocksonly`), because Alpaca's asset model
has no ETF flag.

Against that, the costs are real:
  • Finviz's free tier has no CSV export — Elite only — so this is HTML scraping,
    which their terms of service do not permit.
  • They sit behind Cloudflare and rate-limit aggressively. This can start
    returning 403s on any given day with no warning.
  • A screener pre-filter narrows the ranking population, and an RS percentile
    is only meaningful relative to the population it ranked. Using this as the
    universe means "RS 90" stops meaning "top 10% of the market".

Because of the last point especially, prefer `rs_universe_source: "alpaca"` and
let the filters run locally. If you do enable this, the header of
rs_ratings.json still reports `population`, so the narrower meaning stays
visible.

Failure is always soft: every error path returns [] and rs_screener falls back
to the Alpaca universe. A broken scrape must never end a run.
"""

from __future__ import annotations

import logging
import re
import time

log = logging.getLogger("rs.finviz")

BASE_URL = "https://finviz.com/screener.ashx"
ROWS_PER_PAGE = 20

# Finviz filter tokens. These are the raw URL tokens, not the UI labels — the
# eight `finviz_*` keys that used to sit in bot_config.json held labels like
# "Over 500K" that mapped to nothing and were read by no code at all.
DEFAULT_FILTERS = [
    "ind_stocksonly",   # the one thing Alpaca's asset list cannot tell us
    "sh_price_o10",     # price over $10
    "sh_avgvol_o500",   # average volume over 500K
    "ta_sma50_pa",      # price above SMA50
    "ta_perf_13wup",    # positive 13-week performance
]

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Finviz quote links are the reliable anchor for a ticker on the screener page;
# the table markup itself has changed repeatedly. Digits are allowed after the
# first character: US equity tickers are overwhelmingly alphabetic, but a
# character class that silently truncates one to its alphabetic prefix would
# emit a plausible wrong symbol rather than failing.
_QUOTE_HREF = re.compile(r"quote\.ashx\?t=([A-Z][A-Z0-9.\-]{0,6})", re.I)


def build_url(filters: list[str], offset: int = 1, view: str = "111") -> str:
    query = f"v={view}&f={','.join(filters)}" if filters else f"v={view}"
    return f"{BASE_URL}?{query}&r={offset}"


def parse_tickers(html: str) -> list[str]:
    """Tickers from one screener page, in the order Finviz listed them.

    Parsed off the quote links rather than the table cells: Finviz has
    restructured the table markup several times and the href has stayed put.
    BeautifulSoup is used when available for a tidier parse; the regex is the
    fallback and covers both. lxml is not installed, so html.parser it is.
    """
    if not html:
        return []
    seen: dict[str, None] = {}

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.find_all("a", href=True):
            match = _QUOTE_HREF.search(anchor["href"])
            if match:
                seen.setdefault(match.group(1).upper(), None)
    except Exception:                                      # noqa: BLE001
        pass

    if not seen:
        for match in _QUOTE_HREF.finditer(html):
            seen.setdefault(match.group(1).upper(), None)
    return list(seen)


def fetch_universe(cfg: dict | None = None) -> list[str]:
    """Scrape the Finviz screener into a ticker list. [] on any failure."""
    cfg = cfg or {}
    filters = list(cfg.get("rs_finviz_filters") or DEFAULT_FILTERS)
    max_pages = int(cfg.get("rs_finviz_max_pages", 30))
    pause = float(cfg.get("rs_finviz_pause_sec", 1.0))
    timeout = float(cfg.get("rs_finviz_timeout_sec", 12.0))

    try:
        import requests
    except ImportError:
        log.warning("[rs] finviz adapter needs `requests` — not installed")
        return []

    session = requests.Session()
    session.headers.update({"User-Agent": _UA, "Accept": "text/html"})

    out: dict[str, None] = {}
    for page in range(max_pages):
        url = build_url(filters, offset=page * ROWS_PER_PAGE + 1)
        try:
            response = session.get(url, timeout=timeout)
        except Exception as exc:                           # noqa: BLE001
            log.warning("[rs] finviz page %d failed (%s) — stopping", page + 1, exc)
            break
        if response.status_code != 200:
            log.warning("[rs] finviz page %d returned HTTP %d — stopping",
                        page + 1, response.status_code)
            break

        tickers = parse_tickers(response.text)
        fresh = [t for t in tickers if t not in out]
        for ticker in fresh:
            out.setdefault(ticker, None)
        # Finviz repeats the last page forever past the end of the result set,
        # so "no new tickers" is the only reliable terminator.
        if not fresh:
            break
        if pause > 0:
            time.sleep(pause)

    if not out:
        log.warning("[rs] finviz returned no tickers — the caller should fall back")
    else:
        log.info("[rs] finviz universe: %d tickers", len(out))
    return list(out)
