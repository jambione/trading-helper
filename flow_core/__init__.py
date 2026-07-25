"""Shared pure-math signal core (depth books, trend, confidence, playbook).

No OCR, no broker SDKs — safe to import from
the dashboard, Mobile Trader bridge, and flow_monitor.
"""
from flow_core.core import (  # noqa: F401
    L2Book,
    LongView,
    Signal,
    SignalEngine,
    WallTracker,
    BookFlow,
    SessionVWAP,
    Trade,
    PaperTrader,
    GlitchGate,
    VWAP_MIN_AGE_DEFAULT,
    market_bias,
    playbook,
    project_price,
    quality_grade,
    signal_quality,
    tape_confirms,
    tape_gate_ok,
    confidence,
    parse_l2_text,
)

__all__ = [
    "L2Book", "LongView", "Signal", "SignalEngine", "WallTracker",
    "BookFlow", "SessionVWAP", "Trade", "PaperTrader", "GlitchGate",
    "VWAP_MIN_AGE_DEFAULT",
    "market_bias", "playbook", "project_price", "quality_grade",
    "signal_quality", "tape_confirms", "tape_gate_ok", "confidence",
    "parse_l2_text",
]
