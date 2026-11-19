import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import book_coverage_watch as B  # noqa: E402


def _snap(px, pc, pv, bid=None, ask=None):
    return {"latestTrade": {"p": px}, "prevDailyBar": {"c": pc, "v": pv},
            "latestQuote": {"bp": bid if bid is not None else px - 0.01, "ap": ask if ask is not None else px + 0.01}}


def test_leaders_filters_price_liquidity_and_ranks():
    snaps = {"CRWD": _snap(240.0, 220.0, 3e6), "PENNY": _snap(5.0, 4.0, 1e8), "BIG": _snap(500.0, 450.0, 1e6),
             "THIN": _snap(50.0, 45.0, 1e5), "FLAT": _snap(100.0, 99.5, 1e6), "TTWO": _snap(222.0, 203.7, 2e6)}
    top = B.leaders(snaps)
    assert [r["symbol"] for r in top] == ["CRWD", "TTWO"]     # +9.09% then +8.98%; PENNY/BIG out on price, THIN on $vol, FLAT < 1.5%
    assert top[0]["spread_bp_iex"] is not None


def test_coverage_statuses_and_blockers():
    top = [{"symbol": s, "pct": p} for s, p in (("A", 9.0), ("B", 8.0), ("C", 7.0), ("D", 6.0))]
    reasons = {"B": Counter({"above_max_price": 40, "no_tape": 3}), "C": Counter({"admit_range_pos": 5})}
    cov = B.coverage(top, seated={"A"}, reasons=reasons, kept={"D"})
    assert cov["seated"] == 1 and cov["missing"] == 3 and cov["coverage_pct"] == 25.0
    st = {m["symbol"]: m["status"] for m in cov["missing_detail"]}
    assert st == {"B": "refused", "C": "refused", "D": "kept_earlier"}
    assert cov["blockers"] == {"above_max_price": 1, "admit_range_pos": 1, "kept_earlier": 1}


def test_ledger_tail_is_incremental(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "LEDGER_DIR", tmp_path)
    p = tmp_path / "2026-11-19.jsonl"
    p.write_text(json.dumps({"symbol": "X", "reason": "no_tape", "kept": False}) + "\n"
                 + json.dumps({"symbol": "Y", "kept": True}) + "\n" + '{"symbol": "Z", "rea')
    t = B.LedgerTail("2026-11-19")
    t.update()
    assert t.reasons["X"]["no_tape"] == 1 and t.kept == {"Y"} and "Z" not in t.reasons
    with open(p, "a") as fh:
        fh.write('son": "spread_wide"}\n' + json.dumps({"symbol": "X", "reason": "no_tape"}) + "\n")
    t.update()
    assert t.reasons["Z"]["spread_wide"] == 1 and t.reasons["X"]["no_tape"] == 2


def test_enrich_and_catalysts():
    from datetime import datetime
    now = datetime(2026, 11, 19, 10, 9, tzinfo=B.ET)            # 39 min in -> 10% of the day
    s = {"prevDailyBar": {"c": 100.0, "v": 1e6}, "dailyBar": {"o": 103.0, "v": 3e5},
         "latestTrade": {"p": 105.0}, "latestQuote": {"bp": 104.99, "ap": 105.01}}
    e = B.enrich("X", s, now)
    assert e["pct"] == 5.0 and e["gap_pct"] == 3.0 and abs(e["vol_pace"] - 3.0) < 1e-9
    assert B.catalyst_tags(e, "08:12") == ["news 08:12", "vol 3.0x", "gap +3.0%"]
    assert B.catalyst_tags({"vol_pace": 1.2, "gap_pct": 0.5}, None) == []


def test_purity_statuses():
    rows = {"A": {"symbol": "A", "vol_pace": 3.0}, "B": {"symbol": "B"}, "C": {"symbol": "C", "gap_pct": 4.0}}
    ranked = ["A"] + ["Z%d" % i for i in range(40)] + ["C"]
    p = B.purity({"A", "B", "C"}, rows, ranked, news={})
    st = {b["symbol"]: (b["status"], b["good"]) for b in p["book"]}
    assert st == {"A": ("leader", True), "C": ("mover", False), "B": ("not_a_mover", False)}
    assert p["purity_pct"] == round(100 / 3, 1) and p["book_leaders"] == 1


def test_position_intervals_and_opens():
    f = [{"symbol": "A", "side": "buy", "qty": 1, "price": 10.0, "t": 100},
         {"symbol": "A", "side": "buy", "qty": 5, "price": 10.1, "t": 150},     # adds to the open position
         {"symbol": "A", "side": "sell", "qty": 6, "price": 10.2, "t": 400},
         {"symbol": "A", "side": "buy", "qty": 70, "price": 10.0, "t": 900},    # a new open from flat
         {"symbol": "B", "side": "buy", "qty": 1, "price": 50.0, "t": 950}]
    iv = B.position_intervals(f)
    assert [(x["symbol"], x["t_open"], x["t_close"]) for x in iv] == [("A", 100, 400), ("A", 900, None), ("B", 950, None)]
    o = B.opens_summary(iv, now=1200, day_start=0)
    assert o["today"] == 3 and o["last_10m"] == 2 and o["open_now"] == 2      # the t=100 open is 15 min old
    assert o["share_ge1_pct"] > 0 and o["share_ge2_pct"] > 0


def test_timing_card():
    bars = [{"t": 60 * k, "h": 10 + k * 0.1, "l": 9.9 + k * 0.1, "c": 10 + k * 0.1} for k in range(30)]
    iv = {"symbol": "A", "t_open": 60 * 25 + 5, "px_open": 12.45, "qty": 1, "t_close": 60 * 29, "px_close": 12.5}
    c = B.timing_card(iv, bars)
    assert c["range_pos"] is not None and c["min_since_low"] == 25 and c["green_3m"] is True
    assert c["result_bp"] > 0 and c["held_min"] > 0
