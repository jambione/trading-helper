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
