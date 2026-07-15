"""Max-trust confidence upgrade (webull-l2): unified tape gate, enriched
tape metrics, signal_quality grading, VWAP maturity gate, the staged
BookFlow 'book' pillar, and the score_confidence raw-value/sweep plumbing.
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webull-l2"))

import score_confidence as sc  # noqa: E402
from l2_core import (BookFlow, L2Book, LongView, confidence,  # noqa: E402
                     market_bias, playbook, quality_grade, signal_quality,
                     tape_gate_ok)
from tape_core import Tape  # noqa: E402


def book(bid=5.00, ask=5.05, ask_wall=None, ts=1000.0):
    """3-level book; ask_wall=(price, size) plants a wall on the ask side."""
    bids = [(bid, 100.0), (bid - 0.05, 100.0), (bid - 0.10, 100.0)]
    asks = [(ask, 100.0), (ask + 0.05, 100.0), (ask + 0.10, 100.0)]
    if ask_wall:
        asks = sorted(asks + [ask_wall], key=lambda x: x[0])
    return L2Book(bids, asks, ts=ts)


def tape_dict(buy=0.0, sell=0.0, unsided=0.0, sided_n=0, dom=None):
    total = buy + sell + unsided
    sided = buy + sell
    return {"n": sided_n + (1 if unsided else 0), "sided_n": sided_n,
            "buy": buy, "sell": sell, "total": total,
            "dom": dom if dom is not None
            else ((buy - sell) / sided if sided else 0.0)}


# ------------------------------------------------------- unified tape gate --

def test_tape_gate_volume_aware():
    # a few large clearly-sided prints pass...
    assert tape_gate_ok(tape_dict(buy=5000, sell=1000, sided_n=4))
    # ...but a tape of mostly unsided noise abstains even with many prints
    assert not tape_gate_ok(tape_dict(buy=100, sell=100, unsided=5000,
                                      sided_n=10))
    # and too few sided prints abstains regardless of volume
    assert not tape_gate_ok(tape_dict(buy=9000, sell=0, sided_n=3))


def test_gate_consistent_across_all_consumers():
    """The banner must never abstain while bias/playbook still speak."""
    noisy = tape_dict(buy=100, sell=100, unsided=5000, sided_n=10, dom=0.9)
    b = book()
    conf = confidence(0.5, noisy, b.mid, None)
    assert conf["votes"]["tape"] is None            # banner abstains
    with_t, _, _ = market_bias(b, 0.0, 0.0, tape=noisy)
    without_t, _, _ = market_bias(b, 0.0, 0.0, tape=None)
    assert with_t == without_t                      # bias ignores it too
    assert not playbook(b, 0.1, 0.1, [], noisy)["tape_ok"]


# ------------------------------------------------------ enriched Tape -------

def test_tape_metrics_block_and_weighted_dominance():
    t = Tape()
    rows = [("10:00:03", 5.05, 5000, "S"),      # colored block sell
            ("10:00:02", 5.06, 200, "N"),       # at ask -> quote-rule buy
            ("10:00:01", 5.05, 100, "B")]       # colored buy
    t.ingest_frame(rows, 1000.0, bid=5.00, ask=5.06)
    # downtick from 5.05 -> tick-rule sell (the guess, weighted 0.5)
    t.ingest_frame([("10:00:04", 5.03, 100, "N")] + rows, 1001.0,
                   bid=5.00, ask=5.06)
    srcs = [p[4] for p in t.prints]
    assert srcs == ["C", "Q", "C", "T"]
    m = t.metrics(1001.0, 60)
    assert m["dom_big"] == -1.0 and m["big_n"] == 1   # only the 5000 block
    wb, ws = 300.0, 5000.0 + 0.5 * 100
    assert abs(m["dom_w"] - (wb - ws) / (wb + ws)) < 1e-9
    assert m["pace_n"] == 4.0 and m["pace_vol"] == 5400.0
    assert len(t.prints_since(1000.5)) == 1


# ------------------------------------------------------ signal quality ------

def test_signal_quality_grades():
    q, why = signal_quality(frame_age=1.0, ok_rate=1.0, glitch_rate=0.0,
                            spread_pct=0.2)
    assert quality_grade(q) == "A" and not why
    # the frame-skip staleness hole: frozen pixels must cost the grade
    q2, why2 = signal_quality(frame_age=45.0, ok_rate=1.0, spread_pct=0.2)
    assert q2 == 0.5 and quality_grade(q2) == "B"
    assert "frozen" in why2[0]
    q3, _ = signal_quality(frame_age=120.0, ok_rate=0.4, spread_pct=2.5)
    assert quality_grade(q3) == "C"


def test_signal_quality_sdk_book_skips_ocr_penalties():
    # SDK book: frozen-frame / OCR penalties are moot, spread still counts
    q, why = signal_quality(frame_age=120.0, ok_rate=0.3, glitch_rate=0.5,
                            spread_pct=1.5, sdk_book=True)
    assert q == 0.7 and why == ["spread 1.5%"]


def test_signal_quality_spoof_ding():
    q1, _ = signal_quality(spoof_events=1)
    q2, why = signal_quality(spoof_events=2)
    assert q1 == 1.0 and q2 == 0.7
    assert "spoof" in why[0]


# ------------------------------------------------------ vwap maturity -------

def test_vwap_pillar_abstains_until_mature():
    tp = tape_dict(buy=5000, sell=0, sided_n=5)
    young = confidence(0.5, tp, 5.0, 4.9, vwap_age=60, vwap_min_age=180)
    assert young["votes"]["vwap"] is None and young["total"] == 2
    mature = confidence(0.5, tp, 5.0, 4.9, vwap_age=200, vwap_min_age=180)
    assert mature["votes"]["vwap"] == 1 and mature["total"] == 3
    # default behavior unchanged for old callers (no age passed)
    legacy = confidence(0.5, tp, 5.0, 4.9)
    assert legacy["votes"]["vwap"] == 1


# ------------------------------------------------------ staged book pillar --

def test_book_pillar_staged_off_by_default():
    tp = tape_dict(buy=5000, sell=0, sided_n=5)
    off = confidence(0.5, tp, 5.0, 4.9, book_vote=1)
    assert "book" not in off["votes"] and off["total"] == 3
    on = confidence(0.5, tp, 5.0, 4.9, book_vote=1, book_pillar=True)
    assert on["votes"]["book"] == 1
    assert on["agree"] == 4 and on["total"] == 4


def test_longview_carries_book_pillar_flag():
    lv = LongView({"book_pillar": True, "long_confirm_secs": 0})
    out = lv.update(book(), 0.5, [], 1000.0,
                    tape=tape_dict(buy=5000, sell=0, sided_n=5),
                    vwap=4.9, book_vote=1)
    assert out["book_pillar"] and out["votes"]["book"] == 1


# ------------------------------------------------------ BookFlow ------------

WALL = (5.20, 2000.0)


def test_bookflow_wall_consumed_by_real_prints():
    bf = BookFlow()
    bf.update(book(ask_wall=WALL), [], 0.0)
    # 1200 shares actually trade at the wall price, then it disappears
    prints = [(1.0, 5.20, 1200.0, "B")]
    for i in range(bf.gone_reads):
        out = bf.update(book(), prints if i == 0 else [], 2.0 + i)
    assert out["events"] == [("ASK", 5.20, "CONSUMED")]
    assert out["vote"] == 1            # supply eaten through = bullish
    assert out["spoof_ask"] == 0


def test_bookflow_wall_pulled_untraded_is_spoof():
    bf = BookFlow()
    bf.update(book(ask_wall=WALL), [], 0.0)
    for i in range(bf.gone_reads):
        out = bf.update(book(), [], 2.0 + i)
    assert out["events"] == [("ASK", 5.20, "PULLED")]
    assert out["spoof_ask"] == 1
    assert out["vote"] != 1            # a pulled wall is never a buy signal


def test_bookflow_tolerates_ocr_flicker():
    bf = BookFlow()
    bf.update(book(ask_wall=WALL), [], 0.0)
    out1 = bf.update(book(), [], 1.0)              # missing once
    out2 = bf.update(book(), [], 2.0)              # missing twice
    out3 = bf.update(book(ask_wall=WALL), [], 3.0)  # it was OCR flicker
    assert out1["events"] == out2["events"] == out3["events"] == []
    assert ("ASK", 5.20) in bf.walls               # still tracked, no verdict


def test_bookflow_absorption_at_the_bid():
    bf = BookFlow()
    out = None
    # 35s of heavy selling into a bid that will not budge
    for i in range(8):
        ts = i * 5.0
        out = bf.update(book(), [(ts, 5.00, 500.0, "S")], ts)
    assert out["absorb"] == 1
    assert out["vote"] == 1            # accumulation into the hits


# --------------------------------------------- score_confidence plumbing ----

OLD_FIELDS = ["time", "symbol", "best_bid", "best_ask", "spread_pct",
              "bid_size", "ask_size", "imbalance", "signal", "reason",
              "stance", "agree", "total", "tape_live",
              "trend_vote", "tape_vote", "vwap_vote"]


def _write_csv(path, fields, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(fields)
        w.writerows(rows)


def test_load_rows_tolerates_old_and_new_schemas(tmp_path):
    old = tmp_path / "old.csv"
    _write_csv(old, OLD_FIELDS,
               [["2026-07-13T10:00:00.000", "TEST", "5.0000", "5.0500",
                 "1.000", "300", "300", "1.000", "", "", "LONG", "3", "3",
                 "1", "1", "1", "1"]])
    new = tmp_path / "new.csv"
    _write_csv(new, OLD_FIELDS + ["t5", "tape_dom", "tape_dom_big",
                                  "tape_dom_w", "tape_sided_n",
                                  "tape_sided_share", "tape_pace",
                                  "tape_accel", "vwap", "tape_age",
                                  "quality", "flow", "absorb"],
               [["2026-07-13T10:00:01.000", "TEST", "5.0000", "5.0500",
                 "1.000", "300", "300", "1.000", "", "", "LONG", "3", "3",
                 "1", "1", "1", "1", "0.5", "0.4", "1.0", "0.35", "6",
                 "0.8", "1200.0", "1.5", "5.01", "240", "0.85",
                 "ASK 5.2000 PULLED", "1"]])
    rows = sc.load_rows([old, new])
    assert len(rows) == 2
    o, n = rows
    assert o["tape_dom"] is None and o["quality"] is None
    assert n["tape_dom"] == 0.4 and n["quality"] == 0.85
    assert n["tape_sided_n"] == 6 and n["agree"] == 3


def test_sweep_vote_derivation_respects_gate():
    r = {"tape_dom": 0.30, "tape_sided_n": 6, "tape_sided_share": 0.8,
         "mid": 5.0, "vwap_raw": 4.9}
    assert sc._vote_from_raw(r, "tape_dom_min", 0.25) == 1
    assert sc._vote_from_raw(r, "tape_dom_min", 0.35) == 0
    assert sc._vote_from_raw(r, "vwap_deadband", 0.0005) == 1
    gated = dict(r, tape_sided_n=2)     # too few sided prints -> abstain
    assert sc._vote_from_raw(gated, "tape_dom_min", 0.25) is None


def test_quality_condition_buckets():
    labels = dict(sc._cond_labels({"agree": 3, "total": 3,
                                   "spread_pct": 0.2, "tape_live": 1,
                                   "quality": 0.85}))
    assert labels["agree"] == "3/3"
    assert labels["spread"] == "<0.30%"
    assert labels["quality"] == "grade A"
