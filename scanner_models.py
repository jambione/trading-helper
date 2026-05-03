from pydantic import BaseModel
from typing import Optional
from enum import Enum


class ScannerType(str, Enum):
    GAP = "gap"
    MOMENTUM_HOD = "momentum_hod"
    VOLUME_BREAKOUT = "volume_breakout"
    PRICE_SPIKE = "price_spike"
    FIND_IT_FIRST = "find_it_first"
    VWAP_TREND = "vwap_trend"


SCANNER_LABELS: dict[ScannerType, str] = {
    ScannerType.GAP: "Gap Scanner",
    ScannerType.MOMENTUM_HOD: "Momentum + HOD",
    ScannerType.VOLUME_BREAKOUT: "Volume Breakout",
    ScannerType.PRICE_SPIKE: "Price Spike",
    ScannerType.FIND_IT_FIRST: "Find It First",
    ScannerType.VWAP_TREND: "Trending Above VWAP",
}


class ScannerAlert(BaseModel):
    ticker: str
    price: float
    change_pct: float
    volume: int
    vwap: Optional[float] = None
    day_high: float
    day_low: float
    prev_close: float
    float_millions: Optional[float] = None
    rvol: Optional[float] = None
    scanner_type: ScannerType
    triggered_at: str
    note: str = ""


class ScannerState(BaseModel):
    scanner_type: ScannerType
    label: str
    alerts: list[ScannerAlert] = []


class ScannerBroadcast(BaseModel):
    scanners: list[ScannerState]
    poll_count: int
    timestamp: str
