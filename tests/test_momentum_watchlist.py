"""Watchlist-movers parsing (momentum-monitor/watchlist_ocr.py).

The line fixtures are transcribed from a real premarket Webull sidebar:
two OCR lines per entry (symbol+price, then company+"Pre: +x.xx%"), with
badge tokens, truncated company names, and chart-overlay noise mixed in.
Parsing must never need the OCR deps — these tests run without cv2.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "momentum-monitor"))

from watchlist_ocr import (Mover, parse_watchlist_lines,  # noqa: E402
                           top_movers)

# the sidebar from the 2026-07-16 04:56 premarket session
SIDEBAR = [
    "PBM 2.890",
    "Psyence Biome... Pre: +1.31%",
    "DSWL 3.440",
    "Deswell Inds Pre: -4.36%",
    "ANGH 3.310",
    "Anghami Pre: -7.55%",
    "ATAI 5.36",
    "Atai Beckley Inc Pre: +47.39%",
    "VSA 3.240",
    "VisionSys Al Inc Pre: +0.93%",
    "EHGO 1.710",
    "Eshallgo Inc Pre: +12.35%",
    "TGHL 0.8145",
    "The GrowHub ... Pre: +31.31%",
    "SNDQ 24 3.130",
    "Tradr 2X Short... Pre: +11.16%",
    "VMAR 1.310",
    "Vision Marine T... Pre: -5.36%",
    "DXST 2.080",
    "Decent Holdin... Pre: +50.00%",
    "ATPC 2.520",
    "Agape Atp Corp Pre: +50.79%",
    "HELP 6.51",
    "Cybin Inc Pre: +13.36%",
    "KUST 1.310",
    "Kustom Entert... Pre: -15.27%",
    "SPCX 24 135.27",
    "Space Explorati... Pre: +0.18%",
]


def test_parses_every_sidebar_entry():
    rows = parse_watchlist_lines(SIDEBAR)
    assert [r.sym for r in rows] == [
        "PBM", "DSWL", "ANGH", "ATAI", "VSA", "EHGO", "TGHL",
        "SNDQ", "VMAR", "DXST", "ATPC", "HELP", "KUST", "SPCX"]
    by = {r.sym: r for r in rows}
    assert by["ATAI"].pct == 47.39 and by["ATAI"].price == 5.36
    assert by["ANGH"].pct == -7.55
    assert by["TGHL"].price == 0.8145
    assert all(r.pre for r in rows)


def test_badge_token_between_symbol_and_price():
    rows = parse_watchlist_lines(["SNDQ 24 3.130", "Tradr Pre: +11.16%"])
    assert rows == [Mover("SNDQ", 11.16, 3.13, True)]


def test_top_movers_are_the_biggest_premarket_gainers():
    top = top_movers(parse_watchlist_lines(SIDEBAR), 3)
    assert [(m.sym, m.pct) for m in top] == [
        ("ATPC", 50.79), ("DXST", 50.0), ("ATAI", 47.39)]


def test_abs_rank_lets_a_dump_outrank_a_pop():
    rows = [Mover("UPPP", 5.0), Mover("DWNN", -8.0), Mover("MEHH", 1.0)]
    assert [m.sym for m in top_movers(rows, 2, rank="abs")] == \
        ["DWNN", "UPPP"]


def test_chart_overlay_noise_produces_nothing():
    """If the region ever leaks into the chart, OHLC/indicator overlays
    must not become movers: too many tokens for a symbol line, and a
    %-line with no symbol before it pairs with nothing."""
    noise = [
        "O 3.070 H 3.140 L 3.060 C 3.120 +0.040 (+1.30%)",
        "VWAP VWAP 2.956",
        "MACD(12,26,9) MACD 0.0909459 Signal 0.0667007",
        "ATR(5) ATR 0.1378",
    ]
    assert parse_watchlist_lines(noise) == []


def test_percent_line_without_symbol_is_dropped_and_resets():
    # a stray %-line, then a real entry: only the real entry survives
    rows = parse_watchlist_lines(
        ["Pre: +9.99%", "PBM 2.890", "Psyence Pre: +1.31%"])
    assert rows == [Mover("PBM", 1.31, 2.89, True)]


def test_duplicate_symbol_keeps_the_first_read():
    rows = parse_watchlist_lines(
        ["PBM 2.890", "x Pre: +1.31%", "PBM 2.890", "x Pre: +2.00%"])
    assert rows == [Mover("PBM", 1.31, 2.89, True)]


def test_garbage_percent_is_rejected():
    assert parse_watchlist_lines(["PBM 2.890", "Pre: +5000%"]) == []


def test_regular_hours_line_without_pre_tag():
    rows = parse_watchlist_lines(["DXST 2.080", "Decent Holdin... +50.00%"])
    assert rows == [Mover("DXST", 50.0, 2.08, False)]
