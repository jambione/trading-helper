"""rehearse_open reconstructs the book minute by minute from the desk's ledgers."""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools"))
import rehearse_open as ro  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(hhmm, s=0):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 9, 24, h, m, s, tzinfo=ET).timestamp()


def _w(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_bucket():
    assert ro.bucket("wait_mid_rise") == "armable"
    assert ro.bucket("spread_wide") == "gate"
    assert ro.bucket("stale_quote") == "data"
    assert ro.bucket("tape_only") == "data"
    assert ro.bucket("anything", ok=True) == "armable"
    assert ro.bucket("weird") == "other"


def test_replay_counts_book_blocks_and_trades(tmp_path):
    rep = str(tmp_path)
    _w(os.path.join(rep, "events.jsonl"), [
        {"ts": _ts("09:40", 5), "kind": "admit_funnel", "n_candidates": 12,
         "kept_symbols": ["AAA", "BBB", "CCC"]},
        {"ts": _ts("09:41"), "kind": "arm_recheck", "symbol": "AAA", "stage": "pass"},
        {"ts": _ts("09:41", 7), "kind": "entry_ok", "symbol": "AAA"},
        {"ts": _ts("09:42"), "kind": "watch_skip", "symbol": "ZZZ", "reason": "above_max_price"},
    ])
    _w(os.path.join(rep, "decision_ledger", "2026-09-24.jsonl"), [
        {"ts": _ts("09:40", 10), "stage": "arm", "symbol": "AAA", "arm_why": "wait_mid_rise",
         "arm_ok": False, "tape_age_sec": 4.0},
        {"ts": _ts("09:40", 11), "stage": "arm", "symbol": "BBB", "arm_why": "spread_wide",
         "arm_ok": False, "tape_age_sec": 8.0},
        {"ts": _ts("09:40", 12), "stage": "arm", "symbol": "CCC", "arm_why": "stale_quote",
         "arm_ok": False, "tape_age_sec": 40.0},
    ])
    _w(os.path.join(rep, "admit_ledger", "2026-09-24.jsonl"), [
        {"ts": _ts("09:40", 1), "stage": "inclusion", "symbol": "DDD", "kept": False,
         "reason": "below_min_price"},
        {"ts": _ts("09:40", 2), "stage": "seed", "symbol": "EEE", "kept": False,
         "reason": "thin_rvol", "source": "movers"},
    ])
    res = ro.replay("2026-09-24", "09:40", "09:45", 5, rep)
    r = res["rows"][0]
    assert (r["book"], r["armable"], r["gate"], r["data"]) == (3, 1, 1, 1)
    assert r["cand"] == 12 and r["tape_age_p50"] == 8.0
    assert dict(r["top_incl"]) == {"below_min_price": 1}
    assert dict(r["top_seed"]) == {"movers:thin_rvol": 1}
    kinds = [e["kind"] for e in res["events"]]
    assert kinds == ["arm_recheck", "entry_ok"]          # price-ceiling skips filtered
    crit = dict((n, ok) for n, ok, _ in ro.criteria(res))
    assert crit["every arm reached an order"] is True
    assert crit["book >= 10 by 09:40"] is False


def test_volume_clock_candidates_rescore_logged_rvol(tmp_path):
    import rehearse_whatif as rw
    rep = str(tmp_path)
    _w(os.path.join(rep, "admit_ledger", "2026-09-24.jsonl"), [
        # 09:52: logged 0.69 against the live 22 minutes -> fixed ~1.29, passes
        {"ts": _ts("09:52"), "stage": "seed", "symbol": "PFE", "reason": "thin_rvol",
         "kept": False, "source": "movers", "rvol": 0.69},
        # 11:30: the lag barely matters late; 0.5 stays under 1.0
        {"ts": _ts("11:30"), "stage": "seed", "symbol": "SLOW", "reason": "thin_rvol",
         "kept": False, "source": "movers", "rvol": 0.5},
        # trending rows are not affected by the movers clock
        {"ts": _ts("09:52"), "stage": "seed", "symbol": "TRD", "reason": "thin_rvol",
         "kept": False, "source": "trending", "rvol": 0.9},
    ])
    got = rw.volume_clock_candidates("2026-09-24", "09:30", "12:00", rep)
    assert set(got) == {"PFE"}
    assert 1.2 < got["PFE"][2] < 1.4
