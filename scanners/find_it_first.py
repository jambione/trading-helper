from typing import Optional
from scanners.base import BaseScanner
from scanner_models import ScannerAlert, ScannerType

CHANGE_PCT_MIN = 3.0
VOLUME_MIN = 200_000
PRICE_MIN = 0.50
PRICE_MAX = 20.0
HOD_PROXIMITY = 0.05
FLOAT_MAX_M = 25.0


class FindItFirstScanner(BaseScanner):
    scanner_type = ScannerType.FIND_IT_FIRST

    def _evaluate(self, ticker: dict, float_cache: dict[str, Optional[float]]) -> Optional[ScannerAlert]:
        day = ticker.get("day", {})
        change_pct = float(ticker.get("todaysChangePerc", 0))
        price = float(day.get("c", 0))
        high = float(day.get("h", 0))
        volume = int(day.get("v", 0))
        symbol = ticker.get("ticker", "")
        float_m = float_cache.get(symbol)

        if change_pct < CHANGE_PCT_MIN:
            return None
        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None
        if volume < VOLUME_MIN:
            return None
        if high <= 0 or (high - price) / high > HOD_PROXIMITY:
            return None
        if float_m is not None and float_m > FLOAT_MAX_M:
            return None

        return self._build_alert(ticker, float_cache)
