"""Pairing, verdicts and completeness for the evening replay batch scorer. No market data."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "studies"))

import no_green_scratch_score as S  # noqa: E402


def _t(sym, ts, ret, reason="local_trail"):
    return {"symbol": sym, "entry_ts": ts, "exit_ts": ts + 60, "ret": ret, "reason": reason}


def test_pair_nearest_within_window_once():
    c = [_t("A", 100, 0.0), _t("A", 1000, 0.0), _t("B", 100, 0.0)]
    a = [_t("A", 150, 0.1), _t("A", 120, 0.2), _t("A", 1200, 0.3), _t("C", 100, 0.0)]
    got = [(x["entry_ts"], y["entry_ts"]) for x, y in S.pair(c, a)]
    assert got == [(100, 120)]


def test_scratch_verdict_rules():
    hm, hn = {"A": 0.0001, "B": 0.0001}, {"A": 30, "B": 30}
    assert S.verdict(0.0002, 2.1, hm, hn, 6, 0.95) == "PASS"
    assert S.verdict(0.0002, 1.9, hm, hn, 6, 0.95) == "FAIL"
    assert S.verdict(0.0002, 2.1, {"A": 0.0001, "B": -0.0001}, hn, 6, 0.95) == "FAIL"
    assert S.verdict(0.0002, 2.1, hm, hn, 5, 0.95) == "FAIL"
    assert S.verdict(0.0002, 2.1, hm, hn, 6, 0.85) == "FAIL"
    assert S.verdict(0.0002, 2.1, hm, {"A": 29, "B": 30}, 6, 0.95) == "UNDERPOWERED"


def test_nomom_and_keepfall_verdicts():
    hm, hn = {"A": 0.0001, "B": 0.0001}, {"A": 50, "B": 50}
    assert S.nomom_verdict(0.0002, 2.1, hm, hn, 6, 0.9) == "PASS"
    assert S.nomom_verdict(0.0002, 2.1, hm, hn, 6, 0.89) == "FAIL"
    assert S.keepfall_verdict(0.2, 2.5, 1.12, {"A": -0.00005, "B": 0.0}, hn) == "PASS"
    assert S.keepfall_verdict(0.2, 2.5, 1.12, {"A": -0.00015, "B": 0.0}, hn) == "FAIL"   # worse trades
    assert S.keepfall_verdict(0.2, 2.5, 1.05, {"A": 0.0, "B": 0.0}, hn) == "FAIL"         # < +10% opens
    assert S.keepfall_verdict(0.2, 2.5, 1.12, {"A": 0.0, "B": 0.0}, {"A": 49, "B": 60}) == "UNDERPOWERED"


def test_opens_count_open_positions_and_charging():
    res = {"closed": [_t("A", 1, 0.002), _t("B", 2, 0.0)], "open_at_end": [{}], "slots": {"1": {}, "2": {}, "3": {}}}
    assert S.opens_per_10m(res) == 1.0
    assert abs(S.charged_total(res, 0.001) - 0.0) < 1e-12


def _write(tmp_path, arms, sha="abc1234", cfg="c0ffee"):
    for i, day in enumerate(S.DAYS):
        for arm in arms:
            reason = "no_progress" if arm in ("np90", "np60") else "local_trail"
            closed = [_t("X", 1000 + j * 600, -0.001, reason) for j in range(8)]
            json.dump({"closed": closed, "open_at_end": [], "slots": {str(k): {} for k in range(39)},
                       "sha": sha, "set_json_sha": cfg}, open(tmp_path / f"{day}_{arm}.json", "w"))


def test_incomplete_batches_print_no_verdict(tmp_path):
    _write(tmp_path, ("np180", "np90", "np60", "nomom"))          # keepfall missing
    md = S.score(str(tmp_path), "abc1234")
    assert "INCOMPLETE" in md and "PASS" not in md and "FAIL" not in md


def test_mixed_code_or_config_is_incomplete(tmp_path):
    _write(tmp_path, S.ARMS)
    json.dump({"closed": [], "slots": {}, "sha": "zzz9999", "set_json_sha": "c0ffee"},
              open(tmp_path / f"{S.DAYS[0]}_np90.json", "w"))
    assert "results from other code" in S.score(str(tmp_path), "abc1234")
    _write(tmp_path, S.ARMS)
    json.dump({"closed": [], "slots": {}, "sha": "abc1234", "set_json_sha": None},
              open(tmp_path / f"{S.DAYS[0]}_np90.json", "w"))
    assert "not all pinned" in S.score(str(tmp_path), "abc1234")


def test_complete_batch_scores_and_flags_vacuous(tmp_path):
    _write(tmp_path, S.ARMS)
    md = S.score(str(tmp_path), "abc1234")
    assert "INCOMPLETE" not in md and "## Keep falling seats" in md and "| np90 | 80 |" in md
