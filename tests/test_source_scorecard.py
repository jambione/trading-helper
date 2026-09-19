"""Source scorecard — episodes, overlap views, and the control subtraction.

The failure this tool exists to avoid: a source that proposes the day's biggest
movers scores well even when its picks fade, because it selects on movement.
So the reported number must be `source − control`, and the unit must be the
episode, not the row — otherwise the loudest source wins by re-proposing the
same name all morning rather than by being right.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "tools"))

import source_scorecard as sc


def _row(sym, src, ts, day="2026-09-18", **over):
    r = {"symbol": sym, "proposer_norm": src, "ts": ts, "day": day,
         "decision": "kept", "px": 10.0}
    r.update(over)
    return r


def _prep(rows):
    """load_proposals normalizes; mimic it for hand-built rows."""
    out = []
    for r in rows:
        r = dict(r)
        r["_sym"] = r["symbol"].upper()
        r["_src"] = r["proposer_norm"]
        r["_ts"] = float(r["ts"])
        r["_day"] = r["day"]
        out.append(r)
    return out


# ── Episodes: one opinion, one credit ─────────────────────────────────────────

def test_repeated_proposals_are_one_episode():
    """A source that re-proposes a name every poll has one opinion, not 300.

    Crediting per row would rank sources by how often they poll.
    """
    rows = _prep([_row("AAA", "momentum", 1000 + 20*i) for i in range(50)])
    eps = sc.build_episodes(rows)
    assert len(eps) == 1
    assert eps[0]["ts"] == 1000       # anchored at the FIRST mention


def test_a_re_proposal_after_the_gap_is_a_new_episode():
    rows = _prep([_row("AAA", "momentum", 1000),
                  _row("AAA", "momentum", 1000 + sc.EPISODE_GAP_SEC + 1)])
    assert len(sc.build_episodes(rows)) == 2


def test_two_sources_on_one_symbol_are_two_episodes():
    rows = _prep([_row("AAA", "momentum", 1000),
                  _row("AAA", "trending", 1010)])
    eps = sc.build_episodes(rows)
    assert len(eps) == 2
    assert {e["source"] for e in eps} == {"momentum", "trending"}


def test_episodes_are_per_day():
    rows = _prep([_row("AAA", "momentum", 1000, day="2026-09-17"),
                  _row("AAA", "momentum", 2000, day="2026-09-18")])
    assert len(sc.build_episodes(rows)) == 2


# ── Overlap views ─────────────────────────────────────────────────────────────

def test_exclusive_excludes_shared_symbols():
    rows = _prep([_row("SHARED", "momentum", 1000),
                  _row("SHARED", "trending", 1050),
                  _row("SOLO", "momentum", 1000)])
    eps = sc.build_episodes(rows)
    sc.tag_overlap(eps)
    solo = [e for e in eps if e["symbol"] == "SOLO"][0]
    shared = [e for e in eps if e["symbol"] == "SHARED"]
    assert solo["exclusive"] is True and solo["n_proposers"] == 1
    assert all(e["exclusive"] is False for e in shared)
    assert all(e["n_proposers"] == 2 for e in shared)


def test_first_credits_the_earliest_proposer():
    rows = _prep([_row("AAA", "trending", 1000),
                  _row("AAA", "momentum", 1500)])
    eps = sc.build_episodes(rows)
    sc.tag_overlap(eps)
    first = [e for e in eps if e["is_first"]]
    assert len(first) == 1 and first[0]["source"] == "trending"


# ── The control subtraction — the point of the tool ───────────────────────────

def _scored(sym, src, ts, net, day="2026-09-18"):
    return {"day": day, "symbol": sym, "source": src, "ts": ts, "net": net,
            "mfe": abs(net), "mae": 0.0, "exclusive": True, "is_first": True}


def test_control_is_other_sources_at_the_same_instant():
    by_day = {"2026-09-18": [
        _scored("AAA", "momentum", 1000, 5.0),
        _scored("BBB", "trending", 1010, 1.0),
        _scored("CCC", "trending", 1020, 3.0),
    ]}
    ep = by_day["2026-09-18"][0]
    c = sc._control_for(ep, by_day, same_source=False)
    assert c == pytest.approx(2.0)     # mean of the trending pair, not itself


def test_control_ignores_far_away_instants():
    by_day = {"2026-09-18": [
        _scored("AAA", "momentum", 1000, 5.0),
        _scored("BBB", "trending", 1000 + sc.CONTROL_WINDOW_SEC + 60, 99.0),
    ]}
    assert sc._control_for(by_day["2026-09-18"][0], by_day,
                           same_source=False) is None


def test_a_source_that_only_rides_a_moving_tape_scores_zero():
    """The whole reason controls are mandatory.

    Every name is up 5% — this source picked nothing special, and the delta
    must say so even though its raw return looks excellent.
    """
    scored = [_scored(f"S{i}", "momentum", 1000 + i, 5.0) for i in range(20)]
    scored += [_scored(f"T{i}", "trending", 1000 + i, 5.0) for i in range(20)]
    card = sc.scorecard(scored, "all", 30)
    m = card["sources"]["momentum"]
    assert m["mean_net_pct"] == pytest.approx(5.0)
    assert m["delta_vs_control_pct"] == pytest.approx(0.0, abs=1e-6)


def test_a_source_that_genuinely_picks_better_shows_a_positive_delta():
    scored = [_scored(f"S{i}", "good", 1000 + i, 4.0) for i in range(20)]
    scored += [_scored(f"T{i}", "bad", 1000 + i, 1.0) for i in range(20)]
    card = sc.scorecard(scored, "all", 30)
    assert card["sources"]["good"]["delta_vs_control_pct"] > 0
    assert card["sources"]["bad"]["delta_vs_control_pct"] < 0


# ── Verdicts: THIN dominates ──────────────────────────────────────────────────

def test_a_small_sample_is_thin_not_a_winner():
    """An underpowered positive is the classic way to ship a non-edge."""
    scored = [_scored(f"S{i}", "good", 1000 + i, 9.0) for i in range(4)]
    scored += [_scored(f"T{i}", "bad", 1000 + i, 0.0) for i in range(4)]
    assert sc.scorecard(scored, "all", 30)["sources"]["good"]["verdict"] == "THIN"


def test_verdict_requires_the_powered_sample_size():
    assert sc._verdict(10, 1.0, 0.1, 1.0) == "THIN"
    assert sc._verdict(0, None, 0.0, 0.0) == "EMPTY"


def test_no_difference_is_a_real_verdict():
    v = sc._verdict(500, 0.001, 0.05, 1.0)
    assert v in ("NO_DIFFERENCE", "THIN")


# ── Output contract ───────────────────────────────────────────────────────────

def test_every_card_is_stamped_gross():
    """Never silently assume zero cost — optimize_rstop's existing defect."""
    scored = [_scored(f"S{i}", "m", 1000 + i, 1.0) for i in range(5)]
    assert sc.scorecard(scored, "all", 30)["cost"] == "GROSS"


def test_all_three_views_are_produced():
    rows = _prep([_row("AAA", "momentum", 1000), _row("AAA", "trending", 1010),
                  _row("BBB", "momentum", 1000)])
    eps = sc.build_episodes(rows)
    sc.tag_overlap(eps)
    scored = [{**e, "net": 1.0, "mfe": 1.0, "mae": 0.0} for e in eps]
    for view in ("all", "exclusive", "first"):
        assert sc.scorecard(scored, view, 30)["view"] == view


def test_heartbeat_rows_are_not_decisions(tmp_path, monkeypatch):
    d = tmp_path / "proposal_ledger"
    d.mkdir()
    import json as _json
    (d / "2026-09-18.jsonl").write_text("\n".join(_json.dumps(r) for r in [
        {"symbol": "AAA", "proposer_norm": "momentum", "ts": 1000,
         "day": "2026-09-18", "decision": "kept"},
        {"symbol": "AAA", "proposer_norm": "momentum", "ts": 1100,
         "day": "2026-09-18", "heartbeat": True},
    ]) + "\n")
    monkeypatch.setattr(sc, "ledger_dir", lambda: d)
    assert len(sc.load_proposals(["2026-09-18"])) == 1
