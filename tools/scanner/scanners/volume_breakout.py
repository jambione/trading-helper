from typing import Optional
from scanners.base import BaseScanner
from scanner_models import ScannerAlert, ScannerType
from scanner_utils import calc_rvol

RVOL_MIN = 3.0
VOLUME_MIN = 300_000
PRICE_MIN = 1.0
PRICE_MAX = 50.0
CHANGE_PCT_MIN = 2.0


class VolumeBreakoutScanner(BaseScanner):
    scanner_type = ScannerType.VOLUME_BREAKOUT

    def _evaluate(self, ticker: dict, float_cache: dict[str, Optional[float]]) -> Optional[ScannerAlert]:
        day = ticker.get("day", {})
        prev = ticker.get("prevDay", {})
        change_pct = float(ticker.get("todaysChangePerc", 0))
        price = float(day.get("c", 0))
        volume = int(day.get("v", 0))
        prev_volume = int(prev.get("v", 0))

        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None
        if volume < VOLUME_MIN:
            return None
        if change_pct < CHANGE_PCT_MIN:
            return None

        rvol = calc_rvol(volume, prev_volume)
        if rvol is None or rvol < RVOL_MIN:
            return None

        return self._build_alert(ticker, float_cache, f"{rvol}x")
