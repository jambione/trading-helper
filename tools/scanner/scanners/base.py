from abc import ABC, abstractmethod
from typing import Optional
from scanner_models import ScannerAlert, ScannerType
from scanner_utils import calc_rvol, now_iso


class BaseScanner(ABC):
    scanner_type: ScannerType

    def scan(self, tickers: list[dict], float_cache: dict[str, Optional[float]]) -> list[ScannerAlert]:
        alerts = []
        for t in tickers:
            alert = self._evaluate(t, float_cache)
            if alert is not None:
                alerts.append(alert)
        return alerts

    @abstractmethod
    def _evaluate(self, ticker: dict, float_cache: dict[str, Optional[float]]) -> Optional[ScannerAlert]:
        ...

    def _build_alert(
        self,
        ticker: dict,
        float_cache: dict[str, Optional[float]],
        note: str = "",
    ) -> ScannerAlert:
        day = ticker.get("day", {})
        prev = ticker.get("prevDay", {})
        symbol = ticker.get("ticker", "")
        price = float(day.get("c", 0))
        prev_close = float(prev.get("c", 0))
        volume = int(day.get("v", 0))
        prev_volume = int(prev.get("v", 0))

        return ScannerAlert(
            ticker=symbol,
            price=round(price, 2),
            change_pct=round(float(ticker.get("todaysChangePerc", 0)), 2),
            volume=volume,
            vwap=round(float(day.get("vw", 0)), 2) if day.get("vw") else None,
            day_high=round(float(day.get("h", 0)), 2),
            day_low=round(float(day.get("l", 0)), 2),
            prev_close=round(prev_close, 2),
            float_millions=float_cache.get(symbol),
            rvol=calc_rvol(volume, prev_volume),
            scanner_type=self.scanner_type,
            triggered_at=now_iso(),
            note=note,
        )
