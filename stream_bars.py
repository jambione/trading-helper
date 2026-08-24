"""One-minute bars built from the desk's own price samples. Shadow only.

The indicators run on Alpaca IEX minute bars, and IEX is a few percent of
the consolidated tape. A thin name does not print a bar every minute, so a
%R meant to cover 14 one-minute bars spans **23 minutes at the median and
69 at p90** (2026-08-24), and 44% of readings come back `clock_range` —
position-in-range over whatever the window held — wearing the same label
in the same column as a real %R.

The tape tells a different story about the same names. `tape_age_sec`,
logged for exactly this question and never read until now, puts the median
age of the last print at **11.0s** (p90 34s, p99 66s) over 11,029
observations. That is roughly five prints a minute against IEX's ~0.6
bars a minute.

**Sampled, not streamed, and the distinction is real.** The Finnhub
websocket lives in `signal_engine.py`; `ai_entry_watch` runs inside
`ai_trader.py`. Different processes — a trade callback registered here
would never fire. What crosses is the shared dashboard state, which
`live_print` already reads, so this folds the price the watch loop
observes each poll (~2s) into minute OHLC. Since the poll is ~2s and the
median print gap is 11s, essentially every price change is seen. What is
lost is volume, and any extreme that appears and reverts inside one
sampling interval. `on_trade` is kept for the day this moves into the
engine process and can aggregate real trades.

Repeated identical prices are harmless: folding the same value into
high/low/close is idempotent. Only the sample count inflates, which is
why it is called `n_samples` and never reported as volume.

Rows come back in exactly ``clock_window_rows``' shape —
``[(high, low, close)]`` plus a span — so the SAME
``_live_percent_r_line`` runs against both sources and any difference is
attributable to the bars and nothing else. That is how "would a denser
feed fix the window" becomes a measurement instead of a projection.

**Nothing here decides anything.** GATE 1 is mid-flight and the operator's
stage-2 rule depends entirely on %R quality, so changing indicator inputs
now would make data from before and after incomparable.

Known limits, all real:

  forward only    No history. A symbol admitted at 09:35 has no window
                  until ~09:49. Live use would need an Alpaca seed;
                  shadow mode reports the gap rather than filling it.
  ~2% empty       p99 print gap is 66s, so the odd minute carries nothing.
  fresh != whole  tape_age_sec says how fresh the last print WE SAW was,
                  not that we saw every print. Denser than IEX, still not
                  consolidated tape.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

BAR_SEC = 60.0
# Two hours of minutes. The longest window any %R length asks for is a
# small multiple of 14, so this is generous and bounded.
MAX_BARS = 120

_LOCK = threading.RLock()
# symbol -> deque[(bucket_start_ts, high, low, close, n_samples)]
_BARS: dict[str, deque] = defaultdict(lambda: deque(maxlen=MAX_BARS))
_CUR: dict[str, list] = {}          # symbol -> [bucket, hi, lo, close, n]
_STATS = {"samples": 0, "started_at": None}


def _bucket(ts: float) -> float:
    return float(int(float(ts) // BAR_SEC) * BAR_SEC)


def observe(symbol: str, price: Any, ts: Any = None) -> None:
    """Fold one observed price into its minute. Never raises.

    Called from the shadow-row builder, so it runs once per watched name
    per poll. A logging path may not throw into the entry loop.
    """
    try:
        sym = str(symbol or "").upper().strip()
        px = float(price)
        if not sym or px <= 0:
            return
        t = float(ts) if ts else time.time()
        if t > 1e11:                 # milliseconds
            t /= 1000.0
        if t <= 0:
            t = time.time()
        b = _bucket(t)
        with _LOCK:
            if _STATS["started_at"] is None:
                _STATS["started_at"] = t
            _STATS["samples"] += 1
            cur = _CUR.get(sym)
            if cur is None or b > cur[0]:
                if cur is not None:
                    _BARS[sym].append(tuple(cur))
                _CUR[sym] = [b, px, px, px, 1]
            elif b < cur[0]:
                return               # late sample for a closed minute
            else:
                cur[1] = max(cur[1], px)
                cur[2] = min(cur[2], px)
                cur[3] = px
                cur[4] += 1
    except Exception:  # noqa: BLE001
        return


def on_trade(symbol: str, price: float, volume: Any, ts: Any) -> None:
    """Finnhub trade-callback shape, for when this runs in the engine.

    Unused today: the websocket is in signal_engine.py and this module is
    imported by ai_entry_watch inside ai_trader.py. Kept so moving the
    aggregator into the engine process is a wiring change, not a rewrite.
    """
    observe(symbol, price, ts)


def _closed_and_open(sym: str) -> list[tuple]:
    """Closed bars plus the minute in progress, oldest first."""
    with _LOCK:
        out = list(_BARS.get(sym) or ())
        cur = _CUR.get(sym)
        if cur is not None:
            out.append(tuple(cur))
    return out


def window_rows(symbol: str, now: float, length: int,
                slack: float = 1.25) -> tuple[list[tuple[float, float, float]],
                                              float | None]:
    """``(rows, span_sec)`` in ``clock_window_rows``' exact shape.

    Same clock discipline as the live path: keep only bars whose stamp is
    within ``(length-1) * 60 * slack`` of the newest, so a sparse stretch
    shortens the list rather than silently widening the window into an
    hour of range and calling it a 14-minute %R.
    """
    sym = str(symbol or "").upper().strip()
    bars = _closed_and_open(sym)
    if not bars:
        return [], None
    length = max(2, int(length))
    horizon = (length - 1) * BAR_SEC * max(1.0, float(slack))
    newest = bars[-1][0]
    kept = [b for b in bars if b[0] + 1e-9 >= newest - horizon]
    if not kept:
        return [], None
    kept = kept[-length:]
    rows = [(float(b[1]), float(b[2]), float(b[3])) for b in kept]
    span = (kept[-1][0] - kept[0][0] + BAR_SEC) if len(kept) > 1 else BAR_SEC
    return rows, float(span)


def coverage(symbol: str) -> dict:
    """What the aggregator actually holds for a name.

    ``empty_minutes`` is the honest density measure: minutes inside the
    covered span that carry no observation at all.
    """
    sym = str(symbol or "").upper().strip()
    bars = _closed_and_open(sym)
    if not bars:
        return {"bars": 0, "span_min": None, "samples": 0,
                "empty_minutes": None}
    span_min = (bars[-1][0] - bars[0][0]) / 60.0 + 1.0
    return {
        "bars": len(bars),
        "span_min": round(span_min, 1),
        "samples": sum(int(b[4]) for b in bars),
        "empty_minutes": max(0, int(round(span_min)) - len(bars)),
    }


def stats() -> dict:
    with _LOCK:
        return {"samples": _STATS["samples"], "symbols": len(_BARS),
                "started_at": _STATS["started_at"]}


def reset() -> None:
    """Tests only."""
    with _LOCK:
        _BARS.clear()
        _CUR.clear()
        _STATS.update({"samples": 0, "started_at": None})
