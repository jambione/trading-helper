"""Unit tests for Leg-1 RTH gate (wr_leg1_refusal).

Enforces that $20+ stocks enter on their FIRST move of the day (Leg 1):
  1) Price >= $20.0.
  2) Time <= 11:00 ET.
  3) Session Day Change in [+0.5% .. +5.0%].
  4) Consolidated SIP spread <= 0.08% (8 bp).
  5) Volume ratio >= 1.5x.
  6) Zero prior Empty/Full Squares today in RTH.
"""
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
import ai_entry_watch as ew

ET = ZoneInfo("America/New_York")


def _make_record(symbol="AAPL", price=50.0, pct_change=2.5, vol_ratio=2.0, fast=-25.0, slow=-22.0):
    return {
        "symbol": symbol,
        "price": price,
        "last_ask": price,
        "pct_change": pct_change,
        "admit_pct_change": pct_change,
        "vol_now_ratio": vol_ratio,
        "indicator": {
            "pctr": fast,
            "pctr_slow": slow,
            "pctr_src": "live",
            "pctr_px_src": "live",
        },
    }


def test_gate_disabled_returns_none():
    cfg_off = {"ai_watch_leg1_rth_gate": False}
    rec = _make_record(price=100.0, pct_change=12.0)  # Would be blocked if gate was on
    t = datetime(2026, 10, 27, 10, 0, tzinfo=ET).timestamp()
    assert ew.wr_leg1_refusal("AAPL", rec, t, cfg_off, ask=100.0) is None


def test_cheap_stocks_exempt_from_leg1():
    cfg_on = {"ai_watch_leg1_rth_gate": True, "ai_watch_leg1_min_price": 20.0}
    rec = _make_record(price=12.50, pct_change=8.5)
    t = datetime(2026, 10, 27, 10, 0, tzinfo=ET).timestamp()
    assert ew.wr_leg1_refusal("CHEAP", rec, t, cfg_on, ask=12.50) is None


def test_after_1100_et_refused():
    cfg_on = {"ai_watch_leg1_rth_gate": True, "ai_watch_leg1_max_time": "11:00"}
    rec = _make_record(price=50.0, pct_change=2.5)
    t_after = datetime(2026, 10, 27, 11, 5, tzinfo=ET).timestamp()
    assert ew.wr_leg1_refusal("LATE", rec, t_after, cfg_on, ask=50.0) == "leg1_after_1100"
    assert ew.format_blocker("leg1_after_1100") == "after 11:00 (Leg 1)"


def test_day_chg_extended_refused():
    cfg_on = {"ai_watch_leg1_rth_gate": True, "ai_watch_leg1_max_day_chg": 5.0}
    rec = _make_record(price=50.0, pct_change=6.2)
    t = datetime(2026, 10, 27, 10, 0, tzinfo=ET).timestamp()
    assert ew.wr_leg1_refusal("EXT", rec, t, cfg_on, ask=50.0) == "leg1_extended"
    assert ew.format_blocker("leg1_extended") == "day chg > 5% (Leg 1)"


def test_day_chg_low_refused():
    cfg_on = {"ai_watch_leg1_rth_gate": True, "ai_watch_leg1_min_day_chg": 0.5}
    rec = _make_record(price=50.0, pct_change=0.2)
    t = datetime(2026, 10, 27, 10, 0, tzinfo=ET).timestamp()
    assert ew.wr_leg1_refusal("LOW", rec, t, cfg_on, ask=50.0) == "leg1_chg_low"
    assert ew.format_blocker("leg1_chg_low") == "day chg < 0.5% (Leg 1)"


def test_thin_volume_refused():
    cfg_on = {"ai_watch_leg1_rth_gate": True, "ai_watch_leg1_min_vol_mult": 1.5}
    rec = _make_record(price=50.0, pct_change=2.5, vol_ratio=1.1)
    t = datetime(2026, 10, 27, 10, 0, tzinfo=ET).timestamp()
    assert ew.wr_leg1_refusal("THIN", rec, t, cfg_on, ask=50.0) == "leg1_thin_volume"
    assert ew.format_blocker("leg1_thin_volume") == "thin vol (Leg 1)"


def test_wide_spread_refused(monkeypatch):
    cfg_on = {"ai_watch_leg1_rth_gate": True, "ai_watch_leg1_max_spread_pct": 0.08}
    rec = _make_record(price=50.0, pct_change=2.5, vol_ratio=2.0)
    t = datetime(2026, 10, 27, 10, 0, tzinfo=ET).timestamp()
    monkeypatch.setattr(ew, "sip_spread_pct", lambda s, now=None: 0.12)
    assert ew.wr_leg1_refusal("SPREADY", rec, t, cfg_on, ask=50.0) == "leg1_spread_wide"
    assert ew.format_blocker("leg1_spread_wide") == "spread > 8bp (Leg 1)"


def test_repeat_square_refused(monkeypatch):
    import ob_observe
    sym = "REPEAT"
    cfg_on = {"ai_watch_leg1_rth_gate": True}
    rec = _make_record(symbol=sym, price=50.0, pct_change=2.5, vol_ratio=2.0)
    # Check at 10:15 ET (minute 615). A prior square formed at 09:45 ET.
    t_eval = datetime(2026, 10, 27, 10, 15, tzinfo=ET).timestamp()
    t_open = datetime(2026, 10, 27, 9, 30, tzinfo=ET).timestamp()

    # Build 45 1-minute bars from 09:30 to 10:14 where bars 14..18 (09:44..09:48) formed an Empty Square
    bars = []
    for i in range(45):
        ts = t_open + i * 60
        # Peak at i=15 (09:45)
        px = 50.0 + (5.0 if 13 <= i <= 18 else 0.5) + i * 0.02
        bars.append((ts, px, px + 0.10, px - 0.10, px, 5000.0))

    monkeypatch.setattr(ob_observe, "bars", lambda s: bars if s == sym else [])
    monkeypatch.setattr(ew, "symbol_ohlc", lambda s, c, nw=None: [])
    monkeypatch.setattr(ew, "_cached_ohlc_stamps", lambda s, c, nw=None: None)
    monkeypatch.setattr(ew, "sip_spread_pct", lambda s, now=None: 0.03)

    assert ew.wr_leg1_refusal(sym, rec, t_eval, cfg_on, ask=50.0) == "leg1_repeat_square"
    assert ew.format_blocker("leg1_repeat_square") == "2nd+ square (Leg 1)"


def test_first_leg_allowed(monkeypatch):
    import ob_observe
    sym = "LEG1OK"
    cfg_on = {"ai_watch_leg1_rth_gate": True}
    rec = _make_record(symbol=sym, price=50.0, pct_change=2.5, vol_ratio=2.5)
    # Check at 09:48 ET. First Empty Square is forming right now.
    t_eval = datetime(2026, 10, 27, 9, 48, tzinfo=ET).timestamp()
    t_open = datetime(2026, 10, 27, 9, 30, tzinfo=ET).timestamp()

    # Build 18 bars from 09:30 to 09:47 where price was flat/consolidating until now (no prior square)
    bars = []
    for i in range(18):
        ts = t_open + i * 60
        px = 49.0 + (0.05 if i % 2 == 0 else -0.05) if i < 15 else 49.0 + (i - 14) * 0.35
        bars.append((ts, px, px + 0.05, px - 0.05, px, 2000.0))

    monkeypatch.setattr(ob_observe, "bars", lambda s: bars if s == sym else [])
    monkeypatch.setattr(ew, "symbol_ohlc", lambda s, c, nw=None: [])
    monkeypatch.setattr(ew, "_cached_ohlc_stamps", lambda s, c, nw=None: None)
    monkeypatch.setattr(ew, "sip_spread_pct", lambda s, now=None: 0.03)

    # Allowed!
    assert ew.wr_leg1_refusal(sym, rec, t_eval, cfg_on, ask=50.0) is None


def test_volume_schedule_decay(monkeypatch):
    sym = "DECAY"
    schedule = {
        "10:30": 1.5,
        "11:30": 1.2,
        "13:00": 1.0,
    }
    cfg_sched = {
        "ai_watch_leg1_rth_gate": True,
        "ai_watch_leg1_min_vol_mult": schedule,
        "ai_watch_leg1_max_time": "13:00",
        "ai_watch_leg1_max_day_chg": 6.0,
    }
    monkeypatch.setattr(ew, "sip_spread_pct", lambda s, now=None: 0.03)

    # 1. At 10:15 ET (before 10:30), vol ratio 1.3 is < 1.5 -> Refused thin vol
    t_early = datetime(2026, 10, 27, 10, 15, tzinfo=ET).timestamp()
    rec_13 = _make_record(symbol=sym, price=50.0, pct_change=2.5, vol_ratio=1.3)
    assert ew.wr_leg1_refusal(sym, rec_13, t_early, cfg_sched, ask=50.0) == "leg1_thin_volume"

    # 2. At 11:00 ET (between 10:30 and 11:30), threshold dropped to 1.2.
    #    vol ratio 1.3 is now >= 1.2 -> Allowed!
    t_mid = datetime(2026, 10, 27, 11, 0, tzinfo=ET).timestamp()
    assert ew.wr_leg1_refusal(sym, rec_13, t_mid, cfg_sched, ask=50.0) is None

    # 3. At 12:00 ET (between 11:30 and 13:00), threshold dropped to 1.0.
    #    vol ratio 1.05 is >= 1.0 -> Allowed!
    t_noon = datetime(2026, 10, 27, 12, 0, tzinfo=ET).timestamp()
    rec_105 = _make_record(symbol=sym, price=50.0, pct_change=2.5, vol_ratio=1.05)
    assert ew.wr_leg1_refusal(sym, rec_105, t_noon, cfg_sched, ask=50.0) is None

    #    vol ratio 0.95 is < 1.0 -> Refused thin vol
    rec_095 = _make_record(symbol=sym, price=50.0, pct_change=2.5, vol_ratio=0.95)
    assert ew.wr_leg1_refusal(sym, rec_095, t_noon, cfg_sched, ask=50.0) == "leg1_thin_volume"

    # 4. At 13:05 ET (after 13:00) -> Refused after cutoff
    t_late = datetime(2026, 10, 27, 13, 5, tzinfo=ET).timestamp()
    assert ew.wr_leg1_refusal(sym, rec_13, t_late, cfg_sched, ask=50.0) == "leg1_after_1100"
