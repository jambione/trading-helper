"""The price clock is stamped by the clock that measured the age.

poll_once prices ~30 names in turn with now = the poll's start; on 2026-09-25
polls ran 17-36 s, and stamping `now - age` made late names' prints look older
by the poll's elapsed time (desk median 24 s vs dashboard 10.7 s), which drove
most of the 43% tape_only refusals.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_entry_watch as ew  # noqa: E402
import desk_io  # noqa: E402


def test_stamp_uses_the_measuring_wall_clock(monkeypatch):
    t0 = 1_790_000_000.0
    monkeypatch.setattr(ew, "decision_price", lambda sym, cfg, now: (25.0, "stream", 5.0))
    monkeypatch.setattr(ew.time, "time", lambda: t0 + 20.0)  # 20 s into the poll
    rec = {"symbol": "ZZZT"}
    ew.apply_decision_price(rec, {}, t0)
    assert rec["last_ask_ts"] == t0 + 15.0
    assert ew._LAST_QUOTE_TS["ZZZT"] == t0 + 15.0
    assert abs(ew.row_quote_age_sec(rec, now=t0 + 20.0) - 5.0) < 1e-9


def test_replay_pass_clock_follows_served_reads(tmp_path):
    import gzip
    import json
    wire = tmp_path / "wire.jsonl.gz"
    with gzip.open(wire, "wt") as f:
        f.write(json.dumps({"ts": 1012.5, "ch": "alpaca", "m": "GET", "p": "https://x/v2/q",
                            "q": {"s": 1}, "r": 1, "pt": 1000.0}) + "\n")
    rec = desk_io.Recording(wire)
    desk_io.set_pass(1000.0)
    assert desk_io.pass_clock() is None
    hit = rec.alpaca_at(desk_io.alpaca_key("GET", "/v2/q", {"s": 1}), 1000.0, 1000.0)
    assert hit["r"] == 1 and desk_io.pass_clock() == 1012.5
    desk_io.set_pass(2000.0)
    assert desk_io.pass_clock() is None
    desk_io.set_pass(None)
