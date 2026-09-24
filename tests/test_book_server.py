"""book_server runway ranking + shadow mode."""
import math
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import book_server as bs


def test_pace_term_soft_caps_at_4x():
    assert bs.pace_term(None) == 0.0
    assert bs.pace_term(0.5) < bs.pace_term(1.64)
    assert bs.pace_term(1.64) < bs.pace_term(2.5)
    assert bs.pace_term(2.5) < bs.pace_term(4.0)
    assert abs(bs.pace_term(4.0) - bs.pace_term(10.0)) < 1e-9  # soft-cap


def test_closeness_peaks_near_minus_50():
    assert bs.closeness_to_cross(-100) < bs.closeness_to_cross(-60)
    assert bs.closeness_to_cross(-60) < bs.closeness_to_cross(-50)
    assert bs.closeness_to_cross(-50) > bs.closeness_to_cross(-30)


def test_runway_movers_morning_bump():
    # 09:40 ET on an arbitrary day
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = datetime(2026, 9, 24, 9, 40, tzinfo=et).timestamp()
    morning = bs.runway_score(day_chg_pct=5.0, source="movers", now=now, include_pace=False)
    late = bs.runway_score(
        day_chg_pct=5.0, source="movers",
        now=datetime(2026, 9, 24, 14, 0, tzinfo=et).timestamp(),
        include_pace=False,
    )
    assert morning > late  # morning bump


def test_rank_prefers_near_cross_with_pace():
    rows = [
        {"symbol": "FAR", "source": "research", "day_chg_pct": 2.0,
         "indicator": {"pctr": -90.0}, "rvol_pace_sip": 0.5},
        {"symbol": "NEAR", "source": "movers", "day_chg_pct": 6.0,
         "indicator": {"pctr": -52.0}, "rvol_pace_sip": 2.0,
         "dist_hod_pct": -3.0},
        {"symbol": "MOM", "source": "momentum", "day_chg_pct": 20.0,
         "indicator": {"pctr": -51.0}, "rvol_pace_sip": 5.0},  # filtered out
    ]
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 9, 24, 10, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()
    ranked = bs.rank_candidates(rows, now=now, limit=10)
    syms = [r["symbol"] for r in ranked]
    assert "MOM" not in syms  # momentum not in supply
    assert syms[0] == "NEAR"


def test_shadow_tick_writes_log(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "_shadow_path", lambda day=None: tmp_path / "shadow.jsonl")
    monkeypatch.setattr(bs, "_last_shadow", {"key": None, "ts": 0.0})
    rows = [
        {"symbol": "AAA", "source": "movers", "day_chg_pct": 4.0,
         "indicator": {"pctr": -55.0}, "rvol_pace_sip": 1.8},
    ]
    would = bs.shadow_tick(rows, cfg={"ai_book_server_mode": "shadow"},
                           live_book=["BBB"], max_seats=5)
    assert would and would[0]["symbol"] == "AAA"
    text = (tmp_path / "shadow.jsonl").read_text()
    assert "AAA" in text and "shadow_book" in text


def test_mode_defaults_off():
    assert bs.mode({}) == "off"
    assert bs.mode({"ai_book_server_mode": "shadow"}) == "shadow"
    assert bs.is_live({"ai_book_server_mode": "live"})


def _at(hh, mm):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime(2026, 9, 24, hh, mm, tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_inputs_read_the_fields_live_rows_carry():
    """Movers/research rows carry pct_change, not day_chg_pct (2026-09-24)."""
    mv = bs.row_inputs({"symbol": "PFE", "source": "movers",
                        "pct_change": 4.2, "rvol": 2.1})
    assert mv["chg"] == 4.2
    assert (mv["pace"], mv["pace_src"]) == (2.1, "movers_sip")
    # Trending rvol is an IEX ratio, not the SIP pace statistic.
    tr = bs.row_inputs({"symbol": "KR", "source": "trending", "pct": 0.0,
                        "rvol": 3.0})
    assert tr["chg"] == 0.0          # a flat day is a value, not a miss
    assert tr["pace"] is None and tr["pace_src"] is None
    assert tr["pctr"] is None


def test_inputs_take_pctr_from_engine_map_and_pace_from_cache():
    got = bs.row_inputs({"symbol": "kr", "source": "trending"},
                        indicators={"KR": {"pctr": -58.0}}, paces={"KR": 1.9})
    assert got["pctr"] == -58.0
    assert (got["pace"], got["pace_src"]) == (1.9, "pace_sip")


def test_rank_puts_unknown_pctr_last_and_does_not_mutate():
    rows = [
        {"symbol": "HOT", "source": "movers", "pct_change": 12.0, "rvol": 3.5},
        {"symbol": "NEAR", "source": "trending", "pct": 1.0},
    ]
    before = [dict(r) for r in rows]
    ranked = bs.rank_candidates(rows, now=_at(10, 30),
                                indicators={"NEAR": {"pctr": -55.0}})
    assert [r["symbol"] for r in ranked] == ["NEAR", "HOT"]
    assert rows == before


def test_shadow_logs_on_change_or_heartbeat(tmp_path, monkeypatch):
    import json
    path = tmp_path / "shadow.jsonl"
    monkeypatch.setattr(bs, "_shadow_path", lambda day=None: path)
    monkeypatch.setattr(bs, "_last_shadow", {"key": None, "ts": 0.0})
    rows = [{"symbol": "AAA", "source": "movers", "pct_change": 3.0}]
    t = _at(10, 30)
    bs.shadow_tick(rows, now=t, live_book=["AAA"])
    bs.shadow_tick(rows, now=t + 2, live_book=["AAA"])     # unchanged: quiet
    bs.shadow_tick(rows, now=t + 4, live_book=["BBB"])     # live set moved
    bs.shadow_tick(rows, now=t + 70, live_book=["BBB"])    # heartbeat
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert len(lines) == 3
    assert lines[0]["coverage"] == {"pctr": 0, "pace": 0, "chg": 1}
    assert lines[0]["top"][0]["pctr"] is None
