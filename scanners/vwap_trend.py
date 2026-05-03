from typing import Optional
from scanners.base import BaseScanner
from scanner_models import ScannerAlert, ScannerType

VOLUME_MIN = 300_000
PRICE_MIN = 1.0
PRICE_MAX = 50.0
CHANGE_PCT_MIN = 1.0
ABOVE_VWAP_PCT_MIN = 1.0


class VwapTrendScanner(BaseScanner):
    scanner_type = ScannerType.VWAP_TREND

    def _evaluate(self, ticker: dict, float_cache: dict[str, Optional[float]]) -> Optional[ScannerAlert]:
        day = ticker.get("day", {})
        change_pct = float(ticker.get("todaysChangePerc", 0))
        price = float(day.get("c", 0))
        vwap = float(day.get("vw", 0))
        volume = int(day.get("v", 0))

        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None
        if change_pct < CHANGE_PCT_MIN:
            return None
        if volume < VOLUME_MIN:
            return None
        if vwap <= 0 or price <= vwap:
            return None
        if (price - vwap) / vwap * 100 < ABOVE_VWAP_PCT_MIN:
            return None

        pct_above = round((price - vwap) / vwap * 100, 1)
        return self._build_alert(ticker, float_cache, f"{vwap:.2f}")
