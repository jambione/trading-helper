"""Symbol detection and region tracking (webull-l2).

Regressions from the SPCX/OCEA session:

  * Time&Sales stamps its execution VENUE on every print (OCEA, BOSS, ...).
    By raw frequency that column buried the real symbol 7-to-4, so the
    monitor titled itself OCEA and asked the Webull API for depth on an
    exchange code (INVALID_SYMBOL).
  * The calibrated fallback region was absolute screen pixels. Once the
    window moved, those pixels pointed at the Time&Sales widget, and the
    L2 parser OCR'd tape prints forever ("no clean OCR read yet").
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webull-l2"))

pytest.importorskip("cv2", reason="webull-l2 OCR deps not installed")

from l2_signal import (RegionTracker, _guess_symbol, abs_from_rel,  # noqa: E402
                       lv_banner, rel_from_abs)

# The Webull layout from the screenshot: SPCX in four different places,
# the T&S venue column repeating down a single x.
CHART_HEADER = ("SPCX", 945)
QUOTES_PANEL = ("SPCX", 1500)
POSITIONS_ROW = ("SPCX", 1035)
ORDER_ENTRY = ("SPCX", 1580)
VENUE_COL = [("OCEA", 1860), ("BOSS", 1862), ("OCEA", 1859), ("OCEA", 1861),
             ("OCEA", 1860), ("BOSS", 1858), ("OCEA", 1863), ("OCEA", 1860),
             ("OCEA", 1861)]


def test_venue_column_does_not_outvote_the_real_symbol():
    words = [CHART_HEADER, QUOTES_PANEL, POSITIONS_ROW, ORDER_ENTRY] + VENUE_COL
    assert _guess_symbol(words) == "SPCX"


def test_raw_frequency_alone_would_have_picked_the_venue():
    """Guards the premise: OCEA really does out-number SPCX on screen, so a
    Counter-based guess is wrong and this test would fail on the old code."""
    words = [CHART_HEADER, QUOTES_PANEL, POSITIONS_ROW, ORDER_ENTRY] + VENUE_COL
    occurrences = [t for t, _ in words]
    assert occurrences.count("OCEA") > occurrences.count("SPCX")


def test_single_column_repetition_never_wins():
    """Even 50 prints from one venue lose to a symbol in two places."""
    words = [("ARCA", 1860 + (i % 3)) for i in range(50)]
    words += [("TSLA", 900), ("TSLA", 1500)]
    assert _guess_symbol(words) == "TSLA"


def test_symbol_in_one_place_still_detected():
    assert _guess_symbol([("AAPL", 900)]) == "AAPL"


def test_scale_widens_the_column_tolerance():
    """detect_symbol OCRs a 2x image, so column spread doubles with it.
    Four jittery prints read as separate columns at scale 1, but collapse
    into the one table column they really are at scale 2."""
    jittery = [("OCEA", 1860), ("OCEA", 1870), ("OCEA", 1880), ("OCEA", 1890)]
    # scale 1 (tol 20) splits the run into two columns, so it dodges the
    # table rule and outranks NVDA
    assert _guess_symbol(jittery + [("NVDA", 400)], scale=1) == "OCEA"
    # scale 2 (tol 40) sees the single column it really is -> table -> last
    assert _guess_symbol(jittery + [("NVDA", 400)], scale=2) == "NVDA"


def test_stacked_column_loses_to_a_symbol_seen_once():
    """A venue stamped down one column is a table however many prints it
    has, so it must not beat a symbol that appears a single time."""
    venue = [("OCEA", 1860)] * 9
    assert _guess_symbol(venue + [("NVDA", 400)]) == "NVDA"


def test_venue_wins_only_when_nothing_else_is_on_screen():
    """Documents the floor: with no other candidate, the table column is
    still the best guess available - note_symbol hysteresis and the API's
    INVALID_SYMBOL cache are what keep that from doing damage."""
    assert _guess_symbol([("OCEA", 1860)] * 9) == "OCEA"


def test_blocklisted_labels_are_not_tickers():
    assert _guess_symbol([("VWAP", 100), ("VWAP", 500), ("TSLA", 900)]) == "TSLA"


def test_no_candidates():
    assert _guess_symbol([]) is None
    assert _guess_symbol([("Corp", 10), ("137.10", 20)]) is None


# ------------------------------------------------------------- regions ----

WIN = {"left": 5000, "top": 600, "width": 1000, "height": 800}
PANEL = {"left": 5500, "top": 800, "width": 200, "height": 300}


def test_rel_abs_round_trip():
    rel = rel_from_abs(PANEL, WIN)
    assert abs_from_rel(rel, WIN) == PANEL


def test_region_follows_a_moved_window():
    """The whole point: same window, new position -> region moves with it."""
    rel = rel_from_abs(PANEL, WIN)
    moved = dict(WIN, left=100, top=0)
    got = abs_from_rel(rel, moved)
    assert got == {"left": 600, "top": 200, "width": 200, "height": 300}


def test_region_scales_with_a_resized_window():
    rel = rel_from_abs(PANEL, WIN)
    bigger = dict(WIN, width=2000, height=1600)
    got = abs_from_rel(rel, bigger)
    assert got == {"left": 6000, "top": 1000, "width": 400, "height": 600}


def test_region_clamped_inside_the_window():
    rel = {"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5}
    got = abs_from_rel(rel, WIN)
    assert got["left"] + got["width"] <= WIN["left"] + WIN["width"]
    assert got["top"] + got["height"] <= WIN["top"] + WIN["height"]


def test_degenerate_regions_rejected():
    assert abs_from_rel(None, WIN) is None
    assert abs_from_rel({"x": 0, "y": 0, "w": 0.001, "h": 0.001}, WIN) is None
    assert rel_from_abs(PANEL, None) is None
    assert rel_from_abs(PANEL, dict(WIN, width=0)) is None


def tracker(tmp_path, monkeypatch, cfg):
    import l2_signal
    monkeypatch.setattr(l2_signal, "REGION_CACHE", tmp_path / "cache.json")
    return RegionTracker(cfg, console=None)


def test_fallback_prefers_learned_over_calibrated(tmp_path, monkeypatch):
    t = tracker(tmp_path, monkeypatch,
                {"region": {"left": 1, "top": 2, "width": 300, "height": 300}})
    t.manual_rel = rel_from_abs({"left": 5100, "top": 700,
                                 "width": 100, "height": 100}, WIN)
    t.learned_rel = rel_from_abs(PANEL, WIN)
    assert t.fallback(WIN) == PANEL


def test_fallback_uses_legacy_absolute_when_no_fractions(tmp_path, monkeypatch):
    legacy = {"left": 5475, "top": 642, "width": 284, "height": 331}
    t = tracker(tmp_path, monkeypatch, {"region": legacy})
    assert t.fallback(WIN) == legacy


def test_fallback_without_a_window_survives(tmp_path, monkeypatch):
    legacy = {"left": 5475, "top": 642, "width": 284, "height": 331}
    t = tracker(tmp_path, monkeypatch, {"region": legacy})
    t.learned_rel = rel_from_abs(PANEL, WIN)
    assert t.fallback(None) == legacy      # can't project without a rect


def test_learned_anchor_is_cached_and_reloaded(tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    t = tracker(tmp_path, monkeypatch, {})
    rel = rel_from_abs(PANEL, WIN)
    t._save_cache(rel)
    assert json.loads(cache.read_text())["region_rel"] == rel

    t2 = tracker(tmp_path, monkeypatch, {})
    assert t2.learned_rel == rel
    assert t2.fallback(WIN) == PANEL


def test_corrupt_cache_is_ignored(tmp_path, monkeypatch):
    (tmp_path / "cache.json").write_text("{not json")
    t = tracker(tmp_path, monkeypatch, {})
    assert t.learned_rel is None


# ------------------------------------------------- confidence banner --------

def render(banner, width=64):
    """The banner as plain text, at the narrow width the monitor runs in."""
    from rich.console import Console
    c = Console(width=width, legacy_windows=False, force_terminal=False)
    with c.capture() as cap:
        c.print(banner)
    return cap.get()


LV = {"held": 11.0, "agree": 2, "total": 3, "tape_live": True,
      "stance": "LONG", "quality": 0.9, "quality_why": [],
      "votes": {"trend": 0, "tape": 1, "vwap": 1}}


def test_banner_shows_the_trailing_pace_not_a_target():
    """The 5m target was retired: backtested over the logged sessions it
    landed farther from truth than assuming no change in 3 of every 4
    independent calls, direction a coin flip. The pace it was derived from
    is a measured fact, so that is what shows - and no destination price
    the eye can trade toward."""
    out = render(lv_banner(LV, 137.77, None, (137.77, 138.21, "")))
    assert "%/min" in out
    assert "+0.06%/min" in out          # +0.32% over 5m = +0.064%/min
    assert "138.210" not in out         # the target itself is gone


def test_banner_says_building_pace_before_a_pace_exists():
    """project_price needs ~20s of history; the line must hold its place
    with an honest placeholder rather than imply a flat 0.00 pace."""
    out = render(lv_banner(LV, 4.825, None, None))
    assert "building pace" in out
    assert "0.00" not in out


def test_banner_keeps_a_fixed_line_count():
    """Lines that come and go re-flow the panel on every update - the
    exact jitter the layout was built to stop. proj/walls present or
    absent must render the same number of lines."""
    def lines(b):
        return len([ln for ln in render(b).splitlines() if ln.strip()])
    full = lv_banner(LV, 137.77, [(137.9, 1200)], (137.77, 138.21, ""))
    bare = lv_banner(LV, 137.77, None, None)
    assert lines(full) == lines(bare)


def test_banner_lines_do_not_wrap_at_monitor_width():
    """A wrapped line reflows everything below it."""
    out = render(lv_banner(LV, 137.77, [(137.9, 1200), (138.1, 3400)],
                           (137.77, 138.21, "ask wall 137.900 in the way")))
    for ln in out.splitlines():
        assert len(ln) <= 64


def colour_of(banner, needle):
    """The ANSI colour the banner paints `needle` in."""
    from rich.console import Console
    c = Console(width=64, legacy_windows=False, force_terminal=True,
                color_system="truecolor")
    with c.capture() as cap:
        c.print(banner)
    line = [ln for ln in cap.get().split("\n") if needle in ln][0]
    pre = line[:line.index(needle)][-12:]        # the run right before it
    return ("green" if "32m" in pre else
            "red" if "31m" in pre else
            "yellow" if "33m" in pre else "other")


def test_pace_is_not_green_when_the_banner_says_stand_aside():
    """Live: a sub-$1 name paced +9.20%/5m while tape voted DOWN and the
    book showed distribution at the ask - a spike being sold into. Green
    means 'aligned upside' everywhere else here, so a bare pace must not
    get to say it next to STAND ASIDE."""
    aside = dict(LV, agree=1, total=1, stance="NEUTRAL",
                 votes={"trend": None, "tape": -1, "vwap": None})
    b = lv_banner(aside, 0.674, None, (0.674, 0.736, ""))
    assert "+1.84%/min" in render(b)          # the fact is still reported
    assert colour_of(b, "+1.84%/min") == "yellow"     # but unendorsed


def test_pace_keeps_its_colour_when_it_agrees_with_the_stance():
    b = lv_banner(LV, 137.77, None, (137.77, 138.21, ""))   # LONG + up pace
    assert colour_of(b, "+0.06%/min") == "green"
