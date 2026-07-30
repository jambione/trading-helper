"""Pixel readers against real captured panels.

Every reader bug found on this chart returned None or a wrong number rather
than raising — a panel simply went blank, or a leg quietly read as unlit. None
of it was reachable from the existing suite, which only ever exercised the
screen-free helpers. These tests run the readers over PNGs captured off a live
TradingView window (tests/fixtures/tv/, values in meta.json recorded at capture
time) and pin the specific failures that got through.
"""
import json
import os
import sys

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tv-monitor"))

from tv_core import (  # noqa: E402
    color_mask, line_y, read_check, read_heart, read_star, rightmost_data_x,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "tv")


def _load(name):
    """Fixture as BGRA, matching what mss/Quartz hand the readers."""
    path = os.path.join(FIXTURES, f"{name}.png")
    if not os.path.exists(path):
        pytest.skip(f"missing fixture {name}.png")
    bgr = cv2.imread(path, cv2.IMREAD_COLOR)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)


@pytest.fixture(scope="module")
def meta():
    path = os.path.join(FIXTURES, "meta.json")
    if not os.path.exists(path):
        pytest.skip("missing meta.json")
    with open(path) as fh:
        return json.load(fh)


# ── readers reproduce their captured values ──────────────────────────────────

def test_star_matches_capture(meta):
    got = read_star(_load("star"))
    assert got is not None
    assert got["value"] == pytest.approx(meta["expected"]["star"]["value"], abs=1.5)
    assert 0.0 <= got["value"] <= 100.0


def test_heart_matches_capture(meta):
    got = read_heart(_load("heart"))
    exp = meta["expected"]["heart"]
    assert got is not None
    for key in ("w", "b"):
        if exp.get(key) is None:
            continue
        assert got[key] is not None, f"{key} line went unread"
        assert got[key] == pytest.approx(exp[key], abs=2.0)
        assert -100.0 <= got[key] <= 0.0


def test_check_matches_capture(meta):
    got = read_check(_load("fire"))
    assert got is not None
    assert got["gap"] == pytest.approx(meta["expected"]["fire"]["gap"], abs=1.5)


# ── fail closed, never confidently wrong ─────────────────────────────────────

@pytest.mark.parametrize("reader", [read_star, read_heart, read_check])
def test_blank_panel_reads_none(reader):
    """A black crop is a failed capture. It must not resolve to a value."""
    assert reader(np.zeros((60, 400, 4), dtype=np.uint8)) is None


@pytest.mark.parametrize("reader", [read_star, read_heart, read_check])
def test_noise_panel_reads_none_or_in_range(reader):
    """Random pixels must not yield an out-of-range reading."""
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 60, (60, 400, 4), dtype=np.uint8)   # dark noise
    got = reader(noise)
    if got is None:
        return
    if "value" in got:
        assert 0.0 <= got["value"] <= 100.0
    for key in ("w", "b"):
        if got.get(key) is not None:
            assert -100.0 <= got[key] <= 0.0


# ── regressions for the bugs that shipped ────────────────────────────────────

def _panel(h=60, w=300):
    return np.zeros((h, w, 4), dtype=np.uint8)


def _draw_row(img, y, bgr, x0=0, x1=None):
    img[y, x0:(x1 if x1 is not None else img.shape[1]), :3] = bgr


WHITE = (220, 220, 220)
BLUE = (220, 60, 40)        # BGRA: high B, low R
YELLOW = (60, 200, 200)
GREEN = (60, 200, 60)


def test_flat_line_is_found():
    """A perfectly flat line occupies ONE row.

    line_y used to require a colour to span two distinct rows, so a flat line
    read as no-data — exactly when MACD rests on its average or %R pins to the
    floor, which is when the reading matters most.
    """
    img = _panel()
    _draw_row(img, 30, WHITE)
    assert line_y(img, "white", x_end=img.shape[1] - 1) == pytest.approx(30, abs=1)


def test_single_stray_pixel_is_not_a_line():
    """The flat-line fix must not turn anti-aliasing noise into a reading."""
    img = _panel()
    img[10, 250, :3] = WHITE
    assert line_y(img, "white", x_end=img.shape[1] - 1) is None


def test_heart_reads_both_lines_when_one_ends_early():
    """The %R lines can end at different columns.

    read_heart sampled both at a single shared x_end taken from the longer
    line, so the shorter one returned None on every frame while the other
    looked healthy. Each line is now sampled at its own newest column.
    """
    # Gap sized from the observed case: white ended 82px behind blue on a
    # 788px panel, ~10%. Comfortably inside the staleness guard, which exists
    # to reject a line that stopped rather than one rendering a touch shorter.
    img = _panel(h=100, w=300)
    _draw_row(img, 30, WHITE, 0, 270)      # white stops ~10% early
    _draw_row(img, 70, BLUE, 0, 300)       # blue runs to the edge
    got = read_heart(img)
    assert got is not None
    assert got["w"] is not None, "short line dropped"
    assert got["b"] is not None
    assert got["w"] > got["b"]             # white higher on a 0..-100 scale


def test_heart_rejects_a_line_that_stopped_long_ago():
    """Sampling each line separately must not resurrect stale history."""
    img = _panel(h=100, w=400)
    _draw_row(img, 30, WHITE, 0, 40)       # ancient
    _draw_row(img, 70, BLUE, 0, 400)
    got = read_heart(img)
    assert got is not None
    assert got["w"] is None
    assert got["b"] is not None


def test_check_handles_lines_ending_at_different_columns():
    img = _panel(h=100, w=300)
    _draw_row(img, 20, GREEN, 0, 280)
    _draw_row(img, 60, YELLOW, 0, 300)
    got = read_check(img)
    assert got is not None
    assert got["gap"] > 0                  # signal above its MA -> bullish


def test_colour_mask_channel_order_is_bgra():
    """Quartz hands back B,G,R,A. A swap here silently inverts every reading."""
    img = _panel(h=10, w=10)
    img[:, :, :3] = (0, 0, 255)            # BGRA red
    assert color_mask(img, "red").any()
    assert not color_mask(img, "blue").any()


def test_rightmost_data_x_finds_the_newest_column():
    img = _panel()
    _draw_row(img, 20, WHITE, 0, 173)
    assert rightmost_data_x(img, ("white",)) == 172
    assert rightmost_data_x(img, ("yellow",)) is None


# ── panel location, against a captured window ────────────────────────────────
# Two of the six bugs lived here and both were invisible without a real window:
# locate returned None on every frame because a Unicode minus was dropped, and
# located bounds wandered because a narrow label span got extrapolated to the
# full scale.

def _tesseract_or_skip():
    pytest.importorskip("pytesseract")
    import shutil
    if not shutil.which("tesseract"):
        pytest.skip("tesseract binary not installed")


def test_locate_finds_all_panels(meta):
    _tesseract_or_skip()
    import tv_signal

    tv_signal.load_config()
    win = _load("window")
    rect = {"left": 0, "top": 0,
            "width": win.shape[1], "height": win.shape[0]}
    panels = tv_signal.locate_tv_panels(win, rect)
    assert panels is not None, "locate returned None on a known-good window"
    assert {"star", "heart"} <= set(panels)

    for name, exp in meta["locate"].items():
        if name not in panels:
            continue
        assert panels[name]["top"] == pytest.approx(exp["top"], abs=6)
        assert panels[name]["height"] == pytest.approx(exp["height"], abs=6)


def test_locate_is_deterministic(meta):
    """Same pixels in, same bounds out — the wander was the bug."""
    _tesseract_or_skip()
    import tv_signal

    tv_signal.load_config()
    win = _load("window")
    rect = {"left": 0, "top": 0, "width": win.shape[1], "height": win.shape[0]}
    runs = [tv_signal.locate_tv_panels(win, rect) for _ in range(3)]
    assert all(r is not None for r in runs)
    for name in ("star", "heart"):
        tops = {r[name]["top"] for r in runs}
        heights = {r[name]["height"] for r in runs}
        assert len(tops) == 1 and len(heights) == 1


def test_heart_band_is_below_star_and_similar_height(meta):
    """star runs 100..0 and heart 0..-100 — adjacent, comparable bands.

    Guards the sign recovery: if heart's -100 is misread as +100 it lands in
    star's band and heart collapses or inverts.
    """
    _tesseract_or_skip()
    import tv_signal

    tv_signal.load_config()
    win = _load("window")
    rect = {"left": 0, "top": 0, "width": win.shape[1], "height": win.shape[0]}
    p = tv_signal.locate_tv_panels(win, rect)
    assert p is not None
    assert p["heart"]["top"] >= p["star"]["top"] + p["star"]["height"] - 4
    ratio = p["heart"]["height"] / max(1, p["star"]["height"])
    assert 0.6 <= ratio <= 1.6, f"implausible heart/star height ratio {ratio:.2f}"


def test_leading_minus_detects_sign_and_ignores_digits():
    """TradingView renders U+2212; the digit whitelist drops it, so the sign
    is recovered from the bbox. A digit must never be mistaken for one."""
    import tv_signal

    h, w = 9, 40
    th = np.full((h, w), 255, dtype=np.uint8)     # OTSU output: ink is 0
    th[4, 0:6] = 0                                 # a minus: thin, mid-height
    assert tv_signal._leading_minus(th, 0, 0, w, h)

    digit = np.full((h, w), 255, dtype=np.uint8)
    digit[0:h, 1:4] = 0                            # full-height stroke
    assert not tv_signal._leading_minus(digit, 0, 0, w, h)
