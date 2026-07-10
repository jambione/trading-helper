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


def market_bias(book: L2Book, t1: float | None, t5: float | None,
                wall_mult: float = 4.0):
    """One-glance verdict from the L2 evidence. Returns (score, label,
    reason) where score is -100 (max bearish) .. +100 (max bullish).

    Weights: imbalance +/-40, 1-min drift +/-30, 5-min drift +/-15,
    walls within 1% of the touch +/-15.
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
    score = s_imb + s_t1 + s_t5 + s_w

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
             wall_events: list) -> dict:
    """The daily-use rule, mechanized:
      trade in the direction where 1m and 5m agree,
      only when imbalance confirms it,
      walls define the trigger levels (break above / crack below).
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

    if trend_up and imb_up:
        verdict = "LONG_TRIGGER" if ask_break else "LONG_SETUP"
    elif trend_dn and imb_dn:
        verdict = "BEAR_CRACK" if bid_crack else "BEARISH"
    else:
        verdict = "STAND_ASIDE"
    return {"trend_up": trend_up, "trend_dn": trend_dn,
            "imb_up": imb_up, "imb_dn": imb_dn, "imb": imb,
            "ask_break": ask_break, "bid_crack": bid_crack,
            "verdict": verdict}


class LongView:
    """Slow, stable stance for holds of roughly 10s-10min.

    Uses smoothed inputs (median imbalance over `long_window` seconds,
    the 5-min drift, net wall pressure over the window) plus hysteresis:
    the stance only switches after the new reading has held steady for
    `long_confirm_secs`. No flicker.
    """

    def __init__(self, cfg: dict):
        self.window = cfg.get("long_window", 60)
        self.confirm = cfg.get("long_confirm_secs", 20)
        self.samples = deque()     # (ts, imbalance)
        self.events = deque()      # (ts, side)
        self.stance = "NEUTRAL"
        self.since: float | None = None
        self._cand: str | None = None
        self._cand_since = 0.0

    def update(self, book: L2Book, t5: float | None,
               wall_events: list, now: float) -> dict:
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

        # raw stance from the slow inputs
        if med_imb >= 1.3 and (t5 or 0.0) > 0.1 and ask_breaks >= bid_cracks:
            raw = "LONG"
        elif med_imb <= 0.77 and (t5 or 0.0) < -0.1:
            raw = "BEAR"
        else:
            raw = "NEUTRAL"

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
                "pending": pending, "pending_left": pending_left}


def project_price(history, minutes: float = 5.0,
                  window: float = 120.0) -> tuple | None:
    """(mid_now, projected_mid) by extrapolating the recent drift.
    This is a pace estimate, not a prediction - walls and news bend it."""
    if len(history) < 2:
        return None
    last = history[-1]
    mid_now = (last.best_bid + last.best_ask) / 2
    cutoff = last.ts - window
    base = next((b for b in history if b.ts >= cutoff), None)
    if base is None or last.ts - base.ts < 20:
        return None   # need at least 20s of history to call it a pace
    m0 = (base.best_bid + base.best_ask) / 2
    rate = (mid_now - m0) / (last.ts - base.ts)   # $/sec
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
             mid-price not falling (momentum filter), and enough room to the
             nearest ask wall above (min_room_pct) to make the trade worth it.
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
        # ~11 min of history at 3 reads/s, enough for a 5-min trend
        self.history = deque(maxlen=2000)
        self._buy_streak = 0
        self._sell_streak = 0
        self._last_alert = 0.0

    def trend_pct(self, seconds: float) -> float | None:
        """Mid-price change (%) over the last `seconds` of history.
        The bigger-picture view: where has price actually been drifting."""
        if len(self.history) < 2:
            return None
        last = self.history[-1]
        cutoff = last.ts - seconds
        base = next((b for b in self.history if b.ts >= cutoff), None)
        if base is None or base is last:
            return None
        m0 = (base.best_bid + base.best_ask) / 2
        m1 = (last.best_bid + last.best_ask) / 2
        return 100.0 * (m1 - m0) / m0 if m0 else None

    def update(self, book: L2Book) -> Signal | None:
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

    @property
    def in_position(self) -> bool:
        return self.entry is not None

    def unrealized_pct(self, book: L2Book) -> float:
        if not self.in_position:
            return 0.0
        return 100.0 * (book.best_bid - self.entry) / self.entry

    def peak_pct(self) -> float:
        if not self.in_position or self.hwm is None:
            return 0.0
        return 100.0 * (self.hwm - self.entry) / self.entry

    def update(self, book: L2Book, sig: Signal | None):
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
        trade = Trade(self.entry_ts, self.entry, book.ts, bid, up, reason)
        self.entry = self.entry_ts = self.hwm = None
        return Signal(action, reason, bid, book.imbalance, ts=book.ts), trade
