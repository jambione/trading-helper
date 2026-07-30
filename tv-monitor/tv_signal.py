"""TradingView indicator monitor (star / heart / fire, optional check).

Reads the calibrated panel regions ~1x/second by pixel position,
applies your hierarchy (heart = regime, fire = LazyBear squeeze momentum
for strength + context, star = timing; check/MACD fills the strength
slot instead if the layout still has it) and shows one verdict.

Setup: python tv_calibrate.py   (once, with the chart visible)
Run:   python tv_signal.py

The chart must stay VISIBLE on screen - a hidden browser tab can't be read.
"""
from __future__ import annotations

import contextlib
import csv
import io
import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np
import pytesseract
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

import tv_capture_mac
from tv_core import (LeaderTracker, Trail, bullish_score, combine,
                     grid_cells, master_verdict, read_check, read_heart,
                     read_squeeze, read_star, trend5)

# shared session clock (repo root) — optional: monitor runs fine without it
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from session_clock import session_line
except Exception:                                       # noqa: BLE001
    session_line = None

# optional symbol-load workflow (repo root). The monitor runs fine
# without it - non-Windows, or the automation deps aren't installed - in
# which case the 1-4 hotkeys are simply disabled.
try:
    from windows_agent import workflow_add_tv
except Exception:
    workflow_add_tv = None

if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "tv_config.json"

def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    tp = cfg.get("tesseract_path",
                 r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    import os
    if tp and os.path.exists(tp):
        pytesseract.pytesseract.tesseract_cmd = tp
    return cfg


class SqueezeTracker:
    """Adds the one piece of memory raw read_squeeze() readings lack:
    black->gray on the zero line means the squeeze just FIRED - that's
    LazyBear's entry signal."""

    FIRED_SHOW = 180.0   # keep announcing a fire for this many seconds
    ARM_READS = 3        # consecutive 'on' reads before a release counts
    CANCEL_MOM = 0.5     # momentum through zero the other way kills it

    def __init__(self):
        self.state: str | None = None
        self.on_reads = 0
        self.fired_dir: str | None = None
        self.fired_at = 0.0

    def update(self, sq: dict | None, now: float) -> dict | None:
        """read_squeeze() fields + 'fired'/'fired_ago' (for combine)."""
        if sq is None:
            return None
        if sq["squeeze"] == "on":
            if self.state != "on":
                self.fired_dir, self.fired_at = None, 0.0
            self.state = "on"
            self.on_reads += 1
        elif sq["squeeze"] == "off":
            if self.state == "on" and self.on_reads >= self.ARM_READS:
                self.fired_dir = "LONG" if sq["mom"] > 0 else "SHORT"
                self.fired_at = now
            self.state = "off"
            self.on_reads = 0
        # a fired call is only alive while momentum stays on its side
        if ((self.fired_dir == "LONG" and sq["mom"] < -self.CANCEL_MOM)
                or (self.fired_dir == "SHORT"
                    and sq["mom"] > self.CANCEL_MOM)):
            self.fired_dir = None
        out = dict(sq)
        out["fired"] = out["fired_ago"] = None
        if self.fired_dir and now - self.fired_at < self.FIRED_SHOW:
            out["fired"] = self.fired_dir
            out["fired_ago"] = int(now - self.fired_at)
        return out

    @staticmethod
    def label(info: dict | None) -> str | None:
        if info is None:
            return None
        parts = []
        if info["fired"]:
            parts.append(f"FIRED {info['fired']} {info['fired_ago']}s ago")
        elif info["squeeze"] == "on":
            parts.append("squeeze ON - coiling")
        parts.append(f"mom {info['mom']:+.1f}% "
                     + ("building" if info["building"] else "fading"))
        return ", ".join(parts)


# --------------------------------------------- chart-grid separators --------
# TradingView draws thin uniform lines a touch brighter than the chart
# background between chart cells (and at the sidebar edge). Finding those
# lines locates the real cell boundaries no matter the window size or
# position, and lets us drop the watchlist/detail sidebar entirely -
# unlike an equal geometric split, which assumes the charts fill the
# window edge to edge.


def _candle_mask(win_img: np.ndarray) -> np.ndarray:
    """Saturated red/green pixels - candle bodies. The crosshair, axis
    text and the grey watchlist don't register, so this isolates where
    the actual charts are regardless of overlays."""
    b, g, r = (win_img[..., 0].astype(int), win_img[..., 1].astype(int),
               win_img[..., 2].astype(int))
    return (r - np.maximum(g, b) > 40) | (g - np.maximum(r, b) > 40)


def _ridges(gray: np.ndarray, axis: str, bg: float,
            min_fill: float = 0.82, max_w: int = 14) -> list[int]:
    """Centers of thin, bright, nearly-full-span lines (cell dividers,
    sidebar borders, toolbars). Fill-based and width-limited, so a
    transient crosshair crossing a divider doesn't hide it and a broad
    bright region isn't mistaken for one. axis='v' finds vertical lines,
    'h' horizontal."""
    band = (gray > bg + 2) & (gray < bg + 75)
    fill = band.mean(axis=0 if axis == "v" else 1)
    idx = np.where(fill >= min_fill)[0]
    groups: list[list[int]] = []
    for i in idx:
        if groups and i - groups[-1][-1] <= 3:
            groups[-1].append(int(i))
        else:
            groups.append([int(i)])
    return [grp[len(grp) // 2] for grp in groups if len(grp) <= max_w]


def _mid_split(ridges: list[int], lo: int, hi: int,
               tol: float = 0.2) -> int | None:
    """The ridge nearest the midpoint of [lo, hi], accepted only if it
    lands within tol of it - the interior split of an equal 2-way grid.
    Immune to the extra dividers inside each chart, which sit nowhere
    near the halfway line."""
    mid = (lo + hi) / 2
    inside = [x for x in ridges if lo + 40 < x < hi - 40]
    if not inside:
        return None
    best = min(inside, key=lambda x: abs(x - mid))
    return best if abs(best - mid) <= tol * (hi - lo) else None


def detect_chart_grid(win_img: np.ndarray) -> list[dict] | None:
    """Window-relative cell rects, derived live so the grid follows any
    resize/move. The chart area is found from candle density (not the
    window edges), which drops the right-hand watchlist/detail sidebar;
    the row/column splits snap to divider ridges at the grid midpoints.
    Returns None when the chart area or an expected split is unclear -
    the caller then falls back to the equal-split layouts."""
    g = cv2.cvtColor(win_img, cv2.COLOR_BGRA2GRAY).astype(float)
    H, W = g.shape
    bg = float(np.median(g))
    cand = _candle_mask(win_img)
    colc = cand.sum(axis=0).astype(float)
    rowc = cand.sum(axis=1).astype(float)
    if colc.max() < 20 or rowc.max() < 20:
        return None
    xs = np.where(colc > colc.max() * 0.15)[0]
    ys = np.where(rowc > rowc.max() * 0.15)[0]
    if len(xs) < 50 or len(ys) < 50:
        return None
    cl = int(xs.min())
    ct, cb = int(ys.min()), int(ys.max())

    def dens(a, b):
        a, b = max(0, a), min(W, b)
        return float(colc[a:b].mean()) if b > a else 0.0

    # the chart area's right edge: the first vertical ridge with charts on
    # its left but not its right (the sidebar border). Ridges with candles
    # on BOTH sides are interior column splits.
    vrid = _ridges(g, "v", bg)
    floor = colc.max() * 0.05
    right = W
    col_splits = []
    for x in vrid:
        if x <= cl + 60 or x >= W - 5:
            continue
        left_d, right_d = dens(x - 300, x - 20), dens(x + 20, x + 300)
        if left_d > floor and right_d < left_d * 0.2:
            right = x
            break
        if left_d > floor and right_d > left_d * 0.4:
            col_splits.append(x)
    col_splits = [x for x in col_splits if x < right]

    col = _mid_split(col_splits, cl, right)

    # a chart-ROW divider is a full-width ridge sitting in a candle-free
    # band (no price candles crowding it). So are the top/bottom borders
    # and a chart's own oscillator gaps, so we can't pin the split from
    # geometry alone - we hand the candidates (most gap-like first, plus
    # the no-split case) to the caller, which keeps the grid whose cells
    # actually carry the panels. The legend floats just above the price
    # candles, so each row's top backs off by PAD to keep it in frame.
    rc = _candle_mask(win_img)[:, cl:right].sum(axis=1).astype(float)
    sm = np.convolve(rc, np.ones(31) / 31, mode="same")
    content = np.where(sm > 0.12 * sm.max())[0]
    if len(content) < 100:
        return None
    ct2, cb2 = int(content[0]), int(content[-1])
    pad, mid = 150, (ct2 + cb2) / 2
    gapline = 0.2 * float(np.median(sm[ct2:cb2]))

    hrid = _ridges(np.ascontiguousarray(g[:, cl:right]), "h", bg,
                   min_fill=0.9)
    row_cands = sorted(
        (y for y in hrid if ct2 + 120 < y < cb2 - 120
         and sm[max(0, y - 60):y + 60].mean() < gapline),
        key=lambda y: (sm[max(0, y - 60):y + 60].mean(), abs(y - mid)))

    # the top row must start at the PANE top (just below the toolbar), not
    # at the candles: the legend floats at the pane top and the candles can
    # sit far below it when the chart is zoomed so the price action is low
    # (backing off ct2 by PAD then missed the legend entirely -> the cell
    # OCR'd blank candle area). The pane top is the lowest full-width ridge
    # above the charts - full-width (spans the sidebar too) so a chart's own
    # price gridline can't be mistaken for it. Anchoring here also keeps the
    # top clear of the toolbar's symbol-search box. Only when no such ridge
    # is found (unusual theme) do we fall back to the candle-relative guess.
    tb = max([y for y in _ridges(g, "h", bg, min_fill=0.9) if y < 0.14 * H],
             default=0)
    top = tb + 5 if tb else max(0, ct2 - pad)
    bot = min(H - 25, cb2 + 120)
    xcuts = [cl] + ([col] if col else []) + [right]

    def grid(row):
        yc = [top] + ([row] if row else []) + [bot]
        cells = [{"left": xcuts[j], "top": yc[i],
                  "width": xcuts[j + 1] - xcuts[j],
                  "height": yc[i + 1] - yc[i]}
                 for i in range(len(yc) - 1)
                 for j in range(len(xcuts) - 1)]
        return [c for c in cells
                if c["width"] >= 120 and c["height"] >= 120]

    # candidate grids, best-first: split rows (most gap-like divider
    # first), then the no-split fallback
    grids = [grid(r) for r in row_cands[:2]] + [grid(None)]
    return [g for g in grids if g] or None


# ------------------------------------------------------- auto locate --------

_BROWSERS = ("chrome", "brave", "msedge", "firefox", "opera", "vivaldi")


# TradingView chart tabs title like "NVDA 204.12 ▲ +3.65% Unnamed" -
# symbol + price, NOT the word TradingView. Match both.
_TICKER_TITLE = __import__("re").compile(r"^[A-Z]{1,6}\s+\d+\.\d+")


def find_tv_windows() -> list[dict]:
    """All visible browser windows, best-first: titles that look like a
    TradingView chart (ticker+price or the word itself) sort ahead. The
    caller confirms by actually locating the indicator panels."""
    if sys.platform == "darwin":
        # Quartz enumerates windows and captures them by id, so the chart can
        # sit behind the monitor's own terminal and still read correctly.
        return tv_capture_mac.find_windows()
    if sys.platform != "win32":
        return []
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    results: list[tuple] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)
        if not h:
            return True
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h)
        exe = buf.value.rsplit("\\", 1)[-1].lower() if ok else ""
        if not any(b in exe for b in _BROWSERS):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        tbuf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, tbuf, n + 1)
        title = tbuf.value
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, hh = r.right - r.left, r.bottom - r.top
        if w < 600 or hh < 400:
            return True
        score = (0 if "tradingview" in title.lower()
                 else 1 if _TICKER_TITLE.match(title)
                 else 2)
        results.append((score, -w * hh,
                        {"left": r.left, "top": r.top,
                         "width": w, "height": hh}, title))
        return True

    user32.EnumWindows(cb, 0)
    return [(r, t) for _, _, r, t in
            sorted(results, key=lambda x: (x[0], x[1]))]


# chart-furniture words the legend OCR must never mistake for a ticker:
# exchanges, OHLC letters, and the strategy/annotation overlays that sit
# in the same top-left strip as the legend (BUY/SELL boxes, "Enters
# Oversold" tags).
_NOT_TICKERS = {"O", "H", "L", "C", "D", "W", "M", "VOL", "OHLC", "USD",
                "NYSE", "NASDAQ", "CBOE", "ONE", "BATS", "ARCA", "AMEX",
                "OTC", "UTC", "EST", "EDT", "AM", "PM", "ET", "ADD",
                "COMPARE", "UNNAMED", "UNNAME", "BUY", "SELL", "ENTERS",
                "OVERSOLD", "OVERBOUGHT", "INC", "LTD", "CORP", "RTH",
                "ETH", "ADJ", "SAVE", "PUBLISH", "TRADE", "REPLAY", "ALERT",
                "INDICATORS"}

# the legend band reaches down to just above the candles - deeper than a
# single line, since TradingView floats the legend a little below the
# pane top - so the OCR takes the TOP-most ticker token it finds.
_LEGEND_H = 210

# the exchange tag that closes every legend line ('ZBAO · 1 · NASDAQ').
# It's the anchor that tells the real ticker apart from OCR noise: the
# ticker is the left-most token on the SAME line as one of these.
_EXCHANGES = {"NASDAQ", "NYSE", "CBOE", "BATS", "ARCA", "AMEX", "OTC",
              "ARCX"}
_TICKER_RUN = re.compile(r"[A-Za-z]{1,6}")


def _blank_logo_badge(gray: np.ndarray) -> np.ndarray:
    """Zero out the symbol's logo disc at the far-left of the legend.

    Some tickers show a bright white/coloured badge before the text
    (e.g. ZBAO); a dim grey one is harmless, but a bright one biases the
    global OTSU threshold so the whole legend line OCRs to garbage (ZBAO
    read as 'IO'). Detected as bright connected-components confined to
    the left margin, then blanked up to their right edge - data-driven,
    so it never clips the ticker regardless of capture resolution.
    `gray` is pre-inversion (light text/badge on dark), modified in-place.
    """
    w = gray.shape[1]
    bright = (gray > max(150, int(gray.max() * 0.5))).astype(np.uint8)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(bright, 8)
    cut = 0
    for i in range(1, n):
        x, _, bw, _, area = stats[i]
        if area >= 15 and x < 0.06 * w and (x + bw) < 0.17 * w:
            cut = max(cut, x + bw)
    if cut:
        gray[:, :min(cut + 6, int(0.17 * w))] = 0
    return gray


def _line_candidates(th: np.ndarray, psm: int) -> tuple[list, list]:
    """(exchange-anchored, plain) ticker candidates from one OCR pass.

    Each entry is (top_y, ticker) = a legend line's left-most ticker
    token; it's 'exchange-anchored' when an exchange tag ('... NASDAQ')
    shares that line."""
    d = pytesseract.image_to_data(th, config=f"--psm {psm}",
                                  output_type=pytesseract.Output.DICT)
    lines: dict = {}
    for i, word in enumerate(d["text"]):
        if not word.strip():
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines.setdefault(key, []).append((d["left"][i], d["top"][i], word))
    exch_anchored: list = []
    plain: list = []
    for words in lines.values():
        words.sort()
        runs = []
        has_exchange = False
        for x, y, word in words:
            # 'ZCMD·1·NASDAQ' can come back glued as 'ZCMD-1-NASDAQ';
            # split into letter runs and keep the ticker-shaped ones.
            for run in _TICKER_RUN.findall(word):
                tok = run.upper()
                if tok in _EXCHANGES:
                    has_exchange = True
                if 2 <= len(tok) <= 6 and tok not in _NOT_TICKERS:
                    runs.append((x, y, tok))
        if not runs:
            continue
        runs.sort()
        (exch_anchored if has_exchange else plain).append(
            (runs[0][1], runs[0][2]))
    return exch_anchored, plain


def _ocr_symbol(strip: np.ndarray) -> str | None:
    """Ticker read from a legend strip ('ZCMD · 1 · NASDAQ').

    Positional OCR: the interpunct separators must survive (no char
    whitelist) so tesseract splits the legend into words; each word is
    reduced to its letter runs. Selection prefers the left-most ticker
    on the SAME line as the exchange tag - that anchor beats OCR noise
    like a mis-read badge or a 'BUY'/'Enters Oversold' overlay lower in
    the strip. Falls back to the top-most ticker token when no exchange
    tag was legible.

    Two page-segmentation passes are merged: psm 11 (sparse) gives clean
    positions when it fires, but silently returns nothing on some larger
    legend crops (the SELL/BUY boxes throw it off); psm 6 (uniform block)
    reads those as one glued 'TICKER-1-NASDAQ' line and covers the gap."""
    gray = cv2.cvtColor(np.ascontiguousarray(strip), cv2.COLOR_BGRA2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    gray = _blank_logo_badge(gray)
    if np.median(gray) < 127:
        gray = 255 - gray
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    exch_anchored: list = []
    plain: list = []
    for psm in (11, 6):
        ex, pl = _line_candidates(th, psm)
        exch_anchored += ex
        plain += pl
    pool = exch_anchored or plain
    if not pool:
        return None
    pool.sort()                       # top-most wins within the chosen pool
    return pool[0][1]


def detect_slot_symbol(img: np.ndarray) -> str | None:
    """Ticker of one chart cell, read from its legend (top-left strip):
    TradingView titles cells like 'ZCMD · 1 · NASDAQ'. The window title
    can't help here - it only names the ACTIVE chart of a grid."""
    h, w = img.shape[:2]
    if h < 20 or w < 80:
        return None
    return _ocr_symbol(img[:min(_LEGEND_H, h), :int(w * 0.45)])


def legend_rect(rect: dict) -> dict:
    """Screen region of a cell's legend strip, for cheap re-OCR without
    grabbing the whole cell."""
    return {"left": rect["left"], "top": rect["top"],
            "width": max(80, int(rect["width"] * 0.45)),
            "height": min(_LEGEND_H, rect["height"])}


def _is_gridline(v: float) -> bool:
    """A round multiple-of-10 axis label (0, 10, ... 100, -100) - the
    fixed RSI/exhaustion gridlines, as opposed to a live value tag like
    -45.1 or a mis-OCR'd blob like 32730."""
    return abs(v) <= 100.5 and abs(v - round(v / 10) * 10) < 1.0


def _fit_scale(pts: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Least-squares y = a*value + b over (y, value) gridline labels, so
    the full 0..100 (or 0..-100) scale is recovered even when only, say,
    the 90 and 10 lines are labelled. a must be < 0 (screen y grows as
    the value falls); None if degenerate."""
    if len(pts) < 2:
        return None
    ys = np.array([p[0] for p in pts], float)
    vs = np.array([p[1] for p in pts], float)
    # Extrapolating a narrow span to the full 0..100 multiplies every pixel of
    # OCR noise by 100/span. A 5-unit span was a 20x multiplier, which is what
    # made located panel heights wander between frames. Below this, decline and
    # let the caller derive the band from its equal-height sibling — a known
    # geometry beats a wildly levered measurement.
    if vs.max() - vs.min() < 40:
        return None
    a, b = np.polyfit(vs, ys, 1)
    return (float(a), float(b)) if a < -0.05 else None


def _leading_minus(th: np.ndarray, left: int, top: int,
                   width: int, height: int) -> bool:
    """True when the glyph opening this bbox is a minus rather than a digit.

    The axis OCR runs with a digit whitelist, and TradingView renders its
    negatives with U+2212 rather than ASCII '-', so tesseract drops the sign
    but still stretches the bbox over it: '100.00' comes back 39px wide on the
    star axis and 47px on heart's -100. Without this the heart gridline reads
    +100, lands in star's band, and the scale fit sees one value at two rows
    and gives up - which is why the whole panel locate returned None on macOS.

    A minus is ink confined to a thin band at mid-height; every digit spans
    most of the box. Probe only the first few columns: reach as far as the
    next glyph and a tall digit swamps the test.
    """
    if height < 5 or width < 8:
        return False
    band = th[top:top + height, left:left + 6]
    if band.size == 0:
        return False
    rows = np.flatnonzero((band == 0).any(axis=1))
    if rows.size == 0:
        return False
    extent = rows[-1] - rows[0] + 1
    centre = (rows[0] + rows[-1]) / 2.0
    return (extent <= max(2, height // 3)
            and abs(centre - height / 2.0) <= height / 3.0)


def _axis_columns(toks: list) -> list[tuple[int, list]]:
    """Cluster (y, x, value) labels into vertical axis columns, best
    column first (the one with the most oscillator gridlines - that's
    the indicator axis, not the price ladder or a stray watchlist)."""
    cols: list[list] = []
    for y, x, v in sorted(toks, key=lambda t: t[1]):
        for c in cols:
            if abs(c[0] - x) < 60:
                c[1].append((y, v))
                break
        else:
            cols.append([x, [(y, v)]])
    return sorted(((int(c[0]), sorted(c[1])) for c in cols),
                  key=lambda c: -sum(_is_gridline(v) for _, v in c[1]))


def _assign_panels(toks: list) -> dict | None:
    """toks = [(y, value)] of ONE axis column, sorted top->bottom.

    star (0..100) and heart (0..-100) have fixed scales, but TradingView
    thins the gridline labels in a small cell and OCR is noisy (a 100 can
    come back as 400), so either panel's labels may be missing on a given
    frame. We linear-fit whichever panel HAS labels and derive the other
    as the equal-height band adjacent to it. Deriving heart from star (or
    vice-versa) only borrows the pixel HEIGHT; each panel's own labels set
    its position whenever they're legible. Star is the lowest-weight
    indicator, so inferring it costs little; heart/regime stays
    label-calibrated whenever its own labels are present. Returns
    {name: (top_y, bottom_y)} keyed to value 100/0 (star) and 0/-100
    (heart), or None when neither panel can be anchored."""
    # A zero line is 0.00 / 0.0000, not "small". The old 0.6 window was meant
    # to absorb OCR noise, but on a sub-dollar stock the whole PRICE ladder
    # sits under it — GSUN at $0.36 labels 0.31 through 0.39, every one of
    # which registered as a zero. The topmost then anchored star's floor near
    # the top of the chart and the sign rule below flipped star's own 100 to
    # -100, so locate returned None on exactly the low-priced names this desk
    # watches most.
    zeros = sorted(y for y, v in toks if abs(v) < 0.05)

    # Each panel has a scale nothing else on the chart shares: star runs
    # exactly 100..0, heart exactly 0..-100. That is a strong enough prior to
    # repair OCR rather than just filter it. TradingView's "-100.00" comes back
    # as 109, 400 or 100 depending on the frame, and _is_gridline drops all but
    # the last — which then lands in star's band and drags its fit. Two rules,
    # both leaning on the known scales:
    #
    #   1. A magnitude near 100 IS the 100 line; snap it. No legitimate label
    #      sits at 109 on either panel.
    #   2. Sign comes from position, not from glyphs. Star's band ends at the
    #      first zero; anything below that belongs to heart, so its 100 is
    #      -100 whether or not the minus survived OCR.
    first_zero = zeros[0] if zeros else None
    snapped: list[tuple[float, float]] = []
    for y, v in toks:
        if 90.0 <= abs(v) <= 115.0:
            v = 100.0 if v > 0 else -100.0
        # Only the 100 line is ambiguous — it is the one whose minus TradingView
        # renders with a glyph the digit whitelist drops. Flipping any positive
        # gridline below the first zero was too broad: one stray low label
        # anchors the wrong row and takes star's scale with it.
        if (v >= 90.0 and first_zero is not None and y > first_zero):
            v = -v                      # below star's floor => heart's scale
        snapped.append((y, v))
    toks = snapped

    star_gl = [(y, v) for y, v in toks if _is_gridline(v) and 5 <= v <= 100]
    heart_gl = [(y, v) for y, v in toks if _is_gridline(v) and -100 <= v <= -5]

    # star: its gridlines, plus the top-most 0 line (star sits above heart)
    star_pts = star_gl + ([(zeros[0], 0.0)] if zeros else [])
    sfit = _fit_scale(star_pts)
    s_top = s_bot = None
    if sfit:
        a, b = sfit
        s_top, s_bot = a * 100 + b, b
        if s_bot - s_top < 12:
            s_top = s_bot = None

    # heart: its gridlines, plus its OWN 0 line - the zero just above the
    # -100 label, not the fire panel's 0 that sits below it
    h_bot_y = max((y for y, v in heart_gl), default=None)
    h_zero = max((y for y in zeros if h_bot_y is None or y < h_bot_y),
                 default=None)
    heart_pts = heart_gl + ([(h_zero, 0.0)] if h_zero is not None else [])
    hfit = _fit_scale(heart_pts)
    h_top = h_bot = None
    if hfit:
        ha, hb = hfit
        h_top, h_bot = hb, ha * -100 + hb
        if h_bot - h_top < 12:
            h_top = h_bot = None

    if s_top is None and h_top is None:
        return None
    if s_top is None:                           # derive star above heart
        ppu = (h_bot - h_top) / 100.0
        s_bot = h_top
        s_top = s_bot - ppu * 100
    elif h_top is None:                         # derive heart below star
        ppu = (s_bot - s_top) / 100.0
        h_top = s_bot
        h_bot = h_top + ppu * 100
    return {"star": (s_top, s_bot), "heart": (h_top, h_bot)}


def _panel_dividers(gray: np.ndarray, y0: int, y1: int, x0: int,
                    x1: int) -> list[int]:
    """TradingView separates panels with thin uniform horizontal lines
    slightly brighter than the background. Return their row centers."""
    strip = gray[y0:y1, x0:x1].astype(float)
    if strip.size == 0:
        return []
    means = strip.mean(axis=1)
    stds = strip.std(axis=1)
    bg = float(np.median(means))
    mask = (stds < 7) & (means > bg + 6)
    centers, run = [], []
    for i, m in enumerate(mask):
        if m:
            run.append(i)
        elif run:
            centers.append(y0 + run[len(run) // 2])
            run = []
    if run:
        centers.append(y0 + run[len(run) // 2])
    # merge centers closer than 12px
    merged = []
    for c in centers:
        if merged and c - merged[-1] < 12:
            continue
        merged.append(c)
    return merged


def locate_tv_panels(win_img: np.ndarray, rect: dict) -> dict | None:
    """Locate the indicator panels for one chart. The star (0..100) and
    heart (0..-100) bounds come from a linear fit of whatever axis
    gridlines are visible (see _assign_panels), so a compressed cell that
    only labels the 90/10 lines still resolves. A chart that exposes no
    legible oscillator gridline is dropped rather than guessed - a
    mis-scaled regime read is worse than a missing one. The fire (LazyBear
    squeeze) panel sits just below heart and self-scales, so its edges
    come from the panel divider lines. Works with a sidebar to the right
    of the chart."""
    H, W = win_img.shape[:2]
    gray_full = cv2.cvtColor(win_img, cv2.COLOR_BGRA2GRAY)
    x_crop = int(W * 0.5)
    gray = gray_full[:, x_crop:]
    if np.median(gray) < 127:
        gray = 255 - gray
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    d = pytesseract.image_to_data(
        th, config="--psm 11 -c tessedit_char_whitelist=0123456789.-",
        output_type=pytesseract.Output.DICT)
    toks = []
    for i, txt in enumerate(d["text"]):
        txt = txt.strip().rstrip(".")
        if not txt or len(txt) > 9:
            continue
        try:
            v = float(txt)
        except ValueError:
            continue
        # Recover the sign the digit whitelist threw away, or heart's -100
        # masquerades as a star gridline.
        if v > 0 and _leading_minus(th, d["left"][i], d["top"][i],
                                    d["width"][i], d["height"][i]):
            v = -v
        toks.append((d["top"][i] + d["height"][i] / 2,
                     x_crop + d["left"][i], v))

    # pick the axis column with the most oscillator gridlines, then fit
    # its star/heart scales; try the next-best column if a fit fails
    assign = anchor_x = None
    for ax, col in _axis_columns(toks):
        a = _assign_panels(col)
        if a:
            assign, anchor_x = a, ax
            break
    if assign is None:
        return None

    axis_x = int(anchor_x) - 8
    px1 = axis_x - 4
    # wide region: the newest bar can sit far left of the axis when the
    # time scale extends into future/overnight hours
    px0 = max(0, px1 - 1600)
    if px1 - px0 < 60:
        return None

    def region(ty, by):
        return {"left": rect["left"] + px0, "top": rect["top"] + int(ty),
                "width": px1 - px0, "height": int(by - ty)}

    panels = {"star": region(*assign["star"]),
              "heart": region(*assign["heart"])}

    # fire (squeeze) is the panel just below heart and the LAST oscillator
    # in this layout. Its top is the divider at/under heart's bottom; its
    # bottom is the chart floor. We do NOT stop at the next divider: the
    # momentum panel's own gridlines (0 / +/-1.0) and the squeeze zero line
    # read as uniform horizontal lines too, and a replay control bar adds
    # more - snapping to the first of those truncated the panel to a sliver
    # ABOVE the histogram bars, so read_squeeze saw no green/red and
    # returned None. read_squeeze self-scales and keys off the newest bar,
    # so enclosing empty space (time axis, replay bar) below the bars is
    # harmless; clipping the bars is not.
    h_bot = assign["heart"][1]
    divs = _panel_dividers(gray_full, max(0, int(h_bot) - 4), H - 30,
                           px0, px1)
    f_top = min((dv for dv in divs if dv >= h_bot - 2), default=int(h_bot) + 4)
    # Bottom at the NEXT divider, not the chart floor.
    #
    # Running to H-30 was right for the squeeze panel this slot was written
    # for: its histogram sits on a zero line, its own gridlines read as
    # dividers, and clipping the bars was the failure to avoid — so enclosing
    # the time axis below was harmless.
    #
    # It is wrong for MACD, which is what actually occupies this slot. The
    # captured region came back as a thin strip of plot on top of the time
    # axis and the chart toolbar: the lines were clipped at the top, and the
    # gap — reported as a percentage of panel height — was being divided by a
    # height that was mostly dead space. Both the magnitude and, once a line
    # left the crop, the sign.
    below = [dv for dv in divs if dv > f_top + 15]
    if below:
        f_bot = int(min(below))
    else:
        # TradingView draws no divider above the time axis, so the last pane
        # has no bottom edge to find — measured on a live chart, the only
        # divider below heart was this panel's own top. Fall back to the
        # sibling panes' height: the layout stacks equal-height indicator
        # panes, which is the same assumption _assign_panels already makes
        # when it derives one band from the other. Capped at the chart floor
        # so a bad read cannot run off the bottom.
        sib = int(round(((assign["star"][1] - assign["star"][0])
                         + (assign["heart"][1] - assign["heart"][0])) / 2))
        f_bot = min(H - 30, f_top + max(20, sib))
    if f_bot - f_top >= 15:
        panels["fire"] = region(f_top + 3, f_bot - 3)

    return {k: v for k, v in panels.items() if v["height"] >= 15} or None


class Slot:
    """One chart cell: its screen regions plus the per-symbol memory
    (trails, squeeze arming) that was global back when the monitor
    watched a single chart."""

    def __init__(self, idx: int, rect: dict | None, panels: dict,
                 pin: str | None = None):
        self.idx = idx
        self.rect = rect          # cell screen rect (None = manual regions)
        self.panels = panels
        self.pin = pin            # config-pinned symbol, beats OCR
        self.ocr_sym: str | None = None
        self.symbol: str | None = pin
        self.trails = {"star": Trail(), "heart": Trail(), "gap": Trail(),
                       "mom": Trail()}
        self.squeeze = SqueezeTracker()
        # newest reads, kept for render / state / log
        self.star = self.heart = self.check = None
        self.sq: dict | None = None
        self.res: dict | None = None
        self.trend: dict | None = None
        self.score = 0.0

    @property
    def mismatch(self) -> bool:
        """Pinned symbol disagrees with what the legend OCR sees."""
        return bool(self.pin and self.ocr_sym and self.pin != self.ocr_sym)

    def set_symbol(self, sym: str | None):
        """New ticker in this cell -> its history is someone else's."""
        if sym and sym != self.symbol:
            if self.symbol is not None:
                self.trails = {k: Trail() for k in self.trails}
                self.squeeze = SqueezeTracker()
            self.symbol = sym


class TVTracker:
    """Auto-locates chart cells and their panels; falls back to the
    tv_calibrate regions. One TradingView window may carry 1, 2, or 4
    charts - each candidate grid is confirmed by actually finding the
    indicator axis labels inside its cells."""

    MISS_LIMIT = 5
    _LAYOUTS = {"1x1": (1, 1), "1x2": (1, 2), "2x1": (2, 1), "2x2": (2, 2)}
    _AUTO_ORDER = ("2x2", "1x2", "2x1", "1x1")

    def __init__(self, cfg: dict, console=None):
        self.grid = str(cfg.get("grid", "auto"))
        self.manual = self._manual_slots(cfg)
        self.console = console
        self.cached: list[dict] | None = None  # [{rect, panels, symbol}]
        self.win_rect = None
        self.misses = 0
        self.title_symbol: str | None = None
        # re-locate on a slow timer as well, so re-arranging the indicator
        # panels inside a chart (which doesn't change the window rect and
        # may still read plausible-but-wrong values) is picked up within
        # one interval. 0 disables it. This is the ONLY periodic cost; it
        # runs inside the monitor loop, so it stops when the monitor does.
        self.relocate_interval = float(cfg.get("relocate_interval", 60.0))
        self._last_locate = 0.0

    @staticmethod
    def _manual_slots(cfg: dict) -> list[dict] | None:
        sp = cfg.get("slots_panels") or {}
        if sp:
            return [{"rect": None, "panels": p, "symbol": None}
                    for _, p in sorted(sp.items())]
        if cfg.get("panels"):
            return [{"rect": None, "panels": cfg["panels"], "symbol": None}]
        return None

    def report(self, ok: bool):
        self.misses = 0 if ok else self.misses + 1

    @staticmethod
    def _need(n: int) -> int:
        """How many cells must carry the panel fingerprint to accept a
        layout: all of a 1-chart window, else ceil(0.75n) (one cell may
        be an empty/odd chart)."""
        return 1 if n == 1 else max(2, -(-n * 3 // 4))

    def _probe(self, win_img: np.ndarray, rect: dict,
               cells: list[dict]) -> list[dict]:
        slots = []
        for cell in cells:
            sub = np.ascontiguousarray(
                win_img[cell["top"]:cell["top"] + cell["height"],
                        cell["left"]:cell["left"] + cell["width"]])
            sub_rect = {"left": rect["left"] + cell["left"],
                        "top": rect["top"] + cell["top"],
                        "width": cell["width"], "height": cell["height"]}
            panels = locate_tv_panels(sub, sub_rect)
            if panels:
                slots.append({"rect": sub_rect, "panels": panels,
                              "symbol": detect_slot_symbol(sub)})
        return slots

    def _locate_slots(self, win_img: np.ndarray, rect: dict) -> list[dict]:
        """Locate the chart cells. In auto mode the live separator lines
        come first (they track any resize/move and drop the right-hand
        sidebar); if that doesn't pan out we fall back to equal-split
        layouts, most charts first."""
        if self.grid == "auto":
            for cells in detect_chart_grid(win_img) or []:
                slots = self._probe(win_img, rect, cells)
                if len(slots) >= self._need(len(cells)):
                    return slots
        H, W = win_img.shape[:2]
        layouts = ([self.grid] if self.grid in self._LAYOUTS
                   else self._AUTO_ORDER)
        for name in layouts:
            rows, cols = self._LAYOUTS[name]
            slots = self._probe(win_img, rect, grid_cells(W, H, rows, cols))
            if len(slots) >= self._need(rows * cols):
                return slots
        return []

    def get(self, sct) -> list[dict] | None:
        cands = find_tv_windows()
        if not cands:
            return self.cached or self.manual
        # the active ticker leads the chart tab's title ("NVDA 204.12 ...")
        for _, title in cands:
            m = _TICKER_TITLE.match(title)
            if m:
                self.title_symbol = m.group(0).split()[0]
                break
        now = time.time()
        # a slow timer forces a re-locate even when the window rect is
        # unchanged and reads still "succeed" (see relocate_interval); the
        # rect-mismatch / MISS_LIMIT triggers keep re-trying every poll.
        stale = (self.relocate_interval > 0
                 and now - self._last_locate >= self.relocate_interval)
        if (self.cached and self.win_rect in [r for r, _ in cands]
                and self.misses < self.MISS_LIMIT and not stale):
            return self.cached
        # confirm candidates by actually finding the panel axis labels -
        # that fingerprint only exists on the TradingView chart
        for rect, _ in cands[:3]:
            img = np.asarray(sct.grab(rect))
            slots = self._locate_slots(img, rect)
            if slots:
                if self.console and (self.cached is None
                                     or len(slots) != len(self.cached)):
                    self.console.print(
                        f"[dim]TV grid located: {len(slots)} chart(s)[/dim]")
                self.cached, self.win_rect = slots, rect
                self.misses = 0
                self._last_locate = now
                return self.cached
        # keep the last good grid on a failed (re)locate; stamp the timer
        # so a timed refresh doesn't re-run the OCR every single poll
        self.misses = 0
        self._last_locate = now
        return self.cached or self.manual


L2_STATE = HERE.parent / "l2_state.json"
TV_STATE = HERE.parent / "tv_state.json"

TREND_WIN = 300.0    # the "5m" trend lookback
TREND_MIN = 240.0    # history needed before a trail may vote


def drift5(tr: Trail) -> float | None:
    return tr.slope(TREND_WIN) if tr.span() >= TREND_MIN else None


def read_l2_state() -> dict | None:
    """Latest state published by the flow monitor (if running)."""
    try:
        d = json.loads(L2_STATE.read_text())
        if time.time() - d.get("ts", 0) > 10:
            return None   # stale - L2 monitor stopped or losing reads
        return d
    except Exception:
        return None


def _sym_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return True   # unknown symbol -> don't block
    a, b = a.upper(), b.upper()
    return a == b or a.startswith(b) or b.startswith(a)


def fmt(v, spec=".0f", none="—"):
    return format(v, spec) if v is not None else none


def banner(res, master: dict | None = None) -> Panel:
    """The one thing you see from across the room. The MASTER verdict
    (chart + order flow) takes over when the L2 monitor is running."""
    if master:
        v = master["verdict"]
        if v == "EXECUTE BUY":
            txt, style, border = "▲ ▲ ▲   E X E C U T E   B U Y   ▲ ▲ ▲", \
                "bold black on green", "green"
        elif v == "EXECUTE SELL":
            txt, style, border = "▼ ▼ ▼   E X E C U T E   S E L L   ▼ ▼ ▼", \
                "bold white on red", "red"
        elif v == "EXIT NOW":
            txt, style, border = "▼ ▼ ▼   E X I T   N O W   ▼ ▼ ▼", \
                "bold white on red", "red"
        elif v.startswith("TAKE PROFIT"):
            txt, style, border = "$ $ $   T A K E   P R O F I T   $ $ $", \
                "bold black on gold1", "gold1"
        elif v.startswith("TIGHTEN"):
            txt, style, border = f"◆  {v} — protect the position", \
                "bold black on dark_orange", "dark_orange"
        elif v.startswith("HOLD"):
            txt, style, border = f"◈  {v}", \
                "bold black on green", "green"
        elif v.startswith("CONFLICT"):
            txt, style, border = "◆  CONFLICT — chart vs tape — HOLD", \
                "bold black on dark_orange", "dark_orange"
        elif v.startswith("BUY SETUP"):
            txt, style, border = "◉  BUY SETUP — waiting for order flow", \
                "bold black on cyan", "cyan"
        elif v.startswith("SELL"):
            txt, style, border = "◉  SELL LEANING — flow not confirming", \
                "bold white on grey35", "red"
        else:
            txt, style, border = "—   W A I T   —", \
                "bold yellow on grey23", "yellow"
        return Panel(Align.center(f"[{style}]  {txt}  [/]"),
                     border_style=border, padding=(1, 1))
    if not res:
        txt, style, border = "SCANNING …", "bold white on grey30", "grey50"
    else:
        v = res["verdict"]
        if v == "STRONG BUY":
            txt, style, border = "▲ ▲ ▲   S T R O N G   B U Y   ▲ ▲ ▲", \
                "bold black on green", "green"
        elif "BUY" in v:
            txt, style, border = ("▲ ▲   B U Y   ▲ ▲" if v == "BUY"
                                  else "▲  lean buy  ▲"), \
                "bold black on green", "green"
        elif "SELL" in v:
            txt, style, border = "▼ ▼ ▼   S E L L   ▼ ▼ ▼", \
                "bold white on red", "red"
        elif v.startswith("WATCH"):
            txt, style, border = "◉  GET READY — exhaustion zone", \
                "bold black on cyan", "cyan"
        else:
            txt, style, border = "—   W A I T   —", \
                "bold yellow on grey23", "yellow"
    return Panel(Align.center(f"[{style}]  {txt}  [/]"),
                 border_style=border, padding=(1, 1))


def squeeze_text(sq: dict | None) -> str:
    """Color-coded squeeze reading: direction by color, conviction by
    brightness, badges for the coil / fire states."""
    if not sq:
        return "[dim]no read[/dim]"
    mc = "green" if sq["mom"] > 0 else "red" if sq["mom"] < 0 else "yellow"
    if sq["building"]:
        mom = f"mom [bold {mc}]{sq['mom']:+.1f}%[/bold {mc}] " \
              f"[{mc}]building[/{mc}]"
    else:
        mom = f"mom [dim {mc}]{sq['mom']:+.1f}% fading[/dim {mc}]"
    if sq["fired"]:
        fc = "green" if sq["fired"] == "LONG" else "red"
        tc = "black" if fc == "green" else "white"
        return (f"[bold {tc} on {fc}] FIRED {sq['fired']} [/] "
                f"[dim]{sq['fired_ago']}s ago[/dim]  {mom}")
    if sq["squeeze"] == "on":
        return f"[bold black on yellow] SQUEEZE ON [/] coiling  {mom}"
    return mom


def leader_strip(lead: str | None, score: float | None,
                 l2_sym: str | None) -> Panel:
    """Which chart deserves focus right now."""
    if not lead:
        return Panel(Align.center(
            "[dim]no bullish leader on the grid[/dim]"),
            border_style="grey50", padding=(0, 1))
    txt = f"[bold cyan]🎯 LEADER  {lead}[/bold cyan]"
    if score is not None:
        txt += f"  [green]{score:+.1f}[/green]"
    if l2_sym and not _sym_match(l2_sym, lead):
        txt += (f"    [bold black on yellow]  SWITCH TV → {lead}  [/]"
                f"  [dim]L2 is on {l2_sym}[/dim]")
    elif l2_sym:
        txt += "    [dim]tv on target[/dim]"
    return Panel(Align.center(txt), border_style="cyan", padding=(0, 1))


def slots_table(slots: list[Slot], lead: str | None,
                l2_sym: str | None) -> Table:
    """One row per chart cell - the grid at a glance."""
    t = Table(expand=False)
    t.add_column("Slot"), t.add_column("Symbol"), t.add_column("Verdict")
    t.add_column("5m"), t.add_column("Score", justify="right")
    t.add_column("")
    for s in slots:
        v = s.res["verdict"] if s.res else "—"
        c = ("green" if "BUY" in v else "red" if "SELL" in v
             else "cyan" if v.startswith("WATCH") else "yellow")
        tr = s.trend["dir"] if s.trend else None
        ta = {"up": "[green]▲ up[/green]", "down": "[red]▼ down[/red]",
              "flat": "[yellow]→ flat[/yellow]"}.get(tr, "[dim]…[/dim]")
        tags = []
        if s.symbol and lead and _sym_match(s.symbol, lead):
            tags.append("🎯")
        if s.symbol and l2_sym and _sym_match(l2_sym, s.symbol):
            tags.append("⚡L2")
        if s.mismatch:
            tags.append(f"[red]pin {s.pin} ≠ ocr {s.ocr_sym}[/red]")
        t.add_row(str(s.idx), f"[bold cyan]{s.symbol or '?'}[/bold cyan]",
                  f"[{c}]{v}[/{c}]", ta, f"{s.score:+.1f}", " ".join(tags))
    return t


def detail_table(s: Slot | None, hz: float, misses: int,
                 l2: dict | None = None, master: dict | None = None,
                 l2_note: str | None = None,
                 symbol: str | None = None) -> Table:
    """Per-indicator readout for the focus slot (the chart the
    window is on, else the leader)."""
    star = s.star if s else None
    heart = s.heart if s else None
    check = s.check if s else None
    sq = s.sq if s else None
    res = s.res if s else None
    trend = s.trend if s else None
    symbol = (s.symbol if s else None) or symbol
    t = Table(title=f"TradingView Monitor [bold cyan]{symbol or '?'}"
                    f"[/bold cyan]   {hz:.1f} reads/s   miss:{misses}",
              expand=False)
    t.add_column("Indicator"), t.add_column("Reading", justify="left")

    if session_line:
        t.add_row("⏱ Session", session_line())
    if master:
        mc = ("green" if "BUY" in master["verdict"]
              else "red" if "SELL" in master["verdict"]
              else "dark_orange" if "CONFLICT" in master["verdict"]
              else "yellow")
        t.add_row("MASTER (chart+flow)",
                  f"[bold {mc}]{master['verdict']}[/bold {mc}]  "
                  f"[dim]{master['why']}[/dim]")
    if l2:
        lc = ("green" if l2.get("bias", 0) >= 25
              else "red" if l2.get("bias", 0) <= -25 else "yellow")
        px = l2.get("price")
        # the L2 monitor no longer publishes a projected target ("proj"):
        # it graded worse than assuming no change at all. The trailing
        # pace (%/min) is a measured fact, so that is what's shown.
        pace = l2.get("pace_pct_min")
        pxs = ""
        if px and pace is not None:
            pc = ("green" if pace > 0.01
                  else "red" if pace < -0.01 else "yellow")
            pxs = f"  px {px:.3f} [{pc}]{pace:+.2f}%/min[/{pc}]"
        elif px:
            pxs = f"  px {px:.3f}"
        t.add_row("⚡ Order flow (L2)",
                  f"[{lc}]bias {l2.get('bias', 0):+.0f}[/{lc}]  "
                  f"imb {l2.get('imbalance', '—')}  "
                  f"play {l2.get('play', '—')}{pxs}  "
                  f"[cyan]{l2.get('symbol') or '?'}[/cyan]"
                  + (f"  [bold red]{l2_note}[/bold red]" if l2_note
                     else ""))
    else:
        t.add_row("⚡ Order flow (L2)",
                  "[dim]L2 monitor not running / stale[/dim]")
    if res:
        v = res["verdict"]
        c = ("green" if "BUY" in v
             else "red" if "SELL" in v
             else "cyan" if v.startswith("WATCH") else "yellow")
        t.add_row("VERDICT (chart)", f"[bold {c}]{v}[/bold {c}]")
        t.add_row("", f"[dim]{'; '.join(res['reasons'])}[/dim]")

    if star:
        sc = {"red": "red", "green": "green"}.get(star["color"], "white")
        t.add_row("★ RSI (timing)",
                  f"[{sc}]{star['value']:.0f}[/{sc}] "
                  f"[dim](rising=buy, falling=sell; superseded by ♥)[/dim]")
    else:
        t.add_row("★ RSI", "[dim]no read[/dim]")
    if heart:
        hc = {"red": "green", "blue": "red"}.get(heart["shade"], "yellow")
        t.add_row("♥ Exhaustion (regime)",
                  f"w {fmt(heart['w'])}  b {fmt(heart['b'])}  "
                  f"shade [{hc}]{heart['shade']}[/{hc}] "
                  f"[dim](-100 = exhausted/buy-zone)[/dim]")
    else:
        t.add_row("♥ Exhaustion", "[dim]no read[/dim]")
    if check:   # only on layouts that still carry a MACD panel
        gc = "green" if check["gap"] > 0 else "red"
        t.add_row("✓ MACD (strength)",
                  f"[{gc}]gap {check['gap']:+.1f}%[/{gc}] "
                  f"[dim](signal vs MA; wider = stronger)[/dim]")
    t.add_row("🔥 Squeeze (strength+context)", squeeze_text(sq))
    if trend:
        tc = {"up": "green", "down": "red"}.get(trend["dir"], "yellow")
        arrow = {"up": "▲", "down": "▼"}.get(trend["dir"], "→")
        det = "  ".join(f"{k} {v:+.1f}" for k, v in
                        (("heart", trend["heart"]), ("mom", trend["mom"]),
                         ("rsi", trend["star"])) if v is not None)
        t.add_row("📈 Trend (5m)",
                  f"[bold {tc}]{arrow} {trend['dir'].upper()}"
                  f"[/bold {tc}]  [dim]{det}[/dim]")
    else:
        t.add_row("📈 Trend (5m)", "[dim]warming up (needs ~5 min "
                                   "on this symbol)[/dim]")
    return t


def open_log():
    """tv_log.csv, now with a symbol column; a pre-grid file (old header)
    is rotated aside rather than mixed."""
    path = HERE / "tv_log.csv"
    try:
        if path.exists():
            head = path.open().readline()
            if head and "symbol" not in head:
                path.replace(HERE / "tv_log.old.csv")
    except OSError:
        pass
    f = open(path, "a", newline="")
    w = csv.writer(f)
    if f.tell() == 0:
        w.writerow(["time", "symbol", "star", "star_color", "heart_w",
                    "heart_b", "shade", "gap", "fire", "verdict"])
    return f, w


def _beep():
    if sys.platform == "win32":
        try:
            import winsound
            winsound.Beep(880, 180)
        except Exception:
            pass


def sync_slots(slots: list[Slot], found: list[dict],
               pins: dict[str, str]) -> list[Slot]:
    """Bind Slot state to the located cells. A changed cell count means
    the layout changed - start fresh; otherwise keep each slot's trails
    and just refresh its regions."""
    if len(slots) != len(found):
        slots = []
        for i, f in enumerate(found):
            s = Slot(i + 1, f.get("rect"), f["panels"],
                     pin=pins.get(str(i + 1)))
            s.ocr_sym = f.get("symbol")
            slots.append(s)
    else:
        for s, f in zip(slots, found):
            s.rect, s.panels = f.get("rect"), f["panels"]
            # locate-time OCR is consumed once; the periodic legend
            # re-read owns ocr_sym afterwards
            seed = f.pop("symbol", None)
            if seed:
                s.ocr_sym = seed
    return slots


def refresh_slot_symbols(slots: list[Slot], sct):
    """Re-OCR each cell's legend strip (slow cadence, off the hot path)."""
    for s in slots:
        if s.rect is None:
            continue
        try:
            sym = _ocr_symbol(np.asarray(sct.grab(legend_rect(s.rect))))
        except Exception:
            continue
        if sym:
            s.ocr_sym = sym


def read_slot(s: Slot, sct, t0: float) -> bool:
    """Grab this cell's panels and refresh its verdict/trend/score.
    Returns True when anything was readable."""
    star = heart = check = sq_info = None
    p = s.panels
    try:
        if "star" in p:
            star = read_star(np.asarray(sct.grab(p["star"])))
        if "heart" in p:
            heart = read_heart(np.asarray(sct.grab(p["heart"])))
        if "check" in p:
            check = read_check(np.asarray(sct.grab(p["check"])))
        if "fire" in p:
            sq_info = s.squeeze.update(
                read_squeeze(np.asarray(sct.grab(p["fire"]))), t0)
    except Exception:
        pass

    if star:
        s.trails["star"].add(star["value"], t0)
    if heart:
        hv = heart["w"] if heart["w"] is not None else heart["b"]
        s.trails["heart"].add(hv, t0)
    if check:
        s.trails["gap"].add(check["gap"], t0)
    if sq_info:
        s.trails["mom"].add(sq_info["mom"], t0)

    s.trend = trend5(drift5(s.trails["heart"]), drift5(s.trails["mom"]),
                     drift5(s.trails["star"]))
    s.res = combine(star, heart, check,
                    s.trails["star"].slope(10),
                    s.trails["heart"].slope(30),
                    s.trails["gap"].slope(15),
                    sq_info, s.trend)
    s.star, s.heart, s.check, s.sq = star, heart, check, sq_info
    s.score = bullish_score(s.res["verdict"], s.trend, s.sq)
    return bool(star or heart or check or sq_info)


class LoadHotkey:
    """Press a slot number (1-9) to load that chart's symbol into TradingView,
    or SPACE to load the current grid leader, via the existing
    windows_agent workflow. A daemon thread reads the keys - so the render
    loop never blocks - and runs the send there too (it steals focus for
    ~2s and prints, which we swallow so the Rich Live display stays clean).
    Windows-only; a no-op if the workflow import failed, in which case the
    on-screen hint is hidden."""

    SPACE = " "

    def __init__(self):
        self.enabled = sys.platform == "win32" and workflow_add_tv is not None
        self._by_key: dict[str, str] = {}
        self._leader: str | None = None
        self._status = ""
        self._lock = threading.Lock()
        if self.enabled:
            threading.Thread(target=self._reader, daemon=True).start()

    def update(self, slots: list["Slot"]):
        """Refresh the key -> symbol map from the current grid each poll."""
        with self._lock:
            self._by_key = {str(s.idx): s.symbol for s in slots
                            if s.symbol and 1 <= s.idx <= 9}

    def set_leader(self, sym: str | None):
        """The symbol SPACE will send (the current elected leader)."""
        with self._lock:
            self._leader = sym

    def status(self) -> str:
        with self._lock:
            return self._status

    def _set(self, msg: str):
        with self._lock:
            self._status = f"{datetime.now():%H:%M:%S}  {msg}"

    def _reader(self):
        try:
            import msvcrt
        except Exception:                                # noqa: BLE001
            return
        while True:
            try:
                ch = msvcrt.getwch()
            except Exception:                            # noqa: BLE001
                return                                   # no real console
            with self._lock:
                sym = (self._leader if ch == self.SPACE
                       else self._by_key.get(ch))
            if not sym:
                if ch == self.SPACE:
                    self._set("no leader to send")
                continue
            where = "leader" if ch == self.SPACE else "chart"
            self._set(f"sending {where} {sym} to TradingView ...")
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    ok = workflow_add_tv(sym)
                self._set(f"TradingView now on {sym}" if ok
                          else f"TradingView send FAILED for {sym}")
            except Exception as e:                       # noqa: BLE001
                self._set(f"TradingView send error: {e}")


def main():
    cfg = load_config()
    interval = cfg.get("poll_interval", 1.0)
    sym_refresh = float(cfg.get("symbol_refresh", 20.0))
    pins = {str(k): str(v).upper()
            for k, v in (cfg.get("pins") or {}).items()}
    beep_on = bool(cfg.get("leader_beep", True))
    console = Console()
    tracker = TVTracker(cfg, console)
    leader = LeaderTracker(float(cfg.get("leader_margin", 1.5)),
                           int(cfg.get("leader_confirm_reads", 5)))
    hotkeys = LoadHotkey()

    slots: list[Slot] = []
    logf, logw = open_log()
    misses = 0
    stamps: list[float] = []
    last_sym = 0.0
    prev_lead: str | None = None

    console.print("[bold]TradingView monitor - Ctrl+C to stop.[/bold] "
                  "Keep the chart(s) visible on screen.")
    # macOS reads the window by id through Quartz (works when the chart is
    # covered); everywhere else mss grabs the screen rect. WindowCapture
    # duck-types mss.grab(), so nothing below this line cares which it got.
    capture_backend = (tv_capture_mac.WindowCapture()
                       if tv_capture_mac.AVAILABLE else mss.mss())
    with capture_backend as sct, Live(console=console,
                                      refresh_per_second=2) as live:
        while True:
            t0 = time.time()
            found = tracker.get(sct) or []
            if not found:
                live.update(Group(
                    banner(None, None),
                    detail_table(None, 0.0, misses,
                                 symbol=tracker.title_symbol)))
                time.sleep(2.0)
                continue
            slots = sync_slots(slots, found, pins)
            single = len(slots) == 1
            if t0 - last_sym >= sym_refresh:
                last_sym = t0
                refresh_slot_symbols(slots, sct)
            for s in slots:
                # a single chart's window title is authoritative and beats
                # a whole-window OCR that can catch a browser tab ("YouTube");
                # in a grid the title only names the active cell, so OCR leads
                s.set_symbol(s.pin
                             or (tracker.title_symbol if single else None)
                             or s.ocr_sym)
            hotkeys.update(slots)

            ok_any = False
            for s in slots:
                ok_any = read_slot(s, sct, t0) or ok_any
            tracker.report(ok_any)
            if not ok_any:
                misses += 1

            # leader election: who deserves focus
            scores = {s.symbol: s.score
                      for s in slots if s.symbol and s.res}
            lead = leader.update(scores)
            hotkeys.set_leader(lead)
            if beep_on and lead and lead != prev_lead:
                _beep()
            prev_lead = lead

            # bridge: the slot the focused chart is on gets the MASTER
            # verdict (entry + position/exit logic); anywhere else the
            # leader strip tells you where to point it
            l2 = read_l2_state()
            l2_sym = l2.get("symbol") if l2 else None
            l2_slot = (next((s for s in slots if s.symbol
                             and _sym_match(l2_sym, s.symbol)), None)
                       if l2_sym else None)
            lead_slot = (next((s for s in slots if s.symbol
                               and _sym_match(s.symbol, lead)), None)
                         if lead else None)
            focus = l2_slot or lead_slot or (slots[0] if single else None)
            master = (master_verdict(l2_slot.res["verdict"], l2)
                      if l2_slot and l2_slot.res else None)
            l2_note = (f"L2 on {l2_sym or '?'} — not in grid"
                       if l2 and not l2_slot else None)

            symbols_out = {}
            for s in slots:
                if s.symbol and s.res:
                    symbols_out[s.symbol] = {
                        "slot": s.idx, "verdict": s.res["verdict"],
                        "score": round(s.score, 2),
                        "trend": s.trend["dir"] if s.trend else None}
            try:
                TV_STATE.write_text(json.dumps(
                    {"ts": t0, "leader": lead, "l2_symbol": l2_sym,
                     "symbols": symbols_out,
                     # back-compat: the focus slot mirrors the old
                     # single-symbol fields
                     "symbol": focus.symbol if focus else lead,
                     "verdict": (focus.res["verdict"]
                                 if focus and focus.res else None)}))
            except Exception:
                pass

            for s in slots:
                if not s.res:
                    continue
                logw.writerow([
                    datetime.now().isoformat(timespec="seconds"),
                    s.symbol or "",
                    fmt(s.star and s.star["value"], ".1f", ""),
                    s.star["color"] if s.star else "",
                    fmt(s.heart and s.heart["w"], ".1f", ""),
                    fmt(s.heart and s.heart["b"], ".1f", ""),
                    s.heart["shade"] if s.heart else "",
                    fmt(s.check and s.check["gap"], ".2f", ""),
                    SqueezeTracker.label(s.sq) or "", s.res["verdict"]])
            logf.flush()

            stamps.append(t0)
            stamps[:] = [x for x in stamps if t0 - x <= 5]
            parts = [banner(focus.res if focus else None, master)]
            if not single or (lead and l2_sym
                              and not _sym_match(l2_sym, lead)):
                parts.append(leader_strip(lead, scores.get(lead), l2_sym))
            if not single:
                parts.append(slots_table(slots, lead, l2_sym))
            parts.append(detail_table(focus, len(stamps) / 5.0, misses,
                                      l2, master, l2_note))
            if hotkeys.enabled:
                st = hotkeys.status()
                hint = (f"[dim]keys 1-{max(1, len(slots))}: load that chart "
                        "into TradingView   ·   space: load the leader[/dim]")
                parts.append(Panel(
                    Align.center(hint + (f"     [bold green]{st}[/bold green]"
                                         if st else "")),
                    border_style="grey37", padding=(0, 1)))
            live.update(Group(*parts))
            time.sleep(max(0.0, interval - (time.time() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
