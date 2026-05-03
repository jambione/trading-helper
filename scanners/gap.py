from typing import Optional
from scanners.base import BaseScanner
from scanner_models import ScannerAlert, ScannerType

GAP_PCT_MIN = 10.0
PRICE_MIN = 1.0
PRICE_MAX = 30.0
VOLUME_MIN = 100_000
FLOAT_MAX_M = 50.0


class GapScanner(BaseScanner):
    scanner_type = ScannerType.GAP

    def _evaluate(self, ticker: dict, float_cache: dict[str, Optional[float]]) -> Optional[ScannerAlert]:
        day = ticker.get("day", {})
        change_pct = float(ticker.get("todaysChangePerc", 0))
        price = float(day.get("c", 0))
        volume = int(day.get("v", 0))
        symbol = ticker.get("ticker", "")
        float_m = float_cache.get(symbol)

        if change_pct < GAP_PCT_MIN:
            return None
        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None
        if volume < VOLUME_MIN:
            return None
        if float_m is not None and float_m > FLOAT_MAX_M:
            return None

        return self._build_alert(ticker, float_cache)
