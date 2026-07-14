"""Time & Sales logic: row parsing, cross-frame dedupe, rolling tape
metrics (buy/sell executed volume, print pace, true VWAP).

Pure stdlib like l2_core so it unit-tests without the capture stack; the
capture side (locating the Time&Sales widget, OCR, row colors) lives in
l2_signal.py and only hands parsed rows to this module.

Why this exists: L2 imbalance sums RESTING displayed size, which spoofers
manipulate freely on low-float movers. The tape is executed transactions -
whether real money is lifting the ask or hitting the bid - and is the
ground truth the book signals get checked against.
"""
from __future__ import annotations

import re
import time
from collections import deque

from l2_core import _size

# Real OCR of the tape merges columns freely: "10:06:40138.45 100",
# "10:06:40138.40313", "138.441,455". The time prefix is the only
# unambiguous anchor; price/size blobs are split by trying decimal widths.
TIME_RE = re.compile(r"^\s*(\d{1,2}:\d{2}:\d{2})\s*")
PRICE_RE = re.compile(r"^\d+\.\d{2,4}$")
_JUNK_RE = re.compile(r"^[.,:]{1,3}$")   # OCR remnant of the '--' column


def _valid_size_tok(tok: str) -> bool:
    """Share sizes never lead with 0; decimals only with a K suffix."""
    return bool(re.fullmatch(r"[1-9][\d,]*", tok)
                or re.fullmatch(r"[1-9][\d,]*(?:\.\d{1,2})?K", tok,
                                re.IGNORECASE))


def _split_blob(tstr: str, blob: str, hint: float | None,
                dec: int | None):
    """'138.40313' -> ('..', 138.40, 313).

    `dec` is the price decimal count learned from the frame's clean rows -
    Webull renders every price in the widget with the same width, so a
    known dec resolves the split outright. Without it, all widths are
    tried and only an UNAMBIGUOUS split is accepted (a price hint filters
    out-of-range candidates first). Never guesses between survivors."""
    cands = []
    for d in ((dec,) if dec else (2, 3, 4)):
        m = re.match(rf"^(\d+\.\d{{{d}}})(.+)$", blob)
        if not m or not _valid_size_tok(m.group(2)):
            continue
        price, size = float(m.group(1)), _size(m.group(2))
        if price <= 0 or size <= 0:
            continue
        if hint is not None and not (0.8 * hint <= price <= 1.25 * hint):
            continue
        cands.append((price, size))
    if len(cands) != 1:
        return None
    return (tstr, *cands[0])


def classify_rgb(r: float, g: float, b: float) -> str:
    """Webull tape colors -> aggressor side. Teal-green text/highlight =
    buyer lifted the ask ('B'), red/pink = seller hit the bid ('S'),
    grey/white = between the quotes or unknown ('N')."""
    if g > 60 and g > 1.25 * r:
        return "B"
    if r > 60 and r > 1.25 * g:
        return "S"
    return "N"


def parse_tape_line(text: str, hint: float | None = None,
                    dec: int | None = None) -> tuple | None:
    """One OCR'd row -> (hh:mm:ss, price, size) or None if garbled.

    Tolerates the merges OCR actually produces: time fused to price
    (zero-width column gap) and price fused to size. `hint` (the going
    price) and `dec` (the frame's price decimal width) come from the
    frame's cleanly-parsed rows and resolve merged price/size blobs."""
    m = TIME_RE.match(text)
    if not m:
        return None
    toks = text[m.end():].split()
    if toks and _JUNK_RE.match(toks[-1]):
        toks = toks[:-1]                   # shed the '--' column remnant
    tstr = m.group(1)
    if len(toks) == 2 and PRICE_RE.match(toks[0]) \
            and _valid_size_tok(toks[1]):
        price, size = float(toks[0]), _size(toks[1])
        if price > 0 and size > 0:
            return (tstr, price, size)
        return None
    if len(toks) == 1:
        return _split_blob(tstr, toks[0], hint, dec)
    return None


class Tape:
    """Rolling record of executed prints built from successive frames of
    the scrolling Time&Sales panel (newest row on top).

    Dedupe: the previous frame's top rows are searched for inside the new
    frame; everything above the match is new. Identical prints legitimately
    repeat on a tape, so matching starts with a 4-row signature and backs
    off to shorter ones - under-counting on a degenerate match beats
    double-counting a whole frame.
    """

    def __init__(self, keep: float = 600.0):
        self.keep = keep
        self.prints: deque = deque()   # (wall_ts, price, size, side)
        self._prev: list = []          # last frame's signatures, top-down
        self.pv = 0.0                  # running sum(price*size) for VWAP
        self.v = 0.0                   # running sum(size)
        self._last_px: float | None = None    # for the tick rule
        self._last_side: str = "N"
        self.started = time.time()

    def reset(self):
        """Forget everything (symbol switch - old prints are meaningless)."""
        self.prints.clear()
        self._prev = []
        self.pv = self.v = 0.0
        self._last_px, self._last_side = None, "N"
        self.started = time.time()

    def _infer_side(self, price: float, bid: float | None,
                    ask: float | None) -> str:
        """Webull leaves many prints uncolored, so classify them the
        classic way: quote rule first (at/above the ask = buyer, at/below
        the bid = seller), tick rule as fallback (uptick = buy, downtick
        = sell, unchanged inherits the last known side)."""
        if ask is not None and price >= ask:
            return "B"
        if bid is not None and price <= bid:
            return "S"
        if self._last_px is not None:
            if price > self._last_px:
                return "B"
            if price < self._last_px:
                return "S"
            return self._last_side
        return "N"

    def ingest_frame(self, rows: list, now: float,
                     bid: float | None = None,
                     ask: float | None = None) -> int:
        """rows = [(hh:mm:ss, price, size, side)] top-down, newest first;
        bid/ask are the current L2 quotes for side inference on uncolored
        prints. Records prints not seen in the previous frame."""
        sigs = [r[:3] for r in rows]
        new = rows if not self._prev else rows[:self._match(sigs)]
        for _ts, price, size, side in reversed(new):   # oldest new first
            if side == "N":
                side = self._infer_side(price, bid, ask)
            self.prints.append((now, price, size, side))
            self.pv += price * size
            self.v += size
            self._last_px = price
            if side != "N":
                self._last_side = side
        if rows:
            self._prev = sigs[:6]
        while self.prints and now - self.prints[0][0] > self.keep:
            self.prints.popleft()
        return len(new)

    def _match(self, sigs: list) -> int:
        """Index in the new frame where the previous frame starts."""
        for k in range(min(len(self._prev), 4), 0, -1):
            pat = self._prev[:k]
            for i in range(len(sigs) - k + 1):
                if sigs[i:i + k] == pat:
                    return i
        return len(sigs)   # no overlap at all -> whole frame is new

    def vwap(self) -> float | None:
        """True volume-weighted average price of every captured print."""
        return self.pv / self.v if self.v > 0 else None

    def metrics(self, now: float, window: float) -> dict:
        """Buy/sell executed volume and dominance over the trailing window.
        dom is -1 (all sells) .. +1 (all buys) over the SIDED volume."""
        xs = [p for p in self.prints if now - p[0] <= window]
        buy = sum(sz for _, _, sz, s in xs if s == "B")
        sell = sum(sz for _, _, sz, s in xs if s == "S")
        total = sum(sz for _, _, sz, _ in xs)
        sided = buy + sell
        sided_n = sum(1 for _, _, _, s in xs if s in ("B", "S"))
        # sided_n / sided volume let the confidence gate vote on real sided
        # EVIDENCE (a few large clearly-sided prints) rather than raw print
        # count, and abstain when the tape is mostly unsided noise.
        return {"n": len(xs), "sided_n": sided_n, "buy": buy, "sell": sell,
                "total": total, "dom": (buy - sell) / sided if sided > 0
                else 0.0}
