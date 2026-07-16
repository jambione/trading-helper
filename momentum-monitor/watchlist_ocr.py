"""Webull watchlist scraper — OCR the sidebar for the (pre)market movers.

Same recipe as the L2 / Time&Sales scrapers in webull-l2: find the Webull
window by process, anchor the capture region off a header row ("Symbol
Price/Change" at the top of the watchlist sidebar), then a daemon thread
grabs + OCRs it and keeps the latest parsed rows, skipping OCR entirely
when the pixels haven't changed.

Each watchlist entry renders as two lines:

    PBM                    2.890          <- symbol [badge] price
    Psyence Biome...  Pre: +1.31%         <- company  (Pre:) +/-pct%

so the parser walks the OCR'd lines top-down, remembers the last
symbol-line, and pairs it with the next %-line. Symbols get added and
removed all session; every frame is parsed from scratch, nothing is
tracked across frames. The '-' of a negative percent is the one thing OCR
can silently drop (it would read as a gainer); premarket gappers are
ranked by signed percent, so a dropped minus can't displace a real mover
at the top of the list, only join it.

Import is safe without the OCR deps (cv2/mss/pytesseract): parsing stays
usable for tests, and WatchlistReader just reports itself disabled.
"""
from __future__ import annotations

import re
import sys
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

# the capture/locator plumbing lives with the other scrapers in webull-l2
_WEBULL_L2 = Path(__file__).parent.parent / "webull-l2"
if str(_WEBULL_L2) not in sys.path:
    sys.path.insert(0, str(_WEBULL_L2))

try:
    import cv2
    import mss
    import numpy as np
    import pytesseract

    from l2_signal import find_webull_window, preprocess
    from l2_signal import load_config as _l2_config
    try:
        _l2_config()        # sets pytesseract's tesseract_cmd from config.json
    except Exception:                                      # noqa: BLE001
        pass
    HAVE_OCR = True
except Exception:                                          # noqa: BLE001
    HAVE_OCR = False


# "PBM 2.890" / "SNDQ 24 3.130" (24 = trading-session badge): symbol first,
# price last, at most a couple of badge tokens between. The token cap is the
# noise filter — chart overlays like "O 3.070 H 3.140 L 3.060 C 3.120" carry
# many tokens and never match, even if the region ever leaks into the chart.
SYM_LINE_RE = re.compile(r"^([A-Z]{2,5})(?:\s+\S+){0,2}\s+(\d{1,5}\.\d{2,4})$")
# the lookbehind stops a garbage run like "+5000%" from backtracking into
# a bogus "000%" match — a percent must not be the tail of a longer number
PCT_RE = re.compile(r"(?<![\d.])([+-]?\d{1,3}(?:\.\d{1,2})?)\s*%")


@dataclass
class Mover:
    sym: str
    pct: float                 # signed percent change
    price: float | None = None
    pre: bool = False          # the line carried a "Pre:" tag


def parse_watchlist_lines(lines) -> list[Mover]:
    """OCR'd text lines (top-down) -> [Mover]. Garbled lines are skipped;
    a %-line without a preceding symbol-line pairs with nothing. First
    occurrence wins when OCR doubles a symbol."""
    out: list[Mover] = []
    seen: set[str] = set()
    cur_sym: str | None = None
    cur_price: float | None = None
    for text in lines:
        text = text.strip()
        if not text:
            continue
        if "%" in text:
            m = PCT_RE.search(text)
            if m and cur_sym and cur_sym not in seen:
                pct = float(m[1])
                if abs(pct) <= 500:        # OCR garbage guard
                    out.append(Mover(cur_sym, pct, cur_price,
                                     "pre" in text.lower()))
                    seen.add(cur_sym)
            cur_sym = cur_price = None     # %-line closes the entry either way
            continue
        m = SYM_LINE_RE.match(text)
        if m:
            cur_sym, cur_price = m[1], float(m[2])
    return out


def top_movers(rows: list[Mover], n: int = 3,
               rank: str = "gainers") -> list[Mover]:
    """Top `n` by signed percent ("gainers", the default) or by magnitude
    ("abs", where a -8% dump outranks a +5% pop)."""
    key = (lambda r: abs(r.pct)) if rank == "abs" else (lambda r: r.pct)
    return sorted(rows, key=key, reverse=True)[:n]


# ---------------------------------------------------------------- capture ---

def locate_watchlist_region(win_img, win_rect: dict) -> dict | None:
    """Find the watchlist sidebar inside a screenshot of the Webull window.

    Anchor is the "Symbol  Price/Change" header row: a 'symbol' token in
    the LEFT part of the window (the positions/orders panels also have a
    Symbol column, but they sit mid-window) with a 'price...' token to its
    right on the same row. The region runs from just below that header to
    the bottom of the window, and stays narrow — right edge hugs the
    Price/Change column so chart overlays can't leak in.

    Adaptive threshold like locate_ts_region: the header text is dim grey
    on dark and a global Otsu pass erases it."""
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
        best = None
        for s in (w for w in words if w[0] == "symbol"):
            if s[1] > 0.45 * th.shape[1]:
                continue                   # a mid-window Symbol column header
            mates = [w for w in words
                     if "price" in w[0] and w[1] > s[1]
                     and abs(w[2] - s[2]) <= s[4]
                     and w[1] - s[1] < th.shape[1] // 4]
            if not mates:
                continue
            p = min(mates, key=lambda w: w[1])
            if best is None or s[2] < best[0][2]:
                best = (s, p)              # topmost header wins
        if best is None:
            continue
        s, p = best
        h = s[4]
        left = max(0, s[1] - 2 * h)
        right = p[1] + p[3] + 3 * h        # prices are right-aligned near it
        top = s[2] + int(h * 2.0)          # skip the header row itself
        bottom = th.shape[0] - h
        region = {
            "left": win_rect["left"] + left // scale,
            "top": win_rect["top"] + top // scale,
            "width": (right - left) // scale,
            "height": (bottom - top) // scale,
        }
        region["width"] = min(region["width"], win_rect["left"]
                              + win_rect["width"] - region["left"])
        region["height"] = min(region["height"], win_rect["top"]
                               + win_rect["height"] - region["top"])
        if region["width"] >= 60 and region["height"] >= 80:
            return region
    return None


def watchlist_frame(raw) -> list[Mover]:
    """OCR one captured watchlist frame -> [Mover].

    Words are clustered into visual rows by y-centre instead of trusting
    tesseract's (block, par, line) keys: the symbol and its right-aligned
    price sit far apart, and psm 6 sometimes splits those columns into
    different blocks, which would break the symbol/price pairing."""
    th = preprocess(raw)                   # 3x upscale + adaptive threshold
    d = pytesseract.image_to_data(th, config="--psm 6",
                                  output_type=pytesseract.Output.DICT)
    words = [(d["left"][i], d["top"][i], d["height"][i], d["text"][i].strip())
             for i in range(len(d["text"])) if d["text"][i].strip()]
    words.sort(key=lambda w: w[1] + w[2] / 2)
    rows: list[tuple[float, list]] = []
    for w in words:
        yc = w[1] + w[2] / 2
        if rows and abs(yc - rows[-1][0]) <= 0.6 * max(6, w[2]):
            rows[-1][1].append(w)
        else:
            rows.append((yc, [w]))
    lines = [" ".join(t for _, _, _, t in sorted(ws)) for _, ws in rows]
    return parse_watchlist_lines(lines)


class WatchlistReader:
    """Daemon thread that captures the Webull watchlist sidebar, OCRs it,
    and keeps the latest parsed movers. Mirrors TapeReader: relocates the
    region when the window moves or reads keep failing, skips OCR when
    the frame is pixel-identical, and never lets an exception kill the
    thread. Reports itself disabled when the OCR deps are missing."""

    def __init__(self, cfg: dict, console=None):
        self.enabled = HAVE_OCR and bool(cfg.get("watchlist_enabled", True))
        self.poll = float(cfg.get("watchlist_poll", 2.0))
        self.manual = cfg.get("watchlist_region")
        self.console = console
        self.lock = threading.Lock()
        self.region: dict | None = None
        self.rows: list[Mover] = []
        self.updated: float | None = None
        self.ok = 0
        self.miss = 0
        self._misses_row = 0
        self._win_rect: dict | None = None
        self._frame_key: int | None = None
        if self.enabled:
            threading.Thread(target=self._run, daemon=True).start()

    def snapshot(self) -> dict:
        with self.lock:
            return {"on": self.enabled, "located": self.region is not None,
                    "rows": list(self.rows), "updated": self.updated,
                    "ok": self.ok, "miss": self.miss}

    def _run(self):
        with mss.MSS() as sct:
            while True:
                t0 = time.time()
                try:
                    self._step(sct, t0)
                except Exception:                          # noqa: BLE001
                    pass    # never die on a bad frame
                time.sleep(max(0.0, self.poll - (time.time() - t0)))

    def _step(self, sct, now: float):
        rect = find_webull_window()
        if rect and (self.region is None or rect != self._win_rect
                     or self._misses_row >= 5):
            found = locate_watchlist_region(np.asarray(sct.grab(rect)), rect)
            if found and found != self.region and self.console:
                self.console.print(f"[dim]watchlist located at {found}[/dim]")
            with self.lock:
                self.region = found or self.region or self.manual
            self._win_rect = rect
            self._misses_row = 0
        if not self.region:
            return
        raw = np.asarray(sct.grab(self.region))
        key = zlib.crc32(raw.tobytes())
        if key == self._frame_key:
            with self.lock:
                if self.rows:
                    self.updated = now     # unchanged pixels = still current
            return
        self._frame_key = key
        movers = watchlist_frame(raw)
        with self.lock:
            if movers:
                self.ok += 1
                self._misses_row = 0
                self.rows = movers
                self.updated = now
            else:
                self.miss += 1
                self._misses_row += 1
