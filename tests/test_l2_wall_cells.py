"""The banner's ask-wall line and its no-reflow guarantee (webull-l2).

The walls overhead are the resistance the pace has to carry you through,
so the line answers three things at a glance: WHERE the wall is (your
trigger level), HOW BIG it is (what has to get chewed through), and HOW
FAR the move has to go to reach it. Nearest first - that one is the
trade, and it's the one that survives a crop.

Deliberately NOT here: a real/spoof mark on a listed wall. BookFlow only
rules a level CONSUMED or PULLED once it leaves the book, so a standing
wall has no verdict by construction; the verdict lands on the flow note
beside the pillars at the moment the wall goes.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webull-l2"))

pytest.importorskip("cv2", reason="webull-l2 OCR deps not installed")

from rich.text import Text  # noqa: E402

from l2_signal import lv_banner, wall_cells  # noqa: E402


def plain(cells) -> str:
    if not isinstance(cells, str):
        cells = "   ".join(cells)
    return Text.from_markup(cells).plain


def test_a_wall_reports_price_size_and_distance():
    assert plain(wall_cells(8.185, [(8.310, 12400)])) == "8.310 x12.4K +1.5%"


def test_walls_are_listed_nearest_first():
    """The nearest wall is the trigger level - it leads the line, so it's
    what survives when a narrow pane crops the tail."""
    out = plain(wall_cells(8.185, [(8.310, 12400), (8.600, 5100)]))
    assert out.index("8.310") < out.index("8.600")


def test_small_size_stays_a_plain_share_count():
    assert "x900" in plain(wall_cells(8.185, [(8.310, 900)]))


def test_no_walls_no_cells():
    assert wall_cells(8.185, []) == []


# ------------------------------------------------------- banner wiring ------

def render(banner, width=66):
    from rich.console import Console
    c = Console(width=width, legacy_windows=False, force_terminal=False)
    with c.capture() as cap:
        c.print(banner)
    return cap.get()


LV = {"held": 11.0, "agree": 2, "total": 3, "tape_live": True,
      "stance": "LONG", "quality": 0.9, "quality_why": [],
      "votes": {"trend": 0, "tape": 1, "vwap": 1}}


def test_banner_shows_the_walls_with_size_and_distance():
    out = render(lv_banner(LV, 8.185, [(8.310, 12400)], (8.185, 8.215, "")))
    assert "ask walls ↑" in out
    assert "8.310" in out and "x12.4K" in out and "+1.5%" in out


def test_clear_overhead_still_says_so():
    assert "clear overhead" in render(lv_banner(LV, 8.185, [], None))


def lines(banner, width):
    return len([ln for ln in render(banner, width).splitlines() if ln.strip()])


def test_a_long_banner_crops_instead_of_wrapping():
    """Seen live on CLRO/ENGS: a fat wall stack, a spoof flow note and a
    pending turn each pushed their line past the pane, it wrapped, and the
    panel grew to 8 lines - the exact reflow the fixed line count exists
    to stop. Every line must hold its row at any width."""
    loud = dict(LV, quality=0.3, quality_why=["very spoofy book (9 pulled "
                                             "walls)"],
                tape_live=False, pending="NEUTRAL", pending_left=19.0,
                flow_note="ask wall 8.310 PULLED — spoof")
    b = lv_banner(loud, 8.185,
                  [(8.310, 12400), (8.360, 5100), (8.410, 900)],
                  (8.185, 8.215, ""))
    bare = lv_banner(LV, 8.185, None, (8.185, 8.215, ""))
    assert lines(b, 50) == lines(b, 66) == lines(b, 120) == lines(bare, 120)


def test_the_flow_note_still_reports_the_verdict():
    """A wall's real/spoof verdict fires when the wall LEAVES the book, so
    it can't ride on a listed wall - it belongs beside the pillars."""
    lv = dict(LV, flow_note="ask wall 8.310 PULLED — spoof")
    assert "spoof" in render(lv_banner(lv, 8.185, None, None), width=120)
