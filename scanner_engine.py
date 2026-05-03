import asyncio
import logging
from typing import Callable, Awaitable, Optional
from scanner_models import ScannerBroadcast, ScannerState, ScannerAlert, ScannerType, SCANNER_LABELS
from scanners import (
    GapScanner,
    MomentumHodScanner,
    VolumeBreakoutScanner,
    PriceSpikeScanner,
    FindItFirstScanner,
    VwapTrendScanner,
)
from scanner_utils import now_iso

logger = logging.getLogger(__name__)

BroadcastFn = Callable[[ScannerBroadcast], Awaitable[None]]


class ScannerEngine:
    def __init__(self, client, poll_interval: int = 60):
        self.client = client
        self.poll_interval = poll_interval
        self.poll_count = 0
        self._scanners = [
            GapScanner(),
            MomentumHodScanner(),
            VolumeBreakoutScanner(),
            PriceSpikeScanner(),
            FindItFirstScanner(),
            VwapTrendScanner(),
        ]
        self._running = False
        self._alert_cache: dict[ScannerType, dict[str, ScannerAlert]] = {}
        self._last_broadcast: Optional[ScannerBroadcast] = None

    async def run(self, broadcast: BroadcastFn) -> None:
        self._running = True
        while self._running:
            try:
                result = await self._poll()
                if self._has_changes(result):
                    self._last_broadcast = result
                    await broadcast(result)
                else:
                    logger.info("Poll #%d — no data changes, skipping broadcast", self.poll_count)
            except Exception as e:
                logger.error("Poll cycle failed: %s", e)
            await asyncio.sleep(self.poll_interval)

    @property
    def is_running(self) -> bool:
        return self._running

    def stop(self) -> None:
        self._running = False

    async def _poll(self) -> ScannerBroadcast:
        self.poll_count += 1
        logger.info("Poll #%d starting", self.poll_count)

        gainers = await self.client.get_gainers()
        losers = await self.client.get_losers()

        seen: set[str] = set()
        universe: list[dict] = []
        for t in gainers + losers:
            sym = t.get("ticker", "")
            if sym and sym not in seen:
                seen.add(sym)
                universe.append(t)

        float_cache = self.client._float_cache
        new_tickers = [t.get("ticker", "") for t in universe if t.get("ticker") not in float_cache]
        if new_tickers:
            asyncio.gather(*[self.client.get_float(sym) for sym in new_tickers])

        scanner_states: list[ScannerState] = []
        for scanner in self._scanners:
            alerts = scanner.scan(universe, float_cache)
            alerts = self._merge_timestamps(alerts, scanner.scanner_type)
            scanner_states.append(
                ScannerState(
                    scanner_type=scanner.scanner_type,
                    label=SCANNER_LABELS[scanner.scanner_type],
                    alerts=alerts,
                )
            )

        logger.info(
            "Poll #%d complete — %d tickers, alerts: %s",
            self.poll_count,
            len(universe),
            {s.label: len(s.alerts) for s in scanner_states},
        )

        return ScannerBroadcast(
            scanners=scanner_states,
            poll_count=self.poll_count,
            timestamp=now_iso(),
        )

    def _merge_timestamps(
        self, alerts: list[ScannerAlert], scanner_type: ScannerType
    ) -> list[ScannerAlert]:
        prev = self._alert_cache.get(scanner_type, {})
        result = []
        for alert in alerts:
            existing = prev.get(alert.ticker)
            if existing and not self._data_changed(alert, existing):
                alert = alert.model_copy(update={"triggered_at": existing.triggered_at})
            result.append(alert)
        self._alert_cache[scanner_type] = {a.ticker: a for a in result}
        return result

    def _data_changed(self, a: ScannerAlert, b: ScannerAlert) -> bool:
        return (
            round(a.price, 2) != round(b.price, 2)
            or round(a.change_pct, 1) != round(b.change_pct, 1)
            or a.volume != b.volume
        )

    def _has_changes(self, new: ScannerBroadcast) -> bool:
        if self._last_broadcast is None:
            return True
        prev_by_type = {s.scanner_type: s for s in self._last_broadcast.scanners}
        for scanner in new.scanners:
            prev = prev_by_type.get(scanner.scanner_type)
            if prev is None:
                return True
            if {a.ticker for a in scanner.alerts} != {a.ticker for a in prev.alerts}:
                return True
            prev_alerts = {a.ticker: a for a in prev.alerts}
            for alert in scanner.alerts:
                pa = prev_alerts.get(alert.ticker)
                if pa and self._data_changed(alert, pa):
                    return True
        return False
