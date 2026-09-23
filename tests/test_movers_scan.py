"""movers_screener.scan_select: which snapshot rows the full-market scan nominates."""
from movers_screener import scan_select

KW = {"lo": 10.0, "hi": 100.0, "min_pct": 1.5, "min_rvol": 1.0,
      "min_iex_dollars": 500_000, "frac": 0.5, "top": 25}


def test_green_liquid_name_in_band_is_picked():
    # +3%, half the day gone, already 60% of yesterday's volume -> rvol 1.2
    got = scan_select({"DVN": (48.4, 47.0, 60_000, 100_000)}, **KW)
    assert [r["symbol"] for r in got] == ["DVN"]
    assert got[0]["iex_rvol"] == 1.2


def test_filters_price_band_red_thin_and_quiet():
    snaps = {
        "CHEAP": (5.0, 4.0, 500_000, 100_000),     # under $10
        "PRICY": (250.0, 240.0, 50_000, 10_000),   # over $100
        "RED": (40.0, 41.0, 90_000, 100_000),      # down
        "FLAT": (40.0, 39.8, 90_000, 100_000),     # +0.5% < 1.5%
        "QUIET": (40.0, 38.0, 20_000, 100_000),    # rvol 0.4
        "THIN": (40.0, 38.0, 10_000, 5_000),       # $400k IEX < $500k
        "NOPREV": (40.0, 38.0, 90_000, 0),          # no prior volume: no claim
    }
    assert scan_select(snaps, **KW) == []


def test_ranked_by_dollar_volume_and_capped():
    snaps = {f"S{i}": (20.0, 19.0, 100_000 * (i + 1), 100_000) for i in range(5)}
    got = scan_select(snaps, **{**KW, "top": 3})
    assert [r["symbol"] for r in got] == ["S4", "S3", "S2"]


def test_bad_tuple_is_skipped_not_fatal():
    assert scan_select({"X": (None, "a", 1, 2), "Y": (20.0, 19.0, 90_000, 100_000)},
                       **KW)[0]["symbol"] == "Y"


PM = {"now": 10_000.0, "lo": 20.0, "hi": 100.0, "min_gap": 1.5,
      "max_trade_age": 300.0, "min_prev_dollars": 20e6, "top": 25}


def test_premarket_picks_active_liquid_gap_ups_ranked_by_liquidity():
    from movers_screener import premarket_select
    snaps = {
        "BIG": (51.0, 9_900.0, 50.0, 2_000_000),     # +2%, $100M yesterday
        "MID": (41.0, 9_950.0, 40.0, 1_000_000),     # +2.5%, $40M
        "FLAT": (50.2, 9_950.0, 50.0, 2_000_000),    # +0.4% < 1.5
        "STALE": (51.0, 9_000.0, 50.0, 2_000_000),   # last trade 1000s ago
        "THIN": (51.0, 9_950.0, 50.0, 100_000),      # $5M yesterday
        "CHEAP": (12.0, 9_950.0, 11.0, 9_000_000),   # under $20
    }
    got = premarket_select(snaps, **PM)
    assert [r["symbol"] for r in got] == ["BIG", "MID"]
    assert got[0]["pm_gap"] == 2.0


def test_premarket_window():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import movers_screener as ms
    et = ZoneInfo("America/New_York")
    assert ms._in_premarket(datetime(2026, 9, 24, 8, 0, tzinfo=et))
    assert ms._in_premarket(datetime(2026, 9, 24, 9, 29, tzinfo=et))
    assert not ms._in_premarket(datetime(2026, 9, 24, 9, 30, tzinfo=et))
    assert not ms._in_premarket(datetime(2026, 9, 24, 7, 59, tzinfo=et))
    assert not ms._in_premarket(datetime(2026, 9, 26, 8, 30, tzinfo=et))   # Saturday
