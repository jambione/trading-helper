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
