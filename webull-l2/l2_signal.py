"""Webull Level-2 OCR signal monitor.

Captures the L2 order-book region of your screen, OCRs it, computes
imbalance/spread/wall signals, and alerts via console dashboard, sound,
and Windows toast. Logs every snapshot to CSV.

Region modes (config.json "region_mode"):
    "auto"   - finds the Webull window and locates the L2 panel by its
               header row; re-anchors when the window moves/resizes.
    "manual" - uses the fixed coordinates from calibrate.py.

Usage:
    python l2_signal.py

Requires: Tesseract-OCR installed (https://github.com/UB-Mannheim/tesseract/wiki)
and `pip install -r requirements.txt`.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import threading
import time
import zlib
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

from l2_core import (GlitchGate, L2Book, LongView, PaperTrader, Signal,
                     SignalEngine, Trade, WallTracker, _median, market_bias,
                     parse_l2_text, playbook, project_price)
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


def _guess_symbol(raw_words: list[str]) -> str | None:
    """The active ticker appears several times on screen (chart header,
    quotes panel, order line); watchlist tickers only once. Pick the most
    frequent 2-5 letter uppercase token."""
    import collections
    import re as _re
    cands = [w for w in raw_words
             if _re.fullmatch(r"[A-Z]{2,5}", w) and w not in _NOT_TICKERS]
    if not cands:
        return None
    return collections.Counter(cands).most_common(1)[0][0]


def detect_symbol(win_img: np.ndarray) -> str | None:
    """OCR the Webull window and guess the active ticker symbol."""
    gray = cv2.cvtColor(win_img, cv2.COLOR_BGRA2GRAY)
    gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    if np.median(gray) < 127:
        gray = 255 - gray
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    d = pytesseract.image_to_data(th, config="--psm 11",
                                  output_type=pytesseract.Output.DICT)
    return _guess_symbol([w.strip() for w in d["text"] if w.strip()])

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


class RegionTracker:
    """Keeps the capture region pointed at the L2 panel even when the
    Webull window moves or resizes. Re-anchors when the window rect
    changes or several consecutive reads fail."""

    MISS_LIMIT = 5
    BAD_ANCHOR_LIMIT = 3     # anchors that produced zero good reads
    MANUAL_HOLD = 60         # seconds to sit on manual region before retrying auto

    def __init__(self, cfg: dict, console=None):
        self.mode = cfg.get("region_mode", "auto")
        self.manual = cfg.get("region")
        self.debug = cfg.get("debug", True)
        self.console = console
        self.region = self.manual if self.mode == "manual" else None
        self.win_rect = None
        self.misses = 0
        self.ok_since_anchor = 0
        self.bad_anchors = 0
        self.manual_until = 0.0
        self.symbol: str | None = None
        self._sym_cand: str | None = None

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
            return self.manual
        now = time.time()
        if now < self.manual_until:
            return self.manual
        rect = find_webull_window()
        if rect is None:
            return self.manual  # window not found; try manual as fallback
        if (self.region and rect == self.win_rect
                and self.misses < self.MISS_LIMIT):
            return self.region
        # window moved/resized or reads failing -> re-anchor
        if self.region is not None and self.ok_since_anchor == 0:
            self.bad_anchors += 1
        else:
            self.bad_anchors = 0
        if self.bad_anchors >= self.BAD_ANCHOR_LIMIT and self.manual:
            self._say("auto-locate keeps failing -> using calibrated region "
                      f"for {self.MANUAL_HOLD}s (check debug_anchor.png; "
                      "run calibrate.py to refresh the fallback)")
            self.manual_until = now + self.MANUAL_HOLD
            self.bad_anchors = 0
            self.misses = 0
            return self.manual
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
        elif self.region is None:
            self.region = self.manual
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
        if self.enabled:
            threading.Thread(target=self._run, daemon=True).start()

    def set_quote(self, bid: float, ask: float, ts: float):
        """Latest L2 touch, used to side uncolored prints (quote rule)."""
        with self.lock:
            self._quote = (bid, ask, ts)

    def snapshot(self, now: float) -> dict:
        with self.lock:
            return {"on": self.region is not None and self.ok > 0,
                    "m1": self.tape.metrics(now, 60),
                    "m5": self.tape.metrics(now, 300),
                    "vwap": self.tape.vwap(),
                    "since": self.tape.started,
                    "ok": self.ok, "miss": self.miss}

    def reset(self):
        with self.lock:
            self.tape.reset()

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
                self.tape.ingest_frame(rows, now, bid, ask)
            else:
                self.miss += 1
                self._misses_row += 1


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
    (schema changed, e.g. the symbol column was added), the old file is
    renamed <stem>-old-<stamp>.csv instead of being silently mixed."""
    if path.exists():
        try:
            with open(path) as fh:
                first = fh.readline().strip()
        except Exception:                                  # noqa: BLE001
            first = ""
        if first != ",".join(fields):
            path.rename(path.with_name(
                f"{path.stem}-old-{datetime.now():%Y%m%d-%H%M%S}.csv"))
    new = not path.exists()
    f = open(path, "a", newline="")
    w = csv.writer(f)
    if new:
        w.writerow(fields)
    return f, w


class CsvLog:
    FIELDS = ["time", "symbol", "best_bid", "best_ask", "spread_pct",
              "bid_size", "ask_size", "imbalance", "signal", "reason"]

    def __init__(self, path: Path):
        self.f, self.w = _open_csv(path, self.FIELDS)

    def write(self, book: L2Book, sig: Signal | None, symbol: str = ""):
        self.w.writerow([
            datetime.now().isoformat(timespec="milliseconds"), symbol,
            f"{book.best_bid:.4f}", f"{book.best_ask:.4f}",
            f"{book.spread_pct:.3f}", int(book.bid_size), int(book.ask_size),
            f"{book.imbalance:.3f}",
            sig.action if sig else "", sig.reason if sig else "",
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


def lv_banner(lv: dict | None) -> Panel:
    """THE signal: 5-minute confidence, big and unmissable at the top.

    Four centered lines - a bold directional headline, a colored
    confidence gauge with the agree/total fraction, the three pillar
    votes, and a dim meta line (held time / pending turn / no-tape).
    Everything else in the table below is supporting detail. Confidence =
    agreement of trend + tape + VWAP (see l2_core.confidence), not the
    size of any single meter."""
    if not lv:
        return Panel(
            Align.center("[bold grey70]WARMING UP[/bold grey70]   "
                         "[dim]building the 5-minute picture …[/dim]"),
            title="[dim]5m CONFIDENCE[/dim]", title_align="left",
            border_style="grey50", padding=(1, 2))

    held = lv["held"]
    dur = (f"{int(held // 60)}m{int(held % 60):02d}s"
           if held >= 60 else f"{int(held)}s")
    agree, total = lv.get("agree", 0), lv.get("total", 0)
    tape_live = lv.get("tape_live", False)
    stance = lv["stance"]

    if stance == "NEUTRAL" or agree < 2:
        color = "yellow"
        headline = "◇   STAND ASIDE   ◇"
        sub = "no aligned edge"
    else:
        strong = agree >= 3
        if stance == "LONG":
            color = "green"
            headline = "▲ ▲   GO / HOLD LONG   ▲ ▲" if strong else "▲   LEAN LONG"
            weak_note = "half size"
        else:  # BEAR
            color = "red"
            headline = ("▼ ▼   GET OUT / STAY OUT   ▼ ▼" if strong
                        else "▼   LEAN OUT")
            weak_note = "protect / trim"
        if strong:
            sub = "trend, tape and VWAP all agree"
        elif not tape_live:
            sub = "trend & VWAP agree — unconfirmed by tape"
        else:
            sub = f"{agree} of {total} agree — {weak_note}"

    # confidence gauge: filled (colored) + empty (grey) pips + fraction
    pips = " ".join([f"[{color}]●[/{color}]"] * agree
                    + [f"[grey42]○[/grey42]"] * max(0, total - agree))
    gauge = (f"{pips}    [bold {color}]{agree}/{total}[/bold {color}]"
             f"    [dim]{sub}[/dim]")

    v = lv.get("votes", {})
    pillars = "     ".join((_vote_cell("trend", v.get("trend")),
                            _vote_cell("tape", v.get("tape")),
                            _vote_cell("vwap", v.get("vwap"))))

    meta = f"[dim]held {dur}[/dim]"
    if not lv.get("tape_live", False):
        meta += "   [yellow]· no tape (show Time&Sales)[/yellow]"
    if lv.get("pending"):
        pend = {"LONG": "LONG", "BEAR": "OUT"}.get(lv["pending"], lv["pending"])
        meta += (f"   [dim]→ turning[/dim] [bold {color}]{pend}[/bold {color}] "
                 f"[dim]in {int(lv['pending_left'])}s if it holds[/dim]")

    body = Group(
        Align.center(f"[bold {color}]{headline}[/bold {color}]"),
        Align.center(gauge),
        Align.center(pillars),
        Align.center(meta),
    )
    return Panel(body, title=f"[bold {color}]5m CONFIDENCE[/bold {color}]",
                 title_align="left", border_style=color, padding=(1, 2))


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
           tape: dict | None = None) -> Group:
    title = (f"Webull L2 Monitor [bold cyan]{symbol or '?'}[/bold cyan]"
             f"   {hz:.1f} reads/s   ok:{reads} miss:{misses}")
    if glitches:
        title += f" glitch:{glitches}"
    t = Table(title=title, expand=False)
    t.add_column("Metric"), t.add_column("Value", justify="right")
    if session_line:
        t.add_row("⏱ Session", session_line())
    if book:
        tape_m1 = tape["m1"] if tape and tape.get("on") else None
        # one-glance verdict, always the first thing you see
        score, label, why = market_bias(book, trend1, trend5, tape=tape_m1)
        bc = ("green" if label == "BULLISH"
              else "red" if label == "BEARISH" else "yellow")
        arrow = ("▲" if label == "BULLISH"
                 else "▼" if label == "BEARISH" else "→")
        t.add_row("MARKET BIAS",
                  f"[bold {bc}]{arrow} {label} ({score:+.0f})[/bold {bc}]")
        t.add_row("", f"[{bc}]{_bias_meter(score)}[/{bc}]  [dim]{why}[/dim]")

        # the daily-use playbook: 4 checks -> 1 action
        pb = playbook(book, trend1, trend5, wall_events or [], tape_m1)
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
            mid, target, note = proj
            chg = 100.0 * (target - mid) / mid if mid else 0.0
            pc = ("green" if chg > 0.05 else "red" if chg < -0.05
                  else "yellow")
            t.add_row("Price → proj (5m)",
                      f"{mid:.3f} → [{pc}]{target:.3f} ({chg:+.2f}%)[/{pc}]"
                      + (f"  [dim]{note}[/dim]" if note else "")
                      + "  [dim]at current pace[/dim]")
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
    return Group(lv_banner(lv), t)


# ------------------------------------------------------------------ main ----

def main():
    cfg = load_config()
    engine = SignalEngine(cfg)
    trader = PaperTrader(cfg)
    log = CsvLog(HERE / "l2_log.csv") if cfg.get("csv_log", True) else None
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
    recent_events: list = []   # [(ts, event)] kept ~10s so you can react
    longview = LongView(cfg)
    lv: dict | None = None
    proj: tuple | None = None
    gate = GlitchGate(cfg.get("glitch_jump_pct", 1.5),
                      cfg.get("glitch_confirm", 2))
    frame_key: tuple | None = None       # (region, crc32) of the last grab
    frame_book: L2Book | None = None     # what that frame parsed to

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
    tsreader = TapeReader(cfg, console)
    if tsreader.enabled:
        console.print("[dim]Time&Sales reader on — executed prints feed "
                      "the tape rows, checklist, and VWAP[/dim]")
    active_sym: str | None = None      # symbol the current state belongs to

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
    with mss.MSS() as sct, Live(render(None, None, 0, 0, 0),
                                console=console, refresh_per_second=4) as live:
        while True:
            t0 = time.time()
            region = tracker.get(sct)
            if region is None:
                live.update(render(None, last_sig, reads, misses, 0.0))
                time.sleep(1.0)
                continue
            if tracker.symbol != active_sym:
                old, active_sym = active_sym, tracker.symbol
                if old is not None:
                    # trends, streaks, walls, tape, and any virtual entry
                    # computed on the old stock are garbage for the new one
                    engine.reset()
                    walltrack.state.clear()
                    recent_events.clear()
                    longview = LongView(cfg)
                    gate = GlitchGate(cfg.get("glitch_jump_pct", 1.5),
                                      cfg.get("glitch_confirm", 2))
                    tsreader.reset()
                    frame_key = frame_book = None
                    book = lv = proj = last_sig = None
                    dropped = trader.drop_virtual()
                    console.print(
                        f"[dim]symbol switch {old} → {active_sym}: history "
                        "reset" + (", virtual position dropped" if dropped
                                   else "") + "[/dim]")
            raw = np.asarray(sct.grab(region))
            key = (region["left"], region["top"], region["width"],
                   region["height"], zlib.crc32(raw.tobytes()))
            if key == frame_key:
                # pixels unchanged since the last poll -> skip Tesseract
                # entirely and re-stamp the previous parse (same market
                # state, fresh timestamp). Big CPU win on a quiet tape.
                b = (L2Book(frame_book.bids, frame_book.asks)
                     if frame_book else None)
            else:
                b = ocr_image(raw)
                frame_key, frame_book = key, b
            ok = b is not None
            tracker.report(ok)
            if ok and not gate.accept(b):
                b = None   # isolated out-of-band frame -> held back
            if b:
                reads += 1
                book = b
                if tsreader.enabled:
                    tsreader.set_quote(b.best_bid, b.best_ask, t0)
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
                lv = longview.update(
                    b, engine.trend_pct(cfg.get("trend_window", 300)),
                    evs, t0, tape=tape_m1,
                    vwap=tsnap["vwap"] if tsnap else None)
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

                # publish live state so the TradingView monitor can build
                # a combined chart+orderflow master verdict
                try:
                    score, _, _ = market_bias(b, engine.trend_pct(60),
                                              engine.trend_pct(300),
                                              tape=tape_m1)
                    pb = playbook(b, engine.trend_pct(60),
                                  engine.trend_pct(300),
                                  [e for _, e in recent_events], tape_m1)
                    (HERE.parent / "l2_state.json").write_text(json.dumps({
                        "ts": t0, "symbol": tracker.symbol,
                        "bias": round(score, 1), "play": pb["verdict"],
                        "confidence": (f"{lv['stance']} {lv['agree']}/"
                                       f"{lv['total']}") if lv else None,
                        "imbalance": round(b.imbalance, 2),
                        "tape_dom": (round(tape_m1["dom"], 2)
                                     if tape_m1 else None),
                        "vwap": (round(tsnap["vwap"], 4)
                                 if tsnap and tsnap.get("vwap") else None),
                        "price": round(proj[0], 4) if proj else None,
                        "proj": round(proj[1], 4) if proj else None,
                        "stance": lv["stance"] if lv else None,
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
                    log.write(b, show, active_sym or "")
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
                               else None))
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
