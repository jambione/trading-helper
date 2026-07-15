"""Webull Level-2 OCR signal monitor.

Captures the L2 order-book region of your screen, OCRs it, computes
imbalance/spread/wall signals, and alerts via console dashboard, sound,
and Windows toast. Logs every snapshot to CSV.

Region modes (config.json "region_mode"):
    "auto"   - finds the Webull window and locates the L2 panel by its
               header row; re-anchors when the window moves/resizes. Falls
               back to the last anchor that worked (or the calibrated
               region), projected onto the window's current rect.
    "manual" - uses the region from calibrate.py, tracked to the Webull
               window so it still follows moves/resizes.

Every region is stored as a FRACTION of the Webull window rather than as
screen pixels, so nothing goes stale when the window moves or is resized.

Usage:
    python l2_signal.py

Requires: Tesseract-OCR installed (https://github.com/UB-Mannheim/tesseract/wiki)
and `pip install -r requirements.txt`.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import threading
import time
import zlib
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np
import pytesseract
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from l2_core import (VWAP_MIN_AGE_DEFAULT, BookFlow, GlitchGate, L2Book,
                     LongView, PaperTrader, SessionVWAP, Signal, SignalEngine,
                     Trade, WallTracker, _median, market_bias, parse_l2_text,
                     playbook, project_price, quality_grade, signal_quality,
                     tape_confirms)
from tape_core import Tape, classify_rgb, parse_tape_line

# shared session clock (repo root) — optional: monitor runs fine without it
sys.path.insert(0, str(Path(__file__).parent.parent))
try:
    from session_clock import session_line
except Exception:                                       # noqa: BLE001
    session_line = None

# Make this process DPI-aware so pygetwindow's window coordinates and
# mss's captured pixels are in the SAME coordinate space. Without this,
# Windows display scaling (125%/150%) makes the auto-located region land
# in the wrong place entirely.
if sys.platform == "win32":
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
POS_STATE = HERE.parent / "position_state.json"   # scripts/position_feed.py

TESS_CFG = "--psm 6 -c tessedit_char_whitelist=0123456789.,K"
# Time&Sales rows need ':' for timestamps; '--' (exch/cond column) is
# whitelisted away so rows parse as "time price size".
TS_TESS_CFG = "--psm 6 -c tessedit_char_whitelist=0123456789.:,K"


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text())
    tp = cfg.get("tesseract_path")
    if tp and os.path.exists(tp):
        pytesseract.pytesseract.tesseract_cmd = tp
    return cfg


def read_real_position(symbol: str | None) -> dict | None:
    """The broker position for `symbol` from the position feed, or None.

    None also covers feed missing/stale/error — callers must not treat
    that as 'flat', so sync only happens when the feed is fresh and ok."""
    if not symbol:
        return None
    try:
        d = json.loads(POS_STATE.read_text())
        if not d.get("ok") or time.time() - d.get("ts", 0) > 30:
            return None
        sym = symbol.upper()
        for p in d.get("positions", []):
            ps = str(p.get("symbol", "")).upper()
            if ps == sym or ps.startswith(sym) or sym.startswith(ps):
                return p
        return {}   # feed fresh, symbol not held -> broker flat for us
    except Exception:
        return None


def preprocess(img: np.ndarray) -> np.ndarray:
    """Dark-theme screenshot -> clean black-text-on-white for Tesseract.

    Adaptive (local) threshold instead of global Otsu: the L2 rows have
    colored depth bars behind the numbers, and a global threshold merges
    text into the bars. Local thresholding separates each character from
    whatever is directly behind it."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    gray = cv2.resize(gray, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    if np.median(gray) < 127:      # dark theme -> invert
        gray = 255 - gray
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                 cv2.THRESH_BINARY, 41, 15)


def ocr_book(sct, region: dict) -> L2Book | None:
    return ocr_image(np.asarray(sct.grab(region)))


def ocr_image(raw: np.ndarray) -> L2Book | None:
    # image_to_data gives word boxes; rebuild rows with explicit spaces so
    # narrow column gaps can't merge numbers together.
    d = pytesseract.image_to_data(preprocess(raw), config=TESS_CFG,
                                  output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list] = {}
    for i, word in enumerate(d["text"]):
        word = word.strip()
        if not word:
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines.setdefault(key, []).append((d["left"][i], word))
    text = "\n".join(
        " ".join(w for _, w in sorted(words))
        for _, words in sorted(lines.items())
    )
    book = parse_l2_text(text)
    if book is None:
        book = _ocr_by_columns(preprocess(raw))
    return book


def _ocr_by_columns(th: np.ndarray) -> L2Book | None:
    """Fallback when columns merge: locate the 4 columns via pixel
    projection, OCR each strip separately, then recombine rows."""
    ink = (th < 128).sum(axis=0)          # black-pixel count per x
    min_gap = max(8, th.shape[1] // 30)
    segs, start = [], None
    for x, v in enumerate(ink):
        if v and start is None:
            start = x
        elif not v and start is not None:
            segs.append([start, x]); start = None
    if start is not None:
        segs.append([start, th.shape[1]])
    merged = []
    for s in segs:                        # merge digit-level gaps
        if merged and s[0] - merged[-1][1] < min_gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    if len(merged) < 4:
        return None
    merged = sorted(merged, key=lambda s: s[0] - s[1])[:4]  # 4 widest
    merged.sort(key=lambda s: s[0])
    cols = []
    for a, b in merged:
        pad = 6
        strip = th[:, max(0, a - pad):min(th.shape[1], b + pad)]
        txt = pytesseract.image_to_string(strip, config=TESS_CFG)
        cols.append([ln.strip() for ln in txt.splitlines() if ln.strip()])
    n = min(len(c) for c in cols)
    if n < 3:
        return None
    text = "\n".join(" ".join(c[i] for c in cols) for i in range(n))
    return parse_l2_text(text)


# ------------------------------------------------------- auto region --------

# UI words that look like tickers but aren't
_NOT_TICKERS = {"BID", "ASK", "SIZE", "VWAP", "MACD", "ATR", "EMA", "SMA",
                "RSI", "VOL", "NEWS", "EXT", "AUTO", "OPEN", "TAV", "EDT",
                "EST", "UTC", "AM", "PM", "NA", "OTC", "USD", "INC", "CO",
                "LTD", "LLC", "BUY", "SELL", "DAY", "VS", "TT", "HOD", "LOD"}

# x-positions this close (window pixels, pre-2x-scale) count as one column
_COL_TOL = 20
# repeats this many times down ONE column = a table column, not a symbol
_TABLE_MIN_ROWS = 4


def _guess_symbol(words: list[tuple[str, int]], scale: int = 1) -> str | None:
    """Pick the active ticker from OCR'd window tokens.

    The active ticker is written in several DIFFERENT places — chart header,
    quotes panel, positions row, order line — so it shows up in several
    distinct COLUMNS. A table instead repeats one token down a single x:
    Time&Sales stamps its execution venue (OCEA, BOSS, ARCA…) on every
    print, which by raw frequency buried the real symbol 7-to-4 and had the
    monitor polling depth for an exchange code.

    So position, not identity, decides:
      * a token stacked >= _TABLE_MIN_ROWS times in ONE column is a table
        column and is ranked last outright, however often it appears;
      * otherwise rank by how many distinct columns it spans, then by count.

    Venue codes are deliberately NOT blocklisted: the list would never be
    complete, and some are real tickers (PSX is Phillips 66), so a name-based
    filter would eventually silence a stock you actually trade.

    `words` are (text, x) pairs; `scale` is the factor x was measured at.
    """
    import collections
    import re as _re
    cands: dict[str, list[int]] = collections.defaultdict(list)
    for text, x in words:
        if _re.fullmatch(r"[A-Z]{2,5}", text) and text not in _NOT_TICKERS:
            cands[text].append(x)
    if not cands:
        return None
    tol = _COL_TOL * scale

    def score(xs: list[int]) -> tuple[int, int, int]:
        cols: list[int] = []
        for x in sorted(xs):
            if not cols or x - cols[-1] > tol:
                cols.append(x)
        table = len(cols) == 1 and len(xs) >= _TABLE_MIN_ROWS
        return (0 if table else 1), len(cols), len(xs)

    return max(cands, key=lambda t: score(cands[t]))


def detect_symbol(win_img: np.ndarray) -> str | None:
    """OCR the Webull window and guess the active ticker symbol."""
    gray = cv2.cvtColor(win_img, cv2.COLOR_BGRA2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    if np.median(gray) < 127:
        gray = 255 - gray
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    d = pytesseract.image_to_data(th, config="--psm 11",
                                  output_type=pytesseract.Output.DICT)
    words = [(d["text"][i].strip(), d["left"][i])
             for i in range(len(d["text"])) if d["text"][i].strip()]
    return _guess_symbol(words, scale=2)   # gray was resized 2x above

def find_webull_window() -> dict | None:
    """Bounding box of the (largest) visible window owned by the Webull
    PROCESS (Webull.exe). Matching by process instead of window title
    avoids grabbing unrelated windows that merely mention 'Webull' in
    their title (this console, chat apps, browsers, etc.)."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    results: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFO
        if not h:
            return True
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(h)
        if not ok or "webull" not in buf.value.rsplit("\\", 1)[-1].lower():
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, hh = r.right - r.left, r.bottom - r.top
        if w > 500 and hh > 300:
            results.append({"left": r.left, "top": r.top,
                            "width": w, "height": hh})
        return True

    user32.EnumWindows(cb, 0)
    if not results:
        return None
    return max(results, key=lambda r: r["width"] * r["height"])


def locate_l2_region(win_img: np.ndarray, win_rect: dict,
                     debug_dir: Path | None = None) -> dict | None:
    """Find the L2 header row ('Size  Bid ... Ask  Size') inside a screenshot
    of the Webull window and return the screen region of the rows below it.

    Tries BOTH polarities (normal + inverted) because bright overlays can
    fool a single mean-based inversion decision. On failure, writes the
    detected bid/ask/size words to debug_words.txt when debug_dir is given.

    Decoys like 'Buy $800 @ASK' don't match because tokens must equal
    'bid'/'ask' exactly, sit close together, and have 'Size' on the row.
    """
    gray = cv2.cvtColor(win_img, cv2.COLOR_BGRA2GRAY)
    scale = 2
    gray = cv2.resize(gray, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_CUBIC)
    dark_theme = np.median(gray) < 127
    variants = [255 - gray, gray] if dark_theme else [gray, 255 - gray]

    diag = []
    best = None
    for g in variants:
        _, th = cv2.threshold(g, 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        d = pytesseract.image_to_data(th, config="--psm 11",
                                      output_type=pytesseract.Output.DICT)
        words = [(d["text"][i].strip().lower(), d["left"][i], d["top"][i],
                  d["width"][i], d["height"][i])
                 for i in range(len(d["text"])) if d["text"][i].strip()]
        bids = [w for w in words if w[0] == "bid"]
        asks = [w for w in words if w[0] == "ask"]
        sizes_all = [w for w in words if w[0] == "size"]
        diag.append((bids, asks, sizes_all))

        for b in bids:
            for a in asks:
                gap = a[1] - b[1]
                if gap <= 0 or abs(a[2] - b[2]) > b[4]:
                    continue  # ask must be right of bid, on the same row
                if gap > th.shape[1] // 4:
                    continue  # header Bid/Ask sit close together
                sizes = [w for w in words
                         if w[0] == "size" and abs(w[2] - b[2]) <= b[4]
                         and b[1] - 5 * gap < w[1] < a[1] + 5 * gap]
                if not sizes:
                    continue  # a real header has 'Size' flanking Bid/Ask
                score = len(sizes)
                if best is None or score > best[2]:
                    best = (b, a, score, sizes)
        if best:
            break

    if best is None:
        if debug_dir:
            try:
                with open(debug_dir / "debug_words.txt", "w") as f:
                    f.write("locator failed; words found per variant "
                            "(coords are at 2x window scale):\n")
                    for i, (bb, aa, ss) in enumerate(diag):
                        f.write(f"variant {i}: bid={bb}\n"
                                f"           ask={aa}\n"
                                f"           size={ss}\n")
            except Exception:
                pass
        return None
    b, a, _, sizes = best

    h = b[4]                      # text height at 2x
    gap = a[1] - b[1]
    lefts = [w[1] for w in sizes] + [b[1] - gap]
    rights = [w[1] + w[3] for w in sizes] + [a[1] + a[3] + gap]
    left, right = min(lefts) - h, max(rights) + h
    if right - left > 10 * gap:   # sanity: panel is only a few gaps wide
        left = max(left, b[1] - 3 * gap)
        right = min(right, a[1] + a[3] + 3 * gap)
    top = b[2] + int(h * 2.0)     # just below the header row
    height = int(h * 1.9 * 11)    # ~10-11 data rows
    # if the Trade/TurboTrader/Ladder tabs are visible below the header,
    # stop above them (short panels show fewer rows)
    tabs = [w[2] for w in words
            if w[0] in ("trade", "turbotrader", "ladder") and w[2] > b[2]]
    if tabs:
        height = min(height, min(tabs) - top - h // 2)

    region = {
        "left": win_rect["left"] + max(0, left // scale),
        "top": win_rect["top"] + top // scale,
        "width": (right - left) // scale,
        "height": height // scale,
    }
    # clamp inside the window
    region["width"] = min(region["width"],
                          win_rect["left"] + win_rect["width"] - region["left"])
    region["height"] = min(region["height"],
                           win_rect["top"] + win_rect["height"] - region["top"])
    if region["width"] < 60 or region["height"] < 40:
        return None
    return region


REGION_CACHE = HERE / "region_cache.json"

# Horizon project_price extrapolates over. Only ever divided back OUT to
# recover a %/min pace for display - the target itself is not shown
# anywhere, because it does not survive a backtest (see lv_banner).
PROJ_MINUTES = 5.0


def rel_from_abs(region: dict | None, rect: dict | None) -> dict | None:
    """Absolute screen region -> fractions of the window rect."""
    if not region or not rect or rect["width"] <= 0 or rect["height"] <= 0:
        return None
    return {"x": (region["left"] - rect["left"]) / rect["width"],
            "y": (region["top"] - rect["top"]) / rect["height"],
            "w": region["width"] / rect["width"],
            "h": region["height"] / rect["height"]}


def abs_from_rel(rel: dict | None, rect: dict | None) -> dict | None:
    """Window-relative fractions -> absolute region on the window's CURRENT
    rect, clamped inside it. This is what makes a calibrated region survive
    the window being moved to another monitor or resized."""
    if not rel or not rect:
        return None
    try:
        left = rect["left"] + int(round(rel["x"] * rect["width"]))
        top = rect["top"] + int(round(rel["y"] * rect["height"]))
        width = int(round(rel["w"] * rect["width"]))
        height = int(round(rel["h"] * rect["height"]))
    except (KeyError, TypeError):
        return None
    left = max(rect["left"], min(left, rect["left"] + rect["width"] - 1))
    top = max(rect["top"], min(top, rect["top"] + rect["height"] - 1))
    width = min(width, rect["left"] + rect["width"] - left)
    height = min(height, rect["top"] + rect["height"] - top)
    if width < 60 or height < 40:
        return None
    return {"left": left, "top": top, "width": width, "height": height}


class RegionTracker:
    """Keeps the capture region pointed at the L2 panel even when the
    Webull window moves or resizes. Re-anchors when the window rect
    changes or several consecutive reads fail.

    Three ways to find the panel, best first:
      1. auto-locate  - OCR the window, anchor on the 'Size Bid Ask Size'
                        header. Immune to moves/resizes; needs the header
                        to actually be on screen (an unsubscribed L2 panel
                        renders an upsell instead, and there's nothing to
                        find).
      2. learned rel  - every successful anchor records where the panel sat
                        AS A FRACTION of the window, cached to disk. When
                        auto-locate later fails, that fraction is projected
                        onto the window's current rect, so the fallback
                        follows the window instead of going stale.
      3. calibrated   - config.json "region_rel" (fractions, written by
                        calibrate.py) or legacy "region" (absolute pixels,
                        only valid while the window sits exactly where it
                        was calibrated).
    """

    MISS_LIMIT = 5
    BAD_ANCHOR_LIMIT = 3     # anchors that produced zero good reads
    MANUAL_HOLD = 60         # seconds to sit on manual region before retrying auto
    CACHE_MIN_OK = 5         # good reads before an anchor is worth caching

    def __init__(self, cfg: dict, console=None):
        self.mode = cfg.get("region_mode", "auto")
        self.manual_abs = cfg.get("region")
        self.manual_rel = cfg.get("region_rel")
        self.debug = cfg.get("debug", True)
        self.console = console
        self.region = None
        self.win_rect = None
        self.misses = 0
        self.ok_since_anchor = 0
        self.bad_anchors = 0
        self.manual_until = 0.0
        self.learned_rel = self._load_cache()
        self._cached_rel = None
        self.symbol: str | None = None
        self._sym_cand: str | None = None

    def _load_cache(self) -> dict | None:
        try:
            return json.loads(REGION_CACHE.read_text()).get("region_rel")
        except Exception:                                  # noqa: BLE001
            return None

    def _save_cache(self, rel: dict):
        """Persist a proven anchor. Kept out of config.json so the user's
        settings are never rewritten underneath them."""
        try:
            tmp = REGION_CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"region_rel": rel}, indent=2))
            tmp.replace(REGION_CACHE)
        except Exception:                                  # noqa: BLE001
            pass

    def fallback(self, rect: dict | None) -> dict | None:
        """Best non-auto region for the window's current position."""
        for rel in (self.learned_rel, self.manual_rel):
            got = abs_from_rel(rel, rect)
            if got:
                return got
        return self.manual_abs

    def note_symbol(self, sym: str | None):
        """Adopt a detected symbol with hysteresis: switching wipes all
        per-symbol state upstream, so a CHANGE needs two consecutive
        readings of the same new ticker — one OCR misread (VEE for VEEE)
        must not nuke 5 minutes of history. First detection is instant."""
        if not sym or sym == self.symbol:
            self._sym_cand = None
            return
        if self.symbol is None or sym == self._sym_cand:
            self.symbol, self._sym_cand = sym, None
        else:
            self._sym_cand = sym

    def report(self, ok: bool):
        if ok:
            self.misses = 0
            self.ok_since_anchor += 1
        else:
            self.misses += 1

    def _say(self, msg: str):
        if self.console:
            self.console.print(f"[dim]{msg}[/dim]")

    def get(self, sct) -> dict | None:
        if self.mode == "manual":
            # still tracked to the window: a calibrated region is only
            # meaningful relative to the panel it was drawn around
            return self.fallback(find_webull_window())
        now = time.time()
        rect = find_webull_window()
        if rect is None:
            return self.fallback(None)  # window gone; last resort
        if now < self.manual_until:
            return self.fallback(rect)
        if (self.region and rect == self.win_rect
                and self.misses < self.MISS_LIMIT):
            return self.region
        # an anchor that produced good reads is worth remembering, as a
        # fraction of the window so it survives the next move/resize
        if (self.region and self.ok_since_anchor >= self.CACHE_MIN_OK
                and self.win_rect):
            rel = rel_from_abs(self.region, self.win_rect)
            if rel and rel != self._cached_rel:
                self.learned_rel = self._cached_rel = rel
                self._save_cache(rel)
        # window moved/resized or reads failing -> re-anchor
        if self.region is not None and self.ok_since_anchor == 0:
            self.bad_anchors += 1
        else:
            self.bad_anchors = 0
        if self.bad_anchors >= self.BAD_ANCHOR_LIMIT and self.fallback(rect):
            src = "learned" if self.learned_rel else "calibrated"
            self._say(f"auto-locate keeps failing -> using {src} region "
                      f"for {self.MANUAL_HOLD}s (check debug_anchor.png; "
                      "run calibrate.py to refresh the fallback)")
            self.manual_until = now + self.MANUAL_HOLD
            self.bad_anchors = 0
            self.misses = 0
            return self.fallback(rect)
        img = np.asarray(sct.grab(rect))
        found = locate_l2_region(img, rect,
                                 HERE if self.debug else None)
        try:
            self.note_symbol(detect_symbol(img))
        except Exception:
            pass
        if self.debug:
            dbg = np.ascontiguousarray(img[:, :, :3])
            if found:
                x = found["left"] - rect["left"]
                y = found["top"] - rect["top"]
                cv2.rectangle(dbg, (x, y),
                              (x + found["width"], y + found["height"]),
                              (0, 255, 0), 2)
            cv2.imwrite(str(HERE / "debug_anchor.png"), dbg)
        self.win_rect = rect
        self.misses = 0
        self.ok_since_anchor = 0
        if found:
            if found != self.region:
                self._say(f"L2 panel located at {found}")
            self.region = found
        else:
            # the window just moved or the reads went bad, so the old
            # absolute region is the one thing we know is wrong
            self.region = self.fallback(rect) or self.region
        return self.region


# ---------------------------------------------------------- time & sales ----

def locate_ts_region(win_img: np.ndarray, win_rect: dict) -> dict | None:
    """Find the Time&Sales widget inside a screenshot of the Webull window
    and return the screen region of the print rows below its tab.

    Anchor is the 'Time&Sales' tab text (any OCR token containing 'sales');
    the region runs from there down to the Trade/TurboTrader/Ladder tab row
    when visible, and right to the window edge minus the scrollbar.

    Adaptive threshold, not Otsu: the tab text is dim grey on dark and an
    Otsu pass over the whole window erases it entirely (the bright tape
    rows dominate the histogram); 3x + local threshold reads it clean."""
    gray = cv2.cvtColor(win_img, cv2.COLOR_BGRA2GRAY)
    scale = 3
    gray = cv2.resize(gray, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_CUBIC)
    dark = np.median(gray) < 127
    for g in ([255 - gray, gray] if dark else [gray, 255 - gray]):
        th = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY, 41, 15)
        d = pytesseract.image_to_data(th, config="--psm 11",
                                      output_type=pytesseract.Output.DICT)
        words = [(d["text"][i].strip().lower(), d["left"][i], d["top"][i],
                  d["width"][i], d["height"][i])
                 for i in range(len(d["text"])) if d["text"][i].strip()]
        anchors = [w for w in words if "sales" in w[0]]
        if not anchors:
            continue
        a = min(anchors, key=lambda w: w[2])   # topmost 'sales' token
        h = a[4]
        left = max(0, a[1] - h)
        right = th.shape[1] - int(1.5 * h)     # leave the scrollbar out
        top = a[2] + int(h * 2.2)              # skip the tab bar underline
        height = int(h * 1.9 * 18)             # ~18 print rows
        tabs = [w[2] for w in words
                if w[0] in ("trade", "turbotrader", "ladder")
                and w[2] > a[2]]
        if tabs:
            height = min(height, min(tabs) - top - h // 2)
        region = {
            "left": win_rect["left"] + left // scale,
            "top": win_rect["top"] + top // scale,
            "width": (right - left) // scale,
            "height": height // scale,
        }
        region["width"] = min(region["width"], win_rect["left"]
                              + win_rect["width"] - region["left"])
        region["height"] = min(region["height"], win_rect["top"]
                               + win_rect["height"] - region["top"])
        if region["width"] >= 80 and region["height"] >= 40:
            return region
    return None


def _row_side(raw: np.ndarray, y0: int, y1: int) -> str:
    """Aggressor side from the row's pixels in the RAW (color) frame.
    Uses every clearly-colored pixel in the row band, so it works for
    colored text on dark AND for the solid-highlight big-print rows
    where the text itself is white."""
    strip = raw[max(0, y0):max(y0 + 1, y1), :, :3].astype(np.int16)
    b, g, r = strip[..., 0], strip[..., 1], strip[..., 2]   # mss is BGRA
    colored = np.abs(r - g) > 40
    if int(colored.sum()) < 20:
        return "N"
    return classify_rgb(float(r[colored].mean()), float(g[colored].mean()),
                        float(b[colored].mean()))


def tape_rows(raw: np.ndarray) -> list:
    """OCR one Time&Sales frame -> [(hh:mm:ss, price, size, side)],
    top-down (newest print first), garbled rows skipped.

    Two passes: rows that parse unambiguously establish the going price,
    which then resolves the merged price/size blobs OCR produces when the
    columns touch ('138.40313' -> 138.40 x 313, not 138.403 x 13)."""
    th = preprocess(raw)                       # 3x upscale, binarized
    d = pytesseract.image_to_data(th, config=TS_TESS_CFG,
                                  output_type=pytesseract.Output.DICT)
    lines: dict[tuple, list] = {}
    for i, word in enumerate(d["text"]):
        word = word.strip()
        if not word:
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines.setdefault(key, []).append(
            (d["left"][i], d["top"][i], d["height"][i], word))
    entries = []                               # (text, y0, y1) top-down
    for words in sorted(lines.values(), key=lambda ws: min(w[1] for w in ws)):
        entries.append((" ".join(w[3] for w in sorted(words)),
                        min(w[1] for w in words) // 3,      # raw coords
                        max(w[1] + w[2] for w in words) // 3))
    first = [parse_tape_line(t) for t, _, _ in entries]
    prices = [p[1] for p in first if p]
    hint = _median(prices) if prices else None
    # every price in the widget shares one decimal width; learn it from
    # the clean rows so merged blobs split deterministically
    decs = []
    for (t, _, _), p in zip(entries, first):
        if p:
            for d0 in (4, 3, 2):     # longest first: '1.3350' beats '1.33'
                if f"{p[1]:.{d0}f}" in t:
                    decs.append(d0)
                    break
    dec = max(set(decs), key=decs.count) if decs else None
    rows = []
    for parsed, (text, y0, y1) in zip(first, entries):
        if parsed is None and hint is not None:
            parsed = parse_tape_line(text, hint, dec)
        if parsed:
            rows.append((*parsed, _row_side(raw, y0, y1)))
    return rows


class TapeReader:
    """Daemon thread that captures the Time&Sales widget, OCRs the prints
    with per-row color side-detection, and feeds a Tape aggregator. Runs
    independently of the L2 loop so a slow tape read can never stall the
    book, and skips OCR entirely when the tape pixels haven't changed."""

    def __init__(self, cfg: dict, console=None):
        self.enabled = bool(cfg.get("ts_enabled", True))
        self.poll = float(cfg.get("ts_poll", 0.6))
        self.manual = cfg.get("ts_region")
        self.console = console
        self.tape = Tape()
        self.lock = threading.Lock()
        self.region: dict | None = None
        self.ok = 0
        self.miss = 0
        self._misses_row = 0
        self._win_rect: dict | None = None
        self._frame_key: int | None = None
        self._quote: tuple | None = None       # (bid, ask, ts) from L2 loop
        self.last_print: float | None = None   # wall ts of last NEW print
        # where the tape's prints are coming from, for the log/display:
        # "ocr" (the screen-scraped panel), "seed" (pre-hop stream prints
        # just loaded), "finnhub" (dark-tape stream fallback feeding live)
        self.src: str = "ocr"
        self._last_ocr: float | None = None    # ts of last OCR-ingested print
        self.stream_upto: float = 0.0          # high-water mark of fed prints
        if self.enabled:
            threading.Thread(target=self._run, daemon=True).start()

    def set_quote(self, bid: float, ask: float, ts: float):
        """Latest L2 touch, used to side uncolored prints (quote rule)."""
        with self.lock:
            self._quote = (bid, ask, ts)

    def snapshot(self, now: float) -> dict:
        with self.lock:
            # "on" also when stream prints are feeding the tape: real
            # executed prints are real evidence whether they arrived by
            # OCR or by websocket -- with the T&S panel hidden the pillar
            # used to abstain even though the stream had the data
            has_prints = len(self.tape.prints) > 0
            return {"on": (self.region is not None and self.ok > 0)
                    or has_prints,
                    "m1": self.tape.metrics(now, 60),
                    "m5": self.tape.metrics(now, 300),
                    "vwap": self.tape.vwap(),
                    "since": self.tape.started,
                    "last_print": self.last_print,
                    "src": self.src,
                    "ok": self.ok, "miss": self.miss}

    def reset(self):
        with self.lock:
            self.tape.reset()
            self.last_print = None
            self._last_ocr = None
            self.src = "ocr"
            self.stream_upto = 0.0

    def seed(self, points: list) -> int:
        """Pre-load pre-hop stream prints right after reset() -- see
        Tape.seed_prints. Sets the stream high-water mark so the dark-tape
        fallback never re-feeds the same prints."""
        with self.lock:
            bid = ask = None
            if self._quote:
                bid, ask = self._quote[0], self._quote[1]
            n = self.tape.seed_prints(points, bid, ask)
            if n:
                newest = max(p[0] for p in points)
                self.stream_upto = max(self.stream_upto, newest)
                self.last_print = newest
                self.src = "seed"
            return n

    def ocr_dark(self, now: float, secs: float) -> bool:
        """No NEW print has come off the OCR'd panel for `secs` (or ever) --
        the condition under which the stream fallback may feed, so the two
        feeds never ingest the same trades side by side."""
        with self.lock:
            return self._last_ocr is None or now - self._last_ocr > secs

    def feed_stream(self, points: list, now: float) -> int:
        """Dark-tape fallback: live stream prints while OCR yields nothing
        (panel hidden/obscured, or not shown at all). Caller gates on
        ocr_dark(); the high-water mark makes re-feeds impossible."""
        with self.lock:
            pts = [p for p in points if p[0] > self.stream_upto]
            if not pts:
                return 0
            bid = ask = None
            if self._quote and now - self._quote[2] <= 5.0:
                bid, ask = self._quote[0], self._quote[1]
            n = self.tape.ingest_stream(pts, bid, ask)
            if n:
                self.stream_upto = max(p[0] for p in pts)
                self.last_print = now
                self.src = "finnhub"
            return n

    def prints_since(self, ts: float) -> list:
        """Thread-safe view of prints newer than `ts` (for BookFlow)."""
        with self.lock:
            return self.tape.prints_since(ts)

    def _run(self):
        with mss.MSS() as sct:
            while True:
                t0 = time.time()
                try:
                    self._step(sct, t0)
                except Exception:                          # noqa: BLE001
                    pass   # never let the tape thread die on a bad frame
                time.sleep(max(0.0, self.poll - (time.time() - t0)))

    def _step(self, sct, now: float):
        rect = find_webull_window()
        if rect and (self.region is None or rect != self._win_rect
                     or self._misses_row >= 5):
            found = locate_ts_region(np.asarray(sct.grab(rect)), rect)
            if found and found != self.region and self.console:
                self.console.print(f"[dim]Time&Sales located at {found}[/dim]")
            self.region = found or self.region or self.manual
            self._win_rect = rect
            self._misses_row = 0
        if not self.region:
            return
        raw = np.asarray(sct.grab(self.region))
        key = zlib.crc32(raw.tobytes())
        if key == self._frame_key:
            return                     # tape unchanged -> no OCR needed
        self._frame_key = key
        rows = tape_rows(raw)
        with self.lock:
            bid = ask = None
            if self._quote and now - self._quote[2] <= 5.0:
                bid, ask = self._quote[0], self._quote[1]
            if rows:
                self.ok += 1
                self._misses_row = 0
                if self.tape.ingest_frame(rows, now, bid, ask):
                    self.last_print = now
                    self._last_ocr = now
                    self.src = "ocr"     # OCR back -> fallback stands down
            else:
                self.miss += 1
                self._misses_row += 1


# ------------------------------------------------------- reference price ----

class RefFeed:
    """Real last-trade price per symbol from the Finnhub stream, used to
    veto OCR frames whose mid disagrees with an actual print by a wide
    margin (the catastrophic 139->6 / 6.81->23.74 misreads that polluted
    the paper log and, per score_confidence.py, ~30% of l2_log.csv).

    Screen-scraped depth stays the signal; this is only a truth anchor for
    the price. Degrades to a no-op when there's no key, no `websockets`,
    or no fresh print — the monitor then runs exactly as it did before."""

    def __init__(self, cfg: dict, console=None):
        self.enabled = bool(cfg.get("ref_price_gate", True))
        self.max_age = float(cfg.get("ref_max_age", 15.0))
        self.key = cfg.get("finnhub_key") or os.getenv("FINNHUB_API_KEY", "")
        # built before any early return: _on_trade can fire the moment the
        # callback is registered, and vwap() is called whether or not the
        # feed came up
        self.svwap = SessionVWAP()
        # rolling per-symbol print buffer (ts, price, size) for the tape:
        # the same pre-subscribed watchlist stream that warms the trend
        # and the session VWAP also carries every executed print, so on a
        # hop the TAPE can be seeded too instead of sitting dark for a
        # window (and fed live if the OCR tape goes dark). ~10 min deep,
        # matching Tape's own retention.
        self._prints: dict[str, deque] = {}
        self._prints_keep = 600.0
        # appended from the stream thread, read from the render loop
        self._prints_lock = threading.Lock()
        self._start = self._state = self._unsub = None
        self._subscribed: set[str] = set()   # symbols actually on the wire
        self._watch: set[str] = set()        # watchlist we want warmed
        self._active: str | None = None      # the symbol in focus now
        # ensure() (render loop) and set_watchlist() (warm-start daemon) both
        # reconcile from different threads -> guard the shared sub sets
        self._sub_lock = threading.Lock()
        if not (self.enabled and self.key):
            self.enabled = False
            if console and not self.key:
                console.print("[dim]ref-price gate off — no Finnhub key "
                              "(set FINNHUB_API_KEY or finnhub_key in "
                              "config.json)[/dim]")
            return
        try:
            sys.path.insert(0, str(HERE.parent))
            from finnhub_stream import (FINNHUB_STATE, register_trade_callback,
                                         request_unsubscribe,
                                         start_finnhub_stream)
            self._start, self._state = start_finnhub_stream, FINNHUB_STATE
            self._unsub = request_unsubscribe
            # Every print for every SUBSCRIBED symbol feeds the session VWAP,
            # not just the active one: the watchlist is pre-subscribed, so a
            # name accumulates its VWAP in the background for the minutes
            # before you hop onto it. That warm-in-advance is what lets the
            # pillar clear VWAP_MIN_AGE_DEFAULT on arrival instead of sitting
            # dark for 15 min of a 72-second visit.
            register_trade_callback(self._on_trade)
            if console:
                console.print("[dim]ref-price gate on — Finnhub last-trade "
                              "vetoes wild OCR misreads, and feeds the "
                              "session VWAP pillar[/dim]")
        except Exception as e:                                 # noqa: BLE001
            self.enabled = False
            if console:
                console.print(f"[dim]ref-price gate unavailable: {e}[/dim]")

    def _on_trade(self, symbol: str, price: float, volume: float,
                  ts_ms: float) -> None:
        """Stream-thread callback. Must never raise: an exception here would
        propagate into the websocket reader and kill the price feed the
        glitch gate depends on."""
        try:
            ts = float(ts_ms) / 1000.0 if ts_ms else time.time()
            self.svwap.ingest(symbol, float(price), float(volume), ts)
            if price and volume and float(price) > 0 and float(volume) > 0:
                with self._prints_lock:
                    dq = self._prints.setdefault(symbol.upper(), deque())
                    dq.append((ts, float(price), float(volume)))
                    while dq and ts - dq[0][0] > self._prints_keep:
                        dq.popleft()
        except Exception:                                      # noqa: BLE001
            pass

    def vwap(self, symbol: str | None) -> tuple[float | None, float | None]:
        """(session vwap, seconds of session actually covered) for `symbol`.
        (None, None) when the feed is off or the symbol was never streamed —
        the caller then falls back to the local tape VWAP."""
        if not self.enabled:
            return None, None
        return self.svwap.vwap(symbol), self.svwap.age(symbol)

    def _reconcile(self):
        """Drive the live subscription set to exactly {watchlist + active},
        capped under Finnhub's free-tier 50-symbol limit. Subscribes what's
        newly wanted and UNsubscribes what's no longer wanted (dropped
        watchlist names, stale prior actives) so the set can't creep toward
        the cap over a long session and starve new movers of a warm slot."""
        with self._sub_lock:
            want = set(self._watch)
            if self._active:
                want.add(self._active)
            if len(want) > 45:                   # keep headroom under 50
                want = ({self._active} if self._active else set()) \
                    | set(list(want - {self._active})[:44])
            add = [s for s in want if s not in self._subscribed]
            drop = [s for s in self._subscribed if s not in want]
            try:
                if add:
                    self._start(self.key, add)
                    self._subscribed.update(add)
                if drop and self._unsub:
                    self._unsub(drop)
                    self._subscribed.difference_update(drop)
                    # no more prints are coming for these, so their VWAP can
                    # only go stale; drop it rather than let a name that
                    # rotated off hours ago answer vwap() if it returns
                    self.svwap.forget(drop)
                    with self._prints_lock:
                        for s in drop:
                            self._prints.pop(s.upper(), None)
            except Exception:                                  # noqa: BLE001
                pass

    def ensure(self, symbol: str | None):
        """Mark `symbol` as the active one and reconcile (first call also
        starts the stream). Cheap no-op when the active symbol is unchanged,
        so it's safe to call every poll."""
        if not self.enabled or not symbol or symbol == self._active:
            return
        self._active = symbol
        self._reconcile()

    def set_watchlist(self, symbols):
        """Pre-subscribe a watchlist so each symbol's trade history warms in
        the background BEFORE you switch onto it -- that pre-collected history
        is what seed_points() returns to seed the trend instantly on a switch.
        Names that fall off the list get unsubscribed on the next reconcile."""
        if not self.enabled:
            return
        want = {s for s in symbols if s}
        if want == self._watch:
            return                              # unchanged -> skip the diff
        self._watch = want
        self._reconcile()

    def seed_points(self, symbol: str | None, seconds: float) -> list:
        """(ts, price) points collected for `symbol` over the last `seconds`
        from the stream -- empty unless it was subscribed early enough to have
        accumulated history. Fed to SignalEngine.seed_history() on a switch."""
        if not self.enabled or not symbol or self._state is None:
            return []
        try:
            return self._state.recent_prices(symbol, seconds)
        except Exception:                                      # noqa: BLE001
            return []

    def seed_prints(self, symbol: str | None, seconds: float) -> list:
        """(ts, price, size) prints collected for `symbol` over the last
        `seconds` -- empty unless it was pre-subscribed long enough. Fed to
        the tape on a symbol switch (Tape.seed_prints), the tape-side twin
        of seed_points()."""
        if not self.enabled or not symbol:
            return []
        with self._prints_lock:
            dq = self._prints.get(symbol.upper())
            if not dq:
                return []
            cutoff = dq[-1][0] - seconds
            return [p for p in dq if p[0] >= cutoff]

    def prints_after(self, symbol: str | None, after_ts: float) -> list:
        """(ts, price, size) prints strictly newer than `after_ts` -- the
        dark-tape fallback's incremental read (the caller keeps the
        high-water mark so nothing is fed twice)."""
        if not self.enabled or not symbol:
            return []
        with self._prints_lock:
            dq = self._prints.get(symbol.upper())
            if not dq:
                return []
            return [p for p in dq if p[0] > after_ts]

    def price(self, symbol: str | None) -> float | None:
        """A FRESH real last-trade price for `symbol`, or None. Staleness is
        gated here so the gate never vetoes against an old print (e.g. if
        the feed drops during a fast move)."""
        if not self.enabled or not symbol or self._state is None:
            return None
        try:
            with self._state.lock:
                d = self._state.prices.get(symbol)
            if not d or not d.get("price"):
                return None
            if time.time() - d.get("ts_unix", 0) > self.max_age:
                return None
            return float(d["price"])
        except Exception:                                      # noqa: BLE001
            return None


# --------------------------------------------------------- SDK depth feed ---

def _quiet_sdk_logs():
    """Keep the bridge's own [WEBULL] lines off the terminal — Rich's Live
    display owns it, and a log line written mid-frame shreds the dashboard.
    They go to book_feed.log instead; propagate=False keeps them away from
    root's stderr lastResort handler. (The SDK's own stdout handler is
    pre-empted in providers.webull._route_sdk_logs.)"""
    handler = logging.FileHandler(HERE / "book_feed.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    lg = logging.getLogger("webull_bridge")
    lg.handlers = [handler]
    lg.propagate = False
    lg.setLevel(logging.WARNING)


class BookFeed:
    """Daemon thread polling Webull OpenAPI depth quotes (10 levels) as the
    PRIMARY book source; the OCR pipeline stays as fallback and as the
    symbol detector. Removes OCR misreads from the confidence chain when
    it's live: no merged digits, no frozen-pixel re-stamps, real sizes.

    Needs the OpenAPI *Advanced Quotes* subscription - without it the SDK
    caps depth at 1 (top-of-book: no walls, no real imbalance), which
    get() treats as not-live so auto mode stays on OCR.

    config.json "book_source": "auto" (SDK when fresh, OCR otherwise),
    "ocr" (never poll), "sdk" (prefer SDK, still OCR when it's down).
    Credentials come from config/webull_bridge.json or WEBULL_APP_* env
    vars - the same plumbing the bridge uses."""

    def __init__(self, cfg: dict, console=None):
        self.mode = str(cfg.get("book_source", "auto")).lower()
        self.max_age = 1.5      # SDK book older than this -> OCR wins
        self.lock = threading.Lock()
        self._symbol: str | None = None
        self._book: L2Book | None = None
        self._got: float = 0.0
        self.ok = 0
        self.fail = 0
        self.md = None
        if self.mode == "ocr":
            return
        try:
            sys.path.insert(0, str(HERE.parent))
            from webull_bridge.config import load_config
            from webull_bridge.providers.webull import WebullMarketData
            _quiet_sdk_logs()
            self.md = WebullMarketData(load_config())
            threading.Thread(target=self._run, daemon=True).start()
            if console:
                console.print("[dim]SDK depth feed on — OpenAPI book "
                              "preferred, OCR fallback[/dim]")
        except Exception as e:                                 # noqa: BLE001
            self.md = None
            if console:
                style = "yellow" if self.mode == "sdk" else "dim"
                console.print(f"[{style}]SDK depth feed unavailable ({e}) — "
                              f"staying on OCR[/{style}]")

    @property
    def enabled(self) -> bool:
        return self.md is not None

    def set_symbol(self, symbol: str | None):
        if not symbol or self.md is None:
            return
        with self.lock:
            if symbol != self._symbol:
                self._symbol = symbol
                self._book = None      # never serve another symbol's book

    def get(self, now: float) -> L2Book | None:
        """The current SDK book if it's fresh and REAL depth (>1 level) -
        else None and the caller stays on OCR."""
        if self.md is None:
            return None
        with self.lock:
            if (self._book is not None and now - self._got <= self.max_age
                    and self.md.depth > 1):
                return self._book
        return None

    def _run(self):
        misses = 0
        last_sym = None
        while True:
            t0 = time.time()
            with self.lock:
                sym = self._symbol
            if sym != last_sym:     # new symbol, new chance: drop the backoff
                misses, last_sym = 0, sym
            if sym:
                try:
                    book = self.md._fetch(sym)
                except Exception:                              # noqa: BLE001
                    book = None
                with self.lock:
                    if book is not None and sym == self._symbol:
                        self._book, self._got = book, time.time()
                        self.ok += 1
                        misses = 0
                    elif book is None:
                        self.fail += 1
                        misses += 1
            poll = self.md.poll if self.md else 0.5
            # a book we can't fetch is a book OCR is already covering, so
            # ease off instead of billing the API 2x/sec for the same error
            if misses >= 5:
                poll = min(poll * misses, 10.0)
            time.sleep(max(0.05, poll - (time.time() - t0)))


def _fetch_watchlist(url: str) -> list:
    """Best-effort GET of the dashboard's /api/state; returns the momentum
    ticker symbols. Browser UA so Cloudflare doesn't 403; the feed's auth is
    off by default. Any failure -> [] (warm-start just no-ops for that poll)."""
    import urllib.request
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/api/state",
            headers={"Accept": "application/json", "User-Agent": ua})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
        return [s for s in (str(r.get("ticker") or "").upper().strip()
                            for r in (data.get("tickers") or [])) if s]
    except Exception:                                          # noqa: BLE001
        return []


# ---------------------------------------------------------------- alerts ----

# distinct tones per event: high = get in / take money, low = get out
TONES = {"BUY": (1200, 200), "TAKE_PROFIT": (1500, 180),
         "TRAIL_EXIT": (1000, 220), "SELL": (500, 350), "STOP": (400, 450)}


def beep(action: str):
    try:
        import winsound
        freq, dur = TONES.get(action, (800, 250))
        for _ in range(3):
            winsound.Beep(freq, dur)
    except Exception:
        print("\a", end="", flush=True)


def toast(sig: Signal):
    try:
        from plyer import notification
        notification.notify(
            title=f"L2 {sig.action} signal @ {sig.price:.3f}",
            message=sig.reason,
            timeout=8,
        )
    except Exception:
        pass  # toast is best-effort


def alert(sig: Signal, cfg: dict):
    if cfg.get("sound", True):
        threading.Thread(target=beep, args=(sig.action,), daemon=True).start()
    if cfg.get("toast", True):
        threading.Thread(target=toast, args=(sig,), daemon=True).start()


# ------------------------------------------------------------------- log ----

def _open_csv(path: Path, fields: list[str]):
    """Open a CSV for append; if an existing file's header doesn't match
    (schema changed, e.g. the confidence columns were added), the old file
    is archived as <stem>-old-<stamp>.csv instead of being silently mixed.

    If the archive rename fails because the log is locked -- almost always
    a second monitor still running, occasionally an editor/indexer -- the
    startup must NOT crash (that used to raise WinError 32 to the console).
    We fall back to a distinct <stem>-<stamp>.csv for this session so the
    new schema stays clean and the locked file is left untouched."""
    if path.exists():
        try:
            with open(path) as fh:
                first = fh.readline().strip()
        except Exception:                                  # noqa: BLE001
            first = ""
        if first != ",".join(fields):
            archived = path.with_name(
                f"{path.stem}-old-{datetime.now():%Y%m%d-%H%M%S}.csv")
            try:
                path.rename(archived)
            except OSError as e:                            # locked elsewhere
                path = path.with_name(
                    f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S}.csv")
                print(f"[warn] could not archive {archived.name} "
                      f"({getattr(e, 'strerror', None) or e}); the log is in "
                      "use -- is another monitor still running? Writing this "
                      f"session to {path.name} instead.")
    new = not path.exists()
    f = open(path, "a", newline="")
    w = csv.writer(f)
    if new:
        w.writerow(fields)
    return f, w


class CsvLog:
    # stance/agree/total/tape_live capture the 5m CONFIDENCE banner per row
    # so score_confidence.py can measure the FULL three-pillar signal after
    # the fact (the trend pillar alone is recoverable from price; tape and
    # vwap are not, so they must be logged live or they're lost).
    # trend/tape/vwap_vote are each pillar's own -1/0/1 vote (blank = the
    # pillar abstained), so the scorer can grade each pillar INDIVIDUALLY.
    # The raw-value tail (t5..quality) exists so thresholds can be swept
    # OFFLINE against forward returns -- votes alone can't be re-derived at
    # a different tape_dom_min / deadband, the raw numbers can. flow/absorb
    # are reserved for the book-flow detectors (logged before they vote).
    #
    # vwap_age/vwap_src are what the REBUILT vwap pillar is judged on. The
    # old pillar measured corr +0.43 against trend (it was the same witness
    # twice); the fix is only a mechanism, not yet a proven result, so the
    # age and the source of every VWAP that voted are logged to re-measure
    # that correlation on real hours. vwap_src="session" is the real
    # anchored VWAP, "tape" the reset-on-hop fallback that the maturity
    # gate should now be silencing -- if "tape" rows ever show a vwap_vote,
    # the gate has a hole. tape_age stays: it is the fallback's own age and
    # the column the 0-violation gate check was verified against.
    # The 0c tail (book_vote..ofi) starts the data clock for the staged
    # promotions: book_vote is BookFlow's would-be pillar vote logged
    # directly (cheaper for the scorer than re-deriving it from flow);
    # book_src/tape_src say which FEED produced the book (sdk/ocr) and the
    # tape (ocr/seed/finnhub) so signals can be conditioned on feed quality;
    # bias/play capture the market_bias/playbook layer so its imbalance
    # weighting can finally be graded against the pillars it contradicts;
    # micro_skew/ofi are reserved (blank) for the Phase-5 microstructure
    # signals. Changing FIELDS rotates l2_log.csv via _open_csv's header
    # check; score_confidence reads by column name so old archives still
    # score.
    FIELDS = ["time", "symbol", "best_bid", "best_ask", "spread_pct",
              "bid_size", "ask_size", "imbalance", "signal", "reason",
              "stance", "agree", "total", "tape_live",
              "trend_vote", "tape_vote", "vwap_vote",
              "t5", "tape_dom", "tape_dom_big", "tape_dom_w",
              "tape_sided_n", "tape_sided_share", "tape_pace", "tape_accel",
              "vwap", "vwap_age", "vwap_src", "tape_age", "quality",
              "flow", "absorb",
              "book_vote", "book_src", "tape_src", "bias", "play",
              "micro_skew", "ofi"]

    def __init__(self, path: Path):
        self.f, self.w = _open_csv(path, self.FIELDS)

    def write(self, book: L2Book, sig: Signal | None, symbol: str = "",
              lv: dict | None = None, raw: dict | None = None):
        votes = (lv or {}).get("votes") or {}
        raw = raw or {}

        def _v(k):  # pillar vote -> "" when the pillar had no opinion (None)
            x = votes.get(k)
            return "" if x is None else int(x)

        def _r(k, nd=4):  # raw value -> rounded or "" when unknown
            x = raw.get(k)
            return "" if x is None else round(x, nd)

        self.w.writerow([
            datetime.now().isoformat(timespec="milliseconds"), symbol,
            f"{book.best_bid:.4f}", f"{book.best_ask:.4f}",
            f"{book.spread_pct:.3f}", int(book.bid_size), int(book.ask_size),
            f"{book.imbalance:.3f}",
            sig.action if sig else "", sig.reason if sig else "",
            lv["stance"] if lv else "",
            lv.get("agree", "") if lv else "",
            lv.get("total", "") if lv else "",
            int(lv.get("tape_live", False)) if lv else "",
            _v("trend"), _v("tape"), _v("vwap"),
            _r("t5"), _r("tape_dom", 3), _r("tape_dom_big", 3),
            _r("tape_dom_w", 3), _r("tape_sided_n", 0),
            _r("tape_sided_share", 3), _r("tape_pace", 1),
            _r("tape_accel", 2), _r("vwap"), _r("vwap_age", 0),
            raw.get("vwap_src", ""), _r("tape_age", 0),
            _r("quality", 2), raw.get("flow", ""), raw.get("absorb", ""),
            _r("book_vote", 0), raw.get("book_src", ""),
            raw.get("tape_src", ""), _r("bias", 1), raw.get("play", ""),
            _r("micro_skew"), _r("ofi"),
        ])
        self.f.flush()


class TradeLog:
    """One row per completed round trip -> trades.csv. Review this with
    trade_stats.py to see which setups actually made money, then tune."""
    FIELDS = ["entry_time", "symbol", "entry", "exit_time", "exit",
              "pnl_pct", "reason"]

    def __init__(self, path: Path):
        self.f, self.w = _open_csv(path, self.FIELDS)

    def write(self, tr: Trade):
        self.w.writerow([
            datetime.fromtimestamp(tr.entry_ts).isoformat(timespec="seconds"),
            tr.symbol,
            f"{tr.entry:.4f}",
            datetime.fromtimestamp(tr.exit_ts).isoformat(timespec="seconds"),
            f"{tr.exit:.4f}", f"{tr.pnl_pct:+.2f}", tr.reason,
        ])
        self.f.flush()


# ------------------------------------------------------------- dashboard ----

def _vote_cell(label: str, v) -> str:
    """A pillar's vote as 'trend ▲' with the arrow colored by direction."""
    arrow = {1: "[green]▲[/green]", -1: "[red]▼[/red]",
             0: "[yellow]■[/yellow]"}.get(v, "[grey42]·[/grey42]")
    lab = "grey42" if v is None else "grey70"
    return f"[{lab}]{label}[/{lab}] {arrow}"


def wall_cells(price: float, ask_walls: list) -> list[str]:
    """The ask walls overhead as readable cells: `8.310 x12.4K +1.5%`.

    Price, size and distance - the three things that decide whether a
    wall is worth waiting for. The price is your trigger level, the size
    is what has to get chewed through, the distance is how far the pace
    has to carry you to reach it. Nearest first: that one is the trade.

    No real/spoof mark here, on purpose. BookFlow only rules a wall
    CONSUMED or PULLED once the level LEAVES the book, so a standing wall
    has no verdict by construction - a listed wall wearing one would be
    an artifact (the same OCR churn that inflates the pulled-wall count).
    The verdict lands on the flow note beside the pillars the moment the
    wall goes, which is when it means something."""
    return [f"[bold]{p:.3f}[/bold] [dim]x{_shares(z)}[/dim] "
            f"[dim]{100.0 * (p - price) / price:+.1f}%[/dim]"
            for p, z in ask_walls]


def _fixed(markup: str) -> Text:
    """A banner line that can never re-flow the panel.

    The layout's whole promise is a constant line count (see the Group at
    the end of lv_banner): a line that wraps silently becomes two and
    shoves everything below it down, which is the jitter this panel was
    built to stop. Rich wraps by default, so every line goes through here
    - a line too long for the pane loses its TAIL instead. That's safe:
    each line front-loads what matters (nearest wall first, grade before
    its reason), and the table underneath carries the full detail."""
    t = Text.from_markup(markup)
    t.no_wrap, t.overflow = True, "crop"
    return t


def lv_banner(lv: dict | None, price: float | None = None,
              ask_walls: list | None = None,
              proj: tuple | None = None) -> Panel:
    """THE signal: 5-minute confidence, big and unmissable at the top.

    Centered lines - a bold directional headline, a colored confidence
    gauge with the agree/total fraction, the three pillar votes, the
    current price projected 5m ahead at the current pace, the next ask
    walls overhead (your resistance / targets), and a dim meta line (held
    time / pending turn / no-tape). Everything else in the table below is
    supporting detail. Confidence = agreement of trend + tape + VWAP (see
    l2_core.confidence), not the size of any single meter.

    Projection and walls sit on adjacent lines on purpose: the projection
    is a PACE estimate, not a prediction (l2_core.project_price), and the
    walls directly above are the thing most likely to bend it."""
    if not lv:
        return Panel(
            "[bold grey70]WARMING UP[/bold grey70]   "
            "[dim]building the 5-minute picture …[/dim]",
            title="[dim]5m CONFIDENCE[/dim]", title_align="left",
            border_style="grey50", padding=(1, 2), expand=True)

    held = lv["held"]
    dur = (f"{int(held // 60)}m{int(held % 60):02d}s"
           if held >= 60 else f"{int(held)}s")
    agree, total = lv.get("agree", 0), lv.get("total", 0)
    tape_live = lv.get("tape_live", False)
    stance = lv["stance"]
    grade = quality_grade(lv.get("quality")) if "quality" in lv else None
    q_why = lv.get("quality_why") or []

    v = lv.get("votes", {})
    meta = lv.get("meta") or {}

    if stance == "NEUTRAL" or agree < 2:
        color = "yellow"
        headline = "◇   STAND ASIDE   ◇"
        sub = "no aligned edge"
    else:
        # grade C = the DATA under the read is suspect (frozen book, OCR
        # misses, wild spread...): never show the size-up headline on it
        strong = agree >= 3 and grade != "C"
        # early-hop tape lead: while the trend is voting off seeded pre-hop
        # history, a majority without the tape's own agreement is one
        # seeded witness dressed up -- hold the size-up headline (not the
        # lean) until the live tape confirms. lv["hop_tape_lead"] is set
        # by the main loop only inside hop_window after a switch.
        hop_held = (strong and lv.get("hop_tape_lead")
                    and not tape_confirms(stance, v.get("tape")))
        if hop_held:
            strong = False
        if stance == "LONG":
            color = "green"
            headline = "▲ ▲   GO / HOLD LONG   ▲ ▲" if strong else "▲   LEAN LONG"
            weak_note = "half size"
        else:  # BEAR
            color = "red"
            headline = ("▼ ▼   GET OUT / STAY OUT   ▼ ▼" if strong
                        else "▼   LEAN OUT")
            weak_note = "protect / trim"
        if hop_held:
            sub = "seeded majority — waiting on the tape to confirm"
        elif agree >= 3 and grade == "C":
            sub = ("all agree but data is grade C"
                   + (f" — {q_why[0]}" if q_why else ""))
        elif strong:
            sub = "trend, tape and VWAP all agree"
        elif not tape_live:
            sub = "trend & VWAP agree — unconfirmed by tape"
        else:
            sub = f"{agree} of {total} agree — {weak_note}"

    # confidence gauge: filled (colored) + empty (grey) pips + fraction
    pips = " ".join([f"[{color}]●[/{color}]"] * agree
                    + ["[grey42]○[/grey42]"] * max(0, total - agree))
    gauge = (f"{pips}    [bold {color}]{agree}/{total}[/bold {color}]"
             f"    [dim]{sub}[/dim]")

    # pillar cells with PROVENANCE: an abstaining pillar says why (still
    # warming) and a voting trend says when its history is seeded pre-hop
    # stream data -- a 1/1 ten seconds after a hop must not render like a
    # warmed 3/3 component (that was the warm-up mismatch).
    trend_cell = _vote_cell("trend", v.get("trend"))
    if v.get("trend") is not None:
        src = meta.get("trend_src")
        if src == "seed":
            trend_cell += " [dim](seeded)[/dim]"
        elif src == "mixed":
            trend_cell += " [dim](part-seed)[/dim]"
    tape_cell = _vote_cell("tape", v.get("tape"))
    tspan, twin = meta.get("tape_span"), meta.get("tape_window", 60.0)
    if v.get("tape") is None and tspan is not None and tspan < twin:
        tape_cell += f" [dim]warming {int(tspan)}s/{int(twin)}s[/dim]"
    vwap_cell = _vote_cell("vwap", v.get("vwap"))
    vage, vmin = meta.get("vwap_age"), meta.get("vwap_min_age")
    if v.get("vwap") is None and vage is not None and vmin and vage < vmin:
        vwap_cell += f" [dim]{vage / 60:.0f}m/{vmin / 60:.0f}m[/dim]"
    cells = [trend_cell, tape_cell, vwap_cell]
    if lv.get("book_pillar"):
        cells.append(_vote_cell("book", v.get("book")))
    pillars = "     ".join(cells)
    if lv.get("flow_note"):
        fcol = "red" if "spoof" in lv["flow_note"] else "cyan"
        pillars += f"   [{fcol}]· {lv['flow_note']}[/{fcol}]"

    # price now -> where the current pace lands 5m out. Kept on its own
    # line, not appended to the walls: this panel is narrow, and a line
    # that wraps re-flows the whole banner (the thing the fixed line count
    # below exists to prevent).
    px = f"[dim]now[/dim] [bold]{price:.3f}[/bold]" if price else "[dim]now  —[/dim]"
    # NO 5m TARGET HERE, on purpose. Backtested over the 169 logged
    # sessions, project_price's target lands FARTHER from the truth
    # than assuming no change at all in 3 of every 4 independent calls
    # (median error 17% vs 6%), and its direction is a coin flip
    # (44%, CI 34-54). It extrapolates 120s of drift across 5 minutes,
    # multiplying by 2.5 a number that is mostly noise at this scale -
    # a noise amplifier, not a forecast. What survives is the trailing
    # PACE: a measured fact about what price just did, with no
    # destination for the eye to trade toward.
    pace_val = None
    pace_txt = "[dim]—   building pace[/dim]"   # needs ~20s of history
    if proj and price:
        p_mid, p_target, _ = proj
        chg = 100.0 * (p_target - p_mid) / p_mid if p_mid else 0.0
        pace_val = chg / (PROJ_MINUTES or 5.0)      # %/min
        # Colour by AGREEMENT with the stance, not by the sign of the
        # arithmetic: green means "aligned upside" everywhere else in this
        # banner, and a bare pace has not earned the right to say it.
        agrees = ((pace_val > 0.01 and color == "green")
                  or (pace_val < -0.01 and color == "red"))
        pc = color if agrees else "yellow"
        pace_txt = f"[bold {pc}]{pace_val:+.2f}%/min[/bold {pc}]"

    if pace_val is not None:
        price_line = f"{px}   {pace_txt}   [dim]trailing 2m[/dim]"
    else:
        price_line = f"{px}   {pace_txt}"

    # the next ask walls overhead (resistance / targets) = what the pace
    # above has to get through. Walls are listed with their size and
    # distance and left at that. Marking which one is "in the way" needed
    # a projected target to be in the way OF, and that target is noise
    # (see price_line) - it would have dressed a coin flip up as geometry.
    if not price:
        walls_line = "[dim]walls  —[/dim]"
    elif ask_walls:
        walls_line = ("[dim]ask walls ↑[/dim]   "
                      + "   ".join(wall_cells(price, ask_walls)))
    else:
        walls_line = "[green]clear overhead — no ask walls in view[/green]"

    # every clause here is short and 2-space separated on purpose: at the
    # narrow end of the pane this line carries four of them, and the tail
    # (the pending turn - the most actionable of the four) is what a crop
    # would eat first
    meta = f"[dim]held {dur}[/dim]"
    if grade is not None:
        gcol = {"A": "green", "B": "yellow", "C": "red"}[grade]
        meta += f"  [{gcol}]data {grade}[/{gcol}]"
        if grade != "A" and q_why:
            meta += f" [dim]· {q_why[0]}[/dim]"
    if not lv.get("tape_live", False):
        # the "show Time&Sales" fix-it hint lives on the table's Tape row:
        # it's a one-time instruction, and repeating it here every frame
        # was long enough to wrap the line
        meta += "  [yellow]· no tape[/yellow]"
    if lv.get("pending"):
        pend = {"LONG": "LONG", "BEAR": "OUT"}.get(lv["pending"], lv["pending"])
        meta += (f"  [dim]→[/dim] [bold {color}]{pend}[/bold {color}] "
                 f"[dim]{int(lv['pending_left'])}s[/dim]")

    # Left-anchored, fixed line count so nothing slides when text changes
    # length: centering re-offsets every line each frame (the jarring shift);
    # a constant 6 lines stops vertical reflow when price/proj/pending come
    # and go (each renders a placeholder rather than dropping its line).
    # _fixed() enforces it - see there for why a long line crops.
    body = Group(*(_fixed(ln) for ln in (
        f"[bold {color}]{headline}[/bold {color}]",
        gauge,
        pillars,
        price_line,
        walls_line,
        meta,
    )))
    return Panel(body, title=f"[bold {color}]5m CONFIDENCE[/bold {color}]",
                 title_align="left", border_style=color, padding=(1, 2),
                 expand=True)


def sparkline(vals: list[float], width: int = 40) -> str:
    """Unicode mini-chart of recent mid-prices."""
    if len(vals) < 2:
        return ""
    blocks = "▁▂▃▄▅▆▇█"
    vals = vals[-width:]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return "▄" * len(vals)
    return "".join(blocks[int((v - lo) / (hi - lo) * 7)] for v in vals)


def bucket_mids(history, window: float, buckets: int = 40) -> list[float]:
    """Median mid per time bucket over the trailing window, oldest first.
    Feeds the sparkline the SHAPE of the last 5 minutes instead of just
    the last ~15 seconds of reads; empty buckets are skipped."""
    hist = [b for b in history if b.ts >= history[-1].ts - window] \
        if len(history) >= 2 else []
    if len(hist) < 2:
        return []
    start = hist[-1].ts - window
    cells: list[list[float]] = [[] for _ in range(buckets)]
    for b in hist:
        i = min(buckets - 1, max(0, int((b.ts - start) / window * buckets)))
        cells[i].append(b.mid)
    out = []
    for c in cells:
        if c:
            c.sort()
            out.append(c[len(c) // 2])
    return out


def _bias_meter(score: float, width: int = 21) -> str:
    """bearish ──────●───┼────────── bullish gauge, -100..+100."""
    pos = int(round((max(-100.0, min(100.0, score)) + 100) / 200 * (width - 1)))
    cells = ["─"] * width
    cells[width // 2] = "┼"
    cells[pos] = "●"
    return "".join(cells)


def _trend_cell(pct: float | None) -> str:
    if pct is None:
        return "…"
    arrow = "▲" if pct > 0.05 else "▼" if pct < -0.05 else "→"
    c = "green" if pct > 0.05 else "red" if pct < -0.05 else "yellow"
    return f"[{c}]{arrow} {pct:+.2f}%[/{c}]"


def _shares(v: float) -> str:
    return f"{v / 1000:.1f}K" if v >= 1000 else f"{int(v)}"


def _tape_cell(m: dict) -> str:
    """One tape window -> 'B 62% / S 38%   12.4K sh   34 prints'."""
    sided = m["buy"] + m["sell"]
    if not m["n"] or not sided:
        return "[dim]no prints[/dim]"
    bp = 100.0 * m["buy"] / sided
    c = ("green" if m["dom"] >= 0.25 else "red" if m["dom"] <= -0.25
         else "yellow")
    return (f"[{c}]B {bp:.0f}% / S {100 - bp:.0f}%[/{c}]   "
            f"{_shares(m['total'])} sh   {m['n']} prints")


def render(book: L2Book | None, last_sig: Signal | None, reads: int,
           misses: int, hz: float, trader: PaperTrader | None = None,
           session_pnl: float = 0.0, n_trades: int = 0,
           trend1: float | None = None, trend5: float | None = None,
           spark: str = "", symbol: str | None = None,
           wall_events: list | None = None,
           lv: dict | None = None, proj: tuple | None = None,
           glitches: int = 0, span5: tuple | None = None,
           spark_label: str = "Price (recent)",
           tape: dict | None = None, cfg: dict | None = None,
           book_src: str = "ocr") -> Group:
    title = (f"Webull L2 Monitor [bold cyan]{symbol or '?'}[/bold cyan]"
             f"   {hz:.1f} reads/s   ok:{reads} miss:{misses}")
    if book_src == "sdk":
        title += "   [green]book:SDK[/green]"
    if glitches:
        title += f" glitch:{glitches}"
    # expand=True + fixed Metric width pins the table to the pane width and
    # the Value column's right edge, so numbers update IN PLACE instead of the
    # whole column resizing to the current values (the frame-to-frame jump).
    t = Table(title=title, expand=True)
    t.add_column("Metric", no_wrap=True, width=16)
    t.add_column("Value", justify="right", ratio=1, overflow="fold")
    if session_line:
        t.add_row("⏱ Session", session_line())
    if book:
        tape_m1 = tape["m1"] if tape and tape.get("on") else None
        gate_kw = {"tape_min_sided": int((cfg or {}).get("tape_min_sided", 4)),
                   "tape_sided_share": float((cfg or {}).get(
                       "tape_sided_share", 0.5))}
        # one-glance verdict, always the first thing you see
        score, label, why = market_bias(book, trend1, trend5, tape=tape_m1,
                                        **gate_kw)
        bc = ("green" if label == "BULLISH"
              else "red" if label == "BEARISH" else "yellow")
        arrow = ("▲" if label == "BULLISH"
                 else "▼" if label == "BEARISH" else "→")
        t.add_row("MARKET BIAS",
                  f"[bold {bc}]{arrow} {label} ({score:+.0f})[/bold {bc}]")
        t.add_row("", f"[{bc}]{_bias_meter(score)}[/{bc}]  [dim]{why}[/dim]")

        # the daily-use playbook: 4 checks -> 1 action
        pb = playbook(book, trend1, trend5, wall_events or [], tape_m1,
                      **gate_kw)
        c1 = ("[green]✓ arrows agree ▲[/green]" if pb["trend_up"]
              else "[red]✓ arrows agree ▼[/red]" if pb["trend_dn"]
              else "[dim]✗ arrows disagree[/dim]")
        c2 = ("[green]✓ imbalance confirms[/green]" if pb["imb_up"]
              else "[red]✓ imbalance confirms[/red]" if pb["imb_dn"]
              else "[dim]✗ imbalance neutral[/dim]")
        if pb["ask_break"]:
            c3 = ("[green]● ask wall breaking @ "
                  + ", ".join(f"{p:.3f}" for p in pb["ask_break"]) + "[/green]")
        elif pb["bid_crack"]:
            c3 = ("[red]● support cracking @ "
                  + ", ".join(f"{p:.3f}" for p in pb["bid_crack"]) + "[/red]")
        else:
            c3 = "[dim]walls holding[/dim]"
        if pb["tape_ok"]:
            c4 = ("[green]✓ tape buying[/green]" if pb["tape_up"]
                  else "[red]✓ tape selling[/red]" if pb["tape_dn"]
                  else "[dim]✗ tape mixed[/dim]")
        else:
            c4 = "[dim]— tape n/a[/dim]"
        t.add_row("Checklist", f"{c1}   {c2}   {c3}   {c4}")
        plays = {
            "LONG_TRIGGER": "[bold green]GO LONG — trend+flow aligned and "
                            "the wall above is breaking[/bold green]",
            "LONG_SETUP": "[green]LONG SETUP — aligned; wait for the ask "
                          "wall to break[/green]",
            "BEAR_CRACK": "[bold red]GET OUT — support cracking with "
                          "bearish flow[/bold red]",
            "BEARISH": "[red]BEARISH — stand aside / protect longs[/red]",
            "STAND_ASIDE": "[yellow]STAND ASIDE — checks don't line "
                           "up[/yellow]",
        }
        t.add_row("Play", plays[pb["verdict"]])

        # (the 5m confidence stance now lives in the banner above — no
        # duplicate row here)
        imb = book.imbalance
        color = "green" if imb > 1.3 else "red" if imb < 0.7 else "yellow"
        t.add_row("Best bid / ask", f"{book.best_bid:.3f} / {book.best_ask:.3f}")
        t.add_row("Spread", f"{book.spread:.3f} ({book.spread_pct:.2f}%)")
        t.add_row("Bid size (10 lvl)", f"{int(book.bid_size):,}")
        t.add_row("Ask size (10 lvl)", f"{int(book.ask_size):,}")
        t.add_row("Imbalance", f"[{color}]{imb:.2f}[/{color}]")
        walls = book.walls()
        t.add_row("Walls", "; ".join(f"{s} {p:.3f} x{int(z):,}"
                                     for s, p, z in walls) or "none")
        if tape:
            if tape.get("on"):
                t.add_row("Tape 1m", _tape_cell(tape["m1"]))
                t.add_row("Tape 5m", _tape_cell(tape["m5"]))
                vw = tape.get("vwap")
                if vw:
                    dv = 100.0 * (book.mid - vw) / vw
                    vc = ("green" if dv > 0.05 else "red" if dv < -0.05
                          else "yellow")
                    t.add_row("VWAP (tape)",
                              f"{vw:.3f}   mid [{vc}]{dv:+.2f}%[/{vc}] vs "
                              "vwap  [dim]since "
                              f"{datetime.fromtimestamp(tape['since']):%H:%M}"
                              "[/dim]")
            else:
                t.add_row("Tape", "[dim]Time&Sales widget not located — "
                                  "keep it visible next to L2[/dim]")
        move = f"{_trend_cell(trend1)}   {_trend_cell(trend5)}"
        if trend5 is None and span5 and span5[0] > 0:
            move += (f"  [dim]warming: {span5[0] / 60:.1f}m of "
                     f"{span5[1] / 60:.0f}m[/dim]")
        t.add_row("Move 1m / 5m", move)
        if proj:
            # was "Price -> proj (5m)": a 5-minute target price. It read
            # like a forecast and was not one - see lv_banner for the
            # backtest that retired it. The pace it was built from is real,
            # so that is what the row reports now.
            mid, target, note = proj
            chg = 100.0 * (target - mid) / mid if mid else 0.0
            pace = chg / PROJ_MINUTES
            pc = ("green" if pace > 0.01 else "red" if pace < -0.01
                  else "yellow")
            t.add_row("Pace (trailing 2m)",
                      f"[{pc}]{pace:+.2f}%/min[/{pc}]"
                      + (f"  [dim]{note}[/dim]" if note else "")
                      + "  [dim]what price just did, not a forecast[/dim]")
        if spark:
            t.add_row(spark_label, spark)
        if trader and trader.in_position:
            up = trader.unrealized_pct(book)
            pc = "green" if up >= 0 else "red"
            tag = ("REAL LONG" if getattr(trader, "real", False)
                   else "paper LONG")
            t.add_row("Position",
                      f"[{pc}]{tag} @ {trader.entry:.3f}   "
                      f"uPnL {up:+.2f}%   peak {trader.peak_pct():+.2f}%[/{pc}]")
        else:
            t.add_row("Position", "flat")
        if n_trades:
            sc = "green" if session_pnl >= 0 else "red"
            t.add_row("Session (paper)",
                      f"[{sc}]{session_pnl:+.2f}% over {n_trades} trades[/{sc}]")
    else:
        t.add_row("Status", "[red]no clean OCR read yet[/red]")
    price = ask_walls = None
    if book:
        price = book.mid
        # next 3 ask walls overhead, nearest first (resistance / targets)
        ask_walls = sorted(((p, z) for s, p, z in book.walls() if s == "ASK"),
                           key=lambda x: x[0])[:3]
    return Group(lv_banner(lv, price, ask_walls, proj), t)


# ------------------------------------------------------------------ main ----

def main():
    cfg = load_config()
    engine = SignalEngine(cfg)
    trader = PaperTrader(cfg)
    # csv_name lets an A/B session log to its own file (e.g. warm_on.csv /
    # warm_off.csv) so compare_ab.py can score the two runs side by side
    log = (CsvLog(HERE / cfg.get("csv_name", "l2_log.csv"))
           if cfg.get("csv_log", True) else None)
    tlog = TradeLog(HERE / "trades.csv") if cfg.get("csv_log", True) else None
    interval = cfg.get("poll_interval", 0.35)
    console = Console()

    reads = misses = 0
    last_sig: Signal | None = None
    book: L2Book | None = None
    stamps: list[float] = []
    session_pnl = 0.0
    n_trades = 0
    walltrack = WallTracker(cfg.get("wall_multiple", 4.0))
    bookflow = BookFlow(cfg.get("wall_multiple", 4.0))
    flow: dict | None = None   # last BookFlow read (events/absorb/vote)
    flow_note: tuple | None = None   # (ts, text) last wall verdict, held 10s
    recent_events: list = []   # [(ts, event)] kept ~10s so you can react
    longview = LongView(cfg)
    lv: dict | None = None
    proj: tuple | None = None
    gate = GlitchGate(cfg.get("glitch_jump_pct", 1.5),
                      cfg.get("glitch_confirm", 2),
                      ref_tol_pct=cfg.get("glitch_ref_tol_pct", 15.0))
    frame_key: tuple | None = None       # (region, crc32) of the last grab
    frame_book: L2Book | None = None     # what that frame parsed to
    # data-health windows for signal_quality(): the frame-skip path re-stamps
    # unchanged pixels as a fresh book, so staleness must be tracked HERE --
    # frame_changed is the wall time the pixels last actually changed
    frame_changed: float | None = None
    ok_hist: deque = deque()             # (ts, parse ok?) last 60s
    drop_hist: deque = deque()           # ts of each GlitchGate drop, 60s
    last_dropped = 0

    console.print("[bold]Starting — Ctrl+C to stop.[/bold] "
                  "Keep the L2 panel visible and unobstructed.")

    # real-position feed runs as a daemon thread so it lives and dies
    # with this window; exits then anchor to actual broker fills
    sys.path.insert(0, str(HERE.parent))
    try:
        from webull_bridge.position_feed import start_daemon
        if start_daemon():
            console.print("[dim]position feed on — exits anchor to real "
                          "broker fills[/dim]")
    except Exception as e:
        console.print(f"[dim]position feed unavailable: {e}[/dim]")
    tracker = RegionTracker(cfg, console)
    reffeed = RefFeed(cfg, console)
    tsreader = TapeReader(cfg, console)
    bookfeed = BookFeed(cfg, console)
    if tsreader.enabled:
        console.print("[dim]Time&Sales reader on — executed prints feed "
                      "the tape rows, checklist, and VWAP[/dim]")
    active_sym: str | None = None      # symbol the current state belongs to
    switch_ts: float | None = None     # when the current symbol arrived --
    # drives the early-hop tape-lead gate (hop_window) on the banner
    force_reset = False    # jump guard tripped: reset state next iteration
    veto_streak = 0        # consecutive ref-price gate rejections

    def _symbol_refresher():
        # keeps the displayed ticker current when you switch stocks
        with mss.MSS() as s2:
            while True:
                time.sleep(cfg.get("symbol_refresh", 30))
                rect = tracker.win_rect
                if not rect:
                    continue
                try:
                    tracker.note_symbol(detect_symbol(np.asarray(s2.grab(rect))))
                except Exception:
                    pass

    threading.Thread(target=_symbol_refresher, daemon=True).start()

    def _warm_watchlist():
        # pre-subscribe the momentum monitor's live list to the trade stream
        # so each symbol's trend history warms in the background; on a switch
        # seed_points() then makes the 5m trend valid on arrival (vs ~3 min
        # blind). Best-effort: a down feed just leaves us at live warm-up.
        url = (cfg.get("dashboard_url")
               or os.getenv("DASHBOARD_URL", "https://trading.jbrasfield.com"))
        while True:
            try:
                syms = _fetch_watchlist(url)
                if syms:
                    reffeed.set_watchlist(syms)
            except Exception:                                  # noqa: BLE001
                pass
            time.sleep(max(5.0, float(cfg.get("warm_poll", 12))))

    if reffeed.enabled and cfg.get("warm_start", True):
        threading.Thread(target=_warm_watchlist, daemon=True).start()
        console.print("[dim]warm-start on — pre-subscribing the momentum "
                      "watchlist so trends seed instantly on switch[/dim]")

    with mss.MSS() as sct, Live(render(None, None, 0, 0, 0),
                                console=console, refresh_per_second=4) as live:
        while True:
            t0 = time.time()
            region = tracker.get(sct)
            if region is None:
                live.update(render(None, last_sig, reads, misses, 0.0))
                time.sleep(1.0)
                continue
            if tracker.symbol != active_sym or force_reset:
                old, active_sym = active_sym, tracker.symbol
                switch_ts = t0
                if old is not None:
                    # trends, streaks, walls, tape, and any virtual entry
                    # computed on the old stock are garbage for the new one
                    engine.reset()
                    walltrack.state.clear()
                    bookflow = BookFlow(cfg.get("wall_multiple", 4.0))
                    flow = flow_note = None
                    recent_events.clear()
                    longview = LongView(cfg)
                    gate = GlitchGate(cfg.get("glitch_jump_pct", 1.5),
                                      cfg.get("glitch_confirm", 2),
                                      ref_tol_pct=cfg.get("glitch_ref_tol_pct",
                                                          15.0))
                    tsreader.reset()
                    frame_key = frame_book = None
                    frame_changed = None
                    ok_hist.clear()
                    drop_hist.clear()
                    last_dropped = 0
                    book = lv = proj = last_sig = None
                    dropped = trader.drop_virtual()
                    console.print(
                        f"[dim]symbol switch {old} → {active_sym}: history "
                        "reset" + (", virtual position dropped" if dropped
                                   else "") + "[/dim]")
                # seed the new symbol's trend from stream history collected
                # while it sat on the watchlist -> valid 5m read on arrival
                # instead of ~3 min blind. Runs on the first switch too;
                # empty (symbol not pre-subbed) falls back to live warm-up.
                # NOT on a jump-reset whose title re-read didn't flip the
                # label (old == active_sym): the label is suspect there, and
                # seeding it would pour the WRONG symbol's history into the
                # state that was just wiped to stop exactly that.
                if old != active_sym:
                    seeds = reffeed.seed_points(
                        active_sym, cfg.get("trend_window", 300))
                    if seeds:
                        engine.seed_history(seeds)
                        console.print(
                            f"[dim]seeded {len(seeds)} stream pts → "
                            f"{active_sym}: trend warm on arrival[/dim]")
                    # the tape gets the same treatment: pre-hop stream
                    # prints so the 60s dominance has real executed
                    # evidence on arrival instead of the tape pillar
                    # sitting dark through your whole 2-3 minute visit
                    if tsreader.enabled and cfg.get("tape_seed_finnhub",
                                                    True):
                        tape_pts = reffeed.seed_prints(
                            active_sym, tsreader.tape.keep)
                        if tape_pts:
                            n_seeded = tsreader.seed(tape_pts)
                            if n_seeded:
                                console.print(
                                    f"[dim]seeded {n_seeded} stream prints "
                                    f"→ {active_sym}: tape warm on "
                                    "arrival[/dim]")
                force_reset = False
            reffeed.ensure(tracker.symbol)
            raw = np.asarray(sct.grab(region))
            key = (region["left"], region["top"], region["width"],
                   region["height"], zlib.crc32(raw.tobytes()))
            if key == frame_key:
                # pixels unchanged since the last poll -> skip Tesseract
                # entirely and re-stamp the previous parse (same market
                # state, fresh timestamp). Big CPU win on a quiet tape.
                # frame_changed stays put: signal_quality() turns a long
                # stretch of identical pixels (halted stock, obscured
                # panel) into a visible grade drop instead of the book
                # silently voting forever on frozen data.
                b = (L2Book(frame_book.bids, frame_book.asks)
                     if frame_book else None)
            else:
                b = ocr_image(raw)
                frame_key, frame_book = key, b
                frame_changed = t0
            ok = b is not None
            tracker.report(ok)
            ok_hist.append((t0, ok))
            while ok_hist and t0 - ok_hist[0][0] > 60.0:
                ok_hist.popleft()
            nd = gate.dropped - last_dropped
            if nd > 0:
                drop_hist.extend([t0] * nd)
            last_dropped = gate.dropped
            while drop_hist and t0 - drop_hist[0] > 60.0:
                drop_hist.popleft()
            ref_px = reffeed.price(tracker.symbol)
            if ok and not gate.accept(b, ref_px):
                b = None   # isolated / ref-contradicted frame -> held back
                # a LONG rejection streak is the hop signature for STREAMED
                # symbols: you switched panels, every new-symbol frame
                # contradicts the old symbol's reference price, and the
                # monitor would sit blind until the ~30s title refresh.
                # Re-read the title early instead (twice: note_symbol needs
                # two matching reads to flip); the label flipping triggers
                # the normal switch reset+seed path above.
                veto_streak += 1
                if veto_streak >= int(cfg.get("hop_veto_reads", 8)):
                    veto_streak = 0
                    for _ in range(2):
                        try:
                            tracker.note_symbol(detect_symbol(
                                np.asarray(sct.grab(tracker.win_rect))))
                        except Exception:                      # noqa: BLE001
                            break
            elif ok:
                veto_streak = 0
            # SDK book preferred when live: cleaner numbers, and it keeps
            # the signal fed even when the OCR panel is obscured. The OCR
            # health tracking above still runs so a later fallback is
            # graded honestly.
            book_sdk = False
            if bookfeed.enabled:
                bookfeed.set_symbol(tracker.symbol)
                nb = bookfeed.get(t0)
                if nb is not None:
                    b = nb
                    book_sdk = True
            # undetected-hop guard for UN-streamed symbols (no reference
            # price to contradict, so the glitch gate can't catch it): the
            # title OCR refreshes every ~symbol_refresh seconds, and a hop
            # faster than that feeds the old symbol's trend/vwap/walls with
            # the NEW symbol's book -- live logs showed one "RGNT" segment
            # blending 2.5 -> 1.6 -> 13.4 books, pegging the pace at its
            # clamp and minting fake PULLED-wall spoof events. A persistent
            # (gate-accepted) mid jump this big is a hop, not a move:
            # re-read the title now, wipe the state next iteration, and
            # drop this frame rather than feed it to anyone.
            if (b is not None and book is not None and book.mid
                    and not force_reset):
                jump = abs(b.mid - book.mid) / book.mid
                if jump > float(cfg.get("hop_jump_pct", 20.0)) / 100.0:
                    for _ in range(2):
                        try:
                            tracker.note_symbol(detect_symbol(
                                np.asarray(sct.grab(tracker.win_rect))))
                        except Exception:                      # noqa: BLE001
                            break
                    force_reset = True
                    console.print(
                        f"[dim]book jumped {100 * jump:.0f}% with no symbol "
                        "change — undetected hop: title re-read, state "
                        "reset[/dim]")
                    b = None
            if b:
                reads += 1
                book = b
                if tsreader.enabled:
                    tsreader.set_quote(b.best_bid, b.best_ask, t0)
                    # dark-tape fallback: when the OCR panel has produced
                    # no print for 30s but the stream is flowing, feed the
                    # tape from the stream (never both at once -- OCR is
                    # preferred the moment it wakes back up)
                    if (cfg.get("tape_fallback_finnhub", True)
                            and tsreader.ocr_dark(t0, 30.0)):
                        fpts = reffeed.prints_after(active_sym,
                                                    tsreader.stream_upto)
                        if fpts:
                            tsreader.feed_stream(fpts, t0)
                tsnap = tsreader.snapshot(t0) if tsreader.enabled else None
                tape_m1 = tsnap["m1"] if tsnap and tsnap.get("on") else None
                sig = engine.update(b, tape_m1)
                # anchor exits to the real broker position when the feed
                # is fresh (None = feed unknown -> leave the trader alone)
                real = read_real_position(tracker.symbol)
                if real is not None:
                    trader.sync_real(real or None, b)
                exit_sig, trade = trader.update(b, sig, active_sym or "")
                # exit alerts take priority (they're anchored to your entry)
                show = exit_sig or sig
                if exit_sig:
                    last_sig = exit_sig
                    alert(exit_sig, cfg)   # no-op unless re-enabled in config
                elif sig and not (sig.action == "SELL" and trade):
                    last_sig = sig
                    alert(sig, cfg)        # no-op unless re-enabled in config
                evs = walltrack.update(b)
                for ev in evs:
                    recent_events.append((t0, ev))
                recent_events[:] = [(ts, e) for ts, e in recent_events
                                    if t0 - ts <= 10]
                # book x tape fusion: walls eaten vs pulled + absorption.
                # Display+log only until cfg book_pillar=true (log first,
                # vote later -- score_confidence decides the promotion).
                flow = bookflow.update(
                    b, tsreader.prints_since(bookflow.upto)
                    if tsreader.enabled else [], t0)
                t5_now = engine.trend_pct(cfg.get("trend_window", 300))
                # The vwap pillar prefers the REAL session VWAP: accumulated
                # from executed prints, anchored to the session open, and
                # kept across symbol hops. The local tape VWAP is only a
                # fallback for when the stream has never seen this symbol,
                # and it is deliberately left on its own (short) age - under
                # the maturity gate a tape VWAP that resets every hop can
                # essentially never qualify, so the pillar abstains instead
                # of echoing the trend. Fewer pillars, honestly counted,
                # beats three where two are the same witness.
                sv, sv_age = reffeed.vwap(active_sym)
                if sv is not None:
                    vwap_now, vwap_age_now = sv, sv_age
                else:
                    vwap_now = tsnap["vwap"] if tsnap else None
                    # clamped: t0 is stamped BEFORE the switch-block reset,
                    # so right after a hop tape.started can be a couple of
                    # seconds ahead of it (live logs showed age -2.0)
                    vwap_age_now = (max(0.0, t0 - tsnap["since"])
                                    if tsnap and tsnap.get("on") else None)
                lv = longview.update(
                    b, t5_now,
                    evs, t0, tape=tape_m1,
                    vwap=vwap_now, vwap_age=vwap_age_now,
                    book_vote=flow["vote"] if flow else None,
                    trend_seed_frac=engine.seed_fraction(
                        cfg.get("trend_window", 300)),
                    tape_span=max(0.0, t0 - tsnap["since"])
                    if tsnap and tsnap.get("on") else None,
                    vwap_src="session" if sv is not None else "tape")
                # early-hop tape lead: inside hop_window after a switch,
                # while the trend still votes off seeded history, the
                # banner's size-up headline needs the tape to agree
                lv["hop_tape_lead"] = (
                    bool(cfg.get("hop_tape_lead", True))
                    and switch_ts is not None
                    and t0 - switch_ts <= float(cfg.get("hop_window", 120))
                    and lv["meta"]["trend_src"] != "live")
                # data-quality grade rides along with the stance: the
                # banner shows it and caps size-up advice on grade C
                n_ok = sum(1 for _, o in ok_hist if o)
                lp = tsnap.get("last_print") if tsnap else None
                q, q_why = signal_quality(
                    frame_age=(t0 - frame_changed)
                    if frame_changed is not None else None,
                    ok_rate=n_ok / len(ok_hist) if ok_hist else None,
                    glitch_rate=(len(drop_hist) / len(ok_hist))
                    if ok_hist else None,
                    tape_print_age=(t0 - lp)
                    if (lp and tsnap and tsnap.get("on")) else None,
                    ref_dev_pct=(100.0 * abs(b.mid - ref_px) / ref_px)
                    if ref_px else None,
                    have_ref=ref_px is not None,
                    spread_pct=b.spread_pct,
                    spoof_events=(flow["spoof_bid"] + flow["spoof_ask"])
                    if flow else 0,
                    sdk_book=book_sdk)
                lv["quality"] = q
                lv["quality_why"] = q_why
                # flow verdicts ride along for the banner detail; a wall
                # event is a moment, so it holds on screen ~10s. Kept
                # SHORT: this shares the pillars line, and a long note
                # pushed it past the pane where it got cropped mid-word.
                if flow and flow["events"]:
                    s_, p_, v_ = flow["events"][-1]
                    flow_note = (t0, f"{s_.lower()} {p_:.3f} "
                                 + ("spoofed" if v_ == "PULLED" else "eaten"))
                if flow_note and t0 - flow_note[0] <= 10.0:
                    lv["flow_note"] = flow_note[1]
                elif flow and flow["absorb"]:
                    lv["flow_note"] = ("bid absorption — accumulation"
                                       if flow["absorb"] > 0 else
                                       "ask absorption — distribution")
                # projection: current pace extended 5 min, with the wall
                # standing in its way (if any) noted
                proj = None
                pp = project_price(engine.history,
                                   cfg.get("projection_minutes", 5.0))
                if pp:
                    mid, target = pp
                    note = ""
                    wm = cfg.get("wall_multiple", 4.0)
                    if target > mid:
                        blockers = [p for s, p, _ in b.walls(wm)
                                    if s == "ASK" and mid < p < target]
                        if blockers:
                            note = f"ask wall {min(blockers):.3f} in the way"
                    elif target < mid:
                        blockers = [p for s, p, _ in b.walls(wm)
                                    if s == "BID" and target < p < mid]
                        if blockers:
                            note = f"bid wall {max(blockers):.3f} may hold"
                    proj = (mid, target, note)

                # bias/playbook are computed here (not inside the publish
                # try) so the CSV logs them too -- the 0c columns exist to
                # grade this layer's imbalance weighting against the
                # pillar layer it currently contradicts
                gate_kw = {
                    "tape_min_sided": int(cfg.get("tape_min_sided", 4)),
                    "tape_sided_share": float(cfg.get(
                        "tape_sided_share", 0.5))}
                score, _, _ = market_bias(b, engine.trend_pct(60),
                                          engine.trend_pct(300),
                                          tape=tape_m1, **gate_kw)
                pb = playbook(b, engine.trend_pct(60),
                              engine.trend_pct(300),
                              [e for _, e in recent_events], tape_m1,
                              **gate_kw)
                # publish live state so the TradingView monitor can build
                # a combined chart+orderflow master verdict
                try:
                    (HERE.parent / "l2_state.json").write_text(json.dumps({
                        "ts": t0, "symbol": tracker.symbol,
                        "bias": round(score, 1), "play": pb["verdict"],
                        "confidence": (f"{lv['stance']} {lv['agree']}/"
                                       f"{lv['total']}") if lv else None,
                        "imbalance": round(b.imbalance, 2),
                        "tape_dom": (round(tape_m1["dom"], 2)
                                     if tape_m1 else None),
                        "vwap": (round(vwap_now, 4)
                                 if vwap_now else None),
                        "price": round(proj[0], 4) if proj else None,
                        # NO projected target here. project_price's 5m
                        # target graded worse than "no change" on the
                        # logged sessions (median error 17% vs 6%,
                        # direction a coin flip) -- publishing it had a
                        # downstream display trading toward a number this
                        # code itself calls noise. The trailing pace
                        # (%/min, a measured fact) is what survives.
                        "pace_pct_min": (round(100.0 * (proj[1] - proj[0])
                                               / proj[0] / PROJ_MINUTES, 3)
                                         if proj and proj[0] else None),
                        "stance": lv["stance"] if lv else None,
                        "quality": (round(lv["quality"], 2)
                                    if lv and "quality" in lv else None),
                        "quality_note": ((lv.get("quality_why") or [""])[0]
                                         if lv else None),
                        "book_src": "sdk" if book_sdk else "ocr",
                        "flow_note": lv.get("flow_note") if lv else None,
                        "pos": ({"real": trader.real,
                                 "entry": round(trader.entry, 4),
                                 "upnl": round(trader.unrealized_pct(b), 2),
                                 "peak": round(trader.peak_pct(), 2)}
                                if trader.in_position else None)}))
                except Exception:
                    pass
                if trade:
                    session_pnl += trade.pnl_pct
                    n_trades += 1
                    if tlog:
                        tlog.write(trade)
                if log:
                    sided_vol = ((tape_m1.get("buy", 0)
                                  + tape_m1.get("sell", 0))
                                 if tape_m1 else None)
                    log.write(b, show, active_sym or "", lv, raw={
                        "t5": t5_now,
                        "tape_dom": tape_m1["dom"] if tape_m1 else None,
                        "tape_dom_big": (tape_m1.get("dom_big")
                                         if tape_m1 else None),
                        "tape_dom_w": (tape_m1.get("dom_w")
                                       if tape_m1 else None),
                        "tape_sided_n": (tape_m1.get("sided_n")
                                         if tape_m1 else None),
                        "tape_sided_share": (
                            sided_vol / tape_m1["total"]
                            if tape_m1 and tape_m1.get("total") else None),
                        "tape_pace": (tape_m1.get("pace_vol")
                                      if tape_m1 else None),
                        "tape_accel": (tape_m1.get("accel")
                                       if tape_m1 else None),
                        "vwap": vwap_now,
                        "vwap_age": vwap_age_now,
                        "vwap_src": "session" if sv is not None else "tape",
                        "tape_age": max(0.0, t0 - tsnap["since"])
                        if tsnap and tsnap.get("on") else None,
                        "quality": lv.get("quality") if lv else None,
                        "flow": ";".join(f"{s} {p:.4f} {v}" for s, p, v
                                         in flow["events"]) if flow else "",
                        "absorb": flow["absorb"] if flow else "",
                        "book_vote": flow["vote"] if flow else None,
                        "book_src": "sdk" if book_sdk else "ocr",
                        "tape_src": (tsnap.get("src")
                                     if tsnap and tsnap.get("on") else ""),
                        "bias": score,
                        "play": pb["verdict"],
                        # reserved for the Phase-5 microstructure signals
                        "micro_skew": None,
                        "ofi": None,
                    })
            elif not ok:
                misses += 1
                if cfg.get("debug", True) and misses % 25 == 0:
                    # dump what the failing region looks like + raw OCR text
                    cv2.imwrite(str(HERE / "debug_region_raw.png"),
                                np.ascontiguousarray(raw[:, :, :3]))
                    th = preprocess(raw)
                    cv2.imwrite(str(HERE / "debug_region_th.png"), th)
                    txt = pytesseract.image_to_string(th, config=TESS_CFG)
                    (HERE / "debug_region_text.txt").write_text(
                        f"region={region}\n--- OCR text ---\n{txt}")
            stamps.append(t0)
            stamps[:] = [s for s in stamps if t0 - s <= 5]
            win = cfg.get("trend_window", 300)
            mids = bucket_mids(engine.history, win)
            live.update(render(book, last_sig, reads, misses,
                               len(stamps) / 5.0, trader,
                               session_pnl, n_trades,
                               engine.trend_pct(60),
                               engine.trend_pct(win),
                               sparkline(mids), tracker.symbol,
                               [e for _, e in recent_events], lv, proj,
                               gate.dropped, (engine.trend_span(win), win),
                               f"Price ({win / 60:.0f}m)",
                               tsreader.snapshot(t0) if tsreader.enabled
                               else None, cfg,
                               "sdk" if book_sdk else "ocr"))
            time.sleep(max(0.0, interval - (time.time() - t0)))


# ------------------------------------------------------ optional LLM hook ---
# To add periodic LLM commentary later (cfg["llm"]["enabled"] = true):
# every cfg["llm"]["interval"] seconds, send the last ~30 rows of l2_log.csv
# to the Anthropic API (model claude-haiku) with a prompt like:
#   "Given these L2 snapshots (bid/ask/sizes/imbalance over time), describe
#    order-book behavior in 2 sentences and flag accumulation/distribution."
# Run it in a background thread so it never blocks the OCR loop.

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
