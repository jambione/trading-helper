from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional


def get_minutes_since_open() -> float:
    now = datetime.now(ZoneInfo("America/New_York"))
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < market_open:
        return 0.0
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    minutes = (min(now, market_close) - market_open).total_seconds() / 60
    return max(float(minutes), 1.0)


def calc_rvol(day_volume: int, prev_day_volume: int) -> Optional[float]:
    if prev_day_volume <= 0:
        return None
    minutes = get_minutes_since_open()
    expected = prev_day_volume * (minutes / 390)
    if expected <= 0:
        return None
    return round(day_volume / expected, 1)


def is_market_hours() -> bool:
    now = datetime.now(ZoneInfo("America/New_York"))
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close


def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"
