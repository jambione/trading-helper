from typing import Optional
from scanners.base import BaseScanner
from scanner_models import ScannerAlert, ScannerType

SPIKE_FROM_VWAP_PCT = 5.0
CHANGE_PCT_MIN = 8.0
VOLUME_MIN = 200_000
PRICE_MIN = 1.0
PRICE_MAX = 30.0
MIN_VOLUME_MIN = 50_000


class PriceSpikeScanner(BaseScanner):
    scanner_type = ScannerType.PRICE_SPIKE

    def _evaluate(self, ticker: dict, float_cache: dict[str, Optional[float]]) -> Optional[ScannerAlert]:
        day = ticker.get("day", {})
        minute = ticker.get("min", {})
        change_pct = float(ticker.get("todaysChangePerc", 0))
        price = float(day.get("c", 0))
        vwap = float(day.get("vw", 0))
        volume = int(day.get("v", 0))
        min_volume = int(minute.get("v", 0))

        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None
        if change_pct < CHANGE_PCT_MIN:
            return None
        if volume < VOLUME_MIN:
            return None
        if vwap <= 0:
            return None
        if (price - vwap) / vwap * 100 < SPIKE_FROM_VWAP_PCT:
            return None
        if min_volume < MIN_VOLUME_MIN:
            return None

        pct_above_vwap = round((price - vwap) / vwap * 100, 1)
        return self._build_alert(ticker, float_cache, f"{pct_above_vwap}% > vwap")
