"""The tape-led early-hop upgrade (webull-l2).

The warm-up mismatch: on a symbol switch the TREND pillar votes instantly
(seeded from the pre-subscribed Finnhub stream) while the TAPE resets to
empty and the VWAP usually abstains -- so early "confidence" was one
seeded price-drift witness, the inverse of a tape-led read, in exactly
the 2-3 minute hop window where the money is made.

Covered here:
  * Tape.ingest_stream / Tape.seed_prints - real executed prints from the
    trade stream, sided by quote/tick rule (never color: streams carry no
    aggressor paint), seeding the dominance window on arrival.
  * dedupe hand-off - seeded prints leave frame signatures so an OCR
    frame showing the same trades doesn't double-count them.
  * SignalEngine.seed_fraction - how much of the trend window is pre-hop
    seed, the number the banner's "(seeded)" tag and the hop gate key on.
  * LongView pillar provenance meta.
  * tape_confirms - the presentation-level tape-lead gate: a seeded
    majority may not show "size up" until the live tape agrees.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webull-l2"))

from l2_core import L2Book, LongView, SignalEngine, tape_confirms  # noqa: E402
from tape_core import Tape  # noqa: E402


def book(bid=5.00, ask=5.05, ts=1000.0):
    bids = [(bid, 100.0), (bid - 0.05, 100.0), (bid - 0.10, 100.0)]
    asks = [(ask, 100.0), (ask + 0.05, 100.0), (ask + 0.10, 100.0)]
    return L2Book(bids, asks, ts=ts)


def tape_dict(buy=0.0, sell=0.0, sided_n=0):
    sided = buy + sell
    return {"n": sided_n, "sided_n": sided_n, "buy": buy, "sell": sell,
            "total": sided, "dom": (buy - sell) / sided if sided else 0.0}


# --------------------------------------------------- stream ingest ----------

def test_stream_prints_sided_by_quote_rule_when_touch_known():
    t = Tape()
    n = t.ingest_stream([(1000.0, 5.06, 300), (1001.0, 5.00, 200)],
                        bid=5.00, ask=5.06)
    assert n == 2
    sides = [(p[3], p[4]) for p in t.prints]
    # at the ask = buyer, at the bid = seller; src 'Q', never 'C'
    assert sides == [("B", "Q"), ("S", "Q")]


def test_stream_prints_fall_back_to_tick_rule_without_quotes():
    t = Tape()
    t.ingest_stream([(1000.0, 5.00, 100), (1001.0, 5.02, 100),
                     (1002.0, 4.99, 100)])
    sides = [(p[3], p[4]) for p in t.prints]
    assert sides[0] == ("N", "N")          # nothing to compare against yet
    assert sides[1] == ("B", "T")          # uptick = buy, and it's a GUESS
    assert sides[2] == ("S", "T")          # downtick = sell
    # dom_w downweights exactly these tick-rule calls
    m = t.metrics(1002.0, 60, tick_weight=0.5)
    assert m["dom_w"] == 0.0               # 0.5*100 buy vs 0.5*100 sell


def test_stream_junk_prints_are_dropped():
    t = Tape()
    assert t.ingest_stream([(1000.0, 0.0, 100), (1000.0, 5.0, 0),
                            (1000.0, -1.0, 50)]) == 0
    assert not t.prints


def test_stream_prints_keep_their_own_timestamps():
    """Seeded history must age out of the 60s window like live prints --
    a 9-minute-old print must not count toward the current dominance."""
    t = Tape()
    t.ingest_stream([(1000.0, 5.00, 100), (1500.0, 5.06, 400)],
                    bid=5.00, ask=5.06)
    m = t.metrics(1520.0, 60)
    assert m["buy"] == 400 and m["sell"] == 0     # only the fresh print


# --------------------------------------------------- seeding a hop ----------

def test_seed_prints_gives_dominance_on_arrival():
    """The point of the feature: right after reset() the 60s window has
    real sided evidence instead of an empty tape."""
    t = Tape()
    t.reset()
    now = 2000.0
    pts = [(now - 30 + i, 5.06, 500) for i in range(5)]   # recent lifts
    assert t.seed_prints(pts, bid=5.00, ask=5.06) == 5
    m = t.metrics(now, 60)
    assert m["sided_n"] == 5 and m["dom"] == 1.0
    assert t.vwap() == 5.06


def test_seed_backdates_started_to_the_evidence():
    """started marks the SPAN of evidence (tape_age, accel baseline); a
    seed of 10-minute-old prints must not read as one second of tape."""
    t = Tape()
    t.reset()
    t.seed_prints([(1000.0, 5.0, 100), (1400.0, 5.0, 100)])
    assert t.started == 1000.0


def test_seeded_prints_dedupe_against_the_first_ocr_frame():
    """Seeds leave frame signatures (hh:mm:ss, price, size), so when the
    OCR panel shows the SAME trades a moment later the normal cross-frame
    dedupe drops them instead of double-counting a whole frame."""
    t = Tape()
    ts1, ts2 = 2000.0, 2001.0
    t.seed_prints([(ts1, 5.05, 100), (ts2, 5.06, 300)],
                  bid=5.00, ask=5.06)
    tstr1 = time.strftime("%H:%M:%S", time.localtime(ts1))
    tstr2 = time.strftime("%H:%M:%S", time.localtime(ts2))
    # OCR frame: one genuinely new print on top of the two seeded trades
    rows = [("10:00:09", 5.06, 200, "B"),
            (tstr2, 5.06, 300, "N"),
            (tstr1, 5.05, 100, "N")]
    new = t.ingest_frame(rows, 2002.0, bid=5.00, ask=5.06)
    assert new == 1                        # only the new print ingested
    assert len(t.prints) == 3


# --------------------------------------------------- trend provenance -------

def test_seed_fraction_all_seed_then_decaying_then_live():
    eng = SignalEngine({})
    t0 = 10_000.0
    eng.seed_history([(t0 - 240 + i * 10, 5.0) for i in range(24)])
    assert eng.seed_fraction(300) == 1.0           # nothing live yet
    for i in range(3):                             # live reads arrive
        eng.history.append(book(ts=t0 + i))
    frac = eng.seed_fraction(300)
    assert 0.5 < frac < 1.0                        # mostly seed, some live
    eng.seed = []                                  # seeds aged out
    assert eng.seed_fraction(300) == 0.0


def test_longview_reports_pillar_provenance():
    lv = LongView({"long_confirm_secs": 0})
    out = lv.update(book(), 0.5, [], 1000.0,
                    tape=tape_dict(buy=5000, sell=0, sided_n=5),
                    vwap=4.9, vwap_age=1200.0,
                    trend_seed_frac=0.8, tape_span=12.0, vwap_src="session")
    meta = out["meta"]
    assert meta["trend_src"] == "seed"             # >= 0.5 = seeded read
    assert meta["tape_span"] == 12.0 and meta["tape_window"] == 60.0
    assert meta["vwap_age"] == 1200.0 and meta["vwap_src"] == "session"
    assert meta["vwap_min_age"] == lv.vwap_min_age
    mixed = lv.update(book(), 0.5, [], 1001.0, trend_seed_frac=0.2)
    assert mixed["meta"]["trend_src"] == "mixed"
    live = lv.update(book(), 0.5, [], 1002.0)
    assert live["meta"]["trend_src"] == "live"     # the no-kwargs default


# --------------------------------------------------- the tape-lead gate -----

def test_tape_confirms_requires_the_tape_to_agree():
    assert tape_confirms("LONG", 1)
    assert tape_confirms("BEAR", -1)
    assert not tape_confirms("LONG", -1)           # tape opposes
    assert not tape_confirms("LONG", 0)            # tape flat
    assert not tape_confirms("BEAR", None)         # tape silent/warming
