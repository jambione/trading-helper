"""Cheap ticker shape filters shared by movers + watch seed/admit.

No network. Keep this import-light so ai_entry_watch can reuse the same
levered/ETP rules movers_screener already applies without pulling that
module's env bootstrap.
"""
from __future__ import annotations

from pathlib import Path


def is_common(sym: str) -> bool:
    """True for ordinary common stock.

    A five-letter symbol ending W/U/R/Q is a warrant, unit, right or a name
    in bankruptcy proceedings. They dominate a percent-change ranking because
    they are cheap and thin, and none of them is tradeable here.
    """
    s = str(sym or "").upper()
    if not s.isalpha() or not s or len(s) > 5:
        return False
    return not (len(s) == 5 and s[-1] in "WURQ")


# Single-stock 2x/inverse names that do not carry 2X/3X in the ticker.
# Add a symbol when it shows up on the movers list; keep this small.
# Sibling share classes of a listed name belong here too (MSTU with MSTX)
# so the next product on the same underlying does not need a second incident.
_LEVERED_DENY = frozenset({
    "TSLL", "TSLQ", "TSLZ", "TSLR",   # TSLA 2x / inverse
    "MSTX", "MSTU", "MSTZ", "MST",    # MSTR levered — not MSTR itself
    "CONL", "CONI",                   # COIN 2x
    "HODU", "CRCG", "CSEX",           # 2026-09-03 book
    "NVDL", "NVDX", "NVDU",           # NVDA 2x family
    # Index / sector 3x bull-bear pairs and vol products. Most-actives rows
    # arrive with no issuer name, so the name markers never see them, and a
    # $10-$100 movers band admits them (SOXS 2026-09-23).
    "SOXL", "SOXS", "TQQQ", "SQQQ", "SPXL", "SPXS", "SPXU", "UPRO", "SDS",
    "SSO", "QLD", "QID", "TNA", "TZA", "LABU", "LABD", "FAS", "FAZ",
    "TECL", "TECS", "FNGU", "FNGD", "YINN", "YANG", "NUGT", "DUST",
    "JNUG", "JDST", "GUSH", "DRIP", "ERX", "ERY", "BOIL", "KOLD",
    "UCO", "SCO", "TMF", "TMV", "UDOW", "SDOW", "URTY", "SRTY",
    "SH", "PSQ", "DOG", "UVXY", "UVIX", "SVIX", "SVXY", "VXX", "VIXY",
    "BITX", "BITI", "ETHU", "AMDL", "AAPU", "METU", "GGLL", "AMZU", "MSFU",
    "PTIR", "PLTU", "PLTD",           # PLTR 2x / inverse (PTIR traded 7x on the desk 2026-10-08)
})

# Issuer-name tokens that mean "this is a leveraged / inverse product".
# Bare BULL/BEAR are NOT here: BULL is Webull common stock.
_LEVERED_NAME_MARKERS = (
    " 2X", " 3X", "2X ", "3X ", "-2X", "-3X", "2X-", "3X-",
    "INVERSE", "LEVERED", "LEVERAGED",
    "ULTRASHORT", "ULTRA SHORT", "ULTRAPRO", "ULTRA PRO",
    "BULL 2X", "BEAR 2X", "BULL 3X", "BEAR 3X",
    "DAILY TARGET 2X", "DAILY TARGET 3X",
    "-1X", " -1X",
)


# Issuer names from Alpaca's asset list, saved daily by tight_screener (one request a day). Lets the
# name markers work on paths that only carry a ticker (movers, most-actives): PTIR, "GraniteShares 2x
# Long PLTR Daily ETF", passed the ticker-only check on 2026-10-08. Missing/old file = ticker check only.
ASSET_NAMES_FILE = Path(__file__).resolve().parent / "asset_names.json"
_NAMES: dict = {"mtime": None, "names": {}}


def save_asset_names(names: dict) -> None:
    import json
    import time
    tmp = ASSET_NAMES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"ts": time.time(), "names": names}), encoding="utf-8")
    tmp.replace(ASSET_NAMES_FILE)


def cached_asset_name(sym: str) -> str:
    """Issuer name from asset_names.json ('' when unknown). Reloads on change; never raises."""
    import json
    try:
        mt = ASSET_NAMES_FILE.stat().st_mtime
        if _NAMES["mtime"] != mt:
            _NAMES["names"] = json.loads(ASSET_NAMES_FILE.read_text(encoding="utf-8")).get("names") or {}
            _NAMES["mtime"] = mt
        return str(_NAMES["names"].get(sym) or "")
    except Exception:  # noqa: BLE001
        return ""


def is_levered_etp(sym: str, name: str = "") -> bool:
    """True for inverse / 2x / 3x products the desk cannot trade as stock.

    Alpaca's movers ranking is raw percent-change; single-stock 2x names
    (TSLL, MSTX, CONL) crowd the top the same way warrants do. A price
    band does not remove them. Momentum / trending can surface the same
    names, so watch seed/admit reuse this helper.

    Two cheap tests, no network — same shape as is_common:

      1. Ticker heuristics: 2X/3X in the symbol, CIF* except Cipher
         Mining (CIFR), plus a small denylist of names the heuristics
         miss.
      2. Issuer-name markers when a name is supplied (Direxion Daily
         ... Bull 2X Shares). Bare 'BULL' is not a marker; BULL the
         stock must stay.
    """
    s = str(sym or "").upper().strip()
    n = str(name or cached_asset_name(s) or "").upper()
    if n and any(tag in n for tag in _LEVERED_NAME_MARKERS):
        return True
    if "2X" in s or "3X" in s:
        return True
    if not s.isalpha() or not s:
        return False
    # CIF* levered products, not Cipher Mining (CIFR).
    if s.startswith("CIF") and s != "CIFR":
        return True
    return s in _LEVERED_DENY
