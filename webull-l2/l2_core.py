"""Core logic: OCR-text parsing + signal engine. Pure stdlib, no dependencies.

Kept separate from capture/alert code so it can be unit-tested and reused.
"""
from __future__ import annotations

import math
import re
import time
from collections import deque
from dataclasses import dataclass, field

# Matches one L2 row: "129  3.110  3.180  9"  (bidSize bid ask askSize)
# Sizes may contain commas or a K suffix (13.01K).
ROW_RE = re.compile(
    r"^\s*([\d.,]+K?)\s+(\d+\.\d{2,4})\s+(\d+\.\d{2,4})\s+([\d.,]+K?)\s*$",
    re.IGNORECASE,
)

# In Webull's L2 panel the bid and ask price columns are nearly touching,
# so OCR often merges them into ONE token: "1.3301.340". This matches a
# row whose middle token contains two dots (two concatenated prices).
MERGED_RE = re.compile(
    r"^\s*([\d.,]+K?)\s+(\d+\.\d+\.\d+)\s+([\d.,]+K?)\s*$",
    re.IGNORECASE,
)


def _split_prices(tok: str):
    """'1.3301.340' -> (1.33, 1.34). Tries 3, 2, then 4 decimal places;
    accepts only if it yields a plausible bid<ask pair."""
    for dec in (3, 2, 4):
        m = re.match(rf"^(\d+\.\d{{{dec}}})(\d+\.\d{{{dec}}})$", tok)
        if m:
            p1, p2 = float(m.group(1)), float(m.group(2))
            if 0 < p2 - p1 <= 0.5 * p2:
                return p1, p2
    return None


def _median(vals):
    vals = sorted(vals)
    n = len(vals)
    if not n:
        return None
    return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2


def _size(tok: str) -> float:
    """'13.01K' -> 13010, '1,204' -> 1204, '129' -> 129."""
    tok = tok.replace(",", "").strip()
    mult = 1.0
    if tok.upper().endswith("K"):
        mult, tok = 1000.0, tok[:-1]
    try:
        return float(tok) * mult
    except ValueError:
        return -1.0


@dataclass
class L2Book:
    bids: list  # [(price, size)] best first
    asks: list  # [(price, size)] best first
    ts: float = field(default_factory=time.time)

    @property
    def best_bid(self):
        return self.bids[0][0]

    @property
    def best_ask(self):
        return self.asks[0][0]

    @property
    def mid(self):
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self):
        return self.best_ask - self.best_bid

    @property
    def spread_pct(self):
        mid = (self.best_ask + self.best_bid) / 2
        return 100.0 * self.spread / mid if mid else 0.0

    @property
    def bid_size(self):
        return sum(s for _, s in self.bids)

    @property
    def ask_size(self):
        return sum(s for _, s in self.asks)

    @property
    def imbalance(self):
        """>1 = buy pressure, <1 = sell pressure."""
        return self.bid_size / self.ask_size if self.ask_size else float("inf")

    def walls(self, multiple: float = 4.0):
        """Levels whose size >= multiple x median size on that side."""
        out = []
        for side, levels in (("BID", self.bids), ("ASK", self.asks)):
            sizes = sorted(s for _, s in levels)
            if len(sizes) < 3:
                continue
            med = sizes[len(sizes) // 2]
            if med <= 0:
                continue
            for price, size in levels:
                if size >= multiple * med:
                    out.append((side, price, size))
        return out


def parse_l2_text(text: str) -> L2Book | None:
    """Parse raw OCR text of the Webull L2 panel (Size Bid | Ask Size columns).

    Tolerant of OCR noise: skips malformed lines, then sanity-checks ordering.
    Returns None if fewer than 3 clean levels survive.
    """
    bids, asks = [], []
    for line in text.splitlines():
        m = ROW_RE.match(line)
        if m:
            bs, bp, ap, asz = _size(m[1]), float(m[2]), float(m[3]), _size(m[4])
        else:
            m2 = MERGED_RE.match(line)
            if not m2:
                continue
            pair = _split_prices(m2[2])
            if pair is None:
                continue
            bs, (bp, ap), asz = _size(m2[1]), pair, _size(m2[3])
        if bs < 0 or asz < 0 or bp <= 0 or ap <= 0 or bp >= ap:
            continue  # crossed/garbled row
        bids.append((bp, bs))
        asks.append((ap, asz))

    # Sanity: bids must be non-increasing, asks non-decreasing. Drop violators.
    bids = _monotone(bids, decreasing=True)
    asks = _monotone(asks, decreasing=False)
    n = min(len(bids), len(asks))
    if n < 3:
        return None
    return L2Book(bids[:n], asks[:n])


def _monotone(levels, decreasing: bool):
    out = []
    for p, s in levels:
        if out and ((p > out[-1][0]) if decreasing else (p < out[-1][0])):
            continue
        out.append((p, s))
    return out


class GlitchGate:
    """Drops isolated OCR misreads before they pollute history.

    A book whose mid jumps more than `jump_pct` from the median of the
    recent accepted mids is held back; it only passes once `confirm`
    consecutive reads land on the same side of the band (a real spike
    keeps printing there, an OCR glitch is gone next frame). Costs one
    frame of latency on a genuinely violent move, kills the single-frame
    garbage that used to swing the 1m/5m trend and the projection.
    """

    def __init__(self, jump_pct: float = 1.5, confirm: int = 2,
                 keep: int = 9, ref_tol_pct: float = 15.0):
        self.jump_pct = jump_pct
        self.confirm = max(1, confirm)
        self.ref_tol_pct = ref_tol_pct
        self.mids: deque = deque(maxlen=keep)
        self._streak = 0        # consecutive out-of-band reads
        self._streak_dir = 0    # +1 above the band, -1 below
        self.dropped = 0        # total frames held back (for the UI)

    def accept(self, book: L2Book, ref: float | None = None) -> bool:
        mid = book.mid
        # Hard external sanity check first: a real executed trade price that
        # disagrees with the OCR mid by a wide margin means the OCR grabbed
        # garbage (wrong region, merged/misread digits). The internal streak
        # logic below can't catch this on its own -- two consecutive bad
        # reads make it REBASE onto the garbage level, which is exactly how
        # the 139->6 and 6.81->23.74 rows entered the paper log. `ref` is
        # only ever a fresh print (the caller gates staleness), so vetoing
        # against it can't fight a real fast move.
        if ref and ref > 0 and abs(mid - ref) / ref > self.ref_tol_pct / 100.0:
            self.dropped += 1
            return False
        if len(self.mids) < 3:
            self.mids.append(mid)
            return True
        med = _median(self.mids)
        dev = 100.0 * (mid - med) / med if med else 0.0
        if abs(dev) > self.jump_pct:
            direction = 1 if dev > 0 else -1
            if direction == self._streak_dir:
                self._streak += 1
            else:
                self._streak, self._streak_dir = 1, direction
            if self._streak >= self.confirm:
                # confirmed move -> rebase on the new level
                self.mids.clear()
                self.mids.append(mid)
                self._streak = self._streak_dir = 0
                return True
            self.dropped += 1
            return False
        self._streak = self._streak_dir = 0
        self.mids.append(mid)
        return True


# Regular-hours open, in ET minutes-from-midnight. VWAP is conventionally
# drawn from the session open, and the sessions a small-cap actually trades
# in are premarket / regular / after-hours - so the anchor is the most
# recent of these at or before the trade. The 04:00 anchor is not padding:
# the logs contain 04:18 sessions.
_SESSION_ANCHORS_ET = (4 * 60, 9 * 60 + 30, 16 * 60)


def _session_anchor(ts: float, tz=None) -> float:
    """Unix ts of the session open that `ts` belongs to (ET)."""
    from datetime import datetime, timedelta
    if tz is None:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
    dt = datetime.fromtimestamp(ts, tz)
    mins = dt.hour * 60 + dt.minute
    start = max((m for m in _SESSION_ANCHORS_ET if m <= mins), default=None)
    if start is None:                      # before 04:00 -> overnight belongs
        prev = dt - timedelta(days=1)      # to the prior after-hours session
        base = prev.replace(hour=16, minute=0, second=0, microsecond=0)
        return base.timestamp()
    return dt.replace(hour=start // 60, minute=start % 60,
                      second=0, microsecond=0).timestamp()


class SessionVWAP:
    """A VWAP anchored to the SESSION open, accumulated per symbol from the
    real trade stream, and kept across symbol hops.

    This exists because the vwap confidence pillar was not independent of
    the trend pillar (measured corr +0.43, agree 67% vs 47% if independent
    - see `confidence`). The cause was mechanical: the old VWAP came from
    the local tape reader, which resets on every symbol switch, so it
    spanned a median of 106s and never more than 11.8 min. A VWAP that
    covers ~2 minutes is not "who controls the day" - it is a second,
    slower trend pillar computed off the same recent price path.

    Note the trap that made it inevitable: the old maturity gate was
    vwap_min_age=180 against trend_window=300. A "mature" VWAP was YOUNGER
    than the trend it was supposed to be independent of - it was
    necessarily a subset of the same prices. Independence needs the VWAP to
    span MUCH more time than the trend window, hence a default min age well
    above it (see VWAP_MIN_AGE_DEFAULT).

    Volume-weighted, from actual executed prints - so it is as hard to
    spoof as the tape, unlike resting displayed size.

    `age()` reports the span actually COVERED (from the first print seen),
    not time since the session anchor. Start the monitor at 11:00 and the
    anchor is 09:30, but nothing from 09:30-11:00 was ever observed; the
    caller's maturity gate must judge real coverage, never an anchor we
    slept through."""

    def __init__(self, tz=None):
        self._pv: dict[str, float] = {}      # sum(price * volume)
        self._v: dict[str, float] = {}       # sum(volume)
        self._first: dict[str, float] = {}   # first print ts actually seen
        self._anchor: dict[str, float] = {}  # session open this belongs to
        self._tz = tz

    def ingest(self, symbol: str, price: float, volume: float,
               ts: float | None = None) -> None:
        """One executed print. Safe to call from the stream thread."""
        if not symbol or not price or price <= 0 or not volume or volume <= 0:
            return
        ts = time.time() if ts is None else ts
        sym = symbol.upper()
        anchor = _session_anchor(ts, self._tz)
        if self._anchor.get(sym) != anchor:      # new session -> start over
            self._anchor[sym] = anchor
            self._pv[sym] = self._v[sym] = 0.0
            self._first[sym] = ts
        self._pv[sym] += price * volume
        self._v[sym] += volume

    def vwap(self, symbol: str | None) -> float | None:
        if not symbol:
            return None
        sym = symbol.upper()
        v = self._v.get(sym, 0.0)
        return (self._pv[sym] / v) if v > 0 else None

    def age(self, symbol: str | None, now: float | None = None) -> float | None:
        """Seconds of session actually covered, or None if never seen."""
        if not symbol:
            return None
        first = self._first.get(symbol.upper())
        if first is None:
            return None
        return max(0.0, (time.time() if now is None else now) - first)

    def forget(self, symbols) -> None:
        """Drop symbols that rotated off the watchlist (bounds the dicts)."""
        for s in symbols:
            sym = (s or "").upper()
            for d in (self._pv, self._v, self._first, self._anchor):
                d.pop(sym, None)


def tape_gate_ok(tape: dict | None, min_sided: int = 4,
                 sided_share: float = 0.5) -> bool:
    """True when the tape carries enough REAL sided evidence to trust its
    dominance: at least `min_sided` prints with a side AND sided volume at
    least `sided_share` of total flow. Volume-aware, not print-count - a
    few large clearly-sided prints pass (real money moving fast), a tape
    that's mostly unsided noise doesn't. The single gate every consumer
    goes through (confidence pillar, bias meter, playbook, entry engine),
    so the banner can never abstain while the playbook still speaks."""
    if not tape or tape.get("sided_n", 0) < min_sided:
        return False
    sided_vol = tape.get("buy", 0) + tape.get("sell", 0)
    return sided_vol > 0 and sided_vol >= sided_share * tape.get("total", 0)


def market_bias(book: L2Book, t1: float | None, t5: float | None,
                wall_mult: float = 4.0, tape: dict | None = None, *,
                tape_min_sided: int = 4, tape_sided_share: float = 0.5):
    """One-glance verdict from the L2 evidence. Returns (score, label,
    reason) where score is -100 (max bearish) .. +100 (max bullish).

    Weights: imbalance +/-40, 1-min drift +/-30, 5-min drift +/-15,
    walls within 1% of the touch +/-15, and - when the Time&Sales feed
    is live - executed tape dominance +/-20. The tape term is the only
    input spoofers can't fake (it's transactions, not resting size);
    the total is clamped to +/-100.
    """
    imb = book.imbalance
    s_imb = max(-1.0, min(1.0, math.log2(max(imb, 1e-6)) / 1.5)) * 40
    s_t1 = max(-1.0, min(1.0, (t1 or 0.0) / 0.5)) * 30
    s_t5 = max(-1.0, min(1.0, (t5 or 0.0) / 1.5)) * 15
    s_w = 0.0
    for side, p, _ in book.walls(wall_mult):
        if side == "ASK" and 0 <= 100 * (p - book.best_ask) / book.best_ask <= 1.0:
            s_w -= 7.5   # supply wall overhead
        elif side == "BID" and 0 <= 100 * (book.best_bid - p) / book.best_bid <= 1.0:
            s_w += 7.5   # support wall just below
    s_w = max(-15.0, min(15.0, s_w))
    s_tape = 0.0
    if tape_gate_ok(tape, tape_min_sided, tape_sided_share):
        s_tape = max(-1.0, min(1.0, tape["dom"] / 0.5)) * 20
    score = max(-100.0, min(100.0, s_imb + s_t1 + s_t5 + s_w + s_tape))

    label = ("BULLISH" if score >= 30 else
             "BEARISH" if score <= -30 else "FLAT")

    parts = []
    if imb >= 1.5:
        parts.append(f"bids outweigh asks {imb:.1f}x")
    elif imb <= 0.67 and imb > 0:
        parts.append(f"asks outweigh bids {1 / imb:.1f}x")
    if s_t1 <= -15:
        parts.append("price falling now")
    elif s_t1 >= 15:
        parts.append("price rising now")
    if s_tape >= 10:
        parts.append("tape buying")
    elif s_tape <= -10:
        parts.append("tape selling")
    if s_w < 0:
        parts.append("supply wall overhead")
    elif s_w > 0:
        parts.append("support wall below")
    return score, label, ", ".join(parts) or "mixed / quiet"


class WallTracker:
    """Remembers each wall's peak size so we can tell when a wall is
    being eaten (collapsing) or has vanished (gone) - the level-defining
    events in the playbook."""

    def __init__(self, wall_mult: float = 4.0, collapse_ratio: float = 0.4,
                 gone_reads: int = 3):
        self.wall_mult = wall_mult
        self.collapse_ratio = collapse_ratio
        self.gone_reads = gone_reads
        self.state: dict = {}   # (side, price) -> {peak, miss}

    def update(self, book: L2Book) -> list:
        """Returns [(side, price, 'collapsing'|'gone'), ...] for this read."""
        events = []
        now_walls = {(s, round(p, 4)): sz for s, p, sz in
                     book.walls(self.wall_mult)}
        for key, sz in now_walls.items():
            st = self.state.get(key)
            if st is None:
                self.state[key] = {"peak": sz, "miss": 0}
                continue
            st["miss"] = 0
            if sz > st["peak"]:
                st["peak"] = sz
            elif sz <= self.collapse_ratio * st["peak"]:
                events.append((key[0], key[1], "collapsing"))
        for key, st in list(self.state.items()):
            if key not in now_walls:
                st["miss"] += 1
                if st["miss"] >= self.gone_reads:   # OCR-miss tolerant
                    events.append((key[0], key[1], "gone"))
                    del self.state[key]
        return events


def playbook(book: L2Book, t1: float | None, t5: float | None,
             wall_events: list, tape: dict | None = None, *,
             tape_min_sided: int = 4, tape_sided_share: float = 0.5) -> dict:
    """The daily-use rule, mechanized:
      trade in the direction where 1m and 5m agree,
      only when imbalance confirms it,
      walls define the trigger levels (break above / crack below),
      and the executed tape gets a veto: a "buy" setup where real money
      is hitting the bid (or vice versa) never fully triggers - stacked
      size with opposing prints is the classic spoof shape.
    """
    def up(v): return v is not None and v > 0.05
    def dn(v): return v is not None and v < -0.05
    trend_up, trend_dn = up(t1) and up(t5), dn(t1) and dn(t5)
    imb = book.imbalance
    imb_up, imb_dn = imb >= 1.3, imb <= 0.77
    ask_break = sorted({p for s, p, _ in wall_events
                        if s == "ASK" and p >= book.best_ask})
    bid_crack = sorted({p for s, p, _ in wall_events
                        if s == "BID" and p <= book.best_bid})
    tape_ok = tape_gate_ok(tape, tape_min_sided, tape_sided_share)
    tape_up = tape_ok and tape["dom"] >= 0.25
    tape_dn = tape_ok and tape["dom"] <= -0.25

    if trend_up and imb_up:
        verdict = ("LONG_TRIGGER" if ask_break and not tape_dn
                   else "LONG_SETUP")
    elif trend_dn and imb_dn:
        verdict = ("BEAR_CRACK" if bid_crack and not tape_up
                   else "BEARISH")
    else:
        verdict = "STAND_ASIDE"
    return {"trend_up": trend_up, "trend_dn": trend_dn,
            "imb_up": imb_up, "imb_dn": imb_dn, "imb": imb,
            "ask_break": ask_break, "bid_crack": bid_crack,
            "tape_ok": tape_ok, "tape_up": tape_up, "tape_dn": tape_dn,
            "verdict": verdict}


class BookFlow:
    """Cross-references RESTING size against EXECUTED prints - the signal
    class neither side of the feed can produce alone, and the hardest one
    to spoof: faking it requires actually trading.

    Two detectors, both fed each accepted book plus the prints ingested
    since the previous update:

      * wall PULLED vs CONSUMED - when a tracked wall disappears, the
        executed volume at its price (within a tolerance) while it stood
        decides what happened: traded <= pull_ratio x peak -> PULLED
        (displayed size withdrawn untouched: the classic spoof signature;
        counts toward a rolling per-side spoof score), traded >=
        consume_ratio x peak -> CONSUMED (a real level eaten through:
        directional evidence in the break direction). In between: no call.

        A wall can also leave for a reason that is neither: the book shows
        a FIXED DEPTH, so when price runs, levels left behind scroll out of
        the visible window. Nothing traded at them (price is elsewhere), so
        they used to land in PULLED and read as spoofs - and because a fast
        run is exactly what evicts levels, the artifact fired hardest on
        the clean moves this tool exists to catch. `_scrolled_out` tells
        the two apart by asking whether the WINDOW moved off the level or
        the level vanished under a stationary window; scroll-outs are
        dropped with no verdict. Same guard rejects OCR-garbage prices,
        which sit far outside the window when they evict.
      * absorption at the touch - sided volume hammering one side while
        that side's PRICE refuses to move (bid holds under heavy selling =
        someone is accumulating into the hits; mirrored at the ask). Uses
        price persistence + tape volume, deliberately NOT frame-to-frame
        OCR size deltas (too noisy to trust).

    Staged promotion: vote() only feeds the confidence 'book' pillar when
    cfg book_pillar=true; until then events/absorption are display+log
    only, so score_confidence can grade them on real sessions first."""

    def __init__(self, wall_mult: float = 4.0, tick: float = 0.01,
                 pull_ratio: float = 0.25, consume_ratio: float = 0.5,
                 gone_reads: int = 3, absorb_window: float = 45.0,
                 absorb_min_n: int = 6, absorb_share: float = 0.6,
                 event_keep: float = 120.0, vote_window: float = 45.0):
        self.wall_mult = wall_mult
        self.tick = tick
        self.pull_ratio = pull_ratio
        self.consume_ratio = consume_ratio
        self.gone_reads = gone_reads
        self.absorb_window = absorb_window
        self.absorb_min_n = absorb_min_n
        self.absorb_share = absorb_share
        self.event_keep = event_keep
        self.vote_window = vote_window
        self.walls: dict = {}     # (side, price) -> {peak, traded, miss}
        self.events: deque = deque()   # (ts, side, price, verdict)
        self.touch: deque = deque()    # (ts, best_bid, best_ask)
        self.prints: deque = deque()   # (ts, price, size, side)
        self.upto = 0.0                # watermark: newest print consumed
        self.absorb = 0                # last absorption read: -1/0/+1

    def _tol(self, mid: float) -> float:
        """Price tolerance for 'at this level': one tick or 0.1%,
        whichever is wider (penny grids don't fit $200 stocks)."""
        return max(self.tick, 0.001 * mid)

    @staticmethod
    def _level_gap(levels: list) -> float | None:
        """Median spacing between adjacent visible levels - the book's own
        grid, measured rather than assumed (it is not always the tick:
        Webull aggregates, and gaps are common in thin names)."""
        if len(levels) < 2:
            return None
        gaps = sorted(abs(levels[i + 1][0] - levels[i][0])
                      for i in range(len(levels) - 1))
        return gaps[len(gaps) // 2] or None

    def _scrolled_out(self, side: str, price: float, book: L2Book) -> bool:
        """Did this level leave the VISIBLE window rather than the market?

        The book is a fixed-depth peek, so 'gone from view' has two very
        different causes and only one is a spoof:

          * the window slid off it - price ran up and left a low bid
            behind, or fell and left a high ask behind. The order may well
            still be resting; we simply cannot see it. Not evidence.
          * it vanished while the window stayed put - the level really was
            withdrawn (or eaten). This is the case worth a verdict.

        Distinguished by position, not by history: only a level beyond the
        FAR edge of its side can have scrolled out, since the window can
        only slide away from a level in that direction. A wall pulled at or
        near the touch is never beyond the far edge, so genuine spoofs
        survive this guard. One level of slack absorbs the span shrinking
        by the removed wall itself.
        """
        levels = book.bids if side == "BID" else book.asks
        gap = self._level_gap(levels)
        if gap is None:
            return True          # can't measure the window -> don't guess
        slack = 1.5 * gap
        prices = [p for p, _ in levels]
        if side == "BID":
            return price < min(prices) - slack   # price ran up, left it below
        return price > max(prices) + slack       # price fell, left it above

    def update(self, book: L2Book, new_prints: list, now: float) -> dict:
        """Feed one accepted book + the prints since the last update.
        Returns {"events": [(side, price, verdict)...] (this read),
                 "absorb": -1/0/+1, "spoof_bid"/"spoof_ask": rolling counts,
                 "vote": the would-be book-pillar vote (may be None)}."""
        tol = self._tol(book.mid)
        for p in new_prints:
            ts, px, sz, side = p[0], p[1], p[2], p[3]
            self.prints.append((ts, px, sz, side))
            if ts > self.upto:
                self.upto = ts
            for (wside, wp), st in self.walls.items():
                if abs(px - wp) <= tol:
                    st["traded"] += sz
        while self.prints and now - self.prints[0][0] > self.absorb_window:
            self.prints.popleft()

        cur = {(s, round(p, 4)): sz for s, p, sz in
               book.walls(self.wall_mult)}
        for key, sz in cur.items():
            st = self.walls.get(key)
            if st is None:
                self.walls[key] = {"peak": sz, "traded": 0.0, "miss": 0}
            else:
                st["miss"] = 0
                if sz > st["peak"]:
                    st["peak"] = sz
        events = []
        for key, st in list(self.walls.items()):
            if key in cur:
                continue
            st["miss"] += 1
            if st["miss"] < self.gone_reads:   # OCR-miss tolerant, like
                continue                       # WallTracker.gone_reads
            traded, peak = st["traded"], st["peak"]
            if peak > 0 and not self._scrolled_out(key[0], key[1], book):
                if traded >= self.consume_ratio * peak:
                    events.append((key[0], key[1], "CONSUMED"))
                elif traded <= self.pull_ratio * peak:
                    events.append((key[0], key[1], "PULLED"))
            del self.walls[key]
        for s, p, verdict in events:
            self.events.append((now, s, p, verdict))
        while self.events and now - self.events[0][0] > self.event_keep:
            self.events.popleft()

        self.touch.append((now, book.best_bid, book.best_ask))
        while self.touch and now - self.touch[0][0] > self.absorb_window:
            self.touch.popleft()
        self.absorb = self._absorption(tol, now)

        return {"events": events, "absorb": self.absorb,
                "spoof_bid": self.spoof_count("BID"),
                "spoof_ask": self.spoof_count("ASK"),
                "vote": self.vote(now)}

    def _absorption(self, tol: float, now: float) -> int:
        """+1 = bid holding under heavy hits (bull), -1 = ask capping heavy
        lifts (bear), 0 = neither. Requires the touch history to actually
        span most of the window - 3 seconds of data is not persistence."""
        if len(self.prints) < self.absorb_min_n or len(self.touch) < 2:
            return 0
        if self.touch[-1][0] - self.touch[0][0] < 0.6 * self.absorb_window:
            return 0
        bids = [b for _, b, _ in self.touch]
        asks = [a for _, _, a in self.touch]
        total = sum(sz for _, _, sz, _ in self.prints)
        if total <= 0:
            return 0
        bid_stable = max(bids) - min(bids) <= tol
        ask_stable = max(asks) - min(asks) <= tol
        sell_at_bid = sum(sz for _, px, sz, s in self.prints
                          if s == "S" and px <= min(bids) + tol)
        buy_at_ask = sum(sz for _, px, sz, s in self.prints
                         if s == "B" and px >= max(asks) - tol)
        if bid_stable and sell_at_bid >= self.absorb_share * total:
            return 1
        if ask_stable and buy_at_ask >= self.absorb_share * total:
            return -1
        return 0

    def spoof_count(self, side: str) -> int:
        """PULLED walls on `side` over the rolling event window."""
        return sum(1 for _, s, _, v in self.events
                   if s == side and v == "PULLED")

    def vote(self, now: float) -> int | None:
        """The would-be 'book' pillar vote. Bull = absorption at the bid or
        an ask wall CONSUMED recently (supply eaten through); bear mirror.
        Both at once = 0 (conflict). None = nothing to say (thin window,
        no events) so the pillar abstains rather than votes flat."""
        recent = [(s, v) for ts, s, _, v in self.events
                  if now - ts <= self.vote_window]
        consumed_ask = any(s == "ASK" and v == "CONSUMED" for s, v in recent)
        consumed_bid = any(s == "BID" and v == "CONSUMED" for s, v in recent)
        bull = self.absorb == 1 or consumed_ask
        bear = self.absorb == -1 or consumed_bid
        if bull and bear:
            return 0
        if bull:
            return 1
        if bear:
            return -1
        if not recent and len(self.prints) < self.absorb_min_n:
            return None
        return 0


# The vwap pillar must span MUCH more time than the trend window (300s) or
# it is measuring the same price path and merely echoes it. The old 180s
# was SHORTER than the trend window, which is why trend/vwap measured
# corr +0.43. 15 min = 3x the trend window: long enough to be about the
# session rather than the last few candles.
VWAP_MIN_AGE_DEFAULT = 900.0


def confidence(t5: float | None, tape: dict | None,
               mid: float | None, vwap: float | None, *,
               tape_min_sided: int = 4, tape_sided_share: float = 0.5,
               tape_dom_min: float = 0.25,
               vwap_age: float | None = None,
               vwap_min_age: float = 0.0,
               book_vote: int | None = None,
               book_pillar: bool = False) -> dict:
    """The single 5-minute confidence signal.

    Three hard-to-spoof pillars each vote long / short / neutral, and
    confidence is how many AGREE - not the magnitude of any one of them:

      trend  - the 5-min robust drift (where price actually went)
      tape   - 60s executed buy/sell dominance (real money; unspoofable)
      vwap   - price above / below the "session" VWAP

    THEY ARE NOT ALL INDEPENDENT, and counting agreement assumes they are.
    Measured over the logged sessions (n=21,281 rows where both are live):

      tape  vs vwap   corr +0.08   agree 22.0% (17.6% if independent)
      trend vs tape   corr +0.19   agree 22.3% (19.9% if independent)
      trend vs vwap   corr +0.43   agree 67.0% (47.1% if independent)  <-

    So tape is a genuinely independent witness, but trend and vwap are
    substantially the same one: a 3/3 is nearer two witnesses than three,
    and the gauge overstates itself exactly where it says to size up.

    The cause was mechanical, not statistical, and is FIXED for callers
    that pass a `SessionVWAP`-derived vwap (the monitor does; see its
    docstring). Two things were wrong together:

      * the VWAP came from the local tape reader, which resets on every
        symbol switch -> it spanned a median of 106s, never over 11.8 min:
        not a session VWAP at all, just a slower trend;
      * vwap_min_age was 180s while trend_window is 300s, so a "mature"
        VWAP was YOUNGER than the trend it had to be independent of.

    `vwap_min_age` (verified: 0 violations in the logs) was always doing
    its job; it was simply set below the threshold that makes the pillar
    mean anything. See VWAP_MIN_AGE_DEFAULT.

    The correlations above are the BEFORE measurement, kept as the
    baseline to re-measure against once the rebuilt pillar has logged
    hours. It is not yet proven that the fix delivers independence live -
    only that the mechanism that destroyed it is gone.

    Whether 3/3 actually beats 2/3 forward is UNKNOWN, not established:
    6.3h of logs hold only ~128 independent 2-min windows in total (3/3
    gets ~16; ~101 are needed to detect a 60% win rate), and pooled over
    every directional call the edge is 50.6%, z=+0.11 - which rules out
    nothing weaker than ~61%. Do not read that as "it doesn't work"; read
    it as "not yet measurable". ~60h of logging would settle it.

    Imbalance is deliberately excluded: it's resting displayed size, the
    one input spoofers fake, and trusting it is what the losing paper log
    was built on. Direction is the majority of the pillars that have an
    opinion; `agree`/`total` drive the confidence meter (3/3 = size up,
    2/3 = normal, split = stand aside regardless of how extreme one meter
    looks). `tape_live` is False when the T&S feed isn't giving prints, so
    the caller can cap confidence and say so.

    Tape gate is VOLUME-aware, not print-count: it votes when at least
    `tape_min_sided` prints carry a real side AND the sided volume is at
    least `tape_sided_share` of the flow. So a few large clearly-sided
    prints (real money moving fast) vote - which the old '>= 8 raw prints'
    gate missed - while a tape that's mostly unsided noise abstains.

    VWAP maturity gate: the "VWAP" resets on every symbol switch, so for
    the first minutes it is really price-vs-a-3-minute-mean - a trend echo,
    not an independent pillar - and correlated pillars fake 3/3s exactly in
    the fast-hop decision window. When `vwap_age` (seconds since the tape/
    VWAP accumulator reset) is below `vwap_min_age`, the vwap pillar
    abstains like the tape gate does. Defaults keep old behavior (0 = always
    mature; age None = unknown = mature) so existing callers are unchanged.

    Fourth pillar (staged): when `book_pillar` is on, BookFlow.vote() joins
    as votes['book'] - absorption + consumed-wall evidence, the book x tape
    fusion that requires real executed volume to fake. It is OFF by default
    and should stay off until score_confidence shows the logged flow/absorb
    columns carry forward edge (the repo rule: log first, vote later)."""
    votes: dict = {}
    votes["trend"] = (None if t5 is None else
                      1 if t5 > 0.1 else -1 if t5 < -0.1 else 0)
    if tape_gate_ok(tape, tape_min_sided, tape_sided_share):
        d = tape["dom"]
        votes["tape"] = (1 if d >= tape_dom_min
                         else -1 if d <= -tape_dom_min else 0)
    else:
        votes["tape"] = None
    vwap_mature = (vwap_age is None or vwap_age >= vwap_min_age)
    if vwap and mid and vwap_mature:
        rel = (mid - vwap) / vwap
        votes["vwap"] = 1 if rel > 0.0005 else -1 if rel < -0.0005 else 0
    else:
        votes["vwap"] = None
    if book_pillar:
        votes["book"] = book_vote

    live = [v for v in votes.values() if v is not None]
    longs = sum(1 for v in live if v > 0)
    shorts = sum(1 for v in live if v < 0)
    if longs > shorts:
        direction, agree = "LONG", longs
    elif shorts > longs:
        direction, agree = "SHORT", shorts
    else:
        direction, agree = "NEUTRAL", 0
    return {"dir": direction, "agree": agree, "total": len(live),
            "votes": votes, "tape_live": votes["tape"] is not None}


def signal_quality(*, frame_age: float | None = None,
                   ok_rate: float | None = None,
                   glitch_rate: float | None = None,
                   tape_print_age: float | None = None,
                   ref_dev_pct: float | None = None,
                   have_ref: bool = True,
                   spread_pct: float | None = None,
                   spoof_events: int = 0,
                   sdk_book: bool = False) -> tuple[float, list[str]]:
    """Confidence in the confidence: how much the DATA under the current
    read can be trusted, 0..1, with reasons worst-first.

    The banner's pillars are only as good as their feed, and the feed has
    failure modes the pillars can't see - above all the frame-skip path,
    which re-stamps unchanged pixels as a fresh book, so a halted stock or
    an obscured Webull panel would keep voting trend+vwap forever. This
    function is where that staleness (and OCR misses, glitch drops,
    OCR-vs-stream disagreement, a dead tape, a wide spread) becomes a
    visible penalty instead of silent rot.

    Multiplicative: each problem scales q down independently. `sdk_book`
    marks a book that came from the Webull OpenAPI feed rather than OCR -
    the book-side penalties (frozen frame, OCR, glitches, ref deviation)
    don't apply; tape and spread penalties still do (the tape is OCR
    either way, and a wide spread is a market fact, not a feed artifact).
    UI mapping: A >= 0.8, B >= 0.5, else C (see quality_grade); a 3/3 on
    grade-C data is a different animal from a 3/3 on grade-A."""
    q = 1.0
    reasons: list[tuple[float, str]] = []

    def ding(factor: float, why: str):
        nonlocal q
        q *= factor
        reasons.append((factor, why))

    if not sdk_book:
        if frame_age is not None:
            if frame_age > 90:
                ding(0.2, f"book frozen {frame_age:.0f}s")
            elif frame_age > 30:
                ding(0.5, f"book frozen {frame_age:.0f}s")
            elif frame_age > 10:
                ding(0.8, f"book quiet {frame_age:.0f}s")
        if ok_rate is not None:
            if ok_rate < 0.5:
                ding(0.4, f"OCR failing ({100 * (1 - ok_rate):.0f}% misses)")
            elif ok_rate < 0.8:
                ding(0.7, f"OCR misses {100 * (1 - ok_rate):.0f}%")
        if glitch_rate is not None and glitch_rate > 0.2:
            ding(0.6, f"glitchy frames ({100 * glitch_rate:.0f}%)")
        if ref_dev_pct is not None:
            if ref_dev_pct > 5.0:
                ding(0.5, f"OCR vs stream differ {ref_dev_pct:.1f}%")
            elif ref_dev_pct > 2.0:
                ding(0.8, f"OCR vs stream differ {ref_dev_pct:.1f}%")
        elif not have_ref:
            ding(0.9, "no ref-price anchor")
    if tape_print_age is not None:
        if tape_print_age > 120:
            ding(0.7, f"no prints {tape_print_age:.0f}s")
        elif tape_print_age > 30:
            ding(0.85, f"tape slow ({tape_print_age:.0f}s)")
    if spread_pct is not None:
        if spread_pct > 2.0:
            ding(0.5, f"spread {spread_pct:.1f}%")
        elif spread_pct > 1.0:
            ding(0.7, f"spread {spread_pct:.1f}%")
    if spoof_events >= 5:   # a book this dishonest is barely a data source
        ding(0.55, f"very spoofy book ({spoof_events} pulled walls)")
    elif spoof_events >= 2:  # walls pulled untraded = displayed size lies
        ding(0.7, f"spoofy book ({spoof_events} pulled walls)")
    reasons.sort(key=lambda x: x[0])
    return max(0.0, min(1.0, q)), [w for _, w in reasons]


def quality_grade(q: float | None) -> str:
    """A/B/C label for a signal_quality score (None = unknown -> C)."""
    if q is None:
        return "C"
    return "A" if q >= 0.8 else "B" if q >= 0.5 else "C"


class LongView:
    """Slow, stable 5-minute stance for holds of roughly 10s-10min.

    Its raw read each poll is confidence() over trend + tape + VWAP (see
    that function - imbalance is intentionally out of the core signal);
    hysteresis then holds the stance until a new read persists for
    `long_confirm_secs`, so the headline confidence never flickers. Wall
    events over the window are still tracked for the detail line but no
    longer drive the stance.
    """

    def __init__(self, cfg: dict):
        self.window = cfg.get("long_window", 60)
        self.confirm = cfg.get("long_confirm_secs", 20)
        # volume-aware tape gate thresholds (see confidence())
        self.tape_min_sided = int(cfg.get("tape_min_sided", 4))
        self.tape_sided_share = float(cfg.get("tape_sided_share", 0.5))
        self.tape_dom_min = float(cfg.get("tape_dom_min", 0.25))
        # vwap pillar abstains until the accumulator is this old (secs)
        self.vwap_min_age = float(cfg.get("vwap_min_age",
                                          VWAP_MIN_AGE_DEFAULT))
        # 4th pillar (BookFlow) is staged: log/display until graded
        self.book_pillar = bool(cfg.get("book_pillar", False))
        self.samples = deque()     # (ts, imbalance)
        self.events = deque()      # (ts, side)
        self.stance = "NEUTRAL"
        self.since: float | None = None
        self._cand: str | None = None
        self._cand_since = 0.0

    def update(self, book: L2Book, t5: float | None,
               wall_events: list, now: float,
               tape: dict | None = None, vwap: float | None = None,
               vwap_age: float | None = None,
               book_vote: int | None = None) -> dict:
        if self.since is None:
            self.since = now
        self.samples.append((now, book.imbalance))
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        for s, p, _ in wall_events:
            self.events.append((now, s))
        while self.events and now - self.events[0][0] > self.window:
            self.events.popleft()

        imbs = sorted(i for _, i in self.samples)
        med_imb = imbs[len(imbs) // 2]
        ask_breaks = sum(1 for _, s in self.events if s == "ASK")
        bid_cracks = sum(1 for _, s in self.events if s == "BID")

        # raw stance = the confidence vote (LONG/SHORT/NEUTRAL -> our
        # LONG/BEAR/NEUTRAL vocabulary)
        conf = confidence(t5, tape, book.mid, vwap,
                          tape_min_sided=self.tape_min_sided,
                          tape_sided_share=self.tape_sided_share,
                          tape_dom_min=self.tape_dom_min,
                          vwap_age=vwap_age,
                          vwap_min_age=self.vwap_min_age,
                          book_vote=book_vote,
                          book_pillar=self.book_pillar)
        raw = {"LONG": "LONG", "SHORT": "BEAR",
               "NEUTRAL": "NEUTRAL"}[conf["dir"]]

        # hysteresis: candidate must persist before we switch
        pending = None
        pending_left = 0.0
        if raw == self.stance:
            self._cand = None
        elif raw == self._cand:
            if now - self._cand_since >= self.confirm:
                self.stance, self.since, self._cand = raw, now, None
            else:
                pending = raw
                pending_left = self.confirm - (now - self._cand_since)
        else:
            self._cand, self._cand_since = raw, now
            pending, pending_left = raw, float(self.confirm)

        return {"stance": self.stance, "held": now - self.since,
                "med_imb": med_imb, "t5": t5,
                "ask_breaks": ask_breaks, "bid_cracks": bid_cracks,
                "pending": pending, "pending_left": pending_left,
                "agree": conf["agree"], "total": conf["total"],
                "votes": conf["votes"], "tape_live": conf["tape_live"],
                "book_pillar": self.book_pillar}


def project_price(history, minutes: float = 5.0,
                  window: float = 120.0) -> tuple | None:
    """(mid_now, projected_mid) by extrapolating the recent drift.
    This is a pace estimate, not a prediction - walls and news bend it.
    Endpoints are median mids over a 5s band so a lone misread can't
    bend the projection."""
    if len(history) < 2:
        return None
    last = history[-1]
    cutoff = last.ts - window
    xs = [b for b in history if b.ts >= cutoff]
    if len(xs) < 2 or xs[-1].ts - xs[0].ts < 20:
        return None   # need at least 20s of history to call it a pace
    band = 5.0
    m0 = _median([b.mid for b in xs if b.ts <= xs[0].ts + band])
    mid_now = _median([b.mid for b in xs if b.ts >= xs[-1].ts - band])
    rate = (mid_now - m0) / (xs[-1].ts - xs[0].ts)   # $/sec
    target = mid_now + rate * minutes * 60.0
    # violent tapes extrapolate to nonsense (even negative prices);
    # clamp the projection to a +/-25% band
    target = max(0.01, min(mid_now * 1.25, max(mid_now * 0.75, target)))
    return mid_now, target


@dataclass
class Signal:
    action: str      # "BUY" / "SELL"
    reason: str
    price: float
    imbalance: float
    ts: float = field(default_factory=time.time)


class SignalEngine:
    """Turns a stream of L2Book snapshots into confirmed BUY/SELL signals.

    Entry rules (all tunable via config):
      BUY  - imbalance >= imbalance_buy for `confirm_reads` consecutive reads,
             spread <= max_spread_pct, no large ask wall at best ask,
             mid-price not falling (momentum filter), enough room to the
             nearest ask wall above (min_room_pct), AND the executed tape
             isn't selling (the tape veto - resting bids mean nothing if
             real money is hitting the bid; this is the spoof filter that
             the 52-stop-out paper log was missing).
      SELL - imbalance <= imbalance_sell for `confirm_reads` consecutive reads.
    A cooldown prevents alert spam.
    """

    def __init__(self, cfg: dict):
        self.buy_th = cfg.get("imbalance_buy", 1.8)
        self.sell_th = cfg.get("imbalance_sell", 0.55)
        self.confirm = cfg.get("confirm_reads", 3)
        self.max_spread = cfg.get("max_spread_pct", 1.0)
        self.wall_mult = cfg.get("wall_multiple", 4.0)
        self.cooldown = cfg.get("alert_cooldown", 30)
        self.min_room = cfg.get("min_room_pct", 1.0)
        self.momentum_reads = cfg.get("momentum_reads", 6)
        self.trend_window = cfg.get("trend_window", 300)
        self.max_downtrend = cfg.get("max_downtrend_pct", 0.2)
        self.min_coverage = cfg.get("trend_min_coverage", 0.6)
        self.tape_gate = cfg.get("tape_gate_entries", True)
        self.tape_dom_min = cfg.get("tape_dom_min", 0.25)
        self.tape_min_sided = int(cfg.get("tape_min_sided", 4))
        self.tape_sided_share = float(cfg.get("tape_sided_share", 0.5))
        # ~11 min of history at 3 reads/s, enough for a 5-min trend
        self.history = deque(maxlen=2000)
        # (ts, mid) points pre-loaded from a real-time trade stream on a
        # symbol switch, so the trend pillar is valid immediately instead of
        # blind for min_coverage*trend_window (~3 min). Trend-only.
        self.seed: list[tuple[float, float]] = []
        self._buy_streak = 0
        self._sell_streak = 0
        self._last_alert = 0.0

    def reset(self):
        """Forget all per-symbol state (called on symbol switch - trends
        and streaks computed across two different stocks are garbage)."""
        self.history.clear()
        self.seed = []
        self._buy_streak = self._sell_streak = 0

    def seed_history(self, points):
        """Pre-load (ts, mid) points (e.g. from the Finnhub trade stream that
        was pre-subscribed to your watchlist) so the trend pillar has a real
        5-minute read the moment you switch symbols, instead of warming up
        blind. Seeds feed ONLY trend_pct/trend_span; live OCR history alone
        still drives entries and the momentum filter, so a seed can never by
        itself trigger a trade. Seeds age out of the window as live reads
        accumulate. Call right AFTER reset() on a switch."""
        self.seed = sorted((float(t), float(m)) for t, m in points
                           if m and float(m) > 0)

    def _trend_series(self, seconds: float) -> list:
        """Windowed (ts, mid) series for the trend view. Seeds are kept
        strictly OLDER than the oldest live read (never interleaved), so the
        recent-band endpoint is always live once reads exist and seeds only
        extend the window backwards during warm-up.

        Runs on every render, so the common steady state (no seed) takes the
        fast path: scan the live history tail only. Seeds are dropped the
        moment live history alone covers the window, so they never cost
        anything once warmed."""
        hist = self.history
        live_last = hist[-1].ts if hist else None
        seed_last = self.seed[-1][0] if self.seed else None
        if live_last is None and seed_last is None:
            return []
        last_ts = live_last if seed_last is None else (
            seed_last if live_last is None else max(live_last, seed_last))
        cutoff = last_ts - seconds
        # once live reads span the window, the seeds are dead weight -> drop
        if self.seed and hist and hist[0].ts <= cutoff:
            self.seed = []
        live = [(b.ts, b.mid) for b in hist if b.ts >= cutoff]
        if not self.seed:
            return live
        oldest_live = live[0][0] if live else float("inf")
        return [(t, m) for (t, m) in self.seed
                if cutoff <= t < oldest_live] + live

    def trend_pct(self, seconds: float) -> float | None:
        """Mid-price change (%) over the last `seconds` of history.
        The bigger-picture view: where has price actually been drifting.

        Robust: each endpoint is the MEDIAN mid over a short band, so one
        surviving misread can't swing the number; and the window must be
        `trend_min_coverage` covered before a value is returned — 40s of
        history is never reported as a "5-minute trend" (callers show …
        and the playbook stays conservative until the data is real)."""
        series = self._trend_series(seconds)
        if len(series) < 2:
            return None
        last_ts = series[-1][0]
        cutoff = last_ts - seconds
        xs = [(t, m) for (t, m) in series if t >= cutoff]
        if len(xs) < 2 or xs[-1][0] - xs[0][0] < self.min_coverage * seconds:
            return None
        band = max(3.0, seconds * 0.05)
        m0 = _median([m for (t, m) in xs if t <= xs[0][0] + band])
        m1 = _median([m for (t, m) in xs if t >= xs[-1][0] - band])
        return 100.0 * (m1 - m0) / m0 if m0 else None

    def trend_span(self, seconds: float) -> float:
        """Seconds of history actually available inside the trailing
        window — lets the UI say 'warming up: 2.1m of 5m'. Counts seeded
        history too, so a freshly-switched, stream-seeded symbol reads as
        warm rather than 'warming up'."""
        series = self._trend_series(seconds)
        if len(series) < 2:
            return 0.0
        last_ts = series[-1][0]
        cutoff = last_ts - seconds
        base = next((t for (t, m) in series if t >= cutoff), None)
        return (last_ts - base) if base is not None else 0.0

    def update(self, book: L2Book, tape: dict | None = None) -> Signal | None:
        self.history.append(book)
        imb = book.imbalance
        walls = book.walls(self.wall_mult)
        ask_wall_at_touch = any(
            side == "ASK" and abs(price - book.best_ask) < 1e-9
            for side, price, _ in walls
        )
        bid_wall_at_touch = any(
            side == "BID" and abs(price - book.best_bid) < 1e-9
            for side, price, _ in walls
        )

        self._buy_streak = self._buy_streak + 1 if imb >= self.buy_th else 0
        self._sell_streak = self._sell_streak + 1 if imb <= self.sell_th else 0

        now = book.ts
        if now - self._last_alert < self.cooldown:
            return None

        if (self._buy_streak >= self.confirm
                and book.spread_pct <= self.max_spread
                and not ask_wall_at_touch):
            # momentum filter: mid-price must not be falling
            recent = list(self.history)[-self.momentum_reads:]
            mids = [(b.best_bid + b.best_ask) / 2 for b in recent]
            if len(mids) >= 2 and mids[-1] < mids[0]:
                return None  # buyers stacking but price sliding -> skip
            # bigger-picture filter: don't buy against the broader drift
            drift = self.trend_pct(self.trend_window)
            if drift is not None and drift < -self.max_downtrend:
                return None  # book looks bullish but stock is bleeding
            # tape veto: stacked bids but real money is hitting the bid ->
            # the classic spoof that turned into stop-outs. Only vetoes
            # when the tape has enough prints to be trusted.
            tape_note = ""
            if self.tape_gate and tape_gate_ok(
                    tape, self.tape_min_sided, self.tape_sided_share):
                if tape["dom"] <= -self.tape_dom_min:
                    return None  # executed flow is selling -> not a real bid
                tape_note = f", tape {tape['dom']:+.2f}"
            # room filter: profit room to the nearest ask wall above
            above = [p for s, p, _ in walls
                     if s == "ASK" and p > book.best_ask]
            room = ""
            if above:
                room_pct = 100.0 * (min(above) - book.best_ask) / book.best_ask
                if room_pct < self.min_room:
                    return None  # a lid sits too close above -> no edge
                room = f", {room_pct:.1f}% room to ask wall {min(above):.3f}"
            self._last_alert = now
            reason = (f"imbalance {imb:.2f} for {self._buy_streak} reads, "
                      f"spread {book.spread_pct:.2f}%{room}")
            if drift is not None:
                reason += f", 5m drift {drift:+.2f}%"
            reason += tape_note
            if bid_wall_at_touch:
                reason += ", bid wall support at touch"
            return Signal("BUY", reason, book.best_ask, imb)

        if self._sell_streak >= self.confirm:
            self._last_alert = now
            return Signal(
                "SELL",
                f"imbalance collapsed to {imb:.2f} for {self._sell_streak} reads"
                + (", ask wall at touch" if ask_wall_at_touch else ""),
                book.best_bid, imb,
            )
        return None


@dataclass
class Trade:
    entry_ts: float
    entry: float
    exit_ts: float
    exit: float
    pnl_pct: float
    reason: str
    symbol: str = ""


class PaperTrader:
    """Anchors exits to an entry price so profit can actually be captured.

    Opens a virtual long at the ask on each BUY signal, then watches for:
      TAKE_PROFIT - bid reaches entry + target_pct
      STOP        - bid falls to entry - stop_pct
      TRAIL_EXIT  - gave back trail_pct from the peak after being up
      SELL        - order flow turned (engine SELL) while in position
    Every round trip is returned as a Trade for logging, so you can review
    which setups actually made money and tune the thresholds.
    """

    def __init__(self, cfg: dict):
        self.target = cfg.get("target_pct", 2.0)
        self.stop = cfg.get("stop_pct", 1.0)
        self.trail = cfg.get("trail_pct", 0.8)
        self.entry: float | None = None
        self.entry_ts: float | None = None
        self.hwm: float | None = None
        self.real = False                    # anchored to a broker position
        self._real_key: tuple | None = None  # (qty, avg_price) anchored to
        self._real_muted: tuple | None = None  # already alerted this state

    @property
    def in_position(self) -> bool:
        return self.entry is not None

    def drop_virtual(self) -> bool:
        """Forget a virtual position without logging a trade. Used on
        symbol switch: PnL of an entry on one stock against another
        stock's book is meaningless (this produced the 171.58 -> 143
        'trades' in trades.csv). Real-anchored positions are left alone;
        sync_real re-resolves them against the new symbol's feed."""
        if self.in_position and not self.real:
            self.entry = self.entry_ts = self.hwm = None
            return True
        return False

    def sync_real(self, pos: dict | None, book: L2Book) -> str | None:
        """Anchor exits to a real broker position (position_state.json).

        pos = {'qty', 'avg_price'} for the monitored symbol, or None when
        the broker holds nothing in it. A real anchor replaces any virtual
        entry, so TAKE_PROFIT/STOP/TRAIL fire off the actual fill price.
        After an exit alert the same (qty, avg) is muted so a still-open
        broker position doesn't re-alert every read; adding shares (new
        avg) or re-entering re-arms it. Returns a note when the anchor
        changes, else None."""
        if pos and float(pos.get("avg_price") or 0) > 0:
            key = (float(pos.get("qty") or 0), float(pos["avg_price"]))
            if self._real_muted == key or self._real_key == key:
                return None
            self.entry, self.entry_ts = key[1], book.ts
            self.hwm = book.best_bid
            self.real, self._real_key = True, key
            self._real_muted = None
            return f"exits anchored to real fill @ {key[1]:.3f} x{key[0]:g}"
        if self.real:
            self.entry = self.entry_ts = self.hwm = None
            self.real, self._real_key = False, None
            return "broker flat — real anchor cleared"
        return None

    def unrealized_pct(self, book: L2Book) -> float:
        if not self.in_position:
            return 0.0
        return 100.0 * (book.best_bid - self.entry) / self.entry

    def peak_pct(self) -> float:
        if not self.in_position or self.hwm is None:
            return 0.0
        return 100.0 * (self.hwm - self.entry) / self.entry

    def update(self, book: L2Book, sig: Signal | None, symbol: str = ""):
        """Returns (exit_signal | None, trade | None)."""
        if not self.in_position:
            if sig and sig.action == "BUY":
                self.entry = book.best_ask
                self.entry_ts = book.ts
                self.hwm = book.best_bid
            return None, None

        bid = book.best_bid
        self.hwm = max(self.hwm, bid)
        up = self.unrealized_pct(book)
        peak = self.peak_pct()

        action = reason = None
        if up >= self.target:
            action = "TAKE_PROFIT"
            reason = f"target hit: {up:+.2f}% (entry {self.entry:.3f})"
        elif up <= -self.stop:
            action = "STOP"
            reason = f"stop hit: {up:+.2f}% (entry {self.entry:.3f})"
        elif peak >= self.trail and peak - up >= self.trail:
            action = "TRAIL_EXIT"
            reason = (f"giving back gains: peak {peak:+.2f}% "
                      f"now {up:+.2f}% (entry {self.entry:.3f})")
        elif sig and sig.action == "SELL":
            action = "SELL"
            reason = f"flow turned: {sig.reason} (uPnL {up:+.2f}%)"

        if action is None:
            return None, None
        trade = Trade(self.entry_ts, self.entry, book.ts, bid, up, reason,
                      symbol)
        if self.real:   # one alert per broker position state
            self._real_muted = self._real_key
        self.entry = self.entry_ts = self.hwm = None
        self.real, self._real_key = False, None
        return Signal(action, reason, bid, book.imbalance, ts=book.ts), trade
